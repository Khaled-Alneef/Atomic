"""A batch of rig steps in one process - one tool call drives a whole
sequence against the frozen exe (or the source tree) and reads the log
between steps. Written 7 September 2026 for the day's passes.
  py d.py <root> <out> step step step ...
steps: launch <exe> | launchsrc | attach | key K | keyvk 0xNN | click X Y |
       dclick X Y | move X Y | wheel DELTA COUNT X Y | ctrlwheel DELTA |
       ctrlwheelto DELTA X Y | x4 | x5 | sleep S | shot NAME [x0 y0 x1 y1] |
       mark | log PATTERN | tail N | front | focuswin | close | jobs
`log` prints the atomic.log lines since the last `mark` matching PATTERN
(regex); `jobs` prints the copy's queue file states; `keyvk` is for the
OEM keys rig.key cannot name (0xBB is "=", 0xBD is "-", 0x0D Enter sent
without re-fronting - the search field's Enter must be sent that way,
or the suggestion panel, a second window titled "Atomic", takes it);
`ctrlwheelto` posts WM_MOUSEWHEEL with MK_CONTROL to the window under
the pointer, since SendInput's wheel goes to the keyboard's window;
`launchsrc` runs src/main.py under 3.13 with its log at src/data/."""
import sys, os, time, json, pathlib, re, ctypes
sys.path.insert(0, r"C:\Users\pc\Code\VS_Code\Python\Atomic\.claude\skills\test")
import stress_common as sc
from stress_common import rig
root, out = os.path.abspath(sys.argv[1]), pathlib.Path(sys.argv[2])
out.mkdir(parents=True, exist_ok=True)
sc.LOG = pathlib.Path(root) / "Atomic" / "atomic.log"
u = rig.u
m = sc.log_len()
steps = sys.argv[3:]
i = 0


def xbutton(which):
    u.mouse_event(0x0080, 0, 0, which, 0); time.sleep(0.05); u.mouse_event(0x0100, 0, 0, which, 0)


def take(n=1):
    global i
    got = steps[i + 1:i + 1 + n]; i += n
    return got


