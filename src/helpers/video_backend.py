"""The video player's decode engine: libmpv, embedded into a Qt widget.

Why libmpv and not QtMultimedia, which is already installed and would
have cost nothing: three things this app actually needs, measured on
this machine before the choice was made (packaging/fetch_libmpv.py
pulls the build; `mpv v0.41.0-923`, ffmpeg `N-126125`).

  * **HLS.** Almost everything these sites and Stremio addons hand back
    is an `.m3u8`, not a file. libmpv reports 74 protocols including
    http/https/hls; QtMultimedia on Windows goes through Media
    Foundation, whose HLS support is partial and silently fails to a
    black frame rather than an error.
  * **Arabic subtitles that are actually shaped.** This is the whole
    point of the feature. Arabic needs bidi and contextual glyph
    shaping - letters change form by position - and libass (linked in,
    verified present) does it via harfbuzz. Qt would shape a QLabel
    overlay correctly too, but only for plain text: no .ass positioning,
    styling or typesetting, which is what fansub releases ship.
  * **External subtitle files at all.** QMediaPlayer exposes the
    subtitle *tracks inside* a container and has no way to load a
    downloaded .srt alongside a stream.

The DLL is not in the repository - see packaging/fetch_libmpv.py for
why, and for how to get it back. Everything here degrades to "player
unavailable, here is why" rather than raising: a missing DLL must read
as a clear message with a fix in it, not a traceback on a black screen.
"""

import os
import sys
import threading

# Resolved once, at import: python-mpv finds the library through ctypes
# at *its* import time, so the search path has to be in place before
# `import mpv` happens anywhere in the process.
_load_error = None
_mpv = None


def _library_dir():
    """Where libmpv-2.dll should be, frozen and from source.

    sys._MEIPASS first: in the built exe the DLL is unpacked there by
    PyInstaller, and vendor/ does not exist at all. The source tree's
    vendor/ is the development case."""
    candidates = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(meipass)
        candidates.append(os.path.dirname(sys.executable))
    here = os.path.dirname(os.path.abspath(__file__))
    candidates.append(os.path.abspath(os.path.join(here, "..", "..", "vendor")))
    for directory in candidates:
        if os.path.isfile(os.path.join(directory, "libmpv-2.dll")):
            return directory
    return None


def _load():
    """Import python-mpv with the vendored DLL reachable, once.

    add_dll_directory *and* PATH: the first is what Python 3.8+ actually
    honours for dependent DLL resolution, the second is what ctypes'
    own library search reads. Setting only one of them loaded the
    library on this machine and failed on a clean one - both, and it is
    not a question of which."""
    global _mpv, _load_error
    if _mpv is not None or _load_error is not None:
        return
    directory = _library_dir()
    if not directory:
        _load_error = ("The video engine (libmpv-2.dll) is missing.\n"
                       "Run: python packaging/fetch_libmpv.py")
        return
    try:
        if hasattr(os, "add_dll_directory"):
            os.add_dll_directory(directory)
        os.environ["PATH"] = directory + os.pathsep + os.environ.get("PATH", "")
        import mpv  # noqa: E402  - deliberately after the path is set
        _mpv = mpv
    except Exception as error:                      # pragma: no cover
        _load_error = f"The video engine could not be loaded: {error}"


def available() -> bool:
    _load()
    return _mpv is not None


def unavailable_reason():
    """Why there is no player, in words meant for the user. None when
    there is one."""
    _load()
    return _load_error


# Fonts that actually carry Arabic on Windows, best first. libass picks
# a fallback on its own, but its default pick for a plain .srt is a
# latin font, and the result is a line of tofu boxes rather than an
# obviously-wrong font - which reads as "the subtitles are broken"
# rather than "the font is wrong". Naming one removes the guess.
ARABIC_FONTS = ("Segoe UI", "Tahoma", "Arial", "Noto Naskh Arabic")


