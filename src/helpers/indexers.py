"""Anime torrent indexers asked directly, beside the addons.

The addon route (helpers/streams.py) asks by IMDb id. These ask by
*title*, which no id-based source can do - so they reach the fansub
releases an id-keyed index has never heard of, and they answer for a
title Cinemeta and Torrentio between them do not carry.

**"An id cannot resolve to the wrong show" used to be written here and
it is not true** - measured 22 August 2026 against the owner's Re:Zero
entry (tt5607616) asked for S01E01. Torrentio and TorrentsDB map an
IMDb id onto their own scraped release names, and that mapping returned
seasons 2, 3 and 4 of the right show (`[FLE] ... - S04E01`,
`[PMR] ... S03E01-12`, `[Yameii] ... - S03`) *and* four rows of
entirely different shows (`Zero Stars S01E01`, `Re-Main.S01`,
`Life 2020 S01`, `Loner Life In Another World - 01`). So an addon row's
own name is now checked too, by `episode_conflict` below - which is why
that function lives in this module rather than in streams.py.

**Measured, on this machine, before anything was put in the table** -
the same rule the addon defaults follow. Query "Solo Leveling", and the
owner's four real video entries at episode 5:

    Anime Tosho    https://feed.animetosho.org/json   75 rows, 0.4-2.5s
                   carries info_hash, a magnet with its trackers, and a
                   seeder count. Its rows also carry nyaa_id, tosho_id
                   and anidex_id, which is the useful part: it *is* an
                   index over Nyaa, Tokyo Toshokan and AniDex, and
                   SubsPlease and Erai-raws releases both come back in
                   its results under their own group tags.
    SubsPlease     https://subsplease.org/api/        26 shows, 0.4s
                   the current season, per resolution, with magnets.

And the ones asked for that are **not** here, each for a stated reason:

    Nyaa           nyaa.si times out from this network (12s, no answer),
    Tokyo Toshokan connection reset before TLS, and
    AniDex         connection reset - and it is not DNS: helpers/
                   dns_resolve answers for all three (186.2.163.20,
                   172.67.214.1, 185.178.208.171) and the connection
                   still dies. That is upstream filtering, which nothing
                   in this app can route around. All three are reached
                   anyway, second-hand, through Anime Tosho above and
                   through the "Torrentio Anime" addon, which is pinned
                   to exactly these three providers and does its
                   scraping on its own server.
    Erai-raws      www.erai-raws.info resets the connection too, and its
                   releases are indexed on Anime Tosho regardless.
    ToshiMoe       tosho.moe and toshi.moe do not resolve at all, on the
                   system resolver or over DoH. The domain is gone.
    AniRena        answers 200 and returns a 9KB page with no items for
                   any search shape tried. Nothing to parse, so nothing
                   goes in the table.

Everything fails soft to an empty list. A dead indexer means fewer
sources, never an error.
"""

import concurrent.futures
import json
import re
import urllib.parse
import urllib.request

from . import net

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")

# Per-request ceiling. The deadline passed in bounds the whole search;
# this only stops one indexer eating all of it.
DEFAULT_TIMEOUT = 8

ANIMETOSHO_JSON = "https://feed.animetosho.org/json"
SUBSPLEASE_API = "https://subsplease.org/api/"

# How many rows of one indexer's answer to keep. They come back newest
# first and a search returns 75; past the first couple of dozen they are
# older re-encodes of the same episode, and every one kept is a row in
# the player's source list.
MAX_ROWS = 30

# A word shorter than this says nothing about which series a release is
# ("of", "no", "the"), so it is not held against a release that omits it.
MIN_TITLE_WORD = 3
# How much of the entry's own title a release name has to carry before
# it is believed to be that series. **This is the whole safeguard**, and
# it is set where it is because of a measured near-miss: asking Anime
# Tosho for "Bleach: Thousand-Year Blood War 05" returns
# "[BlackRabbit] Bleach (2004) - S05" in first place - a different show,
# a different episode, and entirely plausible-looking in a list. At 0.6
# that release carries one of five significant words and is rejected;
# "[Judas] Tsue to Tsurugi no Wistoria (Wistoria Wand and Sword)" carries
# all three of its entry's and is kept.
TITLE_WORD_FRACTION = 0.6


def _get(url, timeout):
    request = urllib.request.Request(
        url, headers={"User-Agent": _UA, "Accept": "application/json, */*"})
    deadline = net.deadline_in(timeout)
    with net.urlopen(request, timeout=timeout) as response:
        return net.read_text(response, deadline, net.MAX_RESPONSE_BYTES)


def _get_json(url, timeout):
    return json.loads(_get(url, timeout))


