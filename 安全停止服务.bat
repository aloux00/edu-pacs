@echo off
rem EduPACS safe stop wrapper
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\safe_stop.ps1"
