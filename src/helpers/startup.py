"""Toggle "launch on Windows startup" via the per-user registry Run key
(HKCU\\...\\Run) - no admin rights needed, no extra dependencies."""

import sys
import winreg
from pathlib import Path

_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_VALUE_NAME = "Atomic"
_LEGACY_VALUE_NAME = "PC App"  # this app's old name - cleaned up opportunistically

# Passed by the registered startup command and by nothing else, so the
# app can tell "Windows started me at sign-in" from "the user opened me"
# - the two are otherwise identical from inside the process, and that
# difference is the whole basis of the fullscreen-on-startup setting
# (see app_settings.get_fullscreen_on_startup).
STARTUP_FLAG = "--startup"


def _launch_target() -> str:
    """The executable (plus script, from source) part of the startup
    command, without the flag - also what an already-registered entry is
    matched against before it gets rewritten. See _migrate_missing_flag."""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    # Running from source: launch with pythonw (no console window) if we
    # can find it next to the interpreter, else fall back to python.exe.
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    interpreter = str(pythonw) if pythonw.exists() else sys.executable
    script = str(Path(__file__).resolve().parent.parent / "main.py")
    return f'"{interpreter}" "{script}"'


def _launch_command() -> str:
    return f"{_launch_target()} {STARTUP_FLAG}"


def launched_on_startup(argv=None) -> bool:
    """Whether this process was started by the registered startup entry."""
    return STARTUP_FLAG in (sys.argv[1:] if argv is None else argv)


def _migrate_legacy_value(key) -> None:
    """The app used to register its startup entry under its old name -
    fold that into the current one instead of leaving a stale duplicate
    (or a toggle that reads as "off" while the old entry still runs it)."""
    try:
        winreg.QueryValueEx(key, _LEGACY_VALUE_NAME)
    except OSError:
        return
    try:
        winreg.DeleteValue(key, _LEGACY_VALUE_NAME)
    except OSError:
        pass


def _migrate_missing_flag(key) -> None:
    """Add STARTUP_FLAG to an entry registered before it existed.

    Without it, an app launched at sign-in is indistinguishable from one
    the user opened themselves, so fullscreen-on-startup would silently
    do nothing for everyone who already had startup turned on - until
    they thought to toggle it off and back on.

    Only rewritten when the entry already points at *this* executable.
    Running from source while a frozen Atomic owns the registered entry
    would otherwise replace the user's real startup command with a
    python + main.py one that only exists on a dev machine."""
    try:
        current, _kind = winreg.QueryValueEx(key, _VALUE_NAME)
    except OSError:
        return
    if STARTUP_FLAG in current or not current.strip().startswith(_launch_target()):
        return
    try:
        winreg.SetValueEx(key, _VALUE_NAME, 0, winreg.REG_SZ, _launch_command())
    except OSError:
        pass


def is_enabled() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_READ | winreg.KEY_WRITE) as key:
            _migrate_legacy_value(key)
            winreg.QueryValueEx(key, _VALUE_NAME)
            _migrate_missing_flag(key)
            return True
    except FileNotFoundError:
        return False
    except OSError:
        return False


def set_enabled(enabled: bool) -> None:
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_WRITE) as key:
        _migrate_legacy_value(key)
        if enabled:
            winreg.SetValueEx(key, _VALUE_NAME, 0, winreg.REG_SZ, _launch_command())
        else:
            try:
                winreg.DeleteValue(key, _VALUE_NAME)
            except FileNotFoundError:
                pass
