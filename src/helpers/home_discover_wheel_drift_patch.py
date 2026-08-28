"""Restore Atomic's proven historical drifting wheel profile on high-refresh Home/Discover.

The repo already contained the wheel behavior the owner is asking for. At commit
b805c8c5, _Momentum used friction 24, ramp 40, max speed 3200 and fed the whole
wheel impulse through the pending/ramp path. That profile was explicitly chosen
because the owner wanted the glide to carry farther after the wheel stopped.

Later the app adopted the Reader's drift-free profile globally: friction 50,
ramp 60, unlimited max speed, and eventually a 34% immediate impulse share. The
recent attempts to fake drift on top of that profile were therefore fighting the
newer physics rather than restoring the old behavior.

Apply the old proven profile only to vertical Home/Discover wheel motion on
200Hz+ displays. Movies, every other page, horizontal rows, and the confirmed
160Hz path stay on the current profile.
"""

from __future__ import annotations

import math

_INSTALLED = False
_HIGH_REFRESH_HZ = 200.0

# Proven drifting profile from src/helpers/widgets.py at b805c8c5.
_OLD_FRICTION = 24.0
_OLD_RAMP = 40.0
_OLD_MAX_SPEED = 3200.0
_OLD_IMMEDIATE_SHARE = 0.0

# Current app-wide drift-free profile, restored immediately on non-target paths.
_CURRENT_FRICTION = 50.0
_CURRENT_RAMP = 60.0
_CURRENT_MAX_SPEED = math.inf
_CURRENT_IMMEDIATE_SHARE = 0.34


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

    from PyQt6.QtCore import Qt
    from . import widgets

    old_kick = widgets._Momentum.kick

    def historical_drift_kick(self, distance_px, direction):
        try:
            owner = self.parent()
            area = getattr(owner, "_area", None)
            bar = getattr(self, "_bar", None)
            target = (area is not None and bar is not None
                      and bar.orientation() == Qt.Orientation.Vertical
                      and _high_refresh_area(area))
            if target:
                self.FRICTION = _OLD_FRICTION
                self.RAMP = _OLD_RAMP
                self.MAX_SPEED = _OLD_MAX_SPEED
                self.IMMEDIATE_SHARE = _OLD_IMMEDIATE_SHARE
            elif area is not None and _page_owned(area):
                # Same Home/Discover window moved back to the 160Hz monitor:
                # restore today's profile before handling the next notch.
                self.FRICTION = _CURRENT_FRICTION
                self.RAMP = _CURRENT_RAMP
                self.MAX_SPEED = _CURRENT_MAX_SPEED
                self.IMMEDIATE_SHARE = _CURRENT_IMMEDIATE_SHARE
        except (AttributeError, RuntimeError, TypeError, ValueError):
            pass
        return old_kick(self, distance_px, direction)

    widgets._Momentum.kick = historical_drift_kick
