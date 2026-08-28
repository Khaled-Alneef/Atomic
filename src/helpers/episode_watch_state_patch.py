"""Episode-list watched-state fixes.

Two deliberately narrow corrections for windows.details:

1. Episode still spoiler blur follows the row's real watched state. When the
   Watching setting enables still blur, unwatched/upcoming episode stills use
   the blurred cache and watched episodes use the sharp cache immediately.
2. A single out-of-order watched mark must never manufacture contiguous
   progress. In particular, clearing an explicit SxxE08 mark must not write
   progress=SxxE07 and thereby make episode 7 appear watched.

Kept as a post-import patch so the large details page stays untouched while the
behaviour is easy to remove/merge later.
"""

from __future__ import annotations

import importlib.abc
import importlib.machinery
import sys

_TARGET = "windows.details"
_INSTALLED = False
_PATCHED = False


def _patch(details):
    global _PATCHED
    if _PATCHED:
        return
    _PATCHED = True

    Page = details.DetailsPage
    old_row_card = Page._row_card

    def watched_row_card(self, title, date_text, badge, on_click, on_menu=None,
                         variant="chapter", still_url=None):
        """Expose an episode row's watched state only while its still is built."""
        previous = getattr(self, "_atomic_building_episode_watched", None)
        is_episode = variant == "episode"
        self._atomic_building_episode_watched = bool(
            is_episode and badge and badge[0] == "watched")
        try:
            return old_row_card(
                self, title, date_text, badge, on_click,
                on_menu=on_menu, variant=variant, still_url=still_url)
        finally:
            if previous is None:
                try:
                    del self._atomic_building_episode_watched
                except AttributeError:
                    pass
            else:
                self._atomic_building_episode_watched = previous

    def watched_still_tile(self, url):
        """Blur only an unwatched episode; a watched row always gets sharp art."""
        tile = details._glyph_tile(
            details.ICON_PLAY_GLYPH, details.STILL_SIZE, details.theme.RADIUS,
            point_size=13.0, accent=False)
        if not url:
            return tile

        watched = bool(getattr(self, "_atomic_building_episode_watched", False))
        # Preserve Settings > Watching as the master spoiler-blur switch, but
        # never blur an episode the page already considers watched.
        blur = bool(getattr(self, "_blur_stills", False) and not watched)
        ready = details._STILL_READY.get((url, blur))
        if ready:
            self._draw_still(tile, ready)
            return tile

        key = self._still_key
        self._still_key += 1
        self._still_tiles[key] = tile
        details.lookup_pool.submit(
            details._still_worker, self._signals, key, url, blur)
        return tile

    def _single_mark_progress_target(self, season, episode,
                                     watched_season, watched_episode):
        """Advance saved progress only when this mark is the next contiguous ep."""
        season = int(season or 0)
        episode = int(episode or 0)
        watched_season = int(watched_season or 0)
        watched_episode = int(watched_episode or 0)

        # A brand-new contiguous run starts at S01E01. Specials/out-of-order
        # marks remain explicit History ticks rather than pretending all prior
        # material was watched.
        if not watched_episode:
            return (season, episode) if season == 1 and episode == 1 else None

        if season == watched_season and episode == watched_episode + 1:
            return season, episode

        # Crossing a season boundary is contiguous only when the stored season
        # really reached its final aired episode.
        if season == watched_season + 1 and episode == 1:
            last = self._aired_last_episode(watched_season)
            if last > 0 and watched_episode >= last:
                return season, episode
        return None

    def fixed_episode_menu(self, event, season, episode):
        """Right-click episode actions without inventing watched gaps."""
        try:
            from windows.tracker import correct_progress
        except ImportError:                             # pragma: no cover
            return

        season = int(season or 0)
        episode = int(episode or 0)
        watched_season, watched_episode = self._progress()
        key = details.history.episode_key(season, episode)
        progress_covers = bool(
            watched_episode
            and (watched_season, watched_episode) >= (season, episode))
        explicitly_watched = key in self._history_marks
        already = progress_covers or explicitly_watched

        menu = details.QMenu(self)
        pick_source = menu.addAction("Choose Source...")
        menu.addSeparator()
        mark = menu.addAction("Mark as Unwatched" if already
                              else "Mark as Watched")
        menu.addSeparator()
        mark_all = menu.addAction("Mark All as Watched")
        clear_all = menu.addAction("Mark All as Unwatched")
        chosen = menu.exec(event.globalPosition().toPoint())

        if chosen is pick_source:
            self._open_source_picker(season, episode)
            return

        target = None
        if chosen is mark:
            watched = not already
            episodes = [episode]
            if already:
                # The bug: the old code always set target=episode-1 here.
                # If E08 was watched only by an explicit History tick while E07
                # was unwatched, clearing E08 wrote progress=E07 and *created*
                # a watched E07. Only lower progress when progress itself was
                # what made this row watched.
                if progress_covers:
                    if episode > 1:
                        target = (season, episode - 1)
                    elif season > 1:
                        target = (season - 1,
                                  self._aired_last_episode(season - 1))
                    else:
                        target = (0, 0)
            else:
                # Likewise, marking E08 while progress is E05 should not make
                # E06/E07 watched. Keep it as an explicit tick unless it is the
                # next contiguous episode.
                target = _single_mark_progress_target(
                    self, season, episode, watched_season, watched_episode)
        elif chosen is mark_all:
            watched, episodes = True, self._season_episodes(season)
            target = (season, max(1, self._aired_last_episode(season)))
        elif chosen is clear_all:
            watched, episodes = False, self._season_episodes(season)
            target = ((season - 1, self._aired_last_episode(season - 1))
                      if season > 1 else (0, 0))
        else:
            return

        # History is the exact per-episode truth, including out-of-order marks.
        self._mark_history(
            [details.history.episode_key(season, number) for number in episodes],
            watched)

        if self.entry.get("id") and target is not None:
            if target[1] <= 0:
                self._clear_video_progress()
            elif not correct_progress(
                    self.entry, season=target[0], episode=target[1]):
                details.show_toast(self, "Could Not Save That")
                return

        # Rebuild immediately. This updates both DONE and the still path:
        # watched -> sharp, unwatched -> blurred.
        self._fill_rows()

    Page._row_card = watched_row_card
    Page._still_tile = watched_still_tile
    Page._episode_menu = fixed_episode_menu


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
