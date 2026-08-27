"""Global GUI frame-pacing profiler, off unless asked for.

**Why this exists.** The owner's report, 27 August 2026: the stutter is
not only in the video player - "the ENTIRE application also feels
uneven/stuttery while scrolling and during UI motion". Video judder and
scroll judder had been chased separately for days; if both are uneven,
the thing they share is the Qt GUI thread, and nothing in this app was
measuring that.

`ATOMIC_DEBUG_FRAME_PACING=1` turns it on. It is one QTimer whose own
work is a subtraction and a comparison: if the event loop is healthy it
fires on time, and if something blocks the thread the gap *is* the
block. Only stalls past the bands below are written, so the profiler
cannot become the thing it is measuring - the first version of a monitor
like this that logs every tick measures its own logging.

It is deliberately not wired into any hot path. Nothing here runs unless
the variable is set, and `note_vblank`/`note_paint` are one increment
each so the call sites can be left in permanently.
"""

import os
import time

from PyQt6.QtCore import QObject, QTimer, Qt

from . import logs

ENABLED = bool(os.environ.get("ATOMIC_DEBUG_FRAME_PACING"))

# The tick is 8ms, so a healthy gap is 8-ish. Anything at or past the
# first band is already a missed 120Hz frame; 16.7 is a missed 60Hz one.
BANDS = (12.0, 16.0, 25.0, 40.0)
TICK_MS = 8

# Counters the harnesses and the report read. Incremented from the GUI
# thread except _vblank, which the ticker thread touches - a bare int
# increment, deliberately not locked: an occasional lost count is worth
# less than a lock on a 240Hz path.
counters = {"vblank": 0, "paint": 0, "wakeups": 0, "stalls": 0}
worst = {"ms": 0.0, "where": ""}


def note_vblank():
    if ENABLED:
        counters["vblank"] += 1


def note_paint():
    if ENABLED:
        counters["paint"] += 1


def reset():
    for key in counters:
        counters[key] = 0
    worst["ms"] = 0.0
    worst["where"] = ""


class _Monitor(QObject):
    def __init__(self, window=None):
        super().__init__()
        self._window = window
        self._last = time.monotonic()
        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.setInterval(TICK_MS)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    def _context(self):
        """What the app was doing, cheaply. Only built for a stall - the
        healthy path must not pay for this."""
        window = self._window
        page = player = "?"
        try:
            page = type(getattr(window, "_current_page", None)).__name__
            player = "yes" if getattr(window, "_player_page", None) else "no"
        except (AttributeError, RuntimeError):
            pass
        scrolling = "?"
        try:
            from .widgets import _Momentum
            scrolling = "yes" if getattr(_Momentum, "_live", None) else "no"
        except Exception:
            pass
        return f"page={page} scrolling={scrolling} player={player}"

    def _tick(self):
        now = time.monotonic()
        gap = (now - self._last) * 1000.0
        self._last = now
        counters["wakeups"] += 1
        if gap < BANDS[0]:
            return
        band = BANDS[0]
        for edge in BANDS:
            if gap >= edge:
                band = edge
        counters["stalls"] += 1
        if gap > worst["ms"]:
            worst["ms"] = gap
            worst["where"] = self._context()
        logs.info(f"[FRAME PACING] stall={gap:.1f}ms (>={band:.0f}) "
                  f"{self._context()}")


_monitor = None


def start(window=None):
    """Begin monitoring, once. Returns whether it is running."""
    global _monitor
    if not ENABLED or _monitor is not None:
        return _monitor is not None
    _monitor = _Monitor(window)
    logs.info(f"[FRAME PACING] monitor on, {TICK_MS}ms tick, bands "
              + ", ".join(f"{b:.0f}" for b in BANDS))
    return True
