<#
  EduPACS teaching case library - safe stop script
  Purpose: bypass the native launcher --stop crash (Illegal instruction).
  Strategy: stop only processes whose command line proves they belong to this project:
      * process name Orthanc.exe / python.exe / pythonw.exe
      * AND command line contains this project root, or app.backend.main
  Usage: powershell.exe -NoProfile -ExecutionPolicy Bypass -File safe_stop.ps1 [-NoPause]
#>
param([switch]$NoPause)

$ErrorActionPreference = 'Stop'
# script lives in <root>\scripts ; project root is its parent
$root = Split-Path -Parent $PSScriptRoot
$rootNorm = $root.Replace('/', '\')

Write-Host "EduPACS safe stop"
Write-Host ("Project root: " + $root)
Write-Host ""

# exact match of this project's processes
$mine = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
    $_.Name -in @('Orthanc.exe', 'python.exe', 'pythonw.exe') -and $_.CommandLine -and
    (($_.CommandLine.Replace('/', '\') -like ("*" + $rootNorm + "*")) -or
     ($_.CommandLine -like '*app.backend.main*'))
})

if ($mine.Count -eq 0) {
    Write-Host "No project service process found (already stopped?)."
} else {
    Write-Host "Found project processes, stopping:"
    foreach ($p in $mine) {
        $cl = ($p.CommandLine -replace '\s+', ' ')
        Write-Host ("  - {0}  PID={1}" -f $p.Name, $p.ProcessId)
        Write-Host ("      " + $cl)
    }
    Write-Host ""
    $killed = @()
    foreach ($p in $mine) {
        try {
            Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop
            $killed += ("{0}(PID{1})" -f $p.Name, $p.ProcessId)
        } catch {
            Write-Host ("  [WARN] cannot stop PID={0}: {1}" -f $p.ProcessId, $_.Exception.Message)
        }
    }
    Write-Host ""
    if ($killed.Count) { Write-Host ("Stopped: " + ($killed -join ', ')) }
    else { Write-Host "Stopped: none" }
}

Start-Sleep -Milliseconds 1000

function Test-Port([int]$port) {
    $c = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    return [bool]$c
}
$appUp = Test-Port 8000
$orthUp = Test-Port 8042
$appTxt = if ($appUp) { 'RUNNING' } else { 'STOPPED' }
$orthTxt = if ($orthUp) { 'RUNNING' } else { 'STOPPED' }
Write-Host ""
Write-Host ("Recheck: App(8000)=$appTxt ; Orthanc(8042)=$orthTxt")

if (-not $NoPause) {
    Write-Host ""
    Read-Host "Press Enter to close"
}