# ------------------------------------------------------------- matching

_WORD_RE = re.compile(r"[a-z0-9]+")


def _words(text: str) -> list:
    return [w for w in _WORD_RE.findall((text or "").lower())
            if len(w) >= MIN_TITLE_WORD]


def is_same_title(entry_title: str, release_title: str) -> bool:
    """Whether `release_title` is plausibly a release of `entry_title`.

    Word coverage rather than a similarity ratio, because a release name
    is not a title: it is a title wrapped in a group tag, a resolution,
    a codec and a checksum, and any whole-string measure is dominated by
    the wrapping. What actually separates the right show from the wrong
    one is whether the title's own distinguishing words are all there -
    see TITLE_WORD_FRACTION for the case that fixed."""
    wanted = _words(entry_title)
    if not wanted:
        return False
    have = set(_words(release_title))
    hits = sum(1 for word in wanted if word in have)
    return hits >= max(1, round(len(wanted) * TITLE_WORD_FRACTION))


# `S02E05`, `S02 - 05`, `- 05`, `[05]`, `_05_`, ` 05v2`. Deliberately not
# a bare number anywhere in the string: a resolution, a year and a
# checksum are all numbers, and matching one of those as the episode is
# how the wrong episode plays.
_SxE_RE = re.compile(r"s(\d{1,2})\s*[\-_ ]?\s*e(\d{1,3})", re.I)
_LOOSE_EP_RE = re.compile(r"(?:^|[\s\-_\[(])(\d{1,3})(?:v\d)?(?=[\s\-_\])]|$)")
# `01 ~ 13`, `01-13`, `(01~24)` - a batch covering a run of episodes.
_RANGE_RE = re.compile(r"(\d{1,3})\s*[~\-]\s*(\d{1,3})")
# `S01`, `Season 2`, `Complete Series` with no episode at all.
_SEASON_ONLY_RE = re.compile(r"(?:^|[\s\-_\[(])s(?:eason\s*)?(\d{1,2})"
                             r"(?![\s\-_]?e?\d)", re.I)

# ------------------------------------------- which season a name states
#
# **The season written in words was invisible, and that is the owner's
# "many of the sources download a diff ep from a diff season" (22 August
# 2026).** `_SEASON_ONLY_RE` above reads `S03` - but it is consulted
# *only* when the name carries no loose episode number at all, so
# "[Anipakku] Re:ZERO ... 3rd Season - 01" fell straight through to the
# fansub branch, matched the `01`, and was offered as season 1 episode 1.
# Measured over the owner's own Re:Zero entry at S01E01, that shape plus
# the addon rows nobody checked put **sixteen rows of seasons 2, 3 and 4
# into a list of seventy-nine** - eleven of them from TorrentsDB alone,
# whose answer for that lookup was 39 rows. Among them the well-seeded
# `[FLE] ... - S04E01`, which is what a viewer's eye and the default
# pick both land on first.
#
# So the season is now collected from every form these names use, and
# read as a *set*: a multi-season pack legitimately states several and
# holds the asked-for episode inside one of them.
#
#   S04E01 / S03E01-12     -> _SxE_RE, below
#   S03 / S1 / S03P01      -> _SEASON_TOKEN_RE ("S04 P01" too - a cour
#                             split is still its own season, the same
#                             reason anilist._SEASON_NUMBER_RE ignores
#                             "Part")
#   3rd Season / Season 3  -> _SEASON_TOKEN_RE
#   S01-03                 -> _SEASON_RANGE_RE
#   Season 1+2+Movies      -> _SEASON_LIST_RE
#
# Every one of these anchors on a season keyword or a bare `S`, never on
# a loose number: "E-AC-3", "Opus 2.0" and "AV1" are all numbers with a
# boundary in front of them, which is exactly why loose numbers may
# *accept* a release (below) and may never reject one.
_SEASON_RANGE_RE = re.compile(
    r"(?<![a-z0-9])s\s*0?(\d{1,2})\s*[-~]\s*s?\s*0?(\d{1,2})(?![\s\-_]?e?\d)",
    re.I)
# Deliberately no `-` in the separator: "Season 3 - 08" is a season and an
# episode, not seasons three through eight.
_SEASON_LIST_RE = re.compile(r"season\s*0?(\d{1,2})\s*[+&,]\s*0?(\d{1,2})", re.I)
_SEASON_TOKEN_RE = re.compile(
    r"(?:(?<![a-z0-9])(\d{1,2})(?:st|nd|rd|th)\s*season"
    r"|season\s*0?(\d{1,2})"
    r"|(?<![a-z0-9])s\s?0?(\d{1,2})(?![\s\-_]?e?\d))", re.I)
