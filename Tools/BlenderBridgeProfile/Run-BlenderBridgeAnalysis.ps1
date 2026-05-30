#Requires -Version 5.0
param(
    [Parameter(Mandatory = $true)]
    [string] $FbxPath,
    [string] $BlenderExe = "C:\Program Files\Blender Foundation\Blender 5.1\blender.exe"
)

$ErrorActionPreference = "Stop"
if (-not [System.IO.Path]::IsPathRooted($FbxPath)) {
    $FbxPath = Join-Path (Get-Location).Path $FbxPath
}
$FbxPath = (Resolve-Path -LiteralPath $FbxPath).Path

$pyDir = $PSScriptRoot
$analyze = Join-Path $pyDir "analyze_fbx_file.py"
$profile = Join-Path $pyDir "profile_fbx_import.py"
$outDir = Join-Path $pyDir "reports"
New-Item -ItemType Directory -Force -Path $outDir | Out-Null

$base = [System.IO.Path]::GetFileNameWithoutExtension($FbxPath)
$headerJson = Join-Path $outDir "${base}_header.json"
$importJson = Join-Path $outDir "${base}_import_profile.json"

Write-Host "=== FBX header (no Blender) ===" -ForegroundColor Cyan
python $analyze $FbxPath $headerJson

if (-not (Test-Path -LiteralPath $BlenderExe)) {
    Write-Host "Blender not found at $BlenderExe" -ForegroundColor Red
    exit 1
}

Write-Host "`n=== Blender import profile (--background) ===" -ForegroundColor Cyan
& $BlenderExe --background --python $profile -- $FbxPath $importJson
Write-Host "Wrote: $importJson" -ForegroundColor Green
