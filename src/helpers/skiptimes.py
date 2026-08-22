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

from . import net, title_match

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


def mal_id(title: str, timeout: float = DEFAULT_TIMEOUT):
    """The MyAnimeList id for `title`, via AniList, or None.

    Cached for the session: a title's MAL id does not change, and this
    is asked once per episode otherwise."""
    title = (title or "").strip()
    if not title:
        return None
    key = title.lower()
    with _lock:
        if key in _mal_cache:
            return _mal_cache[key]
    query = {
        "query": ("query($s:String){Media(search:$s,type:ANIME)"
                  "{idMal title{romaji english}}}"),
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
        media = ((body or {}).get("data") or {}).get("Media") or {}
        names = [n for n in (media.get("title") or {}).values() if n]
        # Checked, not trusted: AniList's search is fuzzy and will
        # happily answer with a spin-off. Skipping to the wrong place in
        # an episode is a worse failure than not offering to skip.
        if media.get("idMal") and any(
                title_match.similarity(title, name) >= 0.7 for name in names):
            found = int(media["idMal"])
    except Exception:
        found = None
    with _lock:
        _mal_cache[key] = found
    return found


def _clean(intervals, episode_length=0.0):
    """Drop what cannot be a real opening/ending, and sort by start."""
    out = []
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
        if episode_length and start >= float(episode_length):
            continue
        out.append({"type": row.get("type") or OPENING,
                    "start": start, "end": end,
                    "source": row.get("source") or ""})
    out.sort(key=lambda row: row["start"])
    return out


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
    mal = mal_id(title, timeout)
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
    with _lock:
        _skip_cache[key] = (time.monotonic(), [dict(x) for x in cleaned])
    return cleaned
