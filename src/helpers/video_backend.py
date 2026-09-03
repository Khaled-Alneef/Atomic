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

from . import logs


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
        # **No `vo` override either.** Atomic forced `vo=gpu`, and this
        # libmpv's own default is `gpu-next` - a different presentation
        # renderer, not a synonym. Measured 27 August 2026 by starting a
        # bare libmpv with no options at all beside this one and diffing
        # every presentation property:
        #
        #   current-vo   bare gpu-next   Atomic gpu
        #
        # The owner had by then run the MPV-only diagnostic, which puts
        # nothing over the video at all, and it still stuttered - so the
        # overlays are ruled out and what is left is where Atomic's mpv
        # differs from a standalone one. This was the largest of the
        # eight differences and the only one in the renderer itself.
        #
        # No note ever justified it; it predates gpu-next becoming the
        # default. Removed rather than swapped, so the renderer is
        # whatever this build of mpv would have chosen on its own.
        "hwdec": "auto-safe",
        # **No video-sync override: mpv's own default.** The owner's
        # instruction, 27 August 2026, and the honest state of the
        # evidence - "that conclusion is no longer established because
        # the user STILL sees the problem".
        #
        # What the old note here claimed, and what it actually showed,
        # had drifted apart. It was measured on a 144Hz panel where
        # 144 / 23.976 = 6.006 and display-resample locked at exactly
        # 6.000 - which is real, and is not the machine this is running
        # on any more, nor a demonstration that pans look right. The
        # judder was reported again on 240Hz with vsync-ratio reading a
        # healthy 10.01, so whatever remains is not something this
        # setting was shown to fix. Presenting it as the fix made it the
        # thing nobody re-examined.
        #
        # So the baseline is now what standalone mpv does with no
        # options: mpv picks the sync mode, nothing here overrides it,
        # and any future change has to beat that on a measurement rather
        # than inherit its place from an older one.
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
        # the file; 64MiB is the ceiling mpv itself recommends for
        # network playback and it only costs memory when it is used.
        "demuxer_max_bytes": "64MiB",
        # **Start on what is there, then read ahead** - the owner: "it
        # still does not play until it reaches 100%".
        #
        # This was 20 seconds. Twenty seconds of a 1080p stream is about
        # 12MB, which is HEAD_BYTES to within rounding - so the app's own
        # "Buffering... N%" (which counts the first HEAD_BYTES) and mpv's
        # prebuffer filled at the same rate and finished together, and
        # the picture appeared exactly as the bar hit the end. The bar
        # was never the cause; it was measuring the same thing.
        #
        # 2s is enough for the demuxer to have a decodable run and is
        # what makes the first frame land while the rest arrives.
        # `cache_pause_initial` is stated rather than left to the default
        # so that no future mpv can decide to hold the first frame back:
        # a stall *during* playback still pauses and recovers, which is
        # what cache-pause on its own does.
        "demuxer_readahead_secs": 2,
        "cache_pause_initial": False,
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


# **True from the first mpv core, for the rest of the process** - read by
# helpers.widgets to choose the scroll clock. See the note below for what
# it is compensating for and how that was measured.
core_created = False