while i < len(steps):
    s = steps[i]
    if s == "launch":
        (exe,) = take(1); sc.launch(exe, root, out)
    elif s == "launchsrc":
        # the source tree under 3.13 against the copy; its log is src/data/atomic.log
        import subprocess
        repo = r"C:\Users\pc\Code\VS_Code\Python\Atomic"
        sc.LOG = pathlib.Path(repo) / "src" / "data" / "atomic.log"
        rig.close(); time.sleep(1)
        try: sc.LOG.unlink()
        except OSError: pass
        env = dict(os.environ); env["APPDATA"] = root; env["PYTHONIOENCODING"] = "utf-8"
        subprocess.Popen([r"C:\Users\pc\AppData\Local\Programs\Python\Python313\python.exe", os.path.join(repo, "src", "main.py")],
                         env=env, cwd=repo, stdout=open(out / "src_stdout.log", "ab"), stderr=subprocess.STDOUT, creationflags=0x208)
        if not rig.wait(60): raise SystemExit("no window")
        time.sleep(4)
        for _ in range(4):
            h = rig.find()[0]; rig.u.ShowWindow(h, 9); time.sleep(0.3)
            rig.u.SetWindowPos(h, None, -9, -9, 2578, 1398, 0x14); time.sleep(1.5)
            if rig.find()[1][2] - rig.find()[1][0] >= 2500: break
        time.sleep(2); sc.say("source launched", rig.find()[1]); m = sc.log_len()
    elif s == "focuswin":
        import ctypes.wintypes as wt
        class G(ctypes.Structure):
            _fields_ = [("cbSize", wt.DWORD), ("flags", wt.DWORD), ("hwndActive", wt.HWND), ("hwndFocus", wt.HWND), ("hwndCapture", wt.HWND), ("hwndMenuOwner", wt.HWND), ("hwndMoveSize", wt.HWND), ("hwndCaret", wt.HWND), ("rcCaret", wt.RECT)]
        g = G(); g.cbSize = ctypes.sizeof(G); u.GetGUIThreadInfo(0, ctypes.byref(g))
        def cls(h):
            b = ctypes.create_unicode_buffer(128); u.GetClassNameW(h, b, 128); return b.value
        print("   focus window:", cls(g.hwndFocus) if g.hwndFocus else None, "| active:", cls(g.hwndActive) if g.hwndActive else None)
    elif s == "attach":
        f = rig.find(); sc.say("attached", f[1] if f else None)
    elif s == "key":
        (k,) = take(1); rig.key(k)
    elif s == "click":
        x, y = take(2); rig.click(int(x), int(y))
    elif s == "dclick":
        x, y = take(2); rig.click(int(x), int(y)); time.sleep(1.2); rig.click(int(x), int(y))
    elif s == "move":
        x, y = take(2); rig.move(int(x), int(y))
    elif s == "wheel":
        d, c, x, y = take(4); rig.wheelto(int(d), int(c), 0.05, int(x), int(y))
    elif s == "keyvk":
        # a raw virtual key, for the OEM keys rig.key cannot name: 0xBB is
        # '=' / '+', 0xBD is '-' / '_'
        (code,) = take(1); code = int(code, 0)
        u.keybd_event(code, 0, 0, 0); time.sleep(0.03); u.keybd_event(code, 0, 2, 0)
    elif s == "ctrlwheel":
        (d,) = take(1)
        u.keybd_event(0x11, 0, 0, 0); time.sleep(0.05)
        rig.wheel(int(d), 1, 0.0)
        time.sleep(0.05); u.keybd_event(0x11, 0, 2, 0)
    elif s == "ctrlwheelto":
        # WM_MOUSEWHEEL with MK_CONTROL posted to the window under the
        # pointer - what a real mouse does over an unfocused page
        import ctypes.wintypes as wt
        d, x, y = take(3); f = rig.find(); sx, sy = f[1][0] + int(x), f[1][1] + int(y)
        rig.move(int(x), int(y)); time.sleep(0.05)
        u.WindowFromPoint.restype = wt.HWND; u.WindowFromPoint.argtypes = [wt.POINT]
        target = u.WindowFromPoint(wt.POINT(sx, sy))
        u.PostMessageW.argtypes = [wt.HWND, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
        u.PostMessageW(target, 0x020A, ((int(d) & 0xFFFF) << 16) | 0x0008, ((sy & 0xFFFF) << 16) | (sx & 0xFFFF))
    elif s == "x4":
        xbutton(1)
    elif s == "x5":
        xbutton(2)
    elif s == "sleep":
        (t,) = take(1); time.sleep(float(t))
    elif s == "shot":
        (name,) = take(1)
        crop = None
        if i + 4 < len(steps) and all(re.fullmatch(r"-?\d+", v) for v in steps[i + 1:i + 5]):
            crop = [int(v) for v in take(4)]
        rig.shot(str(out / name), crop); sc.say("shot", name, crop or "")
    elif s == "mark":
        m = sc.log_len()
    elif s == "log":
        (pat,) = take(1)
        for l in sc.log_since(m, pat):
            print("   ", l[11:200])
    elif s == "tail":
        (n,) = take(1)
        for l in sc.log_text().splitlines()[-int(n):]:
            print("   ", l[11:200])
    elif s == "front":
        sc.front()
    elif s == "close":
        rig.close()
    elif s == "jobs":
        try:
            rows = json.load(open(pathlib.Path(root) / "Atomic" / "downloads.json", encoding="utf-8-sig"))
            print("   jobs:", [(r.get("id"), r.get("state"), round(100 * float(r.get("progress") or 0)), (r.get("detail") or "")[:40]) for r in rows if not str(r.get("id", "")).startswith("pad")])
        except Exception as exc:
            print("   jobs: unreadable", exc)
    else:
        raise SystemExit(f"unknown step {s!r} at {i}")
    i += 1
