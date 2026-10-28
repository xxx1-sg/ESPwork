# ============================================================================
#  live_w3_02_no_remote_ack.ps1
#  第 3 周当堂验证 ②：在没有远端接收证据的情况下，页面不能显示"对方已收到"
#
#  这一项**不需要板子**也能完整验证 —— 它验证的是"页面渲染规则 + 后端字段语义"，
#  两者都可以用真实后端 + 打桩 DOM 独立复现。若板子在线，脚本还会走一遍真机闭环。
#
#  判定（与 docs/08 §4.6 / §5 一致）：
#    F1 仅本地反馈发生（无任何服务端回执）
#         → #helpItemStatus 含「佩戴者已按键（待回应）」
#         → #helpItemRemoteAck 为空
#         → 全文检索「对方已收到/已送达/设备已收到回应/已确认」命中 0
#    F2 status=responded 且 remote_acked_at_utc 为空
#         → 「我已回应，等待设备确认」；禁止词命中 0；保持 30s 不自动升级
#    F3 remote_acked_at_utc 非空 → 才允许「佩戴者设备已收到回应」
#    并做**源码级**核对：F3 分支的唯一判据是 remote_acked_at_utc，不是 status
#
#  用法：
#    powershell -ExecutionPolicy Bypass -File tools\live_w3_02_no_remote_ack.ps1
# ============================================================================

param(
    [string]$Api    = "http://127.0.0.1:8000",
    [string]$OutDir = "evidence",
    [string]$Token  = "esp32s3eye-demo-token-2026",
    [switch]$WithDevice          # 加上真机闭环（需要板子在线并已被后端看见）
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

# ---- 控制台编码：必须让 PowerShell 按 UTF-8 解码子进程输出 ----
# 原因：node / python 打印的是 UTF-8 字节；Windows PowerShell 5.1 默认按本地 OEM
#       代码页（简体中文为 cp936）解码子进程 stdout，中文会变成 `鍓嶇銆屾寜閿` 这类乱码。
#       本脚本后面要按**中文判据串**从 node 输出里摘结论，乱码会让匹配全部失配，
#       表现为"用例明明全绿、脚本却报 ✘ 失败"的**假失败**（曾实测到 srcJudgement/
#       srcOrder/hold30s 三项误报）。这里显式统一为 UTF-8，彻底消除编码歧义。
$OutputEncoding = New-Object System.Text.UTF8Encoding($false)
try { [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false) } catch { }

$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$logOut = Join-Path $OutDir "w3_live02_no_remote_ack_$stamp.log"
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

function Head($m) {
    Write-Host ""
    Write-Host ("=" * 78)
    Write-Host $m
    Write-Host ("=" * 78)
}
function Say($m) { Write-Host $m }

$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = "python" }

Head "第 3 周当堂验证 ②：无远端回执时页面不得显示'对方已收到'"
Say "后端     : $Api"
Say "证据文件 : $logOut"
Say ""

# ---- 前置：后端必须在线 ----
try {
    $h = Invoke-WebRequest -Uri "$Api/healthz" -TimeoutSec 5 -UseBasicParsing
    if ($h.StatusCode -ne 200) { throw "healthz 非 200" }
    Say "[前置] 后端在线 ✔"
} catch {
    Say "[前置] ✘ 后端没在跑：$($_.Exception.Message)"
    Say "        请先启动后端再跑本脚本。"
    exit 2
}

# ============================================================================
# A. 页面渲染规则（打桩 DOM 跑真实 app.js）—— 这是本项验证的主体
# ============================================================================
Head "A. 前端渲染规则：三种事实下的文案（打桩 DOM + 真实 web/app.js）"
Say "说明：这一层不需要板子。它直接加载 web/app.js，用假 DOM/fetch 驱动，"
Say "      断言页面**实际写进 #helpItemStatus / #helpItemRemoteAck** 的文本。"
Say ""

$nodeOut = & node tools\test_web_help.js 2>&1
$nodeExit = $LASTEXITCODE
$nodeOut | ForEach-Object { Write-Host "  $_" }

# 把关键结论摘出来
# ★ 注意：不要用 ✔/✘ 这些符号本身去匹配 —— 它们经过 PowerShell 管道时可能被转成
#   '?' 或乱码，导致断言永远不命中（本脚本第一版就踩了这个坑，出现全部假失败）。
#   这里改为匹配**稳定的中文判据串**，再用行首符号判断该行是否被标成失败；
#   最终裁判仍是 node 进程的退出码。
$nodeText = $nodeOut -join "`n"

function Test-NodeCase([string]$pattern) {
    $line = ($nodeOut | Select-String -Pattern $pattern -SimpleMatch:$false |
             Select-Object -First 1)
    if (-not $line) { return $false }
    $s = $line.ToString()
    # 失败行特征：含 ✘/✗，或（乱码时）行首是 '?'
    if ($s -match '✘|✗') { return $false }
    if ($s -match '^\s*\?') { return $false }
    return $true
}

