"""Episode/chapter watched-state fixes.

This patch keeps the details list's visible marks, History ticks and saved
progress in one contiguous model:

* episode still spoiler blur follows the row's real watched state;
* marking an episode/chapter watched marks everything before it watched too;
* marking an episode/chapter unwatched clears that item and everything after it;
* the saved progress boundary moves to the same place as those visible marks.

The range is built from the rows the details page actually knows about, rather
than assuming episode/chapter numbers are gapless.
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
        # Through the page's own queueing (the cover queue, batched per
        # row build) - see DetailsPage._submit_stills for the measurement.
        self._queue_still(key, url, blur)
        return tile

    def _episode_pairs(self):
        """Every real episode known to this page, in playback order."""
        pairs = set()
        for video in getattr(self, "_videos", ()) or ():
            try:
                season = int(video.get("season") or 0)
                episode = int(video.get("number") or video.get("episode") or 0)
            except (AttributeError, TypeError, ValueError):
                continue
            if episode > 0:
                pairs.add((season, episode))
        return sorted(pairs)

    def _chapter_numbers(self):
        """Every real chapter number known to this page, numerically sorted."""
        numbers = set()
        for chapter in getattr(self, "_chapters", ()) or ():
            try:
                number = details.chapter_number(chapter)
            except Exception:
                number = None
            if number is not None:
                try:
                    numbers.add(float(number))
                except (TypeError, ValueError):
                    pass
        return sorted(numbers)

    def fixed_episode_menu(self, event, season, episode):
        """Right-click episode actions with the clicked row as a boundary.

        Mark watched => every known episode through this one is watched.
        Mark unwatched => this episode and every known episode after it is clear.
        """
        try:
            from windows.tracker import correct_progress
        except ImportError:                             # pragma: no cover
            return

        season = int(season or 0)
        episode = int(episode or 0)
        clicked = (season, episode)
        watched_season, watched_episode = self._progress()
        key = details.history.episode_key(season, episode)
        # **The ticks win.** details._episode_menu carries this rule and
        # this wrapper replaces it at import (helpers/_ui_startup), so
        # the rule has to live here too: once any episode carries an
        # explicit mark, the marks are the truth and the progress number
        # is not consulted. The OR that stood here read a later episode
        # as watched off the progress and offered "Mark as Unwatched"
        # for one the owner had ticked off (review, 3 September 2026:
        # DetailsPage._episode_menu resolves to this function).
        if self._history_marks:
            already = key in self._history_marks
        else:
            already = bool(watched_episode
                           and (watched_season, watched_episode) >= clicked)

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

        pairs = _episode_pairs(self)
        if clicked not in pairs and episode > 0:
            pairs = sorted(set(pairs + [clicked]))

        target = None
        if chosen is mark:
            watched = not already
            if watched:
                affected = [pair for pair in pairs if pair <= clicked]
                target = clicked
            else:
                affected = [pair for pair in pairs if pair >= clicked]
                earlier = [pair for pair in pairs if pair < clicked]
                target = earlier[-1] if earlier else (0, 0)
        elif chosen is mark_all:
            # Preserve the existing menu meaning: all episodes in this season.
            watched = True
            affected = [(season, number)
                        for number in self._season_episodes(season)]
            last = max((pair for pair in affected), default=None)
            target = last
        elif chosen is clear_all:
            # Preserve the existing menu meaning: clear this entire season.
            watched = False
            affected = [(season, number)
                        for number in self._season_episodes(season)]
            earlier = [pair for pair in pairs if pair[0] < season]
            target = earlier[-1] if earlier else (0, 0)
        else:
            return

        # History carries exact per-item truth, including unsaved Discover
        # entries which have no saved progress record at all.
        self._mark_history(
            [details.history.episode_key(s, e) for s, e in affected], watched)

        if self.entry.get("id") and target is not None:
            if target[1] <= 0:
                self._clear_video_progress()
            elif not correct_progress(
                    self.entry, season=target[0], episode=target[1]):
                details.show_toast(self, "Could Not Save That")
                return

        # Rebuild immediately. This updates DONE plus watched -> sharp /
        # unwatched -> blurred still artwork.
        self._fill_rows()

    def fixed_chapter_menu(self, event, number):
        """Right-click chapter actions with the clicked row as a boundary.

        Mark read => every known chapter through this one is read.
        Mark unread => this chapter and every known chapter after it is clear.
        """
        try:
            from windows.tracker import correct_progress
        except ImportError:                             # pragma: no cover
            return

        try:
            number = float(number)
        except (TypeError, ValueError):
            return

        last_read = float(self._last_read() or 0.0)
        key = details.history.chapter_key(number)
        already = bool(last_read >= number or key in self._history_marks)

        menu = details.QMenu(self)
        mark = menu.addAction("Mark as Unread" if already else "Mark as Read")
        menu.addSeparator()
        mark_all = menu.addAction("Mark All as Read")
        clear_all = menu.addAction("Mark All as Unread")
        chosen = menu.exec(event.globalPosition().toPoint())

        numbers = _chapter_numbers(self)
        if number not in numbers:
            numbers = sorted(set(numbers + [number]))

        if chosen is mark:
            read = not already
            if read:
                affected = [n for n in numbers if n <= number]
                target = number
            else:
                affected = [n for n in numbers if n >= number]
                earlier = [n for n in numbers if n < number]
                target = earlier[-1] if earlier else 0.0
        elif chosen is mark_all:
            read, affected = True, numbers
            target = max(numbers) if numbers else 0.0
        elif chosen is clear_all:
            read, affected = False, numbers
            target = 0.0
        else:
            return

        self._mark_history(
            [details.history.chapter_key(n) for n in affected], read)

        if self.entry.get("id") and not correct_progress(
                self.entry, chapter=target):
            details.show_toast(self, "Could Not Save That")
            return

        self._fill_rows()

    Page._row_card = watched_row_card
    Page._still_tile = watched_still_tile
    Page._episode_menu = fixed_episode_menu
    Page._chapter_menu = fixed_chapter_menu


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
