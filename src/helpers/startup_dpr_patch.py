"""Make startup DPR authoritative without blocking monitor moves.

There are two separate mixed-DPI hazards in Atomic:

* MainWindow is constructed before its native QWindow is guaranteed to exist.
  main.py historically called images.set_device_ratio(window.devicePixelRatioF())
  before window.winId(); on a 125% primary + 100% secondary setup that early
  QWidget value can still describe the primary screen. Consuming the one startup
  rebuild there leaves the first page cut at the wrong DPR.
* Rebuilding the current page directly from QWindow.screenChanged blocks the
  GUI while Windows is physically moving the native window between monitors.

Startup therefore rebuilds once only after the realised QWindow has an
authoritative screen. Monitor crossing adopts the new DPR immediately but does
not rebuild synchronously. Instead, a pending image refresh is debounced by
MainWindow moveEvent: every move restarts a short timer, and only after the
window has stopped moving is the current page rebuilt at the new DPR. That keeps
the drag nonblocking while preventing old-monitor pixmaps from remaining soft
on the destination monitor.
"""

from __future__ import annotations


_PATCHED_WINDOW_CLASSES = set()
_MOVE_SETTLE_MS = 220


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

    def _refresh_scaled_page(window):
        """Re-cut the visible page for its current screen, outside a drag."""
        try:
            pending = getattr(window, "_atomic_pending_move_dpr", None)
            if pending is None:
                return

            native = _native_ratio(window)
            final_ratio = float(native if native is not None else pending)
            window._atomic_pending_move_dpr = None
            original_set_device_ratio(final_ratio)
            window._atomic_verified_page_dpr = final_ratio

            # The old-monitor variants are harmless on disk because their keys
            # include size/DPR, but process-local pixmaps and tinted assets must
            # not remain the visual source for the current page after crossing.
            try:
                images.clear_scaled_cache()
            except Exception:
                pass

            if getattr(window, "_atomic_dpr_refresh_in_progress", False):
                return
            window._atomic_dpr_refresh_in_progress = True
            try:
                window.refresh_current_page()
            finally:
                window._atomic_dpr_refresh_in_progress = False
        except RuntimeError:
            pass
        except Exception:
            try:
                window._atomic_dpr_refresh_in_progress = False
            except Exception:
                pass

    def _schedule_post_move_refresh(window, ratio):
        """Refresh only after the window stops moving across the DPI boundary."""
        try:
            from PyQt6.QtCore import QTimer

            window._atomic_pending_move_dpr = float(ratio)
            timer = getattr(window, "_atomic_move_dpr_timer", None)
            if timer is None:
                timer = QTimer(window)
                timer.setSingleShot(True)
                timer.timeout.connect(lambda w=window: _refresh_scaled_page(w))
                window._atomic_move_dpr_timer = timer
            timer.start(_MOVE_SETTLE_MS)
        except (RuntimeError, TypeError, ValueError):
            pass

    def _patch_window_class(window):
        """Make screen changes cheap, then refresh once the drag is idle."""
        cls = type(window)
        if cls in _PATCHED_WINDOW_CLASSES:
            return
        old_handler = getattr(cls, "_on_screen_changed", None)
        if not callable(old_handler):
            return

        old_move = getattr(cls, "moveEvent", None)

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

            # Cheap work only while Windows owns the move loop.
            original_set_device_ratio(ratio)
            self._atomic_verified_page_dpr = ratio
            _schedule_post_move_refresh(self, ratio)

        def paced_move_event(self, event):
            # Preserve MainWindow/QWidget's normal move handling first.
            if callable(old_move):
                old_move(self, event)
            # If a monitor crossing is waiting for its sharp-image refresh,
            # every move postpones that work. It can therefore never rebuild
            # the page in the middle of a continuous drag.
            try:
                if getattr(self, "_atomic_pending_move_dpr", None) is not None:
                    timer = getattr(self, "_atomic_move_dpr_timer", None)
                    if timer is not None:
                        timer.start(_MOVE_SETTLE_MS)
            except RuntimeError:
                pass

        cls._on_screen_changed = nonblocking_screen_changed
        cls.moveEvent = paced_move_event
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
                    native = _native_ratio(window)
                    final_ratio = float(native if native is not None else ratio)
                    original_set_device_ratio(final_ratio)
                    window._atomic_verified_page_dpr = final_ratio
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
                    window._atomic_dpr_refresh_in_progress = False

            QTimer.singleShot(0, rebuild)
        except Exception:
            window._atomic_dpr_final_rebuild_queued = False

    def authoritative_set_device_ratio(value) -> float:
        # Adopt the requested value immediately for background/image work. It
        # can still be only a bootstrap value before the native window exists.
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
                _queue_authoritative_startup(window)
                continue

            ratio = original_set_device_ratio(native)
            window._atomic_startup_dpr_rebuilt = True
            window._atomic_verified_page_dpr = float(ratio)
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
            return authoritative_set_device_ratio(screen.devicePixelRatio())
        except Exception:
            return original_device_ratio()

    images.set_device_ratio = authoritative_set_device_ratio
    images.device_ratio = launch_device_ratio
