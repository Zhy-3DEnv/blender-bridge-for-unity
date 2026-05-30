#Requires -Version 5.0
param(
    [int] $MaxWaitSec = 180,
    [int] $PollMs = 100,
    [int] $BridgePort = 35971
)

$ErrorActionPreference = "SilentlyContinue"
$PluginRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$ReportDir = Join-Path $PSScriptRoot "reports"
New-Item -ItemType Directory -Force -Path $ReportDir | Out-Null

$ts = Get-Date -Format "yyyyMMdd_HHmmss"
$ReportPath = Join-Path $ReportDir "flow_watch_$ts.jsonl"

$sw = [System.Diagnostics.Stopwatch]::StartNew()
$events = [System.Collections.Generic.List[object]]::new()

function Log-Event([string] $Phase, [string] $Detail = "") {
    $ms = [math]::Round($sw.Elapsed.TotalMilliseconds, 1)
    $row = [ordered]@{ t_ms = $ms; phase = $Phase; detail = $Detail }
    $events.Add($row) | Out-Null
    Add-Content -LiteralPath $ReportPath -Value ($row | ConvertTo-Json -Compress) -Encoding UTF8
    Write-Host ("[{0,8} ms] {1} {2}" -f $ms, $Phase, $(if ($Detail) { "- $Detail" }))
}

function Test-PortOpen([int] $Port) {
    try {
        $c = New-Object System.Net.Sockets.TcpClient
        $iar = $c.BeginConnect("127.0.0.1", $Port, $null, $null)
        if ($iar.AsyncWaitHandle.WaitOne(80)) { $c.EndConnect($iar); $c.Close(); return $true }
        $c.Close()
    } catch {}
    return $false
}

Log-Event "watch_start" "plugin=$PluginRoot port=$BridgePort"
$baselinePids = @(Get-Process -Name "blender" -EA SilentlyContinue | ForEach-Object { $_.Id })
Log-Event "baseline_blender" "count=$($baselinePids.Count)"

Write-Host "Double-click FBX in Unity now. Watching ${MaxWaitSec}s..." -ForegroundColor Yellow

$seenNew = $false
$newPid = $null
$portMs = $null
$deadline = $sw.Elapsed.Add([TimeSpan]::FromSeconds($MaxWaitSec))

while ($sw.Elapsed -lt $deadline) {
    Start-Sleep -Milliseconds $PollMs
    if (-not $seenNew) {
        foreach ($p in @(Get-Process -Name "blender" -EA SilentlyContinue)) {
            if ($baselinePids -notcontains $p.Id) {
                $seenNew = $true
                $newPid = $p.Id
                Log-Event "blender_spawned" "pid=$($p.Id)"
                break
            }
        }
    }
    if ($null -eq $portMs -and (Test-PortOpen $BridgePort)) {
        $portMs = $sw.Elapsed.TotalMilliseconds
        Log-Event "bridge_port_open" "127.0.0.1:$BridgePort"
    }
}

Log-Event "watch_end" "elapsed_ms=$([math]::Round($sw.Elapsed.TotalMilliseconds,1))"
Write-Host "Report: $ReportPath" -ForegroundColor Green
