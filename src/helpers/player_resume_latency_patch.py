"""Open a resumed file at its seat, always - never from the head first.

**Superseded 3 November 2026, by a measurement. The owner: "when I cont
watching anything it takes ages to take me to the point I stopped in in
the player while that it plays from the start!!!!!!"** Everything after
this paragraph is the history of the head-first shape this module used
to impose, and `load_into_mpv` at the bottom no longer takes it.

Photographed on the frozen build (3.15.1, Brooklyn Nine-Nine S05E13 at
7:21, swarm only): first frame at 14.2s *from 0:00*, the seat at 48s, 32s
of an episode he had already seen, with its subtitles. His own log says
the same on 26 and 31 October and that morning - 87s and 17.6s of head,
and a third he left after 20s still at the start.

The reason given below - mpv at a seat whose bytes do not exist yet blocks
and falls back to the head - was measured the same day
player_seekable_stream_patch found the vendored libmpv's HTTP cannot seek
at all and moved the file onto `atomic://`. Measured again on that stream,
real engine and real libmpv, one throttled local seeder:

    mkv, 2MB pieces, 3MB/s, seat 7:21
      at the seat   first frame 10.1s AT 7:21, then 39.0s in 39.0s
      head first    first frame  7.1s at 0:00, still at 0:39 after 40s

It does not give up any more: the read blocks, the engine follows it, and
the picture arrives at the seat. And head-first is what made the wait
long: mpv goes on reading the opening, so the swarm is split between the
pieces it wants next and the seat band, and the band is what is waited
for.

So nothing plays until the seat does. The wait is on the loading frame,
which says "Skipping to 7:21..." (player._startup_snapshot).

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

# How often a file opened at its seat is asked whether it is getting
# anywhere - see _arm_seat_watchdog. It used to be how long the picture had
# before the file was re-opened from the head; a direct link's whole open
# is 3.9-5.2s across his October log, so 12s is well past a healthy one.
SEAT_WATCHDOG_S = 12.0

# What counts as "the swarm is delivering" across one SEAT_WATCHDOG_S:
# 256KB/s. A 12s bound on the picture is wrong for a swarm - the seat needs
# the header, the index and its own pieces, which on the 16MB pieces of his
# season packs is 48-64MB, whatever any timer says - but a trickle under
# this will not play the episode either, so it is not worth waiting for.
SEAT_FLOW_BYTES_S = 256 * 1024

# And the most a delivering swarm is waited for before the next release is
# asked. His slowest band in October landed 94s after the handover.
SEAT_PATIENCE_S = 120.0


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

    # **The head-first path and its seat poll are gone** (_load_from_head,
    # _arm_head_watchdog, _begin_seat_poll, _seat_data_ready, _seat_blind):
    # nothing had called them since every resume began opening at the seat,
    # and he said to remove them, 3 November 2026 (CLAUDE.md rule 11). Why
    # the poll was built, and what it measured, is above and in
    # .claude/rules/integrations.md.
    Page = module.PlayerPage
    old_load = Page._load_into_mpv
    old_close = Page.close_player

    def _seat_reachable_now(self, stream, seat):
        """Are the bytes at the seat known to be there, right now? The
        file opens at the seat either way since 3 November 2026; this is
        what load_into_mpv's log line says about it. Anything unproven
        answers no. In a real play `streams.prepare` has already added
        the torrent, so the engine not holding it means something is
        wrong or racing.

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
            exactly that.

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

    def _is_engine_stream(stream):
        """Served out of the piece store? `kind` and `engine`, never the
        hash - see the note in _seat_reachable_now."""
        stream = stream or {}
        kind = str(stream.get("kind") or "").strip().lower()
        engine = str(stream.get("engine") or "").strip().lower()
        return kind == "torrent" and engine == "atomic"

    def _engine_payload(stream):
        """Bytes this engine stream has received so far, or None when it
        is not one - a debrid link, or a finished file with no torrent."""
        if not _is_engine_stream(stream):
            return None
        info_hash = str((stream or {}).get("info_hash") or "").strip().lower()
        try:
            from helpers import torrent_engine
            torrent = getattr(torrent_engine, "_torrents", {}).get(info_hash)
            if torrent is None:
                return None
            return int(torrent.handle.status().total_payload_download)
        except Exception:
            return None

    def _arm_seat_watchdog(self, stream, resume_at):
        """No picture at the seat: wait while bytes are arriving, and walk
        to the next release when they are not. **Never back to the head.**

        It used to re-open the file from the beginning at 12s, which is
        the owner's report again by another road - and on a swarm 12s is
        routine rather than a failure, see SEAT_FLOW_BYTES_S. Across one
        tick a torrent is still delivering or it is not; a delivering one
        also moves the player's own give-up (`_seat_deadline`, 45s, which
        would otherwise call a working release "stalled"), up to
        SEAT_PATIENCE_S.

        A direct link has no swarm to read. Its file being open is the
        one sign of life (his 21 October log: `chapter markers` at 8s, a
        first frame later), so that buys it one more tick; then it is the
        next source's turn, seat kept. Cancelled by the first frame, so
        the ordinary case pays one timer."""
        _cancel_watchdog(self)
        run = self._run
        started = time.monotonic()
        state = {"payload": _engine_payload(stream), "ticks": 0}
        timer = QTimer(self)
        timer.setInterval(int(SEAT_WATCHDOG_S * 1000))

        def bite():
            if getattr(self, "_closing", False) or self._run != run:
                _cancel_watchdog(self)
                return
            if not getattr(self, "_awaiting_first_frame", True):
                _cancel_watchdog(self)
                return                    # a frame arrived; nothing to do
            state["ticks"] += 1
            waited = time.monotonic() - started
            payload = _engine_payload(stream)
            moved = None
            if payload is not None and state["payload"] is not None:
                moved = payload - state["payload"]
            state["payload"] = payload
            if moved is not None:
                if (moved >= SEAT_FLOW_BYTES_S * SEAT_WATCHDOG_S
                        and waited < SEAT_PATIENCE_S):
                    _log(f"seat: no picture yet after {waited:.0f}s, swarm "
                         f"delivering {moved / SEAT_WATCHDOG_S / 1e6:.2f}MB/s;"
                         f" waiting")
                    if getattr(self, "_resume_target", None) is not None:
                        self._seat_deadline = (time.monotonic()
                                               + module.SEAT_GIVE_UP_S)
                    return
            elif (state["ticks"] < 2
                    and float(getattr(self, "_duration", 0.0) or 0.0) > 0):
                _log(f"seat: no picture yet after {waited:.0f}s, the file is "
                     f"open; one more tick")
                return
            _cancel_watchdog(self)
            _log(f"seat: no picture at the seat in {waited:.0f}s"
                 + (f", {moved / SEAT_WATCHDOG_S / 1e6:.2f}MB/s"
                    if moved is not None else "")
                 + "; trying another source, seat kept")
            try:
                if not self._try_next_source(self._stream_index,
                                             resume_at=resume_at,
                                             reason="stalled"):
                    # Nothing left to walk to: the player's own give-up
                    # says so when its deadline comes round (_seat_tick).
                    _log("seat: no other source to try")
            except Exception:
                _log("seat: walking to the next source failed")

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

    def load_into_mpv(self, stream, resume_at=None):
        try:
            seat = self._prime_seat(resume_at)[0]
        except Exception:
            seat = None
        if not seat:
            return old_load(self, stream, resume_at)

        # **Every resume opens at the seat - see the top of this module.**
        # The engine is told first, because a release the player reuses
        # (a prewarm, the last source) may never have been given this
        # seat at add(), and an engine stream with no band is a seek
        # nothing is fetching for. mpv's own reads then re-aim it
        # (_serve), and arm_start_band retries by itself until the index
        # is on disk.
        #
        # **The other half of this is the flash he also saw:** a finished
        # file, or a band already on disk with `start_armed` not yet set,
        # read as "unproven" here and opened from the head, then jumped
        # 0.04-0.13s after the first frame (his 29 October, 19:48: 0.1s,
        # 0.07s, 0.1s).
        info_hash = str((stream or {}).get("info_hash") or "").strip().lower()
        if info_hash and _is_engine_stream(stream):
            try:
                from helpers import torrent_engine
                torrent_engine.set_start_seconds(info_hash, float(seat))
                torrent_engine.arm_start_band(info_hash)
            except Exception:
                pass
        ready = ((stream or {}).get("local_file")
                 or _seat_reachable_now(self, stream, seat))
        _log(f"seat: opening at {seat:.1f}s - "
             + ("the bytes there are proven" if ready else
                "fetching them under the loading frame"))
        result = old_load(self, stream, resume_at)
        _arm_seat_watchdog(self, stream, resume_at)
        return result

    def close_player(self):
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
