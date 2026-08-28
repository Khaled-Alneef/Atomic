"""Do not let live QScrollArea motion outrun the paint it produces.

The native-refresh clock remains authoritative. This patch only prevents an
ordinary QWidget scroll area from committing another integer scrollbar value
while the viewport/body has not yet painted the previous one. When a page can
sustain the monitor rate this gate is effectively free; when the GUI thread
misses a presentation opportunity, the next accepted _Momentum tick integrates
the real elapsed time instead of generating intermediate positions that Qt
coalesces away.

This is deliberately not a compositor and does not alter wheel distance,
friction, acceleration, refresh-rate selection, or PosterGrid's painted path.
"""

from __future__ import annotations

import time

_INSTALLED = False


def install():
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    from PyQt6.QtCore import QEvent, QObject
    from PyQt6.QtWidgets import QAbstractScrollArea

    from . import widgets as w

    old_init = w._Momentum.__init__
    old_tick = w._Momentum._tick
    old_set_value = w._Momentum._set_value
    old_stop = w._Momentum._stop_ticking

    def _scroll_area_for(bar):
        try:
            node = bar.parent()
            for _ in range(8):
                if node is None:
                    break
                if isinstance(node, QAbstractScrollArea):
                    return node
                node = node.parent()
        except RuntimeError:
            return None
        return None

    class _PaintAck(QObject):
        """Acknowledge the position once either scroll paint surface runs."""

        def __init__(self, area, motion):
            super().__init__(area)
            self.motion = motion
            self.targets = []
            try:
                self.targets.append(area.viewport())
            except RuntimeError:
                pass

            # QScrollArea normally scroll-blits the body and only repaints the
            # newly exposed strip. Listen to the body as well as the viewport so
            # the gate follows the paint that actually represents the new value.
            try:
                body = area.widget()
                if body is not None and body not in self.targets:
                    self.targets.append(body)
            except (AttributeError, RuntimeError):
                pass

            for target in self.targets:
                target.installEventFilter(self)

        def eventFilter(self, obj, event):
            if event.type() == QEvent.Type.Paint:
                motion = self.motion
                motion._atomic_waiting_paint = False
                motion._atomic_waiting_since = 0.0
            return False

    def init(self, *args, **kwargs):
        old_init(self, *args, **kwargs)
        self._atomic_waiting_paint = False
        self._atomic_waiting_since = 0.0
        self._atomic_paint_ack = None
        try:
            area = _scroll_area_for(self._bar)
            if area is not None:
                self._atomic_paint_ack = _PaintAck(area, self)
        except (AttributeError, RuntimeError):
            pass

    def set_value(self, value):
        # Only a genuinely new integer position requires an acknowledgement.
        # Rounded duplicates produce no bar write and therefore no scroll paint.
        changed = value != getattr(self, "_last_value", None)
        if changed and getattr(self, "_atomic_paint_ack", None) is not None:
            self._atomic_waiting_paint = True
            self._atomic_waiting_since = time.monotonic()
        return old_set_value(self, value)

    def tick(self):
        if (getattr(self, "_atomic_waiting_paint", False)
                and getattr(self, "_atomic_paint_ack", None) is not None):
            now = time.monotonic()
            since = float(getattr(self, "_atomic_waiting_since", 0.0) or 0.0)
            try:
                frame_s = self._frame_s() if self._frame_s is not None else 0.0
            except Exception:
                frame_s = 0.0

            # Fail open for hidden/covered/offscreen areas. Three display frames
            # is long enough for a normal coalesced paint but cannot freeze a
            # scroll if Qt legitimately has nothing visible to paint.
            timeout = max(0.050, 3.0 * frame_s) if frame_s > 0.0 else 0.050
            if since > 0.0 and now - since < timeout:
                return
            self._atomic_waiting_paint = False
            self._atomic_waiting_since = 0.0

        return old_tick(self)

    def stop(self):
        self._atomic_waiting_paint = False
        self._atomic_waiting_since = 0.0
        return old_stop(self)

    w._Momentum.__init__ = init
    w._Momentum._set_value = set_value
    w._Momentum._tick = tick
    w._Momentum._stop_ticking = stop
