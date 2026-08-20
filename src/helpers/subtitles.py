"""Arabic subtitles for the in-app player: finding them, downloading
them, and turning them into text a player can display.

**What was measured before this file was written**, because the honest
answer is smaller than it looks and the shape of this module follows it.
Every keyless Arabic subtitle source that could be found was probed live:

    OpenSubtitles v3 addon  works - 5 Arabic for a film (Inception),
                            **0 Arabic for both anime episodes tried**
    SubtitleCat             answers, Arabic per title, auto-translated
    AnimeTosho              answers - anime releases, not a subtitle
                            site, but it indexes Arabic-subbed releases
    Podnapisi               DNS failure from here
    SubSource               every documented endpoint 404s
    My-Subs                 404
    SubDL addon             DNS failure
    Wizdom                  400
    YifySubtitles           empty body
    wyzie                   401
    OpenSubtitles XML-RPC   401 (deprecated, keyless access removed)

So there is no set of six working Arabic subtitle APIs to wire up; most
of that world is dead, key-gated or behind Cloudflare. Padding the list
with sources that never answer would look like six sources and behave
like one, which is the failure mode this project has already paid for
elsewhere (a lookup that fails soft into silence).

What actually gets Arabic onto an anime episode here, in the order it
will happen in practice:

  1. **Subtitle tracks already inside the file.** Fansub and multi-sub
     releases carry Arabic in the container; mpv lists them and the
     player has a track picker. For this library that is the main path,
     and it costs no network at all.
  2. **A subtitle file the user already has**, via `load_local`.
  3. **The online sources below**, which are genuinely useful for films
     and popular live-action series and thin for seasonal anime.

Everything fails soft: a source that dies returns nothing and the others
still answer.
"""

import concurrent.futures
import gzip
import io
import json
import lzma
import os
import re
import urllib.parse
import urllib.request
import zipfile

from . import app_settings, net, title_match

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")

DEFAULT_TIMEOUT = 8

# The language labels the sources use for Arabic, all of them. `ara` is
# ISO 639-2, `ar` is 639-1, and the scrapers hand back the English word.
ARABIC_CODES = ("ar", "ara", "arabic", "ar-sa", "ara-sa", "arb")

# An extracted subtitle is a text file. Anything past this is not one,
# and unpacking it would be the zip-bomb the cap exists to stop.
MAX_SUBTITLE_BYTES = 8_000_000


def _headers(referer=None) -> dict:
    headers = {"User-Agent": _UA, "Accept": "*/*"}
    if referer:
        headers["Referer"] = referer
    return headers


def _get_bytes(url, timeout, referer=None, max_bytes=net.MAX_RESPONSE_BYTES):
    request = urllib.request.Request(url, headers=_headers(referer))
    deadline = net.deadline_in(timeout)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return net.read_bytes(response, deadline, max_bytes)


def _get_text(url, timeout, referer=None):
    return _get_bytes(url, timeout, referer).decode("utf-8", "replace")


def is_arabic_code(value) -> bool:
    return str(value or "").strip().lower() in ARABIC_CODES


# ------------------------------------------------------------- decoding

# Arabic letters. Counting these is how the right codepage is picked:
# every candidate below "succeeds" at decoding arbitrary bytes, so the
# only real test is whether the result contains Arabic.
_ARABIC_RANGE = re.compile(r"[؀-ۿݐ-ݿﭐ-﷿ﹰ-﻿]")
# Mojibake from reading cp1256 bytes as latin-1/utf-8 lands here.
_SUSPECT_RANGE = re.compile(r"[À-ÿ�]")

# Order matters only as a tie-break; the score below is what decides.
# cp1256 is in here because it is what a large share of Arabic .srt
# files on these sites actually are - reading one as UTF-8 gives a
# screenful of nonsense rather than an error, which is why "it decoded
# fine" is not evidence of anything.
_CODECS = ("utf-8-sig", "utf-8", "cp1256", "iso-8859-6", "cp1252", "latin-1")


