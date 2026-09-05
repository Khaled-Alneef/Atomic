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

# How long a file opened *at* the seat has to produce a picture before
# it is re-opened from the head. Generous enough for a slow host to
# answer a range request and short enough that a viewer is not staring
# at black - see _arm_seat_watchdog.
SEAT_WATCHDOG_S = 12.0


def _log(message):
    """Say what the seat did, on his machine, in his log.

    Every decision in this module used to be silent except the watchdog,
    so "it started the whole ep from the beginning" arrived with no way
    to tell which of four paths had been taken - see
    .claude/rules/testing.md, "let the log find the next bug"."""
    try:
        from . import logs
        logs.info(message)
    except Exception:
        pass


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
                # **"Not yet" or "never"?** The Cues not read yet is
                # "not yet": arm_start_band owns its own retry and this
                # asks again next tick. A container the engine cannot
                # index at all - an .mp4, which is what his film sources
                # are - is "never", and waiting on it is the owner's
                # "it starts from the beginning": the poll sat out its
                # whole 180s for a band that could not be armed while
                # the film played from 0:00. There is nothing to wait
                # for, so the seek goes out blind and the engine
                # follows the demuxer's reads (_serve refocuses on the
                # byte mpv asks for) - measured on his Obsession .mp4
                # over the engine's own server, a ranged read at his
                # seat answered in 15.5s cold and 5.8s once the swarm
                # was up. Slower than an indexed seat, never instead of
                # it.
                return _seat_blind(self, stream)
            return not torrent._start_pieces()
        except Exception:
            return False

    def _seat_blind(self, stream):
        """True when this stream's seat can never be resolved to a byte
        (see torrent_engine.seat_resolvable), so a seek must go out
        without a band."""
        info_hash = str((stream or {}).get("info_hash") or "").strip().lower()
        if not info_hash:
            return False
        try:
            from helpers import torrent_engine
            return torrent_engine.seat_resolvable(info_hash) is False
        except Exception:
            return False

    def _seat_reachable_now(self, stream, seat):
        """Can this file be *opened* at the seat, right now?

        Deliberately not `_seat_data_ready`, and the difference is the
        default. That function answers the poll's question - "is there
        anything left to wait for" - and says yes when there is no
        torrent behind the url at all, because nothing can be waited
        for. Here the question is the opposite way round: opening at the
        seat is only safe when the bytes are *known* to be there, so
        anything unproven answers no and the file opens from the head as
        it did before.

        Measured 3 September 2026 with a stub page driving this method:
        the first version called `_seat_data_ready` and a torrent the
        engine had never heard of opened at the seat - the one case the
        module exists to prevent. In a real play `streams.prepare` has
        already added the torrent, so the engine not holding it means
        something is wrong or racing, and that is not a moment to
        gamble a black screen on.

        Two cases are safe and both are proven, not assumed:

          * **a stream this app is not serving itself** - a debrid link,
            an addon's HTTP stream, an HLS playlist. There is no engine
            behind it, nothing to fill, and range seeking is a request:
            `player._load_into_mpv`'s own note measured `start=8.05`
            opening at time-pos 8.050 against a deliberately slow
            source.
          * **an engine stream whose resume band is already on disk** -
            an episode played before, or a swarm that filled while the
            source list was being drawn. `_start_pieces()` empty is
            exactly that, and it is the same test the poll uses to
            decide the seek can succeed.

        **Told apart by `kind`, never by `info_hash`, and that is not a
        detail.** streams._prepare_with_debrid keeps the hash *for
        identity* - its own docstring says so - while handing back
        `kind="direct"` and a plain HTTPS URL. A guard reading the hash
        therefore sent every debrid play down the slow path, and his own
        log says `debrid: on - releases it has cached play over HTTPS`,
        so that is most of his plays: exactly the case he was reporting.
        `kind="torrent"` with `engine="atomic"` is what
        streams._prepare_with_own_engine hands back, and only that is
        served out of the piece store.
        """
        stream = stream or {}
        kind = str(stream.get("kind") or "").strip().lower()
        engine = str(stream.get("engine") or "").strip().lower()
        if kind != "torrent" and engine != "atomic":
            return True
        info_hash = str(stream.get("info_hash") or "").strip().lower()
        if not info_hash:
            return False          # an engine stream with no hash: unproven
        try:
            from helpers import torrent_engine
            torrent = getattr(torrent_engine, "_torrents", {}).get(info_hash)
            if torrent is None:
                return False
            offset = getattr(torrent, "start_offset", None)
            if offset is None:
                return False
            # **`_start_pieces()` being empty is not proof.** Measured 4
            # September 2026, after the owner reported that reloading a
            # source from inside the player "hides the upper bar and
            # freeze": that function returns [] both when the band has
            # landed *and* when it was never armed
            # (`if self.start_offset is None or not self.start_armed:
            # return []`), and a reload re-hands the torrent to the
            # engine - so a `start_offset` left over from the previous
            # play with `start_armed` still false read as "the bytes are
            # here". mpv was then opened at a seat whose pieces did not
            # exist, which is the blocking read this whole module exists
            # to prevent, and the freeze he saw.
            #
            # So the pieces are asked for directly. `have()` on the
            # piece holding the seat and on the one after it is the
            # cheapest honest question: a demuxer needs a run past the
            # offset, not just the byte at it, and two pieces is 4MB.
            if not getattr(torrent, "start_armed", False):
                return False
            first = torrent.piece_at(int(offset))
            if not all(torrent.have(p) for p in (first, first + 1)):
                return False
            return not torrent._start_pieces()
        except Exception:
            return False

    def _arm_seat_watchdog(self, stream, resume_at):
        """If opening at the seat produces no picture, go back to the head.

        **A net under the fast path, not a second mechanism.** Opening
        with `start=` is safe for the two cases `_seat_reachable_now`
        proves - a stream this app does not serve, and an engine stream
        whose band is on disk - but "proved" is about the bytes, and a
        host can still stall, refuse a range, or hand back something mpv
        cannot demux from the middle. The failure mode that costs is the
        one this whole module exists for: a viewer left on black under a
        message that cannot come true.

        So the picture is given SEAT_WATCHDOG_S to appear, and if it has
        not, the file is re-opened from the head and the ordinary poll
        takes over - which is exactly the path this would have taken
        without the fast route. Cancelled the moment a frame lands, so
        the ordinary case pays one timer.
        """
        _cancel_watchdog(self)
        run = self._run
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.setInterval(int(SEAT_WATCHDOG_S * 1000))

        def bite():
            if getattr(self, "_closing", False) or self._run != run:
                return
            if not getattr(self, "_awaiting_first_frame", True):
                return                    # a frame arrived; nothing to do
            try:
                from helpers import logs
                logs.info("seat: no picture at the seat in "
                          f"{SEAT_WATCHDOG_S:.0f}s; reopening from the head")
            except Exception:
                pass
            _load_from_head(self, stream, resume_at)
            _arm_head_watchdog(self, resume_at)

        timer.timeout.connect(bite)
        self._atomic_seat_watchdog = timer
        timer.start()

    def _arm_head_watchdog(self, resume_at):
        """After the seat watchdog has reopened a source from the head,
        the source gets SEAT_WATCHDOG_S more to draw anything at all;
        then it is a dead source and the walk moves on.

        **The owner's 68.8s start, 5 September 2026.** His log: a race
        of three lanes died at 12s; the walk then landed on a row with a
        direct url, which opened "proven" at the seat and drew nothing;
        the watchdog above reopened it from the head at 12s; and the
        player's own 45s seat give-up was what finally called it stalled
        and moved to the next source - which then played in 8s. A source
        that produces no frame from the head either is not a seat
        problem, it is a dead source, and the walk already knows what to
        do with one of those."""
        run = self._run
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.setInterval(int(SEAT_WATCHDOG_S * 1000))

        def bite():
            if getattr(self, "_closing", False) or self._run != run:
                return
            if not getattr(self, "_awaiting_first_frame", True):
                return
            _stop_poll(self)
            _log(f"seat: no picture from the head either in "
                 f"{SEAT_WATCHDOG_S:.0f}s; giving up on this source")
            try:
                if not self._try_next_source(self._stream_index,
                                             resume_at=resume_at):
                    self._finish_working("No Source Would Start")
            except Exception:
                pass

        timer.timeout.connect(bite)
        self._atomic_seat_watchdog = timer
        timer.start()

    def _cancel_watchdog(self):
        timer = getattr(self, "_atomic_seat_watchdog", None)
        if timer is not None:
            try:
                timer.stop()
            except RuntimeError:
                pass
        self._atomic_seat_watchdog = None

    def _load_from_head(self, stream, resume_at):
        """The slow path, taken deliberately: open unseated and poll."""
        _cancel_watchdog(self)
        try:
            seat = self._prime_seat(resume_at)[0]
        except Exception:
            seat = None
        if not seat:
            return
        sentinel = object()
        prior = self.__dict__.get("_prime_seat", sentinel)
        self._prime_seat = lambda _resume_at=None: (None, None)
        try:
            old_load(self, stream, resume_at)
        finally:
            if prior is sentinel:
                self.__dict__.pop("_prime_seat", None)
            else:
                self.__dict__["_prime_seat"] = prior
        _begin_seat_poll(self, stream, seat)

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
                    _log(f"seat: playback reached {target:.1f}s on its own")
                    return
                if time.monotonic() > current["deadline"]:
                    _stop_poll(self)
                    _log(f"seat: gave up on {target:.1f}s after "
                         f"{SEAT_GIVE_UP_S:.0f}s - its data never landed")
                    return
                if not _seat_data_ready(self, current["stream"], target):
                    # **Ask the engine again, every couple of seconds,
                    # while the seat is not a byte yet.** Its own retry
                    # used to stop after 8s and nothing re-asked: on a
                    # 16MB-piece release the Cues landed 20-30s after the
                    # url, so the band was never armed and this poll sat
                    # out its 180s - the owner's "do not resume" (5
                    # September 2026). arm_start_band runs one retry per
                    # torrent at a time, so asking is cheap.
                    now = time.monotonic()
                    if now - current.get("rearmed_at", 0.0) >= 2.0:
                        current["rearmed_at"] = now
                        info_hash = str(current["stream"].get("info_hash")
                                        or "").strip().lower()
                        if info_hash:
                            try:
                                from helpers import torrent_engine
                                torrent_engine.arm_start_band(info_hash)
                            except Exception:
                                pass
                    return

                _stop_poll(self)
                if _seat_blind(self, current["stream"]):
                    _log(f"seat: no index the engine can read in this "
                         f"container; seeking blind to {target:.1f}s")
                else:
                    _log(f"seat: data for {target:.1f}s has landed; "
                         f"skipping to it")
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

        # **Open at the seat when the seat is already reachable.** The
        # owner, 3 September 2026: *"when I play Reacher it takes a while
        # to skip to the progress point, make it start from the stopped
        # point directly."*
        #
        # He is right, and this module was doing it to him on purpose in
        # a case where it buys nothing. What it exists for is written at
        # the top: a torrent whose bytes at the seat do **not exist yet**
        # cannot be opened with `start=`, because mpv blocks on a stream
        # read tens of seconds long, gives up, and restarts at the head
        # under a "Skipping to 4:00..." that can never come true. That is
        # a real measurement and it stands.
        #
        # It was applied to every resume regardless. Two of them never
        # needed it:
        #
        #   * **a direct URL** - a debrid link, an addon's HTTP stream -
        #     has no engine behind it and nothing to wait for. HTTP range
        #     seeking is a request, so `start=` is exact and immediate,
        #     which is what player._load_into_mpv's own note measured
        #     (start=8.05 opened at time-pos 8.050 against a deliberately
        #     slow source);
        #   * **a torrent whose resume band is already on disk** - an
        #     episode played before, or a swarm that filled while the
        #     sources were being listed. `_seat_data_ready` is the exact
        #     question and it is asked here, before the load, rather than
        #     only from the poll afterwards.
        #
        # In both, going through the head first means watching the
        # opening of an episode he has already seen and then a skip - the
        # "takes a while to skip to the progress point" he is describing.
        # Where the band genuinely is not there, nothing changes: the
        # file opens from the head and the seat is applied when it lands.
        if _seat_reachable_now(self, stream, seat):
            _stop_poll(self)
            _log(f"seat: opening at {seat:.1f}s - the bytes there are proven")
            result = old_load(self, stream, resume_at)
            _arm_seat_watchdog(self, stream, resume_at)
            return result

        # **A watchdog armed by the load before this one is cancelled
        # here too.** It only used to be cancelled on the fast path, so a
        # source switch taken while one was still counting left a
        # single-shot timer holding the *previous* stream: it bit twelve
        # seconds later, saw `_awaiting_first_frame` still true (the new
        # source was still connecting) and re-opened the release that had
        # just been switched away from.
        _cancel_watchdog(self)
        _log(f"seat: opening from the head; {seat:.1f}s will be taken when "
             f"its data lands")

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
        _cancel_watchdog(self)
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
