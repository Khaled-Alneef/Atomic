"""Real window, real player, local file, copy of his data. Prints mpv's own
presentation state every few seconds and starts sampler.py (a separate
process) over the centre of the drawn video."""
import ctypes, ctypes.wintypes as w, json, os, shutil, subprocess, sys, tempfile, time
from pathlib import Path
SP = Path(__file__).parent
REPO = Path(r"C:\Users\pc\Code\VS_Code\Python\Atomic")
FILE = r"C:\Users\pc\Desktop\AA\Watchable\Money_Heist\[Atomic] Money_Heist_EP01_S05.mkv"
SEEK = float(os.environ.get("HARNESS_SEEK", "300"))
SAMPLE_S = float(os.environ.get("HARNESS_SAMPLE_S", "20"))
TAG = os.environ.get("HARNESS_TAG", "run")
EXTRA = json.loads(os.environ.get("HARNESS_MPV", "{}"))   # option overrides for A/B

work = Path(tempfile.mkdtemp(prefix="atomic-play-"))
appdata = work / "appdata"
shutil.copytree(Path(os.environ["APPDATA"]) / "Atomic", appdata / "Atomic", dirs_exist_ok=True)
os.environ["APPDATA"] = str(appdata)
sys.path.insert(0, str(REPO / "src"))
u = ctypes.windll.user32
u.GetWindowRect.argtypes = [w.HWND, ctypes.POINTER(w.RECT)]

def say(*a):
    print(f"[{time.strftime('%H:%M:%S')}]", *a, flush=True)

