"""Keep the promoted player top bar reliably above mpv for mouse input.

The 1.10.148 fix correctly removed raise_() from every follower sync because
raising a top-level Tool on every pointer wake caused enter/leave churn.  The
missing half is that mpv owns a native D3D child and can occasionally regain the
input/z-order edge after activation, menus, fullscreen transitions or a chrome
hide/show cycle.  In that state the translucent bar can still be painted while
Windows sends the pointer to mpv, so a visible button neither hovers nor clicks.

Recover the bar only at real lifecycle boundaries and, while the pointer is over
an actual clickable top-bar child, verify the Windows hit target.  If another
HWND owns that point, raise the bar once.  There is no continuous raise storm and
no scrolling/playback/buffering change.
"""
from __future__ import annotations

import ctypes
import os
import sys

_INSTALLED = False
_PATCHED = set()


def _patch_player(module):
    key = id(module)
    if key in _PATCHED:
        return
    _PATCHED.add(key)

    from PyQt6.QtCore import QEvent, QObject, QPoint, QTimer, Qt
    from PyQt6.QtGui import QCursor
    from PyQt6.QtWidgets import QPushButton

    Page = module.PlayerPage
    old_init = Page.__init__
    old_wake = Page._wake_controls
    old_layout = Page._layout_overlays
    old_close = Page.close_player

    class _TopBarInputGuard(QObject):
        """Raise only when a lifecycle event or a real bad Windows hit demands it."""
        def __init__(self, page, owner, bar):
            super().__init__(page)
            self.page = page
            self.owner = owner
            self.bar = bar
            self._last_raise_ms = 0
            self.timer = QTimer(self)
            self.timer.setInterval(90)
            self.timer.timeout.connect(self._verify_pointer_target)
            self.timer.start()

        def _bar_hwnd(self):
            try:
                return int(self.bar.winId())
            except (AttributeError, RuntimeError, TypeError, ValueError):
                return 0

        def _raise_once(self):
            if self.page._closing or not self.bar.isVisible():
                return
            try:
                self.bar.raise_()
            except RuntimeError:
                return

        def _button_at_cursor(self):
            """Return a visible/enabled top-bar button under the global cursor."""
            try:
                pos = QCursor.pos()
                local = self.bar.mapFromGlobal(pos)
                if not self.bar.rect().contains(local):
                    return None
                child = self.bar.childAt(local)
                while child is not None and child is not self.bar:
                    if isinstance(child, QPushButton):
                        if child.isVisible() and child.isEnabled():
                            return child
                        return None
                    child = child.parentWidget()
            except (AttributeError, RuntimeError):
                return None
            return None

        def _verify_pointer_target(self):
            # Do nothing over empty title-bar space.  We only intervene when the
            # user is actually crossing a clickable control and Windows reports
            # another HWND at that point.
            if os.name != "nt" or self.page._closing or not self.bar.isVisible():
                return
            button = self._button_at_cursor()
            if button is None:
                return
            hwnd = self._bar_hwnd()
            if not hwnd:
                return
            try:
                pos = QCursor.pos()
                packed = (int(pos.y()) << 32) | (int(pos.x()) & 0xffffffff)
                user32 = ctypes.windll.user32
                user32.WindowFromPoint.argtypes = [ctypes.c_longlong]
                user32.WindowFromPoint.restype = ctypes.c_void_p
                hit = int(user32.WindowFromPoint(ctypes.c_longlong(packed)) or 0)
                if not hit:
                    return
                # Qt's button children are normally alien/non-native, so the
                # expected hit is the Tool HWND itself.  If a child ever becomes
                # native, GetAncestor(GA_ROOT) still resolves it to the bar.
                user32.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
                user32.GetAncestor.restype = ctypes.c_void_p
                hit_root = int(user32.GetAncestor(ctypes.c_void_p(hit), 2) or hit)
                bar_root = int(user32.GetAncestor(ctypes.c_void_p(hwnd), 2) or hwnd)
                if hit != hwnd and hit_root != bar_root:
                    self._raise_once()
            except Exception:
                pass

        def eventFilter(self, watched, event):
            et = event.type()
            if watched is self.owner and et in (
                    QEvent.Type.WindowActivate,
                    QEvent.Type.ActivationChange,
                    QEvent.Type.WindowStateChange,
                    QEvent.Type.Show,
                    QEvent.Type.Resize,
                    QEvent.Type.Move,
            ):
                QTimer.singleShot(0, self._raise_once)
            elif watched is self.bar and et in (
                    QEvent.Type.Show,
                    QEvent.Type.WindowActivate,
                    QEvent.Type.Enter,
            ):
                QTimer.singleShot(0, self._raise_once)
            return False

        def stop(self):
            try:
                self.timer.stop()
                self.owner.removeEventFilter(self)
                self.bar.removeEventFilter(self)
            except (AttributeError, RuntimeError):
                pass

    def _ensure_guard(self):
        bar = getattr(self, "top_bar", None)
        if (bar is None
                or not getattr(bar, "_atomic_alpha_overlay_146", False)
                or self._closing):
            return
        guard = getattr(self, "_atomic_topbar_input_guard_153", None)
        if guard is None:
            try:
                owner = self.window()
                guard = _TopBarInputGuard(self, owner, bar)
                owner.installEventFilter(guard)
                bar.installEventFilter(guard)
                self._atomic_topbar_input_guard_153 = guard
            except (AttributeError, RuntimeError):
                return
        QTimer.singleShot(0, guard._raise_once)

    def init(self, *args, **kwargs):
        old_init(self, *args, **kwargs)
        _ensure_guard(self)

    def wake_controls(self):
        result = old_wake(self)
        _ensure_guard(self)
        return result

    def layout_overlays(self):
        result = old_layout(self)
        _ensure_guard(self)
        return result

    def close_player(self):
        guard = getattr(self, "_atomic_topbar_input_guard_153", None)
        if guard is not None:
            guard.stop()
            self._atomic_topbar_input_guard_153 = None
        return old_close(self)

    Page.__init__ = init
    Page._wake_controls = wake_controls
    Page._layout_overlays = layout_overlays
    Page.close_player = close_player


def _chain_player_patch():
    try:
        from . import requested_fixes_patch as requested
        previous = requested._patch_player

        def chained(module):
            previous(module)
            _patch_player(module)

        requested._patch_player = chained
        loaded = sys.modules.get("windows.player")
        if loaded is not None:
            _patch_player(loaded)
    except Exception:
        pass


def install():
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True
    _chain_player_patch()
