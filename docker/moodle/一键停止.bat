@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
echo 停止 EduPACS + Moodle 集成栈（数据卷保留，不会丢失病例与课程）...
docker compose -f docker-compose.moodle.yml down
echo 已停止。
pause
