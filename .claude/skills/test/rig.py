"""Drive and photograph the real Atomic window from a separate process.

usage: python rig.py launch <exe> <appdata_dir>   start detached, wait for window
       python rig.py wait [seconds]               wait for the window (default 30)
       python rig.py shot <name.png>              screenshot the window (physical px)
       python rig.py click <x> <y>                click at window-relative physical px
       python rig.py move <x> <y>                 hover
       python rig.py key <keys>                   e.g. "ctrl+3", "escape", "f11", "r"
       python rig.py type <text>
       python rig.py wheel <delta> [count] [interval_ms] [x y]   120 = one notch down is -120
       python rig.py rect                         print window rect
       python rig.py close                        kill Atomic
"""
import ctypes, ctypes.wintypes as w, os, subprocess, sys, time
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

def find():
    found = []
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
        # clicks meant for the app.
        if buf.value.strip() == "Atomic":
            r = w.RECT(); u.GetWindowRect(h, ctypes.byref(r))
            if r.right - r.left > 400 and r.bottom - r.top > 300:
                found.append((h, (r.left, r.top, r.right, r.bottom), buf.value))
        return True
    u.EnumWindows(cb, 0)
    return found[0] if found else None

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
        print("no window"); return
    h, rect, title = f
    u.SetForegroundWindow(h)
    time.sleep(0.15)
    img = ImageGrab.grab(bbox=rect, all_screens=True)
    if crop:
        img = img.crop(tuple(crop))
    img.save(name)
    print("saved", name, rect, img.size)

def click(x, y, button="left"):
    f = find()
    if not f:
        print("no window"); return
    h, rect, _ = f
    u.SetForegroundWindow(h); time.sleep(0.05)
    # The Claude desktop app on the other monitor confines the cursor to
    # its own screen on every click (measured 3 September 2026: clip
    # rect (-1920, 0, 0, 1080) with Atomic closed). Released here, or
    # SetCursorPos lands at x=-1 and the click misses the app.
    u.ClipCursor(None)
    u.SetCursorPos(rect[0] + int(x), rect[1] + int(y)); time.sleep(0.08)
    down, up = (0x0002, 0x0004) if button == "left" else (0x0008, 0x0010)
    u.mouse_event(down, 0, 0, 0, 0); time.sleep(0.04)
    # The button-down activates the window and the clip comes straight
    # back, warping the cursor to x=-1 before the button-up - so the up
    # landed outside the target and no click ever fired. Put it back.
    u.ClipCursor(None); u.SetCursorPos(rect[0] + int(x), rect[1] + int(y)); time.sleep(0.02)
    u.mouse_event(up, 0, 0, 0, 0)

def move(x, y):
    f = find()
    if not f:
        print("no window"); return
    h, rect, _ = f
    u.ClipCursor(None)
    u.SetCursorPos(rect[0] + int(x), rect[1] + int(y))

VK = {"ctrl": 0x11, "shift": 0x10, "alt": 0x12, "escape": 0x1B, "esc": 0x1B, "enter": 0x0D,
      "f11": 0x7A, "space": 0x20, "left": 0x25, "right": 0x27, "up": 0x26, "down": 0x28,
      "tab": 0x09, "backspace": 0x08, "home": 0x24, "end": 0x23, "pgdn": 0x22, "pgup": 0x21}

def key(spec):
    f = find()
    if f:
        u.SetForegroundWindow(f[0]); time.sleep(0.05)
    parts = spec.lower().split("+")
    codes = []
    for p in parts:
        if p in VK: codes.append(VK[p])
        elif len(p) == 1: codes.append(ord(p.upper()))
        elif p.startswith("f") and p[1:].isdigit(): codes.append(0x6F + int(p[1:]))
        else: raise SystemExit("unknown key " + p)
    for c in codes: u.keybd_event(c, 0, 0, 0); time.sleep(0.02)
    for c in reversed(codes): u.keybd_event(c, 0, 2, 0); time.sleep(0.02)

def wheel(delta, count=1, interval_ms=0.0, x=None, y=None):
    """Mouse wheel through SendInput, at the pointer (or x,y window px):
    `delta` per event in WHEEL_DELTA units (120 = one notch; 20-40 at a
    high rate imitates a precision touchpad's stream), `count` events,
    `interval_ms` apart."""
    f = find()
    if not f:
        print("no window"); return
    h, rect, _ = f
    u.SetForegroundWindow(h); time.sleep(0.05)
    u.ClipCursor(None)
    if x is not None and y is not None:
        # The Claude desktop app re-clips the cursor to its own monitor
        # on every click, so a SetCursorPos can land at x=-1; release,
        # move, and read back until the pointer is where the wheel must
        # be delivered (SendInput's wheel goes to the window under it).
        want = (rect[0] + int(x), rect[1] + int(y))
        pt = w.POINT()
        for _ in range(5):
            u.ClipCursor(None)
            u.SetCursorPos(*want); time.sleep(0.03)
            u.GetCursorPos(ctypes.byref(pt))
            if (pt.x, pt.y) == want:
                break
        print("pointer at", (pt.x, pt.y), "wanted", want)
    class MI(ctypes.Structure):
        _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long), ("mouseData", ctypes.c_uint32),
                    ("dwFlags", ctypes.c_uint32), ("time", ctypes.c_uint32), ("dwExtraInfo", ctypes.c_void_p)]
    class INPUT(ctypes.Structure):
        _fields_ = [("type", ctypes.c_uint32), ("mi", MI), ("pad", ctypes.c_uint64 * 2)]
    inp = INPUT(); inp.type = 0; inp.mi.dwFlags = 0x0800; inp.mi.mouseData = ctypes.c_uint32(int(delta) & 0xFFFFFFFF)
    t0 = time.perf_counter()
    for i in range(int(count)):
        u.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
        due = t0 + (i + 1) * interval_ms / 1000.0
        while time.perf_counter() < due:
            pass
    print("wheel:", count, "events of", delta, "at", interval_ms, "ms")

