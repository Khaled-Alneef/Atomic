"""Do not let live QScrollArea motion outrun the paint it produces.

The native-refresh clock remains authoritative. This patch only prevents an
ordinary QWidget scroll area from committing another integer scrollbar value
while the viewport/body has not yet painted the previous one. When a page can
sustain the monitor rate this gate is effectively free; when the GUI thread
misses a presentation opportunity, the next accepted _Momentum tick integrates
the real elapsed time instead of generating intermediate positions that Qt
coalesces away.

At >=200 Hz, quick_scroll_patch may temporarily cover a qualifying area with a
QQuickWindow compositor. While that surface is active there is deliberately no
QWidget paint to acknowledge, so the gate yields to the compositor and lets the
floating-point motion continue. The normal paint acknowledgement resumes the
moment the Quick surface goes away.
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

    def _quick_active(motion):
        try:
            surface = getattr(motion._bar, "_atomic_quick_scroll_surface", None)
            return bool(surface is not None and surface.active)
        except RuntimeError:
            return False

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
        # The Quick compositor is the paint surface while active. The integer
        # scrollbar still tracks state underneath it, but waiting for a QWidget
        # paint that is intentionally disabled would stall the motion.
        quick = _quick_active(self)
        changed = value != getattr(self, "_last_value", None)
        if (changed and not quick
                and getattr(self, "_atomic_paint_ack", None) is not None):
            self._atomic_waiting_paint = True
            self._atomic_waiting_since = time.monotonic()
        return old_set_value(self, value)

    def tick(self):
        if _quick_active(self):
            self._atomic_waiting_paint = False
            self._atomic_waiting_since = 0.0
            return old_tick(self)

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
