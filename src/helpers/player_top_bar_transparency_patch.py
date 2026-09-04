"""Keep the player's upper controls without a full-width frame over video.

A native child is required for these controls to sit above mpv.  A full native
window, however, also paints its rectangular background over the picture.  That
is harmless while the loading backdrop is up (the colours match) and becomes the
visible dark frame reported once a real video frame appears.

Keep the loading appearance untouched.  Once playback is live, clip the top-bar
HWND to the controls that actually paint.  The title/source labels remain useful
drag targets because their mouse presses still bubble to DragStrip; the empty
full-width rectangle simply stops covering the video.  No player timing or
scrolling code is touched here.
"""

from __future__ import annotations

import sys

_TARGET = "windows.player"
_INSTALLED = False
_PATCHED = False


def _clean_controls(player, page) -> None:
    bar = getattr(page, "top_bar", None)
    if bar is None:
        return
    try:
        from PyQt6.QtWidgets import QLabel, QPushButton

        # Keep the proven native child path.  Do not use
        # WA_TranslucentBackground here: a Qt-translucent native child blends
        # against Qt's backing store, not mpv's separately composed swapchain.
        bar.setAutoFillBackground(False)
        bar.setAttribute(player.Qt.WidgetAttribute.WA_NoSystemBackground, False)
        bar.setAttribute(player.Qt.WidgetAttribute.WA_TranslucentBackground, False)
        bar.setStyleSheet(f"background: {player.theme.BG}; border: none;")

        # Controls themselves do not need the generic gray button/label frames.
        for label in bar.findChildren(QLabel):
            style = label.styleSheet() or ""
            if "AtomicTopBarClean" not in style:
                label.setStyleSheet(
                    style
                    + "\n/* AtomicTopBarClean */"
                    + "\nQLabel { background: transparent; border: none; }")
        for button in bar.findChildren(QPushButton):
            style = button.styleSheet() or ""
            if "AtomicTopBarClean" not in style:
                button.setStyleSheet(
                    style
                    + "\n/* AtomicTopBarClean */"
                    + "\nQPushButton { background: transparent; border: none; }"
                    + f"\nQPushButton:hover {{ background: {player.theme.SURFACE_HOVER}; border: none; }}")
    except RuntimeError:
        pass


def _clip_live_bar(player, page) -> None:
    """Remove only the empty rectangle once a video frame is live.

    Masking is deliberately used instead of Qt translucency or a colour key.
    Both were already proven unreliable for a native child above mpv.  A mask is
    binary and therefore cannot turn into black pixels on HDR/mixed-DPI paths.
    """
    bar = getattr(page, "top_bar", None)
    if bar is None:
        return
    try:
        # Loading is already visually right: leave the whole bar alone there.
        if getattr(page, "_awaiting_first_frame", True):
            bar.clearMask()
            return

        from PyQt6.QtGui import QRegion
        from PyQt6.QtWidgets import QWidget

        layout = bar.layout()
        if layout is not None:
            layout.activate()

        region = QRegion()
        # Only immediate children belong to the top-bar layout.  children()
        # avoids relying on a PyQt findChildren keyword signature that differs
        # between Qt point releases.
        #
        # isVisibleTo(bar), never isVisible(): Qt visibility is
        # conjunctive, so while the bar itself is hidden (controls
        # asleep) every child reports False and the region came out
        # empty - the clip then only ever held if it happened to be
        # recomputed while the bar was on screen. Parent-relative
        # visibility answers "will this child show when the bar does",
        # which is the question the mask is asking.
        for child in bar.children():
            if not isinstance(child, QWidget) or not child.isVisibleTo(bar):
                continue
            rect = child.geometry().adjusted(-3, -2, 3, 2)
            if rect.width() > 0 and rect.height() > 0:
                region = region.united(QRegion(rect))

        if region.isEmpty():
            bar.clearMask()
        else:
            bar.setMask(region)
    except (AttributeError, RuntimeError, TypeError):
        pass


def _sink_bar(player, page) -> None:
    """Take the bar out of the hit test as well as out of sight.

    **The owner, 3 September 2026:** *"the vid player sometimes hide the
    upper bar but still I can click on the button places!!!!"*

    The mask this module sets is what makes that possible. Once video is
    live the bar's HWND is clipped to the union of its controls' rects,
    so the empty full-width rectangle stops covering the picture - and a
    mask on a native child is `SetWindowRgn`, which clips **painting and
    hit-testing together**. That is right while the bar is up. It is
    wrong the moment `_hide_controls` puts the bar away, because the
    region is a property of the window and survives the hide: anything
    that shows that HWND again - a layout pass, a screen change, Qt
    recreating the native child - brings back a bar that paints only
    its controls, over a picture, where nothing looks like a bar.

    So hiding drops the region and hides the handle explicitly rather
    than trusting the widget-level hide alone. `_wake_controls` already
    recomputes the mask on the way back up (see the patch below), so
    nothing is lost by clearing it here.
    """
    bar = getattr(page, "top_bar", None)
    if bar is None:
        return
    try:
        # **The region only. Qt owns whether the window is up.** The
        # first version also called ShowWindow(SW_HIDE) on the handle as
        # belt and braces, and belt and braces on a native child Qt is
        # also managing is how a bar ends up stuck down - the owner, 4
        # September 2026: reloading from inside the player "hides the
        # upper bar and freeze". Dropping the region is the part that
        # actually fixes the click-through, because a region set with
        # SetWindowRgn survives the hide and comes back with the window;
        # hiding the handle behind Qt's back fixes nothing it had not
        # already done.
        bar.clearMask()
    except (AttributeError, RuntimeError, OSError):
        pass


def _patch(player) -> None:
    global _PATCHED
    if _PATCHED:
        return
    _PATCHED = True

    Page = player.PlayerPage
    old_build = Page._build_top_bar
    old_layout = Page._layout_overlays
    old_wake = Page._wake_controls
    old_hide = Page._hide_controls

    def build_top_bar(self):
        old_build(self)
        _clean_controls(player, self)
        _clip_live_bar(player, self)

    def layout_overlays(self):
        result = old_layout(self)
        # Native children can be recreated by fullscreen/screen changes.
        _clean_controls(player, self)
        _clip_live_bar(player, self)
        return result

    def wake_controls(self):
        result = old_wake(self)
        # The first pointer/control wake after frame 1 is the transition that
        # used to expose the full-width dark rectangle.
        _clean_controls(player, self)
        _clip_live_bar(player, self)
        return result

    def hide_controls(self):
        result = old_hide(self)
        # Only when the hide actually happened - _hide_controls returns
        # early while the pointer is on the bar, the episode list is up,
        # a status is showing, or playback is paused.
        bar = getattr(self, "top_bar", None)
        if bar is not None and bar.isHidden():
            _sink_bar(player, self)
        return result

    Page._build_top_bar = build_top_bar
    Page._layout_overlays = layout_overlays
    Page._wake_controls = wake_controls
    Page._hide_controls = hide_controls


def install() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    loaded = sys.modules.get(_TARGET)
    if loaded is not None:
        _patch(loaded)
        return

    from . import player_watch_threshold_patch as threshold
    previous = threshold._patch

    def chained(module):
        previous(module)
        _patch(module)

    threshold._patch = chained
