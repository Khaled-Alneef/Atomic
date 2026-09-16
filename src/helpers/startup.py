"""Toggle "launch on Windows startup", and start the app early enough at
sign-in that it is not the last thing on screen.

**Why this is a scheduled task now, not a registry Run entry (16 September
2026).** The owner's report: "when it start with start up it takes too too
too long to start". Measured against his own Windows event log and the
app's own log across ten sign-ins, one of them (16 September):

    17:07:28  boot
    17:07:48  logon
    17:07:49  explorer.exe
    17:08:13  Explorer *begins* enumerating the HKCU Run key  (+25s)
    17:08:27  Explorer reaches 'Atomic.exe --startup' (9th of 9, dead last)
    17:08:32  Atomic's window is serving
    17:08:33  Home drawn

So 45 seconds from sign-in to a usable window, of which Atomic's own code
is about five - 17:08:27 to 17:08:32. The other forty are Windows: Explorer
does not touch the Run key until the shell is settled (~25s here), then runs
each entry serially, and Atomic sits behind SecurityHealth, Realtek, Riot
Vanguard, Adobe, LGHUB, Google Drive (which alone took 5s) and ScreenRec.
Nothing an entry in that list can do makes it start sooner; its position and
the pre-roll are Explorer's, not ours.

A **logon-triggered scheduled task** does not wait behind any of that. Task
Scheduler fires it in parallel with the shell as soon as the interactive
logon completes (~17:07:48), which is where the ~35-40s comes from. The task
runs as the user, non-elevated (InteractiveToken + LeastPrivilege), so it
needs no admin rights to register - the same permission the Run key had.

The registry Run key stays as the **fallback**: if the Task Scheduler
service is disabled or refuses the registration, set_enabled(True) writes
the old Run entry instead, so "Launch on Windows startup" is never silently
off. reconcile() migrates an existing Run-key install to a task once, on the
next launch, and is a cheap no-op after that.
"""

import os
import subprocess
import sys
import tempfile
import winreg
from pathlib import Path
from xml.sax.saxutils import escape

_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_VALUE_NAME = "Atomic"
_LEGACY_VALUE_NAME = "PC App"  # this app's old name - cleaned up opportunistically

# The scheduled task's name (registered in the root folder, "\Atomic").
_TASK_NAME = "Atomic"

# CREATE_NO_WINDOW: schtasks.exe is a console program, and without this a
# black console box flashes on screen every time the setting is toggled or
# migrated. DETACHED would lose us the exit code; NO_WINDOW keeps it.
_NO_WINDOW = 0x08000000

# Passed by the registered startup command and by nothing else, so the
# app can tell "Windows started me at sign-in" from "the user opened me"
# - the two are otherwise identical from inside the process, and that
# difference is the whole basis of the fullscreen-on-startup setting
# (see app_settings.get_fullscreen_on_startup).
STARTUP_FLAG = "--startup"


def _launch_parts():
    """(command, arguments) for the startup launch, split the way a
    scheduled task's <Exec> wants them - the program in <Command>, the
    rest in <Arguments>. Frozen: the exe, then just the flag. From source:
    pythonw (no console window) if it is next to the interpreter, then the
    script path and the flag."""
    if getattr(sys, "frozen", False):
        return sys.executable, STARTUP_FLAG
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    interpreter = str(pythonw) if pythonw.exists() else sys.executable
    script = str(Path(__file__).resolve().parent.parent / "main.py")
    return interpreter, f'"{script}" {STARTUP_FLAG}'


def _launch_command() -> str:
    """The single quoted command line for the registry Run fallback."""
    command, arguments = _launch_parts()
    return f'"{command}" {arguments}'


def launched_on_startup(argv=None) -> bool:
    """Whether this process was started by the registered startup entry."""
    return STARTUP_FLAG in (sys.argv[1:] if argv is None else argv)


# ---- the scheduled task ------------------------------------------------

def _schtasks() -> str:
    root = os.environ.get("SystemRoot", r"C:\Windows")
    return os.path.join(root, "System32", "schtasks.exe")


def _run_schtasks(args):
    """Run schtasks with the given args; return (rc, stdout, stderr) or
    None if it could not be launched at all. Never raises."""
    try:
        proc = subprocess.run(
            [_schtasks(), *args],
            capture_output=True, text=True, creationflags=_NO_WINDOW)
        return proc.returncode, proc.stdout, proc.stderr
    except Exception:
        return None


