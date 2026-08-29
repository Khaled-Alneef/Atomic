"""Make the player's visible Back button a reliable native control.

The video and the top bar are native child windows on Windows. The exit glyph
was left as a non-native child inside that native bar, while the rest of the
player relies on native stacking to stay above mpv. Promote the glyph itself to
a native child and route it through go_back(), the same unwind rule as Escape
and mouse button 4.

The process-isolated backend also makes close teardown bounded, so a click can
never sit on the UI thread waiting indefinitely for libmpv to terminate.
"""

from __future__ import annotations

import sys

_TARGET = "windows.player"
_INSTALLED = False
_PATCHED = False


def _patch(player):
    global _PATCHED
    if _PATCHED:
        return
    _PATCHED = True

    Page = player.PlayerPage
    old_build = Page._build_top_bar

    def build_top_bar(self):
        old_build(self)
        button = getattr(self, "exit_btn", None)
        if button is None:
            return

        try:
            player._make_native(button)
        except Exception:
            pass
        try:
            player.use_hover_cursor(button)
        except Exception:
            pass

        try:
            button.clicked.disconnect()
        except Exception:
            pass

        def leave():
            if getattr(self, "_closing", False):
                return
            try:
                self._guard_click()
            except Exception:
                pass
            self.go_back()

        button.clicked.connect(leave)
        try:
            button.raise_()
        except Exception:
            pass

    Page._build_top_bar = build_top_bar


def install():
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
