"""Keep image DPR correct across mixed-DPI monitors without blocking moves.

Startup adopts the realised QWindow screen before rebuilding the first page.
During monitor crossing the DPR itself is updated immediately, but the expensive
page/image refresh is deferred until the window has stopped moving.  The refresh
also preserves the page's primary vertical scroll position so changing monitors
never navigates the user back to the top.
"""

from __future__ import annotations


_PATCHED_WINDOW_CLASSES = set()
_MOVE_SETTLE_MS = 80


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

    def _primary_vertical_area(page):
        """The page-level vertical scroller, not a nested shelf/list."""
        try:
            from PyQt6.QtWidgets import QAbstractScrollArea

            candidates = []
            for area in page.findChildren(QAbstractScrollArea):
                try:
                    bar = area.verticalScrollBar()
                    span = int(bar.maximum()) - int(bar.minimum())
                    if span <= 0 or not area.isVisible():
                        continue
                    viewport = area.viewport()
                    # The page scroller owns the largest visible viewport.
                    # Nested horizontal shelves normally have no vertical
                    # range at all; list-like children are smaller.
                    score = int(viewport.width()) * int(viewport.height())
                    candidates.append((score, span, area))
                except (AttributeError, RuntimeError):
                    continue
            if not candidates:
                return None
            candidates.sort(key=lambda row: (row[0], row[1]), reverse=True)
            return candidates[0][2]
        except (AttributeError, RuntimeError):
            return None

    def _capture_page_position(window):
        try:
            page = getattr(window, "_current_page", None)
            if page is None:
                return None
            area = _primary_vertical_area(page)
            if area is None:
                return (type(page).__module__, type(page).__name__, None)
            bar = area.verticalScrollBar()
            return (
                type(page).__module__, type(page).__name__,
                (int(bar.value()), int(bar.minimum()), int(bar.maximum())),
            )
        except (AttributeError, RuntimeError):
            return None

    def _restore_page_position(window, state):
        if not state:
            return
        try:
            module, name, saved = state
            page = getattr(window, "_current_page", None)
            if page is None or type(page).__module__ != module or type(page).__name__ != name:
                return
            if saved is None:
                return
            area = _primary_vertical_area(page)
            if area is None:
                return
            bar = area.verticalScrollBar()
            old_value, old_min, old_max = saved
            new_min, new_max = int(bar.minimum()), int(bar.maximum())
            if old_max > old_min and new_max > new_min:
                fraction = (old_value - old_min) / float(old_max - old_min)
                target = new_min + fraction * (new_max - new_min)
            else:
                target = old_value
            bar.setValue(max(new_min, min(new_max, int(round(target)))))
        except (AttributeError, RuntimeError, TypeError, ValueError):
            pass

    def _restore_page_position_later(window, state):
        """Re-assert after immediate and lazy layout/range updates."""
        try:
            from PyQt6.QtCore import QTimer

            _restore_page_position(window, state)
            for delay in (0, 40, 120):
                QTimer.singleShot(
                    delay,
                    lambda w=window, s=state: _restore_page_position(w, s),
                )
        except Exception:
            _restore_page_position(window, state)

    def _refresh_scaled_page(window):
        try:
            pending = getattr(window, "_atomic_pending_move_dpr", None)
            if pending is None:
                return

            native = _native_ratio(window)
            final_ratio = float(native if native is not None else pending)
            window._atomic_pending_move_dpr = None
            original_set_device_ratio(final_ratio)
            window._atomic_verified_page_dpr = final_ratio

            try:
                images.clear_scaled_cache()
            except Exception:
                pass

            if getattr(window, "_atomic_dpr_refresh_in_progress", False):
                return

            position = _capture_page_position(window)
            window._atomic_dpr_refresh_in_progress = True
            try:
                window.refresh_current_page()
                _restore_page_position_later(window, position)
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

            # Cheap only while Windows owns the move loop.
            original_set_device_ratio(ratio)
            self._atomic_verified_page_dpr = ratio
            _schedule_post_move_refresh(self, ratio)

        def paced_move_event(self, event):
            if callable(old_move):
                old_move(self, event)
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
