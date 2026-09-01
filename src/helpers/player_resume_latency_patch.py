"""Start the picture as soon as data arrives, and take the seat after.

**The owner's report, 30 August 2026:** *"when source reaches buffering
99% then the skipping to .... appears, it does not play the video until
it reaches 100%, it is supposed so start when it starts loading form
0.5% not 100%!!"*

Measured against the real `streams.prepare` with a local seeder throttled
to a realistic rate (217MB file, 2MB pieces, resuming 4:00 in):

    seed 1.5MB/s   url handed over at 11.5s, gauge at 99%, **no picture
                   in 90s**, mpv parked on the seek target it never
                   decoded
    seed 5.0MB/s   url at 9.4s, the seat's own piece arrived at 31.6s,
                   and mpv had by then fallen back to 0:04 and stayed

Two things were wrong and both are fixed elsewhere in this pass (see
torrent_engine._apply_windows): the container index was fetched last of
the tail pieces although `await_start` gates on exactly that piece, and
a resume spent its opening bandwidth on 12MB of head nobody was going to
watch. What is left, and what this module owns, is the shape of the
handoff itself.

`_load_into_mpv` opens the file *at* the seat (`loadfile ... start=`).
That is only sound when the bytes at the seat already exist: when they do
not, mpv blocks on a stream read tens of seconds long, gives up, and
restarts at the head - which is the 0:04 above, under a "Skipping to
4:00..." message that can never come true. Waiting for the band before
handing over would fix the correctness and make the wait *longer*, which
is the opposite of what was asked for.

So the file is opened from the head, unseated, the moment the engine has
anything: a picture appears while the swarm is still filling. The seat is
applied afterwards, once the piece holding it is actually on disk and a
real frame has been drawn - `_seek_absolute(resuming=True)`, the same
path a manual skip uses. A seat that never becomes reachable now costs
the viewer the opening of the episode rather than a black screen.

This is the shape regression_fixes_146 introduced and regression_fixes_152
removed wholesale while restoring the 1.10.139 top bar; it is reinstated
here on its own, after that restore, so the playback fix and the bar fix
stop being one lever.
"""

from __future__ import annotations

import sys
import time

_INSTALLED = False
_PATCHED = set()

# How often the seat poll asks whether the target's data has landed.
POLL_MS = 120

# Give up applying a seat after this long and simply keep playing from
# where the picture already is. Deliberately generous - the viewer is
# watching real video throughout, so a late seek costs nothing but a
# jump - and bounded, so a torrent that never reaches the offset does not
# leave a timer running for the life of the page.
SEAT_GIVE_UP_S = 180.0


