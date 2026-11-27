@echo off
chcp 65001 >nul
title EduPACS - 服务状态
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\service-status.ps1" -Action Show
echo.
pause
