"""Make the first page use the window's authoritative device ratio.

MainWindow builds its first page before main.py can ask the finished QWindow
which screen/DPR it actually owns. Predicting that screen from the cursor was
not reliable: Windows can restore the window on a different monitor. Keep a
cheap bootstrap for early image work, but rebuild the completed first page once
when main.py adopts the finished window's DPR.

That rebuild is intentionally startup-only. images.py already defines the
runtime monitor-switch policy: update the global DPR immediately, let the
currently-live page remain as-is, and let its next normal rebuild create tiles
for the new monitor. Rebuilding the whole active page synchronously from the
QWindow.screenChanged callback blocks the GUI while Windows is moving the
window between monitors, which is exactly the visible half-second "stuck in the
middle" hitch this patch must avoid.
"""

from __future__ import annotations


def install():
    from . import images

    original_device_ratio = images.device_ratio
    original_set_device_ratio = images.set_device_ratio

    def _main_windows():
        try:
            from PyQt6.QtCore import QThread
            from PyQt6.QtWidgets import QApplication

            app = QApplication.instance()
            if app is None or QThread.currentThread() is not app.thread():
                return ()
            return tuple(
                window for window in QApplication.topLevelWidgets()
                if callable(getattr(window, "refresh_current_page", None))
                and getattr(window, "_current_page", None) is not None
            )
        except Exception:
            return ()

    def authoritative_set_device_ratio(value) -> float:
        # Always adopt the new monitor ratio immediately. This operation is
        # cheap and is what future image/tile work should use.
        ratio = original_set_device_ratio(value)

        # Only the first authoritative DPR adoption after MainWindow finished
        # construction needs a synchronous rebuild. That fixes the confirmed
        # first-launch 1080p blur. Later screenChanged calls happen while the
        # user may be physically dragging the native window; rebuilding the
        # entire page there can stall the move for hundreds of milliseconds.
        for window in _main_windows():
            if getattr(window, "_atomic_startup_dpr_rebuilt", False):
                # Keep this informational value current without touching the
                # live widget tree. The next normal page rebuild will consume
                # images.device_ratio() and create new-monitor tiles.
                window._atomic_verified_page_dpr = float(ratio)
                continue

            if getattr(window, "_atomic_dpr_refresh_in_progress", False):
                continue

            window._atomic_startup_dpr_rebuilt = True
            window._atomic_verified_page_dpr = float(ratio)
            window._atomic_dpr_refresh_in_progress = True
            try:
                window.refresh_current_page()
            except Exception:
                # DPI adoption must never make startup fail. The normal page
                # navigation path can still rebuild later if a page happens to
                # be tearing down for an unrelated reason.
                pass
            finally:
                window._atomic_dpr_refresh_in_progress = False
        return ratio

    def launch_device_ratio() -> float:
        if getattr(images, "_ratio", None) is not None:
            return original_device_ratio()
        try:
            from PyQt6.QtCore import QThread
            from PyQt6.QtGui import QCursor, QGuiApplication

            app = QGuiApplication.instance()
            if app is None or QThread.currentThread() is not app.thread():
                return original_device_ratio()
            screen = QGuiApplication.screenAt(QCursor.pos())
            if screen is None:
                screen = app.primaryScreen()
            if screen is None:
                return 1.0
            # Bootstrap only. During MainWindow construction no completed
            # current page exists, so the finished QWindow call later is the
            # one that performs the single startup rebuild.
            return authoritative_set_device_ratio(screen.devicePixelRatio())
        except Exception:
            return original_device_ratio()

    images.set_device_ratio = authoritative_set_device_ratio
    images.device_ratio = launch_device_ratio
