"""Critical player startup recovery.

The player must enter normal playback as soon as Atomic's torrent preparation
has produced the opening/index data. Resume fetching and mpv's own cache must
not become a second startup gate.

This patch does three narrow things:

* a saved resume point is NOT passed into torrent preparation; startup prepares
  the head/index only. The existing deferred-resume path may arm the saved seat
  after a real first frame.
* Atomic's own localhost torrent URL is opened with mpv's cache/readahead
  disabled. The local HTTP range server is already the streaming buffer and
  blocks only on the exact pieces mpv asks for.
* a large/near-EOF time-pos published while frame one is still owed is treated
  as a container/index probe, not playback. It is ignored and mpv is sent back
  to 0 instead of letting the seek bar/final frame become the player state.

Direct HTTP/debrid streams are unchanged. UI/scrolling is untouched.
"""
from __future__ import annotations

import sys

_INSTALLED = False
_PATCHED = set()


def _patch_player(module):
    key = ("player151", id(module))
    if key in _PATCHED:
        return
    _PATCHED.add(key)

    Page = module.PlayerPage
    old_play_stream = Page._play_stream
    old_load = Page._load_into_mpv
    old_property = Page._on_property

    def play_stream(self, index, resume_at=None, solo=False):
        """Do not let a saved seat participate in first-frame preparation."""
        # _play_stream computes _prime_seat synchronously before it spawns the
        # prepare worker. Suppress only that one computation. Once the worker
        # returns, regression_fixes_146's deferred-resume wrapper sees the real
        # method again and can remember/arm the seat after picture one.
        sentinel = object()
        prior = self.__dict__.get("_prime_seat", sentinel)
        self._prime_seat = lambda _resume_at=None: (None, None)
        try:
            return old_play_stream(self, index, resume_at=resume_at, solo=solo)
        finally:
            if prior is sentinel:
                try:
                    del self.__dict__["_prime_seat"]
                except KeyError:
                    pass
            else:
                self.__dict__["_prime_seat"] = prior

    def load_into_mpv(self, stream, resume_at=None):
        """For Atomic localhost torrents, the HTTP range server is the cache."""
        url = str((stream or {}).get("url") or "")
        local_torrent = ("127.0.0.1" in url
                         and bool((stream or {}).get("info_hash")))
        if local_torrent and getattr(self, "handle", None) is not None:
            try:
                # Do not ask mpv to fill another cache after Atomic has already
                # buffered startup. Reads now flow straight to the range server,
                # which waits only for the exact requested torrent pieces.
                self.handle["cache"] = "no"
            except Exception:
                pass
            try:
                self.handle["demuxer-readahead-secs"] = 0
            except Exception:
                pass
            # Marker used only by the startup time-pos guard below.
            self._atomic_local_torrent_151 = True
        else:
            self._atomic_local_torrent_151 = False
        return old_load(self, stream, resume_at)

    def _force_head(self):
        handle = getattr(self, "handle", None)
        if handle is None:
            return
        try:
            if hasattr(handle, "command_async"):
                handle.command_async("seek", 0, "absolute+exact")
                handle.command_async("set", "pause", "no")
            else:  # pragma: no cover
                handle.seek(0, reference="absolute", precision="exact")
                handle["pause"] = False
        except Exception:
            pass

    def on_property(self, name, value):
        if (name == "time-pos" and value is not None
                and getattr(self, "_awaiting_first_frame", False)
                and getattr(self, "_atomic_local_torrent_151", False)):
            try:
                pos = float(value)
            except (TypeError, ValueError):
                pos = 0.0
            try:
                duration = float(getattr(self, "_duration", 0.0) or 0.0)
            except (TypeError, ValueError):
                duration = 0.0

            # Local torrent playback is deliberately opened from the head now;
            # therefore a first reported position far into the file cannot be a
            # legitimate resume. Matroska/MP4 demuxers may inspect the tail while
            # opening their index. Never let that probe become Atomic's first
            # frame or drive the lower bar to EOF.
            near_eof = duration > 0 and pos >= max(15.0, duration * 0.95)
            far_from_head = pos > 15.0 and float(getattr(self, "_position", 0.0) or 0.0) <= 0.0
            if near_eof or far_from_head:
                _force_head(self)
                return None

        return old_property(self, name, value)

    Page._play_stream = play_stream
    Page._load_into_mpv = load_into_mpv
    Page._on_property = on_property


def _chain_player_patch():
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
    _chain_player_patch()
