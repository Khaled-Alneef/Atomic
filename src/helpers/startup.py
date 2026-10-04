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
off.

**Nothing here runs on launch any more (27 September 2026).** reconcile(),
which asked schtasks two seconds into every launch and re-registered the
task when it named another exe, was removed after Defender quarantined the
installed app as Behavior:Win32/Persistence.A!ml - see main.py where it was
called. The entry is written only by set_enabled, from Settings, the setup
wizard and uninstall. A task left naming an old exe is fixed by ticking the
setting off and on.

**The task is registered through Task Scheduler's own COM API, not
schtasks.exe (4 October 2026).** Defender quarantined the installed app
as Behavior:Win32/Persistence.A!ml a second time, and the owner placed it
exactly: right after ticking "Launch on Windows startup". Defender's event
1116 at 05:28:55 names Atomic.exe (started 05:28:33) as the process and
lists what set_enabled(True) had just written - System32\\Tasks\\Atomic
and its two TaskCache keys. The tick spawned a hidden schtasks.exe with
/Create /XML <a temp file> /F: an unsigned program in AppData driving the
console tool to plant its own logon task, which is the textbook shape of
that verdict. The owner chose to keep the fast task over going back to
the Run key, so the same definition now goes to ITaskFolder::RegisterTask
as a string, in-process - no child process, no temp file, no console -
and the queries and deletes that also spawned schtasks (Settings asked
is_enabled every time it opened) go the same way. Whether Defender's
cloud model is satisfied by that cannot be proven on demand; what is
certain is that the pattern it was shown is gone.
"""

import ctypes
import os
import sys
import uuid
import winreg
from contextlib import contextmanager
from pathlib import Path
from xml.sax.saxutils import escape

_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_VALUE_NAME = "Atomic"
_LEGACY_VALUE_NAME = "PC App"  # this app's old name - cleaned up opportunistically

# The scheduled task's name (registered in the root folder, "\Atomic").
_TASK_NAME = "Atomic"

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

# Task Scheduler 2.0 over raw COM vtables - no pywin32 or comtypes in the
# bundle, and both would be a dependency for four calls. Private WinDLLs:
# ctypes signatures are process-global (testing.md), and another module
# declaring CoCreateInstance differently must not change these.
_CLSID_TASK_SCHEDULER = "{0f87369f-a4e5-4cfc-bd3e-73e6154572dd}"
_IID_ITASK_SERVICE = "{2faba4c7-4da9-4013-9697-20cc3fd40f85}"
_CLSCTX_INPROC_SERVER = 0x1
_COINIT_APARTMENTTHREADED = 0x2
_TASK_CREATE_OR_UPDATE = 6
_TASK_LOGON_INTERACTIVE_TOKEN = 3

# Vtable slots (taskschd.h; IUnknown is 0-2, IDispatch 3-6).
_RELEASE = 2
_SERVICE_GET_FOLDER = 7
_SERVICE_CONNECT = 10
_FOLDER_GET_TASK = 13
_FOLDER_DELETE_TASK = 15
_FOLDER_REGISTER_TASK = 16


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]

    @classmethod
    def of(cls, text):
        return cls.from_buffer_copy(uuid.UUID(text).bytes_le)


class _VARIANT(ctypes.Structure):
    """An empty VARIANT (VT_EMPTY, all zero) - every VARIANT argument here
    is "not given". 24 bytes on x64, 16 on x86, as oaidl.h has it."""
    _fields_ = [("vt", ctypes.c_ushort), ("r1", ctypes.c_ushort),
                ("r2", ctypes.c_ushort), ("r3", ctypes.c_ushort),
                ("data", ctypes.c_ubyte * (2 * ctypes.sizeof(ctypes.c_void_p)))]


def _call(obj, slot, argtypes, *args) -> int:
    """Call slot `slot` of a COM object's vtable; returns the HRESULT."""
    vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    proto = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argtypes)
    return proto(vtable[slot])(obj, *args)


def _release(obj) -> None:
    if obj:
        _call(obj, _RELEASE, ())


class _Bstr:
    """A BSTR for the life of a with-block."""

    def __init__(self, text):
        self._oleaut = ctypes.WinDLL("oleaut32")
        self._oleaut.SysAllocString.restype = ctypes.c_void_p
        self._oleaut.SysAllocString.argtypes = [ctypes.c_wchar_p]
        self._oleaut.SysFreeString.argtypes = [ctypes.c_void_p]
        self.value = ctypes.c_void_p(self._oleaut.SysAllocString(text))

    def __enter__(self):
        return self.value

    def __exit__(self, *_exc):
        self._oleaut.SysFreeString(self.value)


class _TaskError(Exception):
    def __init__(self, step, hr):
        super().__init__(f"{step} answered 0x{hr & 0xFFFFFFFF:08X}")


