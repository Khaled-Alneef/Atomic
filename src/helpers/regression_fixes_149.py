"""Restore the known-good pre-regression player startup path.

The owner reported that the persistent "Opening video..." stall did not exist
roughly fifteen versions ago. Repository history confirms the important fact:
1.10.133 and current development have byte-identical windows/player.py and
helpers/torrent_engine.py blobs. The regression is therefore in the runtime
patch stack added later, not in those core files.

This module reverses ONLY the startup/handoff overrides introduced by the later
29-Aug follow-ups while keeping their unrelated fixes:

* mpv gets the 1.10.133 cache/readahead options again;
* the engine again gets its small concurrent container-index safety wait before
  mpv opens the local HTTP stream instead of handing the URL over with the
  critical tail explicitly allowed to be missing;
* 1.10.147's second, stricter piece-priority pass is removed. The core engine's
  own warming policy already narrows the swarm until piece zero arrives, then
  deliberately widens it enough to retain useful peers while libavformat opens
  the container. The later strict pass overrode that measured policy until the
  first decoded frame, creating a circular first-frame dependency.

The newer deferred-resume/EOF guard stays in place: resume bytes still do not
compete with frame one, and a stale near-EOF resume still cannot park playback
on the final second. No scrolling, UI, Reader, Discover, chapter or Settings
behaviour is touched.
"""
from __future__ import annotations

_INSTALLED = False

# Core value present in the known-good 1.10.133 torrent_engine.py.
_INDEX_WAIT_S = 3.0


def _restore_mpv_startup_defaults():
    """Make the final option dict match the known-good 1.10.133 startup."""
    try:
        from . import video_backend
    except Exception:
        return

    current = video_backend.default_options
    if getattr(current, "_atomic_149", False):
        return

    def defaults():
        options = dict(current())
        # 1.10.133 did not override either cache-pause option. Removing them
        # lets this libmpv use the same defaults it used before the regression.
        options.pop("cache_pause_initial", None)
        options.pop("cache_pause_wait", None)
        # Exact core values from video_backend.default_options in 1.10.133.
        # 1.10.144/145 had progressively cut them to 32/16MiB and 5/2 seconds.
        options["demuxer_max_bytes"] = "64MiB"
        options["demuxer_readahead_secs"] = 20
        return options

    defaults._atomic_149 = True
    defaults._atomic_original = current
    video_backend.default_options = defaults


def _restore_torrent_handoff():
    """Undo only the post-1.10.133 first-frame priority/wait overrides."""
    try:
        from . import torrent_engine as engine
    except Exception:
        return

    # 1.10.144 and 1.10.145 forced this to zero. Core await_start waits for
    # head and critical tail CONCURRENTLY, so this is not a fixed 3-second
    # sleep. It only prevents handing mpv a container whose exact opening/index
    # bytes are known to be missing. Missing index is still fail-soft when the
    # small budget expires, exactly as in the core implementation.
    engine.INDEX_WAIT = _INDEX_WAIT_S

    # Resume is now deferred until after frame one by 1.10.146/147, so it needs
    # the same container-open safety window, not the old six-second
    # resume-specific wait and not the zero-wait regression. The actual resume
    # band remains asynchronous and does not gate the initial picture.
    engine.RESUME_INDEX_WAIT = _INDEX_WAIT_S

    Torrent = engine._Torrent
    current_apply = Torrent._apply_windows
    if getattr(current_apply, "_atomic_147", False):
        original = getattr(current_apply, "_atomic_original", None)
        if callable(original):
            # Decisive rollback: restore the core implementation that 1.10.133
            # used. It has its own warming narrow window and then widens once
            # piece zero exists. 1.10.147 called it and immediately overwrote
            # its priorities with an all-zero-outside-urgent-bands second pass
            # until a decoded frame appeared.
            Torrent._apply_windows = original

    # Existing torrent objects can survive between source/episode attempts.
    # Clear only the obsolete strict-priority marker and reapply the normal
    # windows. Stale resume-byte cleanup from 1.10.147 remains installed.
    for torrent in list(getattr(engine, "_torrents", {}).values()):
        try:
            torrent._atomic_startup_lock_147 = False
            if torrent.file_index is not None and not torrent.want_whole:
                torrent.refresh_windows()
        except Exception:
            pass


def install():
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True
    _restore_mpv_startup_defaults()
    _restore_torrent_handoff()
