"""Drive and photograph the real Atomic window from a separate process, on the
other monitor, without ever taking the keyboard or the pointer (CLAUDE.md rule 14).

usage: python rig.py launch <exe> <appdata_dir>   start off his monitor, wait for window
       python rig.py wait [seconds]               wait for the window (default 30)
       python rig.py shot <name.png> [x0 y0 x1 y1]  screenshot the window (physical px)
       python rig.py click <x> <y> [right]        posted click, window-relative physical px
       python rig.py move <x> <y>                 posted hover
       python rig.py key <key>                    posted key: "escape", "f11", "r"; no chords
       python rig.py type <text>                  posted characters
       python rig.py wheel <delta> [count] [interval_ms] [x y]   one notch down is -120
       python rig.py wheelto ...                  the same as wheel
       python rig.py rect                         print window rect
       python rig.py guard                        print what the guard stopped, if anything
       python rig.py close                        close this repo's Atomic, never his

**His rule, 3 November 2026: "make working without taking the window focus and in
the other monitor as a rule that always be done!"** Everything here therefore goes
by message to one window of the process this rig started. Nothing calls
SetForegroundWindow, SetCursorPos, mouse_event, keybd_event or SendInput, because
each of those reaches into whatever he is doing. Measured that day with his own
Atomic open on the 2K panel:

    Popen from here            the test window took the foreground for all 41s
    started through WMI        the foreground never taken, in three runs
    the launch logo            on the primary monitor, always (splash._centre),
                               until ATOMIC_NO_SPLASH

`launch` starts a guard beside the app, which ends it the moment it holds the
foreground or shows a window anywhere but the monitor it was given.
"""
import ctypes, ctypes.wintypes as w, json, os, subprocess, sys, tempfile, time
u = ctypes.windll.user32
try:
    u.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
except Exception:
    u.SetProcessDPIAware()