# The `-E11` / `-12` a pack writes after its first episode: `S04E01-E11`,
# `S03E01-12`. Anchored at the end of the SxxEyy it follows, so a number
# elsewhere in the name can never widen the span.
_SxE_RANGE_TAIL_RE = re.compile(r"\s*[-~]\s*e?(\d{1,3})", re.I)
# Above this a "season" is a codec artefact, not a season.
_MAX_SEASON = 40


def stated_seasons(release_title: str) -> set:
    """Every season this release name names, as a set. Empty means the
    name does not say - which is not the same as saying season one."""
    text = _strip_noise(release_title)
    seasons = set()
    for match in _SxE_RE.finditer(text):
        seasons.add(int(match.group(1)))
    for pattern in (_SEASON_RANGE_RE, _SEASON_LIST_RE):
        for first, last in pattern.findall(text):
            first, last = int(first), int(last)
            if 0 < first <= last <= _MAX_SEASON:
                seasons.update(range(first, last + 1))
    for groups in _SEASON_TOKEN_RE.findall(text):
        value = next((g for g in groups if g), None)
        if value:
            seasons.add(int(value))
    return {s for s in seasons if 0 < s <= _MAX_SEASON}


def episode_conflict(release_title: str, season, episode,
                     compare_season: bool = True, extra_seasons=None) -> bool:
    """True only when this name **positively states** it is something
    other than the episode asked for.

    Deliberately different from `episode_match`, and the difference is
    the whole point of having two. `episode_match` answers "is this
    provably the right episode", which is what a *title* search needs -
    there, anything unproven has to go, because the search is text and
    the wrong show comes back looking as good as the right one.

    An addon is asked by IMDb id, so its rows arrive already claiming to
    be this episode of this show and most of them state nothing further.
    Holding those to `episode_match` would throw away most of the list
    for saying nothing; so this asks the opposite question and drops only
    what contradicts the request. Measured 22 August 2026 on the owner's
    Re:Zero entry (tt5607616) at S01E01: TorrentsDB answered **39 rows,
    11 of which name season 3 or season 4**, and those eleven are what
    goes - the rest, which say nothing about a season, stay.

    Loose numbers are not evidence here, on purpose: an addon's stream
    title is the release name, the file path *and* a stats line, and
    "E-AC-3" and "Opus 2.0" both read as an episode number. Only a
    stated season or a stated SxxEyy counts.

    **`compare_season=False` when the two numberings are not the same
    namespace**, which is not a hypothetical: Bleach: Thousand-Year Blood
    War is its own IMDb title and sits at season 1 here, while every
    index files it as *Bleach season 17*. Measured 22 August 2026 asking
    Torrentio for tt14986406:1:12, a blanket season check dropped eleven
    of sixty-three rows - including `[LostYears] Bleach Thousand-Year
    Blood War - S17E12`, which is exactly the episode requested, and
    `[Judas] Bleach (Season 17)` at 236 seeders, which is what the race
    had been winning. The caller decides (see streams._drop_wrong_season)
    by asking whether the season it wants appears anywhere in the answer
    at all; when it does not, only the *episode* numbers are compared.

    **`extra_seasons`** is the season(s) a caller worked out by a means
    this function cannot - the arc-name map in helpers/anime_identity,
    which reads "Kimetsu no Yaiba - Hashira Geiko Hen" as season 5 where
    `stated_seasons` sees no number at all. Folded in beside the stated
    ones, so an arc-named release is judged exactly as a numbered one:
    that is the whole of the owner's "sources from a diff season" fix for
    Demon Slayer, whose seasons carry arc names and never numbers."""
    if not episode:
        return False
    season = int(season or 1)
    episode = int(episode)
    text = _strip_noise(release_title)

    if compare_season:
        seasons = stated_seasons(release_title) | (set(extra_seasons or ()))
        if seasons and season not in seasons:
            return True

    # What episodes this name states. A pack writes a span
    # (`S01E01-E25`); a single release writes one number.
    spans = []
    for match in _SxE_RE.finditer(text):
        if compare_season and int(match.group(1)) != season:
            continue
        first = last = int(match.group(2))
        tail = _SxE_RANGE_TAIL_RE.match(text, match.end())
        if tail:
            last = max(last, int(tail.group(1)))
        spans.append((first, last))
    if spans and not any(a <= episode <= b for a, b in spans):
        # A pack whose name carries one file's SxxExx as well as its own
        # range - "...S01 [01 ~ 25]... S01E01.mkv" - is still a pack that
        # holds the episode. Only a name with no covering range at all
        # is a contradiction.
        for first, last in ((int(a), int(b)) for a, b in _RANGE_RE.findall(text)):
            if last > first and first <= episode <= last:
                return False
        return True
    return False


