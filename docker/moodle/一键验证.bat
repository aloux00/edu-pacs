@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set "PY=python"
where python >nul 2>nul
if errorlevel 1 set "PY=py -3"
%PY% scripts\configure.py --status
echo.
echo 说明：xAPI 语句可在 EduPACS 浏览病例/作答后，于 LRS 查看：
echo   docker compose -f docker-compose.moodle.yml exec -T lrs python -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8001/statements?limit=5').read().decode()[:2000])"
pause
