"""Atomic helper package bootstrap."""

import os

# Keep the pre-QApplication platform setup used by the current branch. It does
# not alter the 0825ee3 raster scrolling method and is retained for later fixes.
os.environ.setdefault("QT_QPA_UPDATE_IDLE_TIME", "0")

# PyQt6 exposes QRegion from QtGui, not QtCore. The 0825ee3 motion patch imports
# QRegion from QtCore, so retain this compatibility alias before widgets loads.
from PyQt6 import QtCore
from PyQt6.QtGui import QRegion

QtCore.QRegion = QRegion

# Keep the authoritative first-page DPR rebuild: later fix, independent of the
# scrolling compositor restored below.
from . import startup_dpr_patch as _startup_dpr_patch

_startup_dpr_patch._MOVE_SETTLE_MS = 80
_startup_dpr_patch.install()

# Scrolling method restored from commit 0825ee369243802147ead89b7c51cc27027261ea:
# cached DPR-correct QPixmap raster layer translated over the viewport while the
# real QWidget body is frozen. Do not layer the later Quick/live-page detach or
# requestUpdate scheduler over this; the owner identified this exact method as
# the best measured result.
from . import motion_patch as _motion_patch

_motion_patch.install()

# This is cadence-equivalent to the historical patch's native screen interval
# and retains the explicit ATOMIC_PRESENT_HZ diagnostic override.
from . import native_refresh_motion_patch as _native_refresh_motion_patch

_native_refresh_motion_patch.install()

# Later typography fix, unrelated to scroll mechanics.
from . import typography_motion_patch as _typography_motion_patch

_typography_motion_patch.install()

# Retain later non-destructive live-UI improvements: decorative tween deferral
# during motion and paced horizontal thumb dragging. Its monitor-specific wheel
# friction branch was removed in 1.10.120, so it no longer changes vertical
# wheel physics or replaces the restored raster compositor.
from . import high_refresh_live_pacing_patch as _high_refresh_live_pacing_patch

_high_refresh_live_pacing_patch.install()

# IMPORTANT: do not install hero_pages_live_scroll_patch. That later experiment
# deliberately detached Home/Tracker from _atomic_motion_surface and would turn
# off the 0825ee3 compositor exactly where it is needed.
# IMPORTANT: do not install ultimate_scroll_patch. It was a later alternative
# QWindow.requestUpdate scheduler and is intentionally superseded here.

# Later Watch/Read source fixes remain intact.
from . import source_coverage_patch as _source_coverage_patch

_source_coverage_patch.install()

from . import read_coverage_patch as _read_coverage_patch

_read_coverage_patch.install()

# Do not install poster_grid_quick_patch or the old interaction interception
# experiments. The restored raster compositor is the single vertical scroll
# rendering path for ordinary Atomic scroll_area() pages.
