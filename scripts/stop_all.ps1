# EduPACS 教学病例库 停止脚本（安全版：只停本项目进程，不误杀同机其它服务）
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$rootNorm = $root.Replace('/', '\')

$killed = @()
# 1) 命令行能证明属于本项目：Orthanc 以本包 orthanc.json 启动；应用为本包 .venv python 或 app.backend.main
$mine = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
    $_.Name -in @('Orthanc.exe', 'python.exe', 'pythonw.exe') -and $_.CommandLine -and
    (($_.CommandLine.Replace('/', '\') -like ("*" + $rootNorm + "*")) -or
     ($_.CommandLine -like '*app.backend.main*'))
}
foreach ($p in $mine) {
    Write-Host ("停止 {0} (PID {1})" -f $p.Name, $p.ProcessId)
    Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
    $killed += ("{0}(PID{1})" -f $p.Name, $p.ProcessId)
}

# 2) 端口兜底：仅当占用者命令行属于本项目时才停；他人服务只报告、不动手
foreach ($port in 8042, 8000) {
    $conn = Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($conn) {
        $owner = Get-CimInstance Win32_Process -Filter ("ProcessId = " + $conn.OwningProcess) -ErrorAction SilentlyContinue
        $cmd = [string]$owner.CommandLine
        if ($owner -and $cmd -and ($cmd.Replace('/', '\') -like ("*" + $rootNorm + "*") -or $cmd -like '*app.backend.main*')) {
            Stop-Process -Id $conn.OwningProcess -Force -ErrorAction SilentlyContinue
            $killed += ("{0}(端口{1})" -f $owner.Name, $port)
        } elseif ($owner) {
            Write-Host ("端口 {0} 被 {1} (PID {2}) 占用，但非本项目进程，已跳过" -f $port, $owner.Name, $owner.ProcessId)
        }
    }
}

if ($killed.Count) { Write-Host ("已停止：" + ($killed -join ', ')) } else { Write-Host "未发现本项目相关进程" }
