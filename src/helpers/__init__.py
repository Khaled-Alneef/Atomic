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

# IMPORTANT: do not install poster_grid_quick_patch here. Movies currently use
# PosterGrid's original painted motion path and are the owner's perfect control.
# Discover must use that exact same path rather than a special small-grid Quick
# compositor that made its card motion differ from Movies.

# Keep hero artwork/fade work out of active wheel frames.
from . import hero_scroll_patch as _hero_scroll_patch

_hero_scroll_patch.install()

# Installed after hero_scroll_patch so its PageSlide handoff is authoritative:
# first-wheel routing goes straight to the visible vertical scroll owner, and a
# horizontal scrollbar press reveals the live nested row before dragging it.
from . import home_discover_interaction_patch as _home_discover_interaction_patch

_home_discover_interaction_patch.install()
