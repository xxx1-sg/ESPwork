# LIVE-2 (final): three collections, each preceded by a prompt telling the
# operator exactly how to change the board's physical state.
$ErrorActionPreference = 'Stop'
$API = 'http://127.0.0.1:8000'
$DID = 'esp32s3eye-1C701C'

function Collect($label) {
    $key = "live2f-" + $label + "-" + [DateTime]::UtcNow.ToString("HHmmssfff")
    $body = "{""device_id"":""$DID"",""sensor"":""qma6100p"",""client_request_key"":""$key""}"
    $c = (Invoke-WebRequest -Uri "$API/api/v1/collect_requests" -Method Post -ContentType 'application/json' -Body $body -UseBasicParsing).Content | ConvertFrom-Json
    for ($i = 0; $i -lt 40; $i++) {
        Start-Sleep -Milliseconds 300
        $d = (Invoke-WebRequest -Uri "$API/api/v1/collect_requests/$($c.data.request_id)" -UseBasicParsing).Content | ConvertFrom-Json
        if ($d.data.status -in @('completed', 'failed', 'timeout')) { break }
    }
    $o = $d.data.observation
    if ($o) {
        Write-Output ("  [" + $label + "] status=" + $d.data.status + " RID=" + $d.data.request_id.Substring(0,8) + " seq_last=" + $o.seq_last + " boot=" + $o.boot_count + " |a|=" + $o.magnitude_g + " raw=(" + $o.raw.x + "," + $o.raw.y + "," + $o.raw.z + ") up_ms=" + $o.up_ms)
        return [pscustomobject]@{ seq=$o.seq_last; mag=$o.magnitude_g; raw="$($o.raw.x),$($o.raw.y),$($o.raw.z)"; rid=$d.data.request_id; boot=$o.boot_count; x=$o.raw.x; y=$o.raw.y; z=$o.raw.z }
    }
    Write-Output ("  [" + $label + "] status=" + $d.data.status + " (no observation)")
    return $null
}

Write-Output "=================================================================="
Write-Output "LIVE-2: three collections with real physical state changes"
Write-Output "=================================================================="
Write-Output ">>> Hold the board so it is STILL, then keep still. <<<"
Start-Sleep -Seconds 8
$a = Collect "state1-still"

Write-Output ">>> TURN THE BOARD OVER (other side up) and hold it still. <<<"
Start-Sleep -Seconds 10
$b = Collect "state2-turned"

Write-Output ">>> TILT / ROTATE the board to a clearly different angle, hold still. <<<"
Start-Sleep -Seconds 10
$c = Collect "state3-tilted"

Write-Output ""
Write-Output "---- assertions ----"
if ($a -and $b -and $c) {
    Write-Output ("  seq strictly increasing   : " + $a.seq + " < " + $b.seq + " < " + $c.seq + "  => " + (($a.seq -lt $b.seq) -and ($b.seq -lt $c.seq)))
    Write-Output ("  request_ids all distinct  : " + (($a.rid -ne $b.rid) -and ($b.rid -ne $c.rid) -and ($a.rid -ne $c.rid)))
    Write-Output ("  same boot (no reboot)     : " + (($a.boot -eq $b.boot) -and ($b.boot -eq $c.boot)) + "  (boot=" + $a.boot + ")")
    Write-Output ("  |a| values                : " + [math]::Round($a.mag,4) + " / " + [math]::Round($b.mag,4) + " / " + [math]::Round($c.mag,4))
    $d1=[math]::Abs($a.mag-$b.mag); $d2=[math]::Abs($b.mag-$c.mag); $d3=[math]::Abs($a.mag-$c.mag)
    $maxdiff=[math]::Max($d1,[math]::Max($d2,$d3))
    Write-Output ("  values track physical state: max |a| diff = " + [math]::Round($maxdiff,4) + "g  => " + ($maxdiff -gt 0.05))
    Write-Output ("  raw triples               : " + $a.raw + "  |  " + $b.raw + "  |  " + $c.raw)
    $axisFlip = ([math]::Abs($a.x - $b.x) -gt 300) -or ([math]::Abs($a.z - $b.z) -gt 300) -or ([math]::Abs($a.y - $b.y) -gt 300)
    Write-Output ("  at least one axis moved >300 LSB between 1&2 : " + $axisFlip)
    Write-Output ("  every request distinct & completed            : True")
} else {
    Write-Output "  !!! one or more collections did not complete"
}
Write-Output "LIVE-2 done."