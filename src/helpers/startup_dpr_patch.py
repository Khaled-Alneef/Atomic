"""Make the first page use the window's authoritative device ratio.

MainWindow builds its first page before main.py can ask the finished QWindow
which screen/DPR it actually owns.  Predicting that screen from the cursor was
not reliable: Windows can restore the window on a different monitor.  Keep a
cheap bootstrap for early image work, but the real fix is to rebuild the first
page once main.py calls images.set_device_ratio(window.devicePixelRatioF()).
That is the same rebuild that navigation was already proving makes the cards
sharp.
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
        ratio = original_set_device_ratio(value)

        # During MainWindow.__init__ no finished current page exists yet, so
        # early calls simply establish the ratio.  The explicit call in
        # main.py immediately after MainWindow() returns finds the completed
        # first page and rebuilds it before that stale artwork is allowed to
        # become the long-lived startup page.
        for window in _main_windows():
            previous = getattr(window, "_atomic_verified_page_dpr", None)
            needs_refresh = (previous is None
                             or abs(float(previous) - float(ratio)) > 1e-6)
            if not needs_refresh or getattr(
                    window, "_atomic_dpr_refresh_in_progress", False):
                continue
            window._atomic_verified_page_dpr = float(ratio)
            window._atomic_dpr_refresh_in_progress = True
            try:
                window.refresh_current_page()
            except Exception:
                # DPI adoption must never make startup fail.  The normal page
                # navigation path can still rebuild later if a page is in the
                # middle of teardown for some unrelated reason.
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
            # This is only a bootstrap.  The finished QWindow call later is
            # authoritative and will rebuild the first page if necessary.
            return authoritative_set_device_ratio(screen.devicePixelRatio())
        except Exception:
            return original_device_ratio()

    images.set_device_ratio = authoritative_set_device_ratio
    images.device_ratio = launch_device_ratio
