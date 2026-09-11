# Code created by Siddharth Ahuja: www.github.com/ahujasid © 2025

import re
import bpy
import mathutils
import json
import threading
import socket
import queue
import time
import requests
import tempfile
import traceback
import os
import shutil
import uuid
import zipfile
import zlib
from bpy.props import IntProperty, BoolProperty
import io
from datetime import datetime
import hashlib, hmac, base64
import os.path as osp
from collections import deque
from urllib.parse import quote
from contextlib import contextmanager, redirect_stdout, suppress
from bpy.app.handlers import persistent

bl_info = {
    "name": "MCP for Blender",
    "author": "BlenderMCP",
    "version": (1, 6),
    "blender": (3, 0, 0),
    "location": "View3D > Sidebar > MCP for Blender",
    "description": "Connect Blender to Claude via MCP",
    "category": "Interface",
}

# Keep in sync with blender_mcp.addon_manager.EXPECTED_ADDON_PROTOCOL_VERSION.
ADDON_PROTOCOL_VERSION = 5

# Per-snapshot object cap for get_world_state_snapshot. Keep in sync with
# blender_mcp.trajectory.MAX_SNAPSHOT_OBJECTS.
MAX_SNAPSHOT_OBJECTS = 4000

# Selected-name cap for get_world_state_snapshot: select-all in a large scene
# would otherwise make `selected` the dominant field of both step snapshots.
# Keep in sync with blender_mcp.trajectory.MAX_SNAPSHOT_SELECTED.
MAX_SNAPSHOT_SELECTED = 1000

RODIN_FREE_TRIAL_KEY = "vibecoding"

# Add User-Agent as required by Poly Haven API
REQ_HEADERS = requests.utils.default_headers()
REQ_HEADERS.update({"User-Agent": "blender-mcp"})

#region Poly Pizza constants and helpers

POLYPIZZA_API_BASE = "https://api.poly.pizza/v1.1"

# The MCP server resolves human-friendly category/licence names to the numeric
# ids the API filters on, so only ids arrive here. Every query parameter of
# the API is Capitalized (Limit, Page, Category, License, Animated — see
# poly.pizza/apispec/v1.1.yaml): lowercase variants are accepted with HTTP 200
# and then silently ignored, so the capitalisation is load-bearing.


def _polypizza_category_id(category):
    """Validate a numeric category id (names are resolved by the MCP server)."""
    if category is None or category == "":
        return None
    if isinstance(category, bool) or not (
        isinstance(category, int)
        or (isinstance(category, str) and category.strip().lstrip("-").isdigit())
    ):
        raise ValueError(f"Poly Pizza category must be a numeric id in 0-11, got {category!r}")
    value = int(category)
    if not 0 <= value <= 11:
        raise ValueError(f"Poly Pizza category id {value} is out of range (valid ids are 0-11)")
    return value


def _polypizza_licence_id(licence):
    """Validate a numeric licence id (names are resolved by the MCP server)."""
    if licence is None or licence == "":
        return None
    if isinstance(licence, bool) or not (
        isinstance(licence, int)
        or (isinstance(licence, str) and licence.strip().lstrip("-").isdigit())
    ):
        raise ValueError(f"Poly Pizza licence must be 0 (CC-BY) or 1 (CC0), got {licence!r}")
    value = int(licence)
    if value not in (0, 1):
        raise ValueError(f"Poly Pizza licence id {value} is invalid (0 = CC-BY, 1 = CC0)")
    return value


def _polypizza_filter_params(category=None, licence=None, animated=False):
    """Build the query filters for a Poly Pizza search.

    Keys are Capitalized and values numeric because the API silently ignores
    anything else. `Animated` is omitted unless animated-only results were asked
    for: the server treats `Animated=0` as falsy and does not filter on it.
    """
    params = {}
    category_id = _polypizza_category_id(category)
    if category_id is not None:
        params["Category"] = category_id
    licence_id = _polypizza_licence_id(licence)
    if licence_id is not None:
        params["License"] = licence_id
    if animated:
        params["Animated"] = 1
    return params


def _polypizza_summarize_model(model):
    """Trim an API record down to the fields worth sending back over MCP."""
    creator = model.get("Creator") or {}
    return {
        "ID": model.get("ID"),
        "Title": model.get("Title"),
        "Creator": creator.get("Username") if isinstance(creator, dict) else None,
        "Licence": model.get("Licence"),
        "Tri Count": model.get("Tri Count"),
        "Animated": bool(model.get("Animated")),
        "Category": model.get("Category"),
        "Tags": model.get("Tags") or [],
        "Thumbnail": model.get("Thumbnail"),
    }


