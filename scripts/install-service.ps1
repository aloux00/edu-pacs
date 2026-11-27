<#
.SYNOPSIS
    EduPACS 教学病例库 - 安装为 Windows 服务（开机自启、崩溃自动重启）
.DESCRIPTION
    用 WinSW 把 Orthanc 与教学系统后端注册为两个真正的 Windows 服务。
    必须用管理员权限运行（右键"以管理员身份运行"或 install-service.bat）。
    幂等：已安装会先卸载再重装，不会重复注册。
.PARAMETER NoPause
    自动化用：出错后不等待回车。
.PARAMETER NoStart
    只注册服务，不立即启动（重启后会自动启动）。
#>
param(
    [switch]$NoPause,
    [switch]$NoStart
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot          # 项目根
$svcDir = Join-Path $root 'services'
$logDir = Join-Path $root 'logs'
$transcript = Join-Path $root 'install-service.log'
$hadError = $false

try { Start-Transcript -Path $transcript -Force | Out-Null } catch { }

function Fail([string]$msg) {
    $script:hadError = $true
    Write-Host ""
    Write-Host ("  [错误] " + $msg) -ForegroundColor Red
    throw $msg
}
function Info([string]$msg) { Write-Host $msg -ForegroundColor Cyan }
function Ok([string]$msg)   { Write-Host $msg -ForegroundColor Green }

function Show-Diag {
    Write-Host ""
    Write-Host "---------- 诊断信息 ----------" -ForegroundColor Yellow
    Write-Host ("项目根目录 : " + $root)
    Write-Host ("服务目录   : " + $svcDir)
    Write-Host ("运行账号   : " + [Security.Principal.WindowsIdentity]::GetCurrent().Name)
    $adm = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
    Write-Host ("管理员权限 : " + $adm.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator))
    Write-Host ("Orthanc exe: " + (Test-Path (Join-Path $root 'orthanc\Orthanc.exe')))
    Write-Host ("后端 python: " + (Test-Path (Join-Path $root '.venv\Scripts\python.exe')))
    Write-Host ("WinSW Orthanc: " + (Test-Path (Join-Path $svcDir 'EduPACS-Orthanc.exe')))
    Write-Host ("WinSW App    : " + (Test-Path (Join-Path $svcDir 'EduPACS-App.exe')))
    Write-Host ("日志文件   : " + $transcript)
    Write-Host "------------------------------" -ForegroundColor Yellow
}

function Test-PortOpen([int]$port) {
    return ($null -ne (Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue))
}

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

