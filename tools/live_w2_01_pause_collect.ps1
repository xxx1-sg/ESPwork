# Live verification on the REAL board (no serial access -> no chip reset).
# Scenario 1 uses HTTP only. Scenario 3 (shutdown) is driven by the operator
# unplugging the board, so this script only sets it up and reports.
$ErrorActionPreference = 'Stop'
$API = 'http://127.0.0.1:8000'
$DID = 'esp32s3eye-1C701C'

function New-Req($key) {
    $body = "{""device_id"":""$DID"",""sensor"":""qma6100p"",""client_request_key"":""$key""}"
    (Invoke-WebRequest -Uri "$API/api/v1/collect_requests" -Method Post -ContentType 'application/json' -Body $body -UseBasicParsing).Content | ConvertFrom-Json
}
function Wait-Req($rid, $max = 25) {
    for ($i = 0; $i -lt $max; $i++) {
        Start-Sleep -Milliseconds 500
        $d = (Invoke-WebRequest -Uri "$API/api/v1/collect_requests/$rid" -UseBasicParsing).Content | ConvertFrom-Json
        if ($d.data.status -in @('completed', 'failed', 'timeout', 'rejected', 'cancelled')) { return $d.data }
    }
    return $d.data
}
function Dev() {
    ((Invoke-WebRequest -Uri "$API/api/v1/devices" -UseBasicParsing).Content | ConvertFrom-Json).devices |
        Where-Object { $_.device_id -eq $DID }
}

Write-Output "=================================================================="
Write-Output "LIVE-1: pause periodic reports -> collect -> verify new observation"
Write-Output "=================================================================="
$dev0 = Dev
Write-Output ("device before: periodic_enabled=" + $dev0.periodic_enabled + " boot=" + $dev0.last_boot_count + " seq=" + $dev0.last_seq + " status=" + $dev0.last_status)

# 1) pause
$pb = "{""device_id"":""$DID"",""enable"":false,""reason"":""live-1""}"
$pr = (Invoke-WebRequest -Uri "$API/api/v1/device/pause" -Method Post -ContentType 'application/json' -Body $pb -UseBasicParsing).Content | ConvertFrom-Json
Write-Output ("pause -> periodic_enabled=" + $pr.data.periodic_enabled + " command=" + $pr.data.command_id)

Start-Sleep -Seconds 4
$dev1 = Dev
Write-Output ("device after pause: periodic_enabled=" + $dev1.periodic_enabled)

# 2) record row count, wait 20s with NO clicks -> periodic packets must stop
$before = (Invoke-WebRequest -Uri "$API/api/v1/stats" -UseBasicParsing).Content | ConvertFrom-Json
Write-Output ("records before idle = " + $before.records)
Write-Output "idling 20s (no clicks; periodic reports should stop)..."
Start-Sleep -Seconds 20
$after = (Invoke-WebRequest -Uri "$API/api/v1/stats" -UseBasicParsing).Content | ConvertFrom-Json
$delta = $after.records - $before.records
Write-Output ("records after idle  = " + $after.records + "  delta=" + $delta + "  (expect 0)")

# 3) collect
$key = "live1-" + [DateTime]::UtcNow.ToString("HHmmss")
$c = New-Req $key
Write-Output ("created RID=" + $c.data.request_id + " baseline_seq=" + $c.data.baseline.seq_last)
$res = Wait-Req $c.data.request_id
Write-Output ("RESULT status=" + $res.status + " checks=" + ($res.checks | ConvertTo-Json -Compress))
if ($res.observation) {
    Write-Output ("  observation: seq_last=" + $res.observation.seq_last + " freshness=" + $res.observation.freshness + " mag=" + $res.observation.magnitude_g + " raw=" + ($res.observation.raw | ConvertTo-Json -Compress) + " report_mode=manual rid=" + $res.request_id)
}
Write-Output ("  baseline_seq=" + $res.baseline.seq_last + " -> obs_seq=" + $res.observation.seq_last + " (must be greater)")
Write-Output ("  delta_records_during_collect = " + (((Invoke-WebRequest -Uri "$API/api/v1/stats" -UseBasicParsing).Content | ConvertFrom-Json).records - $after.records))

# 4) resume
$rb = "{""device_id"":""$DID"",""enable"":true}"
$rr = (Invoke-WebRequest -Uri "$API/api/v1/device/pause" -Method Post -ContentType 'application/json' -Body $rb -UseBasicParsing).Content | ConvertFrom-Json
Write-Output ("resume -> periodic_enabled=" + $rr.data.periodic_enabled)
Start-Sleep -Seconds 6
$dev2 = Dev
Write-Output ("device after resume: periodic_enabled=" + $dev2.periodic_enabled + " seq=" + $dev2.last_seq)
$post = (Invoke-WebRequest -Uri "$API/api/v1/stats" -UseBasicParsing).Content | ConvertFrom-Json
Write-Output ("records resumed growth: " + $post.records)
Write-Output "LIVE-1 done."