@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo ============================================================
echo  EduPACS + Moodle + OHIF + LTI 1.3 + xAPI  一键集成栈
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

set "PY=python"
where python >nul 2>nul
if errorlevel 1 (
  where py >nul 2>nul
  if errorlevel 1 (
    echo [错误] 未找到 Python 3，请先安装或使用 py -3。
    pause
    exit /b 1
  )
  set "PY=py -3"
)

echo [1/5] 生成配置与随机凭据 ...
%PY% scripts\configure.py --init
if errorlevel 1 goto :fail

echo [2/5] 拉取镜像并构建 EduPACS 应用镜像（首次较慢）...
docker compose -f docker-compose.moodle.yml pull mariadb moodle orthanc lrs
if errorlevel 1 goto :fail
docker compose -f docker-compose.moodle.yml up -d --build
if errorlevel 1 goto :fail

echo [3/5] 等待 Moodle 安装并在其中注册 LTI 1.3 工具、创建课程与活动 ...
docker compose -f docker-compose.moodle.yml --profile tools run --rm bootstrap
if errorlevel 1 goto :fail

echo [4/5] 回写 Deployment ID 并重启 EduPACS 后端 ...
%PY% scripts\configure.py --finalize
if errorlevel 1 goto :fail

echo [5/5] 验证服务端点 ...
%PY% scripts\configure.py --status

echo.
echo 完成。浏览器打开上面输出的 Moodle 地址，进入课程点击「EduPACS 影像阅片」。
echo.
pause
exit /b 0

:fail
echo.
echo [失败] 安装中断，请查看上方错误日志。
echo   查看容器状态：docker compose -f docker-compose.moodle.yml ps
echo   查看 Moodle 日志：docker compose -f docker-compose.moodle.yml logs -f moodle
echo   查看引导日志：重新运行 bootstrap 步骤
pause
exit /b 1
