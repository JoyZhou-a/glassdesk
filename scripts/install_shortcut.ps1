# 在桌面创建 GlassDesk 悬浮窗快捷方式。
#
# 用法（在仓库根目录执行）：
#   pwsh scripts/install_shortcut.ps1
#   pwsh scripts/install_shortcut.ps1 -Name "GlassDesk" -Startup
#
# 参数 -Startup 会把快捷方式同时放进「启动」文件夹，开机自动弹出。

param(
    [string]$Name = "GlassDesk",
    [switch]$Startup
)

$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonw = Join-Path $projectRoot ".venv\Scripts\pythonw.exe"
$overlay = Join-Path $projectRoot "scripts\overlay.py"
$icon = Join-Path $projectRoot "assets\glassdesk.ico"

if (-not (Test-Path $pythonw)) {
    Write-Error "找不到 $pythonw —— 先跑 uv sync"
}
if (-not (Test-Path $overlay)) {
    Write-Error "找不到 $overlay"
}
if (-not (Test-Path $icon)) {
    Write-Warning "找不到图标 $icon，快捷方式将使用默认图标"
    $icon = ""
}

$desktop = [Environment]::GetFolderPath("Desktop")
$linkPath = Join-Path $desktop "$Name.lnk"

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($linkPath)
$shortcut.TargetPath = $pythonw
$shortcut.Arguments = "`"$overlay`""
$shortcut.WorkingDirectory = $projectRoot
$shortcut.Description = "GlassDesk 玻璃行业知识助手（悬浮窗）"
if ($icon) { $shortcut.IconLocation = "$icon,0" }
$shortcut.WindowStyle = 7          # 最小化启动，避免闪一下控制台
$shortcut.Save()

Write-Host "[OK] 已创建桌面快捷方式：$linkPath" -ForegroundColor Green

if ($Startup) {
    $startupDir = [Environment]::GetFolderPath("Startup")
    $startupLink = Join-Path $startupDir "$Name.lnk"
    Copy-Item $linkPath $startupLink -Force
    Write-Host "[OK] 已加入开机启动：$startupLink" -ForegroundColor Green
}

Write-Host ""
Write-Host "双击桌面图标即可打开悬浮窗。" -ForegroundColor Cyan
Write-Host "如果后端已经在跑，它不会重复启动。" -ForegroundColor DarkGray
