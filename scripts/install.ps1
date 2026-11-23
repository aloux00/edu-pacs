# EduPACS 教学病例库系统 —— Windows 离线安装器
# ============================================================
# 适配部署包（zip 解压后）双击/右键运行；幂等可重复执行。
#
# 用法（推荐在项目根目录打开 PowerShell）：
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install.ps1
#
# 可选参数：
#   -IndexUrl <url>    pip 镜像源（默认清华 pypi；内网镜像可传自己的地址）
#   -Wheels <dir>      本地 wheels 目录（默认自动使用随包 wheels\，完全离线）
#   -WithFirewall     注册防火墙入站规则（8000/4242，需管理员权限）
#   -WithShortcuts    在当前用户桌面创建「EduPACS 教学病例库」快捷方式（指向启动器）
#   -WithAutostart    注册开机自启计划任务（以 SYSTEM 运行 start_all.ps1 -NoBrowser，
#                     不弹浏览器；需管理员权限）
#
# 流程：检测 Python → 建 .venv → 装锁定依赖 →（可选）防火墙/快捷方式/自启 → 汇总指引
param(
    [string]$IndexUrl = "https://pypi.tuna.tsinghua.edu.cn/simple",
    [string]$Wheels = "",
    [switch]$WithFirewall,
    [switch]$WithShortcuts,
    [switch]$WithAutostart
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $root

function Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Ok($msg)  { Write-Host "    $msg" -ForegroundColor Green }
function Warn($msg){ Write-Host "    $msg" -ForegroundColor Yellow }

$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
         ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

Step "安装位置：$root"

# ---------- 1. 检测 Python（.venv 已存在则直接复用） ----------
$venvPy = Join-Path $root ".venv\Scripts\python.exe"
if (Test-Path $venvPy) {
    Ok "已存在 .venv（依赖已装过，跳过环境创建；如需重装请先删除 .venv 目录）"
    $pyExe = $venvPy
} else {
    Step "检测 Python 3.12+"
    $cands = @()
    foreach ($c in @("py", "python")) {
        $cmd = Get-Command $c -ErrorAction SilentlyContinue
        if ($cmd) { $cands += $cmd.Source }
    }
    $pyExe = $null
    foreach ($c in $cands) {
        $ver = & $c -c "import sys;print('%d.%d'%sys.version_info[:2])" 2>$null
        if ($ver -and ([version]$ver -ge [version]"3.12")) { $pyExe = $c; Ok "找到 Python $ver（$c）"; break }
    }
    if (-not $pyExe) {
        # 兜底：常见安装路径探测（精简系统 PATH 缺失场景）
        foreach ($p in @(
            "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
            "$env:ProgramFiles\Python312\python.exe",
            "C:\Python312\python.exe")) {
            if (Test-Path $p) { $pyExe = $p; Ok "找到 Python（硬编码路径）：$p"; break }
        }
    }
    if (-not $pyExe) {
        Write-Host "    [错误] 未找到 Python 3.12+。请先安装 Python 3.12（勾选 Add to PATH），" -ForegroundColor Red
        Write-Host "    或将已有 .venv 目录从源机器一并拷贝过来后重试。" -ForegroundColor Red
        exit 1
    }

    Step "创建虚拟环境 .venv"
    & $pyExe -m venv .venv
    if ($LASTEXITCODE -ne 0) { Write-Host "    [错误] venv 创建失败" -ForegroundColor Red; exit 1 }
    Ok ".venv 已创建"
    $pyExe = $venvPy
}

# ---------- 2. 安装依赖（幂等：pip 重复安装很快） ----------
Step "安装依赖（requirements.lock.txt 全量版本锁定）"
if (-not (Test-Path $venvPy)) { Write-Host "    [错误] .venv 不可用" -ForegroundColor Red; exit 1 }
# 随包 wheels 是 Windows / Python 3.12 的完全离线依赖；存在即默认启用
$bundledWheels = Join-Path $root "wheels"
if (-not $Wheels -and (Test-Path (Join-Path $bundledWheels "*.whl"))) {
    $Wheels = $bundledWheels
    Ok "检测到随包离线 wheels：默认走完全离线安装"
}
$pipArgs = @("-m", "pip", "install", "-r", "requirements.lock.txt")
if ($Wheels -and (Test-Path $Wheels)) {
    $pipArgs += @("--no-index", "--find-links", (Resolve-Path $Wheels).Path)
    Ok "离线 wheels 模式：$Wheels"
} else {
    $pipArgs += @("-i", $IndexUrl)
    if ($Wheels) { Warn "-Wheels 指定的目录不存在，忽略并改用在线源" }
    else { Warn "未发现随包 wheels，将使用在线源：$IndexUrl" }
}
& $venvPy @pipArgs
if ($LASTEXITCODE -ne 0) {
    Write-Host "    [错误] pip 安装失败。可换源重试：" -ForegroundColor Red
    Write-Host "    powershell -File scripts\install.ps1 -IndexUrl https://mirrors.aliyun.com/pypi/simple" -ForegroundColor Yellow
    Write-Host "    或完全离线：在有网机 pip download -r requirements.lock.txt -d wheels `"仅 win_amd64 py312`"，把 wheels 目录拷来后加 -Wheels wheels" -ForegroundColor Yellow
    Write-Host "    注意：随包 wheels 仅适配 Windows 64 位 + Python 3.12；3.13/32 位请改装 3.12 后重试。" -ForegroundColor Yellow
    exit 1
}
Ok "依赖安装完成"

# ---------- 3. 防火墙（可选） ----------
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

# ---------- 4. 桌面快捷方式（可选） ----------
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

# ---------- 5. 开机自启计划任务（可选） ----------
if ($WithAutostart) {
    Step "注册开机自启计划任务（EduPACS-Autostart）"
    if ($admin) {
        $action  = "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$root\scripts\start_all.ps1`" -NoBrowser"
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
Write-Host "============================================================" -ForegroundColor Cyan
