"""Full app removal, for Settings > Uninstall - distinct from Settings >
Clear Data (which only wipes one content category at a time and leaves
the app itself alone). His ask, 9 November 2026: "make it delete the
whole dir", then "make it delete the app entirely from windows!". So:
the Windows startup entry, %APPDATA%\\Atomic, the program, its shortcuts
and taskbar pin, and what it keeps in %TEMP%.

**None of that can be done from inside the app, so a detached script does
it once the app has gone.** It used to `rmtree` the folder here, with
`ignore_errors`, while the app was still running. Measured on a throwaway
APPDATA with the build open: 189 files before, 74 still there after the
rmtree, because Edge holds its profile open and the log and the crash
record are open here. Windows refuses to delete an open file and
ignore_errors says nothing. Waiting matters as much as retrying: what the
app writes on the way out lands after any delete it could do itself.

The removal of the program had a different fault: it handed cmd one
string holding quoted paths, Popen escaped those quotes as \\", and cmd
passed the backslashes on to `rd`/`del`, which answered "The filename,
directory name, or volume label syntax is incorrect" for every path, with
or without a space. So Uninstall had never removed the program either.

Left alone on purpose: ~/Downloads/Atomic, which is what he chose to
download rather than anything the app needs, and the Windows Firewall
rule, which takes administrator rights to remove and the app has none.
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

from . import child_process, logs, startup, storage

# Every argument is one folder or file to remove. They come in as
# arguments, and the image name and the two process ids as environment
# variables, so the file itself is plain ASCII and a non-English user name
# cannot be mangled by the batch code page (the same reasoning as updater's
# swap scripts). One pass over all of them, then again for whatever is
# left: Edge's helpers outlive the app by a moment, and one locked file
# must not hold up the rest. %TEMP%'s are named by wildcard here rather
# than listed, so a long list cannot overflow the command line.
_REMOVE_SCRIPT = r"""@echo off
cd /d "%TEMP%"
set "SYS=%SystemRoot%\System32"
set /a WAITED=0
:wait
set "ALIVE="
for %%P in (%ATOMIC_PID_1% %ATOMIC_PID_2%) do (
    "%SYS%\tasklist.exe" /FI "PID eq %%P" /FI "IMAGENAME eq %ATOMIC_IMAGE%" 2>nul | "%SYS%\find.exe" /i "%ATOMIC_IMAGE%" >nul && set "ALIVE=1"
)
if not defined ALIVE goto remove
set /a WAITED+=1
if %WAITED% geq 30 goto remove
"%SYS%\PING.EXE" -n 2 127.0.0.1 >nul
goto wait
:remove
set /a TRIES=0
:again
set "LEFT="
for %%A in (%*) do (
    if exist "%%~A\" (rd /s /q "%%~A" >nul 2>&1) else if exist "%%~A" del /f /q "%%~A" >nul 2>&1
    if exist "%%~A" set "LEFT=1"
)
if not defined LEFT goto temp
set /a TRIES+=1
if %TRIES% geq 30 goto temp
"%SYS%\PING.EXE" -n 2 127.0.0.1 >nul
goto again
:temp
if exist "%TEMP%\atomic-stream-cache\" rd /s /q "%TEMP%\atomic-stream-cache" >nul 2>&1
for /d %%D in ("%TEMP%\atomic-subs-*") do rd /s /q "%%D" >nul 2>&1
del /f /q "%TEMP%\Atomic-update-*.exe" >nul 2>&1
del /f /q "%TEMP%\atomic-update-*.bat" >nul 2>&1
(goto) 2>nul & del "%~f0"
"""


def _shortcuts():
    """Where an Atomic.lnk can be: the Desktop and Start menu ones
    packaging/bridge creates, and the taskbar pin he may have made. The
    exact name only - a glob for *Atomic* would take "Atomic Heart"."""
    import ctypes
    from ctypes import wintypes
    found = []
    # CSIDL_DESKTOPDIRECTORY 0x10 (follows a redirected Desktop), and the
    # per-user Start menu Programs folder, CSIDL_PROGRAMS 0x02.
    for csidl in (0x10, 0x02):
        buf = ctypes.create_unicode_buffer(wintypes.MAX_PATH)
        try:
            if ctypes.windll.shell32.SHGetFolderPathW(None, csidl, None, 0, buf) == 0:
                found.append(Path(buf.value) / "Atomic.lnk")
        except Exception:
            pass
    appdata = os.environ.get("APPDATA")
    if appdata:
        found.append(Path(appdata) / "Microsoft" / "Internet Explorer" / "Quick Launch"
                     / "User Pinned" / "TaskBar" / "Atomic.lnk")
    return found


def _program_paths(exe_path):
    """The program's own files, for the copy that is running."""
    if startup.is_installed_copy():
        # **The installed folder build goes whole** (21 September 2026):
        # its folder in %LOCALAPPDATA%\Programs, what an interrupted update
        # left beside it, and the shortcuts. Only the installed copy
        # removes a folder or a shortcut - a test build run from anywhere
        # else takes only its own files, not the install beside it.
        folder = exe_path.parent
        parent = folder.parent
        return [folder, parent / "Atomic.old", *parent.glob("Atomic.new-*"),
                *(p for p in _shortcuts() if p.exists())]
    paths = []
    if exe_path.suffix.lower() == ".exe" and exe_path.exists():
        paths.append(exe_path)
        # A folder build: the exe is nothing without this beside it.
        internal = exe_path.parent / "_internal"
        if internal.is_dir():
            paths.append(internal)
    return paths


def _command_line(script, paths) -> str:
    """One string, not a list: cmd reads quotes its own way, and a list is
    what put \\" in front of every path (top of file). /s makes cmd take
    everything between the outer pair literally, script path included, so
    a user name with a space in it is safe too."""
    quoted = " ".join(f'"{arg}"' for arg in [script, *paths])
    return f'cmd /d /s /c "{quoted}"'


def run():
    """Deregister the Windows-startup entry when it is this copy's (so a
    stale one doesn't try to launch a file that's about to stop existing;
    a test copy leaves the installed app's alone) and hand everything
    else to the detached script. Does *not* quit the app - the caller does
    that right after, and the script waits for it."""
    # **A source run removes nothing.** storage.DATA_DIR there is his real
    # %APPDATA%\Atomic once web/backend has been imported (testing.md), and
    # the Windows startup entry is the installed app's. A press from a
    # development run must not cost him either.
    if not getattr(sys, "frozen", False):
        logs.info("uninstall: running from source, nothing was removed")
        return

    if startup.is_installed_copy() or startup.names_this_copy():
        startup.set_enabled(False)

    exe_path = Path(sys.executable)
    paths = [Path(storage.DATA_DIR).resolve(), *_program_paths(exe_path)]

    script = Path(tempfile.gettempdir()) / f"atomic-uninstall-{os.getpid()}.bat"
    script.write_text(_REMOVE_SCRIPT, encoding="ascii")
    # The two ids are this process and the one above it, for the same
    # reason as updater.apply_update; the script checks the image name
    # too, so a parent that is not this app (Explorer) is never waited on.
    env = child_process.clean_env()
    env.update(ATOMIC_IMAGE=exe_path.name, ATOMIC_PID_1=str(os.getpid()),
               ATOMIC_PID_2=str(os.getppid()))
    subprocess.Popen(
        _command_line(script, paths),
        creationflags=child_process.flags(detached=True), close_fds=True,
        env=env,
    )
    logs.info(f"uninstall: removal of {len(paths)} paths handed to {script.name}")
