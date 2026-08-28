"""Give high-refresh Home/Discover a real post-wheel coast.

Changing _Momentum friction alone was not perceptible on the owner's 240 Hz
mouse wheel because each physical notch is already shaped by the same impulse /
friction pair. This patch therefore adds an explicit *post-input* tail instead:
when the wheel has been quiet for 65 ms, the active Home/Discover momentum gets
a small residual velocity in the same direction. Any newer wheel event cancels
that pending tail by advancing a generation token.

The scope stays intentionally narrow: vertical Home/Discover wheel motion on
200 Hz+ displays only. Movies, every other page, horizontal rows and the
confirmed 160 Hz path are untouched.
"""

from __future__ import annotations

import time

_INSTALLED = False
_HIGH_REFRESH_HZ = 200.0
_HIGH_REFRESH_FRICTION = 32.0
_DEFAULT_FRICTION = 50.0
_COAST_QUIET_MS = 65
# Extra residual speed after the final physical notch. With friction 32 this is
# only a short tail, not a long inertial glide. It is deliberately modest so the
# owner can feel continuation without the page floating away.
_COAST_VELOCITY_PX_S = 360.0


def _page_owned(area) -> bool:
    node = area
    for _ in range(20):
        if node is None:
            break
        try:
            cls = type(node)
            if ((cls.__module__ == "windows.home" and cls.__name__ == "HomePage")
                    or (cls.__module__ == "windows.tracker"
                        and cls.__name__ == "DiscoverPage")):
                return True
            node = node.parentWidget()
        except (AttributeError, RuntimeError):
            break
    return False


def _high_refresh_area(area) -> bool:
    try:
        if area is None or not _page_owned(area):
            return False
        screen = area.screen()
        rate = float(screen.refreshRate()) if screen is not None else 0.0
        return rate >= _HIGH_REFRESH_HZ
    except (AttributeError, RuntimeError, TypeError, ValueError):
        return False


def install():
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    from PyQt6.QtCore import QEvent, QTimer, Qt
    from . import widgets

    # Keep the lower-friction high-refresh profile, but identify the page from
    # _SmoothWheel._area rather than QScrollBar's internal parent chain.
    old_kick = widgets._Momentum.kick

    def drift_kick(self, distance_px, direction):
        try:
            owner = self.parent()
            area = getattr(owner, "_area", None)
            bar = getattr(self, "_bar", None)
            if (area is not None and bar is not None
                    and bar.orientation() == Qt.Orientation.Vertical
                    and _page_owned(area)):
                self.FRICTION = (_HIGH_REFRESH_FRICTION
                                 if _high_refresh_area(area)
                                 else _DEFAULT_FRICTION)
        except (AttributeError, RuntimeError, TypeError, ValueError):
            pass
        return old_kick(self, distance_px, direction)

    widgets._Momentum.kick = drift_kick

    # Physical wheel events are the reliable place to know when the user's hand
    # has stopped. Wrap _SmoothWheel's existing filter, let it process the event
    # normally, then schedule one tiny continuation. A generation token means a
    # second notch inside the quiet window invalidates the first pending coast.
    old_wheel_filter = widgets._SmoothWheel.eventFilter

    def wheel_filter(self, obj, event):
        schedule = False
        direction = 0
        generation = None
        try:
            if event.type() == QEvent.Type.Wheel:
                area = getattr(self, "_area", None)
                if _high_refresh_area(area):
                    pixel = event.pixelDelta()
                    angle = event.angleDelta()
                    dy = pixel.y() if not pixel.isNull() else angle.y()
                    dx = pixel.x() if not pixel.isNull() else angle.x()
                    if dy and abs(dy) >= abs(dx):
                        direction = -1 if dy > 0 else 1
                        generation = int(getattr(
                            self, "_atomic_wheel_coast_generation", 0)) + 1
                        self._atomic_wheel_coast_generation = generation
                        schedule = True
        except (AttributeError, RuntimeError, TypeError, ValueError):
            schedule = False

        result = old_wheel_filter(self, obj, event)

        if schedule:
            def apply_coast(owner=self, token=generation, sign=direction):
                try:
                    if int(getattr(owner, "_atomic_wheel_coast_generation", 0)) != token:
                        return
                    area = getattr(owner, "_area", None)
                    if not _high_refresh_area(area):
                        return
                    motion = getattr(owner, "_motion", None)
                    if motion is None:
                        return
                    bar = getattr(motion, "_bar", None)
                    if bar is None or bar.orientation() != Qt.Orientation.Vertical:
                        return

                    # True drift: extend the existing velocity rather than
                    # synthesizing another notch. If the normal glide happened
                    # to settle inside the 65 ms quiet window, re-anchor at the
                    # current scrollbar value and restart the same frame clock.
                    if getattr(motion, "_pos", None) is None:
                        motion._pos = float(bar.value())
                        motion._pending = 0.0
                        motion._vel = 0.0
                    motion.FRICTION = _HIGH_REFRESH_FRICTION
                    if motion._vel * sign < 0:
                        return
                    motion._vel += sign * _COAST_VELOCITY_PX_S
                    motion._last = time.monotonic()
                    motion._start_ticking()
                except (AttributeError, RuntimeError, TypeError, ValueError):
                    pass

            QTimer.singleShot(_COAST_QUIET_MS, apply_coast)

        return result

    widgets._SmoothWheel.eventFilter = wheel_filter
