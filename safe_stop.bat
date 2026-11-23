@echo off
rem EduPACS safe stop wrapper - calls scripts\safe_stop.ps1 with exact project-process matching
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\safe_stop.ps1"
