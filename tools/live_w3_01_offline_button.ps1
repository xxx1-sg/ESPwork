# ============================================================================
#  live_w3_01_offline_button.ps1
#  第 3 周当堂验证 ①：断开外网，本地依然可以识别按键触发
#
#  判定（与 docs/08 §5 一致）：
#    (a) 串口出现 [HELP] 行且含 "local feedback done" 语义
#    (b) 触发 → 串口行时延 ≤300ms（板端 up_ms 差值）
#    (c) 现场听到蜂鸣、看到 LED 变化（脚本无法断言，需人工确认并记录）
#    (d) 后端 help_events 行数增量 = 0（断网无法上传 —— 预期行为）
#    (e) 板子不复位（boot_count 不变）
#
#  ⚠️ 断网方式：关掉板子所连的 2.4G 热点 / 路由器，**不要**停本机后端进程
#     （本脚本需要读后端数据库来证明"行数没增加"）。
#
#  用法：
#    powershell -ExecutionPolicy Bypass -File tools\live_w3_01_offline_button.ps1
#    powershell -ExecutionPolicy Bypass -File tools\live_w3_01_offline_button.ps1 -Port COM5 -Seconds 40
# ============================================================================

param(
    [string]$Port    = "COM5",
    [int]   $Seconds = 40,
    [string]$Api     = "http://127.0.0.1:8000",
    [string]$OutDir  = "evidence"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

# ---- 控制台编码统一为 UTF-8 ----
# 子进程（python / serial_watch.py）输出与写盘都是 UTF-8；Windows PowerShell 5.1
# 默认按本地 OEM 代码页解码，中文会变乱码。统一为 UTF-8 以保证日志与屏幕显示一致。
$OutputEncoding = New-Object System.Text.UTF8Encoding($false)
try { [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false) } catch { }

$stamp  = Get-Date -Format "yyyyMMdd_HHmmss"
$logOut = Join-Path $OutDir "w3_live01_offline_$stamp.log"
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

function Say($m) { Write-Host $m }
function Head($m) {
    Write-Host ""
    Write-Host ("=" * 78)
    Write-Host $m
    Write-Host ("=" * 78)
}

Head "第 3 周当堂验证 ①：断开外网仍可本地识别按键触发"
Say "串口      : $Port"
Say "抓取时长  : $Seconds 秒"
Say "后端      : $Api"
Say "证据文件  : $logOut"
Say ""
Say "★ 开始前请确认："
Say "   1) 已经断开板子的外网（关热点/拔网线），且板子串口已停止打印 [POST] 行"
Say "   2) 本机后端**仍在运行**（本脚本要读它的数据库来证明行数没涨）"
Say "   3) 手放在板子的 BOOT 按键上"
Say ""

# ---- 断网前置检查：后端必须活着 ----
try {
    $h = Invoke-WebRequest -Uri "$Api/healthz" -TimeoutSec 5 -UseBasicParsing
    if ($h.StatusCode -ne 200) { throw "healthz 非 200" }
    Say "[前置] 本机后端在线 ✔（注意：断的是板子的出网，不是本机后端）"
} catch {
    Say "[前置] ✘ 本机后端没在跑：$($_.Exception.Message)"
    Say "        请先启动后端（tools\start_system.ps1 或 python backend\server.py），再跑本脚本。"
    Say "        —— 断外网指的是**板子**出不去，不是把本机后端停掉。"
    exit 2
}

# ---- 记录基线：help_events 行数 ----
$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = "python" }

function Get-HelpCount {
    $code = @"
import os,sqlite3,sys
db=os.path.join(r'$root','backend','data','sensor.db')
try:
    c=sqlite3.connect(db)
    print(c.execute('SELECT COUNT(*) FROM help_events').fetchone()[0])
except Exception as e:
    print('ERR:'+str(e))
"@
    $tmp = Join-Path $env:TEMP "w3_count_$stamp.py"
    Set-Content -Path $tmp -Value $code -Encoding UTF8
    $r = & $py $tmp 2>&1
    Remove-Item $tmp -ErrorAction SilentlyContinue
    return ($r | Select-Object -Last 1)
}

$before = Get-HelpCount
Say "[基线] help_events 行数 = $before"

# ---- 抓串口（用工程既有的 serial_watch.py，它处理了 USB 重新枚举）----
Say ""
Say "[执行] 开始抓串口，请在 $Seconds 秒内按一次 BOOT 键（按 1~2 次即可）..."
Say ""

