"""Presentation-driven scrolling for Atomic's live QWidget surfaces.

Qt explicitly warns against driving animation from QScreen.refreshRate() plus a
millisecond timer. QWindow.requestUpdate() is the platform-facing animation
clock: UpdateRequest is synchronized to display vsync where possible and adapts
when a top-level window moves between monitors.

Atomic's Qt Quick scroll compositor already advances from QQuickWindow's
frameSwapped signal, so it is left alone. This patch changes only live QWidget
scrolling (Home/Discover and any other surface with no Quick motion surface):
_Momentum ticks are registered with one scheduler per top-level QWindow and the
scheduler advances them once per UpdateRequest. The old timer remains a safe
fallback when no exposed native QWindow exists (startup/offscreen/RDP edge
cases).

The earlier high-refresh live pacing patch also wrapped _Momentum.kick to change
friction on 200+ Hz displays. That is monitor-specific drift. Unwrap that layer
here before any wheel impulse is computed so every display uses the exact same
base momentum physics.
"""

from __future__ import annotations

import weakref

_INSTALLED = False
_PATCHED = False
_schedulers = weakref.WeakKeyDictionary()


def _unwrap_monitor_specific_kick(w):
    """Remove high_refresh_live_pacing_patch's friction-changing kick wrapper."""
    current = w._Momentum.kick
    code = getattr(current, "__code__", None)
    closure = getattr(current, "__closure__", None)
    if code is None or not closure:
        return
    try:
        cells = dict(zip(code.co_freevars, closure))
        cell = cells.get("old_momentum_kick")
        base = cell.cell_contents if cell is not None else None
    except Exception:
        base = None
    if callable(base):
        w._Momentum.kick = base


def _patch_widgets(w):
    global _PATCHED
    if _PATCHED:
        return
    _PATCHED = True

    from PyQt6.QtCore import QEvent, QObject

    _unwrap_monitor_specific_kick(w)

    old_start = w._Momentum._start_ticking
    old_stop = w._Momentum._stop_ticking

    def _quick_surface(motion):
        try:
            return getattr(motion._bar, "_atomic_motion_surface", None)
        except (AttributeError, RuntimeError):
            return None

    def _window_for(motion):
        """The exposed top-level QWindow whose presentation clock owns motion."""
        try:
            bar = motion._bar
            top = bar.window()
            window = top.windowHandle() if top is not None else None
            if window is None or not window.isExposed():
                return None
            return window
        except (AttributeError, RuntimeError):
            return None

    class _WindowUpdateClock(QObject):
        """One coalesced requestUpdate loop for every top-level native window."""

        def __init__(self, window):
            super().__init__(window)
            self.window = window
            self.motions = weakref.WeakSet()
            self.request_pending = False
            window.installEventFilter(self)

        def add(self, motion):
            self.motions.add(motion)
            motion._atomic_update_clock = self
            self._request()

        def remove(self, motion):
            self.motions.discard(motion)
            try:
                if getattr(motion, "_atomic_update_clock", None) is self:
                    motion._atomic_update_clock = None
            except Exception:
                pass

        def _request(self):
            if self.request_pending or not self.motions:
                return
            try:
                if not self.window.isExposed():
                    return
                self.request_pending = True
                self.window.requestUpdate()
            except RuntimeError:
                self.request_pending = False

        def eventFilter(self, obj, event):
            if obj is self.window and event.type() == QEvent.Type.UpdateRequest:
                self.request_pending = False
                active = list(self.motions)
                for motion in active:
                    if motion not in self.motions:
                        continue
                    try:
                        motion._tick()
                    except RuntimeError:
                        self.remove(motion)
                    except Exception:
                        try:
                            w.logs.exception("presentation-driven scroll tick failed")
                        except Exception:
                            pass
                        self.remove(motion)
                if self.motions:
                    self._request()
                # Do not consume the event; QWindow still needs its normal event
                # processing for platform/update bookkeeping.
                return False
            return False

    def _clock_for(window):
        clock = _schedulers.get(window)
        if clock is None:
            clock = _WindowUpdateClock(window)
            _schedulers[window] = clock
        return clock

    def presentation_start(self):
        # Qt Quick surfaces already tick from frameSwapped. Never stack a second
        # clock on top of the compositor path.
        if _quick_surface(self) is not None:
            return old_start(self)

        window = _window_for(self)
        if window is None:
            return old_start(self)

        # Match _Momentum's normal glide accounting without starting the
        # millisecond QTimer or refresh-rate ticker.
        if not getattr(self, "_counted", False):
            self._counted = True
            w._GLIDING += 1
        try:
            self._timer.stop()
        except Exception:
            pass
        if getattr(self, "_vblank_on", False):
            try:
                ticker = w._vblank_ticker_for_use()
                if ticker is not None:
                    ticker.tick.disconnect(self._tick)
                    ticker.release()
            except Exception:
                pass
            self._vblank_on = False
        _clock_for(window).add(self)

    def presentation_stop(self):
        clock = getattr(self, "_atomic_update_clock", None)
        if clock is None:
            return old_stop(self)

        clock.remove(self)
        if getattr(self, "_counted", False):
            self._counted = False
            w._GLIDING = max(0, w._GLIDING - 1)
        try:
            self._timer.stop()
        except Exception:
            pass
        if getattr(self, "_vblank_on", False):
            try:
                ticker = w._vblank_ticker_for_use()
                if ticker is not None:
                    ticker.tick.disconnect(self._tick)
                    ticker.release()
            except Exception:
                pass
            self._vblank_on = False

    w._Momentum._start_ticking = presentation_start
    w._Momentum._stop_ticking = presentation_stop

    try:
        from . import updater
        updater.APP_VERSION = "1.10.120"
        updater._HEADERS["User-Agent"] = f"Atomic/{updater.APP_VERSION}"
    except Exception:
        pass


def install():
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    import sys
    module = sys.modules.get("helpers.widgets")
    if module is not None:
        _patch_widgets(module)
        return

    import importlib.abc
    import importlib.machinery

    class _Loader(importlib.abc.Loader):
        def __init__(self, wrapped):
            self._wrapped = wrapped

        def create_module(self, spec):
            creator = getattr(self._wrapped, "create_module", None)
            return creator(spec) if creator is not None else None

        def exec_module(self, module):
            self._wrapped.exec_module(module)
            _patch_widgets(module)

    class _Finder(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path, target=None):
            if fullname != "helpers.widgets":
                return None
            spec = importlib.machinery.PathFinder.find_spec(fullname, path)
            if spec is not None and spec.loader is not None:
                spec.loader = _Loader(spec.loader)
            return spec

    sys.meta_path.insert(0, _Finder())
