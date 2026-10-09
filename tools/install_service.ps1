# install_service.ps1 —— 把传感器后端注册成 Windows 服务（需管理员）
#
#   ★ 必须以【管理员】身份运行，否则 New-Service 会 Access denied。
#   ★ 用这个脚本，就不要再用"启动文件夹"那套了（二选一，避免起两个守护）。
#
# 用法：
#   powershell -ExecutionPolicy Bypass -File D:\ESP\tools\install_service.ps1
#
# 卸载：
#   powershell -ExecutionPolicy Bypass -File D:\ESP\tools\install_service.ps1 -Uninstall

param(
  [string]$Name        = "ESP_SensorBackend",
  [string]$DisplayName = "ESP Sensor Backend",
  [string]$Root        = "D:\ESP",
  [switch]$Uninstall
)

$ErrorActionPreference = "Stop"

# ---- 管理员检查：不满足就直接说清楚，别让它抛一堆看不懂的异常 ----
# 注意：这里必须写成一行。用反引号续行会把强制类型转换和它的参数拆开，PowerShell 解析不了。
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
  Write-Host "[X] 需要管理员权限。" -ForegroundColor Red
  Write-Host "    请右键 PowerShell -> 以管理员身份运行，再执行本脚本。" -ForegroundColor Yellow
  exit 1
}

$python = Join-Path $Root ".venv\Scripts\python.exe"
$guard  = Join-Path $Root "tools\run_backend_forever.py"

if ($Uninstall) {
  Write-Host "=== 卸载服务 $Name ===" -ForegroundColor Cyan
  $svc = Get-Service -Name $Name -ErrorAction SilentlyContinue
  if (-not $svc) { Write-Host "服务不存在，无需卸载。"; exit 0 }
  if ($svc.Status -ne "Stopped") { Stop-Service -Name $Name -Force; Start-Sleep 2 }
  # 先停守护（它会不停重拉后端），再删服务
  try { & $python (Join-Path $Root "tools\stop_backend.py") } catch { }
  sc.exe delete $Name | Out-Null
  Write-Host "[OK] 已卸载。" -ForegroundColor Green
  Write-Host "     注意：启动文件夹里的自启项如果还在，请手动删掉（shell:startup）。"
  exit 0
}

# ---- 安装 ----
if (-not (Test-Path $python)) { throw "找不到 Python: $python" }
if (-not (Test-Path $guard))  { throw "找不到守护脚本: $guard" }

if (Get-Service -Name $Name -ErrorAction SilentlyContinue) {
  Write-Host "[!] 服务 $Name 已存在。先跑 -Uninstall 再装，或直接用 Start-Service。" -ForegroundColor Yellow
  exit 1
}

Write-Host "=== 注册服务 $Name ===" -ForegroundColor Cyan
# ★ 路径含空格时必须整体加引号，否则服务会以 1053 超时启动失败
$binPath = '"{0}" "{1}"' -f $python, $guard

New-Service -Name $Name `
  -BinaryPathName $binPath `
  -DisplayName $DisplayName `
  -Description "收到 ESP32-S3 开发板上报的加速度数据并落库（含崩溃自动重拉）" `
  -StartupType Automatic

# 崩溃自动重启：第 1/2/3 次失败各等 5 秒后重启
sc.exe failure $Name reset= 86400 actions= restart/5000/restart/5000/restart/5000 | Out-Null

Start-Service -Name $Name
Start-Sleep -Seconds 8

$svc = Get-Service -Name $Name
Write-Host ""
Write-Host "服务状态: $($svc.Status)" -ForegroundColor Green
Write-Host ""
Write-Host "验证后端是否真的起来了:"
try {
  $r = Invoke-WebRequest "http://127.0.0.1:8000/healthz" -TimeoutSec 8 -UseBasicParsing
  Write-Host "  [OK] /healthz -> $($r.Content)" -ForegroundColor Green
} catch {
  Write-Host "  [X] /healthz 没响应，看日志: $Root\evidence\backend_guard.log" -ForegroundColor Red
}

Write-Host ""
Write-Host "常用命令：" -ForegroundColor White
Write-Host "  查看状态 : Get-Service $Name"
Write-Host "  停止     : Stop-Service $Name"
Write-Host "  启动     : Start-Service $Name"
Write-Host "  卸载     : powershell -File $PSCommandPath -Uninstall"
Write-Host ""
Write-Host "★ 提醒：如果启动文件夹里还有 ESP_SensorBackend_Autostart.cmd，请删掉（shell:startup），" -ForegroundColor Yellow
Write-Host "  否则服务 + 自启项会各带一个守护进程。" -ForegroundColor Yellow