$watch = Join-Path $PSScriptRoot "serial_watch.py"
& $py $watch --port $Port --seconds $Seconds --out $logOut 2>&1 | ForEach-Object { Write-Host "  $_" }

# ---- 分析串口日志 ----
Say ""
Head "判定结果"

if (-not (Test-Path $logOut)) {
    Say "✘ 没有生成串口日志（$logOut）—— 请确认串口被占用情况与端口号"
    exit 1
}

# 串口日志由 serial_watch.py 以 UTF-8 写入，必须按 UTF-8 读，否则中文行显示为乱码
$lines = Get-Content $logOut -Encoding UTF8 -ErrorAction SilentlyContinue
$help = $lines | Where-Object { $_ -match "\[HELP\]" }
$helpTrigger = $help | Where-Object { $_ -match "\[HELP\]\s+trigger" }
$helpFeedback = $help | Where-Object { $_ -match "local feedback done" }
$bootLines = $lines | Where-Object { $_ -match "boot_count|第\s*\d+\s*次开机|boot=" }

Say "(a) 串口 [HELP] 行数            : $($help.Count)"
Say "    其中 trigger 行            : $($helpTrigger.Count)"
Say "    其中含 local feedback done : $($helpFeedback.Count)"

$passA = ($helpFeedback.Count -ge 1)
Say "    → 判定 (a) 本地反馈已发生     : $(if($passA){'✔ 通过'}else{'✘ 失败'})"

# (b) 时延：从 [HELP] 行里解析 latency=xxx ms
$lat = @()
foreach ($l in $helpTrigger) {
    if ($l -match "latency=(-?\d+)ms") { $lat += [int]$Matches[1] }
}
if ($lat.Count -gt 0) {
    $maxLat = ($lat | Measure-Object -Maximum).Maximum
    $medLat = ($lat | Sort-Object)[[int]($lat.Count / 2)]
    Say "(b) 本地反馈时延 (up_ms 差值)  : 共 $($lat.Count) 次，最大 ${maxLat}ms，中位 ${medLat}ms"
    $passB = ($maxLat -le 300)
    Say "    → 判定 (b) ≤300ms            : $(if($passB){'✔ 通过'}else{'✘ 失败'})"
} else {
    Say "(b) 未解析到 latency=（说明没有成功识别到按键，或串口未抓到）"
    $passB = $false
    Say "    → 判定 (b) ≤300ms            : ✘ 失败（未识别到按键）"
}

# (d) 后端行数增量应为 0
$after = Get-HelpCount
Say "(d) 后端 help_events 行数        : 之前=$before  之后=$after"
$passD = ($before -eq $after)
if ($passD) {
    Say "    → 判定 (d) 断网期间增量为 0   : ✔ 通过（预期：断网无法上传）"
} else {
    Say "    → 判定 (d) 断网期间增量为 0   : ✘ 失败 —— 行数涨了，说明板子其实还联着网"
    Say "       请确认外网真的断了（热点关了 / 路由器关了）。"
}

# (e) 是否复位
$boots = @()
foreach ($l in $lines) {
    if ($l -match "boot=(\d+)") { $boots += [int]$Matches[1] }
}
$uniqBoot = $boots | Sort-Object -Unique
Say "(e) 日志中出现的 boot 值         : $($uniqBoot -join ', ')"
$passE = ($uniqBoot.Count -le 1)
Say "    → 判定 (e) 未发生复位         : $(if($passE){'✔ 通过'}else{'✘ 失败（出现多个 boot，可能是抓串口本身导致的复位，需人工判读）'})"

Say ""
Say "(c) 实物反馈（蜂鸣声 / LED 变化）: ⚠️ 需人工确认 —— 脚本不能替你听和看"
Say "    请在此记录：听到蜂鸣 是/否 ___    看到 LED 变化 是/否 ___   （建议拍照或录像留证）"

Head "当堂验证 ① 结论"
$allPass = $passA -and $passB -and $passD
if ($allPass) {
    Say "✔ 自动化判定通过：(a) 本地反馈已发生 + (b) 时延 ≤300ms + (d) 断网期间上传为 0"
    Say "  还需人工补记 (c) 实物反馈，并保留本条串口日志作为证据。"
} else {
    Say "✘ 自动化判定未全部通过 —— 逐条看上面的判定行。"
}
Say ""
Say "证据文件 : $logOut"
if ($passA) {
    Say ""
    Say "本次 [HELP] 行原文："
    $help | Select-Object -First 6 | ForEach-Object { Write-Host "  $_" }
}

exit $(if ($allPass) { 0 } else { 1 })