@echo off
setlocal
REM -- The repo is wherever this script lives, so a clone in any folder
REM -- on any machine works without editing this file.
cd /d "%~dp0"
title Atomic - pull from development

echo ============================================
echo   Atomic  -  pull from development
echo ============================================
echo.

git fetch origin development
if errorlevel 1 goto fetchfail

set AHEAD=
set BEHIND=
for /f "tokens=1,2" %%a in ('git rev-list --left-right --count HEAD...origin/development') do (
    set AHEAD=%%a
    set BEHIND=%%b
)

echo   commits only you have : %AHEAD%
echo   commits you are behind: %BEHIND%
echo.

if "%AHEAD%"=="0" if "%BEHIND%"=="0" (
    echo Already up to date - nothing to pull.
    goto done
)

REM -- Save uncommitted work before touching history. The reset below
REM -- would otherwise delete it permanently.
set DIRTY=
git diff --quiet
if errorlevel 1 set DIRTY=1
git diff --cached --quiet
if errorlevel 1 set DIRTY=1

if defined DIRTY (
    echo You have uncommitted changes - stashing them first.
    git stash push -m "pull.bat auto-stash"
    echo.
)

if "%AHEAD%"=="0" (
    REM -- Never a plain `git pull`: on a branch that has been force
    REM -- pushed it quietly makes a merge commit that resurrects the
    REM -- commits the remote just dropped.
    echo Fast-forwarding to development...
    git merge --ff-only origin/development
    if errorlevel 1 goto mergefail
) else (
    echo.
    echo   NOTE: development was rewritten on GitHub - some commits that
    echo   were there before are gone. Taking the remote's history exactly.
    echo.
    git reset --hard origin/development
    if errorlevel 1 goto mergefail
)

if defined DIRTY (
    echo.
    echo Putting your uncommitted changes back...
    git stash pop
    if errorlevel 1 (
        echo.
        echo   Your changes could not be re-applied cleanly and are still
        echo   saved. Recover them with:  git stash list  /  git stash pop
    )
)

:done
echo.
echo Now on:
git log --oneline -1
echo.
echo Done. Run build next.
goto end

:fetchfail
echo.
echo   FETCH FAILED - no connection to GitHub, or the repo is busy.
echo   Nothing was changed.
goto end

:mergefail
echo.
echo   UPDATE FAILED - nothing further was changed.
if defined DIRTY echo   Your changes are safe in the stash: git stash list

:end
echo.
pause