def wheelto(delta, count=1, interval_ms=0.0, x=None, y=None):
    """WM_MOUSEWHEEL posted straight to the window under the pointer -
    what Windows' own hover routing does for a real mouse. SendInput's
    wheel goes to the keyboard-focus window, which after the web reader
    closes is the Qt main window, whose host widget does not forward a
    wheel to the native WebView2 child (measured 5 September 2026: 3
    notches moved Home right after launch and nothing afterwards)."""
    f = find()
    if not f:
        print("no window"); return
    h, rect, _ = f
    u.SetForegroundWindow(h); time.sleep(0.05)
    want = (rect[0] + int(x), rect[1] + int(y)) if x is not None else None
    pt = w.POINT()
    for _ in range(5):
        u.ClipCursor(None)
        if want: u.SetCursorPos(*want)
        time.sleep(0.03)
        u.GetCursorPos(ctypes.byref(pt))
        if not want or (pt.x, pt.y) == want:
            break
    u.WindowFromPoint.restype = w.HWND
    target = u.WindowFromPoint(pt)
    buf = ctypes.create_unicode_buffer(64); u.GetClassNameW(target, buf, 64)
    print("pointer at", (pt.x, pt.y), "-> window", target, buf.value)
    u.PostMessageW.argtypes = [w.HWND, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
    lparam = (pt.y << 16) | (pt.x & 0xFFFF)
    t0 = time.perf_counter()
    for i in range(int(count)):
        wparam = ((int(delta) & 0xFFFF) << 16)
        u.PostMessageW(target, 0x020A, wparam, lparam)
        due = t0 + (i + 1) * interval_ms / 1000.0
        while time.perf_counter() < due:
            pass
    print("wheelto:", count, "events of", delta, "at", interval_ms, "ms")

def typetext(text):
    for ch in text:
        vk = u.VkKeyScanW(ord(ch)) & 0xFF
        u.keybd_event(vk, 0, 0, 0); u.keybd_event(vk, 0, 2, 0); time.sleep(0.02)

def close():
    # The window's own process, whatever started it (Atomic.exe or a
    # python.exe running the source tree), then the exe by name.
    # Every Atomic window, not the first: two source runs were up at once
    # and the survivor took the next test's clicks.
    for _ in range(6):
        f = find()
        if not f:
            break
        pid = w.DWORD(0)
        u.GetWindowThreadProcessId(f[0], ctypes.byref(pid))
        if pid.value:
            subprocess.call(["taskkill", "/PID", str(pid.value), "/F", "/T"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(0.8)
    subprocess.call(["taskkill", "/IM", "Atomic.exe", "/F", "/T"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "launch":
        exe, appdata = sys.argv[2], sys.argv[3]
        env = dict(os.environ, APPDATA=appdata)
        cmd = ["py", "-3.13", exe] if exe.endswith(".py") else [exe]
        env["PYTHONIOENCODING"] = "utf-8"
        log = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "app_stdout.log"), "ab")
        subprocess.Popen(cmd, env=env, cwd=os.path.dirname(exe) or ".", stdout=log, stderr=log,
                         creationflags=0x00000008 | 0x00000200)   # DETACHED_PROCESS | NEW_PROCESS_GROUP
        f = wait(60)
        print("window:", f)
    elif cmd == "wait": print(wait(float(sys.argv[2]) if len(sys.argv) > 2 else 30))
    elif cmd == "shot": shot(sys.argv[2], [int(v) for v in sys.argv[3:7]] if len(sys.argv) >= 7 else None)
    elif cmd == "click": click(int(sys.argv[2]), int(sys.argv[3]), sys.argv[4] if len(sys.argv) > 4 else "left")
    elif cmd == "move": move(int(sys.argv[2]), int(sys.argv[3]))
    elif cmd == "key": key(sys.argv[2])
    elif cmd == "type": typetext(sys.argv[2])
    elif cmd == "wheelto": wheelto(int(sys.argv[2]), int(sys.argv[3]) if len(sys.argv) > 3 else 1, float(sys.argv[4]) if len(sys.argv) > 4 else 0.0, int(sys.argv[5]) if len(sys.argv) > 6 else None, int(sys.argv[6]) if len(sys.argv) > 6 else None)
    elif cmd == "wheel": wheel(int(sys.argv[2]), int(sys.argv[3]) if len(sys.argv) > 3 else 1, float(sys.argv[4]) if len(sys.argv) > 4 else 0.0, int(sys.argv[5]) if len(sys.argv) > 6 else None, int(sys.argv[6]) if len(sys.argv) > 6 else None)
    elif cmd == "rect": print(find())
    elif cmd == "close": close()
    elif cmd == "sleep": time.sleep(float(sys.argv[2]))
