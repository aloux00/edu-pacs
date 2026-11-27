<#
.SYNOPSIS
    EduPACS 教学病例库 - 卸载 Windows 服务（不影响数据与安装文件）
.DESCRIPTION
    停止并删除 EduPACS-App、EduPACS-Orthanc 两个服务。
    数据库、影像、备份、日志全部保留；不会删除任何项目文件。
.PARAMETER NoPause
    自动化用：出错后不等待回车。
#>
param([switch]$NoPause)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$svcDir = Join-Path $root 'services'
$hadError = $false

function Fail([string]$msg) {
    $script:hadError = $true
    Write-Host ("  [错误] " + $msg) -ForegroundColor Red
    throw $msg
}

try {
    Write-Host ""
    Write-Host "EduPACS - 卸载 Windows 服务" -ForegroundColor Cyan
    $principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        Fail "没有管理员权限。请右键 uninstall-service.bat 选择『以管理员身份运行』。"
    }

    # App 依赖 Orthanc，先停 App
    foreach ($name in @('EduPACS-App', 'EduPACS-Orthanc')) {
        $exe = Join-Path $svcDir ($name + '.exe')
        if (-not (Test-Path $exe)) { Write-Host ("  跳过 " + $name + "（未找到 WinSW 文件）") -ForegroundColor Yellow; continue }
        $st = & $exe status 2>&1 | Out-String
        if ($st.Trim() -eq 'NonExistent') {
            Write-Host ("  " + $name + " 未安装，跳过")
            continue
        }
        Write-Host ("  停止 " + $name + "...")
        & $exe stop 2>&1 | Out-Null
        Start-Sleep -Milliseconds 800
        Write-Host ("  卸载 " + $name + "...")
        & $exe uninstall 2>&1 | Out-Null
        Start-Sleep -Milliseconds 500
        $st2 = & $exe status 2>&1 | Out-String
        if ($st2.Trim() -ne 'NonExistent') { Fail ($name + " 卸载失败") }
        Write-Host ("  " + $name + " 已卸载") -ForegroundColor Green
    }

    Write-Host ""
    Write-Host "完成。数据（app\data、orthanc\storage、logs、backups）均已保留。" -ForegroundColor Green
    Write-Host "如需恢复开机自启的计划任务：Enable-ScheduledTask -TaskName EduPACS-Autostart"
}
catch { $script:hadError = $true }
finally {
    Write-Host ""
    if ($hadError -and -not $NoPause) { Read-Host "卸载遇到问题，按回车键关闭窗口" }
    elseif (-not $NoPause) { Read-Host "卸载完成，按回车键关闭窗口" }
    exit $(if ($hadError) { 1 } else { 0 })
}
