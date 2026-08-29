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

# The visible top-left Back arrow leaves the player directly when the app is in
# fullscreen. Outside fullscreen it keeps the normal panel/episode unwind path.
from . import player_back_button_patch as _player_back_button_patch

_player_back_button_patch.install()

# Restore the player's old upper-bar surface while removing only the gray-like
# resting frames behind its labels and buttons.
from . import player_top_bar_transparency_patch as _player_top_bar_transparency_patch

_player_top_bar_transparency_patch.install()

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
# Configured reading providers stay as separate cards; MangaDex fills only
# catalogue gaps so a title cannot falsely read as "Nothing found".
from . import discover_reading_search_patch as _discover_reading_search_patch

_discover_reading_search_patch.install()

# Typography/source coverage fixes are independent of scroll rendering.
from . import typography_motion_patch as _typography_motion_patch

_typography_motion_patch.install()

from . import source_coverage_patch as _source_coverage_patch

_source_coverage_patch.install()

from . import read_coverage_patch as _read_coverage_patch

_read_coverage_patch.install()

# The requested 29-Aug fixes are deliberately installed after typography and
# source coverage: they soften only 2K+ UI text, cap the enlarged source waits,
# remove the Settings Picture copy, fix reader chapter ordering/Continue/back,
# shorten player initial buffering, and re-arm (not rewrite) scrolling after
# immersive reader/player teardown.
from . import requested_fixes_patch as _requested_fixes_patch

_requested_fixes_patch.install()

# Follow-up for the four remaining regressions: Reader Back no longer mutates
# top-level geometry, Player's upper bar keeps one visual path across frame 1,
# prepared streams do not sit at a fake 99% buffer stage, and a Discover result
# query survives a mixed-DPI monitor rebuild. Scroll physics stay untouched.
from . import regression_fixes_144 as _regression_fixes_144

_regression_fixes_144.install()

# 1.10.145 follow-up: actually apply the live top-bar clip at first frame,
# remove resume-only startup waits, keep Settings Cancel from rebuilding the
# page behind it, and give Skip Intro / Next Episode the same deep-teal action
# colours as Continue Watching. No size or scroll changes.
from . import regression_fixes_145 as _regression_fixes_145

_regression_fixes_145.install()

# 1.10.146: the clipped native-child top bar still left one dark rectangle per
# control. Promote only that bar to a true per-pixel-alpha owned overlay. Resume
# now opens the playable head immediately and jumps to the saved seat only when
# that exact range exists, instead of blocking first picture on start=<seat>.
from . import regression_fixes_146 as _regression_fixes_146

_regression_fixes_146.install()

# 1.10.147 retains its safe stale-resume cleanup and actual-duration EOF guard.
# Its strict startup-priority override is deliberately undone by 1.10.149 below.
from . import regression_fixes_147 as _regression_fixes_147

_regression_fixes_147.install()

# 1.10.148: a click on the video remains a click while the pointer is moving,
# using the down-position rather than the release-position. Also stabilise the
# promoted transparent top bar: correct global hit geometry, no raise-on-move
# storm, arrow cursor on inert title/source labels, and stable pill hover paint.
from . import regression_fixes_148 as _regression_fixes_148

_regression_fixes_148.install()

# 1.10.149: restore the known-good pre-147 torrent opening policy (verified
# against 1.10.133). Reader/index bands remain urgent, but the rest of the
# selected episode stays fetchable at priority 1 so an unexpected mpv Range
# request cannot deadlock first-frame startup behind a priority-0 piece.
from . import regression_fixes_149 as _regression_fixes_149

_regression_fixes_149.install()

# Final identity must run after all runtime patches because some older modules
# also set APP_VERSION.
from . import development_version_patch as _development_version_patch

_development_version_patch.install()

# Intentionally NOT installed here:
# - player_present_queue_patch (1.10.140 A/B: no visible improvement)
# - player_native_present_patch (1.10.139 A/B: no visible improvement)
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