# **Creating an mpv core costs this process two thirds of its paint rate,
# permanently, and nothing here can prevent it.** Measured 30 August 2026
# against the owner's standing report - "when I enter the player then
# leave it, the whole app scrolling becomes low on FPS".
#
# Reproduced on the real window, counting the scroll body's own paints
# during an identical wheel glide:
#
#     clean process                    135-146 paints/s
#     after one mpv core exists         45-51 paints/s
#     after that core is terminated     46-51 paints/s   (never recovers)
#
# The scroll position tracks the paints exactly - 4px steps become 11px -
# so this is the whole of what "low FPS" means here.
#
# **What it actually is, found 31 August 2026: Qt's timers, and only
# Qt's timers.** Painting is not the victim - driving `update()` in a
# tight loop gives 405/s before an mpv core and 378/s after, and
# `repaint()` 443 against 389. What collapses is timer delivery:
#
#     clean       QTimer precise 248/s   coarse 246/s   py thread 227/s
#     mpv alive   QTimer precise  93/s   coarse  96/s   py thread 226/s
#
# A plain Python thread keeps perfect time while Qt's own timers lose
# two thirds of their firings, so this is not the OS clock, not thread
# scheduling and not the GPU - it is the main thread's message loop,
# which is where Qt timers live and where a Python thread does not.
# `_Momentum` and the painted grids both advance on a QTimer, which is
# why every surface in the app slowed down together.
#
# The answer was to move the scroll clock off Qt's timers and onto the
# shared vblank ticker, which is a Python thread. That started here, as
# a switch thrown by `core_created` above; since 31 August 2026 the
# ticker is simply the default for every surface (helpers.widgets has
# the evenness table that settled it), so this flag no longer gates
# anything - it stays because "has an mpv core ever existed in this
# process" is the first question any future measurement of this will
# need to answer.
#
# What it is NOT, each eliminated by measurement rather than by argument:
#
#   * the render backend - d3d11, auto, angle, dxinterop and vo=direct3d
#     all land on 45-46/s;
#   * video at all - vo=null is 45/s;
#   * audio - ao=null is 47/s, and vo=null+ao=null is 46/s;
#   * the native child window - creating and destroying one without mpv
#     leaves the rate at 123-124/s;
#   * which window it draws into - mpv on its own top-level window is
#     still 45/s;
#   * python-mpv's event thread - start_event_thread=False is 45/s;
#   * anything in the option table below - a bare MPV(wid=...) with no
#     options at all is 48-50/s;
#   * the system timer resolution - NtQueryTimerResolution reads 0.5ms
#     before, during and after, and re-claiming timeBeginPeriod(1)
#     changes nothing;
#   * the screen's reported refresh - 240.00Hz throughout;
#   * CPU starvation - the process is idle between paints;
#   * DWM's MMCSS scheduling - DwmEnableMMCSS(FALSE) returns 0xd0000001
#     and restores nothing.
#
# So it is something libmpv's core initialisation does to the process
# that outlives the handle, and no configuration reaches it. The only
# fix that can work is to stop hosting libmpv in the UI process - a
# player child process rendering into its own window - which is an
# architecture change, not a patch. Until then this is a known cost of
# opening the player once per session, and anyone re-measuring scroll
# smoothness must do it in a process that has never opened the player,
# or the number is about this and not about their change.


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
    global core_created
    _load()
    if _mpv is None:
        raise PlayerError(_load_error or "no video engine")
    options = default_options()
    options.update(overrides)

    # **In its own process, because an mpv core in this one permanently
    # wrecks Qt's timers.** Measured: QTimer 250/s clean, 64/s once a
    # core has existed and for the rest of the session, against 226/s
    # unchanged for a plain Python thread. It is not the timer
    # resolution (NtQueryTimerResolution reports 0.500ms throughout) and
    # not power throttling (opting out changed nothing) - it is Qt's own
    # dispatcher, and nothing here can undo it. The same measurement put
    # mpv in a separate process at 250/s, 100% of baseline.
    #
    # The child renders into this window: mpv is handed `wid` and
    # parents its own window under it, which Windows allows across
    # processes on the same desktop.
    from . import mpv_proxy
    if mpv_proxy.enabled():
        # **Reached only when a fresh child cannot be started.** Until 2
        # September 2026 a pre-started child that had died (its socket
        # carried a 20s idle timeout - see mpv_proxy.serve) raised out of
        # start() and landed here, so the "fallback" put the core that
        # wrecks Qt's timers into this process in the middle of a normal
        # session - twice in the owner's log for that day, both right
        # after a stop and a continue ("the player seems does not start
        # when I stopped when I continue"). start() now checks the warm
        # child and spawns another itself, so an exception here means
        # the child genuinely cannot run, and the line says which core
        # the session is on from now on.
        try:
            handle = mpv_proxy.start(window_id, options)
            core_created = True
            return handle
        except Exception as error:
            logs.info(f"the video process could not be started ({error}); "
                      f"falling back to an in-process core for the rest "
                      f"of this session")
    else:
        logs.info("mpv in-process core requested (ATOMIC_MPV_INPROC=1)")

    try:
        handle = _mpv.MPV(wid=str(int(window_id)), **options)
    except Exception as error:
        raise PlayerError(str(error)) from error
    # See core_created below - the scroll clock changes the moment this
    # is true, and it never goes back for the life of the process.
    core_created = True
    return handle


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
