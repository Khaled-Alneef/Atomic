"""The one rule for marking an episode watched or unwatched by hand.

The title page's episode list has carried this rule since
helpers/episode_watch_state_patch: the row clicked is a *boundary*.

* Mark as Watched    - every known episode through this one is watched;
* Mark as Unwatched  - this episode and every known one after it is clear,
                       and progress steps back to the episode before it;
* Mark All as ...    - the same, for the whole season.

The player's own episode panel had a second copy of the decision, and the
copy had drifted (the owner, 19 September 2026: "the marking /unmarking as
watched inside the video player is broken ... make it like the one in the
ep/ch list pages"). Read side by side, the player's copy:

* moved only the entry's progress number and never touched the History
  ticks - and the list page reads the ticks, not the number, the moment a
  title carries any (details._fill_episode_rows, "the ticks win"). So an
  episode unmarked in the player was still DONE on the list, and one
  marked there ticked a single row instead of everything before it;
* decided "already watched" from the number alone, so with ticks present
  its menu offered the wrong verb for a row the list drew the other way;
* trusted an unverified progress number, which is the tracker's guess at
  what is *out*, not at what was watched (details._progress refuses it);
* did nothing at all for the first episode of a season (its target was
  "episode 0", which it returned on), and nothing for an unsaved title,
  which has no id for correct_progress to write against and whose only
  store is History.

So the decision lives here, once, Qt-free and with no storage in it - it
answers *what* changes; the page that asked does the writing, because the
list page and the player hold different things to redraw.
"""

from . import history

MARK = "mark"
MARK_ALL = "mark_all"
CLEAR_ALL = "clear_all"


def known_pairs(videos) -> list:
    """Every real (season, episode) in Cinemeta's rows, in playback order.

    **Season 0 is not part of that order** - a special is not the episode
    before episode 1, and sorted it lands there: marking S01E01 ticked
    Jujutsu Kaisen's eight specials and unmarking it stored S01E08
    (episode_watch_state_patch._episode_pairs has the whole measurement,
    4 September 2026). They still draw and still play; they do not decide
    where watching stands."""
    pairs = set()
    for video in videos or ():
        try:
            season = int(video.get("season") or 0)
            episode = int(video.get("number") or video.get("episode") or 0)
        except (AttributeError, TypeError, ValueError):
            continue
        if season > 0 and episode > 0:
            pairs.add((season, episode))
    return sorted(pairs)


def is_watched(marks, progress, season, episode) -> bool:
    """Whether this episode reads as watched - the row's badge and the
    menu's verb, which must be one answer.

    **The ticks win where there are ticks.** Once a title carries any
    explicit mark the marks are the truth and each episode is independent;
    the progress number ("everything up to here") only speaks for a title
    that has none. `progress` is (season, episode), and the caller passes
    (0, 0) for a number that is not verified."""
    if marks:
        return history.episode_key(season, episode) in marks
    watched_season, watched_episode = progress or (0, 0)
    return bool(watched_episode) and (
        (int(watched_season or 0), int(watched_episode)) >= (season, episode))


def plan(pairs, season, episode, action, already=False, season_episodes=None):
    """What one menu choice changes: (watched, affected, target).

    `affected` is the (season, episode) ticks to write, `watched` which
    way, and `target` where progress stands afterwards - (0, 0) for
    "nothing watched at all", which the caller clears rather than writes
    (correct_progress refuses a zero episode). None for an unknown action.

    `season_episodes` is what "all" means for this season - the caller's
    list of the numbers it actually offers, so an unaired row is never
    ticked. Left out, it is every known episode of the season."""
    clicked = (int(season or 0), int(episode or 0))
    pairs = sorted(set(pairs or ()))
    if clicked not in pairs and clicked[0] > 0 and clicked[1] > 0:
        pairs = sorted(set(pairs + [clicked]))
    if season_episodes is None:
        season_episodes = [e for s, e in pairs if s == clicked[0]]
    whole_season = sorted({(clicked[0], int(e)) for e in season_episodes if e})
    # Where progress falls back to when a whole season stops counting.
    before_season = [pair for pair in pairs if pair[0] < clicked[0]]

    if action == MARK:
        if already:
            earlier = [pair for pair in pairs if pair < clicked]
            return (False, [pair for pair in pairs if pair >= clicked],
                    earlier[-1] if earlier else (0, 0))
        return True, [pair for pair in pairs if pair <= clicked], clicked
    if action == MARK_ALL:
        return (True, whole_season,
                whole_season[-1] if whole_season else clicked)
    if action == CLEAR_ALL:
        return (False, whole_season,
                before_season[-1] if before_season else (0, 0))
    return None
