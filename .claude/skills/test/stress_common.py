"""Shared by the two stress drivers: launch the frozen exe against a copy,
read the log by segment, read the resume file."""
import os, re, sys, time, json, subprocess, pathlib
import ctypes, ctypes.wintypes as wt
sys.path.insert(0, r"C:\Users\pc\Code\VS_Code\Python\Atomic\.claude\skills\test")
import rig
rig.u.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
rig.u.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.UINT]
T0 = time.time()
LOG = None
# the catalogue grids: nine columns, first row's title line
CARD_X = [458, 700, 942, 1185, 1427, 1669, 1912, 2154, 2396]
CARD_Y = 694


def say(*a):
    print(f"[{time.time() - T0:6.1f}s]", *a, flush=True)


def log_text():
    try:
        return LOG.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def log_len():
    return len(log_text().splitlines())


def log_since(n, pattern=None):
    lines = log_text().splitlines()[n:]
    if pattern:
        lines = [l for l in lines if re.search(pattern, l)]
    return lines


def front():
    f = rig.find()
    if f:
        rig.u.SetForegroundWindow(f[0]); time.sleep(0.15)


def open_card(i):
    """A card body on the catalogue grid: first click focuses the page."""
    front(); rig.click(CARD_X[i], CARD_Y); time.sleep(1.2); rig.click(CARD_X[i], CARD_Y)


def launch(exe, root, out):
    global LOG
    LOG = pathlib.Path(root) / "Atomic" / "atomic.log"
    rig.close(); time.sleep(1)
    try:
        LOG.unlink()
    except OSError:
        pass
    env = dict(os.environ); env["APPDATA"] = root; env["PYTHONIOENCODING"] = "utf-8"
    subprocess.Popen([exe], env=env, cwd=os.path.dirname(os.path.abspath(exe)),
                     stdout=open(pathlib.Path(out) / "app_stdout.log", "ab"), stderr=subprocess.STDOUT,
                     creationflags=0x00000008 | 0x00000200)
    if not rig.wait(40):
        raise SystemExit("no window")
    time.sleep(4)
    for _ in range(4):
        h = rig.find()[0]; rig.u.ShowWindow(h, 9); time.sleep(0.3)
        rig.u.SetWindowPos(h, None, -9, -9, 2578, 1398, 0x14); time.sleep(1.5)
        if rig.find()[1][2] - rig.find()[1][0] >= 2500:
            break
    time.sleep(2)
    say("launched", rig.find()[1])


def newest_resume(root):
    """(entry_id, season, episode, position, updated_at) of the newest record."""
    try:
        rows = json.load(open(pathlib.Path(root) / "Atomic" / "player_state.json", encoding="utf-8-sig"))
    except Exception:
        return None
    rows = [r for r in rows if isinstance(r, dict) and r.get("updated_at")]
    if not rows:
        return None
    r = max(rows, key=lambda r: r["updated_at"])
    return (r.get("entry_id"), r.get("season"), r.get("episode"), r.get("position") or 0.0, r["updated_at"])
