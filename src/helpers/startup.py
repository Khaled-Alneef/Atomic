"""Toggle "launch on Windows startup" via the per-user registry Run key
(HKCU\\...\\Run) - no admin rights needed, no extra dependencies."""

import sys
import winreg
from pathlib import Path

_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_VALUE_NAME = "Atomic"
_LEGACY_VALUE_NAME = "PC App"  # this app's old name - cleaned up opportunistically


def _launch_command() -> str:
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    # Running from source: launch with pythonw (no console window) if we
    # can find it next to the interpreter, else fall back to python.exe.
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    interpreter = str(pythonw) if pythonw.exists() else sys.executable
    script = str(Path(__file__).resolve().parent.parent / "main.py")
    return f'"{interpreter}" "{script}"'


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


def is_enabled() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_READ | winreg.KEY_WRITE) as key:
            _migrate_legacy_value(key)
            winreg.QueryValueEx(key, _VALUE_NAME)
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
