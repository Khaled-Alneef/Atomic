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
# real QWidget body is frozen.
from . import motion_patch as _motion_patch

_motion_patch.install()

# Preserve native per-monitor cadence and explicit diagnostic override.
from . import native_refresh_motion_patch as _native_refresh_motion_patch

_native_refresh_motion_patch.install()

# Later typography fix, unrelated to scroll mechanics.
from . import typography_motion_patch as _typography_motion_patch

_typography_motion_patch.install()

# Retain later non-destructive live-UI improvements. Its monitor-specific wheel
# friction branch was removed in 1.10.120.
from . import high_refresh_live_pacing_patch as _high_refresh_live_pacing_patch

_high_refresh_live_pacing_patch.install()

# Preserve the later Home/Main, Discover, History and Schedule fixes WITHOUT the
# old Qt Quick compositor-detach behavior. This module keeps hero prewarming,
# deferred artwork/poster swaps and lazy Tracker-tab handling while leaving the
# 0825ee3 raster _atomic_motion_surface intact.
from . import page_scroll_fixes_patch as _page_scroll_fixes_patch

_page_scroll_fixes_patch.install()

# Do not install hero_pages_live_scroll_patch: its compositor-detach half would
# remove the restored 0825ee3 raster surface. Do not install ultimate_scroll_patch
# either; its QWindow.requestUpdate scheduler is the later alternative path.

# Later Watch/Read source fixes remain intact.
from . import source_coverage_patch as _source_coverage_patch

_source_coverage_patch.install()

from . import read_coverage_patch as _read_coverage_patch

_read_coverage_patch.install()

# Final identity must run after all runtime patches because some older patch
# modules also set APP_VERSION.
from . import development_version_patch as _development_version_patch

_development_version_patch.install()

# Do not install poster_grid_quick_patch or the old interaction interception
# experiments. Raster motion remains the authoritative vertical scrolling path.