try:
    from helpers import storage
    import main
    # testing.md: web/backend re-points DATA_DIR at import, so set all three
    # after every import and assert before anything writes.
    copy = appdata / "Atomic"
    storage.DATA_DIR = copy
    try:
        from web import backend, server
        backend.DATA_DIR = copy; server.DATA = copy
    except Exception as e:
        say("web not imported:", e)
    assert str(storage.DATA_DIR).startswith(str(appdata)), storage.DATA_DIR
    say("DATA_DIR", storage.DATA_DIR)
    from helpers import video_backend
    if EXTRA:
        _orig_defaults = video_backend.default_options
        def patched_defaults():
            d = _orig_defaults(); d.update(EXTRA); return d
        video_backend.default_options = patched_defaults
        say("mpv option overrides:", EXTRA)
    from PyQt6.QtCore import QTimer
    from PyQt6.QtWidgets import QApplication
    from windows import player
    state = {}

    NAMES = ["current-vo", "video-sync", "hwdec-current", "display-fps",
             "estimated-display-fps", "vsync-ratio", "vsync-jitter",
             "frame-drop-count", "decoder-frame-drop-count",
             "vo-delayed-frame-count", "mistimed-frame-count", "interpolation",
             "container-fps", "estimated-vf-fps", "gpu-api", "gpu-context",
             "d3d11-flip", "d3d11-sync-interval", "swapchain-depth",
             "d3d11-adapter", "d3d11-output-csp", "target-colorspace-hint",
             "video-sync-max-video-change", "display-names", "display-width",
             "display-height", "display-hidpi-scale", "time-pos",
             "paused-for-cache", "avsync", "video-speed-correction",
             "audio-speed-correction", "deinterlace", "video-params/w",
             "video-params/h", "dwidth", "dheight", "osd-dimensions",
             "dither-depth", "tscale", "scale", "dscale", "cscale", "deband",
             "hwdec", "ao", "speed", "pause"]

    def read(handle, name):
        try:
            return handle[name]
        except Exception as e:
            return f"<{type(e).__name__}>"

    def poll():
        page = state.get("page")
        if page is None or page.handle is None:
            say("poll: no handle"); return
        vals = {n: read(page.handle, n) for n in NAMES}
        say("MPV " + " ".join(f"{k}={vals[k]!r}" for k in NAMES))

    def start(window):
        if os.environ.get("HARNESS_ENTRY") == "aot":
            # His own entry (downloads.json carries it), through the real
            # source path: find_streams, prepare, play - no local file.
            entry = {"id": "51ad279b-2302-483a-885d-dc3af30f78b2", "title": "Attack on Titan",
                     "type": "Anime", "imdb_id": "tt2560140",
                     "url": "stremio:///detail/series/tt2560140", "site_id": None}
            say("opening player on Attack on Titan S01E02 via sources")
            page = player.open_player(window, entry, season=1, episode=2)
            state["page"] = page
            state["waited"] = 0
            wait_playing()
            return
        entry = {"id": "harness-money-heist", "title": "Money Heist", "type": "Series",
                 "imdb_id": "tt6468322"}
        stream = {"url": FILE, "kind": "direct", "title": "local file", "source": "Local",
                  "quality": "1080p"}
        say("opening player")
        page = player.open_player(window, entry, season=5, episode=1, streams=[stream])
        state["page"] = page
        QTimer.singleShot(7000, seek)

    def wait_playing():
        """Poll the core until time-pos advances; then give it 8s and sample."""
        page = state["page"]
        state["waited"] += 1
        pos = read(page.handle, "time-pos") if page.handle is not None else None
        last = state.get("last_pos")
        state["last_pos"] = pos
        if isinstance(pos, (int, float)) and isinstance(last, (int, float)) and pos > last + 0.5:
            say(f"playing: time-pos {pos:.1f} after {state['waited']}s; sampling in 8s "
                f"(hwdec={read(page.handle, 'hwdec-current')} fps={read(page.handle, 'container-fps')} "
                f"path={str(read(page.handle, 'path'))[:80]})")
            if os.environ.get("HARNESS_FULLSCREEN") == "1":
                say("entering full screen (the app's own F11 path)")
                QTimer.singleShot(1500, page.toggle_fullscreen)
            QTimer.singleShot(8000, sample)
            return
        if state["waited"] > 150:
            say("gave up waiting for playback; time-pos", pos); finish(); return
        if state["waited"] % 10 == 0:
            say(f"waiting... {state['waited']}s time-pos={pos} cache={read(page.handle, 'paused-for-cache') if page.handle else None}")
        QTimer.singleShot(1000, wait_playing)

    def seek():
        page = state["page"]
        say("seek to", SEEK)
        try:
            page.handle.command("seek", SEEK, "absolute")
        except Exception as e:
            say("seek failed", e)
        QTimer.singleShot(5000, sample)

    def sample():
        page = state["page"]
        try:
            u.SetCursorPos(-1800, 1000)     # pointer off the primary panel
        except Exception:
            pass
        hwnd = int(page.surface.winId())
        r = w.RECT(); u.GetWindowRect(hwnd, ctypes.byref(r))
        osd = read(page.handle, "osd-dimensions") or {}
        ml, mr, mt, mb = (int(osd.get(k, 0)) for k in ("ml", "mr", "mt", "mb"))
        vx0, vy0, vx1, vy1 = r.left + ml, r.top + mt, r.right - mr, r.bottom - mb
        cw, ch = 480, 270
        cx, cy = (vx0 + vx1) // 2, (vy0 + vy1) // 2
        region = (cx - cw // 2, cy - ch // 2, cw, ch)
        say(f"surface rect phys=({r.left},{r.top},{r.right},{r.bottom}) osd={osd} "
            f"video=({vx0},{vy0},{vx1},{vy1}) region={region}")
        state["region"] = region; state["video"] = (vx0, vy0, vx1, vy1)
        out = SP / f"samples_{TAG}.npz"
        log = open(SP / f"sampler_{TAG}.txt", "w")
        state["proc"] = subprocess.Popen([sys.executable, str(SP / "sampler.py"), str(SAMPLE_S), str(out),
                                          *map(str, region)], stdout=log, stderr=subprocess.STDOUT)
        vlog = open(SP / f"vblank_{TAG}.txt", "w")
        state["vproc"] = subprocess.Popen([sys.executable, str(SP / "vblank_probe.py"), str(SAMPLE_S)],
                                          stdout=vlog, stderr=subprocess.STDOUT)
        say("sampler + vblank probe started; time-pos", read(page.handle, "time-pos"),
            "fullscreen:", page.window().isFullScreen())
        state["polls"] = 0
        t = QTimer(page); t.setInterval(5000); t.timeout.connect(lambda: tick(t)); t.start()
        state["timer"] = t
        poll()

    def tick(t):
        state["polls"] += 1
        poll()
        if state["polls"] * 5 >= SAMPLE_S + 4:
            t.stop(); finish()

    def finish():
        say("finishing; time-pos", read(state["page"].handle, "time-pos"))
        try:
            state["page"].close_player()
        except Exception as e:
            say("close failed", e)
        QTimer.singleShot(1500, QApplication.instance().quit)

    Real = main.MainWindow
    class MW(Real):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            QTimer.singleShot(3500, lambda: start(self))
    main.MainWindow = MW
    try:
        main.main()
    except SystemExit:
        pass
    p = state.get("proc")
    if p is not None:
        p.wait(timeout=60)
        say("sampler said:", (SP / f"sampler_{TAG}.txt").read_text())
    v = state.get("vproc")
    if v is not None:
        v.wait(timeout=60)
        say("vblank probe said: " + (SP / f"vblank_{TAG}.txt").read_text())
finally:
    shutil.rmtree(work, ignore_errors=True)
    say("copy removed:", not work.exists())
