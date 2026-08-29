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

# Episode stills follow the real watched state. Manual episode/chapter marks are
# contiguous boundaries: marking watched/read fills everything before the
# clicked item; marking unwatched/unread clears that item and everything after.
from . import episode_watch_state_patch as _episode_watch_state_patch

_episode_watch_state_patch.install()

# Opening an episode records History but does not mark it watched. The automatic
# watched mark is written only after the playhead reaches 85% of the duration;
# end-file/source-switch events cannot force an early mark.
from . import player_watch_threshold_patch as _player_watch_threshold_patch

_player_watch_threshold_patch.install()

# Windows video presentation follows the native path used by Stremio: libmpv
# attaches to Atomic's real top-level player HWND instead of nesting inside a
# second Qt native child, and copy-safe D3D11 hardware decode keeps decoder
# surfaces from disabling VRR. This is player-only and changes no app UI motion.
from . import player_native_present_patch as _player_native_present_patch

_player_native_present_patch.install()

# The visible top-left Back arrow uses the same unwind path as Escape/mouse
# Back: close panel, close episode list, leave fullscreen, then leave player.
from . import player_back_button_patch as _player_back_button_patch

_player_back_button_patch.install()

# Global search is one mixed, scrollable Watch + Read suggestion list under the
# persistent field. Clicking a suggestion opens that title's episode/chapter
# list; Enter keeps running the full query on Discover.
from . import global_search_visual_patch as _global_search_visual_patch

_global_search_visual_patch.install()

# Polish ONLY that suggestion QListWidget: per-pixel/slower mouse wheel,
# app-theme frame, Movies-style animated accent hover, and pointing-hand cursor.
# No other page/list receives these scroll or hover overrides.
from . import global_search_list_polish_patch as _global_search_list_polish_patch

_global_search_list_polish_patch.install()

# The suggestion surface is a separate Tool window. Keep it attached to the
# main window during move/resize (including monitor changes), and make any click
# outside the actual list/search field leave global search completely.
from . import global_search_interaction_patch as _global_search_interaction_patch

_global_search_interaction_patch.install()

# A full Enter search on the combined Discover page must include Reading too.
# Configured reading-site matches stay first; MangaDex fills only the catalogue
# gaps so a title cannot falsely read as "Nothing found".
from . import discover_reading_search_patch as _discover_reading_search_patch

_discover_reading_search_patch.install()

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
# - player_callback_pacing_patch (1.10.136 A/B: no visible improvement)
# - player_windows_pacing_patch (1.10.135 A/B: no visible improvement)
# - player_process_backend_patch (discarded before test)
# - motion_patch (raster compositor)
# - poster_grid_motion_patch
# - high_refresh_live_pacing_patch
# - page_scroll_fixes_patch
# - hero_pages_live_scroll_patch
# - ultimate_scroll_patch
# - poster_grid_quick_patch
# Those are failed/older experiments and are not stacked under the current
# stable UI/player path.
