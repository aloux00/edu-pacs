@echo off
rem EduPACS Teaching Case Library - Launcher (ASCII only to avoid codepage issues)
rem Prefer the native C launcher (instant startup); fall back to the PowerShell one if missing.
cd /d "%~dp0"
for %%f in ("%~dp0EduPACS*.exe") do (
    start "" "%%~ff"
    exit /b
)
start "" powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "%~dp0scripts\launcher.ps1"