$f1ok     = Test-NodeCase 'F1 #helpItemStatus'
$f2ok     = Test-NodeCase 'F2 #helpItemStatus'
$f3ok     = Test-NodeCase 'F3 #helpItemStatus'
$srcok    = Test-NodeCase 'helpRemoteAckText 的 F3 分支判据'
$srcorder = Test-NodeCase 'F3 分支出现在 responded 分支'
$holdok   = Test-NodeCase '30 秒内文案保持'
$nodeAllPass = ($nodeExit -eq 0)

Say ""
Say "判定："
Say "  F1 仅本地反馈 → 「佩戴者已按键（待回应）」     : $(if($f1ok){'✔ 通过'}else{'✘ 失败'})"
Say "  F2 responded 但未 ack → 「我已回应，等待设备确认」: $(if($f2ok){'✔ 通过'}else{'✘ 失败'})"
Say "  F3 设备已 ack → 才允许「佩戴者设备已收到回应」  : $(if($f3ok){'✔ 通过'}else{'✘ 失败'})"
Say "  源码核对：F3 唯一判据 = remote_acked_at_utc      : $(if($srcok){'✔ 通过'}else{'✘ 失败'})"
Say "  源码核对：F3 判定在 responded 分支之前         : $(if($srcorder){'✔ 通过'}else{'✘ 失败'})"
Say "  30 秒保持不自动升级                          : $(if($holdok){'✔ 通过'}else{'✘ 失败'})"
Say "  前端用例总退出码                             : $nodeExit $(if($nodeAllPass){'（全部通过）'}else{'（有失败）'})"

# 最终以 node 退出码为准；上面的单项匹配仅用于逐条展示
$passA = $nodeAllPass

# ============================================================================
# B. 后端字段语义：回应后 remote_acked_at_utc 必须仍为 NULL（三重一致）
# ============================================================================
Head "B. 后端字段语义：回应 ≠ 已送达（用真实后端跑一遍）"

$probe = Join-Path $env:TEMP "w3_probe_$stamp.py"
$probeCode = @'
import json, sys, urllib.request, urllib.error, uuid, datetime
API = sys.argv[1]; TOKEN = sys.argv[2]
DEV = "esp32s3eye-1C701C"

def req(method, path, obj=None, token=None):
    data = json.dumps(obj).encode() if obj is not None else None
    r = urllib.request.Request(API + path, data=data, method=method,
                               headers={"Content-Type": "application/json"})
    if token: r.add_header("X-Device-Token", token)
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try: return e.code, json.loads(e.read().decode())
        except Exception: return e.code, {}

ok = True
# ① 设备上传一条求助（模拟设备侧报文；本项验证的是**后端语义**，不是实体按键）
hid = str(uuid.uuid4())
now = datetime.datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + \
      datetime.datetime.now().astimezone().strftime("%z")
st, b = req("POST", "/api/v1/help_events", {
    "device_id": DEV, "help_id": hid, "client_request_key": "btn-live-%s" % hid[:8],
    "trigger_type": "button", "trigger_at_utc": now, "trigger_up_ms": 400000,
    "boot_count": 1, "queued_at_utc": now, "local_feedback": "buzzer+led",
    "local_feedback_at_utc": now, "message": None, "pending_local": False}, token=TOKEN)
print("  [1] 上传求助 -> HTTP %d status=%s" % (st, (b.get("data") or {}).get("status")))
if st != 201: ok = False

# ② 查看者回应该事件
st, b = req("POST", "/api/v1/help_events/%s/response" % hid,
            {"actor": "live-verifier", "message": "当堂验证"})
d = b.get("data") or {}
ra_resp = d.get("remote_acked_at_utc")
print("  [2] 回应 -> HTTP %d status=%s remote_acked_at_utc=%r" % (st, d.get("status"), ra_resp))
if st != 200 or d.get("status") != "responded": ok = False
if ra_resp is not None:
    print("      ✘ 回应响应里 remote_acked_at_utc 竟然非空 —— 违反红线！"); ok = False
else:
    print("      ✔ 回应响应里 remote_acked_at_utc 为 null（设备尚未确认）")

# ③ 详情接口
st, b = req("GET", "/api/v1/help_events/%s" % hid)
d = b.get("data") or {}
ra_detail = d.get("remote_acked_at_utc")
print("  [3] 详情接口 remote_acked_at_utc=%r" % ra_detail)
if ra_detail is not None:
    print("      ✘ 详情里已非空 —— 违反红线！"); ok = False
else:
    print("      ✔ 详情里仍为 null")

# ④ 列表接口
st, b = req("GET", "/api/v1/help_events?device_id=%s&limit=50" % DEV)
rows = ((b.get("data") or {}).get("help_events") or [])
hit = [x for x in rows if x["help_id"] == hid]
ra_list = hit[0]["remote_acked_at_utc"] if hit else "NOT_FOUND"
print("  [4] 列表接口 remote_acked_at_utc=%r" % ra_list)
if ra_list is not None:
    print("      ✘ 列表里已非空 —— 违反红线！"); ok = False
