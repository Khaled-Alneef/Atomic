@echo off
setlocal
REM -- The repo is wherever this script lives, so a clone in any folder
REM -- on any machine works without editing this file.
cd /d "%~dp0"
title Atomic - build

echo ============================================
echo   Atomic  -  build Atomic.exe
echo ============================================
echo.

REM -- Windows will not let the build replace a running binary, and a
REM -- half-replaced exe is worse than none.
echo Closing any running Atomic...
taskkill /IM Atomic.exe /F >nul 2>&1

echo Building - this takes about a minute.
echo.

REM -- 3.13 on purpose: there is no libtorrent wheel for the machine's
REM -- default Python, and a build made under that one produces an exe
REM -- where every torrent silently looks broken. packaging\build.py
REM -- re-execs itself into 3.13 anyway, but asking for it here means a
REM -- machine without 3.13 fails loudly instead of shipping that exe.
py -3.13 packaging\build.py
if errorlevel 1 goto fail

echo.
echo ============================================
for %%F in (Atomic.exe) do echo   Atomic.exe   %%~zF bytes   built %%~tF
echo ============================================
echo.
echo   The time above must be from just now. PyInstaller caches hard,
echo   and an old timestamp means it re-used the previous binary
echo   instead of building yours.
goto end

:fail
echo.
echo   BUILD FAILED - scroll up for the first line that says ERROR.
echo   Atomic.exe was left as it was.

:end
echo.
pause
