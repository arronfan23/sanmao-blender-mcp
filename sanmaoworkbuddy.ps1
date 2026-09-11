# WorkBuddy + Blender MCP 一键安装脚本
# ============================================================
#  三猫云 sanmaocloud - WorkBuddy + Blender MCP 一键安装脚本
#  技术支持: 三猫云 sanmaocloud
# ============================================================
# 使用方法: 右键 -> 使用 PowerShell 运行; 如果提示禁止运行脚本,
# 先在 PowerShell 里执行: Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
$ErrorActionPreference = "Continue"
[Console]::OutputEncoding = [Text.Encoding]::UTF8

Write-Host ""
Write-Host "  ========================================" -ForegroundColor Magenta
Write-Host "   三猫云 sanmaocloud" -ForegroundColor Magenta
Write-Host "   WorkBuddy + Blender MCP 一键安装" -ForegroundColor Magenta
Write-Host "  ========================================" -ForegroundColor Magenta

function Write-Step($msg) { Write-Host "`n=== $msg ===" -ForegroundColor Cyan }

Write-Step "1/4 检查并安装 uv (用来运行 MCP 服务)"
$uv = Get-Command uv -ErrorAction SilentlyContinue
if ($uv) {
    Write-Host "uv 已安装: $(uv --version)"
} else {
    Write-Host "正在安装 uv..."
    powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
    $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        Write-Host "[!] uv 安装失败, 请检查网络后重试" -ForegroundColor Red
        pause; exit 1
    }
    Write-Host "uv 安装完成: $(uv --version)"
}

Write-Step "2/4 检查并安装 Blender"
$blenderDir = Get-ChildItem "C:\Program Files\Blender Foundation" -Directory -ErrorAction SilentlyContinue
if ($blenderDir) {
    Write-Host "Blender 已安装: $($blenderDir.Name -join ', ')"
} else {
    Write-Host "正在下载安装 Blender (约 300MB, 请耐心等待)..."
    winget install --id BlenderFoundation.Blender -e --accept-source-agreements --accept-package-agreements --silent
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[!] Blender 自动安装失败, 请打开 https://www.blender.org/download/ 手动下载安装" -ForegroundColor Red
    }
}

Write-Step "3/4 下载 Blender 插件 addon.py"
$dest = "$env:USERPROFILE\Desktop\blender-mcp"
New-Item -ItemType Directory -Force $dest | Out-Null
$bundled = Join-Path $PSScriptRoot "addon.py"
if (Test-Path $bundled) {
    Copy-Item $bundled "$dest\addon.py" -Force
    $ok = $true
} else {
    $urls = @(
        "https://raw.githubusercontent.com/ahujasid/blender-mcp/main/addon.py",
        "https://gh-proxy.com/https://raw.githubusercontent.com/ahujasid/blender-mcp/main/addon.py"
    )
    $ok = $false
    foreach ($u in $urls) {
        curl.exe -sL $u -m 60 -o "$dest\addon.py"
        if ((Get-Item "$dest\addon.py" -ErrorAction SilentlyContinue).Length -gt 10000) { $ok = $true; break }
    }
}
if ($ok) { Write-Host "插件已保存到: $dest\addon.py" }
else { Write-Host "[!] 插件下载失败, 请把网络问题处理后重试" -ForegroundColor Red }

Write-Step "4/4 预下载 blender-mcp 服务包"
& uv run --with blender-mcp python -c "print('blender-mcp ok')"

# 生成给 WorkBuddy 用的配置文件, 方便直接复制
@"
{
  "mcpServers": {
    "blender": {
      "command": "uvx",
      "args": ["blender-mcp"]
    }
  }
}
"@ | Out-File "$dest\WorkBuddy连接器配置.json" -Encoding utf8

Write-Host "`n全部完成! 接下来请打开《2-手动操作说明.txt》照着点几下鼠标。" -ForegroundColor Green
Write-Host "如有问题请联系 三猫云 sanmaocloud 技术支持。" -ForegroundColor Magenta
pause