u.EnumWindows.argtypes = [ctypes.WINFUNCTYPE(ctypes.c_bool, w.HWND, w.LPARAM), w.LPARAM]
u.GetWindowTextW.argtypes = [w.HWND, w.LPWSTR, ctypes.c_int]
u.GetWindowTextLengthW.argtypes = [w.HWND]
u.IsWindowVisible.argtypes = [w.HWND]
u.GetWindowRect.argtypes = [w.HWND, ctypes.POINTER(w.RECT)]
u.GetWindowThreadProcessId.argtypes = [w.HWND, ctypes.POINTER(w.DWORD)]
u.GetForegroundWindow.restype = w.HWND
u.WindowFromPoint.restype = w.HWND
u.WindowFromPoint.argtypes = [w.POINT]
u.GetAncestor.restype = w.HWND
u.GetAncestor.argtypes = [w.HWND, w.UINT]
u.ScreenToClient.argtypes = [w.HWND, ctypes.POINTER(w.POINT)]
u.GetClassNameW.argtypes = [w.HWND, w.LPWSTR, ctypes.c_int]
u.PostMessageW.argtypes = [w.HWND, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
u.SetWindowPos.argtypes = [w.HWND, w.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, w.UINT]

k32 = ctypes.windll.kernel32
k32.OpenProcess.restype = w.HANDLE
k32.GetExitCodeProcess.argtypes = [w.HANDLE, ctypes.POINTER(w.DWORD)]
k32.CloseHandle.argtypes = [w.HANDLE]
k32.TerminateProcess.argtypes = [w.HANDLE, w.UINT]
k32.QueryFullProcessImageNameW.argtypes = [
    w.HANDLE, w.DWORD, w.LPWSTR, ctypes.POINTER(w.DWORD)]
k32.CreateToolhelp32Snapshot.restype = w.HANDLE
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_TERMINATE = 0x0001
HWND_TOPMOST = -1
SWP_KEEP_FOCUS = 0x0001 | 0x0002 | 0x0010   # NOSIZE, NOMOVE, NOACTIVATE

HERE = os.path.dirname(os.path.abspath(__file__))
_PID_FILE = os.path.join(HERE, "rig_pid.txt")
_GUARD_FILE = os.path.join(HERE, "rig_guard.txt")


def _owning_exe_name(hwnd):
    """The lowercase image name (no path) of the process that owns this
    window, or "" if it cannot be read."""
    pid = w.DWORD(0)
    u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        return ""
    handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(260)
        size = w.DWORD(260)
        if not k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return ""
        return os.path.basename(buf.value).lower()
    finally:
        k32.CloseHandle(handle)


def _alive(pid):
    handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    code = w.DWORD(0)
    ok = k32.GetExitCodeProcess(handle, ctypes.byref(code)) and code.value == 259
    k32.CloseHandle(handle)
    return bool(ok)


def _pinned_pid():
    """The pid `launch` started; None when nothing was launched or it is gone
    - fail closed, never fall back to any window titled Atomic (that is how the
    owner's own copy would take a click)."""
    try:
        pid = int(open(_PID_FILE).read().strip())
    except (OSError, ValueError):
        return None     # nothing launched by this rig: drive nothing
    return pid if _alive(pid) else None


def monitors():
    """(rect, work rect, is primary, dpi) of every monitor, in physical px."""
    class MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", w.DWORD), ("rcMonitor", w.RECT), ("rcWork", w.RECT), ("dwFlags", w.DWORD)]
    found = []
    shcore = ctypes.WinDLL("shcore")

    @ctypes.WINFUNCTYPE(ctypes.c_bool, w.HMONITOR, w.HDC, ctypes.POINTER(w.RECT), w.LPARAM)
    def cb(handle, _dc, _rect, _):
        info = MONITORINFO(); info.cbSize = ctypes.sizeof(MONITORINFO)
        u.GetMonitorInfoW(handle, ctypes.byref(info))
        x, y = w.UINT(96), w.UINT(96)
        shcore.GetDpiForMonitor(handle, 0, ctypes.byref(x), ctypes.byref(y))
        tuple_of = lambda r: (r.left, r.top, r.right, r.bottom)
        found.append((tuple_of(info.rcMonitor), tuple_of(info.rcWork), bool(info.dwFlags & 1), x.value))
        return True
    u.EnumDisplayMonitors(None, None, cb, 0)
    return found


def test_monitor():
    """The monitor tests run on: any but the primary, which is the 2K panel he
    works on. None with only one connected, and then nothing is launched."""
    others = [m for m in monitors() if not m[2]]
    return others[0] if others else None


def find():
    found = []
    pinned = _pinned_pid()
    if pinned is None:
        return None             # pinned to a launch that is gone: match nothing

    @ctypes.WINFUNCTYPE(ctypes.c_bool, w.HWND, w.LPARAM)
    def cb(h, _):
        if not u.IsWindowVisible(h):
            return True
        n = u.GetWindowTextLengthW(h)
        if n <= 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        u.GetWindowTextW(h, buf, n + 1)
        # Exactly the app's title. "Atomic - File Explorer" (the repo folder
        # open in Explorer) matched a startswith test once and took the
        # clicks meant for the app. Title alone is not enough any more:
        # the Claude Code desktop app titles its own window after the open
        # project, so on this repo its window is *also* exactly "Atomic" -
        # measured 5 September 2026, a screenshot meant for the test build
        # came back showing the chat transcript instead. Filtering by the
        # owning process (Atomic.exe, or py/python for a source run) is
        # what a title match alone cannot do.
        if buf.value.strip() == "Atomic":
            # **Only the process this rig launched.** 8 October 2026: the
            # owner's installed Atomic was open beside the test build, both
            # "Atomic", both Atomic.exe - and the first match takes the clicks.
            owner = w.DWORD(0)
            u.GetWindowThreadProcessId(h, ctypes.byref(owner))
            if owner.value != pinned:
                return True
            exe = _owning_exe_name(h)
            if exe not in ("atomic.exe", "python.exe", "py.exe", "pythonw.exe"):
                return True
            r = w.RECT(); u.GetWindowRect(h, ctypes.byref(r))
            if r.right - r.left > 400 and r.bottom - r.top > 300:
                found.append((h, (r.left, r.top, r.right, r.bottom), buf.value))
        return True
    u.EnumWindows(cb, 0)
    # The largest: the search suggestions popup is a second window called
    # "Atomic" that a click and a screenshot would otherwise take for the app.
    return max(found, key=lambda f: (f[1][2] - f[1][0]) * (f[1][3] - f[1][1])) if found else None


def wait(seconds=30):
    t0 = time.time()
    while time.time() - t0 < seconds:
        f = find()
        if f:
            return f
        time.sleep(0.25)
    return None


def shot(name, crop=None):
    from PIL import ImageGrab
    f = find()
    if not f:
        print("no window"); _say_guard(); return
    h, rect, title = f
    img = ImageGrab.grab(bbox=rect, all_screens=True)
    if crop:
        img = img.crop(tuple(crop))
    img.save(name)
    print("saved", name, rect, img.size)


def _target(x, y):
    """The window under a point of *our* window, and the screen point. By
    message, because a real click would take the pointer and the foreground.
    Refused unless the window really is ours: whatever is under that point
    gets the message, and it must never be something of his."""
    f = find()
    if not f:
        _say_guard()
        raise SystemExit("no window")
    h, rect, _ = f
    pt = w.POINT(rect[0] + int(x), rect[1] + int(y))
    target = u.WindowFromPoint(pt)
    if not target or u.GetAncestor(target, 2) != h:     # GA_ROOT
        raise SystemExit(f"refused: the window at {x},{y} is not the test window")
    return target, pt


def _client_lparam(target, pt):
    c = w.POINT(pt.x, pt.y)
    u.ScreenToClient(target, ctypes.byref(c))
    return ((c.y & 0xFFFF) << 16) | (c.x & 0xFFFF)


def click(x, y, button="left"):
    target, pt = _target(x, y)
    lparam = _client_lparam(target, pt)
    down, up, held = (0x0201, 0x0202, 0x0001) if button == "left" else (0x0204, 0x0205, 0x0002)
    u.PostMessageW(target, 0x0200, 0, lparam); time.sleep(0.08)
    u.PostMessageW(target, down, held, lparam); time.sleep(0.05)
    u.PostMessageW(target, up, 0, lparam)


def move(x, y):
    target, pt = _target(x, y)
    u.PostMessageW(target, 0x0200, 0, _client_lparam(target, pt))


VK = {"escape": 0x1B, "esc": 0x1B, "enter": 0x0D, "f11": 0x7A, "space": 0x20, "left": 0x25,
      "right": 0x27, "up": 0x26, "down": 0x28, "tab": 0x09, "backspace": 0x08, "home": 0x24,
      "end": 0x23, "pgdn": 0x22, "pgup": 0x21}


def key(spec):
    """One key, by message, to the window itself. A chord cannot be sent this
    way: Ctrl is read from the real keyboard, which is his. Refused rather than
    held down under whatever he is typing - click the thing instead."""
    f = find()
    if not f:
        _say_guard()
        raise SystemExit("no window")
    p = spec.lower()
    if "+" in p:
        raise SystemExit(f"refused: {spec} needs the real keyboard (rule 14); click it instead")
    if p in VK: code = VK[p]
    elif len(p) == 1: code = ord(p.upper())
    elif p.startswith("f") and p[1:].isdigit(): code = 0x6F + int(p[1:])
    elif p.startswith("0x"): code = int(p, 16)
    else: raise SystemExit("unknown key " + p)
    scan = u.MapVirtualKeyW(code, 0)
    u.PostMessageW(f[0], 0x0100, code, 1 | (scan << 16)); time.sleep(0.03)
    u.PostMessageW(f[0], 0x0101, code, 1 | (scan << 16) | (3 << 30))


def wheelto(delta, count=1, interval_ms=0.0, x=None, y=None):
    """WM_MOUSEWHEEL posted straight to the window at a point - what Windows'
    own hover routing does for a real mouse. The middle of the window without
    one. SendInput's wheel goes to the keyboard-focus window, which is not ours
    and must not be (and measured 5 September 2026 it died in the Qt host)."""
    if x is None or y is None:
        f = find()
        if not f:
            _say_guard()
            raise SystemExit("no window")
        x, y = (f[1][2] - f[1][0]) // 2, (f[1][3] - f[1][1]) // 2
    target, pt = _target(x, y)
    lparam = ((pt.y & 0xFFFF) << 16) | (pt.x & 0xFFFF)
    t0 = time.perf_counter()
    for i in range(int(count)):
        u.PostMessageW(target, 0x020A, (int(delta) & 0xFFFF) << 16, lparam)
        due = t0 + (i + 1) * interval_ms / 1000.0
        while time.perf_counter() < due:
            pass
    print("wheel:", count, "events of", delta, "at", interval_ms, "ms")


wheel = wheelto


def typetext(text):
    f = find()
    if not f:
        _say_guard()
        raise SystemExit("no window")
    for ch in text:
        u.PostMessageW(f[0], 0x0102, ord(ch), 1); time.sleep(0.02)


def close():
    # **Only this repository's build or a source run - never by window
    # title or by exe name.** It used to kill every window titled
    # "Atomic" and then `taskkill /IM Atomic.exe`, which takes the
    # owner's installed copy with it (memory: never close his installed
    # Atomic; found 2 October 2026). Matched by path: app\Atomic.exe
    # under this repo, or a python.exe whose command line runs this
    # repo's src/main.py. Every match, not the first: two source runs
    # were up at once and the survivor took the next test's clicks.
    repo = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
    script = (
        "$r = '" + repo.replace("'", "''") + "'; "
        "Get-CimInstance Win32_Process | Where-Object { "
        "($_.Name -eq 'Atomic.exe' -and $_.ExecutablePath -like \"$r\\*\") -or "
        "($_.Name -like 'python*.exe' -and $_.CommandLine -like \"*$r*src*main.py*\") "
        "} | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }")
    subprocess.call(["powershell", "-NoProfile", "-Command", script],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.8)


def legacy_driver(path):
    """For the drivers written before rule 14, which launch for themselves, hold
    the foreground and move the pointer. They stop here until converted, unless
    he has said it is fine for this run."""
    if os.environ.get("ATOMIC_RIG_HE_SAID_OK") != "1":
        raise SystemExit(f"{os.path.basename(path)} takes the foreground and the pointer "
                         "(CLAUDE.md rule 14). Use rig.py's commands, or convert it to them.")


def _say_guard():
    try:
        print("guard:", open(_GUARD_FILE, encoding="utf-8").read().strip())
    except OSError:
        pass


def _family(root):
    """root and everything it started, read from a process snapshot."""
    class PE(ctypes.Structure):
        _fields_ = [("dwSize", w.DWORD), ("cntUsage", w.DWORD), ("th32ProcessID", w.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", w.DWORD),
                    ("cntThreads", w.DWORD), ("th32ParentProcessID", w.DWORD),
                    ("pcPriClassBase", ctypes.c_long), ("dwFlags", w.DWORD),
                    ("szExeFile", ctypes.c_wchar * 260)]
    snap = k32.CreateToolhelp32Snapshot(2, 0)
    pairs = []
    k32.Process32FirstW.argtypes = k32.Process32NextW.argtypes = [w.HANDLE, ctypes.POINTER(PE)]
    entry = PE(); entry.dwSize = ctypes.sizeof(PE)
    more = k32.Process32FirstW(snap, ctypes.byref(entry))
    while more:
        pairs.append((entry.th32ProcessID, entry.th32ParentProcessID))
        more = k32.Process32NextW(snap, ctypes.byref(entry))
    k32.CloseHandle(snap)
    mine, grew = {root}, True
    while grew:
        grew = False
        for pid, parent in pairs:
            if parent in mine and pid not in mine:
                mine.add(pid); grew = True
    return mine


def guard(pid, allowed):
    """Runs beside the app for as long as it lives. Ends it the moment it holds
    the foreground or shows a window off the monitor it was given, and leaves
    the reason in rig_guard.txt, which the rig's commands print."""
    try:
        os.remove(_GUARD_FILE)
    except OSError:
        pass
    family, read_at = {pid}, 0.0
    t0 = time.time()
    bad = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, w.HWND, w.LPARAM)
    def cb(h, _):
        owner = w.DWORD(0)
        u.GetWindowThreadProcessId(h, ctypes.byref(owner))
        if owner.value in family and u.IsWindowVisible(h):
            r = w.RECT(); u.GetWindowRect(h, ctypes.byref(r))
            on_screen = r.right > r.left and r.bottom > r.top and r.left > -30000
            outside = r.right <= allowed[0] or r.left >= allowed[2] or r.bottom <= allowed[1] or r.top >= allowed[3]
            spills = r.left < allowed[0] - 16 or r.right > allowed[2] + 16 or r.top < allowed[1] - 16 or r.bottom > allowed[3] + 16
            if on_screen and (outside or spills):
                bad.append(f"a window at {(r.left, r.top, r.right, r.bottom)}, off the test monitor {allowed}")
        return True

    while _alive(pid):
        now = time.time()
        if now - read_at > 0.5:
            family, read_at = _family(pid), now
        u.EnumWindows(cb, 0)
        owner = w.DWORD(0)
        u.GetWindowThreadProcessId(u.GetForegroundWindow(), ctypes.byref(owner))
        if owner.value in family:
            bad.append("the foreground")
        if bad:
            open(_GUARD_FILE, "w", encoding="utf-8").write(f"stopped the app at +{now - t0:.2f}s: it took {bad[0]}")
            handle = k32.OpenProcess(PROCESS_TERMINATE, False, pid)
            if handle:
                k32.TerminateProcess(handle, 1); k32.CloseHandle(handle)
            close()
            return
        time.sleep(0.01)


def launch(exe, appdata):
    """Start it on the other monitor with no right to the foreground: the window
    placed by the copy's own settings, the launch logo off, spawned through WMI so
    it does not inherit this process's standing, and the guard beside it."""
    monitor = test_monitor()
    if monitor is None:
        sys.exit("refused: only one monitor is connected, and it is his (rule 14)")
    appdata = os.path.abspath(appdata)
    real = os.path.abspath(os.environ.get("APPDATA", ""))
    if appdata == real or appdata.startswith(real + os.sep):
        sys.exit("refused: that is his real data, not a copy (rule 5)")
    settings = os.path.join(appdata, "Atomic", "settings.json")
    try:
        saved = json.load(open(settings, encoding="utf-8-sig"))
    except (OSError, ValueError):
        saved = {}
    (left, top, right, bottom) = monitor[1]
    ratio = monitor[3] / 96.0
    saved["window_geometry"] = {"x": left, "y": top, "width": int((right - left) / ratio),
                                "height": int((bottom - top) / ratio) - 42, "maximized": False}
    os.makedirs(os.path.dirname(settings), exist_ok=True)
    json.dump(saved, open(settings, "w", encoding="utf-8"))

    # A source run starts python.exe itself, not the py launcher, so the pid
    # below is the window's owner and can be pinned. Unpinned, a source run
    # that failed to start (15 October 2026: a relative path) left find() free
    # to take the owner's own open Atomic, and eighteen clicks landed in it.
    if exe.endswith(".py"):
        exe = os.path.abspath(exe)
        python = subprocess.run(["py", "-3.13", "-c", "import sys; print(sys.executable)"],
                                capture_output=True, text=True).stdout.strip()
        line = f'"{python}" "{exe}"'
    else:
        exe = os.path.abspath(exe)
        line = f'"{exe}"'
    env = dict(os.environ, APPDATA=appdata, PYTHONIOENCODING="utf-8", ATOMIC_NO_SPLASH="1")
    envfile = os.path.join(tempfile.gettempdir(), f"rig_env_{os.getpid()}.json")
    json.dump([f"{a}={b}" for a, b in env.items()], open(envfile, "w", encoding="utf-8"))
    ps = ("$e = [string[]](Get-Content -Raw '" + envfile + "' | ConvertFrom-Json); "
          "$si = New-CimInstance -ClassName Win32_ProcessStartup -ClientOnly -Property @{EnvironmentVariables=$e}; "
          "$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{CommandLine='"
          + line + "'; CurrentDirectory='" + os.path.dirname(exe)
          + "'; ProcessStartupInformation=[CimInstance]$si}; "
          "Write-Output (\"$($r.ReturnValue) $($r.ProcessId)\")")
    out = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True)
    os.remove(envfile)
    try:
        rc, pid = out.stdout.split()[-2:]
    except ValueError:
        sys.exit("launch failed: " + out.stdout + out.stderr)
    if rc != "0":
        sys.exit("launch failed: " + out.stdout + out.stderr)
    open(_PID_FILE, "w").write(pid)
    subprocess.Popen([sys.executable, os.path.abspath(__file__), "_guard", pid] + [str(v) for v in monitor[0]],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=0x00000008 | 0x00000200)   # DETACHED_PROCESS | NEW_PROCESS_GROUP
    f = wait(60)
    if not f:
        _say_guard()
        sys.exit("launch failed: no window")
    # On top of what is on that monitor, so a screenshot is of the app, and
    # NOACTIVATE so being on top costs nobody the keyboard.
    u.SetWindowPos(f[0], w.HWND(HWND_TOPMOST), 0, 0, 0, 0, SWP_KEEP_FOCUS)
    print("window:", f, "on monitor", monitor[0])


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "launch": launch(sys.argv[2], sys.argv[3])
    elif cmd == "_guard": guard(int(sys.argv[2]), [int(v) for v in sys.argv[3:7]])
    elif cmd == "guard":
        _say_guard()
        print("app running:", _pinned_pid() is not None)
    elif cmd == "wait": print(wait(float(sys.argv[2]) if len(sys.argv) > 2 else 30))
    elif cmd == "shot": shot(sys.argv[2], [int(v) for v in sys.argv[3:7]] if len(sys.argv) >= 7 else None)
    elif cmd == "click": click(int(sys.argv[2]), int(sys.argv[3]), sys.argv[4] if len(sys.argv) > 4 else "left")
    elif cmd == "move": move(int(sys.argv[2]), int(sys.argv[3]))
    elif cmd == "key": key(sys.argv[2])
    elif cmd == "type": typetext(sys.argv[2])
    elif cmd in ("wheel", "wheelto"): wheelto(int(sys.argv[2]), int(sys.argv[3]) if len(sys.argv) > 3 else 1, float(sys.argv[4]) if len(sys.argv) > 4 else 0.0, int(sys.argv[5]) if len(sys.argv) > 6 else None, int(sys.argv[6]) if len(sys.argv) > 6 else None)
    elif cmd == "rect": print(find())
    elif cmd == "close": close()
    elif cmd == "sleep": time.sleep(float(sys.argv[2]))
