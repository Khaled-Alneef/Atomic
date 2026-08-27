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
        # **Display synchronisation and frame generation are two
        # different things, and this is the line between them.** They
        # get conflated in every discussion of "smooth playback", and
        # conflating them here once already produced a bug report
        # ("make them at least 144!!!!" - see player._live_video_stats,
        # where the same confusion is recorded from the other side).
        #
        #   video-sync + interpolation + tscale=oversample
        #       = *synchronisation*. Every frame the file contains is
        #         presented, aligned to the panel's raster, each held
        #         for a whole number of refreshes. Nothing is invented:
        #         a 23.976fps master still presents 23.976 distinct
        #         frames a second (measured). oversample repeats, it
        #         does not blend, so line art stays sharp.
        #
        #   tscale=mitchell (Motion Smoothing, below)
        #       = *frame generation*. Blends adjacent frames to
        #         synthesise motion the master never had. This is the
        #         soap-opera effect, and it is a preference, not a fix.
        #
        # Native source FPS stays the default either way - neither of
        # these changes how many frames the file has.
        "video_sync": "display-resample",
        # **No interpolation, and no control that turns it on.** The
        # owner, 27 August 2026: "remove the smoothing in the vid player
        # and the cadence lock entirely from the app, they are useless!"
        # Both were measured before being taken out, and he is right on
        # the evidence:
        #
        #   * Motion smoothing (interpolation + tscale=mitchell) could
        #     not fix pacing at all - with it on, vsync-ratio read the
        #     same 6.88 to 7.02 as with it off. All it changed was that
        #     frames got blended, which is the ghosting on anime line
        #     art. It also made mpv render one frame per *display*
        #     refresh - 240 a second here - for the same picture.
        #   * Cadence lock (video-sync-max-video-change=2) did lock the
        #     cadence on a 165Hz panel (7.00000 flat against 6.88-7.02)
        #     but at the cost of 1.69% slower playback, and on his 240Hz
        #     panel it does nothing whatever: 240 / 23.976 = 10.01,
        #     which mpv already absorbs inside its default 1%.
        #
        # Stated rather than left implicit, because "off" is the claim
        # being made and mpv's default is not this file's to assume.
        "interpolation": False,
        # Stated rather than left to mpv's probe, which is what Stremio
        # does too. d3d11 is the context d3d11va decoding hands its
        # surfaces to, so choosing anything else costs a copy per frame.
        **({"gpu_context": "d3d11"} if sys.platform == "win32" else {}),
        # **Tag the swapchain with the video's own colorspace.**
        #
        # The owner, 26 August 2026: "the video player saturation seems a
        # bit brighter than it should be". Measured through the
        # display-config API rather than guessed at, and it corrected the
        # first theory, which was HDR:
        #
        #   display 0: HDR supported=True enabled=False wideColorEnforced=True
        #   display 1: HDR supported=True enabled=False wideColorEnforced=False
        #
        # HDR is off, so that was not it. What display 0 has is Windows'
        # automatic colour management, which is exactly the case where an
        # *untagged* swapchain gets re-interpreted into a wider gamut and
        # BT.709 video comes back oversaturated. Hinting the colorspace
        # ends the guessing: Windows converts from what the content
        # actually is instead of assuming what it might be.
        #
        # It is also one of the options the comparison above found
        # Stremio setting and this not - same libmpv-2.dll, which is the
        # only reason that comparison means anything.
        #
        # **Not verified in motion.** Judging a colour change needs the
        # picture on screen next to a reference, which is the owner's eye
        # and not a harness. The setup it addresses is measured; the
        # result of the change is not.
        **({"target_colorspace_hint": True} if sys.platform == "win32" else {}),
        # **No scaler overrides - reverted 24 August 2026, same day they
        # went in.** spline36/deband landed as the answer to "stremio
        # has better quality" and the owner's very next report was "my
        # PC lagging the monitors started freezing": his primary panel
        # is 240Hz, display-resample presents at the full 240, and two
        # spline passes plus deband per frame at 1440p on his GPU is a
        # load that starves the desktop compositor itself - both
        # monitors, not just the app. mpv's defaults are what shipped
        # before and never caused this. Re-attempt only with the GPU
        # load measured (mpv's own stats or GPU-Z) at 240Hz first, and
        # gate anything expensive on the refresh rate actually present.
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
