"""Make episode auto-watched state mean 85% actually played.

The player already carries WATCHED_FRACTION = 0.85 and checks time-pos against
it. Two older shortcuts defeated that rule:

* open_player() ticked the episode watched immediately just for opening it;
* PlayerPage._on_ended() calls _check_watched(force=True), while mpv's
  end-file signal can also occur for stop/source-change paths, not only a
  natural EOF.

This patch keeps History's ordinary "opened/played" touch on open, removes the
instant watched tick, and makes every automatic watched decision obey the 85%
position threshold. When the threshold is crossed it also writes the exact
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

    def threshold_open_player(window, entry, season=None, episode=None,
                              streams=None):
        """Original player wiring, minus the instant watched tick on open."""
        host = (window.immersive_host() if hasattr(window, "immersive_host")
                else (window.centralWidget() if hasattr(window, "centralWidget")
                      else window))

        player.forget_untethered_resume()

        # Opening something should still make it appear in Watch History, but
        # "opened" is not "watched". The watched key is added only by
        # threshold_check_watched after 85% playback.
        try:
            from helpers import history
            from windows.tracker import format_episode_progress
            shown = (format_episode_progress(int(season or 0), int(episode))
                     if episode else None)
            history.touch(entry, progress=shown)
        except Exception:
            player.logs.exception("could not record the watch history")

        existing = getattr(window, "_player_page", None)
        if existing is not None:
            try:
                existing.close_player()
            except RuntimeError:
                pass

        def on_close():
            window._player_page = None

        try:
            page = player.PlayerPage(host, entry, season=season, episode=episode,
                                     streams=streams, on_close=on_close)
        except Exception:
            # Preserve the original half-built-widget cleanup contract.
            for stray in host.findChildren(player.PlayerPage):
                if not hasattr(stray, "surface"):
                    try:
                        stray.hide()
                        stray.setParent(None)
                        stray.deleteLater()
                    except RuntimeError:
                        pass
            raise

        window._player_page = page
        page.setGeometry(host.rect())
        page.show()
        page.raise_()
        player.freeze_covered(page)
        page.setFocus()
        return page

    player.open_player = threshold_open_player


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
