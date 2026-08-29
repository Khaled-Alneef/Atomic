"""Harbor-style motion-compensated frame synthesis for Atomic's mpv player.

The owner's recording was measured frame by frame after the ordinary player
pacing fixes made no visible difference.  Its repeated positions line up with
sampling a ~23.976 fps source at the recorder's ~25 fps timestamps, so another
DWM/timer/cache tweak cannot create the missing motion samples.  Harbor solves
this class of low-fps motion with SVP's VapourSynth/svpflow engine rather than
mpv's simple temporal blending.

This module follows that architecture on Windows:
  * detect an existing SVP 4 installation;
  * locate VSScript.dll plus svpflow1/2;
  * prime the VapourSynth DLL search path before libmpv loads;
  * generate an Atomic-owned .vpy using Super -> Analyse -> SmoothFps;
  * add it as a named mpv VapourSynth filter;
  * switch hwdec to auto-copy while the filter is active, exactly because the
    analysis filter needs CPU-visible frames.

Nothing is downloaded or bundled here.  SVP is third-party licensed software;
Atomic reuses an installation the user already has.  With no usable SVP install
this patch is a complete no-op and the existing player remains unchanged.
"""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
from pathlib import Path

from . import storage

_INSTALLED = False
_READY = False
_ROOT = None
_MANAGER = None
_VSSCRIPT = None
_FLOW1 = None
_FLOW2 = None
_SCRIPT = None
_DLL_HANDLES = []
_MANAGER_STARTED = False

_LOAD_WITH_ALTERED_SEARCH_PATH = 0x00000008


# Harbor's Windows search locations, plus the two common LOCALAPPDATA shapes.
def _candidate_roots():
    seen = set()
    raw = [
        Path(r"C:\Program Files (x86)\SVP 4"),
        Path(r"C:\Program Files\SVP 4"),
    ]
    for env in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        value = os.environ.get(env)
        if not value:
            continue
        base = Path(value)
        raw.append(base / "SVP 4")
        raw.append(base / "Programs" / "SVP 4")
    for root in raw:
        key = os.path.normcase(str(root))
        if key in seen:
            continue
        seen.add(key)
        yield root


def _find_file(root: Path, names, depth=6):
    wanted = {str(name).lower() for name in names}

    def walk(folder: Path, left: int):
        if left <= 0:
            return None
        try:
            entries = list(folder.iterdir())
        except OSError:
            return None
        children = []
        for path in entries:
            try:
                if path.is_dir():
                    children.append(path)
                elif path.name.lower() in wanted:
                    return path
            except OSError:
                continue
        for child in children:
            found = walk(child, left - 1)
            if found is not None:
                return found
        return None

    return walk(root, depth)


def _vsscript_file(root: Path):
    candidates = []

    def collect(folder: Path, left: int):
        if left <= 0:
            return
        try:
            entries = list(folder.iterdir())
        except OSError:
            return
        for path in entries:
            try:
                if path.is_dir():
                    collect(path, left - 1)
                elif path.name.lower() == "vsscript.dll":
                    candidates.append(path)
            except OSError:
                continue

    collect(root, 6)

    def has_python(path):
        try:
            return any(p.is_file() and p.name.lower().startswith("python3")
                       and p.name.lower().endswith(".dll")
                       for p in path.iterdir())
        except OSError:
            return False

    # Same preference as Harbor: a VSScript beside its Python runtime first,
    # then a 64-bit-looking directory, then whatever remains.
    candidates.sort(key=lambda path: (
        not has_python(path.parent),
        "64" not in str(path.parent),
        len(str(path)),
    ))
    return candidates[0] if candidates else None


def _discover():
    global _ROOT, _MANAGER, _VSSCRIPT, _FLOW1, _FLOW2
    if os.name != "nt" or os.environ.get("ATOMIC_DISABLE_SVP"):
        return False
    for root in _candidate_roots():
        manager = root / "SVPManager.exe"
        if not manager.is_file():
            continue
        vsscript = _vsscript_file(root)
        flow1 = _find_file(root, ("svpflow1_vs.dll", "svpflow1_vs64.dll"))
        flow2 = _find_file(root, ("svpflow2_vs.dll", "svpflow2_vs64.dll"))
        if not (vsscript and flow1 and flow2):
            continue
        _ROOT, _MANAGER = root, manager
        _VSSCRIPT, _FLOW1, _FLOW2 = vsscript, flow1, flow2
        return True
    return False


def _prepend_path(folder: Path):
    value = str(folder)
    current = os.environ.get("PATH", "")
    entries = current.split(os.pathsep) if current else []
    if not any(os.path.normcase(entry) == os.path.normcase(value)
               for entry in entries if entry):
        os.environ["PATH"] = value + (os.pathsep + current if current else "")


def _crt_set_env(name: str, value: str):
    """Mirror Harbor's CRT environment update for VSScript consumers."""
    entry = f"{name}={value}"
    for module_name in ("ucrtbase", "msvcrt"):
        try:
            module = ctypes.CDLL(module_name)
            putenv = module._wputenv
            putenv.argtypes = [ctypes.c_wchar_p]
            putenv.restype = ctypes.c_int
            putenv(entry)
        except Exception:
            continue


def _load_dll(path: Path):
    try:
        handle = ctypes.WinDLL(str(path), winmode=_LOAD_WITH_ALTERED_SEARCH_PATH)
        _DLL_HANDLES.append(handle)  # keep it resident for libmpv/VapourSynth
        return True
    except Exception:
        return False