def decode(raw: bytes) -> str:
    """Subtitle bytes as text, choosing the encoding correctly.

    **UTF-8 is tried strictly first, and if it succeeds it wins outright.**
    That ordering is not a preference, it is the whole correctness of this
    function, and scoring by "how much Arabic came out" gets it backwards:

      * UTF-8 is self-validating. Real cp1256 Arabic text is a stream of
        high bytes that is almost never a valid UTF-8 sequence, so a
        successful strict decode is strong evidence on its own.
      * The reverse is not true. cp1256 decodes *any* byte, and UTF-8
        Arabic read as cp1256 produces more Arabic-range characters than
        the correct answer does - the two-bytes-per-letter mojibake is
        itself made of Arabic letters.

    Measured, and the reason this was rewritten: a real Arabic .srt from
    OpenSubtitles scored higher as cp1256 under the old heuristic and came
    out as طھط±ط¬ظ…ط© - correct-looking to a scorer, unreadable to a person.

    Only once strict UTF-8 has actually failed is the legacy-codepage
    scoring below reached, which is where a genuine cp1256 file lands."""
    if not raw:
        return ""
    for codec in ("utf-8-sig", "utf-8"):
        try:
            return raw.decode(codec)
        except UnicodeDecodeError:
            pass
    best, best_score = "", None
    for codec in _CODECS:
        if codec.startswith("utf-8"):
            continue
        try:
            text = raw.decode(codec)
        except (UnicodeDecodeError, LookupError):
            continue
        score = len(_ARABIC_RANGE.findall(text)) - len(_SUSPECT_RANGE.findall(text))
        if best_score is None or score > best_score:
            best, best_score = text, score
    return best


def _unpack(raw: bytes, name_hint: str = "") -> bytes:
    """The subtitle out of whatever container it arrived in.

    These sources hand back .zip and .gz as often as a bare file, and a
    zip routinely holds a sample video or several releases' subtitles -
    so the largest subtitle-looking member is the one taken, not the
    first."""
    if raw[:2] == b"\x1f\x8b":
        try:
            return gzip.decompress(raw)[:MAX_SUBTITLE_BYTES]
        except Exception:
            return raw
    # AnimeTosho serves every attachment xz-compressed whatever the
    # extension says - a URL ending in .ass whose first bytes are
    # \xfd7zXZ (measured on a ToonsHub Arabic track). Without this the
    # bytes "decode" into codepage noise and the file is rejected as not
    # being a subtitle.
    if raw[:6] == b"\xfd7zXZ\x00":
        try:
            return lzma.decompress(raw)[:MAX_SUBTITLE_BYTES]
        except Exception:
            return raw
    if raw[:2] == b"PK":
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                members = [m for m in archive.infolist()
                           if m.filename.lower().endswith((".srt", ".ass", ".ssa", ".vtt", ".sub"))
                           and m.file_size <= MAX_SUBTITLE_BYTES]
                if not members:
                    return raw
                member = max(members, key=lambda m: m.file_size)
                with archive.open(member) as handle:
                    return handle.read(MAX_SUBTITLE_BYTES)
        except Exception:
            return raw
    return raw


def format_of(name: str, text: str = "") -> str:
    lowered = (name or "").lower()
    if lowered.endswith((".ass", ".ssa")) or "[script info]" in (text or "")[:400].lower():
        return "ass"
    if lowered.endswith(".vtt") or (text or "").lstrip().startswith("WEBVTT"):
        return "vtt"
    return "srt"


# -------------------------------------------------------------- parsing

_TIME_RE = re.compile(r"(\d+):(\d{2}):(\d{2})[,.](\d{1,3})")
_ASS_TAG_RE = re.compile(r"\{[^}]*\}")
_VTT_TAG_RE = re.compile(r"</?[cvbi][^>]*>")


def _seconds(hours, minutes, seconds, fraction) -> float:
    fraction = (fraction + "00")[:3]
    return int(hours) * 3600 + int(minutes) * 60 + int(seconds) + int(fraction) / 1000.0


def _parse_srt_vtt(text: str) -> list:
    cues = []
    for block in re.split(r"\r?\n\s*\r?\n", text.replace("﻿", "")):
        times = _TIME_RE.findall(block)
        if len(times) < 2:
            continue
        lines = [l for l in block.splitlines() if "-->" not in l]
        # Drop the leading sequence number an .srt block starts with.
        if lines and lines[0].strip().isdigit():
            lines = lines[1:]
        body = _VTT_TAG_RE.sub("", "\n".join(lines)).strip()
        if not body:
            continue
        cues.append({"start": _seconds(*times[0]), "end": _seconds(*times[1]),
                     "text": body})
    return cues