def default_options() -> dict:
    """mpv options every player instance starts with.

    keep_open: mpv's default is to unload the file at EOF, which makes
    the last frame vanish and every property read fail - the UI wants to
    sit on the end frame and show 100%.

    ytdl off: there is no yt-dlp in this bundle, and leaving it on means
    mpv spends a timeout shelling out to something that is not there
    before it fails a URL it was never going to play."""
    return {
        "vo": "gpu",
        "hwdec": "auto-safe",
        # Lock frame presentation to the display's own raster instead of
        # the audio clock. Measured 22 August 2026 on the owner's 144Hz
        # panel (GTX 1660 SUPER, 1080p H.264 via d3d11va): with the
        # default `video-sync=audio` mpv times frames on the audio clock
        # and never aligns them to a vsync - the display-sync counters
        # (vsync-ratio, vsync-jitter, mistimed-frame-count) do not even
        # exist in that mode - so 24fps content lands a half-vsync off
        # and pans micro-judder. With display-resample the same clip
        # locked at vsync-ratio exactly 6.000 (144/23.976), jitter
        # 0.000, 0 dropped / 0 delayed / 0 mistimed over 9s, and mpv
        # measured the real raster itself (estimated-display-fps
        # 144.003) - no refresh rate is hardcoded anywhere and none
        # should be. Note what this does *not* do: a 23.976fps master
        # still presents 23.976 distinct frames a second (measured; a
        # 60fps clip presents 60.05) - nothing invents frames, each just
        # holds for exactly 6 refreshes instead of roughly 6.
        # interpolation+oversample costs nothing at an integer ratio and
        # is what keeps non-integer ones (25fps content, a 60Hz second
        # monitor) from beating: oversample repeats frames aligned to
        # vsync rather than blending them, so anime line art stays sharp.
        "video_sync": "display-resample",
        "interpolation": True,
        "tscale": "oversample",
        "keep_open": "yes",
        "idle": "yes",
        "ytdl": False,
        "osc": False,                 # the app draws its own controls
        "input_default_bindings": False,
        "input_vo_keyboard": False,
        "cache": "yes",
        # A stream that stalls mid-buffer should recover rather than end
        # the file; these are the values mpv itself recommends for
        # network playback.
        "demuxer_max_bytes": "64MiB",
        "demuxer_readahead_secs": 20,
        "sub_font": ARABIC_FONTS[0],
        "sub_ass_override": "scale",  # keep .ass styling, honour our size
        "sub_auto": "no",             # we choose subtitles explicitly
        # No embedded subtitle track selected when a file opens, ever -
        # the owner's ask. `sub_auto: no` above only stops mpv picking up
        # a .srt sitting *beside* the file; a track inside the container
        # was still auto-selected, so an English or Chinese sub burned
        # itself over the picture on release after release. "no" is not
        # "hidden": nothing is chosen at all, which is what makes Off the
        # honest state in the tracks panel on load. Picking a row still
        # sets sid, and an external subtitle is still added with
        # `select` (player._apply_subtitle), so both routes in are
        # untouched.
        "sid": "no",
        # Arabic .srt files are very often Windows-1256. We transcode to
        # UTF-8 before handing a file over (see helpers/subtitles.py), so
        # this is the belt to that braces - and "auto" is safe because a
        # correctly-UTF-8 file is detected as one.
        "sub_codepage": "auto",
    }


class PlayerError(Exception):
    """Playback failed in a way the UI should say out loud."""


def create(window_id: int, **overrides):
    """An MPV instance rendering into an existing native window.

    `window_id` is a real HWND - the caller must have made its widget a
    native window first (Qt.WA_NativeWindow) and read winId() *after*
    that, or mpv renders into a handle Qt later replaces and the video
    appears in its own detached window.

    Raises PlayerError rather than returning None: every call site here
    already has to tell the user something, and a None that means two
    different things (no engine / bad options) is how that message goes
    wrong."""
    _load()
    if _mpv is None:
        raise PlayerError(_load_error or "no video engine")
    options = default_options()
    options.update(overrides)
    try:
        return _mpv.MPV(wid=str(int(window_id)), **options)
    except Exception as error:
        raise PlayerError(str(error)) from error


def version_info() -> dict:
    """What engine is actually loaded - for Settings, and for a bug
    report that would otherwise be a guess about which build ran."""
    _load()
    if _mpv is None:
        return {}
    handle = None
    try:
        handle = _mpv.MPV(vo="null", ao="null", idle="yes")
        return {"mpv": handle.mpv_version, "ffmpeg": handle.ffmpeg_version}
    except Exception:
        return {}
    finally:
        if handle is not None:
            try:
                handle.terminate()
            except Exception:
                pass


# mpv delivers events and log messages on its own thread. Anything
# crossing back into Qt has to go through a signal, exactly like the
# lookup threads in tracker.py - touching a widget from here is the same
# crash, just from a different thread.
_terminate_lock = threading.Lock()


def shutdown(handle):
    """Tear an MPV instance down without letting its own shutdown raise.

    terminate() joins mpv's event thread. If that thread is mid-callback
    into Python it can surface whatever the callback raised, on the UI
    thread, during window close - which is a crash on exit that looks
    like a Qt bug and is not one. Swallowing is correct here: there is
    nothing left to salvage at this point."""
    if handle is None:
        return
    with _terminate_lock:
        try:
            handle.terminate()
        except Exception:
            pass
