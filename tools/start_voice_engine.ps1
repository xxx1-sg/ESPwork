<#
  start_voice_engine.ps1 —— 第 5 周：一条命令起「语音引擎」（识别 + 合成）

  为什么单独一个脚本？
    voice_engine.py 要和主后端分开起（进程隔离：引擎挂了文字入口不许挂）。
    但它有 3 个容易忘的环境变量（模型目录 / TTS 引擎 / 语速），
    忘了设就会变成"识别请求 500"或"播报 60 秒"，所以这里统一预设并自检。

  用法：
    # 最常用：默认 vosk 小模型 + Windows SAPI Huihui + 语速 1
    powershell -ExecutionPolicy Bypass -File D:\ESP\tools\start_voice_engine.ps1

    # 换端口（主后端用 SENSOR_VOICE_ENGINE_URL 指向它）
    powershell -ExecutionPolicy Bypass -File D:\ESP\tools\start_voice_engine.ps1 -Port 8013

    # 不说话（只验证"引擎不可用时文字入口仍可用"这条红线）
    powershell -ExecutionPolicy Bypass -File D:\ESP\tools\start_voice_engine.ps1 -NoTts

  起来之后：
    * 自述      http://127.0.0.1:8013/health
    * 日志      D:\ESP\evidence\voice_engine_run.log
    * 停止      Get-NetTCPConnection -State Listen -LocalPort 8013 |
                ForEach-Object { Stop-Process -Id $_.OwningProcess -Force }
#>
param(
  [int]$Port = 8013,
  [string]$ModelDir = "",
  [string]$TtsVoice = "Huihui",
  [int]$TtsRate = 1,
  [string]$Python = "D:\ESP\.venv\Scripts\python.exe",
  [string]$LogDir = "D:\ESP\evidence",
  [switch]$NoAsr,
  [switch]$NoTts
)

$ErrorActionPreference = "Stop"
# 关掉 Invoke-WebRequest 的进度条：非交互宿主里它会把整段输出重绘一遍，
# 看起来就像"脚本打印了两遍"（纯显示问题，但很误导）。
$ProgressPreference = "SilentlyContinue"

# 注意（踩坑记录）：这里**不要**去改 [Console]::OutputEncoding。
# 本脚本不解析任何子进程的中文输出（引擎自述是 /health 的 JSON，由 Invoke-WebRequest 拿，
# 全程在进程内），改了反而会在输出被重定向时把每个汉字写两遍（"语音引擎" → "语语音音引引擎擎"）。

$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
if (-not (Test-Path $Python)) { $Python = Join-Path $root ".venv\Scripts\python.exe" }
if (-not (Test-Path $Python)) { throw "找不到 Python: $Python（先按 docs/02 建虚拟环境）" }
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir | Out-Null }

$engine = Join-Path $root "backend\voice_engine.py"
if (-not (Test-Path $engine)) { throw "找不到 $engine" }

