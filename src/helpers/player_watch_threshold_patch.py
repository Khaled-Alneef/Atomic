"""Make episode auto-watched state mean 85% actually played.

The player already carries WATCHED_FRACTION = 0.85 and checks time-pos against
it. Two older shortcuts defeated that rule:

* open_player() ticked the episode watched immediately just for opening it;
* PlayerPage._on_ended() calls _check_watched(force=True), while mpv's
  end-file signal can also occur for stop/source-change paths, not only a
  natural EOF.

This patch keeps History's ordinary "opened/played" touch on open, turns the
instant watched tick off (player.MARK_WATCHED_ON_OPEN, one flag rather than a
second copy of open_player - see the note beside it), and makes every automatic
watched decision obey the 85% position threshold. When the threshold is crossed it also writes the exact
per-episode History tick, so unsaved Discover titles get the same watched state
as saved tracker entries.
"""

from __future__ import annotations

import importlib.abc
import importlib.machinery
import sys

_TARGET = "windows.player"
_INSTALLED = False
_PATCHED = False


def _patch(player):
    global _PATCHED
    if _PATCHED:
        return
    _PATCHED = True

    # Keep the requested rule explicit even if the base constant changes later.
    player.WATCHED_FRACTION = 0.85

    Page = player.PlayerPage
    old_check_watched = Page._check_watched

    def threshold_check_watched(self, force=False):
        """Auto-mark only after the playhead reaches 85% of the duration.

        `force` is deliberately ignored. mpv's end-file event is broader than
        natural EOF (source replacement/stop can produce it), so forcing a mark
        from that event can call an episode watched well before 85%.
        """
        before = bool(getattr(self, "_marked_watched", False))

        # The original method contains the desired duration/position test and
        # tracker progress write. Passing False makes that one rule authoritative
        # for time-pos updates *and* end-file callbacks.
        result = old_check_watched(self, False)

        crossed = (not before and bool(getattr(self, "_marked_watched", False)))
        if crossed and getattr(self, "episode", None):
            # Tracker progress is unavailable for an unsaved Discover entry, so
            # the exact History tick must be written here as the medium-neutral
            # watched truth. DetailsPage reads this same key for DONE + unblur.
            try:
                from helpers import history
                history.set_watched(
                    self.entry,
                    history.episode_key(self.season, self.episode),
                    True,
                )
            except Exception:
                player.logs.exception("could not record 85% episode watched mark")

            # If the episode bar is open, let its normal renderer observe the
            # new progress/tick immediately where possible. The details page
            # performs its own fresh read when the player closes.
            try:
                refresh = getattr(self, "_fill_episode_bar", None)
                if callable(refresh):
                    refresh()
            except Exception:
                pass
        return result

    Page._check_watched = threshold_check_watched

    # **The flag, not a second open_player.** This used to be a copy of
    # player.open_player with the instant watched tick removed, and the
    # copy is what actually ran - so every fix the original gained after
    # it was written silently never ran at all. Measured 12 September
    # 2026, reading the function the app really calls: the copy had
    # neither `web_pages.overlay_opened(page)` (so the Home document
    # under the player was never put down, and went on painting over it
    # - a native child over a non-native sibling) nor
    # `webview2_host.keyboard_to_qt(page)` (so the F11 fix of 11
    # September had never once run when the player opened over a web
    # page). That is .claude/rules/testing.md's "a wrapper that replaced
    # the patched function outright, so the fix had never run", and the
    # answer to it is one implementation with a flag on the one line
    # that differs.
    player.MARK_WATCHED_ON_OPEN = False


class _Loader(importlib.abc.Loader):
    def __init__(self, wrapped):
        self._wrapped = wrapped

    def create_module(self, spec):
        creator = getattr(self._wrapped, "create_module", None)
        return creator(spec) if creator is not None else None

    def exec_module(self, module):
        self._wrapped.exec_module(module)
        _patch(module)


class _Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname != _TARGET:
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec is None or spec.loader is None:
            return spec
        spec.loader = _Loader(spec.loader)
        return spec


def install():
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True
    module = sys.modules.get(_TARGET)
    if module is not None:
        _patch(module)
        return
    sys.meta_path.insert(0, _Finder())
