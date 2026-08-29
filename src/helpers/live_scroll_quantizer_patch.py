"""Improve high-refresh live QWidget scrolling without a raster overlay.

Home and Tracker's Discover/Saved/History pages cannot safely use the moving
QWidget-snapshot compositor: the 1.10.158 A/B removed the visual corruption but
also confirmed that the live integer scrollbar path is less smooth.

Keep those pages live and stable, but replace absolute round(position) commits
with an error-distributed integer stepper on >=150 Hz displays.  The momentum
integrator remains floating point; this patch only decides when accumulated
travel has earned the next whole QWidget pixel.  That avoids arbitrary rounding
phase changes while never presenting a second copy of the page.

This is deliberately scoped to HomePage / TrackerPage QScrollBars. PosterGrid's
separate Qt Quick scene-graph path remains untouched.
"""

from __future__ import annotations

import math

_INSTALLED = False


def install():
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    from PyQt6.QtWidgets import QAbstractScrollArea
    from . import widgets as w

    old_init = w._Momentum.__init__
    old_set_value = w._Momentum._set_value
    old_stop = w._Momentum._stop_ticking

    def _area_for(bar):
        try:
            node = bar.parent()
            for _ in range(10):
                if node is None:
                    return None
                if isinstance(node, QAbstractScrollArea):
                    return node
                node = node.parent()
        except RuntimeError:
            return None
        return None

    def _guarded(widget) -> bool:
        seen = set()
        node = widget
        for _ in range(32):
            if node is None or id(node) in seen:
                break
            seen.add(id(node))
            try:
                classes = type(node).__mro__
            except Exception:
                classes = ()
            for cls in classes:
                module = getattr(cls, "__module__", "")
                name = getattr(cls, "__name__", "")
                if module == "windows.home" and name == "HomePage":
                    return True
                if module == "windows.tracker" and name == "TrackerPage":
                    return True
            try:
                node = node.parentWidget()
            except (AttributeError, RuntimeError):
                break
        return False

    def _eligible(motion) -> bool:
        try:
            area = _area_for(motion._bar)
            if area is None or not _guarded(area):
                return False
            screen = area.screen()
            return screen is not None and float(screen.refreshRate()) >= 150.0
        except (AttributeError, RuntimeError, TypeError, ValueError):
            return False

    def init(self, *args, **kwargs):
        old_init(self, *args, **kwargs)
        self._atomic_live_q_last_pos = None
        self._atomic_live_q_residual = 0.0
        self._atomic_live_q_value = None

    def _reset(motion):
        motion._atomic_live_q_last_pos = None
        motion._atomic_live_q_residual = 0.0
        motion._atomic_live_q_value = None

    def set_value(self, value):
        if not _eligible(self) or self._pos is None:
            _reset(self)
            return old_set_value(self, value)

        desired = float(self._pos)
        previous_sample = self._atomic_live_q_last_pos
        if previous_sample is None:
            self._atomic_live_q_last_pos = desired
            self._atomic_live_q_value = int(self._bar.value())
            self._atomic_live_q_residual = desired - float(self._atomic_live_q_value)
            # First commit remains the core model's requested value so a fresh
            # wheel event answers immediately rather than waiting to accumulate.
            committed = int(value)
            self._atomic_live_q_value = committed
            self._atomic_live_q_residual = desired - float(committed)
            return old_set_value(self, committed)

        delta = desired - float(previous_sample)
        self._atomic_live_q_last_pos = desired
        residual = float(self._atomic_live_q_residual) + delta
        current = int(self._atomic_live_q_value
                      if self._atomic_live_q_value is not None
                      else self._bar.value())

        # At the terminal snap/boundary, honour the core model exactly. This
        # prevents a sub-pixel remainder from leaving the scrollbar one pixel
        # short after momentum stops.
        terminal = ((abs(float(getattr(self, "_vel", 0.0))) < self.STOP_SPEED
                     and not getattr(self, "_pending", 0.0))
                    or desired <= float(self._bar.minimum())
                    or desired >= float(self._bar.maximum()))
        if terminal:
            committed = int(value)
            self._atomic_live_q_value = committed
            self._atomic_live_q_residual = desired - float(committed)
            return old_set_value(self, committed)

        # Truncate toward zero: residual travel is conserved for the next
        # refresh instead of being discarded by absolute round(). With a slowly
        # changing velocity this distributes 1px commits across refreshes in the
        # same ratio as the floating-point motion, but never moves backwards.
        step = math.floor(residual) if residual >= 0.0 else math.ceil(residual)
        if step:
            committed = max(self._bar.minimum(),
                            min(self._bar.maximum(), current + int(step)))
            actual = committed - current
            residual -= float(actual)
            self._atomic_live_q_value = committed
            self._atomic_live_q_residual = residual
            return old_set_value(self, committed)

        self._atomic_live_q_residual = residual
        # No earned whole pixel this refresh. Avoid a redundant setValue/repaint.
        return None

    def stop(self):
        result = old_stop(self)
        _reset(self)
        return result

    w._Momentum.__init__ = init
    w._Momentum._set_value = set_value
    w._Momentum._stop_ticking = stop
