<#
.SYNOPSIS
    EduPACS 服务状态与快捷管理（不需要管理员权限查看，启停需要）
#>
param(
    [ValidateSet('Show', 'Start', 'Stop', 'Restart')]
    [string]$Action = 'Show'
)

$root = Split-Path -Parent $PSScriptRoot
$svcDir = Join-Path $root 'services'
$names = @('EduPACS-Orthanc', 'EduPACS-App')

function Get-OrthancBasicHeader {
    try {
        $cfg = Get-Content (Join-Path $root 'orthanc\orthanc.json') -Raw -Encoding UTF8 | ConvertFrom-Json
        $u = $cfg.RegisteredUsers.PSObject.Properties | Select-Object -First 1
        if ($u) {
            $pair = "$($u.Name):$($u.Value)"
            return @{ Authorization = 'Basic ' + [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($pair)) }
        }
    } catch { }
    return @{}
}

if ($Action -in @('Start', 'Stop', 'Restart')) {
    $principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        Write-Host "  [错误] 启停服务需要管理员权限，请右键以管理员身份运行。" -ForegroundColor Red
        exit 1
    }
    $order = if ($Action -eq 'Start') { $names } else { @('EduPACS-App', 'EduPACS-Orthanc') }
    foreach ($n in $order) {
        $exe = Join-Path $svcDir ($n + '.exe')
        $verb = if ($Action -eq 'Restart') { 'restart' } else { $Action.ToLower() }
        Write-Host ("  " + $n + " : " + $verb)
        & $exe $verb 2>&1 | Out-Null
        if ($Action -eq 'Restart') { Start-Sleep -Milliseconds 800 }
    }
    Start-Sleep -Seconds 2
}

Write-Host ""
Write-Host "========== EduPACS Windows 服务状态 ==========" -ForegroundColor Cyan
foreach ($n in $names) {
    $exe = Join-Path $svcDir ($n + '.exe')
    if (-not (Test-Path $exe)) { Write-Host ("  " + $n + " : WinSW 文件缺失"); continue }
    $raw = & $exe status 2>&1 | Out-String
    $st = $raw.Trim()
    $svc = Get-Service -Name $n -ErrorAction SilentlyContinue
    if ($st -eq 'NonExistent') {
        Write-Host ("  " + $n + " : 未安装") -ForegroundColor Yellow
    } else {
        $color = if ($st -eq 'Running') { 'Green' } else { 'Red' }
        $startType = if ($svc) { $svc.StartType } else { '?' }
        Write-Host ("  " + $n + " : " + $st + "  (启动类型 " + $startType + ")") -ForegroundColor $color
    }
}

$hdr = Get-OrthancBasicHeader
$o = $false; $a = $false
try { Invoke-WebRequest 'http://127.0.0.1:8042/system' -UseBasicParsing -TimeoutSec 3 -Headers $hdr | Out-Null; $o = $true } catch { }
try { Invoke-WebRequest 'http://127.0.0.1:8000/api/config' -UseBasicParsing -TimeoutSec 3 | Out-Null; $a = $true } catch { }
Write-Host ("  Orthanc HTTP 8042  : " + $(if ($o) { '可访问' } else { '不可访问' })) -ForegroundColor $(if ($o) { 'Green' } else { 'Red' })
Write-Host ("  教学系统 HTTP 8000 : " + $(if ($a) { '可访问' } else { '不可访问' })) -ForegroundColor $(if ($a) { 'Green' } else { 'Red' })
Write-Host "==============================================" -ForegroundColor Cyan
