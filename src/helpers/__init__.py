"""Atomic helper package bootstrap.

**One finder per target module - never a second.** Python's import
machinery uses the FIRST sys.meta_path finder that returns a spec, so
when several patches each insert their own finder for the same module,
only the one inserted last ever runs and the rest silently never apply.
That is exactly what happened to windows.details: four finders
(episode_watch_state_patch, regression_fixes_154, regression_fixes_155,
development_version_patch's sharp stills) raced for it, only the newest
won, and the cascade watched/read menus the owner asked for existed in
the tree while the running app served the unpatched core methods -
found 29 August 2026 by asserting the installed method names, not by
reading the installs below, which all look correct. A new patch for an
already-finder-hooked module must chain the owning patch's `_patch`
(the windows.player patches show the shape) or be called from the
winning finder (see development_version_patch for windows.details).
"""

# **Chromium's flags, and they must be set before Qt starts.** The
# category grid renders in QtWebEngine (helpers/web_grid), and its
# compositor otherwise caps itself well below a 240Hz panel - which is
# the whole fault this move exists to leave behind. Unknown flags are
# ignored by Chromium, so this is safe on any build.
#
# QtWebEngineWidgets is imported here for its side effect: Qt requires
# it to be loaded before the QApplication exists, and importing it late
# aborts the process with a message about shared OpenGL contexts.
import os as _os

# **One flag: smooth scrolling, which QtWebEngine does not give you.**
# Measured 31 August 2026, and it is the whole of two weeks of "it jumps":
# a wheel notch in the embedded grid produced **exactly two scroll events,
# 60px apart, with nothing in between** - one discrete teleport per notch,
# no animation. A browser carries the same notch across ~200ms of eased
# frames, which is why Stremio looks like it does.
#
# It explains every number in the side-by-side: Atomic measured *more*
# even than Stremio (9% against 22%) with **zero** lurches against its 11,
# and still felt worse - because every notch being an identical single
# jump is perfectly even and perfectly steppy at the same time.
#
# Chromium has this on by default; embedders do not get it. That is the
# gap between "rendered by Chromium" and "behaves like Chromium".
#
# Nothing else is set here. An earlier version added --disable-gpu-vsync
# and --disable-frame-rate-limit on the strength of a throttled
# measurement, and unlocking presentation from the display refresh is
# what tearing is - he felt that immediately too.
# **And the animation has to run at the panel's rate.** Enabling smooth
# scrolling alone gave him after-image traces immediately, and the frame
# stamps say why: the eased notch arrived over 10 frames spaced 15.3,
# 17.2, 15.8, 17.0, 16.8ms apart - **60fps on a 240Hz display**, so every
# position is held for four refreshes and the eye sees four copies of it.
# There was nothing to smear before because there was no animation at
# all; adding one exposed the cap.
#
# --disable-frame-rate-limit lifts the compositor's own 60Hz ceiling.
# **--disable-gpu-vsync is deliberately NOT here**: an earlier build set
# both, and unlocking presentation from the refresh is what tearing is -
# he felt that one immediately too. The cap and the sync are different
# things, and only the cap is in the way.
_os.environ.setdefault(
    "QTWEBENGINE_CHROMIUM_FLAGS",
    "--enable-smooth-scrolling --disable-frame-rate-limit")
try:
    from PyQt6 import QtWebEngineWidgets as _qt_web  # noqa: F401
    # The cover scheme has to be declared before QApplication exists -
    # see helpers/web_grid.COVER_SCHEME.
    from .web_grid import register_cover_scheme as _register_covers
    _register_covers()
except Exception:
    pass            # no web engine in this build - web_grid falls back


import os

os.environ.setdefault("QT_QPA_UPDATE_IDLE_TIME", "0")

# **Every monitor is scaled to the same logical width** - see
# helpers/screen_scale, which carries the table of what was tried before
# and why each earlier answer was the wrong reading of "the same size".
# Short version: one factor for all screens makes a card the same number
# of pixels everywhere, which on the smaller panel covers more of it and
# reads as zoomed in ("the size of the 1080P now became larger"), and
# Windows' own per-monitor factors are a step function that lands 6%
# away from equal here. A factor of width/1920 per screen lays the app
# out identically on both.
#
# PassThrough, because those factors are fractional (1.3333 for a
# 2560-wide panel) and any rounding policy would quantise them straight
# back to the mismatch this removes. QT_SCALE_FACTOR is deliberately not
# set: it MULTIPLIES a live per-screen factor rather than replacing it
# (measured 31 August 2026 - 1.25 gave 1.5625 on the 2K), so it would
# undo the equality. QT_FONT_DPI likewise stays unset: it switches off
# Qt's per-screen scaling altogether, which is the one thing this needs
# to keep.
#
# All of it must happen before QGuiApplication exists, which importing
# this package before main() guarantees.
from . import screen_scale as _screen_scale

_screen_scale.apply()

try:
    from PyQt6.QtCore import Qt as _Qt
    from PyQt6.QtGui import QGuiApplication as _QGuiApp
    _QGuiApp.setHighDpiScaleFactorRoundingPolicy(
        _Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
except Exception:
    pass

# **Claim a 1ms timer for the life of the process.**
#
# Every smooth surface in this app schedules its next frame with a short
# QTimer - `poster_grid._schedule_frame` asks for 0-4ms, `_Momentum`'s
# fallback asks for one refresh - and Windows rounds a timer request up
# to the **system-global** timer resolution, 15.6ms by default. That is
# 64 frames a second on a 240Hz panel.
#
# poster_grid._hold_vblank already measured the consequence and named it
# without being able to fix it: three identical runs of the same grid
# gave 236.1, **65.2** and 235.2 steps a second - "whether this widget is
# smooth depended on whether some other process on the machine happened
# to raise the global timer resolution". Measured again 30 August 2026 on
# the owner's manga grid: **89 committed positions a second** against a
# 240Hz panel, in 5.7px steps, while the same grid dragged moved in 0.6px
# steps.
#
# It also explains two of his reports that looked unrelated. A 240Hz
# panel needs a 4.2ms timer and a 165Hz one needs 6.1ms, so the coarse
# clock costs the faster panel more - his "the 165Hz 1080P monitor is
# smoother than the 2K 240Hz". And **mpv raises this resolution itself
# while it plays and drops it when it closes**, which is why the app
# scrolled worse *after* visiting the player than before it: it was
# borrowing mpv's clock and then losing it.
#
# timeBeginPeriod is per-process since Windows 10 2004 and is what every
# browser and media player does. Released at exit for tidiness; Windows
# reclaims it on process death either way.
if os.name == "nt":
    try:
        import atexit as _atexit
        import ctypes as _ctypes

        _winmm = _ctypes.WinDLL("winmm")
        if _winmm.timeBeginPeriod(1) == 0:      # TIMERR_NOERROR
            _atexit.register(lambda: _winmm.timeEndPeriod(1))
    except Exception:
        pass

# Every Qt UI startup patch, in one module so that this package can be
# imported without PyQt6 - see helpers/_ui_startup.py. A patch failing
# for any other reason still raises, loudly, exactly as before: only a
# missing PyQt6 is swallowed, and only the Qt-free web shell can produce
# that.
try:
    from . import _ui_startup as _ui_startup      # noqa: F401
except ModuleNotFoundError as _exc:
    if not str(_exc.name or "").startswith("PyQt6"):
        raise
