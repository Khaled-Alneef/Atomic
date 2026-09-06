"""The black flash: open Kingdom (WAN) 886, leave it, enter it again, and
wheel fast while sampler.py records the page area; count the frames that
are entirely the reader's ground. Targets its own process's window, so a
running Atomic of the owner's is left alone.
Usage: py drive_flash.py <exe-or-main.py> <root> <out> <label> <runs>"""
import sys, os, time, pathlib, subprocess, ctypes, ctypes.wintypes as wt
import numpy as np
sys.path.insert(0, r"C:\Users\pc\Code\VS_Code\Python\Atomic\.claude\skills\test")
import rig
target, root, out, label, runs = sys.argv[1:6]
runs = int(runs); root = os.path.abspath(root); out = pathlib.Path(out); out.mkdir(parents=True, exist_ok=True)
PY = r"C:\Users\pc\AppData\Local\Programs\Python\Python313\python.exe"
SAMPLER = r"C:\Users\pc\Code\VS_Code\Python\Atomic\.claude\skills\test\sampler.py"
u = rig.u
u.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
u.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.UINT]
env = dict(os.environ); env["APPDATA"] = root; env["PYTHONIOENCODING"] = "utf-8"
WNDENUM = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)


def atomic_windows():
    """(hwnd, rect, pid) of every visible window titled Atomic."""
    found = []
    def cb(h, _):
        if not u.IsWindowVisible(h):
            return True
        n = u.GetWindowTextLengthW(h)
        buf = ctypes.create_unicode_buffer(n + 1); u.GetWindowTextW(h, buf, n + 1)
        if buf.value != "Atomic":
            return True
        pid = wt.DWORD(); u.GetWindowThreadProcessId(h, ctypes.byref(pid))
        r = wt.RECT(); u.GetWindowRect(h, ctypes.byref(r))
        found.append((h, (r.left, r.top, r.right, r.bottom), pid.value))
        return True
    u.EnumWindows(WNDENUM(cb), 0)
    return found


PRE = {pid for h, r, pid in atomic_windows()}      # the owner's own instance, left alone
if target.endswith(".py"):
    proc = subprocess.Popen([PY, target], env=env, cwd=os.path.dirname(os.path.dirname(target)),
                            stdout=open(out / f"src_{label}.log", "ab"), stderr=subprocess.STDOUT, creationflags=0x208)
    log = pathlib.Path(os.path.dirname(target)) / "data" / "atomic.log"
else:
    proc = subprocess.Popen([target], env=env, cwd=os.path.dirname(target), stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT, creationflags=0x208)
    log = pathlib.Path(root) / "Atomic" / "atomic.log"
def find_mine():
    mine = [(h, r) for h, r, pid in atomic_windows() if pid not in PRE]
    if not mine:
        return None
    h, r = max(mine, key=lambda f: (f[1][2] - f[1][0]) * (f[1][3] - f[1][1]))
    return (h, r, None)


rig.find = find_mine
t0 = time.time()
while time.time() - t0 < 60 and not find_mine():
    time.sleep(0.2)
if not find_mine():
    raise SystemExit("no window of my own")
time.sleep(4)
for _ in range(4):
    h = find_mine()[0]; u.ShowWindow(h, 9); time.sleep(0.3)
    u.SetWindowPos(h, None, -9, -9, 2578, 1398, 0x14); time.sleep(1.5)
    if find_mine()[1][2] - find_mine()[1][0] >= 2500:
        break
time.sleep(1.5)


def say(*a):
    print(f"[{time.time() - t0:6.1f}s]", *a, flush=True)


def log_len():
    try:
        return len(log.read_text(encoding="utf-8", errors="replace").splitlines())
    except OSError:
        return 0


def log_since(n, pat):
    import re
    try:
        lines = log.read_text(encoding="utf-8", errors="replace").splitlines()[n:]
    except OSError:
        return []
    return [l for l in lines if re.search(pat, l)]


def front():
    u.SetForegroundWindow(find_mine()[0]); time.sleep(0.15)


# Kingdom (WAN) through the search box
opened = False
for attempt in range(3):
    rig.key("ctrl+1"); time.sleep(3)
    front(); rig.click(449, 1257); time.sleep(1.2); rig.click(449, 1257); time.sleep(3.5)
    m = log_len(); rig.click(1859, 376); time.sleep(12)
    if log_since(m, r"reader sized"):
        opened = True
        break
    say("reader did not open, trying again"); rig.key("escape"); time.sleep(1.5)
if not opened:
    rig.shot(str(out / f"flash_{label}_noreader.png")); proc.kill(); raise SystemExit("could not open the chapter")
rig.shot(str(out / f"flash_{label}_reader.png"))
h, rect, _ = find_mine(); x0, y0, x1, y1 = rect; w, hh = x1 - x0, y1 - y0; cx, cy = w // 2, hh // 2
region = (x0 + int(w * 0.2), y0 + 100, int(w * 0.6), hh - 160)
results = []
for run in range(1, runs + 1):
    # leave the chapter and enter it again
    front(); rig.click(cx, cy); time.sleep(0.3); rig.key("escape"); time.sleep(2.0)
    npz = out / f"flash_{label}_{run}.npz"
    sampler = subprocess.Popen([sys.executable, SAMPLER, "10", str(npz), *map(str, region)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.7)
    m = log_len(); t_click = time.time(); rig.click(1859, 376); time.sleep(0.15)       # enter again, and wheel at once
    say(f"click wall {time.strftime('%H:%M:%S', time.localtime(t_click))}.{int((t_click % 1) * 1000):03d} sampler started {t_click - 0.7:.3f}")
    rig.move(cx, cy)
    if os.environ.get("NOWHEEL"):
        time.sleep(4.0)
    else:
        rig.wheelto(-120, 60, 40, cx, cy); time.sleep(1.2)
        rig.wheelto(-120, 60, 40, cx, cy); time.sleep(1.2)
        rig.wheelto(120, 60, 40, cx, cy); time.sleep(1.2)
        rig.wheelto(-120, 60, 40, cx, cy)
    sampler.wait(timeout=30)
    d = np.load(str(npz)); frames, ft = d["frames"], d["frame_t"]
    dark = np.array([(f < 24).mean() for f in frames]); med = float(np.median(dark))
    flashes = [(round(float(ft[i] - ft[0]), 2), round(float(dark[i]), 2))
               for i in range(1, len(frames) - 1)
               if dark[i] > 0.6 and dark[i] - max(dark[i - 1], dark[i + 1]) > 0.3]
    results.append(len(flashes))
    say(f"{label} run {run}: {len(frames)} distinct frames, ground frames {len(flashes)} {flashes[:8]} | reader lines {len(log_since(m, 'reader sized'))}")
say(f"{label}: ground frames per run {results}")
rig.key("escape"); time.sleep(1); rig.key("escape"); time.sleep(0.8)
proc.kill()
