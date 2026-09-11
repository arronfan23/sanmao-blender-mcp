@echo off
chcp 936 >nul
title 三猫云 sanmaocloud - Blender MCP 一键安装
echo 正在启动安装脚本, 请勿关闭本窗口...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0sanmaoworkbuddy.ps1"
if errorlevel 1 (
  echo.
  echo 脚本出错, 请把本窗口截图发给三猫云技术支持
  pause
)
