"""Apply the high-refresh Home/Discover wheel tail to the real scroll owner.

The earlier pacing patch tried to identify these pages from QScrollBar's parent
chain. That is not a reliable page-owner path in Qt, so the special friction could
silently never activate. _Momentum is parented to _SmoothWheel, and _SmoothWheel
stores the real QScrollArea in `_area`; use that stable relationship instead.

This remains deliberately local: only vertical Home/Discover wheel motion on
200Hz+ screens gets the short tail. Movies, ordinary pages, horizontal rows and
the confirmed 160Hz path keep the app-wide profile.
"""

from __future__ import annotations

_INSTALLED = False
_HIGH_REFRESH_HZ = 200.0
_HIGH_REFRESH_FRICTION = 32.0
_DEFAULT_FRICTION = 50.0


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


def install():
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    from PyQt6.QtCore import Qt
    from . import widgets

    old_kick = widgets._Momentum.kick

    def drift_kick(self, distance_px, direction):
        try:
            owner = self.parent()
            area = getattr(owner, "_area", None)
            bar = getattr(self, "_bar", None)
            if (area is not None and bar is not None
                    and bar.orientation() == Qt.Orientation.Vertical
                    and _page_owned(area)):
                screen = area.screen()
                rate = float(screen.refreshRate()) if screen is not None else 0.0
                self.FRICTION = (_HIGH_REFRESH_FRICTION
                                 if rate >= _HIGH_REFRESH_HZ
                                 else _DEFAULT_FRICTION)
        except (AttributeError, RuntimeError, TypeError, ValueError):
            pass
        return old_kick(self, distance_px, direction)

    widgets._Momentum.kick = drift_kick