def _parse_ass(text: str) -> list:
    """ASS/SSA dialogue lines.

    Field order is read from the `Format:` line rather than assumed:
    it varies between releases, and a hardcoded index silently reads the
    style column as the subtitle text."""
    cues, fields = [], None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("format:"):
            candidate = [f.strip().lower() for f in stripped.split(":", 1)[1].split(",")]
            # Only the *Events* format line. A real .ass file carries two
            # `Format:` lines and the [V4+ Styles] one comes first, with
            # ~23 style columns - taking it made every Dialogue line look
            # short and parse to zero cues (measured on a Crunchyroll
            # Arabic track: 344 dialogue lines, 0 cues). The Events line
            # is the one that names Start and End.
            if "start" in candidate and "end" in candidate:
                fields = candidate
            continue
        if not stripped.lower().startswith("dialogue:"):
            continue
        if not fields:
            fields = ["layer", "start", "end", "style", "name", "marginl",
                      "marginr", "marginv", "effect", "text"]
        parts = stripped.split(":", 1)[1].split(",", len(fields) - 1)
        if len(parts) < len(fields):
            continue
        row = dict(zip(fields, parts))
        start, end = _TIME_RE.findall(row.get("start", "")), _TIME_RE.findall(row.get("end", ""))
        if not start or not end:
            # ASS times are h:mm:ss.cc - one digit of hours, which the
            # shared regex above does not match.
            start = re.findall(r"(\d+):(\d{2}):(\d{2})[.,](\d{1,3})", row.get("start", ""))
            end = re.findall(r"(\d+):(\d{2}):(\d{2})[.,](\d{1,3})", row.get("end", ""))
        if not start or not end:
            continue
        body = _ASS_TAG_RE.sub("", row.get("text", "")).replace("\\N", "\n").replace("\\n", "\n").strip()
        if body:
            cues.append({"start": _seconds(*start[0]), "end": _seconds(*end[0]),
                         "text": body})
    return cues


def parse(text: str, fmt: str = None) -> list:
    """Subtitle text as [{start, end, text}], seconds.

    Never raises: a malformed file yields the cues that did parse, which
    is what a player should show rather than nothing at all."""
    if not text:
        return []
    fmt = (fmt or format_of("", text)).lower()
    try:
        cues = _parse_ass(text) if fmt in ("ass", "ssa") else _parse_srt_vtt(text)
    except Exception:
        return []
    cues.sort(key=lambda cue: cue["start"])
    return cues


# -------------------------------------------------------------- sources

def _opensubtitles_v3(query, deadline) -> list:
    """The OpenSubtitles Stremio addon - keyless, needs an IMDb id.

    Measured: good Arabic coverage on films, effectively none on the
    seasonal anime in this library. Kept because when it does answer it
    is the best-matched result available, and it costs one request."""
    imdb_id = query.get("imdb_id")
    if not imdb_id:
        return []
    kind = "series" if query.get("episode") else "movie"
    stream_id = imdb_id
    if query.get("episode"):
        stream_id = f"{imdb_id}:{int(query.get('season') or 1)}:{int(query['episode'])}"
    url = (f"https://opensubtitles-v3.strem.io/subtitles/{kind}/"
           f"{urllib.parse.quote(stream_id, safe='')}.json")
    timeout = net.step_timeout(deadline, DEFAULT_TIMEOUT)
    if timeout is None:
        return []
    body = json.loads(_get_text(url, timeout))
    results, english = [], []
    for item in (body or {}).get("subtitles") or []:
        lang = str(item.get("lang") or "").lower()
        arabic = is_arabic_code(lang)
        # English rides along as well as Arabic now, deliberately: for
        # seasonal anime the measured Arabic coverage here is zero, and
        # an English track is the raw material the AI translator turns
        # into Arabic (see the player's subtitle worker). Anything else
        # is noise - a Vietnamese track unlocks nothing.
        if not arabic and lang not in ("eng", "en"):
            continue
        row = {
            "source": "OpenSubtitles",
            "name": os.path.basename(str(item.get("url") or "")) or "Subtitle",
            "release": str(item.get("id") or ""),
            "lang": "ar" if arabic else "en",
            "url": item.get("url"),
            "format": "srt",
            "rating": 0,
        }
        (results if arabic else english).append(row)
    return results + english[:4]


