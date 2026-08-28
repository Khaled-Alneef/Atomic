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

A DPR refresh rebuilds the current page from scratch, so it also snapshots every
scroll area's horizontal/vertical position and restores them around that rebuild.
Crossing monitors therefore changes image resolution only; it never navigates the
user back to the top of the page or resets a sideways shelf.
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

    def _capture_scroll_state(window):
        """All scroll positions on the visible page, in stable child order."""
        try:
            from PyQt6.QtWidgets import QAbstractScrollArea

            page = getattr(window, "_current_page", None)
            if page is None:
                return ()
            states = []
            for area in page.findChildren(QAbstractScrollArea):
                try:
                    vbar = area.verticalScrollBar()
                    hbar = area.horizontalScrollBar()
                    states.append((
                        int(vbar.value()), int(vbar.minimum()), int(vbar.maximum()),
                        int(hbar.value()), int(hbar.minimum()), int(hbar.maximum()),
                    ))
                except RuntimeError:
                    states.append(None)
            return tuple(states)
        except (AttributeError, RuntimeError):
            return ()

    def _restore_scroll_state(window, states):
        """Restore a same-page rebuild without assuming its ranges are identical."""
        if not states:
            return
        try:
            from PyQt6.QtWidgets import QAbstractScrollArea

            page = getattr(window, "_current_page", None)
            if page is None:
                return
            areas = page.findChildren(QAbstractScrollArea)
            for area, state in zip(areas, states):
                if state is None:
                    continue
                try:
                    v_value, v_old_min, v_old_max, h_value, h_old_min, h_old_max = state
                    vbar = area.verticalScrollBar()
                    hbar = area.horizontalScrollBar()

                    # Logical layout sizes normally make the ranges identical
                    # across DPR changes. If one does change, preserve the same
                    # relative position rather than clamping a formerly-valid
                    # value to an unrelated end of the new range.
                    if v_old_max > v_old_min and vbar.maximum() > vbar.minimum():
                        fraction = ((v_value - v_old_min)
                                    / float(v_old_max - v_old_min))
                        v_target = (vbar.minimum()
                                    + fraction * (vbar.maximum() - vbar.minimum()))
                    else:
                        v_target = v_value
                    if h_old_max > h_old_min and hbar.maximum() > hbar.minimum():
                        fraction = ((h_value - h_old_min)
                                    / float(h_old_max - h_old_min))
                        h_target = (hbar.minimum()
                                    + fraction * (hbar.maximum() - hbar.minimum()))
                    else:
                        h_target = h_value

                    vbar.setValue(max(vbar.minimum(),
                                      min(vbar.maximum(), int(round(v_target)))))
                    hbar.setValue(max(hbar.minimum(),
                                      min(hbar.maximum(), int(round(h_target)))))
                except RuntimeError:
                    continue
        except (AttributeError, RuntimeError):
            pass

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

            scroll_state = _capture_scroll_state(window)
            window._atomic_dpr_refresh_in_progress = True
            try:
                window.refresh_current_page()
                # The same page is constructed synchronously, so restoring here
                # prevents a visible one-frame jump to zero. Re-assert once on
                # the next event-loop turn because lazy layout/range updates may
                # finish after refresh_current_page() returns.
                _restore_scroll_state(window, scroll_state)
                try:
                    from PyQt6.QtCore import QTimer
                    QTimer.singleShot(
                        0, lambda w=window, s=scroll_state:
                        _restore_scroll_state(w, s))
                except Exception:
                    pass
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