def _strip_noise(title: str) -> str:
    """A release name with the parts that hold numbers-that-are-not-
    episodes taken out: resolution, year, bit depth, audio channels and
    the CRC in brackets at the end."""
    text = re.sub(r"\b(?:480|540|576|720|1080|1440|2160)p?\b", " ", title or "")
    text = re.sub(r"\b(?:19|20)\d{2}\b", " ", text)
    text = re.sub(r"\b(?:8|10|12)\s*bit\b", " ", text, flags=re.I)
    text = re.sub(r"\b(?:x|h)\.?26[45]\b", " ", text, flags=re.I)
    text = re.sub(r"\b(?:aac|flac|opus|eac3|ac3|dts)\s*\d(?:\.\d)?\b", " ",
                  text, flags=re.I)
    text = re.sub(r"\[[0-9A-F]{8}\]", " ", text)
    return text


def episode_match(release_title: str, season, episode):
    """How this release relates to the episode being asked for.

    Returns "exact" (it is that episode), "pack" (a batch or season that
    contains it, so the file has to be chosen inside the torrent), or
    None (it is something else, and must not be offered).

    None is the important return. An indexer search is a *text* search,
    so the wrong episode of the right show comes back looking exactly as
    good as the right one - and quietly playing a different episode is
    the failure this codebase has already shipped once (see
    streams.episode_fallbacks)."""
    if not episode:
        return "pack"
    episode = int(episode)
    season = int(season or 1)
    text = _strip_noise(release_title)

    # **The season, before anything else.** Everything below reads
    # episode numbers, and an episode number means nothing until it is
    # known which season it belongs to: "3rd Season - 01" and
    # "Season 3 Episode 01-04" both used to fall through to the branches
    # below and be returned as season 1 - see episode_conflict for the
    # measurement, and for why the season is read as a set.
    if episode_conflict(release_title, season, episode):
        return None

    marked = _SxE_RE.search(text)
    if marked:
        return ("exact" if (int(marked.group(2)) == episode
                            and int(marked.group(1)) == season) else None)

    for start, end in ((int(a), int(b)) for a, b in _RANGE_RE.findall(text)):
        if start <= episode <= end and end > start:
            return "pack"

    # A season pack with no episode number at all. Only for the season
    # actually asked for - "S01" is not where season 2 episode 5 lives.
    season_only = _SEASON_ONLY_RE.search(text)
    if season_only and not _LOOSE_EP_RE.search(text):
        return "pack" if int(season_only.group(1)) == season else None

    # The classic fansub form: "Title - 05". The number has to stand on
    # its own, which is what _LOOSE_EP_RE's boundaries are for.
    for found in _LOOSE_EP_RE.findall(text):
        if int(found) == episode:
            return "exact"
    return None


# ------------------------------------------------------------ magnet

_TRACKER_RE = re.compile(r"[?&]tr=([^&]+)")
_HASH_RE = re.compile(r"btih:([0-9a-zA-Z]+)", re.I)


def _trackers_from(magnet: str) -> list:
    """The announce URLs a magnet carries, in the shape the addon
    protocol writes them - streams.prepare and torrent_engine.add both
    already understand `tracker:<url>`, so an indexer's trackers arrive
    on the same path as an addon's rather than through a second one.

    They are not optional. A bare info hash leaves the engine on DHT
    alone, which is the difference between a stream starting and one
    that reports zero peers for thirty seconds (see streams.prepare)."""
    return [f"tracker:{urllib.parse.unquote(t)}"
            for t in _TRACKER_RE.findall(magnet or "")]


def _hash_from(magnet: str) -> str:
    """The info hash out of a magnet, as 40 lowercase hex.

    Half of these are base32 - SubsPlease writes every one that way -
    and a base32 hash handed to libtorrent is simply not a hash. It is
    decoded here, once, so nothing downstream has to know."""
    found = _HASH_RE.search(magnet or "")
    if not found:
        return ""
    value = found.group(1)
    if len(value) == 40:
        try:
            int(value, 16)
            return value.lower()
        except ValueError:
            return ""
    if len(value) == 32:
        import base64
        try:
            return base64.b32decode(value.upper()).hex()
        except Exception:
            return ""
    return ""


_QUALITY_RE = re.compile(r"\b(2160p|1440p|1080p|720p|480p|360p|4k)\b", re.I)


def _quality_of(text: str) -> str:
    found = _QUALITY_RE.search(text or "")
    return found.group(1).lower() if found else ""


