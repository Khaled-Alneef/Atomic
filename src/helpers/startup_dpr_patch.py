"""Pick the correct monitor DPI before MainWindow finishes constructing.

main.py adopts the real window DPR immediately after MainWindow() returns, but
that constructor already builds the first page.  If the app is launched on a
non-primary monitor, those first covers can therefore be cut for the primary
screen and stay soft until the page is rebuilt.

Before main.py has an authoritative window ratio, prefer the screen under the
launch cursor.  This is only a bootstrap fallback: the existing
images.set_device_ratio(window.devicePixelRatioF()) call and screenChanged
connection still take over as soon as the QWindow exists.
"""

from __future__ import annotations


def install():
    from . import images

    original_device_ratio = images.device_ratio

    def launch_device_ratio() -> float:
        if getattr(images, "_ratio", None) is not None:
            return original_device_ratio()
        try:
            from PyQt6.QtCore import QThread
            from PyQt6.QtGui import QCursor, QGuiApplication

            app = QGuiApplication.instance()
            # GUI objects must not be queried from cover-fetch worker threads.
            # If a worker gets here first, retain images.py's safe primary-
            # screen fallback; the UI thread will establish the launch ratio.
            if app is None or QThread.currentThread() is not app.thread():
                return original_device_ratio()
            screen = QGuiApplication.screenAt(QCursor.pos())
            if screen is None:
                screen = app.primaryScreen()
            if screen is None:
                return 1.0
            return images.set_device_ratio(screen.devicePixelRatio())
        except Exception:
            return original_device_ratio()

    images.device_ratio = launch_device_ratio
