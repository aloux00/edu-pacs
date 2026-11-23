# EduPACS 教学病例库系统 - 图形启动器 v2
# 双击项目根目录的 EduPACS启动器.bat 使用
# v2 变更：
#   1) 点击审计：每次按钮点击都会写入 .temp\launcher_click.log（便于诊断"点击无反应"）
#   2) 启停改为子进程执行（-TestAction Start/Stop），界面线程不再阻塞，杜绝卡死
#   3) 全局异常钩子 + 窗体关闭留痕，任何异常/退出都有日志可查
param(
    [switch]$Test,
    [ValidateSet('', 'Status', 'Start', 'Stop')]
    [string]$TestAction = ''
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$errFile = Join-Path $root '.temp\launcher_error.log'
$clickFile = Join-Path $root '.temp\launcher_click.log'
$launcherPs1 = Join-Path $root 'scripts\launcher.ps1'

function Write-Log([string]$file, [string]$msg) {
    try {
        $dir = Split-Path -Parent $file
        if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
        Add-Content -LiteralPath $file -Value ("[{0} pid={1}] {2}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $PID, $msg) -Encoding UTF8
    } catch { }
}
function Write-ErrLog([string]$msg) { Write-Log $errFile $msg }
function Write-ClickLog([string]$msg) { Write-Log $clickFile $msg }

function Test-Url([string]$url) {
    try {
        $r = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 2 -Headers (Get-OrthancAuthHeader)
        return ($r.StatusCode -eq 200)
    } catch { return $false }
}

function Test-PortOpen([int]$port) {
    return ($null -ne (Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue))
}

# Orthanc 已开启 HTTP 认证：读 orthanc.json 服务账号生成 Basic 认证头（应用/教学系统无需认证）
function Get-OrthancAuthHeader {
    try {
        $cfgJson = Get-Content (Join-Path $root 'orthanc\orthanc.json') -Raw -Encoding UTF8 | ConvertFrom-Json
        $ru = $cfgJson.RegisteredUsers
        if ($ru) {
            $first = ($ru.PSObject.Properties | Select-Object -First 1)
            $pair = "$($first.Name):$($first.Value)"
            $b64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($pair))
            return @{ Authorization = "Basic $b64" }
        }
    } catch { }
    return @{}
}

function Get-ServiceState {
    return @{
        Orthanc = (Test-Url 'http://127.0.0.1:8042/system')
        App     = (Test-Url 'http://127.0.0.1:8000/api/config')
    }
}

function Open-Url([string]$url) {
    # 不依赖 ${env:ProgramFiles(x86)}（该写法在部分上下文取值为空，导致 Join-Path 抛异常）
    $roots = @()
    try { $x86 = [Environment]::GetEnvironmentVariable('ProgramFiles(x86)'); if ($x86) { $roots += $x86 } } catch { }
    try { $pf = [Environment]::GetEnvironmentVariable('ProgramFiles'); if ($pf) { $roots += $pf } } catch { }
    $roots += @('C:\Program Files (x86)', 'C:\Program Files')
    foreach ($r in $roots) {
        if (-not $r) { continue }
        foreach ($exe in @('Microsoft\Edge\Application\msedge.exe', 'Google\Chrome\Application\chrome.exe')) {
            $p = Join-Path $r $exe
            if (Test-Path -LiteralPath $p) { Start-Process -FilePath $p -ArgumentList $url; return $true }
        }
    }
    try { Start-Process $url; return $true } catch { return $false }
}

function Get-LanIp {
    try {
        $ip = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
            Where-Object { $_.IPAddress -notmatch '^(127\.|169\.254\.)' -and $_.PrefixOrigin -ne 'WellKnown' } |
            Select-Object -First 1 -ExpandProperty IPAddress
        if ($ip) { return $ip }
    } catch { }
    try {
        return ([System.Net.Dns]::GetHostAddresses([System.Net.Dns]::GetHostName()) |
            Where-Object { $_.AddressFamily -eq 'InterNetwork' -and $_.ToString() -notmatch '^127\.' } |
            Select-Object -First 1).ToString()
    } catch { return '127.0.0.1' }
}

function New-AppIcon {
    $path = Join-Path $root 'scripts\edupacs.ico'
    if (Test-Path $path) { return $path }
    try {
        Add-Type -AssemblyName System.Drawing
        $bmp = New-Object System.Drawing.Bitmap 64, 64
        $g = [System.Drawing.Graphics]::FromImage($bmp)
        $g.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
        $brush = New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::FromArgb(15, 118, 110))
        $g.FillRectangle($brush, 0, 0, 64, 64)
        $font = New-Object System.Drawing.Font('Microsoft YaHei', 30, [System.Drawing.FontStyle]::Bold)
        $fmt = New-Object System.Drawing.StringFormat
        $fmt.Alignment = [System.Drawing.StringAlignment]::Center
        $fmt.LineAlignment = [System.Drawing.StringAlignment]::Center
        $rect = New-Object System.Drawing.RectangleF 0, 0, 64, 64
        $g.DrawString('教', $font, [System.Drawing.Brushes]::White, $rect, $fmt)
        $hIcon = $bmp.GetHicon()
        $icon = [System.Drawing.Icon]::FromHandle($hIcon)
        $fs = [System.IO.File]::Create($path)
        $icon.Save($fs)
        $fs.Close()
        $g.Dispose(); $bmp.Dispose(); $icon.Dispose()
        return $path
    } catch { return $null }
}

