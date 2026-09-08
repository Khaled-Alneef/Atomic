"""Targeted follow-up for the four regressions reported on 29 Aug.

Do not touch the established scroll equations/native-refresh path.  This module
only fixes state transitions around Reader, Player and mixed-DPI page rebuilds.
"""
from __future__ import annotations

import sys

_INSTALLED = False
_PATCHED_READER = set()
_PATCHED_PLAYER = set()
_PATCHED_TRACKER = set()


def _fix_top_bar_transition():
    """Keep the upper player bar on the same DWM-alpha path before/after frame 1.

    The previous revision changed to a QRegion mask once playback became live.
    That was the only visual-state change at first frame and produced the dark
    rectangular fragments the owner still saw.  Loading already looks correct,
    so playback now uses that exact same native child + DWM alpha treatment.
    """
    try:
        from . import player_top_bar_transparency_patch as topbar
    except Exception:
        return

    def same_surface(_player, page):
        bar = getattr(page, "top_bar", None)
        if bar is None:
            return
        try:
            bar.clearMask()
        except RuntimeError:
            pass

    topbar._clip_live_bar = same_surface


def _fix_reader(module):
    key = id(module)
    if key in _PATCHED_READER:
        return
    _PATCHED_READER.add(key)

    # The outer-edge helper changes the *top-level window geometry* to move the
    # one dead Windows pixel off-screen.  Restoring it afterwards is exactly why
    # Back still flashes/resizes: Qt and Windows briefly disagree about the
    # maximised/restore rectangle.  Do not mutate the main window at all while
    # entering Reader.  The app-wide edge-wheel relay still covers the usable
    # reader/scrollbar area; one physical screen-edge pixel is not worth changing
    # the application's window state.
    def hold_without_window_mutation(window):
        try:
            if window is not None:
                window._edge_reach_on = False
        except Exception:
            pass
        return None

    def release_without_window_mutation(window):
        # Nothing was changed on entry, therefore Back has nothing to restore.
        try:
            if window is not None:
                window._edge_reach_on = False
        except Exception:
            pass
        return None

    module._hold_edge_reach = hold_without_window_mutation
    module._release_edge_reach = release_without_window_mutation


def _fix_player(module):
    key = id(module)
    if key in _PATCHED_PLAYER:
        return
    _PATCHED_PLAYER.add(key)
    Page = module.PlayerPage
    old_snapshot = Page._startup_snapshot

    def startup_snapshot(self):
        target, text = old_snapshot(self)
        text = str(text or "")
        # Once the prepared head is complete, buffering is complete.  The old
        # meter deliberately capped that state at 99 until mpv emitted its first
        # decoded frame, so a container-open/demux delay looked like buffering
        # had frozen.  Finish the buffer meter and describe the remaining phase
        # accurately instead of parking at 99%.
        if text.startswith("Buffering... 99%") or text == "Opening video...":
            return 1.0, "Opening video..."
        return target, text

    Page._startup_snapshot = startup_snapshot


def _fix_video_startup():
    """Hand a prepared local stream to mpv with minimal initial runway."""
    try:
        from . import video_backend
    except Exception:
        return
    old = video_backend.default_options
    if getattr(old, "_atomic_144", False):
        return

    def defaults():
        options = dict(old())
        # prepare()/torrent_engine has already waited for playable head data.
        # Avoid asking mpv to build a large second runway before frame one.
        options["cache_pause_initial"] = False
        options["cache_pause_wait"] = 0.10
        options["demuxer_readahead_secs"] = 5
        options["demuxer_max_bytes"] = "32MiB"
        return options

    defaults._atomic_144 = True
    video_backend.default_options = defaults

    try:
        from . import torrent_engine
        # A missing Matroska tail index is explicitly non-fatal in await_start;
        # do not hold the first frame for an optional safety wait.  Resume keeps
        # its separate RESUME_INDEX_WAIT because it needs Cues to resolve time.
        if hasattr(torrent_engine, "INDEX_WAIT"):
            torrent_engine.INDEX_WAIT = 0.0
    except Exception:
        pass


def _fix_tracker(module):
    """Restore a live Discover result query during a DPI-triggered rebuild."""
    key = id(module)
    if key in _PATCHED_TRACKER:
        return
    _PATCHED_TRACKER.add(key)
    Page = module.TrackerPage
    old_init = Page.__init__
    old_start = Page._start_discover

    # Query state is intentionally transient in TrackerPage, but a monitor move
    # rebuilds the page object for DPR. Keep only the last *non-empty* result
    # query per tracker class; it is used only while that DPR rebuild flag is on.
    live_queries = {}

    def start_discover(self, query):
        query = str(query or "")
        if query.strip():
            live_queries[type(self).__name__] = query
        return old_start(self, query)

    def init(self, *args, **kwargs):
        old_init(self, *args, **kwargs)
        try:
            window = self.window()
            rebuilding = bool(getattr(window, "_atomic_dpr_refresh_in_progress", False))
        except RuntimeError:
            rebuilding = False
        if not rebuilding:
            return
        query = live_queries.get(type(self).__name__, "")
        if not query:
            return
        try:
            # Restore the section first.  This performs Discover's lazy build,
            # so _start_discover writes into the real result holders rather than
            # a page that is still showing its default landing state.
            setter = getattr(self, "set_active_section", None)
            if callable(setter):
                setter("discover")
            old_start(self, query)
        except (AttributeError, RuntimeError):
            pass

    Page.__init__ = init
    Page._start_discover = start_discover


def _chain_runtime_patches():
    # Reader: requested_fixes_patch owns the existing lazy import hook.  Replace
    # its patcher with a chain so ours always runs after its older restoration
    # logic, whether Reader is already loaded or imported later.
    try:
        from . import requested_fixes_patch as requested
        previous_reader = requested._patch_reader

        def reader_chain(module):
            previous_reader(module)
            _fix_reader(module)

        requested._patch_reader = reader_chain
        loaded = sys.modules.get("windows.reader")
        if loaded is not None:
            _fix_reader(loaded)

        previous_player = requested._patch_player

        def player_chain(module):
            previous_player(module)
            _fix_player(module)

        requested._patch_player = player_chain
        loaded = sys.modules.get("windows.player")
        if loaded is not None:
            _fix_player(loaded)
    except Exception:
        pass

    # Tracker already has a shared import hook in discover_reading_search_patch;
    # chain it instead of competing with a second finder for windows.tracker.
    try:
        from . import discover_reading_search_patch as discover_patch
        previous_tracker = discover_patch._patch

        def tracker_chain(module):
            previous_tracker(module)
            _fix_tracker(module)

        discover_patch._patch = tracker_chain
        loaded = sys.modules.get("windows.tracker")
        if loaded is not None:
            _fix_tracker(loaded)
    except Exception:
        pass


def install():
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True
    _fix_top_bar_transition()
    _fix_video_startup()
    _chain_runtime_patches()
