@echo off
title sanmaocloud - Blender MCP Installer
echo Starting installer, please do not close this window...
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0sanmaoworkbuddy.ps1"
echo.
echo ========================================
echo  Script finished.
echo  If you see red errors above, screenshot
echo  this window and send to sanmaocloud support.
echo ========================================
pause
