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
    old_load = Page._load_into_mpv
    old_property = Page._on_property

    # **The `_play_stream` override that used to sit here is gone, 5
    # September 2026**, and its removal is half the answer to the owner's
    # "when I changed the source from 1080P to 4K in the vid player, it
    # started the whole ep from the beginning".
    #
    # It suppressed `_prime_seat` across the whole of `_play_stream`,
    # which is the one place that tells the *torrent engine* where
    # playback is going to begin - `_play_stream` passes it into
    # `streams.prepare` as `prime`, before the torrent is created. Its
    # own comment says why it did that: to feed regression_fixes_146's
    # deferred-resume wrapper. 146 was removed wholesale by
    # regression_fixes_152, and the behaviour was reinstated on 3
    # September as helpers/player_resume_latency_patch - which suppresses
    # `_prime_seat` itself, around `old_load` alone, exactly where it
    # wants the file opened from the head.
    #
    # So this override had stopped doing its job and kept only its cost:
    # a 4K release switched to ten minutes in was created with no start
    # seconds, so the swarm was asked for the *head* of the file while
    # the seat sat in a poll waiting for a 16MB band at 10:10 that
    # nothing had asked for. Measured with a stub page driving the real
    # patch: a cold torrent is handed to mpv with `start=None` - it opens
    # at 0:00 - and the seat then needs a first frame plus the whole band
    # before it is applied, giving up silently after 180s.
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

            # Matroska/MP4 demuxers may inspect the tail while opening their
            # index. Never let that probe become Atomic's first frame or drive
            # the lower bar to EOF.
            #
            # **Not when a seat was actually asked for**, and that is the other
            # half of "it started the whole ep from the beginning". This was
            # written when local torrent playback was *always* opened from the
            # head, so "a first position far into the file" could only be a
            # probe. player_resume_latency_patch ended that on 3 September: its
            # fast path opens the file at the seat whenever the bytes there are
            # proven to exist, and `_load_into_mpv` sets `_resume_target` to the
            # seat in the same breath. The first honest time-pos of such a file
            # *is* the seat - 610.0 on a ten-minute resume - and with
            # `_position` still 0.0 from `_begin_episode` this read it as a tail
            # probe and seeked back to zero. So the player's own promise is the
            # discriminator: a seat it asked for is never forced to the head.
            asked_for_seat = getattr(self, "_resume_target", None) is not None
            near_eof = duration > 0 and pos >= max(15.0, duration * 0.95)
            far_from_head = (pos > 15.0
                             and float(getattr(self, "_position", 0.0) or 0.0) <= 0.0)
            if not asked_for_seat and (near_eof or far_from_head):
                _force_head(self)
                return None

        return old_property(self, name, value)

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
