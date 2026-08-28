"""Atomic helper package bootstrap."""

import os

# The shared high-refresh scroll surface depends on Qt Quick's threaded scene
# graph. Qt documents this as the smoother/vsync-driven render loop when there is
# one visible QQuickWindow. Set it before importing PyQt/creating QApplication;
# an explicit diagnostic/user override still wins.
os.environ.setdefault("QSG_RENDER_LOOP", "threaded")

# Qt Quick's requestUpdate path is normally allowed to idle for about 5ms on
# desktop platforms. That is already longer than one 240 Hz refresh (4.17ms),
# so a scene-graph surface can miss the monitor cadence even when its render
# work is cheap. Set this before QApplication/QPA is created; the Quick render
# loop/vsync still throttles presentation, this only removes the extra GUI-side
# idle delay. Respect an explicit diagnostic/user override.
os.environ.setdefault("QT_QPA_UPDATE_IDLE_TIME", "0")

# PyQt6 exposes QRegion from QtGui, not QtCore. motion_patch is installed
# before helpers.widgets is imported, so provide the correct QtGui class on
# QtCore as a narrow compatibility alias for the patch's deferred import.
from PyQt6 import QtCore
from PyQt6.QtGui import QRegion

QtCore.QRegion = QRegion

# Keep the authoritative first-page DPR rebuild: this is the fix confirmed to
# make first-launch card artwork sharp on the 1080p monitor.
from . import startup_dpr_patch as _startup_dpr_patch

# The post-monitor-drag refresh is already protected by moveEvent: every move
# restarts its one-shot timer, so shortening this only changes how quickly the
# sharp-DPR rebuild starts *after* the hand stops. 80ms is fast enough to feel
# immediate while still leaving a small quiet window after the last move event.
_startup_dpr_patch._MOVE_SETTLE_MS = 80
_startup_dpr_patch.install()

# motion_patch still supplies the proven QWidget motion model and scrollbar
# integration. Its per-area Quick child is discarded by the shared native
# Flickable path for opaque main-page scrollers, leaving the live QWidget model
# as the fallback rather than another native Quick swapchain.
from . import motion_patch as _motion_patch

_motion_patch.install()

# Keep the horizontal scrollbar drag pacing and the fallback live-widget cadence
# rules. The shared vertical Flickable intercepts wheel motion before this path.
from . import high_refresh_live_pacing_patch as _high_refresh_live_pacing_patch

_high_refresh_live_pacing_patch.install()

# Movies is the known-perfect reference. Home and Discover keep their old
# page-owned compositor surfaces detached; they can use the one shared Flickable
# without bringing those per-page native children back.
from . import hero_pages_live_scroll_patch as _hero_pages_live_scroll_patch

_hero_pages_live_scroll_patch.install()

# One shared Qt Quick Flickable owns high-refresh vertical wheel motion. Python
# only handles input arrivals/final commit; Flickable owns the intermediate
# sub-pixel positions in Qt/C++.
from . import render_thread_scroll_patch as _render_thread_scroll_patch

_render_thread_scroll_patch.install()

# Keep the same compositor but soften how physical wheel notches are handed to
# it, and remove the periodic QWidget thumb wakeup during a render-thread glide.
from . import flick_input_refinement_patch as _flick_input_refinement_patch

_flick_input_refinement_patch.install()

# Do not install poster_grid_quick_patch: Discover should stay on the same
# original painted PosterGrid path as the perfect Movies page.
# Do not install the old Home/Discover hero/interaction interception patches;
# they were compensating for a page-wide snapshot that these pages no longer use.