# ---------------- 无界面测试入口（也是 GUI 按钮调用的子进程通道） ----------------
if ($Test -or $TestAction) {
    try {
        Write-ClickLog ("cli: TestAction=" + $TestAction)
        if ($TestAction -eq 'Start') {
            $orthancExe = Join-Path $root 'orthanc\Orthanc.exe'
            $orthancCfg = Join-Path $root 'orthanc\orthanc.json'
            if (-not (Test-PortOpen 8042)) {
                if (-not (Test-Path $orthancExe)) { throw "未找到 orthanc\Orthanc.exe，请确认部署包完整" }
                Start-Process -FilePath $orthancExe -ArgumentList ('"{0}"' -f $orthancCfg) `
                    -WorkingDirectory (Join-Path $root 'orthanc') -WindowStyle Minimized
            }
            if (-not (Test-PortOpen 8000)) {
                $py = Join-Path $root '.venv\Scripts\python.exe'
                if (-not (Test-Path $py)) { $py = 'python' }
                Start-Process -FilePath $py `
                    -ArgumentList '-m', 'uvicorn', 'app.backend.main:app', '--host', '0.0.0.0', '--port', '8000' `
                    -WorkingDirectory $root -WindowStyle Minimized
            }
            $deadline = (Get-Date).AddSeconds(45)
            do { Start-Sleep -Milliseconds 800 } while ((Get-Date) -lt $deadline -and -not ((Test-Url 'http://127.0.0.1:8042/system') -and (Test-Url 'http://127.0.0.1:8000/api/config')))
        }
        elseif ($TestAction -eq 'Stop') {
            # 安全停止：只停命令行能证明属于本项目的进程（引用本包路径或 app.backend.main），
            # 不再按端口+进程名盲杀，避免误杀同机其它项目的 Orthanc/python 服务
            $rootNorm = $root.Replace('/', '\')
            $killed = @()
            $mine = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
                $_.Name -in @('Orthanc.exe', 'python.exe', 'pythonw.exe') -and $_.CommandLine -and
                (($_.CommandLine.Replace('/', '\') -like ("*" + $rootNorm + "*")) -or
                 ($_.CommandLine -like '*app.backend.main*'))
            }
            foreach ($p in $mine) {
                Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
                $killed += ("{0}(PID{1})" -f $p.Name, $p.ProcessId)
            }
            Start-Sleep -Milliseconds 800
            Write-Output ("STOPPED: " + ($(if ($killed.Count) { $killed -join ', ' } else { '无' })))
        }
        $st = Get-ServiceState
        Write-Output ("Orthanc=" + $st.Orthanc + " App=" + $st.App)
        Write-Output ("LAN_IP=" + (Get-LanIp))
        Write-Output "TEST_OK"
        Write-ClickLog ("cli: done " + $TestAction + " O=" + $st.Orthanc + " A=" + $st.App)
        exit 0
    } catch {
        Write-Output ("TEST_FAIL: " + $_.Exception.Message)
        Write-ErrLog ("cli " + $TestAction + ": " + $_.Exception.Message)
        exit 1
    }
}

# ---------------- 图形界面 ----------------
try {
    Add-Type -AssemblyName System.Windows.Forms
    Add-Type -AssemblyName System.Drawing
    [System.Windows.Forms.Application]::EnableVisualStyles()

    # 全局异常钩子：任何未捕获异常都留日志（不再无声消失）
    try {
        [System.Windows.Forms.Application]::add_ThreadException({
            param($s, $e)
            try { Write-ErrLog ("ThreadException: " + $e.Exception.Message) } catch { }
        })
    } catch { }
    try {
        [System.AppDomain]::CurrentDomain.add_UnhandledException({
            param($s, $e)
            try { Write-ErrLog ("Unhandled: " + $e.ExceptionObject) } catch { }
        })
    } catch { }

    $form = New-Object System.Windows.Forms.Form
    $form.Text = 'EduPACS 教学病例库系统 · 启动器'
    $form.Size = New-Object System.Drawing.Size 490, 488
    $form.StartPosition = 'CenterScreen'
    $form.FormBorderStyle = 'FixedSingle'
    $form.MaximizeBox = $false
    $form.BackColor = [System.Drawing.Color]::FromArgb(241, 245, 249)
    $form.Font = New-Object System.Drawing.Font('Microsoft YaHei UI', 9)
    $ico = New-AppIcon
    if ($ico) { try { $form.Icon = New-Object System.Drawing.Icon $ico } catch { } }

    # 顶部标题栏
    $header = New-Object System.Windows.Forms.Panel
    $header.Size = New-Object System.Drawing.Size 490, 66
    $header.Location = New-Object System.Drawing.Point 0, 0
    $header.BackColor = [System.Drawing.Color]::FromArgb(15, 118, 110)
    $form.Controls.Add($header)

    $lblTitle = New-Object System.Windows.Forms.Label
    $lblTitle.Text = 'EduPACS 教学病例库系统'
    $lblTitle.Font = New-Object System.Drawing.Font('Microsoft YaHei UI', 14, [System.Drawing.FontStyle]::Bold)
    $lblTitle.ForeColor = [System.Drawing.Color]::White
    $lblTitle.AutoSize = $true
    $lblTitle.Location = New-Object System.Drawing.Point 20, 10
    $header.Controls.Add($lblTitle)

    $lblSub = New-Object System.Windows.Forms.Label
    $lblSub.Text = 'Orthanc + OHIF 一体化医学影像教学平台'
    $lblSub.ForeColor = [System.Drawing.Color]::FromArgb(204, 251, 241)
    $lblSub.AutoSize = $true
    $lblSub.Location = New-Object System.Drawing.Point 22, 40
    $header.Controls.Add($lblSub)

    # 服务状态卡片
    $card = New-Object System.Windows.Forms.Panel
    $card.Size = New-Object System.Drawing.Size 450, 108
    $card.Location = New-Object System.Drawing.Point 20, 84
    $card.BackColor = [System.Drawing.Color]::White
    $form.Controls.Add($card)

    $lblO1 = New-Object System.Windows.Forms.Label
    $lblO1.Text = '影像服务 Orthanc'
    $lblO1.Font = New-Object System.Drawing.Font('Microsoft YaHei UI', 10, [System.Drawing.FontStyle]::Bold)
    $lblO1.AutoSize = $true
    $lblO1.Location = New-Object System.Drawing.Point 16, 14
    $card.Controls.Add($lblO1)

    $lblO2 = New-Object System.Windows.Forms.Label
    $lblO2.Text = 'DICOM 4242 · HTTP 8042 · OHIF 阅片'
    $lblO2.ForeColor = [System.Drawing.Color]::FromArgb(100, 116, 139)
    $lblO2.AutoSize = $true
    $lblO2.Location = New-Object System.Drawing.Point 18, 38
    $card.Controls.Add($lblO2)

    $lblA1 = New-Object System.Windows.Forms.Label
    $lblA1.Text = '教学病例库系统'
    $lblA1.Font = New-Object System.Drawing.Font('Microsoft YaHei UI', 10, [System.Drawing.FontStyle]::Bold)
    $lblA1.AutoSize = $true
    $lblA1.Location = New-Object System.Drawing.Point 16, 64
    $card.Controls.Add($lblA1)

    $lblA2 = New-Object System.Windows.Forms.Label
    $lblA2.Text = 'HTTP 8000 · 账号/病例/问答/统计'
    $lblA2.ForeColor = [System.Drawing.Color]::FromArgb(100, 116, 139)
    $lblA2.AutoSize = $true
    $lblA2.Location = New-Object System.Drawing.Point 18, 87
    $card.Controls.Add($lblA2)

    $pillO = New-Object System.Windows.Forms.Label
    $pillO.Size = New-Object System.Drawing.Size 90, 28
    $pillO.Location = New-Object System.Drawing.Point 344, 14
    $pillO.TextAlign = 'MiddleCenter'
    $pillO.Font = New-Object System.Drawing.Font('Microsoft YaHei UI', 9, [System.Drawing.FontStyle]::Bold)
    $card.Controls.Add($pillO)

    $pillA = New-Object System.Windows.Forms.Label
    $pillA.Size = New-Object System.Drawing.Size 90, 28
    $pillA.Location = New-Object System.Drawing.Point 344, 64
    $pillA.TextAlign = 'MiddleCenter'
    $pillA.Font = New-Object System.Drawing.Font('Microsoft YaHei UI', 9, [System.Drawing.FontStyle]::Bold)
    $card.Controls.Add($pillA)

    function Set-Pill($pill, [bool]$on, [bool]$checking) {
        if ($checking) {
            $pill.Text = '检测中…'
            $pill.BackColor = [System.Drawing.Color]::FromArgb(241, 245, 249)
            $pill.ForeColor = [System.Drawing.Color]::FromArgb(100, 116, 139)
        } elseif ($on) {
            $pill.Text = '● 运行中'
            $pill.BackColor = [System.Drawing.Color]::FromArgb(220, 252, 231)
            $pill.ForeColor = [System.Drawing.Color]::FromArgb(22, 101, 52)
        } else {
            $pill.Text = '● 已停止'
            $pill.BackColor = [System.Drawing.Color]::FromArgb(254, 226, 226)
            $pill.ForeColor = [System.Drawing.Color]::FromArgb(185, 28, 28)
        }
    }

    # 按钮
    $btnStart = New-Object System.Windows.Forms.Button
    $btnStart.Text = '一键启动'
    $btnStart.Size = New-Object System.Drawing.Size 150, 46
    $btnStart.Location = New-Object System.Drawing.Point 20, 208
    $btnStart.BackColor = [System.Drawing.Color]::FromArgb(15, 118, 110)
    $btnStart.ForeColor = [System.Drawing.Color]::White
    $btnStart.Font = New-Object System.Drawing.Font('Microsoft YaHei UI', 11, [System.Drawing.FontStyle]::Bold)
    $btnStart.FlatStyle = 'Flat'
    $btnStart.FlatAppearance.BorderSize = 0
    $btnStart.Cursor = 'Hand'
    $form.Controls.Add($btnStart)

    $btnStop = New-Object System.Windows.Forms.Button
    $btnStop.Text = '一键停止'
    $btnStop.Size = New-Object System.Drawing.Size 150, 46
    $btnStop.Location = New-Object System.Drawing.Point 182, 208
    $btnStop.BackColor = [System.Drawing.Color]::White
    $btnStop.ForeColor = [System.Drawing.Color]::FromArgb(185, 28, 28)
    $btnStop.Font = New-Object System.Drawing.Font('Microsoft YaHei UI', 11, [System.Drawing.FontStyle]::Bold)
    $btnStop.FlatStyle = 'Flat'
    $btnStop.FlatAppearance.BorderColor = [System.Drawing.Color]::FromArgb(220, 38, 38)
    $btnStop.FlatAppearance.BorderSize = 1
    $btnStop.Cursor = 'Hand'
    $form.Controls.Add($btnStop)

    $btnOpen = New-Object System.Windows.Forms.Button
    $btnOpen.Text = '打开教学系统'
    $btnOpen.Size = New-Object System.Drawing.Size 140, 38
    $btnOpen.Location = New-Object System.Drawing.Point 20, 272
    $btnOpen.BackColor = [System.Drawing.Color]::White
    $btnOpen.FlatStyle = 'Flat'
    $btnOpen.FlatAppearance.BorderColor = [System.Drawing.Color]::FromArgb(203, 213, 225)
    $btnOpen.Cursor = 'Hand'
    $form.Controls.Add($btnOpen)

    $btnOrthanc = New-Object System.Windows.Forms.Button
    $btnOrthanc.Text = '打开 Orthanc 后台'
    $btnOrthanc.Size = New-Object System.Drawing.Size 160, 38
    $btnOrthanc.Location = New-Object System.Drawing.Point 172, 272
    $btnOrthanc.BackColor = [System.Drawing.Color]::White
    $btnOrthanc.FlatStyle = 'Flat'
    $btnOrthanc.FlatAppearance.BorderColor = [System.Drawing.Color]::FromArgb(203, 213, 225)
    $btnOrthanc.Cursor = 'Hand'
    $form.Controls.Add($btnOrthanc)

    # 信息区
    $lanIp = Get-LanIp
    $lblAddr = New-Object System.Windows.Forms.Label
    $lblAddr.Text = ("本机访问：http://127.0.0.1:8000`n局域网访问：http://" + $lanIp + ":8000（学生/教师电脑）")
    $lblAddr.Location = New-Object System.Drawing.Point 22, 326
    $lblAddr.AutoSize = $true
    $lblAddr.ForeColor = [System.Drawing.Color]::FromArgb(51, 65, 85)
    $form.Controls.Add($lblAddr)

    $lblTip = New-Object System.Windows.Forms.Label
    $lblTip.Text = "默认管理员 admin / admin123（上线后请修改）`n每日备份：orthanc\storage 与 app\data`nOrthanc 后台（8042）已启用认证，账号密码见 orthanc\orthanc.json"
    $lblTip.Location = New-Object System.Drawing.Point 22, 360
    $lblTip.AutoSize = $true
    $lblTip.ForeColor = [System.Drawing.Color]::FromArgb(148, 163, 184)
    $form.Controls.Add($lblTip)

    $statusBar = New-Object System.Windows.Forms.Label
    $statusBar.Text = '就绪'
    $statusBar.Location = New-Object System.Drawing.Point 22, 434
    $statusBar.AutoSize = $true
    $statusBar.ForeColor = [System.Drawing.Color]::FromArgb(15, 118, 110)
    $form.Controls.Add($statusBar)

    # 后台动作状态（GUI 只发号施令，实际启停交给子进程 -TestAction）
    $script:pending = ''          # '' / 'start' / 'stop'
    $script:pendingDeadline = $null
    $script:autoOpen = $false
    $script:lastKey = ''

    function Invoke-Cli([string]$action) {
        Start-Process -FilePath 'powershell.exe' `
            -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-WindowStyle', 'Hidden', '-File', ('"{0}"' -f $launcherPs1), '-TestAction', $action)
    }

    # 启动
    $btnStart.Add_Click({
        try {
            Write-ClickLog 'btn: 一键启动'
            $st = Get-ServiceState
            if ($st.App -and $st.Orthanc) {
                $statusBar.Text = '服务已在运行，正在打开浏览器…'
                [void](Open-Url 'http://127.0.0.1:8000')
                return
            }
            $btnStart.Enabled = $false
            $btnStop.Enabled = $false
            $statusBar.Text = '正在后台启动，请稍候…'
            Invoke-Cli 'Start'
            $script:pending = 'start'
            $script:pendingDeadline = (Get-Date).AddSeconds(60)
            $script:autoOpen = $true
        } catch {
            Write-ErrLog ("btnStart: " + $_.Exception.Message)
            $statusBar.Text = '启动失败：' + $_.Exception.Message
            $btnStart.Enabled = $true
            $btnStop.Enabled = $true
        }
    })

    # 停止
    $btnStop.Add_Click({
        try {
            Write-ClickLog 'btn: 一键停止'
            $r = [System.Windows.Forms.MessageBox]::Show(
                "确认停止 Orthanc 与教学系统服务？`n（学生/教师将暂时无法访问）",
                '确认停止', 'YesNo', 'Question')
            if ($r -ne 'Yes') { return }
            $btnStart.Enabled = $false
            $btnStop.Enabled = $false
            $statusBar.Text = '正在停止…'
            Invoke-Cli 'Stop'
            $script:pending = 'stop'
            $script:pendingDeadline = (Get-Date).AddSeconds(20)
        } catch {
            Write-ErrLog ("btnStop: " + $_.Exception.Message)
            $statusBar.Text = '停止失败：' + $_.Exception.Message
            $btnStart.Enabled = $true
            $btnStop.Enabled = $true
        }
    })

    $btnOpen.Add_Click({
        try {
            Write-ClickLog 'btn: 打开教学系统'
            $ok = Open-Url 'http://127.0.0.1:8000'
            if ($ok) { $statusBar.Text = '已在浏览器打开教学系统' } else { $statusBar.Text = '未找到可用浏览器，请手动打开 http://127.0.0.1:8000' }
        } catch {
            Write-ErrLog ("btnOpen: " + $_.Exception.Message)
            $statusBar.Text = '打开失败：' + $_.Exception.Message
        }
    })

    $btnOrthanc.Add_Click({
        try {
            Write-ClickLog 'btn: 打开Orthanc后台'
            $acct = ''
            try {
                $cfgJson = Get-Content (Join-Path $root 'orthanc\orthanc.json') -Raw -Encoding UTF8 | ConvertFrom-Json
                $ru = $cfgJson.RegisteredUsers
                if ($ru) { $acct = ($ru.PSObject.Properties | Select-Object -First 1).Name }
            } catch { }
            $ok = Open-Url 'http://127.0.0.1:8042/'
            if ($ok) {
                if ($acct) { $statusBar.Text = ("已在浏览器打开 Orthanc 后台，认证账号 " + $acct + "（密码见 orthanc\orthanc.json）") }
                else { $statusBar.Text = '已在浏览器打开 Orthanc 后台（如弹出认证框，账号密码见 orthanc\orthanc.json）' }
            } else { $statusBar.Text = '未找到可用浏览器，请手动打开 http://127.0.0.1:8042（需服务账号认证）' }
        } catch {
            Write-ErrLog ("btnOrthanc: " + $_.Exception.Message)
            $statusBar.Text = '打开失败：' + $_.Exception.Message
        }
    })

    # 定时刷新状态 + 收尾
    function Update-Status {
        try {
            $st = Get-ServiceState
            Set-Pill $pillO $st.Orthanc $false
            Set-Pill $pillA $st.App $false
            $key = ("O=" + $st.Orthanc + " A=" + $st.App)
            if ($key -ne $script:lastKey) {
                Write-ClickLog ("state: " + $key)
                $script:lastKey = $key
            }
            if ($script:pending -eq 'start') {
                if ($st.App -and $st.Orthanc) {
                    $script:pending = ''
                    $btnStart.Enabled = $true
                    $btnStop.Enabled = $true
                    $statusBar.Text = '启动完成，正在打开浏览器…'
                    if ($script:autoOpen) { [void](Open-Url 'http://127.0.0.1:8000'); $script:autoOpen = $false }
                } elseif ((Get-Date) -gt $script:pendingDeadline) {
                    $script:pending = ''
                    $btnStart.Enabled = $true
                    $btnStop.Enabled = $true
                    $statusBar.Text = '启动未完全成功，请查看 .temp\launcher_error.log 或端口占用'
                }
            } elseif ($script:pending -eq 'stop') {
                if (-not ($st.App -or $st.Orthanc)) {
                    $script:pending = ''
                    $btnStart.Enabled = $true
                    $btnStop.Enabled = $true
                    $statusBar.Text = '服务已停止'
                } elseif ((Get-Date) -gt $script:pendingDeadline) {
                    $script:pending = ''
                    $btnStart.Enabled = $true
                    $btnStop.Enabled = $true
                    $statusBar.Text = '停止未完全成功，请查看 .temp\launcher_error.log'
                }
            }
        } catch {
            try { Write-ErrLog ("timer: " + $_.Exception.Message) } catch { }
        }
    }

    # 定时器
    $timer = New-Object System.Windows.Forms.Timer
    $timer.Interval = 3000
    $timer.Add_Tick({ Update-Status })
    $timer.Start()

    $form.Add_Shown({
        Write-ClickLog 'GUI: shown'
        Set-Pill $pillO $false $true
        Set-Pill $pillA $false $true
        Update-Status
    })

    # 窗体关闭留痕：正常关闭会有此记录；若进程消失但无此记录，说明是被强杀/崩溃
    $form.Add_FormClosed({ Write-ClickLog 'GUI: closed' })

    [void]$form.ShowDialog()
    $timer.Stop()
    $form.Dispose()
    Write-ClickLog 'GUI: exit'
} catch {
    Write-ErrLog ("GUI: " + $_.Exception.Message)
    try {
        [System.Windows.Forms.MessageBox]::Show(
            ("启动器运行出错：`n" + $_.Exception.Message + "`n`n详情见 .temp\launcher_error.log"),
            'EduPACS 启动器', 'OK', 'Error') | Out-Null
    } catch { }
}