def _polypizza_cdn_error(status_code, headers, content):
    """Describe a CDN response that is not a GLB, or None when it is one.

    static.poly.pizza sits behind Cloudflare bot management and answers 403 with
    an HTML challenge from datacenter IPs. That is neither an auth failure nor a
    missing model, so it gets its own message.
    """
    if status_code == 200 and content[:4] == b"glTF":
        return None

    headers = headers or {}
    content_type = ""
    for key in ("Content-Type", "content-type"):
        value = headers.get(key)
        if value:
            content_type = str(value).lower()
            break

    challenged = bool(headers.get("cf-mitigated") or headers.get("Cf-Mitigated"))
    looks_like_html = "text/html" in content_type or content[:1] == b"<"

    if challenged or (looks_like_html and status_code != 200):
        return (
            f"Poly Pizza's CDN returned a Cloudflare bot-protection challenge (HTTP {status_code}) "
            "instead of the model file. This is not an API key problem - static.poly.pizza takes no "
            "API key - and the model exists. The CDN blocks datacenter, VPN and cloud IPs; retry from "
            "a residential connection, or download the .glb by hand from https://poly.pizza and import "
            "it with File > Import > glTF 2.0."
        )
    if status_code != 200:
        return f"Poly Pizza model file download failed with status code {status_code}"
    if looks_like_html:
        return (
            "Poly Pizza's CDN returned an HTML page instead of a GLB file. The download link may have "
            "expired; search again to get a fresh one."
        )
    return "Poly Pizza returned a file that is not a valid GLB (missing glTF magic bytes)"

#endregion

#region Manual edit capture
# Records what the human does in Blender while an MCP session is live.

MAX_EDIT_EVENTS = 256

# Operators that fire constantly during interactive work and carry no meaningful
# intent on their own.
_IGNORED_OPERATORS = frozenset({
    "view3d.rotate",
    "view3d.move",
    "view3d.zoom",
    "view3d.dolly",
    "view3d.view_axis",
    "view3d.view_orbit",
    "view3d.view_pan",
    "view3d.smoothview",
    "view3d.cursor3d",
    "wm.tool_set_by_id",
    "wm.context_set_value",
    "screen.animation_step",
})

# Operator properties holding filesystem paths. Never recorded.
_PATH_PROPERTY_NAMES = frozenset({
    "filepath",
    "filename",
    "directory",
    "filepath_raw",
    "relpath",
})
_PATH_PROPERTY_SUBSTRINGS = ("filepath", "filename", "directory", "_dir", "path")
MAX_OPERATOR_PROPERTY_CHARS = 200

# depsgraph_update_post fires on every scene update, many times per second
# during interactive drags.
EDIT_POLL_MIN_INTERVAL = 0.1


def _is_path_property(identifier):
    """True if an operator property likely holds a filesystem path."""
    lowered = identifier.lower()
    if lowered in _PATH_PROPERTY_NAMES:
        return True
    return any(token in lowered for token in _PATH_PROPERTY_SUBSTRINGS)