_SC_RESULT_RE = re.compile(r'href="(subs/[^"]+\.html)"[^>]*>(.*?)</a>', re.S | re.I)
_SC_ROW_RE = re.compile(
    r'<td[^>]*>\s*([A-Za-z؀-ۿ ]{2,24})\s*</td>.{0,400}?href="([^"]+\.srt)"',
    re.S | re.I)


def _subtitlecat(query, deadline) -> list:
    """SubtitleCat - searchable by title, carries Arabic for a wide
    catalogue because much of it is machine-translated.

    That is worth saying plainly in the UI rather than hiding: an
    auto-translated Arabic line is often serviceable and sometimes
    nonsense, and a user deserves to know which kind they picked. The
    `translated` flag rides along for exactly that."""
    title = query.get("title") or ""
    if not title:
        return []
    timeout = net.step_timeout(deadline, DEFAULT_TIMEOUT)
    if timeout is None:
        return []
    search_url = ("https://www.subtitlecat.com/index.php?search="
                  + urllib.parse.quote_plus(_search_terms(query)))
    body = _get_text(search_url, timeout)
    pages = _SC_RESULT_RE.findall(body)[:4]
    results = []
    for path, label in pages:
        timeout = net.step_timeout(deadline, DEFAULT_TIMEOUT)
        if timeout is None:
            break
        name = re.sub(r"<[^>]+>", "", label).strip()
        if title_match.similarity(title, name) < 0.30:
            continue
        page_url = "https://www.subtitlecat.com/" + path
        try:
            page = _get_text(page_url, timeout, referer=search_url)
        except Exception:
            continue
        for language, href in _SC_ROW_RE.findall(page):
            if "arab" not in language.strip().lower():
                continue
            results.append({
                "source": "SubtitleCat",
                "name": name or "Arabic subtitle",
                "release": name,
                "lang": "ar",
                "url": urllib.parse.urljoin(page_url, href.strip()),
                "format": "srt",
                "rating": 0,
                "translated": True,
                "referer": page_url,
            })
    return results


# One per-language attachment link on an AnimeTosho article page:
# `<a href="https://animetosho.org/storage/attach/...">Arabic [ara, ASS]</a>`.
_AT_ATTACH_RE = re.compile(
    r'<a href="(https://animetosho\.org/storage/attach/[^"]+)"[^>]*>'
    r'([^<]{0,90})</a>', re.I)
_AT_LANG_RE = re.compile(r"\[(\w{2,3}),\s*(\w{2,4})\]")

# How many release pages one search is allowed to open, and how many
# results are enough to stop early. Each page is a request; a search
# that opened every one of 47 matches would be most of the wait.
# Raised from 4/3 at the owner's ask for more Arabic on anime: this is
# the one source that actually carries it, so two more page fetches
# here buy more than any new source measured to date.
_AT_MAX_PAGES = 6
_AT_ENOUGH = 5


