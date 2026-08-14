"""When the next episode/chapter of a tracker entry lands, and how that
reads on the card's hover tooltip.

One place for the three sources, because each medium has a different one:

  Anime   AniList's published airing schedule (helpers/anilist.py)
  Series  TVMaze's published airing schedule (helpers/tvmaze.py)
  Manga   estimated from MangaDex release history (helpers/mangadex.py) -
          nothing publishes scanlation release dates, so this one is a
          projection, and says so on the card ("Expected" + an estimated
          flag) rather than stating a guess as fact.

The looked-up time is cached on the entry itself (`next_release`) and the
countdown is rendered fresh on every hover from that stored timestamp -
so the remaining time is always right without re-hitting an API to show
a tooltip.
"""

from datetime import datetime, timedelta, timezone

from . import anilist, mangadex, tvmaze

MEDIUM_ANIME = "anime"
MEDIUM_SERIES = "series"
MEDIUM_MANGA = "manga"

# How long a stored lookup stays fresh. Airing schedules shift by a day
# or two (delays, specials) often enough that a stale week is misleading,
# but not so often that this needs checking on every launch.
_TTL = timedelta(hours=12)

# Nothing new is coming for these, so they're never looked up.
FINISHED_STATUSES = {"Completed", "Dropped"}


# ---- lookup ----------------------------------------------------------

def fetch(medium: str, title: str, imdb_id=None, known_latest_chapter=None,
          manga_id=None):
    """Look up the next release. Blocking (call it off the UI thread) and
    fails soft: any lookup problem just means no schedule is known.

    Returns (next_release, manga id): the dict stored as the entry's
    `next_release` (or None), and - manga only - the MangaDex id this
    title resolved to, for the caller to cache on the entry and pass back
    in as `manga_id` next time. That id is returned separately from the
    schedule rather than inside it precisely because it outlives one: a
    lookup that comes back empty overwrites `next_release` with None, and
    an id stored in there would be lost with it, putting the search this
    exists to avoid back on the next refresh (see
    mangadex.fetch_next_chapter). Anime and series have a real published
    schedule keyed by title/IMDb id, so they have nothing to cache."""
    if medium == MEDIUM_MANGA:
        found, resolved_id = mangadex.fetch_next_chapter(
            title, known_latest_chapter, manga_id)
        return _pack(found, "mangadex"), resolved_id
    if medium == MEDIUM_SERIES:
        return _pack(tvmaze.fetch_next_episode(imdb_id, title), "tvmaze"), None

    found = anilist.fetch_next_episode(title)
    if found:
        return _pack(found, "anilist"), None
    # AniList is the anime schedule source, but it only knows what it has
    # an entry for and the tracker's title has to match one. Anime entries
    # carry the same IMDb id Series entries do, and TVMaze lists plenty of
    # anime by it - so an exact-id lookup is a free second chance at a
    # real schedule rather than showing nothing.
    if not imdb_id:
        return None, None
    return _pack(tvmaze.fetch_next_episode(imdb_id, title), "tvmaze"), None


def _pack(found, source):
    if not found or not found.get("at"):
        return None
    return {
        "at": found["at"].astimezone(timezone.utc).isoformat(),
        "episode": found.get("episode"),
        "season": found.get("season"),
        "chapter": found.get("chapter"),
        "estimated": bool(found.get("estimated")),
        "source": source,
    }


def needs_refresh(entry: dict, force: bool = False) -> bool:
    """Whether this entry's schedule is worth (re-)looking up now."""
    if entry.get("status") in FINISHED_STATUSES:
        return False
    if force:
        return True
    stored = entry.get("next_release")
    when = _release_time(stored)
    # Already past: whatever it pointed at has aired, so there's a new
    # one to find regardless of how recently this was checked.
    if when and when <= datetime.now(timezone.utc):
        return True
    checked_at = _parse(entry.get("next_release_checked_at"))
    return not checked_at or datetime.now(timezone.utc) - checked_at > _TTL


# ---- display ---------------------------------------------------------

def tooltip_lines(entry: dict, medium: str) -> list:
    """The extra hover lines for an entry, or [] when nothing's known.

    Identical wording for every medium - "Expected:" then "Countdown:" -
    so a card reads the same whichever page it's on. Manga adds the
    chapter number it's counting down to, which anime/series have no
    equivalent for (the number a schedule gives is per-cour and doesn't
    line up with the season/episode already shown above it)."""
    when = _release_time(entry.get("next_release"))
    if not when:
        return []
    local = when.astimezone()
    remaining = when - datetime.now(timezone.utc)

    lines = []
    if medium == MEDIUM_MANGA:
        chapter = (entry.get("next_release") or {}).get("chapter")
        if chapter:
            lines.append(f"Next Chapter: {chapter:g}")
    lines.append(f"Expected: {format_slot(local)}")
    lines.append(f"Countdown: {format_countdown(remaining)}")
    return lines


def format_slot(local: datetime) -> str:
    """"Monday 8:00 PM" - always the weekday name, never a date, so every
    card reads the same way. Something more than a week out is genuinely
    ambiguous stated as a weekday alone, but the countdown sitting right
    underneath it resolves that ("Monday 8:00 PM" + "10d 13h 1m" can
    only mean the Monday after next)."""
    clock = local.strftime("%I:%M %p").lstrip("0")
    return f"{local.strftime('%A')} {clock}"


def format_countdown(remaining: timedelta) -> str:
    """"2d 5h 23m", trimmed to the units that matter at this distance."""
    seconds = int(remaining.total_seconds())
    if seconds <= 0:
        return "any moment now"
    seconds += 30  # to the nearest minute, not the one just gone
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes = seconds // 60
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


# ---- internals -------------------------------------------------------

def _parse(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    # Stored values are written with an offset, but a hand-edited or
    # older data file might not have one - assume UTC rather than blowing
    # up on a naive/aware comparison later.
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _release_time(stored):
    return _parse(stored.get("at")) if isinstance(stored, dict) else None
