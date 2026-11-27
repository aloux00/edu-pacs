# EduPACS 教学病例库系统 —— Windows 离线安装器（增强版）
# ============================================================
# 适配部署包（zip 解压后）双击/右键运行；幂等可重复执行。
#
# 用法（推荐在项目根目录打开 PowerShell）：
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install.ps1
#
# 可选参数：
#   -IndexUrl <url>    pip 镜像源（默认清华 pypi；仅当未使用随包 wheels 时生效）
#   -Wheels <dir>      本地 wheels 目录（默认自动使用随包 wheels\，完全离线）
#   -WithFirewall     注册防火墙入站规则（8000/4242，需管理员权限）
#   -WithShortcuts    在当前用户桌面创建「EduPACS 教学病例库」快捷方式（指向启动器）
#   -WithAutostart    注册开机自启计划任务（以 SYSTEM 运行 start_all.ps1 -NoBrowser）
#
# 本版改进：
#   1) 精确校验 Python 必须为 3.12.x 且 64 位（与随包 wheels 的 cp312-win_amd64 匹配）
#   2) 安装前预检 wheels 完整性，缺哪个包直接点名
#   3) 全过程写入 install.log；任何失败都暂停等待确认，窗口不再一闪而过
#   4) 失败时打印诊断信息（Python 版本/位数、wheels 数量、安装模式、日志路径）
#
# 流程：检测 Python → 建 .venv → 预检 wheels → 装锁定依赖 →（可选）防火墙/快捷方式/自启 → 汇总
param(
    [string]$IndexUrl = "https://pypi.tuna.tsinghua.edu.cn/simple",
    [string]$Wheels = "",
    [switch]$WithFirewall,
    [switch]$WithShortcuts,
    [switch]$WithAutostart,
    [switch]$NoPause
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
if (-not $root) { $root = (Get-Location).Path }
Set-Location $root

# ---------- 日志：全程落盘，出错后可回看 ----------
$logPath = Join-Path $root "install.log"
$transcriptOn = $false
try { Start-Transcript -Path $logPath -Force | Out-Null; $transcriptOn = $true }
catch { Write-Host "    [警告] 无法写入运行日志：$logPath" -ForegroundColor Yellow }

function Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Ok($msg)   { Write-Host "    $msg" -ForegroundColor Green }
function Warn($msg) { Write-Host "    $msg" -ForegroundColor Yellow }
function Err($msg)  { Write-Host "    [错误] $msg" -ForegroundColor Red }

# 供失败诊断使用的上下文
$pyInfo = $null
$bundledWheels = Join-Path $root "wheels"
$wheelFiles = @()
$pipMode = "未确定"
$hadError = $false

function Show-Diag {
    Write-Host ""
    Write-Host "---------------- 诊断信息（请一并反馈） ----------------" -ForegroundColor Yellow
    if ($pyInfo) { Write-Host "  Python   : $($pyInfo.Exe)  |  $($pyInfo.Ver)  |  $($pyInfo.Bits) 位" }
    else         { Write-Host "  Python   : 未找到符合要求的解释器（需 Python 3.12 64 位）" }
    Write-Host "  项目目录 : $root"
    Write-Host "  wheels   : $bundledWheels （$($wheelFiles.Count) 个 .whl）"
    Write-Host "  安装模式 : $pipMode"
    Write-Host "  运行日志 : $logPath"
    Write-Host "--------------------------------------------------------" -ForegroundColor Yellow
}

function Fail($msg) {
    Write-Host ""
    Write-Host "============================================================" -ForegroundColor Red
    Write-Host " 安装失败：$msg" -ForegroundColor Red
    Write-Host "============================================================" -ForegroundColor Red
    Write-Host " 完整日志已保存：$logPath" -ForegroundColor Yellow
    Write-Host " 请把 install.log 的内容发给技术支持，以便准确判断错误原因。" -ForegroundColor Yellow
}

$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
         ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

try {
    Step "安装位置：$root"

    # ---------- 1. 检测 Python：必须 3.12.x + 64 位 ----------
    function Get-PyInfo([string]$path) {
        $code = "import sys,struct;print('%d.%d.%d'%sys.version_info[:3]);print(struct.calcsize('P')*8)"
        $prev = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        $out = & $path -c $code 2>$null
        $codeRet = $LASTEXITCODE
        $ErrorActionPreference = $prev
        if ($codeRet -ne 0 -or -not $out -or $out.Count -lt 2) { return $null }
        return @{ Exe = $path; Ver = [string]$out[0]; Bits = [int]$out[1] }
    }

    $venvPy = Join-Path $root ".venv\Scripts\python.exe"
    if (Test-Path $venvPy) {
        # 已装过环境：仍要确认解释器位数/版本，避免旧环境带错 wheels
        $pyInfo = Get-PyInfo $venvPy
        Ok "已存在 .venv：$($pyInfo.Ver) / $($pyInfo.Bits) 位（如需重装请先删除 .venv 目录）"
    } else {
        Step "检测 Python 3.12.x（64 位，必须与随包 wheels 匹配）"

        $cands = @()

        # (a) 枚举 Windows py 启动器登记的所有解释器（ py -0p 每行末尾是真实 python.exe 路径）
        $prevEAP = $ErrorActionPreference; $ErrorActionPreference = "Continue"
        $pyList = & py -0p 2>$null
        $ErrorActionPreference = $prevEAP
        foreach ($line in $pyList) {
            if ($line -match '(\S+\.exe)\s*$') { $cands += $matches[1] }
        }
        # (b) PATH 上的 python / python3 / py
        foreach ($c in @("python", "python3", "py")) {
            $cmd = Get-Command $c -ErrorAction SilentlyContinue
            if ($cmd) { $cands += $cmd.Source }
        }
        # (c) uv 管理的 Python（py 启动器通常识别不到，但可直接调用）
        $uvRoot = Join-Path $env:APPDATA "uv\python"
        if (Test-Path $uvRoot) {
            $uvExes = Get-ChildItem $uvRoot -Filter "python.exe" -Recurse -File -ErrorAction SilentlyContinue |
                      Where-Object { $_.FullName -like "*3.12*" }
            foreach ($u in $uvExes) { $cands += $u.FullName }
        }
        # (d) 常见安装路径
        foreach ($p in @(
            "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
            "$env:LOCALAPPDATA\Programs\Python\Python312-64\python.exe",
            "$env:ProgramFiles\Python312\python.exe",
            "${env:ProgramFiles(x86)}\Python312\python.exe",
            "C:\Python312\python.exe")) {
            if (Test-Path $p) { $cands += $p }
        }

        $seen = @{}
        $found = @()
        foreach ($c in ($cands | Select-Object -Unique)) {
            if ($seen.ContainsKey($c)) { continue }
            $seen[$c] = $true
            $info = Get-PyInfo $c
            if ($info) {
                $found += "$($info.Ver)/$($info.Bits)位 ($c)"
                if ($info.Ver -like "3.12.*" -and $info.Bits -eq 64 -and -not $pyInfo) { $pyInfo = $info }
            }
        }

        if (-not $pyInfo) {
            $detail = if ($found.Count) { $found -join "；" } else { "未检测到任何 Python 解释器" }
            throw "未找到 Python 3.12.x（64 位）。随包离线依赖仅适配 Python 3.12 + 64 位，其他版本会导致 pip 报 'from versions: none'。`n    检测到的解释器：$detail`n    请到 python.org 下载并安装 Python 3.12 的 Windows installer (64-bit)，安装时勾选 Add Python to PATH，然后重新运行本脚本。"
        }
        Ok "找到 Python $($pyInfo.Ver) / $($pyInfo.Bits) 位：$($pyInfo.Exe)"

        Step "创建虚拟环境 .venv"
        & $pyInfo.Exe -m venv .venv
        if ($LASTEXITCODE -ne 0) { throw "venv 创建失败（退出码 $LASTEXITCODE）。可能是 Python 安装不完整或磁盘权限不足。" }
        $pyInfo = Get-PyInfo $venvPy
        if (-not $pyInfo) { throw "虚拟环境创建后无法启动：$venvPy" }
        Ok ".venv 已创建"
    }

    if ($pyInfo.Ver -notlike "3.12.*" -or $pyInfo.Bits -ne 64) {
        throw "当前 Python 为 $($pyInfo.Ver) / $($pyInfo.Bits) 位，与随包 wheels（cp312-win_amd64）不兼容。`n    请安装 Python 3.12 64 位后重试；或删除 .venv 后重新安装匹配的环境。"
    }

    # ---------- 2. 预检 wheels 完整性 ----------
    Step "检查安装依赖来源"
    if (-not $Wheels -and (Test-Path $bundledWheels)) {
        $wheelFiles = @(Get-ChildItem -Path $bundledWheels -Filter *.whl -File -ErrorAction SilentlyContinue)
        if ($wheelFiles.Count -gt 0) {
            $Wheels = $bundledWheels
            Ok "检测到随包离线 wheels：$($wheelFiles.Count) 个，默认走完全离线安装"
        }
    } elseif ($Wheels -and (Test-Path $Wheels)) {
        $wheelFiles = @(Get-ChildItem -Path $Wheels -Filter *.whl -File -ErrorAction SilentlyContinue)
    }

    $lockFile = Join-Path $root "requirements.lock.txt"
    if (-not (Test-Path $lockFile)) { throw "找不到依赖锁定文件：$lockFile。请确认部署包已完整解压到 $root。" }

    if ($Wheels -and (Test-Path $Wheels)) {
        $pipMode = "离线（--no-index --find-links $Wheels）"

        # 逐个核对 lock 中的包是否都有对应 wheel，避免 pip 抛 'from versions: none'
        $missing = @()
        foreach ($line in (Get-Content $lockFile)) {
            if ($line -match '^\s*([A-Za-z0-9_.\-]+)==') {
                $norm = ($matches[1] -replace '[-_.]', '-')
                $hit = $wheelFiles | Where-Object { (($_.BaseName -replace '[-_.]', '-') -like "$norm-*") }
                if (-not $hit) { $missing += $matches[1] }
            }
        }
        if ($missing.Count -gt 0) {
            throw "随包 wheels 缺少以下依赖的安装包：$($missing -join ', ')。`n    常见原因：部署包未完整解压、wheels 目录被安全软件删除、或拷贝时遗漏。`n    请重新完整解压部署包（确认 wheels 目录含 32 个 .whl）后重试。"
        }
        Ok "wheels 完整性预检通过（requirements.lock.txt 全部命中）"
    } else {
        $pipMode = "在线（$IndexUrl）"
        if ($Wheels) { Warn "-Wheels 指定目录不存在或不含 .whl，已改用在线源" }
        else         { Warn "未发现随包 wheels，将使用在线源：$IndexUrl（内网请确保能访问该地址）" }
    }

    # ---------- 3. 安装依赖 ----------
    Step "安装依赖（requirements.lock.txt 全量版本锁定）"
    if (-not (Test-Path $venvPy)) { throw "虚拟环境解释器不可用：$venvPy" }

    $pipArgs = @("-m", "pip", "install", "-r", "requirements.lock.txt")
    if ($Wheels -and (Test-Path $Wheels)) {
        $pipArgs += @("--no-index", "--find-links", (Resolve-Path $Wheels).Path)
    } else {
        $pipArgs += @("-i", $IndexUrl)
    }

    # 外部命令的 stderr 不应触发 terminate，临时放宽后靠 $LASTEXITCODE 判定
    $prevEAP = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    & $venvPy @pipArgs 2>&1 | ForEach-Object { Write-Host $_ }
    $pipCode = $LASTEXITCODE
    $ErrorActionPreference = $prevEAP

    if ($pipCode -ne 0) {
        throw "pip 安装失败（退出码 $pipCode）。`n    可能原因：网络/镜像不可达、Python 版本或位数与 wheels 不匹配、wheels 不完整。`n    可尝试：powershell -File scripts\install.ps1 -IndexUrl https://mirrors.aliyun.com/pypi/simple`n    或在有网机器执行 pip download -r requirements.lock.txt -d wheels --platform win_amd64 --python-version 312 --only-binary=:all: 后拷回 wheels 目录。"
    }
    Ok "依赖安装完成"

    # ---------- 4. 防火墙（可选） ----------
    if ($WithFirewall) {
        Step "注册防火墙入站规则（8000 / 4242）"
        if ($admin) {
            netsh advfirewall firewall delete rule name="EduPACS-Web-8000"   | Out-Null
            netsh advfirewall firewall delete rule name="EduPACS-DICOM-4242" | Out-Null
            netsh advfirewall firewall add rule name="EduPACS-Web-8000"   dir=in action=allow protocol=TCP localport=8000 | Out-Null
            netsh advfirewall firewall add rule name="EduPACS-DICOM-4242" dir=in action=allow protocol=TCP localport=4242 | Out-Null
            Ok "已放行 8000（师生访问）与 4242（DICOM 设备直推）；8042 未放行（仅本机）"
        } else {
            Warn "未以管理员运行，跳过防火墙规则。请以管理员重跑并加 -WithFirewall"
        }
    }

    # ---------- 5. 桌面快捷方式（可选） ----------
    if ($WithShortcuts) {
        Step "创建桌面快捷方式"
        $launcher = Join-Path $root "EduPACS启动器.bat"
        if (-not (Test-Path $launcher)) { $launcher = Join-Path $root "EduPACS启动器.exe" }
        $desktop = [Environment]::GetFolderPath("Desktop")
        $lnk = Join-Path $desktop "EduPACS 教学病例库.lnk"
        $ws = New-Object -ComObject WScript.Shell
        $sc = $ws.CreateShortcut($lnk)
        $sc.TargetPath = $launcher
        $sc.WorkingDirectory = $root
        $sc.IconLocation = "$(Join-Path $root 'EduPACS启动器.exe'),0"
        $sc.Description = "EduPACS 教学病例库启动器"
        $sc.Save()
        Ok "已创建：$lnk"
    }

    # ---------- 6. 开机自启计划任务（可选） ----------
    if ($WithAutostart) {
        Step "注册开机自启计划任务（EduPACS-Autostart）"
        if ($admin) {
            $action = "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$root\scripts\start_all.ps1`" -NoBrowser"
            schtasks /Delete /TN "EduPACS-Autostart" /F 2>$null | Out-Null
            schtasks /Create /TN "EduPACS-Autostart" /TR $action /SC ONSTART /RU SYSTEM /RL HIGHEST /F | Out-Null
            if ($LASTEXITCODE -eq 0) { Ok "已注册（开机自动拉起两服务，不弹浏览器）" }
            else { Warn "计划任务注册失败，请手动检查 schtasks 输出" }
        } else {
            Warn "未以管理员运行，跳过自启任务。请以管理员重跑并加 -WithAutostart"
        }
    }

    # ---------- 汇总 ----------
    $lan = (Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
            Where-Object { $_.IPAddress -notlike "127.*" -and $_.PrefixOrigin -ne "WellKnown" } |
            Select-Object -First 1).IPAddress
    Write-Host ""
    Write-Host "============================================================" -ForegroundColor Cyan
    Write-Host " EduPACS 安装完成" -ForegroundColor Cyan
    Write-Host "   下一步（三种任选）："
    Write-Host "     1. 双击 EduPACS启动器.bat / .exe → 点「一键启动」"
    Write-Host "     2. 运行 scripts\start_all.ps1"
    Write-Host "     3. 命令行：EduPACS启动器.exe --start"
    Write-Host ""
    Write-Host "   本机访问：http://127.0.0.1:8000"
    if ($lan) { Write-Host "   师生访问：http://$lan`:8000" }
    Write-Host "   默认账号：admin / admin123（首次登录强制改密）"
    Write-Host ""
    Write-Host "   部署后必做冒烟：.venv\Scripts\python tools\smoke_test.py --orthanc-user edupacs_service --orthanc-pass <orthanc.json 里的密码>"
    Write-Host "   运行日志：$logPath"
    Write-Host "============================================================" -ForegroundColor Cyan
}
catch {
    $hadError = $true
    Fail $_.Exception.Message
    Show-Diag
}

try { if ($transcriptOn) { Stop-Transcript | Out-Null } } catch { }

if ($hadError) {
    Write-Host ""
    # -NoPause 仅供自动化测试使用；用户双击运行时会在此暂停，避免窗口一闪而过
    if (-not $NoPause) {
        Read-Host "安装未完成。请查看上方红色错误信息与 install.log，按回车键关闭窗口"
    }
    exit 1
}