def _animetosho(query, deadline) -> list:
    """AnimeTosho, for the case the other sources are worst at: anime.

    Not a subtitle site - it indexes anime releases - but every release
    page lists the subtitle tracks *inside* the release as individually
    downloadable attachments, per language. Measured on a real seasonal
    episode (Solo Leveling S02E05): the ToonsHub multi-sub release
    carries 19 attachments including **Arabic [ara, ASS]** - actual
    scanlator-grade Arabic, keyless, for an episode every subtitle site
    measured had nothing for. This is the anime path.

    The first version of this source returned the article *page* URL
    with a `needs_page` flag that nothing ever resolved, so every one of
    its results failed to download - a source that answers and can never
    deliver. The page walk happens here now, bounded, and what comes
    back is the direct attachment URL (xz-compressed - see _unpack)."""
    title = query.get("title") or ""
    episode = query.get("episode")
    if not title or not episode:
        return []
    timeout = net.step_timeout(deadline, DEFAULT_TIMEOUT)
    if timeout is None:
        return []
    # The fansub numbering, not S01E05: these releases write "Title - 05"
    # or "S02E05" under the show's own season, and the plain form matches
    # both (the feed's search is a text search).
    terms = f"{title} {int(episode):02d}"
    url = "https://feed.animetosho.org/json?q=" + urllib.parse.quote_plus(terms)
    body = json.loads(_get_text(url, timeout))
    rows = body if isinstance(body, list) else []
    if not rows and int(query.get("season") or 0) > 1:
        # Nothing under the plain form - the release may be filed only
        # under SxxEyy. One extra request, and only on a miss, so an
        # episode the first query answered costs nothing more.
        timeout = net.step_timeout(deadline, DEFAULT_TIMEOUT)
        if timeout is None:
            return []
        terms = f"{title} S{int(query['season']):02d}E{int(episode):02d}"
        try:
            body = json.loads(_get_text(
                "https://feed.animetosho.org/json?q="
                + urllib.parse.quote_plus(terms), timeout))
            rows = body if isinstance(body, list) else []
        except Exception:
            rows = []

    def episode_stated(name):
        return re.search(rf"(?:^|[\s\-_\[(e])0?{int(episode)}(?:v\d)?(?=[\s\-_\])]|$)",
                         name, re.I)

    # Multi-sub groups first - they are the ones that carry Arabic at
    # all, and page fetches are the budget being spent.
    candidates = [r for r in rows
                  if episode_stated(str(r.get("title") or ""))
                  and title_match.similarity(title, str(r.get("title") or "")) > 0.2]
    candidates.sort(key=lambda r: 0 if re.search(
        r"multi|toonshub|erai", str(r.get("title") or ""), re.I) else 1)

    results, pages = [], 0
    for row in candidates:
        if pages >= _AT_MAX_PAGES or len(results) >= _AT_ENOUGH:
            break
        link = row.get("link") or row.get("article_url")
        if not link:
            continue
        timeout = net.step_timeout(deadline, DEFAULT_TIMEOUT)
        if timeout is None:
            break
        pages += 1
        try:
            page = _get_text(link, timeout)
        except Exception:
            continue
        release = str(row.get("title") or "")[:110]
        for attach_url, label in _AT_ATTACH_RE.findall(page):
            label = label.strip()
            found = _AT_LANG_RE.search(label)
            code = (found.group(1).lower() if found else "")
            if not is_arabic_code(code) and "arab" not in label.lower():
                continue
            results.append({
                "source": "AnimeTosho",
                "name": f"{label} — {release}",
                "release": release,
                "lang": "ar",
                "url": attach_url,
                "format": (found.group(2).lower() if found else "ass"),
                "rating": int(row.get("seeders") or 0),
            })
    return results


def _subdl(query, deadline) -> list:
    """SubDL, once the owner has pasted a key in Settings (API Keys).

    Dark without one on purpose - the key field exists precisely so this
    can light up without a new build. Asked by IMDb id first, which
    cannot match the wrong title the way a name search can."""
    key = app_settings.get_subdl_key()
    if not key:
        return []
    timeout = net.step_timeout(deadline, DEFAULT_TIMEOUT)
    if timeout is None:
        return []
    params = {"api_key": key, "languages": "AR", "subs_per_page": "30"}
    if query.get("imdb_id"):
        params["imdb_id"] = query["imdb_id"]
    else:
        params["film_name"] = query.get("title") or ""
    if query.get("episode"):
        params["type"] = "tv"
        params["season_number"] = str(int(query.get("season") or 1))
        params["episode_number"] = str(int(query["episode"]))
    else:
        params["type"] = "movie"
    url = "https://api.subdl.com/api/v1/subtitles?" + urllib.parse.urlencode(params)
    body = json.loads(_get_text(url, timeout))
    if not isinstance(body, dict) or not body.get("status"):
        return []
    results = []
    for item in body.get("subtitles") or []:
        path = str(item.get("url") or "")
        if not path:
            continue
        name = str(item.get("release_name") or item.get("name") or "Arabic subtitle")
        results.append({
            "source": "SubDL",
            "name": name[:120],
            "release": name[:120],
            "lang": "ar",
            # The API hands back a site-relative path; the files live on
            # the dl host, zipped.
            "url": urllib.parse.urljoin("https://dl.subdl.com/", path),
            "format": "srt",
            "rating": 0,
        })
    return results


