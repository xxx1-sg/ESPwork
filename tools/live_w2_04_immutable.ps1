# LIVE-4: replay an OLD observation against a live request.
# Expect: 409 STALE_OBSERVATION, request status UNCHANGED, rejection recorded.
$ErrorActionPreference = 'Stop'
$API = 'http://127.0.0.1:8000'
$DID = 'esp32s3eye-1C701C'
$TOK = 'esp32s3eye-demo-token-2026'
$PY  = 'D:\ESP\.venv\Scripts\python.exe'

Write-Output "=================================================================="
Write-Output "LIVE-4: old observation must NOT be recorded as this collection"
Write-Output "=================================================================="

# Fingerprint an old historical record (immutability baseline)
Write-Output "--- fingerprint of an old historical row (must stay identical) ---"
$snap = & $PY -c "import sqlite3;c=sqlite3.connect(r'D:\ESP\backend\data\sensor.db');r=c.execute('SELECT id,upload_id,boot_count,seq,raw_x,raw_y,raw_z,magnitude_g,verify_ok FROM sensor_records ORDER BY id ASC LIMIT 1').fetchone();print('|'.join(str(x) for x in r))"
Write-Output ("BEFORE: " + $snap)

# Create a fresh request (do NOT let it be served: use a bogus device boot so the
# real board still picks it up, but we submit our forged observation first)
$key = "live4-" + [DateTime]::UtcNow.ToString("HHmmss")
$body = "{""device_id"":""$DID"",""sensor"":""qma6100p"",""client_request_key"":""$key""}"
$c = (Invoke-WebRequest -Uri "$API/api/v1/collect_requests" -Method Post -ContentType 'application/json' -Body $body -UseBasicParsing).Content | ConvertFrom-Json
$rid = $c.data.request_id
$nonce = & $PY -c "import sqlite3;print(sqlite3.connect(r'D:\ESP\backend\data\sensor.db').execute('SELECT nonce FROM collect_requests WHERE request_id=?',('$rid',)).fetchone()[0])"
Write-Output ("created RID=" + $rid)
Write-Output ("baseline boot=" + $c.data.baseline.boot_count + " seq=" + $c.data.baseline.seq_last + " up=" + $c.data.baseline.uptime_ms)

# status before the replay
$before = & $PY -c "import sqlite3;print(sqlite3.connect(r'D:\ESP\backend\data\sensor.db').execute('SELECT status FROM collect_requests WHERE request_id=?',('$rid',)).fetchone()[0])"
$rejBefore = & $PY -c "import sqlite3;print(sqlite3.connect(r'D:\ESP\backend\data\sensor.db').execute('SELECT COUNT(*) FROM rejected_observations').fetchone()[0])"
$obsBefore = & $PY -c "import sqlite3;print(sqlite3.connect(r'D:\ESP\backend\data\sensor.db').execute('SELECT COUNT(*) FROM observations').fetchone()[0])"
Write-Output ("status before = " + $before + "  rejected=" + $rejBefore + "  observations=" + $obsBefore)

# Build a REPLAY: old boot/seq/up_ms equal to the baseline (i.e. NOT newer)
$payload = @{
    request_id = $rid; nonce = $nonce; device_id = $DID
    boot_count = $c.data.baseline.boot_count
    seq_first  = $c.data.baseline.seq_last
    seq_last   = $c.data.baseline.seq_last
    sample_count = 1; sample_period_ms = 100
    up_ms      = $c.data.baseline.uptime_ms
    ts_utc_ms  = $null; ts_local = $null; collect_mode = 'single'
    sensor = @{ model='QMA6100P'; type='accelerometer'; i2c_addr=18; chip_id=144; range_g=2; lsb_per_g=4096; odr_hz=100; oversample=8 }
    unit = @{ raw='LSB(14bit signed)'; accel_g='g'; accel_ms2='m/s^2'; magnitude_g='g' }
    samples = @(@{ seq=$c.data.baseline.seq_last; idx=1; up_ms=$c.data.baseline.uptime_ms; raw=@{x=-2787;y=-1851;z=-1209}; accel_g=@{x=-0.680420;y=-0.451904;z=-0.295166}; accel_ms2=@{x=-6.672;y=-4.432;z=-2.895}; magnitude_g=0.868511 })
    raw = @{x=-2787;y=-1851;z=-1209}
    accel_g = @{x=-0.680420;y=-0.451904;z=-0.295166}
    accel_ms2 = @{x=-6.672;y=-4.432;z=-2.895}
    magnitude_g = 0.868511
} | ConvertTo-Json -Depth 8 -Compress
$payload | Out-File -Encoding ascii D:\ESP\.tmp\replay.json

Write-Output ""
Write-Output "--- submitting a REPLAYED old observation ---"
$resp = curl.exe -s -w "`nHTTP=%{http_code}" -X POST "$API/api/v1/observations" `
        -H "Content-Type: application/json" -H "X-Device-Token: $TOK" `
        --data-binary "@D:\ESP\.tmp\replay.json"
Write-Output $resp

$after = & $PY -c "import sqlite3;print(sqlite3.connect(r'D:\ESP\backend\data\sensor.db').execute('SELECT status FROM collect_requests WHERE request_id=?',('$rid',)).fetchone()[0])"
$rejAfter = & $PY -c "import sqlite3;print(sqlite3.connect(r'D:\ESP\backend\data\sensor.db').execute('SELECT COUNT(*) FROM rejected_observations').fetchone()[0])"
$obsAfter = & $PY -c "import sqlite3;print(sqlite3.connect(r'D:\ESP\backend\data\sensor.db').execute('SELECT COUNT(*) FROM observations').fetchone()[0])"
$snap2 = & $PY -c "import sqlite3;c=sqlite3.connect(r'D:\ESP\backend\data\sensor.db');r=c.execute('SELECT id,upload_id,boot_count,seq,raw_x,raw_y,raw_z,magnitude_g,verify_ok FROM sensor_records ORDER BY id ASC LIMIT 1').fetchone();print('|'.join(str(x) for x in r))"

Write-Output ""
Write-Output "---- assertions ----"
Write-Output ("  HTTP 409 + STALE_OBSERVATION : " + ($resp -match '409' -and $resp -match 'STALE_OBSERVATION'))
Write-Output ("  request status NOT rewritten : " + $before + " -> " + $after + "  => " + ($before -eq $after))
Write-Output ("  status is not 'completed'    : " + ($after -ne 'completed'))
Write-Output ("  rejected_observations +1     : " + $rejBefore + " -> " + $rejAfter + "  => " + ([int]$rejAfter -eq [int]$rejBefore + 1))
Write-Output ("  observations unchanged       : " + $obsBefore + " -> " + $obsAfter + "  => " + ($obsBefore -eq $obsAfter))
Write-Output ("  old DB row unchanged         : " + ($snap -eq $snap2))
Write-Output ("    BEFORE " + $snap)
Write-Output ("    AFTER  " + $snap2)

# also confirm the "timeout != hardware fault" evidence for a shut-down device
Write-Output ""
Write-Output "--- timeout != hardware fault (device-level facts) ---"
& $PY -c @"
import sqlite3
c=sqlite3.connect(r'D:\ESP\backend\data\sensor.db')
print('sensor_error events :', c.execute(\"SELECT COUNT(*) FROM device_events WHERE event_type='sensor_error'\").fetchone()[0])
print('command_timeout     :', c.execute(\"SELECT COUNT(*) FROM device_events WHERE event_type='command_timeout'\").fetchone()[0])
print('devices.last_status :', c.execute('SELECT last_status FROM devices').fetchall())
"@
Write-Output "LIVE-4 done."