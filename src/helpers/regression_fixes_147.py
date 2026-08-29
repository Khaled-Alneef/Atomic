"""Critical player startup/resume fixes after 1.10.146.

Two real causes remained behind the reported behaviour:

1. The torrent engine widened the request set again as soon as piece zero landed.
   That makes the *whole selected file* priority 1 while mpv is still opening, so
   a screenshot can truthfully show 10+ MB/s and 17% complete while the two or
   three exact head/index pieces needed for frame one are still unfinished.
   During startup this patch keeps ONLY current reader bands + the container tail
   wanted.  The normal wide streaming window returns the instant a decoded frame
   actually appears.

2. A reused torrent kept start_offset/start_armed from an older resume.  A later
   episode/source could therefore test readiness against stale seek bytes.  Every
   owned add now begins with a clean resume-band state, and a resume band is not
   allowed to compete with frame-one bytes.  It is armed only after picture one.

A resume target is also checked against the duration of the file that actually
opened before any seek is issued.  A stale point at/past the final 3% is cleared
instead of parking mpv on EOF.

No scrolling/UI-layout/chapter behaviour is changed here.
"""
from __future__ import annotations

import math
import sys

_INSTALLED = False
_PATCHED_PLAYER = set()


def _install_engine_startup_lock():
    try:
        from . import torrent_engine as engine
    except Exception:
        return

    Torrent = engine._Torrent

    # ---- arm_start_band -------------------------------------------------
    old_arm = engine.arm_start_band
    if not getattr(old_arm, "_atomic_147", False):
        def arm_start_band(info_hash):
            key = str(info_hash or "").strip().lower()
            torrent = getattr(engine, "_torrents", {}).get(key)
            if (torrent is not None
                    and getattr(torrent, "_atomic_startup_lock_147", False)):
                # Remembering start_seconds is fine; fetching that band before
                # frame one is not.  Head/index own the connection until then.
                return None
            return old_arm(info_hash)

        arm_start_band._atomic_147 = True
        arm_start_band._atomic_original = old_arm
        engine.arm_start_band = arm_start_band
        engine._atomic_arm_start_band_147 = old_arm

    # ---- add ------------------------------------------------------------
    old_add = engine.add
    if not getattr(old_add, "_atomic_147", False):
        def add(info_hash, *args, **kwargs):
            key = str(info_hash or "").strip().lower()
            own = bool(kwargs.get("own", True))

            # Reused torrents are the dangerous case: clear the previous
            # playback's resolved byte before add() gets a chance to focus with
            # it.  start_seconds is written afresh below when this playback has
            # a real resume point.
            existing = getattr(engine, "_torrents", {}).get(key)
            if own and existing is not None:
                try:
                    existing._atomic_startup_lock_147 = True
                    existing.start_offset = None
                    existing.start_armed = False
                    existing.start_seconds = None
                except Exception:
                    pass

            result = old_add(info_hash, *args, **kwargs)
            if not own or not result:
                return result

            torrent = getattr(engine, "_torrents", {}).get(
                str(result).strip().lower())
            if torrent is None:
                return result
            try:
                torrent._atomic_startup_lock_147 = True
                # Never inherit a resolved byte from the previous file/session.
                torrent.start_offset = None
                torrent.start_armed = False
                start_at = kwargs.get("start_at")
                torrent.start_seconds = (float(start_at)
                                         if start_at is not None else None)
                # Re-apply immediately.  The wrapped _apply_windows below will
                # collapse this to the true startup-critical pieces.
                if torrent.file_index is not None and not torrent.want_whole:
                    torrent.focus(0, engine.HEAD_BYTES)
            except Exception:
                pass
            return result

        add._atomic_147 = True
        add._atomic_original = old_add
        engine.add = add

    # ---- _Torrent._apply_windows ---------------------------------------
    old_apply = Torrent._apply_windows
    if not getattr(old_apply, "_atomic_147", False):
        def _strict_startup_priorities(torrent, windows):
            """Want only what can produce frame one.

            Unlike the normal streaming policy there is deliberately no
            priority-1 remainder here.  This lock exists only before the first
            decoded frame, when downloading unrelated middle pieces is exactly
            the failure mode reported by the completion percentage racing ahead
            of playback.
            """
            if torrent.want_whole or torrent.file_index is None:
                return
            handle = torrent.handle
            info = torrent.info
            total = int(info.num_pieces())
            if total <= 0:
                return
            file_first = torrent.piece_at(0)
            file_last = torrent.piece_at(max(torrent.file_size() - 1, 0))
            piece_len = max(1, int(torrent.piece_length()))
            urgent_count = max(
                2, int(math.ceil(float(engine.URGENT_BYTES) / piece_len)))

            ordered = sorted(list(windows or []), key=lambda row: -row[2])
            if not ordered:
                ordered = [(0, engine.HEAD_BYTES, 0)]

            priorities = [0] * total
            bands = []
            # Each actual mpv HTTP reader gets its own tiny urgent band.  The
            # primary 0-offset placeholder supplies the opening band before the
            # first GET exists.
            for offset, _span, _seq in ordered:
                start = max(file_first, torrent.piece_at(int(offset)))
                # Move past already-complete pieces so every priority-7 slot is
                # buying data rather than pointing at disk we already own.
                scanned = 0
                scan_limit = max(8, urgent_count * 8)
                while (start <= file_last and scanned < scan_limit
                       and torrent.have(start)):
                    start += 1
                    scanned += 1
                if start > file_last:
                    continue
                end = min(file_last, start + urgent_count - 1)
                for piece in range(start, end + 1):
                    priorities[piece] = 7
                bands.append((start, end))

            # Matroska Cues / MP4 moov-at-tail can be required before frame one.
            # Keep the complete tail band urgent, but nothing in the middle.
            tail = list(torrent._tail_pieces())
            for piece in tail:
                if 0 <= piece < total:
                    priorities[piece] = 7

            handle.prioritize_pieces(priorities)
            try:
                handle.clear_piece_deadlines()
            except Exception:
                pass

            # The newest reader is what mpv is blocked on now.  Give every
            # selected piece a concrete order so a large-piece torrent cannot
            # fill all of them halfway and finish none.
            for rank, (first, last) in enumerate(bands):
                base = 120 + rank * 5000
                for index, piece in enumerate(range(first, last + 1)):
                    try:
                        handle.set_piece_deadline(piece, base + index * 220)
                    except Exception:
                        pass
            for index, piece in enumerate(tail):
                try:
                    handle.set_piece_deadline(piece, 180 + index * 140)
                except Exception:
                    pass

        def apply_windows(self, windows):
            result = old_apply(self, windows)
            if getattr(self, "_atomic_startup_lock_147", False):
                try:
                    _strict_startup_priorities(self, windows)
                except Exception:
                    # Startup concentration is an optimisation/safety policy;
                    # the old engine remains the fallback if a binding differs.
                    pass
            return result

        apply_windows._atomic_147 = True
        apply_windows._atomic_original = old_apply
        Torrent._apply_windows = apply_windows

    def release_startup_lock(info_hash):
        key = str(info_hash or "").strip().lower()
        torrent = getattr(engine, "_torrents", {}).get(key)
        if torrent is None:
            return False
        try:
            torrent._atomic_startup_lock_147 = False
            # Restore the established normal streaming policy immediately.
            torrent.refresh_windows()
            return True
        except Exception:
            return False

    engine.release_startup_lock = release_startup_lock


