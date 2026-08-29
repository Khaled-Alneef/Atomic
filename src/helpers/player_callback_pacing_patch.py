"""Keep continuous mpv playback telemetry out of Python's event callback path.

Atomic's video is rendered by libmpv in native threads, but the player also
observed ``time-pos`` and ``demuxer-cache-time`` through python-mpv.  Those
properties change continuously while a file is playing.  python-mpv therefore
wakes its Python event thread, calls a Python callback, emits a Qt signal and
runs the player UI update path over and over for telemetry which only needs UI
rate updates.

This remains true in ATOMIC_MPV_ONLY mode: hiding every native overlay removes
painting/stacking, not the property observers.  It is also a real difference
from standalone mpv, and from Stremio's native Rust property bridge.

The fix is deliberately player-only and does not alter mpv's renderer, video
sync, hardware decoding, stream buffering, subtitles or any app-wide timer.
Only the two continuous observations are intercepted.  The player samples them
from the Qt thread instead: 20 Hz until the first frame is established, then
10 Hz for playback position and 4 Hz for buffered position.  All discrete mpv
properties/events remain normal observed events.
"""

from __future__ import annotations

from PyQt6.QtCore import QTimer, Qt

_INSTALLED = False
_CONTINUOUS = {"time-pos", "demuxer-cache-time"}
_STARTUP_POLL_MS = 50
_PLAYBACK_POLL_MS = 100
_BUFFER_POLL_MS = 250


class _ObservedHandle:
    """Transparent mpv handle which diverts only continuous observations."""

    def __init__(self, inner):
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "_atomic_continuous_callbacks", {})

    @property
    def _atomic_inner_handle(self):
        return object.__getattribute__(self, "_inner")

    def observe_property(self, name, callback):
        if str(name) in _CONTINUOUS:
            self._atomic_continuous_callbacks[str(name)] = callback
            return callback
        return self._inner.observe_property(name, callback)

    def __getitem__(self, key):
        return self._inner[key]

    def __setitem__(self, key, value):
        self._inner[key] = value

    def __getattr__(self, name):
        return getattr(self._inner, name)



def _unwrap(handle):
    return getattr(handle, "_atomic_inner_handle", handle)


def _read_property(handle, python_name):
    try:
        return getattr(handle, python_name)
    except Exception:
        return None


def install():
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    from helpers import video_backend
    from windows import player

    old_create = video_backend.create
    old_shutdown = video_backend.shutdown
    old_start = player.PlayerPage._start
    old_close = player.PlayerPage.close_player

    def create(window_id: int, **overrides):
        return _ObservedHandle(old_create(window_id, **overrides))

    def shutdown(handle):
        return old_shutdown(_unwrap(handle))

    def start(self):
        result = old_start(self)
        handle = getattr(self, "handle", None)
        if not isinstance(handle, _ObservedHandle):
            return result
        if getattr(self, "_atomic_mpv_poll_timer", None) is not None:
            return result

        timer = QTimer(self)
        timer.setTimerType(Qt.TimerType.PreciseTimer)
        timer.setInterval(_STARTUP_POLL_MS)
        self._atomic_mpv_poll_timer = timer
        self._atomic_last_buffer_poll = 0.0

        def poll():
            if getattr(self, "_closing", False) or getattr(self, "handle", None) is None:
                timer.stop()
                return

            current = getattr(self, "handle", None)
            if not isinstance(current, _ObservedHandle):
                timer.stop()
                return

            # Do not route these samples back through _mpv_property: that
            # helper exists to cross from mpv's worker thread into Qt.  This
            # timer already runs on Qt's thread, so the slot can be called
            # directly and avoids a needless queued signal.
            value = _read_property(current, "time_pos")
            if value is not None:
                try:
                    self._on_property("time-pos", value)
                except RuntimeError:
                    timer.stop()
                    return

            import time
            now = time.monotonic()
            if now - getattr(self, "_atomic_last_buffer_poll", 0.0) >= \
                    (_BUFFER_POLL_MS / 1000.0):
                self._atomic_last_buffer_poll = now
                buffered = _read_property(current, "demuxer_cache_time")
                if buffered is not None:
                    try:
                        self._on_property("demuxer-cache-time", buffered)
                    except RuntimeError:
                        timer.stop()
                        return

            wanted = (_STARTUP_POLL_MS if getattr(self, "_awaiting_first_frame", False)
                      else _PLAYBACK_POLL_MS)
            if timer.interval() != wanted:
                timer.setInterval(wanted)

        timer.timeout.connect(poll)
        timer.start()
        return result

    def close_player(self, *args, **kwargs):
        timer = getattr(self, "_atomic_mpv_poll_timer", None)
        if timer is not None:
            try:
                timer.stop()
            except RuntimeError:
                pass
        return old_close(self, *args, **kwargs)

    video_backend.create = create
    video_backend.shutdown = shutdown
    player.PlayerPage._start = start
    player.PlayerPage.close_player = close_player
