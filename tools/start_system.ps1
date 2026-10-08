<#
  start_system.ps1 —— 一条命令把「后端 + 网页」起来（可选再起内网穿透）

  用法：
    # 最常用：起后端，板子直连本机内网地址
    powershell -ExecutionPolicy Bypass -File D:\ESP\tools\start_system.ps1

    # 换端口 / 换库
    powershell -ExecutionPolicy Bypass -File D:\ESP\tools\start_system.ps1 -Port 8000 -Db D:\ESP\backend\data\sensor.db

    # 同时开内网穿透（本机没有公网 IP 时用，会打印临时 https 地址）
    powershell -ExecutionPolicy Bypass -File D:\ESP\tools\start_system.ps1 -Tunnel

  起来之后：
    * 网页      http://127.0.0.1:8000/
    * 板子要填  http://<本机内网IP>:8000/api/v1/ingest   （见脚本打印出来的那一行）
    * 日志      D:\ESP\evidence\backend_run.log
    * 停止      Get-Process python | Where-Object { $_.Path -like '*\.venv\*' } | Stop-Process
#>
param(
  [string]$Db = "D:\ESP\backend\data\sensor.db",
  [string]$WebDir = "D:\ESP\web",
  [int]$Port = 8000,
  [string]$Token = "CHANGE_ME_DEVICE_TOKEN",
  [string]$Python = "D:\ESP\.venv\Scripts\python.exe",
  [string]$LogDir = "D:\ESP\evidence",
  [switch]$Tunnel
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
if (-not (Test-Path $Python)) { $Python = Join-Path $root ".venv\Scripts\python.exe" }
if (-not (Test-Path $Python)) { throw "找不到 Python: $Python（先按 docs/02 建虚拟环境）" }
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir | Out-Null }

# ---- 0. 端口占用检查：已经在跑就别重复起 ----
$busy = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue
if ($busy) {
  Write-Host "[!] 端口 $Port 已经在监听（可能后端已在运行），不重复启动。" -ForegroundColor Yellow
} else {
  Write-Host "[1/3] 启动后端（监听 0.0.0.0:$Port）..." -ForegroundColor Cyan
  $env:SENSOR_HOST = "0.0.0.0"
  $env:SENSOR_PORT = "$Port"
  $env:SENSOR_DB = $Db
  $env:SENSOR_WEB_DIR = $WebDir
  $env:SENSOR_TOKEN = $Token
  $env:PYTHONIOENCODING = "utf-8"
  $log = Join-Path $LogDir "backend_run.log"
  Start-Process -FilePath $Python `
    -ArgumentList (Join-Path $root "backend\server.py") `
    -WorkingDirectory $root `
    -WindowStyle Hidden `
    -RedirectStandardOutput $log `
    -RedirectStandardError (Join-Path $LogDir "backend_run.err.log") | Out-Null
  Start-Sleep -Seconds 2
}

# ---- 1. 等后端就绪 ----
Write-Host "[2/3] 等后端就绪..." -ForegroundColor Cyan
$ok = $false
for ($i = 1; $i -le 15; $i++) {
  try {
    $r = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/healthz" -TimeoutSec 3 -UseBasicParsing
    if ($r.StatusCode -eq 200) { $ok = $true; break }
  } catch { Start-Sleep -Milliseconds 700 }
}
if (-not $ok) {
  Write-Host "[✘] 后端没起来，看日志: $(Join-Path $LogDir 'backend_run.err.log')" -ForegroundColor Red
  exit 1
}
$stats = (Invoke-WebRequest -Uri "http://127.0.0.1:$Port/api/v1/stats" -TimeoutSec 5 -UseBasicParsing).Content
Write-Host "[✔] 后端就绪  $stats" -ForegroundColor Green

# ---- 2. 打印板子该填的地址（用本机真实内网 IP，不猜） ----
$ips = Get-NetIPAddress -AddressFamily IPv4 |
  Where-Object { $_.IPAddress -notlike "127.*" -and $_.IPAddress -notlike "169.254.*" -and
                 $_.InterfaceAlias -notlike "*WSL*" -and $_.InterfaceAlias -notlike "*Loopback*" } |
  Select-Object -ExpandProperty IPAddress -Unique
Write-Host ""
Write-Host "网页:        http://127.0.0.1:$Port/" -ForegroundColor White
foreach ($ip in $ips) {
  Write-Host "板子固件里:  #define APP_SERVER_URL `"http://${ip}:$Port/api/v1/ingest`"" -ForegroundColor White
}
Write-Host "（开发板必须能路由到这个地址；本工程实测开发板 10.1.42.183 与开发机 10.1.41.182 互通）"

if ($Tunnel) {
  Write-Host ""
  Write-Host "[3/3] 启动 Cloudflare 临时隧道..." -ForegroundColor Cyan
  $cf = Join-Path $root "tools\bin\cloudflared.exe"
  if (-not (Test-Path $cf)) { Write-Host "[!] 找不到 $cf" -ForegroundColor Yellow; exit 0 }
  $tlog = Join-Path $LogDir "tunnel_run.log"
  Start-Process -FilePath $cf -ArgumentList "tunnel", "--url", "http://localhost:$Port", "--no-autoupdate" `
    -WindowStyle Hidden -RedirectStandardOutput $tlog -RedirectStandardError (Join-Path $LogDir "tunnel_run.err.log") | Out-Null
  $url = $null
  for ($i = 1; $i -le 40; $i++) {
    Start-Sleep -Seconds 1
    if (Test-Path $tlog) {
      $m = Select-String -Path $tlog -Pattern "https://[a-z0-9-]+\.trycloudflare\.com" -ErrorAction SilentlyContinue |
        Where-Object { $_.Line -notmatch "api\.trycloudflare|developers\.cloudflare|cloudflare\.com/website|www\.cloudflare" } |
        Select-Object -First 1
      if ($m) { $url = $m.Matches.Value; break }
    }
  }
  if ($url) {
    Write-Host "[✔] 隧道地址: $url" -ForegroundColor Green
    Write-Host "    固件换成 HTTPS 一条命令搞定:" -ForegroundColor White
    Write-Host "    python tools\switch_endpoint.py --url $url/api/v1/ingest" -ForegroundColor White
  } else {
    Write-Host "[!] 没拿到隧道地址，看日志: $tlog（常见原因：本机 DNS 被投毒，见 docs/03 第 4.2 节）" -ForegroundColor Yellow
  }
}