def _stream(title, info_hash, trackers, seeders, source, size_bytes=0):
    return {"title": title, "url": None, "kind": "torrent", "source": source,
            "quality": _quality_of(title), "reason": "", "headers": {},
            "info_hash": info_hash, "file_index": None,
            "sources": trackers, "seeders": int(seeders or 0),
            # For the source lists and the smallest-first default pick;
            # 0 where the source states none (SubsPlease's API doesn't).
            "size_bytes": int(size_bytes or 0)}


# ------------------------------------------------------------ the sources

def animetosho(entry_title, season, episode, deadline) -> list:
    """Anime Tosho, by title.

    One request. The query is the entry's own title plus the episode
    number written the way fansubs write it, which is what its index
    holds - asking for "S02E05" alone finds only the releases that spell
    it that way, and most do not."""
    timeout = net.step_timeout(deadline, DEFAULT_TIMEOUT)
    if timeout is None:
        return []
    query = entry_title if not episode else f"{entry_title} {int(episode):02d}"
    url = f"{ANIMETOSHO_JSON}?q={urllib.parse.quote(query)}"
    try:
        rows = _get_json(url, timeout)
    except Exception:
        return []
    results = []
    for row in (rows if isinstance(rows, list) else [])[:MAX_ROWS * 2]:
        title = str(row.get("title") or row.get("torrent_name") or "")
        info_hash = str(row.get("info_hash") or "").lower()
        if not re.fullmatch(r"[0-9a-f]{40}", info_hash):
            info_hash = _hash_from(row.get("magnet_uri") or "")
        if not info_hash or not is_same_title(entry_title, title):
            continue
        if episode_match(title, season, episode) is None:
            continue
        results.append(_stream(title, info_hash,
                               _trackers_from(row.get("magnet_uri") or ""),
                               row.get("seeders"), "Anime Tosho",
                               size_bytes=row.get("total_size")))
        if len(results) >= MAX_ROWS:
            break
    return results


def subsplease(entry_title, season, episode, deadline) -> list:
    """SubsPlease's own release list, by title.

    The current season, which is precisely what it is for - it publishes
    the simulcast rips within hours and keeps one entry per episode per
    resolution. Its API answers a dict keyed by release, each row
    carrying `show`, `episode` and a `downloads` list of
    (resolution, magnet).

    The episode number here is the *show's* own numbering with no season
    in it, so a season-2 episode 5 is filed as whatever SubsPlease calls
    it - which is why the row's number is only trusted when it matches,
    and the release name is checked as well."""
    timeout = net.step_timeout(deadline, DEFAULT_TIMEOUT)
    if timeout is None:
        return []
    url = (f"{SUBSPLEASE_API}?f=search&tz=UTC"
           f"&s={urllib.parse.quote(entry_title)}")
    try:
        body = _get_json(url, timeout)
    except Exception:
        return []
    if not isinstance(body, dict):
        return []
    results = []
    for row in body.values():
        if not isinstance(row, dict):
            continue
        show = str(row.get("show") or "")
        if not is_same_title(entry_title, show):
            continue
        number = str(row.get("episode") or "").strip()
        if episode:
            try:
                if int(float(number)) != int(episode):
                    continue
            except (TypeError, ValueError):
                continue
        for download in row.get("downloads") or []:
            magnet = str(download.get("magnet") or "")
            info_hash = _hash_from(magnet)
            if not info_hash:
                continue
            resolution = str(download.get("res") or "").strip()
            title = f"[SubsPlease] {show} - {number} ({resolution}p)"
            results.append(_stream(title, info_hash, _trackers_from(magnet),
                                   0, "SubsPlease"))
        if len(results) >= MAX_ROWS:
            break
    return results


SOURCES = (("Anime Tosho", animetosho), ("SubsPlease", subsplease))


def search(entry, *, season=None, episode=None, deadline=None) -> list:
    """Every indexer, asked at once, for one episode of one entry.

    Concurrent and bounded by the number of sources - two threads, not a
    pool, because that is the whole population. Serially this is the sum
    of the slowest two (measured 0.4-2.5s each); together it is the
    slower one, and it runs beside the addon lookup rather than after
    it.

    Never raises and never returns None."""
    title = str((entry or {}).get("title") or "").strip()
    if not title:
        return []
    if deadline is None:
        deadline = net.deadline_in(12)

    def run(pair):
        _, fetch = pair
        try:
            return fetch(title, season, episode, deadline)
        except Exception:
            return []

    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(SOURCES)) as pool:
        for found in pool.map(run, SOURCES):
            results.extend(found or [])
    return results
