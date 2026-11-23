@echo off
rem ============================================================
rem  EduPACS launcher compile script
rem  Must use -mcpu=baseline: newer zig emits AVX-512 instructions
rem  that crash with 0xc000001d on CPUs without AVX-512 (e.g. i5-3340M).
rem  baseline = x86-64 SSE2, runs on every amd64 machine.
rem ============================================================
setlocal
cd /d "%~dp0.."

where zig >nul 2>nul
if errorlevel 1 (
  echo [ERROR] zig not found in PATH. Install with: winget install --id Zig.Zig --scope user
  exit /b 1
)

echo [1/2] compiling icon resource...
zig rc scripts/launcher.rc scripts/launcher.res
if errorlevel 1 ( echo [ERROR] zig rc failed & exit /b 1 )

echo [2/2] compiling launcher (baseline x86-64)...
zig cc scripts/launcher_native.c scripts/launcher.res -O2 -s -static -mcpu=baseline -Wl,--subsystem,windows -o EduPACS启动器.exe -lwininet -liphlpapi -lshell32 -lgdi32 -luser32
if errorlevel 1 ( echo [ERROR] zig cc failed & exit /b 1 )

echo [OK] EduPACS启动器.exe rebuilt with baseline instructions.
echo.
echo Verify no AVX-512 (EVEX 62) in hot path:
echo   zig objdump -d EduPACS启动器.exe ^| findstr /C:"zmm" /C:"ev"
endlocal
