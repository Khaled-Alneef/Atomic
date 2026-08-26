"""Where an episode's opening, ending and recap are, so the player can
offer to skip them.

Three sources, asked in this order, and they are genuinely different
kinds of answer rather than one with backups:

  * **The release's own chapter markers.** Free, instant, offline, and
    exactly right when present - a lot of fansub mkvs carry chapters
    named "Opening", "OP", "Intro", "Ending", "Preview". mpv exposes
    them as `chapter-list`, so the player reads these itself (see
    player._skips_from_chapters); nothing in this module is needed for
    them.
  * **AniSkip.** A crowd-sourced database keyed by *MyAnimeList* id and
    episode number. Verified live 21 August 2026: Demon Slayer
    (mal 38000) episode 5 answers `op 44.2 -> 134.2` and
    `ed 1290.6 -> 1380.6` in 1.7s. It needs no key.
  * **TheIntroDB.** Asked for by name and wired up below, but it is
    **dark from this machine and says so honestly**: measured the same
    day, `api.theintrodb.com` has no DNS record at all (getaddrinfo
    fails) and `theintrodb.com` times out before TLS. If it comes back,
    this asks it without any other change.

The MAL id comes from AniList, which this app already talks to -
`Media.idMal` on a title search. Cached per title, because it never
changes.

Everything fails soft to an empty list: no skip data means no Skip
button, never an error and never a wrong seek.
"""

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import logs, net, title_match

_UA = "Atomic/1.0"
DEFAULT_TIMEOUT = 8

ANISKIP_URL = "https://api.aniskip.com/v2/skip-times"
# Wired, unreachable from here (see the module docstring). Left as a
# constant so turning it back on is one line if it ever resolves.
INTRODB_URL = "https://api.theintrodb.com/v1"

# What this app calls each kind, whatever a source calls it.
OPENING = "op"
ENDING = "ed"
RECAP = "recap"

_ANISKIP_TYPES = {
    "op": OPENING, "mixed-op": OPENING,
    "ed": ENDING, "mixed-ed": ENDING,
    "recap": RECAP,
}

# An interval shorter than this is noise, and one longer than this is not
# an opening - both have been seen in crowd-sourced data, and a Skip
# button that jumps four minutes into the episode is worse than none.
MIN_SPAN_S = 3.0
MAX_SPAN_S = 150.0

# **Where in the file each kind is allowed to sit, as a fraction of the
# runtime.** Measured live 24 August 2026 over three of the owner's own
# titles, and this is not a tidy-up: AniSkip files *endings at the head
# of episode 1* on all three.
#
#     Iruma-kun ep1   ed    4.08 ->   94.08     0.3% of the file
#     Iruma-kun ep1   ed   42.65 ->  132.65     2.9%
#     Attack on Titan ep1  ed   24.41 ->  114.41     1.7%
#     Jujutsu Kaisen ep1   ed  223.97 ->  313.96    15.8%
#
# against every *real* ending in the same sample at 89.7 - 94.6%. The
# player offers "Next Episode" over an ending interval, so the first of
# those is the owner's screenshot: **"Next Episode ->" showing at 0:22
# of Welcome to Demon-School, Iruma-kun S01E01**, twenty-two seconds
# into a twenty-four minute episode. The gap between 15.8% and 89.7% is
# wide enough that 0.5 needs no defending; it is set there rather than
# at 0.85 so a double-length premiere whose credits genuinely start
# early still keeps its offer.
#
# A recap is the mirror image - "previously on" is at the head or it is
# not a recap. Measured: Jujutsu Kaisen S01E03 recap 0.50-41.62,
# Iruma-kun S01E02 recap 24.08-51.04, both inside the first 2%.
#
# Openings deliberately have **no** position rule. The same measurement
# has Jujutsu Kaisen ep2's real opening at 24.3% and Demon Slayer's
# double-length premiere at 44%, so any threshold tight enough to be
# useful would throw away real data.
ENDING_MIN_POSITION = 0.5
RECAP_MAX_POSITION = 0.35

# How long each kind actually runs, from the 18 openings / 20 endings
# measured 22 August 2026 (both median 90.0s) and the recaps measured
# since (17.4 - 90.0s). Used only to choose between two crowd entries
# that overlap - see _resolve_overlaps.
CANONICAL_SPAN_S = {OPENING: 90.0, ENDING: 90.0, RECAP: 45.0}

_mal_cache = {}
_skip_cache = {}
_lock = threading.Lock()
CACHE_TTL_S = 12 * 60 * 60