try {
    Write-Host ""
    Write-Host "============================================================" -ForegroundColor Cyan
    Write-Host " EduPACS 教学病例库 - 安装为 Windows 服务" -ForegroundColor Cyan
    Write-Host "============================================================" -ForegroundColor Cyan
    Write-Host ""

    # 1) 管理员权限
    $principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        Fail "没有管理员权限。请右键install-service.bat选择『以管理员身份运行』。"
    }
    Info "[1/7] 管理员权限检查通过"

    # 2) 文件完整性
    $need = @(
        (Join-Path $root 'orthanc\Orthanc.exe'),
        (Join-Path $root 'orthanc\orthanc.json'),
        (Join-Path $root '.venv\Scripts\python.exe'),
        (Join-Path $root 'app\backend\main.py'),
        (Join-Path $svcDir 'EduPACS-Orthanc.exe'),
        (Join-Path $svcDir 'EduPACS-Orthanc.xml'),
        (Join-Path $svcDir 'EduPACS-App.exe'),
        (Join-Path $svcDir 'EduPACS-App.xml')
    )
    $missing = $need | Where-Object { -not (Test-Path $_) }
    if ($missing) { Fail ("缺少文件：`n" + ($missing -join "`n")) }
    if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir -Force | Out-Null }
    Info "[2/7] 文件完整性检查通过（8/8）"

    # 3) 端口冲突检查：若被非本项目进程占用则停止
    foreach ($port in 8000, 8042) {
        $conn = Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($conn) {
            $owner = Get-CimInstance Win32_Process -Filter ("ProcessId=" + $conn.OwningProcess) -ErrorAction SilentlyContinue
            $cmd = [string]$owner.CommandLine
            $mine = $cmd -like '*app.backend.main*' -or $cmd.Replace('/', '\') -like ('*' + $root.Replace('/', '\') + '*')
            if ($mine) {
                Write-Host ("  端口 " + $port + " 被本项目的既有进程占用（PID " + $conn.OwningProcess + "），安装后请先停止它。" ) -ForegroundColor Yellow
            } else {
                Fail ("端口 " + $port + " 被其它程序占用（" + $owner.Name + " PID " + $conn.OwningProcess + "），请先关闭该程序。")
            }
        }
    }
    Info "[3/7] 端口检查完成"

    # 4) 冲突的自启计划任务：存在则禁用，避免与服务同时拉起进程
    $task = Get-ScheduledTask -TaskName 'EduPACS-Autostart' -ErrorAction SilentlyContinue
    if ($task -and $task.State -ne 'Disabled') {
        Write-Host "  检测到旧的开机自启计划任务 EduPACS-Autostart，正在禁用（避免与服务冲突）..." -ForegroundColor Yellow
        Disable-ScheduledTask -TaskName 'EduPACS-Autostart' -ErrorAction SilentlyContinue | Out-Null
        Write-Host "  已禁用。如需恢复请运行：Enable-ScheduledTask -TaskName EduPACS-Autostart" -ForegroundColor Yellow
    }
    Info "[4/7] 自启冲突检查完成"

    # 5) 卸载旧服务（幂等）
    foreach ($name in @('EduPACS-Orthanc', 'EduPACS-App')) {
        $exe = Join-Path $svcDir ($name + '.exe')
        $st = & $exe status 2>&1 | Out-String
        if ($st.Trim() -ne 'NonExistent') {
            Write-Host ("  发现已安装服务 " + $name + "，先停止并卸载...")
            & $exe stop 2>&1 | Out-Null
            Start-Sleep -Milliseconds 800
            & $exe uninstall 2>&1 | Out-Null
            Start-Sleep -Milliseconds 500
        }
    }
    Info "[5/7] 旧服务清理完成"

    # 6) 安装两个服务（顺序：Orthanc 先，App 后，App 依赖 Orthanc）
    foreach ($name in @('EduPACS-Orthanc', 'EduPACS-App')) {
        $exe = Join-Path $svcDir ($name + '.exe')
        $out = & $exe install 2>&1 | Out-String
        Write-Host ("  " + $name + ": " + $out.Trim())
        $st = & $exe status 2>&1 | Out-String
        if ($st.Trim() -eq 'NonExistent') { Fail ($name + " 安装失败，详见 " + $transcript) }
        $sc = Get-Service -Name $name -ErrorAction SilentlyContinue
        if (-not $sc) { Fail ($name + " 未在服务管理器中注册") }
        Ok ("  " + $name + " 已注册（启动类型：Automatic）")
    }
    Info "[6/7] 两个服务注册完成"

    if ($NoStart) {
        Write-Host ""
        Ok "仅注册完成，未启动。下次开机会自动启动。"
    } else {
        # 7) 启动 + 健康检查
        Info "[7/7] 正在启动服务并等待就绪（最多 60 秒）..."
        foreach ($name in @('EduPACS-Orthanc', 'EduPACS-App')) {
            $exe = Join-Path $svcDir ($name + '.exe')
            & $exe start 2>&1 | Out-Null
        }
        $hdr = Get-OrthancBasicHeader
        $okO = $false; $okA = $false
        for ($i = 0; $i -lt 60; $i++) {
            Start-Sleep -Seconds 1
            if (-not $okO) {
                try { Invoke-WebRequest 'http://127.0.0.1:8042/system' -UseBasicParsing -TimeoutSec 2 -Headers $hdr | Out-Null; $okO = $true } catch { }
            }
            if (-not $okA) {
                try { Invoke-WebRequest 'http://127.0.0.1:8000/api/config' -UseBasicParsing -TimeoutSec 2 | Out-Null; $okA = $true } catch { }
            }
            if ($okO -and $okA) { break }
        }
        Write-Host ""
        if ($okO) { Ok "  Orthanc 服务   : 就绪 (8042)" } else { Write-Host "  Orthanc 服务   : 未就绪（查 logs\ 目录）" -ForegroundColor Red }
        if ($okA) { Ok "  教学系统服务   : 就绪 (8000)" } else { Write-Host "  教学系统服务   : 未就绪（查 logs\ 目录）" -ForegroundColor Red }
        if (-not ($okO -and $okA)) { Fail "服务已注册但健康检查未通过，请查看 logs 目录与 services 下日志" }
    }

    Write-Host ""
    Write-Host "============================================================" -ForegroundColor Green
    Ok " 安装完成！两个服务已设为开机自启，后台运行，无需登录桌面。"
    Write-Host " 管理命令：services.msc  /  net start|stop EduPACS-Orthanc"
    Write-Host "          net start|stop EduPACS-App  /  scripts\service-status.ps1"
    Write-Host " 卸载命令：scripts\uninstall-service.ps1"
    Write-Host "============================================================" -ForegroundColor Green
}
catch {
    $script:hadError = $true
    Show-Diag
}
finally {
    try { Stop-Transcript | Out-Null } catch { }
    Write-Host ""
    if ($hadError) {
        Write-Host "安装未完成。请查看上方错误信息与 install-service.log。" -ForegroundColor Red
        if (-not $NoPause) { Read-Host "按回车键关闭窗口" }
        exit 1
    }
    if (-not $NoPause) { Read-Host "安装完成，按回车键关闭窗口" }
    exit 0
}
