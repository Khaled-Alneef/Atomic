"""Make startup DPR authoritative without blocking monitor moves.

There are two separate mixed-DPI hazards in Atomic:

* MainWindow is constructed before its native QWindow is guaranteed to exist.
  main.py historically called images.set_device_ratio(window.devicePixelRatioF())
  before window.winId(); on a 125% primary + 100% secondary setup that early
  QWidget value can still describe the primary screen. Consuming the one startup
  rebuild there leaves the first 1080p page permanently cut at the wrong DPR.
* MainWindow._on_screen_changed historically cleared image caches and rebuilt
  the whole current page synchronously from QWindow.screenChanged. Windows emits
  that signal while the native window is physically crossing monitors, so the
  rebuild blocks the GUI in the middle of the drag.

This patch keeps the cheap early DPR bootstrap, but only consumes the one startup
page rebuild after the native window has a real screen. The rebuild itself is
posted to the next event-loop turn, after winId()/windowHandle()/screen setup has
fully settled, and scaled image caches are cleared immediately before it so the
first 1080p page cannot reuse anything cut during the earlier 2K bootstrap.

It also replaces the screen-change handler before main.py's deferred signal
hookup: monitor crossing updates the global DPR immediately, but never rebuilds
the page in that callback. The next normal page rebuild uses the new DPR.
"""

from __future__ import annotations


_PATCHED_WINDOW_CLASSES = set()


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

    def _native_ratio(window):
        """The realised QWindow's screen ratio, or None until authoritative."""
        try:
            handle = window.windowHandle()
            if handle is None:
                return None
            screen = handle.screen()
            if screen is None:
                return None
            ratio = float(screen.devicePixelRatio() or 1.0)
            return ratio if ratio > 0.0 else 1.0
        except (AttributeError, RuntimeError, TypeError, ValueError):
            return None

    def _patch_window_class(window):
        """Remove the synchronous page rebuild from monitor crossing."""
        cls = type(window)
        if cls in _PATCHED_WINDOW_CLASSES:
            return
        old_handler = getattr(cls, "_on_screen_changed", None)
        if not callable(old_handler):
            return

        def nonblocking_screen_changed(self, screen):
            try:
                ratio = float(screen.devicePixelRatio() if screen is not None
                              else self.devicePixelRatioF() or 1.0)
            except (AttributeError, RuntimeError, TypeError, ValueError):
                ratio = 1.0

            previous = float(getattr(self, "_watched_ratio", ratio))
            self._watched_ratio = ratio
            try:
                self._watch_screen_dpi(screen)
            except (AttributeError, RuntimeError):
                pass

            if abs(ratio - previous) < 0.01:
                return

            # Cheap only: future image work immediately uses the new DPR.
            # Deliberately do NOT clear caches, restyle rails, or rebuild the
            # current page here. Those operations used to run synchronously
            # inside screenChanged and caused the ~0.5s mid-drag freeze.
            original_set_device_ratio(ratio)
            self._atomic_verified_page_dpr = ratio

        cls._on_screen_changed = nonblocking_screen_changed
        _PATCHED_WINDOW_CLASSES.add(cls)

    def _queue_authoritative_startup(window):
        """Retry once the native window/screen exists, without duplicate jobs."""
        if getattr(window, "_atomic_dpr_authoritative_queued", False):
            return
        window._atomic_dpr_authoritative_queued = True
        try:
            from PyQt6.QtCore import QTimer

            def adopt():
                window._atomic_dpr_authoritative_queued = False
                try:
                    ratio = _native_ratio(window)
                    if ratio is None:
                        _queue_authoritative_startup(window)
                        return
                    authoritative_set_device_ratio(ratio)
                except RuntimeError:
                    pass

            QTimer.singleShot(0, adopt)
        except Exception:
            window._atomic_dpr_authoritative_queued = False

    def _queue_final_rebuild(window, ratio):
        """Rebuild once after native screen adoption and startup settles."""
        if getattr(window, "_atomic_dpr_final_rebuild_queued", False):
            return
        window._atomic_dpr_final_rebuild_queued = True
        try:
            from PyQt6.QtCore import QTimer

            def rebuild():
                window._atomic_dpr_final_rebuild_queued = False
                try:
                    # Re-read the native screen at execution time. Windows may
                    # finish assigning the restored window between posting and
                    # running this callback.
                    native = _native_ratio(window)
                    final_ratio = float(native if native is not None else ratio)
                    original_set_device_ratio(final_ratio)
                    window._atomic_verified_page_dpr = final_ratio

                    # Startup only. This is deliberately NOT done from the
                    # screenChanged path, so dragging between monitors stays
                    # nonblocking. Clearing here prevents a 1.25 bootstrap tile
                    # from surviving into the first 1.0 page on the 1080p screen.
                    try:
                        images.clear_scaled_cache()
                    except Exception:
                        pass

                    window._atomic_dpr_refresh_in_progress = True
                    try:
                        window.refresh_current_page()
                    finally:
                        window._atomic_dpr_refresh_in_progress = False
                except RuntimeError:
                    pass
                except Exception:
                    # Startup sharpness must never make launch fail.
                    window._atomic_dpr_refresh_in_progress = False

            QTimer.singleShot(0, rebuild)
        except Exception:
            window._atomic_dpr_final_rebuild_queued = False

    def authoritative_set_device_ratio(value) -> float:
        # Adopt the requested value immediately for any background/image work.
        # It may be only a bootstrap value before the native QWindow exists.
        ratio = original_set_device_ratio(value)

        for window in _main_windows():
            _patch_window_class(window)

            if getattr(window, "_atomic_startup_dpr_rebuilt", False):
                window._atomic_verified_page_dpr = float(ratio)
                continue

            if getattr(window, "_atomic_dpr_refresh_in_progress", False):
                continue

            native = _native_ratio(window)
            if native is None:
                # Critical: do not mark the startup rebuild as consumed from
                # main.py's pre-winId() QWidget DPR. Wait for a real QWindow.
                _queue_authoritative_startup(window)
                continue

            # The QWindow is authoritative. Ignore an early caller-provided
            # ratio if it disagrees with the screen Windows actually chose.
            ratio = original_set_device_ratio(native)
            window._atomic_startup_dpr_rebuilt = True
            window._atomic_verified_page_dpr = float(ratio)

            # Do not rebuild inline in the startup call stack. main.py still
            # has native-window setup to finish; posting this one turn later
            # makes the chosen screen/DPR stable before any image is re-cut.
            _queue_final_rebuild(window, ratio)
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
            # Bootstrap only. The completed MainWindow is rebuilt later from
            # its realised QWindow.screen(), never from this cursor prediction.
            return authoritative_set_device_ratio(screen.devicePixelRatio())
        except Exception:
            return original_device_ratio()

    images.set_device_ratio = authoritative_set_device_ratio
    images.device_ratio = launch_device_ratio