# ---- 0. 端口已在监听：不重复起（重复起会绑不上端口然后静默退出）----
$busy = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue
if ($busy) {
  Write-Host "[!] 端口 $Port 已在监听（PID $($busy.OwningProcess | Select-Object -First 1)），不重复启动。" -ForegroundColor Yellow
} else {
  # ---- 1. 模型目录：默认 <工程>\.models\vosk-model-small-cn-0.22 ----
  if (-not $ModelDir) { $ModelDir = Join-Path $root ".models\vosk-model-small-cn-0.22" }
  $hasModel = Test-Path $ModelDir
  if ($NoAsr) {
    Write-Host "[1/4] ASR 关闭（-NoAsr）：识别请求会如实返回 503。" -ForegroundColor Yellow
  } elseif ($hasModel) {
    $mb = [math]::Round(((Get-ChildItem $ModelDir -Recurse -File | Measure-Object Length -Sum).Sum / 1MB), 1)
    Write-Host "[1/4] 识别模型就绪: $ModelDir ($mb MB)" -ForegroundColor Green
  } else {
    Write-Host "[1/4] [✘] 模型目录不存在: $ModelDir" -ForegroundColor Red
    Write-Host "      先下载（约 44MB，见 docs/13 §2.3）：" -ForegroundColor Yellow
    Write-Host "      D:\ESP\.venv\Scripts\python.exe D:\ESP\tools\fetch_url.py ``" -ForegroundColor Yellow
    Write-Host "        https://alphacephei.com/vosk/models/vosk-model-small-cn-0.22.zip ``" -ForegroundColor Yellow
    Write-Host "        D:\ESP\.tmp\vosk-cn.zip" -ForegroundColor Yellow
    Write-Host "      引擎仍会启动，但识别请求会 500 并说明原因（文字入口不受影响）。" -ForegroundColor Yellow
  }

  # ---- 2. 起进程 ----
  Write-Host "[2/4] 启动语音引擎（监听 127.0.0.1:$Port）..." -ForegroundColor Cyan
  $env:VOICE_ENGINE_HOST = "127.0.0.1"
  $env:VOICE_ENGINE_PORT = "$Port"
  $env:VOICE_ASR_BACKEND = if ($NoAsr) { "none" } else { "vosk" }
  $env:VOICE_ASR_MODEL_DIR = $ModelDir
  $env:VOICE_TTS_BACKEND = if ($NoTts) { "none" } else { "sapi" }
  $env:VOICE_TTS_VOICE = $TtsVoice
  $env:VOICE_TTS_RATE = "$TtsRate"
  $env:PYTHONIOENCODING = "utf-8"
  $env:PYTHONUNBUFFERED = "1"

  $startLog = Join-Path $LogDir "voice_engine_run.log"
  $startErr = Join-Path $LogDir "voice_engine_run.err.log"
  $spawned = $false
  try {
    Start-Process -FilePath $Python `
      -ArgumentList $engine `
      -WorkingDirectory $root `
      -WindowStyle Hidden `
      -RedirectStandardOutput $startLog `
      -RedirectStandardError  $startErr | Out-Null
    $spawned = $true
  } catch {
    # ★ 已知环境陷阱（docs/13 §5.7 D8）：本机进程环境里同时有 HTTP_PROXY 与 http_proxy
    #   （Git Bash 拉起的会话常这样）。Start-Process 在构造子进程环境块时用的是
    #   "不区分大小写的字典"，于是抛「已添加项。字典中的关键字:'HTTPS_PROXY' 所添加的关键字:'https_proxy'」，
    #   连 Get-ChildItem Env: 都会炸，而且**从 PowerShell 侧删不掉**（写入不区分大小写、
    #   删除只命中其中一条）。绕法：自己建 ProcessStartInfo 且**不碰 EnvironmentVariables**，
    #   子进程就会原样继承父进程环境块（重复键在 Windows 环境块里本身合法，无害）；
    #   再把 stdout/stderr 交给 Python 自己重定向到日志文件。
    if ("$($_.Exception.Message)" -notmatch "已添加项|already been added|相同键") { throw }
    Write-Host "      (检测到环境里有大小写重复的 *_PROXY 变量，改用直连方式拉起进程)" -ForegroundColor Yellow
    $boot = @"
import runpy, sys
log = open(r'$startLog', 'a', encoding='utf-8', buffering=1)
sys.stdout = log; sys.stderr = log
sys.argv = [r'$engine']
runpy.run_path(r'$engine', run_name='__main__')
"@
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $Python
    $psi.Arguments = "-c " + ('"' + ($boot -replace '"', '\"') + '"')
    $psi.WorkingDirectory = $root
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    [void][System.Diagnostics.Process]::Start($psi)
    $spawned = $true
  }
  Start-Sleep -Seconds 2
}

# ---- 3. 等自述接口就绪（模型预热 0.2s 左右，给 30 次机会）----
Write-Host "[3/4] 等引擎自述..." -ForegroundColor Cyan
$health = $null
for ($i = 1; $i -le 30; $i++) {
  try {
    $r = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 3 -UseBasicParsing
    if ($r.StatusCode -eq 200) { $health = $r.Content | ConvertFrom-Json; break }
  } catch { Start-Sleep -Milliseconds 700 }
}
if (-not $health) {
  Write-Host "[✘] 引擎没起来，看日志: $(Join-Path $LogDir 'voice_engine_run.err.log')" -ForegroundColor Red
  exit 1
}

# ---- 4. 打印「到底是哪个引擎在哪台机器上跑」——这句话要能直接抄进交付记录 ----
Write-Host "[4/4] [OK] 语音引擎就绪" -ForegroundColor Green
Write-Host ""
Write-Host "  运行位置      : $($health.host):$($health.port)  ($($health.os) / Python $($health.python))" -ForegroundColor White
Write-Host "  识别 ASR      : $($health.asr.backend)  模型存在=$($health.asr.model_present)  已加载=$($health.asr.loaded)  耗时=$($health.asr.load_ms)ms" -ForegroundColor White
Write-Host "  合成 TTS      : $($health.tts.backend)  语音提示=$($health.tts.voice_hint)" -ForegroundColor White
# ★ 如实报告"到底能不能真发声"：没有音频输出设备时 SAPI 的 Speak() 一定失败
#   （即使只想写文件），此时引擎会降级到 tone 合成音。这件事必须在这里就说清楚，
#   不能等到测了一半才从 COMError 里猜。
if ($health.tts.speaks -eq $false) {
  Write-Host "  [!!] 该系统没有可用的音频输出设备（audio_outputs=$($health.tts.audio_outputs)）" -ForegroundColor Red
  Write-Host "       SAPI 无法合成真人语音；$($health.tts.note)" -ForegroundColor Yellow
  Write-Host "       影响：语音链路的取消/隔离/澄清/时序照旧可测，但『真人声识别』测不了。" -ForegroundColor Yellow
  Write-Host "       处理：装声卡驱动/接音频设备后重启本引擎即可自动回到真实语音。" -ForegroundColor Yellow
} elseif ($health.tts.speaks -eq $true) {
  Write-Host "  发声能力      : 正常（可见音频输出设备 $($health.tts.audio_outputs) 个）" -ForegroundColor Green
}
Write-Host "  上限          : 音频 $([math]::Round($health.limits.max_audio_bytes/1MB,1))MB / 文本 $($health.limits.max_tts_chars) 字" -ForegroundColor White
Write-Host ""

# ---- 5. 顺手确认主后端也认得它（文字入口与语音入口共用同一个后端）----
$mainUp = $false
try {
  $mh = Invoke-WebRequest -Uri "http://127.0.0.1:8000/api/v1/voice/health" -TimeoutSec 4 -UseBasicParsing
  $mainUp = ($mh.StatusCode -eq 200)
} catch { }
if ($mainUp) {
  Write-Host "  主后端 8000 已挂上语音路由: http://127.0.0.1:8000/api/v1/voice/health" -ForegroundColor Green
} else {
  Write-Host "  [!] 主后端 8000 未响应或仍是旧代码（voice 路由 404）。" -ForegroundColor Yellow
  Write-Host "      请另行启动: powershell -ExecutionPolicy Bypass -File D:\ESP\tools\start_system.ps1" -ForegroundColor Yellow
}
Write-Host ""
Write-Host "  日志: $(Join-Path $LogDir 'voice_engine_run.log')" -ForegroundColor White
Write-Host "  停止: Get-NetTCPConnection -State Listen -LocalPort $Port | ForEach-Object { Stop-Process -Id `$_.OwningProcess -Force }" -ForegroundColor White
