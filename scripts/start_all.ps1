# EduPACS 教学病例库 一键启动脚本
# 用法：右键"使用 PowerShell 运行"，或命令行 .\scripts\start_all.ps1
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $root

function Test-Port($port) {
    return $null -ne (Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue)
}

# 1) 启动 Orthanc（若未运行）
if (-not (Test-Port 8042)) {
    Write-Host "[1/3] 启动 Orthanc (HTTP 8042 / DICOM 4242)..."
    Start-Process -FilePath "$root\orthanc\Orthanc.exe" `
        -ArgumentList ('"{0}"' -f "$root\orthanc\orthanc.json") `
        -WorkingDirectory "$root\orthanc" -WindowStyle Minimized
} else {
    Write-Host "[1/3] Orthanc 已在运行，跳过"
}

# 2) 启动教学系统后端（若未运行）
if (-not (Test-Port 8000)) {
    $py = "$root\.venv\Scripts\python.exe"
    if (-not (Test-Path $py)) {
        # 依赖尚未安装：给出明确指引而不是报一串路径错误
        Write-Host ""
        Write-Host "  [错误] 未找到 .venv\Scripts\python.exe —— Python 依赖尚未安装。" -ForegroundColor Red
        Write-Host "  请先在项目根目录执行（详见 docs\部署与使用手册.md 第 4.2 节）：" -ForegroundColor Yellow
        Write-Host "      python -m venv .venv" -ForegroundColor Yellow
        Write-Host "      .venv\Scripts\pip install -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple" -ForegroundColor Yellow
        Write-Host ""
        exit 1
    }
    Write-Host "[2/3] 启动教学病例库系统 (HTTP 8000)..."
    Start-Process -FilePath $py `
        -ArgumentList '-m', 'uvicorn', 'app.backend.main:app', '--host', '0.0.0.0', '--port', '8000' `
        -WorkingDirectory $root -WindowStyle Minimized
} else {
    Write-Host "[2/3] 教学系统已在运行，跳过"
}

# 3) 健康检查
Write-Host "[3/3] 等待服务就绪..."
$okOrthanc = $false; $okApp = $false
# Orthanc 已开启 HTTP 认证：从 orthanc.json 读取服务账号生成 Basic 认证头
$orthancHeaders = @{}
try {
    $cfgJson = Get-Content "$root\orthanc\orthanc.json" -Raw -Encoding UTF8 | ConvertFrom-Json
    $ru = $cfgJson.RegisteredUsers
    if ($ru) {
        $first = ($ru.PSObject.Properties | Select-Object -First 1)
        $pair = "$($first.Name):$($first.Value)"
        $b64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($pair))
        $orthancHeaders = @{ Authorization = "Basic $b64" }
    }
} catch { }
for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Seconds 1
    if (-not $okOrthanc) {
        try { Invoke-WebRequest -Uri 'http://127.0.0.1:8042/system' -UseBasicParsing -TimeoutSec 2 -Headers $orthancHeaders | Out-Null; $okOrthanc = $true } catch {}
    }
    if (-not $okApp) {
        try { Invoke-WebRequest -Uri 'http://127.0.0.1:8000/api/config' -UseBasicParsing -TimeoutSec 2 | Out-Null; $okApp = $true } catch {}
    }
    if ($okOrthanc -and $okApp) { break }
}
Write-Host ""
if ($okOrthanc) { Write-Host "  Orthanc 就绪   : http://127.0.0.1:8042（仅本机，已启用认证）" -ForegroundColor Green }
else { Write-Host "  Orthanc 未就绪，请查看 orthanc 目录下日志或手动运行 orthanc\Orthanc.exe orthanc\orthanc.json" -ForegroundColor Red }
if ($okApp) { Write-Host "  教学系统就绪   : http://127.0.0.1:8000（OHIF 阅片入口同源，默认账号 admin / admin123，首登强制改密）" -ForegroundColor Green }
else { Write-Host "  教学系统未就绪，请手动运行: .venv\Scripts\python -m uvicorn app.backend.main:app --host 0.0.0.0 --port 8000" -ForegroundColor Red }
Write-Host ""
$browser = $null
$roots = @()
try { $x86 = [Environment]::GetEnvironmentVariable('ProgramFiles(x86)'); if ($x86) { $roots += $x86 } } catch { }
try { $pf = [Environment]::GetEnvironmentVariable('ProgramFiles'); if ($pf) { $roots += $pf } } catch { }
$roots += @('C:\Program Files (x86)', 'C:\Program Files')
foreach ($r in $roots) {
    if (-not $r) { continue }
    foreach ($exe in @('Microsoft\Edge\Application\msedge.exe', 'Google\Chrome\Application\chrome.exe')) {
        $p = Join-Path $r $exe
        if (Test-Path -LiteralPath $p) { $browser = $p; break }
    }
    if ($browser) { break }
}
if ($okApp -and $browser -and -not $NoBrowser) { Start-Process -FilePath $browser -ArgumentList 'http://127.0.0.1:8000' }
elseif (-not $browser) { Write-Host "  请手动在浏览器打开 http://127.0.0.1:8000" -ForegroundColor Yellow }