def _current_user() -> str:
    """The current user as DOMAIN\\User (NameSamCompatible), for the task's
    principal and its logon trigger. Falls back to the environment names,
    which are what a task registered without an explicit user gets anyway."""
    try:
        import ctypes
        from ctypes import wintypes
        size = wintypes.ULONG(0)
        # NameSamCompatible = 2
        ctypes.windll.secur32.GetUserNameExW(2, None, ctypes.byref(size))
        buf = ctypes.create_unicode_buffer(size.value or 256)
        if ctypes.windll.secur32.GetUserNameExW(2, buf, ctypes.byref(size)):
            return buf.value
    except Exception:
        pass
    domain = os.environ.get("USERDOMAIN") or os.environ.get("COMPUTERNAME", "")
    user = os.environ.get("USERNAME", "")
    return f"{domain}\\{user}" if domain else user


def _task_xml() -> str:
    """The task definition. Everything here is load-bearing:

    * **The battery flags are false.** This is the whole reason not to
      accept a schtasks-generated default - the owner runs Atomic on a
      laptop, and DisallowStartIfOnBatteries defaults *true*, which would
      mean the app simply does not launch at sign-in whenever he is
      unplugged.
    * **ExecutionTimeLimit PT0S** - no limit. A desktop app is not a job
      the scheduler should ever decide has run too long and kill.
    * **InteractiveToken + LeastPrivilege** - runs as the signed-in user,
      non-elevated, in the interactive desktop; registrable without admin.
    * **Priority 6** so the app is not scheduled at background priority.
    """
    command, arguments = _launch_parts()
    user = _current_user()
    return (
        '<?xml version="1.0" encoding="UTF-16"?>\n'
        '<Task version="1.2" '
        'xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">\n'
        "  <RegistrationInfo>\n"
        "    <Description>Starts Atomic when you sign in to Windows."
        "</Description>\n"
        "  </RegistrationInfo>\n"
        "  <Triggers>\n"
        "    <LogonTrigger>\n"
        "      <Enabled>true</Enabled>\n"
        f"      <UserId>{escape(user)}</UserId>\n"
        "    </LogonTrigger>\n"
        "  </Triggers>\n"
        "  <Principals>\n"
        '    <Principal id="Author">\n'
        f"      <UserId>{escape(user)}</UserId>\n"
        "      <LogonType>InteractiveToken</LogonType>\n"
        "      <RunLevel>LeastPrivilege</RunLevel>\n"
        "    </Principal>\n"
        "  </Principals>\n"
        "  <Settings>\n"
        "    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>\n"
        "    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>\n"
        "    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>\n"
        "    <AllowHardTerminate>true</AllowHardTerminate>\n"
        "    <StartWhenAvailable>false</StartWhenAvailable>\n"
        "    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>\n"
        "    <IdleSettings>\n"
        "      <StopOnIdleEnd>false</StopOnIdleEnd>\n"
        "      <RestartOnIdle>false</RestartOnIdle>\n"
        "    </IdleSettings>\n"
        "    <AllowStartOnDemand>true</AllowStartOnDemand>\n"
        "    <Enabled>true</Enabled>\n"
        "    <Hidden>false</Hidden>\n"
        "    <RunOnlyIfIdle>false</RunOnlyIfIdle>\n"
        "    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>\n"
        "    <Priority>6</Priority>\n"
        "  </Settings>\n"
        '  <Actions Context="Author">\n'
        "    <Exec>\n"
        f"      <Command>{escape(command)}</Command>\n"
        f"      <Arguments>{escape(arguments)}</Arguments>\n"
        "    </Exec>\n"
        "  </Actions>\n"
        "</Task>\n"
    )


def _task_exists() -> bool:
    result = _run_schtasks(["/Query", "/TN", _TASK_NAME])
    return bool(result) and result[0] == 0


def _create_task() -> bool:
    """Register (or replace) the logon task. Returns True on success.

    schtasks /Create /XML reads a file, and it must be UTF-16 - the XML
    declares that encoding and schtasks rejects a mismatch."""
    xml = _task_xml()
    handle, path = tempfile.mkstemp(prefix="atomic-task-", suffix=".xml")
    try:
        with os.fdopen(handle, "w", encoding="utf-16") as fh:
            fh.write(xml)
        result = _run_schtasks(
            ["/Create", "/TN", _TASK_NAME, "/XML", path, "/F"])
        return bool(result) and result[0] == 0
    except Exception:
        return False
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def _delete_task() -> None:
    _run_schtasks(["/Delete", "/TN", _TASK_NAME, "/F"])


