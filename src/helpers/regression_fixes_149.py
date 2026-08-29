"""Restore the known-good pre-1.10.147 torrent open policy.

The 1.10.147 startup lock tried to make first-frame startup faster by replacing
Torrent._apply_windows' normal selected-file priorities with a second, much
stricter array: only a few guessed reader/index pieces stayed wanted and almost
every other piece of the episode became priority 0 until the first time-pos.

That creates a circular failure when libavformat/mpv asks for any additional
piece while opening: the first frame cannot appear until that read completes,
but the lock is released only after the first frame appears.  The visible
result is the player sitting forever at "Opening video..." while the swarm is
healthy.

1.10.133 used the engine's normal policy: current reader/index pieces are urgent,
while every other piece of the selected episode remains fetchable at low
priority 1.  Restore exactly that _apply_windows implementation here.  Keep the
later safe pieces of 1.10.147 (stale resume-state clearing and actual-duration
EOF guard) and keep 1.10.146's deferred resume path.

No UI, scrolling, source ranking, chapter, or playback-presentation code changes.
"""
from __future__ import annotations

_INSTALLED = False


def install():
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    try:
        from . import torrent_engine as engine
    except Exception:
        return

    Torrent = getattr(engine, "_Torrent", None)
    if Torrent is None:
        return

    current = getattr(Torrent, "_apply_windows", None)
    original = getattr(current, "_atomic_original", None)
    if original is None:
        # Either 1.10.147 was not installed or the engine already has its normal
        # implementation.  Nothing to undo.
        return

    # This is the implementation that 1.10.147 wrapped.  It is also the same
    # selected-file priority policy present in the known-good 1.10.133 tree:
    # urgent reader/index bands at high priority, remainder of THIS episode at
    # priority 1, other files in a pack still at 0.
    Torrent._apply_windows = original

    # Existing torrent objects created before this module installed are rare
    # during normal startup, but clearing the marker makes the state truthful
    # and prevents any later helper from believing the strict lock is active.
    try:
        for torrent in list(getattr(engine, "_torrents", {}).values()):
            torrent._atomic_startup_lock_147 = False
    except Exception:
        pass
