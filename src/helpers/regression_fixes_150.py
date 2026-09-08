"""Play immediately once Atomic has finished preparing/buffering the source.

Atomic's torrent preparation is already the startup buffer. 1.10.149 restored
mpv's default initial cache-pause behaviour while rolling back an unrelated
startup regression, which reintroduced a second wait after Atomic's own
buffering had completed. This patch removes only that duplicate gate.

No torrent priorities, source selection, scrolling, UI layout, reader logic or
normal mid-playback rebuffering behaviour is changed.
"""
from __future__ import annotations

import sys

_INSTALLED = False
_PATCHED = set()


def _patch_video_backend():
    try:
        from . import video_backend
    except Exception:
        return

    current = video_backend.default_options
    if getattr(current, "_atomic_150", False):
        return

    def defaults():
        options = dict(current())
        # Atomic/its torrent engine has already waited for playable startup
        # data before the local URL is handed to mpv. Do not make mpv build a
        # second initial cache runway before decoding frame one.
        options["cache_pause_initial"] = False
        # If mpv does enter a cache pause later, resume as soon as data is
        # available instead of requiring an extra startup-sized runway.
        options["cache_pause_wait"] = 0.0
        return options

    defaults._atomic_150 = True
    defaults._atomic_original = current
    video_backend.default_options = defaults


def _patch_player(module):
    key = ("player", id(module))
    if key in _PATCHED:
        return
    _PATCHED.add(key)

    Page = module.PlayerPage
    old_property = Page._on_property

    def on_property(self, name, value):
        result = old_property(self, name, value)

        # cache-buffering-state=100 is mpv's own statement that the current
        # cache wait is complete. During first-frame startup there is no reason
        # to remain paused after that point: Atomic has already completed its
        # source preparation stage. Reassert play asynchronously so this never
        # blocks Qt behind mpv's core.
        if (name == "cache-buffering-state"
                and int(value or 0) >= 100
                and getattr(self, "_awaiting_first_frame", False)
                and getattr(self, "handle", None) is not None):
            try:
                handle = self.handle
                if hasattr(handle, "command_async"):
                    handle.command_async("set", "pause", "no")
                else:  # pragma: no cover
                    handle["pause"] = False
            except Exception:
                pass
        return result

    Page._on_property = on_property


def _chain_player_patch():
    # requested_fixes_patch owns the lazy windows.player hook. Chain after the
    # existing patches so this is the final startup-buffer behaviour.
    try:
        from . import requested_fixes_patch as requested
        previous = requested._patch_player

        def chained(module):
            previous(module)
            _patch_player(module)

        requested._patch_player = chained
        loaded = sys.modules.get("windows.player")
        if loaded is not None:
            _patch_player(loaded)
    except Exception:
        pass


def install():
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True
    _patch_video_backend()
    _chain_player_patch()