def _prime_environment():
    if _VSSCRIPT is None:
        return False
    folder = _VSSCRIPT.parent
    _prepend_path(folder)
    os.environ["VSSCRIPT_PATH"] = str(_VSSCRIPT)
    _crt_set_env("VSSCRIPT_PATH", str(_VSSCRIPT))

    # VapourSynth distributions commonly keep their Python runtime beside
    # VSScript.dll. Load it first, then vapoursynth.dll, then VSScript.dll,
    # matching Harbor's dependency order.
    try:
        python_dlls = sorted(
            p for p in folder.iterdir()
            if p.is_file() and p.name.lower().startswith("python3")
            and p.name.lower().endswith(".dll"))
    except OSError:
        python_dlls = []
    for path in python_dlls:
        _load_dll(path)
    vapoursynth = folder / "vapoursynth.dll"
    if vapoursynth.is_file():
        _load_dll(vapoursynth)
    return _load_dll(_VSSCRIPT)


_VPY_TEMPLATE = r'''import vapoursynth as vs
from fractions import Fraction
core = vs.core

if not hasattr(core, "svp1"):
    core.std.LoadPlugin(__FLOW1__)
if not hasattr(core, "svp2"):
    core.std.LoadPlugin(__FLOW2__)

clip = video_in
_f = clip.format
if _f is None or _f.color_family != vs.YUV or _f.bits_per_sample != 8 or _f.subsampling_w != 1 or _f.subsampling_h != 1:
    clip = core.resize.Bicubic(clip, format=vs.YUV420P8, dither_type="error_diffusion")

src = container_fps if container_fps and container_fps > 1 else 23.976
screen = display_fps if display_fps and display_fps > 20 else 60.0

# Already-high-frame-rate material needs no synthesis. For low-fps video,
# choose a generated rate near 60 that divides the *actual monitor* by a whole
# number. This keeps the generated frames evenly presented on 60/75/90/100/
# 120/144/165/240 Hz displays instead of fixing 24fps only to introduce a new
# output cadence mismatch.
if src >= 50:
    clip.set_output()
else:
    candidates = []
    for div in range(1, 9):
        fps = screen / div
        if fps >= max(40.0, src * 1.45) and fps <= 75.0:
            candidates.append(fps)
    target = min(candidates, key=lambda fps: (abs(fps - 60.0), -fps)) if candidates else min(screen, 60.0)
    if target <= src * 1.05:
        clip.set_output()
    else:
        fr = Fraction(target / src).limit_denominator(2000)
        num, den = fr.numerator, fr.denominator
        sup = core.svp1.Super(clip, "{gpu:1}")
        vec = core.svp1.Analyse(sup["clip"], sup["data"], clip, "{}")
        smooth = core.svp2.SmoothFps(
            clip, sup["clip"], sup["data"], vec["clip"], vec["data"],
            "{rate:{num:%d,den:%d},algo:13,mask:{cover:80}}" % (num, den),
            src=clip, fps=src)
        smooth = core.std.AssumeFPS(
            smooth,
            fpsnum=int(round(src * num / den * 1000)),
            fpsden=1000)
        smooth.set_output()
'''


def _write_script():
    global _SCRIPT
    if not (_FLOW1 and _FLOW2):
        return None
    folder = storage.DATA_DIR / "svp"
    try:
        folder.mkdir(parents=True, exist_ok=True)
        script = (_VPY_TEMPLATE
                  .replace("__FLOW1__", json.dumps(str(_FLOW1)))
                  .replace("__FLOW2__", json.dumps(str(_FLOW2))))
        path = folder / "atomic_svp.vpy"
        if not path.is_file() or path.read_text(encoding="utf-8") != script:
            temporary = path.with_suffix(".vpy.tmp")
            temporary.write_text(script, encoding="utf-8")
            os.replace(temporary, path)
        _SCRIPT = path
        return path
    except OSError:
        return None


def _manager_running():
    if os.name != "nt":
        return False
    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        output = subprocess.check_output(
            ["tasklist", "/FI", "IMAGENAME eq SVPManager.exe", "/NH"],
            stderr=subprocess.DEVNULL,
            creationflags=flags,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=2,
        )
        return "svpmanager.exe" in output.lower()
    except Exception:
        return False


def _ensure_manager():
    global _MANAGER_STARTED
    if _MANAGER_STARTED or _MANAGER is None or _manager_running():
        _MANAGER_STARTED = True
        return
    try:
        subprocess.Popen(
            [str(_MANAGER)],
            cwd=str(_ROOT or _MANAGER.parent),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        _MANAGER_STARTED = True
    except OSError:
        pass


def ready():
    return bool(_READY and _SCRIPT and _SCRIPT.is_file())


def status():
    return {
        "ready": ready(),
        "root": str(_ROOT or ""),
        "script": str(_SCRIPT or ""),
    }


def install():
    global _INSTALLED, _READY
    if _INSTALLED:
        return
    _INSTALLED = True
    if not _discover():
        return
    if not _prime_environment():
        return
    if _write_script() is None:
        return

    from . import video_backend

    original_create = video_backend.create

    def create(window_id: int, **overrides):
        if not ready():
            return original_create(window_id, **overrides)
        _ensure_manager()
        path = str(_SCRIPT).replace("\\", "/")
        tuned = {
            # Harbor's exact compatibility choices for VapourSynth/SVP.
            "vf": f"@atomic-svp:vapoursynth=[{path}]",
            "hwdec": "auto-copy",
            "hr_seek_framedrop": False,
        }
        tuned.update(overrides)
        try:
            return original_create(window_id, **tuned)
        except Exception:
            # The filter is optional enhancement. A stale/broken SVP install
            # must never take the ordinary player down with it.
            try:
                from . import logs
                logs.exception("SVP filter could not start; using native playback")
            except Exception:
                pass
            return original_create(window_id, **overrides)

    video_backend.create = create
    _READY = True