else:
    print("      ✔ 列表里仍为 null（三重一致：响应 / 详情 / 列表）")

# ⑤ 该事件的 help_ack 是否确实已入队（证明"回应确实下发了指令，但还没送达"）
st, b = req("GET", "/api/v1/help_events/%s" % hid)
cmds = (b.get("data") or {}).get("pending_commands") or []
types = [c.get("type") for c in cmds]
print("  [5] 该事件待下发指令 = %s" % (types or "（已被设备取走）"))
print("      → 入队 ≠ 送达：指令躺在队列里没人取，就不算送达")

print("")
print("  help_id = %s" % hid)
print("PROBE_RESULT=%s" % ("PASS" if ok else "FAIL"))
sys.exit(0 if ok else 1)
'@
Set-Content -Path $probe -Value $probeCode -Encoding UTF8

$probeOut = & $py $probe $Api $Token 2>&1
$probeExit = $LASTEXITCODE
$probeOut | ForEach-Object { Write-Host $_ }

$passB = ($probeExit -eq 0)

# ============================================================================
# C. （可选）真机闭环：设备取走 help_ack 并回执后，字段才变非空
# ============================================================================
$passC = $true
if ($WithDevice) {
    Head "C. 真机闭环：设备确认后才写入 remote_acked_at_utc"
    Say "说明：这一节需要板子在线。脚本会等设备轮询取走 help_ack 并回执（最多 60 秒）。"
    Say ""
    $probe2 = Join-Path $env:TEMP "w3_probe2_$stamp.py"
    $probe2Code = @'
import json, sqlite3, sys, time, os
db = os.path.join(sys.argv[1], "backend", "data", "sensor.db")
hid = sys.argv[2]
print("  等待设备取走 help_ack 并回执（最多 60 秒）...")
for i in range(60):
    c = sqlite3.connect(db)
    r = c.execute("SELECT status, remote_acked_at_utc FROM help_events WHERE help_id=?",
                  (hid,)).fetchone()
    c.close()
    if r and r[1]:
        print("  ✔ 第 %d 秒：remote_acked_at_utc = %s（设备确实回执了）" % (i + 1, r[1]))
        print("    此时页面**才被允许**显示「佩戴者设备已收到回应」")
        sys.exit(0)
    time.sleep(1)
print("  ✘ 60 秒内设备没有回执 —— 请确认板子在线且已烧第 3 周固件")
sys.exit(1)
'@
    Set-Content -Path $probe2 -Value $probe2Code -Encoding UTF8
    # 从上面 probe 的输出里取出 help_id
    $hidLine = $probeOut | Select-String -Pattern "help_id = (\S+)" | Select-Object -First 1
    if ($hidLine -and $hidLine.Matches.Count -gt 0) {
        $hid = $hidLine.Matches[0].Groups[1].Value
        $out2 = & $py $probe2 $root $hid 2>&1
        $out2 | ForEach-Object { Write-Host $_ }
        $passC = ($LASTEXITCODE -eq 0)
    } else {
        Say "  ✘ 没能从上一节取到 help_id，跳过真机闭环"
        $passC = $false
    }
} else {
    Say ""
    Say "（未加 -WithDevice：跳过真机闭环那一节。要跑请加 -WithDevice 并确保板子在线。）"
}

# ============================================================================
Head "当堂验证 ② 结论"
Say "A 前端渲染规则（F1/F2/F3 + 源码判据 + 30s 保持） : $(if($passA){'✔ 通过'}else{'✘ 失败'})"
Say "B 后端字段语义（回应后三重一致仍为 null）        : $(if($passB){'✔ 通过'}else{'✘ 失败'})"
if ($WithDevice) {
    Say "C 真机闭环（设备确认后字段才非空）              : $(if($passC){'✔ 通过'}else{'✘ 失败'})"
}

$all = $passA -and $passB -and $passC
Say ""
if ($all) {
    Say "✔ 结论：在没有远端接收证据（remote_acked_at_utc 为空）的情况下，"
    Say "        页面不显示「对方已收到」—— 三项事实 F1/F2/F3 的文案与禁止词全部符合契约。"
} else {
    Say "✘ 结论：存在未通过项，详见上面各节。"
}

# 落盘证据
@(
    "==== 第 3 周当堂验证 ② 证据 ====",
    "时间: $(Get-Date -Format o)",
    "后端: $Api",
    "",
    "---- A. 前端渲染规则（node tools/test_web_help.js 全文）----",
    ($nodeOut -join "`n"),
    "",
    "---- B. 后端字段语义探针 ----",
    ($probeOut -join "`n"),
    "",
    "结果: F1=$f1ok F2=$f2ok F3=$f3ok srcJudgement=$srcok srcOrder=$srcorder hold30s=$holdok backendSemantics=$passB"
) | Set-Content -Path $logOut -Encoding UTF8

Say ""
Say "证据文件 : $logOut"
Remove-Item $probe -ErrorAction SilentlyContinue
if ($WithDevice) { Remove-Item $probe2 -ErrorAction SilentlyContinue }
exit $(if ($all) { 0 } else { 1 })