def _patch(module):
    key = ("resume-latency", id(module))
    if key in _PATCHED:
        return
    _PATCHED.add(key)

    from PyQt6.QtCore import QTimer

    Page = module.PlayerPage
    old_load = Page._load_into_mpv
    old_close = Page.close_player

    def _stop_poll(self):
        timer = getattr(self, "_atomic_seat_timer", None)
        if timer is not None:
            try:
                timer.stop()
            except RuntimeError:
                pass
        self._atomic_seat_job = None

    def _seat_data_ready(self, stream, seat):
        """Is there enough contiguous data at the seat to play from it?

        **The whole armed band, not just the piece the offset falls in.**
        Measured 30 August 2026: seeking as soon as that single piece
        existed put mpv at the target for ~25s without decoding, then
        dropped it back to the edge of what it had already buffered
        (52.3s) where it froze - one piece is 2MB, a couple of seconds of
        video, and the demuxer needs a run past it before anything plays.
        `_start_pieces()` is exactly the band `arm_start_band` primed
        (RESUME_BAND_BYTES, 16MB) minus whatever has landed, so an empty
        list is the honest "the seek can succeed now".

        A direct URL (debrid, an addon) has no engine behind it and
        nothing to wait for, so it is ready by definition."""
        info_hash = str((stream or {}).get("info_hash") or "").strip().lower()
        if not info_hash:
            return True
        try:
            from helpers import torrent_engine
        except Exception:
            return True
        torrent = getattr(torrent_engine, "_torrents", {}).get(info_hash)
        if torrent is None:
            return True
        try:
            if getattr(torrent, "start_offset", None) is None:
                # The Cues have not been read yet, so the seat is not a
                # byte yet. arm_start_band owns its own retry; this only
                # asks again next tick.
                return False
            return not torrent._start_pieces()
        except Exception:
            return False

    def _begin_seat_poll(self, stream, seat):
        _stop_poll(self)
        job = {"run": self._run, "seat": float(seat),
               "stream": dict(stream or {}),
               "deadline": time.monotonic() + SEAT_GIVE_UP_S}
        self._atomic_seat_job = job

        # Prime the engine's resume band once, here, and never from the
        # poll: arm_start_band starts its own background retry when the
        # index is not readable yet, and calling it four times a second
        # would race several of those against one another.
        info_hash = str(job["stream"].get("info_hash") or "").strip().lower()
        if info_hash:
            try:
                from helpers import torrent_engine
                torrent_engine.set_start_seconds(info_hash, job["seat"])
                torrent_engine.arm_start_band(info_hash)
            except Exception:
                pass

        timer = getattr(self, "_atomic_seat_timer", None)
        if timer is None:
            timer = QTimer(self)
            timer.setInterval(POLL_MS)
            self._atomic_seat_timer = timer

            def poll():
                current = getattr(self, "_atomic_seat_job", None)
                if not current:
                    timer.stop()
                    return
                if getattr(self, "_closing", False) or current["run"] != self._run:
                    _stop_poll(self)
                    return
                # **A real frame first, always.** This is the whole point
                # of the module: nothing may seek before the viewer has a
                # picture, because a seek issued into an opening file is
                # the race that used to leave "Skipping to..." on screen
                # for good.
                if getattr(self, "_awaiting_first_frame", True):
                    return
                target = float(current["seat"])
                position = float(getattr(self, "_position", 0.0) or 0.0)
                if position >= target - module.RESUME_LANDED_TOLERANCE_S:
                    _stop_poll(self)      # playback got there on its own
                    return
                if time.monotonic() > current["deadline"]:
                    _stop_poll(self)
                    return
                if not _seat_data_ready(self, current["stream"], target):
                    return

                _stop_poll(self)
                # From here it is the ordinary skip path, and it owns the
                # promise/watchdog bookkeeping (_seat_timer, _resume_target)
                # exactly as a manual jump does.
                self._resume_target = target
                self._seat_reported = None
                self._seat_deadline = time.monotonic() + module.SEAT_GIVE_UP_S
                self._seat_timer.start()
                if getattr(self, "_work_toast", None) is not None:
                    self._say_working(
                        f"Skipping To {module._format_time(target)}...")
                self._seek_absolute(target, resuming=True)
                # **mpv can come back from this seek paused, and then
                # nothing restarts it.** Measured 30 August 2026: after
                # a seek into a torrent it had to wait on, mpv settled
                # with a full demuxer cache, `paused-for-cache` false and
                # `pause` *true* - a frozen picture with the data for it
                # already in hand. Only asserted when the viewer has not
                # paused deliberately, which `_paused` tracks.
                if not getattr(self, "_paused", False):
                    try:
                        self.handle["pause"] = False
                    except Exception:
                        pass

            timer.timeout.connect(poll)
        timer.start()

    def load_into_mpv(self, stream, resume_at=None):
        try:
            seat = self._prime_seat(resume_at)[0]
        except Exception:
            seat = None
        if not seat:
            _stop_poll(self)
            return old_load(self, stream, resume_at)

        # `old_load` reads the seat back out of `_prime_seat` to decide
        # whether to open with mpv's blocking `start=`. Suppress that one
        # read so the file opens from the head; the engine already has
        # the real seat (streams.prepare passed it to torrent_engine.add)
        # and the poll below keeps it primed.
        sentinel = object()
        prior = self.__dict__.get("_prime_seat", sentinel)
        self._prime_seat = lambda _resume_at=None: (None, None)
        try:
            result = old_load(self, stream, resume_at)
        finally:
            if prior is sentinel:
                self.__dict__.pop("_prime_seat", None)
            else:
                self.__dict__["_prime_seat"] = prior

        _begin_seat_poll(self, stream, seat)
        return result

    def close_player(self):
        _stop_poll(self)
        return old_close(self)

    Page._load_into_mpv = load_into_mpv
    Page.close_player = close_player


def install():
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    # Chained onto the shared windows.player hook rather than given a
    # meta-path finder of its own - see helpers/__init__.py for what a
    # second finder on one module silently does. This must land *after*
    # regression_fixes_152's restore of the core startup methods, which
    # is why development_version_patch installs it last.
    try:
        from . import requested_fixes_patch as requested
        previous = requested._patch_player

        def chained(module):
            previous(module)
            _patch(module)

        requested._patch_player = chained
        loaded = sys.modules.get("windows.player")
        if loaded is not None:
            _patch(loaded)
    except Exception:
        pass
