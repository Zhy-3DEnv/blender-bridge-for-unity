#Requires -Version 5.0
param(
    [Parameter(Mandatory = $true)]
    [string] $FbxPath,
    [string] $BlenderExe = "C:\Program Files\Blender Foundation\Blender 5.1\blender.exe"
)

$ErrorActionPreference = "Stop"
$PluginRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Injector = Join-Path $PluginRoot "Editor\blender-bridge-injector.py"

if (-not [System.IO.Path]::IsPathRooted($FbxPath)) {
    $FbxPath = Join-Path (Get-Location).Path $FbxPath
}
$FbxPath = (Resolve-Path -LiteralPath $FbxPath).Path

if (-not (Test-Path -LiteralPath $BlenderExe)) { throw "Blender not found: $BlenderExe" }
if (-not (Test-Path -LiteralPath $Injector)) { throw "Injector not found: $Injector" }

$outDir = Join-Path $PSScriptRoot "reports"
New-Item -ItemType Directory -Force -Path $outDir | Out-Null
$base = [System.IO.Path]::GetFileNameWithoutExtension($FbxPath)
$profileLog = Join-Path $outDir "${base}_gui_profile_$(Get-Date -Format 'yyyyMMdd_HHmmss').log"

Write-Host "=== FBX header ===" -ForegroundColor Cyan
python (Join-Path $PSScriptRoot "analyze_fbx_file.py") $FbxPath

$psi = New-Object System.Diagnostics.ProcessStartInfo
$psi.FileName = $BlenderExe
$psi.Arguments = "--python `"$Injector`" -- `"$FbxPath`""
$psi.UseShellExecute = $false
$psi.RedirectStandardOutput = $true
$psi.RedirectStandardError = $true
$psi.CreateNoWindow = $false
$psi.EnvironmentVariables["BLENDER_BRIDGE_PROFILE"] = "1"
$psi.EnvironmentVariables["BRIDGE_BLENDER_PORT"] = "35971"
$psi.EnvironmentVariables["BLENDER_BRIDGE_INJECTOR"] = $Injector

$sw = [System.Diagnostics.Stopwatch]::StartNew()
$p = [System.Diagnostics.Process]::Start($psi)
$stdout = $p.StandardOutput
$stderr = $p.StandardError
$lines = [System.Collections.Generic.List[string]]::new()

while (-not $p.HasExited) {
    while ($stdout.Peek() -ge 0) {
        $l = $stdout.ReadLine()
        $lines.Add($l)
        if ($l -match "BRIDGE_PROFILE|BLENDER_BRIDGE") {
            Write-Host ("[{0,7:F1}s] {1}" -f $sw.Elapsed.TotalSeconds, $l)
        }
    }
    while ($stderr.Peek() -ge 0) {
        $l = $stderr.ReadLine()
        $lines.Add($l)
        if ($l -match "BRIDGE_PROFILE|BLENDER_BRIDGE") {
            Write-Host ("[{0,7:F1}s] {1}" -f $sw.Elapsed.TotalSeconds, $l)
        }
    }
    Start-Sleep -Milliseconds 200
}

$lines | Set-Content -LiteralPath $profileLog -Encoding UTF8
Write-Host "Log: $profileLog ($([math]::Round($sw.Elapsed.TotalSeconds,2)) s)" -ForegroundColor Green
