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

# Keep that exact native-refresh method, but do not let an ordinary QScrollArea
# compute several integer positions before Qt has painted the previous one. This
# is a presentation acknowledgement only; it does not add a compositor, change
# wheel physics, or replace the normal QWidget/PosterGrid render paths.
from . import scroll_presentation_patch as _scroll_presentation_patch

_scroll_presentation_patch.install()

# Episode-list spoiler art follows the real watched state, and single
# out-of-order watched marks stay exact instead of manufacturing contiguous
# progress for the episode before them.
from . import episode_watch_state_patch as _episode_watch_state_patch

_episode_watch_state_patch.install()

# Opening an episode records History but does not mark it watched. The automatic
# watched mark is written only after the playhead reaches 85% of the duration;
# end-file/source-switch events cannot force an early mark.
from . import player_watch_threshold_patch as _player_watch_threshold_patch

_player_watch_threshold_patch.install()

# Global search suggestions use large thumbnail-left rows. Remote Discover
# posters are filled asynchronously through the existing cover queue/cache so
# typing stays immediate.
from . import global_search_visual_patch as _global_search_visual_patch

_global_search_visual_patch.install()

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
