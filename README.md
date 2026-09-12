# 三猫云 sanmaocloud - WorkBuddy 控制 Blender 一键部署

让腾讯 WorkBuddy 通过 MCP 协议控制 Blender，用自然语言建模、渲染。
本仓库提供 Windows 一键安装包，适合零基础用户。

## 使用方法

1. 下载本仓库全部文件（Code -> Download ZIP，解压）。
2. 双击运行 `0-双击我安装.bat`，等待显示"全部完成"。
   （不要右键运行 .ps1 文件，直接双击 bat 即可）
3. 打开 `2-手动操作说明.txt`，照着完成 Blender 插件安装和 WorkBuddy 连接器配置。

## 文件说明

| 文件 | 作用 |
|------|------|
| `0-双击我安装.bat` | 启动入口，双击运行 |
| `sanmaoworkbuddy.ps1` | 一键安装脚本（自动装 uv、Blender、插件、预下载 MCP 服务包） |
| `addon.py` | Blender 插件（来自 [ahujasid/blender-mcp](https://github.com/ahujasid/blender-mcp)） |
| `2-手动操作说明.txt` | 手动操作图文步骤 |

## 工作原理

- `uvx blender-mcp` 启动 MCP 服务进程
- Blender 插件 addon.py 监听本地 9876 端口
- WorkBuddy 通过"自定义连接器"接入 MCP 配置，即可用自然语言操作 Blender

## 技术支持

三猫云 sanmaocloud