def _search_terms(query) -> str:
    """What to type into a search box for this episode."""
    title = query.get("title") or ""
    if query.get("episode"):
        season = int(query.get("season") or 1)
        return f"{title} S{season:02d}E{int(query['episode']):02d}"
    if query.get("year"):
        return f"{title} {query['year']}"
    return title


# Ordered by how well they measured. Each is called inside its own
# try/except: one source failing must never empty the list. SubDL leads
# because it is the one keyed source - a result from it was asked for by
# name, with a pasted credential, and outranks scraping.
_SOURCES = (
    ("SubDL", _subdl),
    ("OpenSubtitles", _opensubtitles_v3),
    ("SubtitleCat", _subtitlecat),
    ("AnimeTosho", _animetosho),
)


def sources() -> tuple:
    """Source names, for the UI to group results under."""
    return tuple(name for name, _ in _SOURCES)


def search(title, *, year=None, season=None, episode=None, imdb_id=None,
           kind="series", deadline=None) -> list:
    """Arabic subtitles for this episode or film, best match first.

    The sources run together, not in a row: AnimeTosho now walks release
    pages and SubtitleCat walks result pages, so serially this was the
    sum of every page fetch and the last source regularly fell off the
    deadline. Four short-lived threads, one per source - the same shape
    streams.find_streams uses, and for the same reason.

    Never raises, never returns None."""
    if deadline is None:
        deadline = net.deadline_in(24)
    query = {"title": title or "", "year": year, "season": season,
             "episode": episode, "imdb_id": imdb_id, "kind": kind}

    def run(pair):
        _, source = pair
        try:
            return source(query, deadline) or []
        except Exception:
            return []

    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(_SOURCES)) as pool:
        for found in pool.map(run, _SOURCES):
            results.extend(found)
    return _rank(results, query)


def _rank(results, query) -> list:
    """Arabic before anything else, then exact episode match, then a real
    translation over a machine one, then whatever the source rated it.

    Language leads because English results exist here only as feedstock
    for the AI translator - useful, but never ahead of actual Arabic."""
    wanted = ""
    if query.get("episode"):
        wanted = f"s{int(query.get('season') or 1):02d}e{int(query['episode']):02d}"

    def key(item):
        name = (item.get("release") or item.get("name") or "").lower()
        return (0 if is_arabic_code(item.get("lang")) else 1,
                0 if wanted and wanted in name else 1,
                1 if item.get("translated") else 0,
                -int(item.get("rating") or 0))

    seen, unique = set(), []
    for item in sorted(results, key=key):
        marker = item.get("url")
        if not marker or marker in seen:
            continue
        seen.add(marker)
        unique.append(item)
    return unique


def fetch(entry: dict, deadline=None) -> str:
    """Download one search result and return it as decoded text.

    Returns None rather than raising - the player turns that into "that
    subtitle could not be downloaded" and leaves playback alone."""
    if not entry or not entry.get("url"):
        return None
    if deadline is None:
        deadline = net.deadline_in(20)
    timeout = net.step_timeout(deadline, DEFAULT_TIMEOUT * 2)
    if timeout is None:
        return None
    try:
        raw = _get_bytes(entry["url"], timeout, referer=entry.get("referer"),
                         max_bytes=MAX_SUBTITLE_BYTES)
    except Exception:
        return None
    raw = _unpack(raw, entry.get("name") or "")
    text = decode(raw)
    # A downloaded HTML error page decodes perfectly and is not a
    # subtitle; parse() would return zero cues and the player would show
    # a blank track with no explanation.
    if not text or (not _TIME_RE.search(text) and "dialogue:" not in text.lower()):
        return None
    return text


def load_local(path: str) -> str:
    """A subtitle file the user already has, decoded the same way.

    Present because it is the one path that always works - no source,
    no network, no coverage question."""
    try:
        with open(path, "rb") as handle:
            return decode(_unpack(handle.read(MAX_SUBTITLE_BYTES), path))
    except Exception:
        return None
