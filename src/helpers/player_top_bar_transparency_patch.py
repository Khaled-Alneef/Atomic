"""Remove only the player's full-width top-bar frame/background.

The title, resolution/source pill, audio/subtitle controls and buttons keep their
existing widgets, styles, geometry, hover behavior and click handlers. Only the
native top-bar container stops painting the dark veil behind them.

The player uses native child windows above mpv, so the normal bar veil also
changes the opacity of every child. Bypass that veil for the top bar only;
controls at the bottom keep their existing DWM translucency unchanged.
"""

from __future__ import annotations

import sys

_TARGET = "windows.player"
_INSTALLED = False
_PATCHED = False


def _clear_bar(player, page) -> None:
    bar = getattr(page, "top_bar", None)
    if bar is None:
        return
    try:
        bar.setObjectName("AtomicPlayerTransparentTopBar")
        bar.setAutoFillBackground(False)
        bar.setAttribute(player.Qt.WidgetAttribute.WA_NoSystemBackground, True)
        bar.setAttribute(player.Qt.WidgetAttribute.WA_TranslucentBackground, True)
        # Object selector is deliberate: a bare `background: transparent`
        # stylesheet on the parent can cascade into its buttons. The request is
        # to change the frame only, not any button/label appearance.
        bar.setStyleSheet(
            "QWidget#AtomicPlayerTransparentTopBar {"
            " background: transparent; border: none;"
            "}"
        )
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
    old_veil = Page._veil

    def build_top_bar(self):
        old_build(self)
        _clear_bar(player, self)

    def layout_overlays(self):
        result = old_layout(self)
        # Qt can recreate a native child HWND after a screen/fullscreen change.
        # Reassert only the container's transparent paint state afterwards.
        _clear_bar(player, self)
        return result

    def veil(self, widget, alpha):
        if widget is getattr(self, "top_bar", None):
            # The old low-alpha veil was what made the full-width dark strip.
            # Keep the container at full opacity so its existing labels/buttons
            # retain their original appearance; its own background paints none.
            return old_veil(self, widget, 255)
        return old_veil(self, widget, alpha)

    Page._build_top_bar = build_top_bar
    Page._layout_overlays = layout_overlays
    Page._veil = veil


def install() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    loaded = sys.modules.get(_TARGET)
    if loaded is not None:
        _patch(loaded)
        return

    # Chain after the established watched-threshold/back-button import hook;
    # do not add a second meta-path finder for windows.player.
    from . import player_watch_threshold_patch as threshold

    previous = threshold._patch

    def chained(module):
        previous(module)
        _patch(module)

    threshold._patch = chained
