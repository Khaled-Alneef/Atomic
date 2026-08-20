@echo off
REM Run the first-run simulation without needing Python on PATH.
REM
REM Double-click this, or run it from any shell. It exists because
REM neither `py` nor `python` resolves on this machine (the launcher was
REM never added to PATH, and `python` hits the Store alias), so the
REM documented `py -3.13 tools\first_run_sim.py` could not start at all.
REM
REM 3.13 specifically, not "whatever is newest": libtorrent publishes no
REM wheel past it, and the app refuses to build or stream without one.
setlocal
set "SIM=%~dp0first_run_sim.py"

set "PY="
if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if not defined PY if exist "%ProgramFiles%\Python313\python.exe" set "PY=%ProgramFiles%\Python313\python.exe"
if not defined PY if exist "C:\Python313\python.exe" set "PY=C:\Python313\python.exe"
if not defined PY where py >nul 2>&1 && set "PY=py -3.13"

if not defined PY (
    echo Could not find Python 3.13.
    echo Looked in %%LOCALAPPDATA%%\Programs\Python\Python313, Program Files and C:\Python313.
    pause
    exit /b 1
)

echo Using %PY%
"%PY%" -u "%SIM%" %*
echo.
pause
