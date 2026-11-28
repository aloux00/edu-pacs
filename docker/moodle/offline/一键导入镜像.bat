@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0edupacs\docker\moodle\offline"

echo ============================================================
echo  EduPACS + Moodle 离线镜像导入（约 6 GB，需数分钟）
echo ============================================================

where docker >nul 2>nul
if errorlevel 1 (
  echo [错误] 未找到 docker，请先安装 Docker Desktop 并切换到 Linux 容器。
  pause
  exit /b 1
)
docker version >nul 2>&1
if errorlevel 1 (
  echo [错误] Docker 引擎未启动，请先启动 Docker Desktop 再运行本脚本。
  pause
  exit /b 1
)

if not exist images.tar (
  echo [错误] 未找到 images.tar，请确认离线包完整。
  pause
  exit /b 1
)

echo [1/2] 校验镜像包完整性 ...
certutil -hashfile images.tar SHA256 > "%TEMP%\edupacs_sha.txt" 2>nul
findstr /v ":" "%TEMP%\edupacs_sha.txt" | findstr /r "^[0-9a-fA-F]" > "%TEMP%\edupacs_sha_real.txt"
set /p ACTUAL=<"%TEMP%\edupacs_sha_real.txt"
set /p EXPECTED=<images.tar.sha256
echo   实际: %ACTUAL%
echo   期望: %EXPECTED%
echo %EXPECTED% | findstr /i "%ACTUAL%" >nul
if errorlevel 1 (
  echo [警告] SHA-256 不匹配！文件可能损坏或不完整。
  echo   如确认文件无误（例如重新生成过清单），可选择继续。
  choice /C YN /M "是否继续导入"
  if errorlevel 2 (
    echo 已取消。
    pause
    exit /b 1
  )
) else (
  echo [OK] SHA-256 校验通过。
)

echo [2/2] 导入镜像 ...
docker load -i images.tar
if errorlevel 1 (
  echo [失败] 镜像导入出错，请查看上方日志。
  pause
  exit /b 1
)

echo.
echo 完成！已导入 5 个镜像：
docker images --format "  {{.Repository}}:{{.Tag}}" | findstr /r "mariadb bitnamilegacy orthancteam python edupacs"
echo.
echo 下一步：双击包根目录的「启动Moodle集成栈.bat」启动教学平台。
echo 提示：首次启动前请按 README 编辑 .env 中的两个 PUBLIC_HOST。
pause
