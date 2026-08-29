"""Player input/chrome fixes after 1.10.147.

Only two interaction bugs are addressed here:

1. A video click remains a click even if the pointer is moving between mouse
   down and mouse up.  The old poll rejected it through CLICK_MOVE_TOLERANCE_PX
   and then re-tested the *release* position, so moving across an overlay while
   holding the button made a valid press disappear.  We keep the press position
   as the identity of the click and exclude only the intentional window-drag
   band at the top of bare video.

2. The 1.10.146 transparent top bar is an owned top-level Tool window, but old
   player helpers still measured it as though it were a child and its follower
   raised that Tool window on every pointer wake.  That caused repeated
   enter/leave/cursor churn over the title and resolution/audio pills.  Give the
   promoted bar correct global hit geometry, an explicit arrow cursor on inert
   labels, stable pill hover styling, and geometry-only follower sync (no raise
   storm).

No playback buffering, scrolling, chapter or settings behaviour is changed.
"""
from __future__ import annotations

import ctypes
import os
import sys
import types

_INSTALLED = False
_PATCHED = set()


def _patch_player(module):
    key = id(module)
    if key in _PATCHED:
        return
    _PATCHED.add(key)

    from PyQt6.QtCore import QPoint, QRect, Qt
    from PyQt6.QtGui import QCursor

    Page = module.PlayerPage
    old_init = Page.__init__
    old_pointer_in = Page._pointer_in
    old_widget_rect = Page._widget_rect

    def _promoted_topbar(page, widget):
        return (widget is getattr(page, "top_bar", None)
                and getattr(widget, "_atomic_alpha_overlay_146", False))

    def _global_bar_rect(page, bar):
        try:
            if bar is None or not bar.isVisible():
                return QRect()
            # A promoted Tool window's geometry is already global logical
            # coordinates. mapTo(page.window()) is invalid for a top-level
            # sibling and was the reason the idle/hover code disagreed with
            # where Windows said the pointer really was.
            return QRect(bar.geometry())
        except RuntimeError:
            return QRect()

    def pointer_in(self, widget):
        if _promoted_topbar(self, widget):
            return _global_bar_rect(self, widget).contains(QCursor.pos())
        return old_pointer_in(self, widget)

    def widget_rect(self, widget):
        if _promoted_topbar(self, widget):
            return _global_bar_rect(self, widget)
        return old_widget_rect(self, widget)

    def _stabilise_topbar(self):
        bar = getattr(self, "top_bar", None)
        if bar is None or not getattr(bar, "_atomic_alpha_overlay_146", False):
            return
        try:
            # The bar itself and non-clickable identity text are ordinary arrow
            # territory. Clickable children keep their own hover-cursor filter,
            # so 1080P/JPN still correctly become a hand without the title
            # inheriting/flickering into one.
            bar.setCursor(Qt.CursorShape.ArrowCursor)
            for label_name in ("title_label", "source_label"):
                label = getattr(self, label_name, None)
                if label is not None:
                    label.setCursor(Qt.CursorShape.ArrowCursor)

            # Resting pills must remain visually bare over the video. Their
            # hover is one stable rounded fill with no border transition; the
            # old stylesheet still had a :hover accent border left underneath
            # the transparency patch, so entering/leaving rapidly repolished a
            # different rectangle around these two controls.
            pill_style = (
                f"QPushButton {{ background: transparent; color: {module.theme.TEXT};"
                f" border: none; padding: 3px 12px; font-size: 10pt;"
                f" font-weight: 700; border-radius: {module.theme.RADIUS_LG}px; }}"
                f"QPushButton:hover {{ background: {module.theme.SURFACE_HOVER};"
                f" color: {module.theme.TEXT}; border: none; }}"
                f"QPushButton:pressed {{ background: {module.theme.SURFACE_ACTIVE};"
                f" border: none; }}")
            for name in ("res_pill", "audio_pill"):
                pill = getattr(self, name, None)
                if pill is not None:
                    pill.setStyleSheet(pill_style)
        except (AttributeError, RuntimeError):
            pass

        follower = getattr(self, "_atomic_topbar_follower_146", None)
        if follower is None or getattr(follower, "_atomic_stable_148", False):
            return

        def sync(follower_self):
            """Follow geometry only; an owned Tool already stays above owner."""
            try:
                page = follower_self.page
                bar_widget = follower_self.bar
                if page._closing:
                    return
                origin = page.mapToGlobal(QPoint(0, 0))
                wanted = QRect(origin.x(), origin.y(),
                               page.width(), module.TOPBAR_HEIGHT)
                if bar_widget.geometry() != wanted:
                    bar_widget.setGeometry(wanted)
                # Deliberately NO raise_ here. The 1.10.146 follower used to
                # call SetWindowPos on every pointer movement, producing the
                # hover/cursor churn this patch exists to remove. The original
                # _wake_controls already raises once when the chrome comes back
                # from hidden, and ownership keeps it above the player after.
            except (AttributeError, RuntimeError):
                pass

        try:
            follower.sync = types.MethodType(sync, follower)
            follower._atomic_stable_148 = True
        except Exception:
            pass

    def init(self, *args, **kwargs):
        old_init(self, *args, **kwargs)
        _stabilise_topbar(self)

    def _press_in_drag_band(self, position):
        """The one moving press that is not a video click: window drag."""
        try:
            window = self.window()
            if window is None or window.isFullScreen():
                return False
            surface = self.surface
            if not surface.isVisible() or int(getattr(surface, "drag_band", 0)) <= 0:
                return False
            rect = self._widget_rect(surface)
            if rect.isEmpty() or not rect.contains(position):
                return False
            return position.y() - rect.top() <= int(surface.drag_band)
        except (AttributeError, RuntimeError):
            return False

    def poll_mouse(self):
        """Click the video regardless of pointer travel during that click."""
        if self._closing or os.name != "nt":
            return
        try:
            state = ctypes.windll.user32.GetAsyncKeyState(module._VK_LBUTTON)
        except Exception:
            return
        down = bool(state & 0x8000)
        pressed_since = bool(state & 0x0001)
        position = QCursor.pos()
        self._poll_side_buttons(position)

        if down and not self._mouse_down:
            self._mouse_down = True
            self._press_seq += 1
            self._press_pos = position
            # Identity belongs to where the button went DOWN. Do not later
            # invalidate it because the pointer kept moving while held.
            self._press_on_video = (
                self._is_active()
                and self._over_video(position)
                and not _press_in_drag_band(self, position))
            return
        if down:
            return

        if self._mouse_down:
            self._mouse_down = False
            press_position = self._press_pos or position
            if self._press_on_video:
                # Pass the press point too. _click_toggle intentionally checks
                # _over_video again for panel guards; using the release point
                # reintroduced the exact moving-cursor loss above.
                self._click_toggle(press_position)
            self._press_on_video = False
            self._press_pos = None
            return

        if pressed_since:
            # Whole click between polls: there is no press coordinate to retain,
            # so current position remains the only honest location available.
            self._press_seq += 1
            if (self._is_active() and self._over_video(position)
                    and not _press_in_drag_band(self, position)):
                self._click_toggle(position)

    Page.__init__ = init
    Page._pointer_in = pointer_in
    Page._widget_rect = widget_rect
    Page._poll_mouse = poll_mouse


def _chain_player_patch():
    # requested_fixes_patch is the common lazy-import owner. By chaining after
    # its current value we run after 1.10.146/147 whether windows.player was
    # imported before startup completed or much later.
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
