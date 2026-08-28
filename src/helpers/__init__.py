"""Atomic helper package bootstrap."""

import os

os.environ.setdefault("QT_QPA_UPDATE_IDLE_TIME", "0")

# PyQt6 exposes QRegion from QtGui, not QtCore. The restored 0825ee3 motion
# patch imports it through QtCore, so retain the narrow compatibility alias.
from PyQt6 import QtCore
from PyQt6.QtGui import QRegion

QtCore.QRegion = QRegion

# Keep the later mixed-DPI/startup sharpness fix.
from . import startup_dpr_patch as _startup_dpr_patch

_startup_dpr_patch._MOVE_SETTLE_MS = 80
_startup_dpr_patch.install()

# Authoritative vertical scroll renderer: the 0825ee3 raster compositor, now
# with the later proven full-page/large-runway cache policy restored. During a
# glide the live child-widget tree is frozen and one rasterized page surface is
# translated over the viewport.
from . import motion_patch as _motion_patch

_motion_patch.install()

# Restore the painted-card precision that was present during the successful
# period. PosterGrid owns its cards in one paint surface; this prevents its
# final viewport conversion from throwing away fractional Y motion.
from . import poster_grid_motion_patch as _poster_grid_motion_patch

_poster_grid_motion_patch.install()

# Preserve native per-monitor cadence and explicit diagnostic override.
from . import native_refresh_motion_patch as _native_refresh_motion_patch

_native_refresh_motion_patch.install()

# Later typography fix, unrelated to scroll mechanics.
from . import typography_motion_patch as _typography_motion_patch

_typography_motion_patch.install()

# Retain non-destructive live-UI improvements. The old monitor-specific vertical
# wheel friction branch has already been removed.
from . import high_refresh_live_pacing_patch as _high_refresh_live_pacing_patch

_high_refresh_live_pacing_patch.install()

# Keep Home/Main hero work, Discover poster coalescing and lazy History/Schedule
# tab hooks without detaching the raster motion surface.
from . import page_scroll_fixes_patch as _page_scroll_fixes_patch

_page_scroll_fixes_patch.install()

# Do not install hero_pages_live_scroll_patch: its compositor-detach half would
# remove the one-surface raster path. Do not install ultimate_scroll_patch or
# poster_grid_quick_patch either; those are later alternative render paths.

# Later Watch/Read source fixes remain intact.
from . import source_coverage_patch as _source_coverage_patch

_source_coverage_patch.install()

from . import read_coverage_patch as _read_coverage_patch

_read_coverage_patch.install()

# Final identity must run after all runtime patches because some older modules
# also set APP_VERSION.
from . import development_version_patch as _development_version_patch

_development_version_patch.install()
