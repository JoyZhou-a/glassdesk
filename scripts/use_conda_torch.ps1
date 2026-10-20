# 从 conda 环境把 CUDA 版 torch 搬进本项目的 .venv
#
# 背景：国内下载 CUDA 版 torch 实测只有 0.5 MB/s（3.4 GB 需约 105 分钟），
# 但机器上已有一个能用的 conda 环境（bert_gpu）装了 torch 2.13.0+cu130，
# 且其 Python 版本（3.12.13）与本项目（3.12.14）同一小版本，二进制兼容。
#
# 注意：`uv sync --extra ml` 会重新装回 PyPI 的 CPU 版 torch。
# 每次 sync 之后要重跑本脚本。
#
# 用法：
#   pwsh scripts/use_conda_torch.ps1
#   pwsh scripts/use_conda_torch.ps1 -CondaEnv my_env

param(
    [string]$CondaEnv = "bert_gpu",
    [string]$CondaRoot = "$env:USERPROFILE\.conda\envs"
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$src = Join-Path $CondaRoot "$CondaEnv\Lib\site-packages"
$dst = Join-Path $projectRoot ".venv\Lib\site-packages"
$condaPy = Join-Path $CondaRoot "$CondaEnv\python.exe"

if (-not (Test-Path $condaPy)) {
    Write-Error "找不到 conda 环境的 python：$condaPy"
}
if (-not (Test-Path $dst)) {
    Write-Error "找不到本项目的 .venv：$dst（先运行 uv sync）"
}

# --- 检查源环境的 torch 是否真的支持 GPU ---
Write-Host "检查源环境 $CondaEnv ..." -ForegroundColor Cyan
$probe = & $condaPy -c @"
import json, torch
print(json.dumps({
  'torch': torch.__version__,
  'cuda': torch.cuda.is_available(),
  'arch': torch.cuda.get_arch_list() if torch.cuda.is_available() else [],
}))
"@ 2>&1 | Select-Object -Last 1

$info = $probe | ConvertFrom-Json
Write-Host ("  torch={0}  cuda={1}  arch={2}" -f $info.torch, $info.cuda, ($info.arch -join ","))
if (-not $info.cuda) {
    Write-Error "$CondaEnv 里的 torch 不支持 CUDA，换一个环境"
}
if (-not ($info.arch -contains "sm_120")) {
    Write-Warning "该 torch 不含 sm_120 架构，RTX 5060 Ti 可能无法使用"
}

# --- 卸载本项目的 CPU 版 torch ---
Write-Host "卸载 .venv 中的 CPU 版 torch ..." -ForegroundColor Cyan
$venvPy = Join-Path $projectRoot ".venv\Scripts\python.exe"
& $venvPy -m pip uninstall -y torch 2>&1 | Select-Object -Last 2
uv pip uninstall --python $venvPy torch 2>&1 | Select-Object -Last 2

# --- 复制 torch / torchgen / functorch / dist-info ---
$items = @("torch", "torchgen", "functorch")
$items += Get-ChildItem $src -Directory -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -match '^(torch|torchgen)-.*\.dist-info$' } |
    Select-Object -ExpandProperty Name

foreach ($item in $items) {
    $from = Join-Path $src $item
    if (-not (Test-Path $from)) { continue }
    Write-Host "  复制 $item ..." -ForegroundColor DarkGray
    robocopy $from (Join-Path $dst $item) /E /NFL /NDL /NJH /NJS /NP | Out-Null
    if ($LASTEXITCODE -ge 8) { Write-Error "robocopy 复制 $item 失败（exit=$LASTEXITCODE）" }
}

# --- 验证 ---
Write-Host "验证 .venv 中的 torch ..." -ForegroundColor Cyan
& $venvPy -c @"
import torch
assert torch.cuda.is_available(), 'CUDA 不可用'
name = torch.cuda.get_device_name(0)
cap = torch.cuda.get_device_capability(0)
x = torch.randn(512, 512, device='cuda')
_ = (x @ x).sum().item()
print(f'  OK: {torch.__version__} / {name} / sm_{cap[0]}{cap[1]} / GPU 计算正常')
"@
if ($LASTEXITCODE -ne 0) { Write-Error "验证失败" }

Write-Host "完成。" -ForegroundColor Green
