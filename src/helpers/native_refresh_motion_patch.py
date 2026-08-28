"""Use each monitor's native refresh cadence for committed UI motion.

The shared motion helper historically treated 120 Hz as a target and converted
240 Hz into an exact 120 Hz divider. That is phase-stable, but it also means
half of a 240 Hz monitor's refreshes necessarily repeat the previous position.
The owner's direct 28 August 2026 A/B found native-refresh motion dramatically
clearer on the 2560x1440 240 Hz display, while the 1080p display must remain
adaptive rather than being hard-coded for one panel.

Keep ATOMIC_PRESENT_HZ as the diagnostic escape hatch: when it is explicitly
set, the existing exact-divider implementation still owns the decision. In
normal use, return the current widget screen's real refresh interval directly.
"""

from __future__ import annotations

import os

_INSTALLED = False


def install():
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    from . import updater, widgets

    old_present_frame_s = widgets.present_frame_s

    def native_present_frame_s(widget=None):
        # Explicit A/B requests keep the old exact-divider behavior so a
        # forced 120 Hz comparison on a 240 Hz panel remains reproducible.
        if os.environ.get("ATOMIC_PRESENT_HZ"):
            return old_present_frame_s(widget)
        return widgets.screen_frame_s(widget)

    widgets.present_frame_s = native_present_frame_s

    # Development pushes advance the third component. This module is loaded by
    # helpers/__init__.py before main imports updater from the package, so the
    # in-process build identity and update User-Agent both see 1.10.114.
    updater.APP_VERSION = "1.10.114"
    updater._HEADERS["User-Agent"] = f"Atomic/{updater.APP_VERSION}"
