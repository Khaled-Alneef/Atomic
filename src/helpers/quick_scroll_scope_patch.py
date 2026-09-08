"""Keep the live QWidget scroll position frozen while Quick owns a glide.

The high-refresh Qt Quick overlay is the only path that has been user-confirmed
smooth on both 165 Hz and 240 Hz.  The page corruption came from advancing the
real QScrollArea underneath that overlay on every momentum tick as well, which
left two independently-presented copies of the same page moving at once.

Do not disable Quick on Home/Tracker.  Instead, while a Quick surface is active,
let _Momentum keep integrating its floating-point position but suppress the
integer QScrollBar commit.  The overlay is the only visible moving surface.
When the glide ends (or is aborted), commit the final integer scrollbar value
once while the overlay is still covering the viewport, repaint the live page,
and then reveal it.
"""

from __future__ import annotations

_INSTALLED = False


def install():
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    from . import widgets as w

    previous_scroll_area = w.scroll_area
    old_set_value = w._Momentum._set_value

    class _FreezeUnderlaySurface:
        """Proxy a Quick surface and keep its QScrollArea stationary."""

        def __init__(self, original, area):
            self._original = original
            self._area = area
            self._bar = area.verticalScrollBar()
            self._pending_value = int(self._bar.value())
            self._motion = None

        @property
        def active(self):
            try:
                return bool(self._original.active)
            except (AttributeError, RuntimeError):
                return False

        def begin(self, pos):
            self._pending_value = int(round(float(pos)))
            return self._original.begin(pos)

        def present(self, pos):
            return self._original.present(pos)

        def remember(self, motion, value):
            self._motion = motion
            self._pending_value = int(value)

        def _commit(self, pos=None):
            try:
                if pos is not None:
                    value = int(round(float(pos)))
                else:
                    value = int(self._pending_value)
                value = max(self._bar.minimum(), min(self._bar.maximum(), value))
                self._bar.setValue(value)
                if self._motion is not None:
                    self._motion._last_value = value
                self._pending_value = value
            except (AttributeError, RuntimeError, TypeError, ValueError):
                pass

        def end(self, pos=None):
            # The original surface still has body updates disabled here. Commit
            # the final QWidget position under the cover, then let it re-enable,
            # repaint, and hide the Quick window.
            self._commit(pos)
            return self._original.end(pos)

        def abort(self):
            # Resize/hide/click can terminate the overlay before normal momentum
            # completion. Land the real page at the most recent requested value
            # before revealing it so there is no catch-up jump afterward.
            self._commit()
            return self._original.abort()

    def frozen_scroll_area(body, *args, **kwargs):
        area = previous_scroll_area(body, *args, **kwargs)
        try:
            bar = area.verticalScrollBar()
            surface = getattr(bar, "_atomic_quick_scroll_surface", None)
            if surface is not None and not isinstance(surface, _FreezeUnderlaySurface):
                proxy = _FreezeUnderlaySurface(surface, area)
                bar._atomic_quick_scroll_surface = proxy
                area._atomic_quick_scroll_surface = proxy
        except (AttributeError, RuntimeError):
            pass
        return area

    def set_value(self, value):
        try:
            surface = getattr(self._bar, "_atomic_quick_scroll_surface", None)
        except RuntimeError:
            surface = None
        if isinstance(surface, _FreezeUnderlaySurface) and surface.active:
            # Keep the real QWidget tree fixed. The Quick texture receives the
            # floating-point position separately from quick_scroll_patch._tick.
            surface.remember(self, value)
            return None
        return old_set_value(self, value)

    w.scroll_area = frozen_scroll_area
    w._Momentum._set_value = set_value
