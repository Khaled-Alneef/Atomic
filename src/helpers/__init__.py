"""Atomic helper package bootstrap."""

import os

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

_startup_dpr_patch.install()

# Native-refresh Qt Quick compositor for ordinary QScrollArea pages.
from . import motion_patch as _motion_patch

_motion_patch.install()

# Keep expensive live QWidget motion phase-locked on 200+ Hz displays and give
# SideScroller horizontal thumbs the same paced drag model as vertical bars.
# The Quick compositor's decoupled 240 Hz path remains native-refresh.
from . import high_refresh_live_pacing_patch as _high_refresh_live_pacing_patch

_high_refresh_live_pacing_patch.install()

# Movies is the known-perfect reference. Home and Discover contain a HeroBanner
# plus nested horizontal scrollers, so a page-wide snapshot is the wrong owner
# for their motion: it can cover the live rows and makes the first wheel event
# pay for a large synchronous page render. Detach only those two pages from the
# compositor after construction; every other page keeps the GPU path.
from . import hero_pages_live_scroll_patch as _hero_pages_live_scroll_patch

_hero_pages_live_scroll_patch.install()

# Do not install poster_grid_quick_patch: Discover should stay on the same
# original painted PosterGrid path as the perfect Movies page.
# Do not install the old Home/Discover hero/interaction interception patches;
# they were compensating for a page-wide snapshot that these pages no longer use.