def _patch_player(module):
    key = id(module)
    if key in _PATCHED_PLAYER:
        return
    _PATCHED_PLAYER.add(key)

    Page = module.PlayerPage
    old_property = Page._on_property
    old_seek = Page._seek_absolute

    def _current_hash(page):
        try:
            stream = page._streams[page._stream_index] or {}
            return str(stream.get("info_hash") or "").strip().lower()
        except Exception:
            return ""

    def on_property(self, name, value):
        was_waiting = bool(getattr(self, "_awaiting_first_frame", False))
        result = old_property(self, name, value)

        # The exact boundary the engine needs: not file-loaded, not duration,
        # not a buffering percentage - a time-pos that the standing player code
        # accepted as the first decoded frame.
        if (name == "time-pos" and was_waiting
                and not getattr(self, "_awaiting_first_frame", True)):
            info_hash = _current_hash(self)
            if info_hash:
                try:
                    from helpers import torrent_engine
                    torrent_engine.release_startup_lock(info_hash)
                    # A saved seat was deliberately forbidden from competing
                    # with frame one.  Now that picture exists, arm it fresh.
                    torrent = getattr(torrent_engine, "_torrents", {}).get(info_hash)
                    if torrent is not None and torrent.start_seconds:
                        torrent_engine.arm_start_band(info_hash)
                except Exception:
                    pass
        return result

    def seek_absolute(self, seconds, resuming=False):
        if resuming:
            try:
                target = float(seconds)
            except (TypeError, ValueError):
                return
            duration = float(getattr(self, "_duration", 0.0) or 0.0)
            if duration > 0:
                # Use the actual file mpv opened, not the duration stored by an
                # older source.  At/past the same 97% boundary used everywhere
                # else in the player, a resume is credits/EOF, not a useful seat.
                invalid = (target >= duration
                           or target >= duration * module.RESUME_MAX_FRACTION)
                if invalid:
                    try:
                        self._clear_seat()
                    except Exception:
                        pass
                    try:
                        module.clear_resume(self.entry, self.season, self.episode)
                    except Exception:
                        pass
                    # Keep the already-playing picture at the beginning.  Most
                    # importantly, never issue a seek that can park mpv at EOF.
                    return
        return old_seek(self, seconds, resuming=resuming)

    Page._on_property = on_property
    Page._seek_absolute = seek_absolute


def _chain_player_patch():
    # requested_fixes_patch remains the lazy import owner for windows.player.
    # Chain after every previous patch so the first-frame transition observed
    # here is the final one the user actually runs.
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
    _install_engine_startup_lock()
    _chain_player_patch()