class UserEditRecorder:
    """Buffers human-originated operator and undo events for the MCP server.

    Anything that happens while an agent command is running is attributed to
    the agent, not the human; `agent_command()` brackets that window.
    """

    def __init__(self):
        self._events = deque(maxlen=MAX_EDIT_EVENTS)
        self._agent_depth = 0
        self._last_operator_count = 0
        self._seen_baseline = False
        self._last_poll_time = 0.0

    @contextmanager
    def agent_command(self):
        """Suppress capture for the duration of an agent-issued command."""
        self._agent_depth += 1
        try:
            yield
        finally:
            self._agent_depth = max(0, self._agent_depth - 1)
            self._resync_operator_baseline()

    @property
    def _suppressed(self):
        return self._agent_depth > 0

    def _operator_stack(self):
        try:
            return list(bpy.context.window_manager.operators)
        except Exception:
            return []

    def _resync_operator_baseline(self):
        self._last_operator_count = len(self._operator_stack())
        self._seen_baseline = True

    def poll_operators(self, now=None):
        """Emit rows for operators run since the last poll. Main thread only.

        Throttled to EDIT_POLL_MIN_INTERVAL.
        """
        if self._suppressed:
            return
        now = time.time() if now is None else now
        if (now - self._last_poll_time) < EDIT_POLL_MIN_INTERVAL:
            return
        self._last_poll_time = now
        stack = self._operator_stack()
        count = len(stack)

        # First poll only establishes a baseline.
        if not self._seen_baseline:
            self._last_operator_count = count
            self._seen_baseline = True
            return

        if count <= self._last_operator_count:
            # Unchanged, or shrank because of an undo. Hold the high-water
            # mark so a later redo does not replay emitted operators.
            return

        for op in stack[self._last_operator_count:count]:
            self._record_operator(op)
        self._last_operator_count = count

    def _record_operator(self, op):
        try:
            bl_idname = getattr(op, "bl_idname", None)
            if not bl_idname:
                return
            # bl_idname is UPPER_CASE_OT_form; normalise to bpy.ops form.
            normalized = bl_idname.lower().replace("_ot_", ".", 1)
            if normalized in _IGNORED_OPERATORS:
                return
            self._events.append({
                "kind": "operator",
                "bl_idname": normalized,
                "name": getattr(op, "name", None),
                "properties": self._operator_properties(op),
                "timestamp": time.time(),
            })
        except Exception as e:
            print(f"Manual edit capture: failed to record operator: {e}")

    @staticmethod
    def _operator_properties(op):
        """Best-effort scalar snapshot of an operator's resolved properties."""
        props = {}
        try:
            rna_props = op.properties.bl_rna.properties
        except Exception:
            return props
        for prop in rna_props:
            if prop.identifier == "rna_type":
                continue
            if _is_path_property(prop.identifier):
                continue
            try:
                value = getattr(op.properties, prop.identifier)
            except Exception:
                continue
            if isinstance(value, str):
                props[prop.identifier] = value[:MAX_OPERATOR_PROPERTY_CHARS]
            elif isinstance(value, (bool, int, float)):
                props[prop.identifier] = value
            elif hasattr(value, "__len__") and not isinstance(value, (dict, bytes)):
                try:
                    items = [
                        v[:MAX_OPERATOR_PROPERTY_CHARS] if isinstance(v, str) else v
                        for v in value
                        if isinstance(v, (bool, int, float, str))
                    ]
                    if items and len(items) <= 16:
                        props[prop.identifier] = items
                except Exception:
                    continue
        return props

    def record_undo(self, kind):
        """Record an undo/redo. This is the strongest rejection signal we get."""
        if self._suppressed:
            return
        self._events.append({
            "kind": kind,
            "timestamp": time.time(),
        })
        # Keep the high-water mark so a redo does not re-emit consumed entries.
        self._last_operator_count = max(
            self._last_operator_count, len(self._operator_stack())
        )
        self._seen_baseline = True

    def drain(self):
        """Hand buffered events to the MCP server and clear them."""
        events = list(self._events)
        self._events.clear()
        return events


_edit_recorder = UserEditRecorder()


def get_edit_recorder():
    return _edit_recorder


@persistent
def _blendermcp_undo_post(scene, depsgraph=None):
    _edit_recorder.record_undo("undo")


@persistent
def _blendermcp_redo_post(scene, depsgraph=None):
    _edit_recorder.record_undo("redo")


@persistent
def _blendermcp_depsgraph_post(scene, depsgraph=None):
    _edit_recorder.poll_operators()


def _telemetry_consent_enabled():
    """Read the consent preference directly. Fails closed."""
    try:
        addon_prefs = bpy.context.preferences.addons.get(__name__)
        if not addon_prefs:
            return False
        return bool(addon_prefs.preferences.telemetry_consent)
    except Exception:
        return False


def _register_edit_capture_handlers():
    """Attach manual-edit handlers, but only with telemetry consent."""
    if not _telemetry_consent_enabled():
        _unregister_edit_capture_handlers()
        return False

    handlers = [
        (bpy.app.handlers.undo_post, _blendermcp_undo_post),
        (bpy.app.handlers.redo_post, _blendermcp_redo_post),
        (bpy.app.handlers.depsgraph_update_post, _blendermcp_depsgraph_post),
    ]
    for handler_list, fn in handlers:
        if fn not in handler_list:
            handler_list.append(fn)
    return True


