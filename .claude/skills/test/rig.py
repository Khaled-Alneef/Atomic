"""Drive and photograph the real Atomic window from a separate process.

usage: python rig.py launch <exe> <appdata_dir>   start detached, wait for window
       python rig.py wait [seconds]               wait for the window (default 30)
       python rig.py shot <name.png>              screenshot the window (physical px)
       python rig.py click <x> <y>                click at window-relative physical px
       python rig.py move <x> <y>                 hover
       python rig.py key <keys>                   e.g. "ctrl+3", "escape", "f11", "r"
       python rig.py type <text>
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
    elif cmd == "rect": print(find())
    elif cmd == "close": close()
    elif cmd == "sleep": time.sleep(float(sys.argv[2]))
