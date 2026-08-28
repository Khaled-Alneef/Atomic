"""Atomic helper package bootstrap."""

import os

os.environ.setdefault("QT_QPA_UPDATE_IDLE_TIME", "0")

# Keep the later mixed-DPI/startup sharpness fix; it is independent of the
# scrolling cadence change below.
from . import startup_dpr_patch as _startup_dpr_patch

_startup_dpr_patch._MOVE_SETTLE_MS = 80
_startup_dpr_patch.install()

# Restore the exact scrolling method that previously tested/felt correct:
# leave the normal QWidget/PosterGrid rendering paths alone and only make the
# committed motion clock follow the active monitor's native refresh rate.
# ATOMIC_PRESENT_HZ remains the explicit diagnostic/A-B override.
from . import native_refresh_motion_patch as _native_refresh_motion_patch

_native_refresh_motion_patch.install()

# Typography/source coverage fixes are independent of scroll rendering.
from . import typography_motion_patch as _typography_motion_patch

_typography_motion_patch.install()

from . import source_coverage_patch as _source_coverage_patch

_source_coverage_patch.install()

from . import read_coverage_patch as _read_coverage_patch

_read_coverage_patch.install()

# Final identity must run after all runtime patches because some older modules
# also set APP_VERSION.
from . import development_version_patch as _development_version_patch

_development_version_patch.install()

# Intentionally NOT installed here:
# - motion_patch (raster compositor)
# - poster_grid_motion_patch
# - high_refresh_live_pacing_patch
# - page_scroll_fixes_patch
# - hero_pages_live_scroll_patch
# - ultimate_scroll_patch
# - poster_grid_quick_patch
# Those are later experiments/layers and are not part of the previously
# successful native-refresh fix.
