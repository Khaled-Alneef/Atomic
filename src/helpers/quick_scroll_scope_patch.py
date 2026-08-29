"""Keep the raster-snapshot Quick compositor away from widget-heavy pages.

The 165/240 Hz A/B established two separate facts:

* the Qt Quick presentation clock fixes the high-refresh cadence problem;
* moving a frozen QWidget snapshot over Home and Tracker's Discover/Saved/
  History surfaces corrupts/shakes their live card presentation.

Those pages continue to change while visible (cover delivery, hover state,
Discover batches, saved/history rebuilds), so a photograph of the QWidget tree
is the wrong rendering boundary.  Until those surfaces are represented by live
Qt Quick delegates, leave them on their normal QWidget renderer.  The separate
PosterGrid Quick compositor remains enabled; category grids are not part of the
reported corruption and keep the accepted 165/240 Hz path.

This patch is deliberately a scope guard rather than another timing tweak.  It
wraps the already-created quick-scroll surface with a lazy proxy, so the page
ancestry is checked when a wheel glide actually begins (after the QScrollArea
has been inserted into its final page hierarchy).
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

    def _is_guarded_page(widget) -> bool:
        """True for HomePage or anything derived from TrackerPage."""
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

    class _ScopedSurface:
        """Delegate to the snapshot compositor only on pages safe for it."""

        def __init__(self, original, area, body):
            self._original = original
            self._area = area
            self._body = body

        def _blocked(self):
            return (_is_guarded_page(self._area)
                    or _is_guarded_page(self._body))

        @property
        def active(self):
            if self._blocked():
                return False
            try:
                return bool(self._original.active)
            except (AttributeError, RuntimeError):
                return False

        def begin(self, pos):
            if self._blocked():
                return False
            return self._original.begin(pos)

        def present(self, pos):
            if not self._blocked():
                return self._original.present(pos)

        def end(self, pos=None):
            if not self._blocked():
                return self._original.end(pos)

        def abort(self):
            try:
                return self._original.abort()
            except (AttributeError, RuntimeError):
                return None

    def scoped_scroll_area(body, *args, **kwargs):
        area = previous_scroll_area(body, *args, **kwargs)
        try:
            bar = area.verticalScrollBar()
            surface = getattr(bar, "_atomic_quick_scroll_surface", None)
            if surface is not None and not isinstance(surface, _ScopedSurface):
                proxy = _ScopedSurface(surface, area, body)
                bar._atomic_quick_scroll_surface = proxy
                area._atomic_quick_scroll_surface = proxy
        except (AttributeError, RuntimeError):
            pass
        return area

    w.scroll_area = scoped_scroll_area
