"""Make the player's visible Back arrow leave fullscreen playback directly.

Outside fullscreen, the button still uses go_back() so an open player panel or
episode list unwinds through the existing tested path. In fullscreen, however,
the visible Back arrow is an explicit request to leave the player: it calls
close_player() immediately instead of spending the first press only leaving
fullscreen.

The raw Windows mouse poll is guarded before either route so the physical click
cannot be observed a second time after Qt delivers clicked().
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

        # This button owns only the base close_player connection.
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

            window = getattr(self, "_window", None)
            try:
                fullscreen = bool(window is not None and window.isFullScreen())
            except Exception:
                fullscreen = False

            if fullscreen:
                # Do not consume a press merely restoring the app window.
                # The player owns fullscreen, so Back leaves the player now;
                # close_player() performs the normal player teardown/restore.
                self.close_player()
            else:
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
