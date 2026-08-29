"""Restore the pre-mpv-0.41 three-frame Windows presentation queue.

Atomic ships mpv 0.41, whose default swapchain depth was reduced from 3 to 2.
The player's recorded pan shows held presentation samples followed by catch-up
jumps. mpv documents swapchain depth as the in-flight frame limit and notes
that a larger depth can improve pipelining and prevent missed vsyncs.

This patch changes only the Windows player creation option. It does not change
interpolation, video-sync mode, playback speed, hardware decoding, renderer,
source selection, or any app UI/scroll motion.
"""

from __future__ import annotations

import os

_INSTALLED = False
_PATCHED = False
DEPTH = 3


def _patch_backend() -> None:
    global _PATCHED
    if _PATCHED or os.name != "nt":
        return
    _PATCHED = True

    from . import video_backend

    original_create = video_backend.create

    def create(window_id: int, **overrides):
        # Restore mpv's pre-0.41 presentation slack. Caller-supplied overrides
        # remain authoritative for diagnostics/A-B tests.
        tuned = {"swapchain_depth": DEPTH}
        tuned.update(overrides)
        return original_create(window_id, **tuned)

    video_backend.create = create


def install() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True
    _patch_backend()
