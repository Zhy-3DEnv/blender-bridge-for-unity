#Requires -Version 5.0
<#
.SYNOPSIS
  Batch test Blender Bridge: FBX header + background import + cold port baseline + hot TCP.

.PARAMETER Folder
  Absolute path, or path relative to current directory, containing .fbx files to sample.

.EXAMPLE
  cd E:\Project\Sausage-man-2
  powershell -File C:\...\UnityBridgeBlender\Tools\BlenderBridgeProfile\Batch-BlenderBridgeTest.ps1 `
    -Folder "Assets\Scenes\Resource\DreamIsland\CommonBuildings\RainbowIsland\Models\Render"
#>
param(
    [Parameter(Mandatory = $true)]
    [string] $Folder,
    [int] $SampleCount = 10,
    [int] $Seed = 20260531,
    [string] $BlenderExe = "C:\Program Files\Blender Foundation\Blender 5.1\blender.exe",
    [int] $BridgePort = 35971
)

$ErrorActionPreference = "Stop"
$PluginRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Injector = Join-Path $PluginRoot "Editor\blender-bridge-injector.py"
if (-not [System.IO.Path]::IsPathRooted($Folder)) {
    $Folder = Join-Path (Get-Location).Path $Folder
}
$FolderAbs = (Resolve-Path -LiteralPath $Folder).Path
$ReportDir = Join-Path $PSScriptRoot "reports"
New-Item -ItemType Directory -Force -Path $ReportDir | Out-Null
$ts = Get-Date -Format "yyyyMMdd_HHmmss"
$ReportJson = Join-Path $ReportDir "batch_$ts.json"

function Stop-BlenderForce {
    Get-Process -Name "blender" -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 1
}

function Test-BridgePort([int] $Port, [int] $TimeoutMs = 200) {
    try {
        $c = New-Object System.Net.Sockets.TcpClient
        $iar = $c.BeginConnect("127.0.0.1", $Port, $null, $null)
        if ($iar.AsyncWaitHandle.WaitOne($TimeoutMs)) {
            $c.EndConnect($iar)
            $c.Close()
            return $true
        }
        $c.Close()
    } catch {}
    return $false
}

function Wait-BridgePort([int] $Port, [int] $TimeoutSec) {
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        if (Test-BridgePort $Port) { return $true }
        Start-Sleep -Milliseconds 200
    }
    return $false
}

function Measure-ColdPortBaseline {
    Stop-BlenderForce
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $BlenderExe
    $psi.Arguments = "--python `"$Injector`""
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.EnvironmentVariables["BRIDGE_BLENDER_PORT"] = "$BridgePort"
    $psi.EnvironmentVariables["BLENDER_BRIDGE_INJECTOR"] = $Injector
    $proc = [System.Diagnostics.Process]::Start($psi)
    $open = Wait-BridgePort $BridgePort 90
    return [pscustomobject]@{
        spawn_ms     = [math]::Round($sw.Elapsed.TotalMilliseconds, 1)
        port_open_ms = if ($open) { [math]::Round($sw.Elapsed.TotalMilliseconds, 1) } else { $null }
        port_ready   = $open
        pid          = $proc.Id
    }
}

$allFbx = @(Get-ChildItem -LiteralPath $FolderAbs -File | Where-Object { $_.Extension -match '(?i)\.fbx$' })
$rng = New-Object System.Random $Seed
$sample = @($allFbx | Sort-Object { $rng.Next() } | Select-Object -First ([math]::Min($SampleCount, $allFbx.Count)))

Write-Host "Sample $($sample.Count)/$($allFbx.Count) seed=$Seed" -ForegroundColor Cyan

$analyzePy = Join-Path $PSScriptRoot "analyze_fbx_file.py"
$profilePy = Join-Path $PSScriptRoot "profile_fbx_import.py"
$tcpPy = Join-Path $PSScriptRoot "tcp_bridge_client.py"

$rows = @()
foreach ($f in $sample) {
    Write-Host "[import] $($f.Name)" -ForegroundColor DarkGray
    $hdrPath = Join-Path $ReportDir "tmp_hdr.json"
    python $analyzePy $f.FullName $hdrPath 2>&1 | Out-Null
    $hdr = Get-Content -Raw -LiteralPath $hdrPath | ConvertFrom-Json

    $profPath = Join-Path $ReportDir "tmp_prof.json"
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    & $BlenderExe --background --python $profilePy -- $f.FullName $profPath 2>&1 | Out-Null
    $wall = [math]::Round($sw.Elapsed.TotalSeconds, 4)
    $prof = Get-Content -Raw -LiteralPath $profPath | ConvertFrom-Json

    $rows += [pscustomobject]@{
        file       = $f.Name
        size_kb    = [math]::Round($hdr.size_bytes / 1024, 1)
        encoding   = $hdr.fbx_encoding
        import_sec = [double]$prof.phases.after_import
        vertices   = $prof.mesh_summary.vertices
        faces      = $prof.mesh_summary.faces
        wall_sec   = $wall
    }
}

Write-Host "[cold baseline] one GUI server boot..." -ForegroundColor Cyan
$cold = Measure-ColdPortBaseline

Write-Host "[hot tcp] $($sample.Count) files..." -ForegroundColor Cyan
$hotRows = @()
if ($cold.port_ready) {
    foreach ($f in $sample) {
        $out = python $tcpPy $f.FullName $BridgePort 2>&1 | Out-String
        $tcp = $out | ConvertFrom-Json
        $hotRows += [pscustomobject]@{
            file          = $f.Name
            tcp_ok        = $tcp.ok
            connect_ms    = $tcp.connect_ms
            import_ack_ms = $tcp.import_ack_ms
            total_ms      = $tcp.total_ms
            error         = $tcp.error
        }
    }
}
Stop-BlenderForce

$importSecs = @($rows | ForEach-Object { $_.import_sec })
$report = [ordered]@{
    generated_at  = (Get-Date).ToString("o")
    folder        = $FolderAbs
    seed          = $Seed
    note          = "Hot reuse: TCP PING/IMPORT ACK only. Unity returns on OK (queued)."
    cold_baseline = $cold
    hot_tcp       = $hotRows
    samples       = $rows
    summary       = [ordered]@{
        import_sec = @{
            min = ($importSecs | Measure-Object -Minimum).Minimum
            max = ($importSecs | Measure-Object -Maximum).Maximum
            avg = [math]::Round(($importSecs | Measure-Object -Average).Average, 4)
        }
        hot_tcp_ok_count = @($hotRows | Where-Object { $_.tcp_ok }).Count
        hot_tcp_ack_ms_avg = if ($hotRows.Count -gt 0) {
            [math]::Round((@($hotRows | Where-Object { $_.tcp_ok } | ForEach-Object { [double]$_.import_ack_ms }) | Measure-Object -Average).Average, 2)
        } else { $null }
    }
}

$report | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $ReportJson -Encoding UTF8
Write-Host "Report: $ReportJson" -ForegroundColor Green
Write-Host ($report.summary | ConvertTo-Json)
