# LIVE-3: device shutdown -> request must go to 'timeout'
# This script records the "before" facts, fires a request, and asks the operator
# to unplug the board. It then reports the timeout evidence.
$ErrorActionPreference = 'Stop'
$API = 'http://127.0.0.1:8000'
$DID = 'esp32s3eye-1C701C'
$PY  = 'D:\ESP\.venv\Scripts\python.exe'

Write-Output "=================================================================="
Write-Output "LIVE-3: device powered off -> timeout (NOT a hardware fault)"
Write-Output "=================================================================="

Write-Output "--- device-level facts BEFORE shutdown ---"
& $PY -c @"
import sqlite3
c=sqlite3.connect(r'D:\ESP\backend\data\sensor.db')
print('devices.last_status :', c.execute('SELECT last_status FROM devices').fetchall())
print('sensor_error count  :', c.execute(\"SELECT COUNT(*) FROM device_events WHERE event_type='sensor_error'\").fetchone()[0])
print('command_timeout cnt :', c.execute(\"SELECT COUNT(*) FROM device_events WHERE event_type='command_timeout'\").fetchone()[0])
"@

Write-Output ""
Write-Output ">>> OPERATOR: UNPLUG the board's USB power NOW. <<<"
Write-Output ">>> The script will wait 25 seconds, then fire the request. <<<"
Start-Sleep -Seconds 25

# confirm the device really is gone (no new rows for the last 8 seconds)
$stale = & $PY -c @"
import sqlite3, datetime
c=sqlite3.connect(r'D:\ESP\backend\data\sensor.db')
last=c.execute('SELECT MAX(received_at_utc) FROM sensor_records').fetchone()[0]
now=datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3]+'Z'
print(last)
"@
Write-Output ("last record received at: " + $stale)
Start-Sleep -Seconds 6
$stale2 = & $PY -c "import sqlite3;print(sqlite3.connect(r'D:\ESP\backend\data\sensor.db').execute('SELECT MAX(received_at_utc) FROM sensor_records').fetchone()[0])"
Write-Output ("after 6 more seconds   : " + $stale2 + "   (identical => device is OFF)")
Write-Output ("=> device confirmed offline: " + ($stale -eq $stale2))

Write-Output ""
Write-Output "creating a collect request for the powered-off device..."
$key = "live3-" + [DateTime]::UtcNow.ToString("HHmmss")
$body = "{""device_id"":""$DID"",""sensor"":""qma6100p"",""client_request_key"":""$key""}"
$c = (Invoke-WebRequest -Uri "$API/api/v1/collect_requests" -Method Post -ContentType 'application/json' -Body $body -UseBasicParsing).Content | ConvertFrom-Json
$rid = $c.data.request_id
Write-Output ("created RID=" + $rid + " timeout_ms=" + $c.data.timeout_ms)
Write-Output ("  submitted = " + $c.data.submitted_at_utc)
Write-Output ("  deadline  = " + $c.data.deadline_utc)

Write-Output "waiting for the deadline scan to mark it timeout..."
$final = $null
for ($i = 0; $i -lt 40; $i++) {
    Start-Sleep -Milliseconds 700
    $d = (Invoke-WebRequest -Uri "$API/api/v1/collect_requests/$rid" -UseBasicParsing).Content | ConvertFrom-Json
    $final = $d.data
    if ($final.status -in @('completed','failed','timeout','rejected','cancelled')) { break }
}
Write-Output ("FINAL status = " + $final.status + "  failure_reason=" + $final.failure_reason)
Write-Output ("  dispatched_at = " + $final.dispatched_at_utc)
Write-Output ("  deadline      = " + $final.deadline_utc)
Write-Output ("  device seen   = " + ($final.device | ConvertTo-Json -Compress))
Write-Output ("  rejections    = " + $final.rejections.Count)
$obsTxt = "none (correct)"
if ($final.observation) { $obsTxt = "PRESENT (WRONG!)" }
Write-Output ("  observation   = " + $obsTxt)

Write-Output ""
Write-Output "---- device-level facts AFTER (must show NO sensor_error) ----"
& $PY -c @"
import sqlite3
c=sqlite3.connect(r'D:\ESP\backend\data\sensor.db')
print('devices.last_status :', c.execute('SELECT last_status FROM devices').fetchall())
print('sensor_error count  :', c.execute(\"SELECT COUNT(*) FROM device_events WHERE event_type='sensor_error'\").fetchone()[0])
print('command_timeout cnt :', c.execute(\"SELECT COUNT(*) FROM device_events WHERE event_type='command_timeout'\").fetchone()[0])
print('timeout for this rid:', c.execute(\"SELECT COUNT(*) FROM device_events WHERE event_type='command_timeout' AND detail_json LIKE ?\", ('%$rid%',)).fetchone()[0])
"@

Write-Output ""
Write-Output "---- assertions ----"
Write-Output ("  status == timeout                          : " + ($final.status -eq 'timeout'))
Write-Output ("  failure_reason is NOT SENSOR_ERROR         : " + ($final.failure_reason -ne 'SENSOR_ERROR') + "  (" + $final.failure_reason + ")")
Write-Output ("  no observation recorded                    : " + ($null -eq $final.observation))
Write-Output ("  device NOT marked sensor_error             : " + ($final.device.last_status -ne 'sensor_error') + "  (" + $final.device.last_status + ")")
Write-Output "LIVE-3 done. (Re-plug the board when finished.)"