# ---- the registry Run key (legacy + fallback) --------------------------

def _run_key_value():
    """The current HKCU Run value for the app, or None."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0,
                            winreg.KEY_READ) as key:
            value, _kind = winreg.QueryValueEx(key, _VALUE_NAME)
            return value
    except OSError:
        return None


def _delete_run_key_values() -> None:
    """Remove both this app's Run entry and its old-name one. Used both
    when disabling startup and when a task takes the Run key's place."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0,
                            winreg.KEY_WRITE) as key:
            for name in (_VALUE_NAME, _LEGACY_VALUE_NAME):
                try:
                    winreg.DeleteValue(key, name)
                except OSError:
                    pass
    except OSError:
        pass


def _write_run_key_value() -> bool:
    """The fallback path: register the old Run entry. Returns True on
    success. Only reached when the scheduled task could not be created."""
    try:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0,
                                winreg.KEY_WRITE) as key:
            winreg.SetValueEx(key, _VALUE_NAME, 0, winreg.REG_SZ,
                              _launch_command())
        return True
    except OSError:
        return False


# ---- public API --------------------------------------------------------

def is_enabled() -> bool:
    """Whether Atomic is set to launch at sign-in, by either mechanism."""
    return _task_exists() or _run_key_value() is not None


def set_enabled(enabled: bool) -> None:
    """Turn "launch on Windows startup" on or off.

    On: register the logon task and remove any Run-key entry; if the task
    cannot be registered, fall back to the Run key so startup still works.
    Off: remove both."""
    if not enabled:
        _delete_task()
        _delete_run_key_values()
        return
    if _create_task():
        _delete_run_key_values()
        return
    # Task Scheduler refused (service disabled, policy, etc.) - keep the
    # old, slower Run entry rather than leaving startup silently off.
    _write_run_key_value()


def reconcile() -> None:
    """Migrate an existing Run-key install to a scheduled task, once.

    Called on launch. Cheap in the common case: if there is no Run-key
    entry there is nothing to migrate and this returns after one registry
    read. When there is one, it means either an install from before the
    task existed or a launch where task creation had failed - convert it,
    and only drop the Run entry if the task actually took."""
    if _run_key_value() is None:
        return
    if _create_task():
        _delete_run_key_values()


def allow_precise_timers() -> bool:
    """Opt this process out of Windows 11's timer-resolution throttling.

    **Measured 24 August 2026 on the owner's new PC (Windows 11 26200,
    240Hz panel): a 4ms Qt PreciseTimer fired every 13.9ms** - foreground
    window, timeBeginPeriod(1) active, in-tick work 0.26ms - so every
    QTimer-driven glide in the app (the wheel, the tweens, the fold) was
    quantised to ~72 positions a second on a panel refreshing 240 times
    a second. That is the owner's "in 2k it is not smooth", and no code
    that keeps riding OS timers can fix it while the OS coalesces them.

    PROCESS_POWER_THROTTLING_IGNORE_TIMER_RESOLUTION with a zero state
    mask tells the scheduler to always honour this process's requested
    resolution (the documented opt-out). Fails soft on any Windows that
    lacks it: the vblank ticker in widgets._Momentum does not depend on
    this succeeding - this is for everything else that still ticks."""
    import ctypes

    class _PowerThrottling(ctypes.Structure):
        _fields_ = [("Version", ctypes.c_uint),
                    ("ControlMask", ctypes.c_uint),
                    ("StateMask", ctypes.c_uint)]

    try:
        state = _PowerThrottling(1, 0x4, 0)     # IGNORE_TIMER_RESOLUTION off
        ok = ctypes.windll.kernel32.SetProcessInformation(
            ctypes.windll.kernel32.GetCurrentProcess(),
            4,                                   # ProcessPowerThrottling
            ctypes.byref(state), ctypes.sizeof(state))
        ctypes.windll.winmm.timeBeginPeriod(1)
        return bool(ok)
    except Exception:
        return False