@contextmanager
def _root_folder():
    """The scheduler's root folder ("\\"), connected as this user. Raises
    _TaskError on any failing step; releases everything on the way out.

    CoInitializeEx is asked for an STA, which is what Qt's main thread
    already is: S_OK/S_FALSE are paired with CoUninitialize, and
    RPC_E_CHANGED_MODE (a thread already in the MTA) is used as it is."""
    ole = ctypes.WinDLL("ole32")
    ole.CoInitializeEx.restype = ctypes.c_long
    ole.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    ole.CoCreateInstance.restype = ctypes.c_long
    ole.CoCreateInstance.argtypes = [ctypes.POINTER(_GUID), ctypes.c_void_p,
                                     ctypes.c_ulong, ctypes.POINTER(_GUID),
                                     ctypes.POINTER(ctypes.c_void_p)]
    ole.CoUninitialize.argtypes = []
    initialised = ole.CoInitializeEx(None, _COINIT_APARTMENTTHREADED) in (0, 1)
    service = ctypes.c_void_p()
    folder = ctypes.c_void_p()
    try:
        clsid = _GUID.of(_CLSID_TASK_SCHEDULER)
        iid = _GUID.of(_IID_ITASK_SERVICE)
        hr = ole.CoCreateInstance(ctypes.byref(clsid), None, _CLSCTX_INPROC_SERVER,
                                  ctypes.byref(iid), ctypes.byref(service))
        if hr < 0:
            raise _TaskError("CoCreateInstance", hr)
        empty = _VARIANT()
        hr = _call(service, _SERVICE_CONNECT, (_VARIANT,) * 4,
                   empty, empty, empty, empty)
        if hr < 0:
            raise _TaskError("Connect", hr)
        with _Bstr("\\") as root:
            hr = _call(service, _SERVICE_GET_FOLDER,
                       (ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)),
                       root, ctypes.byref(folder))
        if hr < 0:
            raise _TaskError("GetFolder", hr)
        yield folder
    finally:
        _release(folder)
        _release(service)
        if initialised:
            ole.CoUninitialize()


def _log(message) -> None:
    try:
        from . import logs
        logs.info(f"startup: {message}")
    except Exception:
        pass


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


def _task_exists(name=_TASK_NAME) -> bool:
    try:
        with _root_folder() as folder, _Bstr(name) as path:
            task = ctypes.c_void_p()
            hr = _call(folder, _FOLDER_GET_TASK,
                       (ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)),
                       path, ctypes.byref(task))
            _release(task)
            return hr >= 0
    except Exception:
        return False


def _create_task(name=_TASK_NAME, xml=None) -> bool:
    """Register (or replace) the logon task. Returns True on success.

    RegisterTask takes the XML as text, so the definition is never written
    to disk; TASK_CREATE_OR_UPDATE is what schtasks' /F was."""
    try:
        with _root_folder() as folder, _Bstr(name) as path, \
                _Bstr(xml or _task_xml()) as text:
            registered = ctypes.c_void_p()
            empty = _VARIANT()
            hr = _call(folder, _FOLDER_REGISTER_TASK,
                       (ctypes.c_void_p, ctypes.c_void_p, ctypes.c_long,
                        _VARIANT, _VARIANT, ctypes.c_int, _VARIANT,
                        ctypes.POINTER(ctypes.c_void_p)),
                       path, text, _TASK_CREATE_OR_UPDATE, empty, empty,
                       _TASK_LOGON_INTERACTIVE_TOKEN, empty,
                       ctypes.byref(registered))
            _release(registered)
            if hr < 0:
                raise _TaskError("RegisterTask", hr)
        _log(f"logon task registered ({name})")
        return True
    except Exception as exc:
        _log(f"logon task not registered ({type(exc).__name__}: {exc})")
        return False


def _delete_task(name=_TASK_NAME) -> None:
    """Remove the task if it is there; a missing one is not an error."""
    try:
        with _root_folder() as folder, _Bstr(name) as path:
            hr = _call(folder, _FOLDER_DELETE_TASK,
                       (ctypes.c_void_p, ctypes.c_long), path, 0)
        if hr >= 0:
            _log(f"logon task removed ({name})")
    except Exception as exc:
        _log(f"logon task not removed ({type(exc).__name__}: {exc})")


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


def install_dir() -> Path:
    """Where the bridge installs the folder build (packaging/bridge)."""
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser(r"~\AppData\Local")
    return Path(base) / "Programs" / "Atomic"


def is_installed_copy() -> bool:
    """Whether this process is the installed Atomic (uninstall asks)."""
    try:
        here = Path(sys.executable).resolve().parent
        return os.path.normcase(str(here)) == os.path.normcase(str(install_dir().resolve()))
    except Exception:
        return False


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
