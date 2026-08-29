"""Restore the player's previous upper bar, without gray control frames.

The earlier patch made the native bar window itself translucent. On Windows that
can reveal the black mpv/native surface behind it, which is why the whole upper
strip became black. Keep the original bar paint/veil and change only the resting
frames behind its labels/buttons.
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

        # Pre-transparency-patch bar behaviour: an ordinary native child with
        # the player's own veil/fade. Do not make the HWND translucent.
        bar.setAutoFillBackground(False)
        bar.setAttribute(player.Qt.WidgetAttribute.WA_NoSystemBackground, False)
        bar.setAttribute(player.Qt.WidgetAttribute.WA_TranslucentBackground, False)
        bar.setStyleSheet(f"background: {player.theme.BG};")

        # Remove only the gray-like resting boxes behind the controls. Existing
        # text, glyphs, geometry, handlers and hover feedback stay intact.
        for label in bar.findChildren(QLabel):
            style = label.styleSheet() or ""
            label.setStyleSheet(
                style + "\nQLabel { background: transparent; border: none; }")
        for button in bar.findChildren(QPushButton):
            style = button.styleSheet() or ""
            button.setStyleSheet(
                style
                + "\nQPushButton { background: transparent; border: none; }"
                + f"\nQPushButton:hover {{ background: {player.theme.SURFACE_HOVER}; border: none; }}")
    except RuntimeError:
        pass


def _patch(player) -> None:
    global _PATCHED
    if _PATCHED:
        return
    _PATCHED = True

    Page = player.PlayerPage
    old_build = Page._build_top_bar
    old_layout = Page._layout_overlays

    def build_top_bar(self):
        old_build(self)
        _clean_controls(player, self)

    def layout_overlays(self):
        result = old_layout(self)
        # Native children can be recreated by fullscreen/screen changes.
        _clean_controls(player, self)
        return result

    Page._build_top_bar = build_top_bar
    Page._layout_overlays = layout_overlays


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