def sync_edit_capture_handlers():
    """Re-apply the consent gate. Safe to call when consent or server state changes."""
    try:
        server_running = bool(
            getattr(bpy.types, "blendermcp_server", None)
            and bpy.types.blendermcp_server.running
        )
    except Exception:
        server_running = False

    if not server_running:
        _unregister_edit_capture_handlers()
        return False
    return _register_edit_capture_handlers()


def _unregister_edit_capture_handlers():
    handlers = [
        (bpy.app.handlers.undo_post, _blendermcp_undo_post),
        (bpy.app.handlers.redo_post, _blendermcp_redo_post),
        (bpy.app.handlers.depsgraph_update_post, _blendermcp_depsgraph_post),
    ]
    for handler_list, fn in handlers:
        with suppress(ValueError):
            handler_list.remove(fn)
#endregion


def get_blendermcp_addon_preferences(context=None):
    """Get add-on preferences object if available."""
    if context is None:
        context = bpy.context
    addon = context.preferences.addons.get(__name__)
    return addon.preferences if addon else None

class BlenderMCPServer:
    def __init__(self, host='localhost', port=9876):
        self.host = host
        self.port = port
        self.running = False
        self.socket = None
        self.server_thread = None
        # Commands are pushed here by client threads and drained by a single
        # timer running on Blender's main thread. bpy.app.timers is not
        # thread-safe, so registering a timer per command (the previous
        # approach) could silently drop the callback - on Windows especially -
        # leaving the client blocked in recv() until its socket timeout.
        self.command_queue = queue.Queue()
        # Live client sockets, so stop() can unblock threads parked in recv().
        self._clients = set()
        self._clients_lock = threading.Lock()

    def _get_config_value(self, scene_attr, pref_attr=None, env_var=None):
        """Read config in order: addon preferences -> scene -> env var."""
        prefs = get_blendermcp_addon_preferences()
        if prefs and pref_attr:
            pref_value = getattr(prefs, pref_attr, "")
            if pref_value:
                return pref_value

        scene_value = getattr(bpy.context.scene, scene_attr, "")
        if scene_value:
            return scene_value

        if env_var:
            env_value = os.getenv(env_var, "")
            if env_value:
                return env_value
        return ""

    def _get_hyper3d_api_key(self):
        # Let the free-trial button temporarily override persistent keys
        # without overwriting user-saved private keys.
        scene_value = getattr(bpy.context.scene, "blendermcp_hyper3d_api_key", "")
        if scene_value == RODIN_FREE_TRIAL_KEY:
            return scene_value
        return self._get_config_value(
            "blendermcp_hyper3d_api_key",
            "hyper3d_api_key",
            "BLENDERMCP_HYPER3D_API_KEY",
        )

    def _get_sketchfab_api_key(self):
        return self._get_config_value(
            "blendermcp_sketchfab_api_key",
            "sketchfab_api_key",
            "BLENDERMCP_SKETCHFAB_API_KEY",
        )

    def _get_polypizza_api_key(self):
        return self._get_config_value(
            "blendermcp_polypizza_api_key",
            "polypizza_api_key",
            "BLENDERMCP_POLYPIZZA_API_KEY",
        )

    def _get_hunyuan3d_secret_id(self):
        return self._get_config_value(
            "blendermcp_hunyuan3d_secret_id",
            "hunyuan3d_secret_id",
            "BLENDERMCP_HUNYUAN3D_SECRET_ID",
        )

    def _get_hunyuan3d_secret_key(self):
        return self._get_config_value(
            "blendermcp_hunyuan3d_secret_key",
            "hunyuan3d_secret_key",
            "BLENDERMCP_HUNYUAN3D_SECRET_KEY",
        )

    def _get_hunyuan3d_api_url(self):
        return self._get_config_value(
            "blendermcp_hunyuan3d_api_url",
            "hunyuan3d_api_url",
            "BLENDERMCP_HUNYUAN3D_API_URL",
        ) or "http://localhost:8081"

    def start(self):
        if bpy.app.background:
            print("BlenderMCP: cannot start server in background mode (blender -b) - commands would never execute\n"
                  "BlenderMCP: run Blender with a GUI, or use a virtual display: xvfb-run -a blender")
            return

        if self.running:
            print("Server is already running")
            return

        self.running = True

        try:
            # Create socket
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.socket.bind((self.host, self.port))
            # Backlog of 1 meant a reconnecting client could complete the TCP
            # handshake and then never be accept()ed - a connection that looks
            # established but is never serviced.
            self.socket.listen(5)

            # Start server thread
            self.server_thread = threading.Thread(target=self._server_loop)
            self.server_thread.daemon = True
            self.server_thread.start()

            _register_edit_capture_handlers()

            # start() is called from the operator, i.e. the main thread, so
            # this is the only safe place to touch bpy.app.timers.
            if not bpy.app.timers.is_registered(self._drain_command_queue):
                bpy.app.timers.register(self._drain_command_queue, persistent=True)

            print(f"BlenderMCP server started on {self.host}:{self.port}")
        except Exception as e:
            print(f"Failed to start server: {str(e)}")
            self.stop()

    def stop(self):
        self.running = False

        _unregister_edit_capture_handlers()
        get_edit_recorder().drain()

        try:
            if bpy.app.timers.is_registered(self._drain_command_queue):
                bpy.app.timers.unregister(self._drain_command_queue)
        except Exception:
            pass

        # Close socket
        if self.socket:
            try:
                self.socket.close()
            except:
                pass
            self.socket = None

        # Shut down live client sockets. Without this, handler threads stay
        # parked in a blocking recv() forever; being daemon threads they then
        # outlive the restart and close connections the new server owns
        # (the WinError 10054 seen after toggling the addon).
        with self._clients_lock:
            clients = list(self._clients)
            self._clients.clear()
        for client in clients:
            try:
                client.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
            try:
                client.close()
            except Exception:
                pass

        # Drop any commands that will never be serviced now.
        while True:
            try:
                self.command_queue.get_nowait()
            except queue.Empty:
                break

        # Wait for thread to finish
        if self.server_thread:
            try:
                if self.server_thread.is_alive():
                    self.server_thread.join(timeout=1.0)
            except:
                pass
            self.server_thread = None

        print("BlenderMCP server stopped")

    def _server_loop(self):
        """Main server loop in a separate thread"""
        print("Server thread started")
        self.socket.settimeout(1.0)  # Timeout to allow for stopping

        while self.running:
            try:
                # Accept new connection
                try:
                    client, address = self.socket.accept()
                    print(f"Connected to client: {address}")

                    # Handle client in a separate thread
                    client_thread = threading.Thread(
                        target=self._handle_client,
                        args=(client,)
                    )
                    client_thread.daemon = True
                    client_thread.start()
                except socket.timeout:
                    # Just check running condition
                    continue
                except Exception as e:
                    print(f"Error accepting connection: {str(e)}")
                    time.sleep(0.5)
            except Exception as e:
                print(f"Error in server loop: {str(e)}")
                if not self.running:
                    break
                time.sleep(0.5)

        print("Server thread stopped")

    def _drain_command_queue(self):
        """Run queued commands on Blender's main thread.

        Registered once by start(); returns the poll interval so Blender keeps
        calling it. All bpy access happens here, on the main thread.
        """
        if not self.running:
            return None

        while True:
            try:
                command, client = self.command_queue.get_nowait()
            except queue.Empty:
                break

            try:
                response = self.execute_command(command)
                response_json = json.dumps(response)
            except Exception as e:
                print(f"Error executing command: {str(e)}")
                traceback.print_exc()
                response_json = json.dumps({"status": "error", "message": str(e)})

            try:
                client.sendall(response_json.encode('utf-8'))
            except Exception:
                print("Failed to send response - client disconnected")

        return 0.05

    def _handle_client(self, client):
        """Handle connected client"""
        print("Client handler started")
        # A finite timeout keeps this loop responsive to self.running instead
        # of parking in recv() forever.
        client.settimeout(1.0)
        with self._clients_lock:
            self._clients.add(client)
        buffer = b''

        try:
            while self.running:
                # Receive data
                try:
                    data = client.recv(8192)
                    if not data:
                        print("Client disconnected")
                        break

                    buffer += data
                    try:
                        # Try to parse command
                        command = json.loads(buffer.decode('utf-8'))
                        buffer = b''

                        # Hand off to the main thread. Never call
                        # bpy.app.timers.register() from here - it is not
                        # thread-safe and the callback can be silently lost.
                        print(f"Queued command: {command.get('type')}")
                        self.command_queue.put((command, client))
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        # Incomplete data, wait for more. A multi-byte UTF-8
                        # character can land split across a recv() chunk
                        # boundary, which fails decode() before json.loads()
                        # ever runs - that's incomplete data too, not garbage.
                        pass
                except socket.timeout:
                    # Expected; loop round and re-check self.running.
                    continue
                except Exception as e:
                    print(f"Error receiving data: {str(e)}")
                    break
        except Exception as e:
            print(f"Error in client handler: {str(e)}")
        finally:
            with self._clients_lock:
                self._clients.discard(client)
            try:
                client.close()
            except:
                pass
            print("Client handler stopped")

    def execute_command(self, command):
        """Execute a command in the main Blender thread"""
        try:
            with get_edit_recorder().agent_command():
                return self._execute_command_internal(command)

        except Exception as e:
            print(f"Error executing command: {str(e)}")
            traceback.print_exc()
            return {"status": "error", "message": str(e)}

    def _execute_command_internal(self, command):
        """Internal command execution with proper context"""
        cmd_type = command.get("type")
        params = command.get("params", {})

        # Trivial liveness check. Touches no bpy data, so a successful ping
        # alongside a failing command isolates data access from transport.
        if cmd_type == "ping":
            return {"status": "success", "result": {"pong": True}}

        # Add a handler for checking PolyHaven status
        if cmd_type == "get_polyhaven_status":
            return {"status": "success", "result": self.get_polyhaven_status()}

        # Base handlers that are always available
        handlers = {
            "get_scene_info": self.get_scene_info,
            "get_world_state_snapshot": self.get_world_state_snapshot,
            "get_addon_info": self.get_addon_info,
            "get_object_info": self.get_object_info,
            "get_viewport_screenshot": self.get_viewport_screenshot,
            "execute_code": self.execute_code,
            "drain_human_activity": self.drain_human_activity,
            "get_telemetry_consent": self.get_telemetry_consent,
            "set_telemetry_consent": self.set_telemetry_consent,
            "get_polyhaven_status": self.get_polyhaven_status,
            "get_hyper3d_status": self.get_hyper3d_status,
            "get_sketchfab_status": self.get_sketchfab_status,
            "get_polypizza_status": self.get_polypizza_status,
            "get_hunyuan3d_status": self.get_hunyuan3d_status,
        }

        # Add Polyhaven handlers only if enabled
        if bpy.context.scene.blendermcp_use_polyhaven:
            polyhaven_handlers = {
                "get_polyhaven_categories": self.get_polyhaven_categories,
                "search_polyhaven_assets": self.search_polyhaven_assets,
                "download_polyhaven_asset": self.download_polyhaven_asset,
                "set_texture": self.set_texture,
            }
            handlers.update(polyhaven_handlers)

        # Add Hyper3d handlers only if enabled
        if bpy.context.scene.blendermcp_use_hyper3d:
            polyhaven_handlers = {
                "create_rodin_job": self.create_rodin_job,
                "poll_rodin_job_status": self.poll_rodin_job_status,
                "import_generated_asset": self.import_generated_asset,
            }
            handlers.update(polyhaven_handlers)

        # Add Sketchfab handlers only if enabled
        if bpy.context.scene.blendermcp_use_sketchfab:
            sketchfab_handlers = {
                "search_sketchfab_models": self.search_sketchfab_models,
                "get_sketchfab_model_preview": self.get_sketchfab_model_preview,
                "download_sketchfab_model": self.download_sketchfab_model,
            }
            handlers.update(sketchfab_handlers)

        # Add Poly Pizza handlers only if enabled
        if bpy.context.scene.blendermcp_use_polypizza:
            polypizza_handlers = {
                "search_polypizza_models": self.search_polypizza_models,
                "download_polypizza_model": self.download_polypizza_model,
            }
            handlers.update(polypizza_handlers)

        # Add Hunyuan3d handlers only if enabled
        if bpy.context.scene.blendermcp_use_hunyuan3d:
            hunyuan_handlers = {
                "create_hunyuan_job": self.create_hunyuan_job,
                "poll_hunyuan_job_status": self.poll_hunyuan_job_status,
                "import_generated_asset_hunyuan": self.import_generated_asset_hunyuan
            }
            handlers.update(hunyuan_handlers)

        handler = handlers.get(cmd_type)
        if handler:
            try:
                print(f"Executing handler for {cmd_type}")
                result = handler(**params)
                print(f"Handler execution complete")
                return {"status": "success", "result": result}
            except Exception as e:
                print(f"Error in handler: {str(e)}")
                traceback.print_exc()
                return {"status": "error", "message": str(e)}
        else:
            return {"status": "error", "message": f"Unknown command type: {cmd_type}"}



    def get_addon_info(self):
        """Version/capability handshake for the MCP server (and install tooling)."""
        return {
            "name": bl_info.get("name", "MCP for Blender"),
            "addon_version": list(bl_info.get("version", (0, 0))),
            "protocol_version": ADDON_PROTOCOL_VERSION,
            "capabilities": sorted([
                "get_scene_info",
                "get_world_state_snapshot",
                "get_addon_info",
                "get_object_info",
                "get_viewport_screenshot",
                "execute_code",
                "drain_human_activity",
                "get_telemetry_consent",
                "set_telemetry_consent",
            ]),
            "blender_version": bpy.app.version_string,
        }

    def get_scene_info(self):
        """Get information about the current Blender scene"""
        try:
            print("Getting scene info...")
            # Simplify the scene info to reduce data size
            scene_info = {
                "name": bpy.context.scene.name,
                "object_count": len(bpy.context.scene.objects),
                "objects": [],
                "materials_count": len(bpy.data.materials),
            }

            # Collect minimal object information (limit to first 10 objects)
            for i, obj in enumerate(bpy.context.scene.objects):
                if i >= 10:  # Reduced from 20 to 10
                    break

                obj_info = {
                    "name": obj.name,
                    "type": obj.type,
                    # Only include basic location data
                    "location": [round(float(obj.location.x), 2),
                                round(float(obj.location.y), 2),
                                round(float(obj.location.z), 2)],
                }
                scene_info["objects"].append(obj_info)

            print(f"Scene info collected: {len(scene_info['objects'])} objects")
            return scene_info
        except Exception as e:
            print(f"Error in get_scene_info: {str(e)}")
            traceback.print_exc()
            return {"error": str(e)}

    def drain_human_activity(self):
        """Return human-originated events buffered since the last drain.

        Consent is enforced MCP-side (the server only drains and uploads when
        the user has opted in), but we also refuse here so a buffer does not
        accumulate for a user who has said no.
        """
        try:
            if not self.get_telemetry_consent().get("consent"):
                get_edit_recorder().drain()
                return {"events": []}
            return {"events": get_edit_recorder().drain()}
        except Exception as e:
            print(f"Error draining manual edits: {str(e)}")
            return {"error": str(e)}

    @staticmethod
    def _snapshot_geometry(obj):
        """World-space AABB + dimensions for one object, or None.

        Without these, downstream analysis cannot compute contact, containment
        or collision: `scale` alone is a multiplier on unknown base geometry.
        Uses obj.bound_box (8 cached local corners) rather than mesh vertices,
        so cost is constant per object regardless of poly count.
        """
        bound_box = getattr(obj, "bound_box", None)
        if not bound_box:
            return None
        try:
            matrix_world = obj.matrix_world
            xs, ys, zs = [], [], []
            for corner in bound_box:
                world = matrix_world @ mathutils.Vector(corner)
                xs.append(world.x)
                ys.append(world.y)
                zs.append(world.z)
            return {
                "aabb_min": [round(min(xs), 3), round(min(ys), 3), round(min(zs), 3)],
                "aabb_max": [round(max(xs), 3), round(max(ys), 3), round(max(zs), 3)],
                "dimensions": [
                    round(float(obj.dimensions.x), 3),
                    round(float(obj.dimensions.y), 3),
                    round(float(obj.dimensions.z), 3),
                ],
            }
        except Exception:
            return None

    @staticmethod
    def _snapshot_relations(obj):
        """Parent and constraint targets, so hierarchies read correctly.

        World `location` alone misreports parented objects, whose authored
        values are parent-relative.
        """
        relations = {}
        parent = getattr(obj, "parent", None)
        if parent:
            relations["parent"] = parent.name
            relations["parent_type"] = obj.parent_type
            loc = obj.matrix_local.translation
            relations["local_location"] = [
                round(float(loc.x), 3),
                round(float(loc.y), 3),
                round(float(loc.z), 3),
            ]
        constraints = []
        for constraint in getattr(obj, "constraints", None) or []:
            entry = {"type": constraint.type}
            target = getattr(constraint, "target", None)
            if target:
                entry["target"] = target.name
            constraints.append(entry)
            if len(constraints) >= 8:
                break
        if constraints:
            relations["constraints"] = constraints
        modifiers = [m.type for m in (getattr(obj, "modifiers", None) or [])[:8]]
        if modifiers:
            relations["modifiers"] = modifiers
        return relations

    @staticmethod
    def _snapshot_animation(obj):
        """Action name and per-channel keyframe summary for one object, or {}.

        Static transforms alone cannot distinguish an authored edit from
        playback landing on a different frame. Reads F-curve metadata
        (`data_path`, `array_index`, `len(keyframe_points)`) rather than
        individual keyframes, so cost stays proportional to channel count
        rather than to animation length.
        """
        try:
            anim_data = getattr(obj, "animation_data", None)
            if not anim_data:
                return {}

            animation = {}
            action = getattr(anim_data, "action", None)
            if action:
                animation["action"] = action.name
                channels = []
                total_keyframes = 0
                frame_min, frame_max = None, None
                for fcurve in action.fcurves:
                    keyframe_points = fcurve.keyframe_points
                    count = len(keyframe_points)
                    total_keyframes += count
                    if count and len(channels) < 16:
                        channels.append({
                            "data_path": fcurve.data_path,
                            "array_index": fcurve.array_index,
                            "keyframes": count,
                        })
                    if count:
                        first = keyframe_points[0].co.x
                        last = keyframe_points[-1].co.x
                        frame_min = first if frame_min is None else min(frame_min, first)
                        frame_max = last if frame_max is None else max(frame_max, last)
                if channels:
                    animation["channels"] = channels
                animation["keyframe_count"] = total_keyframes
                if frame_min is not None:
                    animation["frame_range"] = [round(float(frame_min), 3),
                                                round(float(frame_max), 3)]

            drivers = getattr(anim_data, "drivers", None)
            if drivers and len(drivers):
                animation["driver_count"] = len(drivers)

            nla_tracks = [
                track.name
                for track in (getattr(anim_data, "nla_tracks", None) or [])[:8]
            ]
            if nla_tracks:
                animation["nla_tracks"] = nla_tracks

            return {"animation": animation} if animation else {}
        except Exception:
            return {}

    @staticmethod
    def _shader_fingerprint(id_block):
        """Stable short hash of a node tree (material or world), or None.

        Node identities plus rounded input values, so tweaking a color or
        rewiring a link changes the fingerprint. Lets downstream deltas see
        shader edits that leave every object transform untouched.
        """
        try:
            if id_block is None:
                return None
            tree = id_block.node_tree if getattr(id_block, "use_nodes", False) else None
            if tree is None:
                color = getattr(id_block, "diffuse_color", None) or getattr(id_block, "color", None)
                basis = str([round(float(v), 3) for v in color]) if color is not None else ""
            else:
                parts = []
                for node in tree.nodes:
                    values = []
                    for sock in node.inputs:
                        dv = getattr(sock, "default_value", None)
                        if isinstance(dv, (int, float)):
                            values.append(round(float(dv), 3))
                        elif dv is not None:
                            with suppress(TypeError, ValueError):
                                values.extend(round(float(v), 3) for v in dv)
                    parts.append(f"{node.bl_idname}{values}")
                parts.sort()
                parts.append(str(len(tree.links)))
                basis = "|".join(parts)
            return format(zlib.crc32(basis.encode("utf-8")), "08x")
        except Exception:
            return None

    @staticmethod
    def _project_id():
        """Salted hash linking sessions on the same .blend without storing its path."""
        try:
            filepath = bpy.data.filepath
            if not filepath:
                return None
            return hashlib.sha256(f"{uuid.getnode()}:{filepath}".encode("utf-8")).hexdigest()[:16]
        except Exception:
            return None

    def get_world_state_snapshot(self):
        """Compact world-state snapshot for trajectory capture (no mesh/shader detail)."""
        try:
            scene = bpy.context.scene
            selected = [obj.name for obj in bpy.context.selected_objects]
            selected_count = len(selected)
            selected_truncated = selected_count > MAX_SNAPSHOT_SELECTED
            if selected_truncated:
                # Sorted so before/after snapshots keep the same subset.
                selected = sorted(selected)[:MAX_SNAPSHOT_SELECTED]
            objects = []

            all_objects = list(scene.objects)
            truncated = len(all_objects) > MAX_SNAPSHOT_OBJECTS
            if truncated:
                # scene.objects iterates in an order that shifts as objects are
                # created, so an arbitrary prefix would leave the before/after
                # snapshots of one step holding different subsets and the delta
                # reporting ph