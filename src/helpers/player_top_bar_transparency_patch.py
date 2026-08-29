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

        from PyQt6.QtCore import QRegion
        from PyQt6.QtWidgets import QWidget

        layout = bar.layout()
        if layout is not None:
            layout.activate()

        region = QRegion()
        # Direct layout children are the actual visible controls/labels.  A
        # couple of pixels of air avoids clipping antialiased glyph edges.
        for child in bar.findChildren(
                QWidget, options=player.Qt.FindChildOption.FindDirectChildrenOnly):
            if not child.isVisible():
                continue
            rect = child.geometry().adjusted(-3, -2, 3, 2)
            if rect.width() > 0 and rect.height() > 0:
                region = region.united(QRegion(rect))

        if region.isEmpty():
            bar.clearMask()
        else:
            bar.setMask(region)
    except (AttributeError, RuntimeError):
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

    Page._build_top_bar = build_top_bar
    Page._layout_overlays = layout_overlays
    Page._wake_controls = wake_controls


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