def _get_json(url, timeout):
    request = urllib.request.Request(url, headers={
        "Accept": "application/json", "User-Agent": _UA})
    deadline = net.deadline_in(timeout)
    with net.urlopen(request, timeout=timeout) as response:
        return json.loads(net.read_text(response, deadline))


def mal_id(title: str, timeout: float = DEFAULT_TIMEOUT, season=None):
    """The MyAnimeList id for `title` - for `season` of it, when the
    franchise files each season as its own anime - via AniList, or None.

    **`season` matters, and ignoring it was the owner's "in Bleach ep3s4
    the skip intro button is totally inaccurate" (24 August 2026).**
    AniSkip is keyed by MAL id, and MAL files Bleach: Thousand-Year
    Blood War as four ids (41467 / 53998 / 56784 / 60636, one per
    cour). This used to take AniList's single best match - part 1 - so
    asking for S04E03 fetched **part 1 episode 3's** intervals: an
    opening at 137.5s, correct for an episode nobody was watching, drawn
    over the one playing. Measured live; and part 4's own id answers
    404 (no crowd entry yet), so the right behaviour for that episode
    today is **no button**, which this now produces.

    The season is resolved by listing every AniList match for the title
    and taking the Nth by start date - the same order seasons air in.
    A title with exactly one match keeps it whatever the season (One
    Piece is one entry with one long numbering), and a season past the
    list answers None rather than the wrong part's data.

    Cached for the session per (title, season): ids do not change."""
    title = (title or "").strip()
    if not title:
        return None
    try:
        season = int(season) if season else 0
    except (TypeError, ValueError):
        season = 0
    key = (title.lower(), season)
    with _lock:
        if key in _mal_cache:
            return _mal_cache[key]
    query = {
        "query": ("query($s:String){Page(perPage:10){media(search:$s,"
                  "type:ANIME,format_in:[TV,TV_SHORT,ONA])"
                  "{idMal episodes startDate{year month day}"
                  " title{romaji english}}}}"),
        "variables": {"s": title},
    }
    found = None
    try:
        request = urllib.request.Request(
            "https://graphql.anilist.co", data=json.dumps(query).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Accept": "application/json", "User-Agent": _UA})
        deadline = net.deadline_in(timeout)
        with net.urlopen(request, timeout=timeout) as response:
            body = json.loads(net.read_text(response, deadline))
        rows = (((body or {}).get("data") or {}).get("Page") or {}).get("media") or []
        # Checked, not trusted: AniList's search is fuzzy and will
        # happily answer with a spin-off. Skipping to the wrong place in
        # an episode is a worse failure than not offering to skip.
        matches = []
        for media in rows:
            names = [n for n in (media.get("title") or {}).values() if n]
            if media.get("idMal") and any(
                    title_match.similarity(title, name) >= 0.7
                    for name in names):
                matches.append(media)
        if len(matches) == 1 or season <= 1:
            # One entry is the whole franchise; and season 1 of a
            # per-part franchise is the earliest entry either way.
            if matches:
                def aired(media):
                    date = media.get("startDate") or {}
                    return (date.get("year") or 9999,
                            date.get("month") or 99, date.get("day") or 99)
                found = int(sorted(matches, key=aired)[0]["idMal"])
        elif season >= 2 and len(matches) >= season:
            def aired(media):
                date = media.get("startDate") or {}
                return (date.get("year") or 9999,
                        date.get("month") or 99, date.get("day") or 99)
            found = int(sorted(matches, key=aired)[season - 1]["idMal"])
    except Exception:
        found = None
    with _lock:
        _mal_cache[key] = found
    return found


# **An opening filed at exactly 0.0 and exactly 90.0 seconds long is a
# default somebody typed, not a measurement.**
#
# The owner, 25 August 2026, with a screenshot: "it shows skip intro,
# while it is not the intro now, not all intros [are] in the beginning of
# the video ... you should always use the API". The API *was* being used,
# and the API is what was wrong - asked live that day, AniSkip answers
# `From Old Country Bumpkin to Master Swordsman` with
#
#     ep1  op 0.00 -> 90.00      ep2  op 0.00 -> 90.00
#     ep3  op 0.00 -> 90.00
#
# three episodes, the same two round numbers each time, over a frame the
# owner screenshotted at 0:12 showing story.
#
# Surveyed over 26 openings across ten of his titles before drawing this
# line, because "starts at zero" alone is not enough to condemn one:
#
#     6 openings start under 1.0s, 20 start later
#     Spy x Family     ep1 0.00 + 96.02   ep2 0.00 + 90.12   - real
#     Frieren          ep3 1.35 + 90.00                      - real
#     Attack on Titan  ep3 4.00 + 92.00                      - real
#
# So a show whose opening genuinely runs from the first frame exists and
# must keep its button. What separates it is that a real submission
# carries a measured length - 96.02, 90.12, 92.00 - while the default
# carries the nominal 90.00 exactly. Both halves must match before an
# interval is thrown away, and only for an opening: an ending or a recap
# at 0.0 is already handled by ENDING_MIN_POSITION and its neighbours.
PLACEHOLDER_START_S = 0.05
PLACEHOLDER_SPAN_S = 90.0
PLACEHOLDER_SPAN_EPSILON_S = 0.05


def _is_placeholder_opening(kind, start, span) -> bool:
    return (kind == OPENING and start < PLACEHOLDER_START_S
            and abs(span - PLACEHOLDER_SPAN_S) < PLACEHOLDER_SPAN_EPSILON_S)


def _clean(intervals, episode_length=0.0):
    """Drop what cannot be a real opening/ending/recap, resolve crowd
    entries that contradict each other, and sort by start."""
    out = []
    length = float(episode_length or 0.0)
    for row in intervals or []:
        try:
            start = float(row.get("start"))
            end = float(row.get("end"))
        except (TypeError, ValueError):
            continue
        if end <= start:
            continue
        span = end - start
        if span < MIN_SPAN_S or span > MAX_SPAN_S:
            continue
        # An interval that runs past the file is data about a different
        # cut of the episode - seen in crowd-sourced entries, where an
        # "op" can be filed at 21 minutes into a 24 minute episode.
        if length and start >= length:
            continue
        kind = row.get("type") or OPENING
        if _is_placeholder_opening(kind, start, span):
            continue        # a typed default, not a measured opening
        # **Where it sits, not only how long it is** - see the note on
        # ENDING_MIN_POSITION for the four measured intervals this
        # rejects and the one screenshot it explains.
        if length:
            where = start / length
            if kind == ENDING and where < ENDING_MIN_POSITION:
                continue
            if kind == RECAP and where > RECAP_MAX_POSITION:
                continue
        out.append({"type": kind, "start": start, "end": end,
                    "source": row.get("source") or ""})
    out.sort(key=lambda row: row["start"])
    return _resolve_overlaps(out)


def _resolve_overlaps(rows):
    """One interval per kind per stretch of the episode.

    AniSkip returns several submissions for the same episode and they
    overlap. Measured live 24 August 2026:

        Iruma-kun ep1   op   31.41 -> 145.06   (113.6s)
                        op  114.96 -> 204.01    (89.0s)
        Attack on Titan ep1  op   47.37 -> 137.37    (90.0s)
                             op   75.00 -> 135.00    (60.0s)

    Both pairs overlap, so the player showed the button, seeked to the
    first interval's end, and landed *inside the second one* - where the
    same button appeared again. That is the owner's "the skip intro and
    the skip recap btn are really inaccurate", and it is the crowd data
    disagreeing rather than the offer being computed wrongly.

    The winner is the one whose length is closest to what that kind
    actually runs (CANONICAL_SPAN_S). Note that neither "keep the
    earlier" nor "keep the later" works: Iruma's real title sequence is
    the *later* entry (89.0s, a standard OP) and Attack on Titan's is
    the *earlier* one (90.0s), and picking by length gets both right.
    Kept intervals of *different* kinds are left alone - a recap
    running into an opening is normal, and `_current_skip` takes the
    first match, which is the recap."""
    kept = []
    for row in rows:
        want = CANONICAL_SPAN_S.get(row["type"], 90.0)
        clash = None
        for other in kept:
            if (other["type"] == row["type"]
                    and row["start"] < other["end"]
                    and other["start"] < row["end"]):
                clash = other
                break
        if clash is None:
            kept.append(row)
            continue
        if abs((row["end"] - row["start"]) - want) < abs(
                (clash["end"] - clash["start"]) - want):
            kept[kept.index(clash)] = row
    kept.sort(key=lambda row: row["start"])
    return kept


# AniSkip 500s at random on entries that are perfectly fine a second
# later - measured 22 August 2026 on One Piece (mal 21) episode 1, eight
# identical requests: 200,200,200,500,500,500,200,200. A single try
# therefore loses the button on about a third of the openings this
# episode has data for, and nothing about that is visible from the chair.
# Only a 5xx is retried: a 404 is AniSkip's honest "no entry", and asking
# again cannot change it.
ANISKIP_TRIES = 3
ANISKIP_RETRY_PAUSE_S = 0.4


def _aniskip(mal, episode, episode_length, timeout):
    types = "&".join(f"types[]={name}" for name in
                     ("op", "ed", "recap", "mixed-op", "mixed-ed"))
    # **episodeLength is deliberately left at 0, which is AniSkip's
    # "unknown".** Sent as the real runtime it is a *filter*, and the
    # entry has to have been submitted against a matching length or the
    # whole episode 404s - so passing the duration mpv had just reported
    # was throwing away real data. Measured 22 August 2026 over six of
    # the owner's titles, thirteen episodes:
    #
    #     episodeLength sent as the real runtime    6/13 answered
    #     episodeLength left at 0                  10/13 answered
    #
    # One Piece episodes 2 and 5, Frieren 5 and Bleach TYBW 5 all 404 with
    # it and return an op and an ed without it. It is also *why the offer
    # came and went*: the length is whatever mpv knew when the lookup
    # fired, so the same episode answered or did not depending on how far
    # the file had loaded. Nothing is lost by dropping it - `_clean` still
    # holds every interval against the real duration, locally, where a
    # mismatch can be judged instead of 404'd.
    url = (f"{ANISKIP_URL}/{int(mal)}/{int(episode)}"
           f"?{types}&episodeLength=0")
    body = None
    for attempt in range(ANISKIP_TRIES):
        try:
            body = _get_json(url, timeout)
            break
        except urllib.error.HTTPError as error:
            if error.code < 500 or attempt == ANISKIP_TRIES - 1:
                raise
            time.sleep(ANISKIP_RETRY_PAUSE_S)
    rows = []
    for result in (body or {}).get("results") or []:
        interval = result.get("interval") or {}
        kind = _ANISKIP_TYPES.get(str(result.get("skipType") or "").lower())
        if not kind:
            continue
        rows.append({"type": kind, "source": "aniskip",
                     "start": interval.get("startTime"),
                     "end": interval.get("endTime")})
    return rows


def _introdb(title, season, episode, timeout):
    """TheIntroDB. Returns [] here - see the module docstring - but the
    shape is real, so a working host needs no other change."""
    url = (f"{INTRODB_URL}/timestamps?"
           + urllib.parse.urlencode({"title": title, "season": season or 1,
                                     "episode": episode or 1}))
    body = _get_json(url, timeout)
    rows = []
    for row in (body or {}).get("timestamps") or []:
        kind = str(row.get("type") or "").lower()
        if kind in ("intro", "opening", "op"):
            kind = OPENING
        elif kind in ("outro", "credits", "ending", "ed"):
            kind = ENDING
        elif kind == "recap":
            kind = RECAP
        else:
            continue
        rows.append({"type": kind, "source": "theintrodb",
                     "start": row.get("start"), "end": row.get("end")})
    return rows


def fetch(title: str, season=None, episode=None, episode_length: float = 0.0,
          timeout: float = DEFAULT_TIMEOUT) -> list:
    """Skip intervals for one episode, as
    [{"type": "op"/"ed"/"recap", "start": s, "end": s, "source": ...}].

    Never raises and never blocks the UI thread - call it from a worker.
    An empty list is the normal answer for anything that is not anime,
    and the player simply shows no button."""
    if not episode:
        return []
    key = ((title or "").lower(), int(season or 0), int(episode))
    now = time.monotonic()
    with _lock:
        row = _skip_cache.get(key)
        if row and now - row[0] < CACHE_TTL_S:
            return [dict(x) for x in row[1]]

    rows = []
    mal = mal_id(title, timeout, season=season)
    if mal:
        try:
            rows = _aniskip(mal, episode, episode_length, timeout)
        except Exception:
            rows = []
    if not rows:
        try:
            rows = _introdb(title, season, episode, timeout)
        except Exception:
            rows = []
    cleaned = _clean(rows, episode_length)
    if first_episode(season, episode):
        cleaned = [row for row in cleaned if row["type"] != RECAP]
    with _lock:
        _skip_cache[key] = (time.monotonic(), [dict(x) for x in cleaned])
    return cleaned


def first_episode(season, episode) -> bool:
    """Whether this is the very first episode of the whole series - the
    one case where a recap is not merely unlikely but impossible, since
    there is nothing before it to recap.

    Shared with the player, which applies the same rule to a *chapter
    marker* named "Recap": the owner watched Attack on Titan S01E01 and
    was offered "Skip Recap" over the cold open. Season 2 episode 1
    deliberately does not qualify - a season premiere recapping the
    previous season is a real thing."""
    try:
        return int(episode or 0) == 1 and int(season or 1) <= 1
    except (TypeError, ValueError):
        return False
