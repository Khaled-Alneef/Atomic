"""Make the player's visible Back arrow use the same reliable path as Escape.

The button used to call close_player() directly while Escape and mouse Back go
through go_back(), which first unwinds an open panel, episode list or fullscreen
state. Keep the button as an ordinary child of the already-native top bar (the
same arrangement as the working Episodes/resolution/audio controls), but route
its click through that tested Back path and guard the press from the player's
raw mouse poll.
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
            player.use_hover_cursor(button)
        except Exception:
            pass

        # Remove only the old close_player connection. This button owns no
        # other clicked actions in the base player.
        try:
            button.clicked.disconnect()
        except Exception:
            pass

        def leave():
            if getattr(self, "_closing", False):
                return
            # The Windows raw-mouse fallback can observe the same physical
            # press after Qt delivered clicked(). Mark it as consumed before
            # changing visibility, exactly as panels/skip buttons do.
            try:
                self._guard_click()
            except Exception:
                pass
            self.go_back()

        button.clicked.connect(leave)

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
