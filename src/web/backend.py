"""The web pages' bridge to Atomic's real data.

Of this app's 98 helpers, 37 need Qt and all 37 are UI - so everything
this file reads (storage, history, app_settings, chapter_source) is the
app's own code rather than a second implementation of it. `images` is the
one exception, and is deliberately not used: it builds QPixmaps, and a
browser decodes its own pictures.

Pictures come through this module rather than being linked directly.
Three reasons, each one a bug that happened:

  * covers live in %APPDATA%/Atomic/image_cache, hero art in logo_cache,
    and game art in game_art - all absolute paths on disk, which a page
    served over http:// cannot open;
  * a discover row spells its picture `poster`, a saved entry spells it
    `cover_url`, and a game spells it `cover` - reading only the first
    left every discover card and every game blank;
  * manga hosts check the Referer and serve a placeholder without it.
"""

import hashlib
import os
import pathlib
import sys
import threading
import urllib.request
from datetime import datetime

_SRC = pathlib.Path(__file__).resolve().parent.parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from helpers import storage                                   # noqa: E402

# storage picks src/data from a source tree and %APPDATA%/Atomic only
# when frozen, so a page run from source would show an empty library
# beside a full app. ATOMIC_DATA_DIR overrides it, which is how a test
# points this at a copy rather than the real thing.
DATA_DIR = pathlib.Path(os.environ.get("ATOMIC_DATA_DIR")
                        or (pathlib.Path(os.environ.get("APPDATA", "."))
                            / "Atomic"))
storage.DATA_DIR = DATA_DIR

try:
    from helpers import chapter_source
except Exception:
    chapter_source = None
try:
    from helpers import net
except Exception:
    net = None

READING = ("manga", "manhwa", "manhua")
COVERS = DATA_DIR / "image_cache"

# token -> (where it came from, headers it needs). Remote pictures and
# on-disk ones both land here so the page has one kind of image URL.
_sources = {}
_lock = threading.Lock()


def _token(key):
    return hashlib.sha1(str(key).encode("utf-8")).hexdigest()[:16]


def _load(name):
    try:
        rows = storage.load(name, [])
    except Exception:
        return []
    return [r for r in rows if isinstance(r, dict)]


def local_url(path):
    """A URL for a file already on this machine."""
    path = str(path or "").strip()
    if not path or not os.path.isfile(path):
        return ""
    token = _token(path)
    with _lock:
        _sources[token] = (path, None)
    return "/img/" + token


def remote_url(url, headers=None):
    """A URL for a picture this server will fetch on the page's behalf."""
    url = str(url or "").strip()
    if not url:
        return ""
    token = _token(url)
    with _lock:
        _sources[token] = (url, dict(headers or {}))
    return "/img/" + token


def cover_url(entry):
    """This entry's picture, wherever it keeps it.

    The order matters: a cached file beats a remote URL because it is
    already on disk, and `poster`/`cover`/`icon` are here because
    discover rows and games spell it those ways. Leaving any of them out
    is a page of blank cards.
    """
    name = pathlib.Path(str(entry.get("cover_path") or "")).name
    if name and (COVERS / name).exists():
        return "/cover/" + name
    # `image` and `art` are what apps and websites call theirs, `cover`
    # and `icon` what games do - all absolute paths on disk. Reading only
    # some of them is a row of blank cards, which is how Apps and
    # Websites first arrived on Home.
    for key in ("cover", "icon", "image", "art"):
        value = str(entry.get(key) or "")
        if value and os.path.isabs(value):
            found = local_url(value)
            if found:
                return found
    for key in ("cover_url", "poster", "cover", "image"):
        value = str(entry.get(key) or "")
        if value.startswith("http"):
            return remote_url(value)
    return ""


def hero_for(entry):
    """The banner art an entry carries, if it has any.

    hero_backdrop and hero_logo are absolute paths into logo_cache,
    written by helpers/hero_art. The Qt Home draws exactly these, so the
    web Home draws exactly these - anything else would be a different
    banner that merely looked similar.
    """
    if not isinstance(entry, dict):
        return None
    backdrop = local_url(entry.get("hero_backdrop"))
    if not backdrop:
        return None
    return {
        "title": str(entry.get("title") or ""),
        "backdrop": backdrop,
        "logo": local_url(entry.get("hero_logo")),
        # The entry's own cover, beside the wide art. The banner had
        # only the backdrop and the owner asked for the cover on it.
        "cover": cover_url(entry),
        # The medium decides the second button's wording - View
        # Chapters, View Episodes or View Details - exactly as
        # home._hero_open does.
        "type": str(entry.get("type") or ""),
        "hide_title": bool(entry.get("hero_hide_title")),
        "meta": _readable(entry.get("next_release"))
                or str(entry.get("progress") or ""),
        # The bullet line the Qt banner carried - runtime, years, genres
        # - built here so the page only has to join them.
        "bullets": _bullets(entry),
        "id": str(entry.get("id") or entry.get("entry_id") or ""),
    }


def _bullets(entry):
    """The short facts a banner shows, in the order the Qt one used.

    Only what the entry actually has: an empty bullet reads as a stray
    separator, which is worse than a shorter line.
    """
    out = []
    runtime = str(entry.get("runtime") or entry.get("duration") or "").strip()
    if runtime:
        out.append(runtime if "min" in runtime.lower() else f"{runtime} min")
    years = str(entry.get("years") or entry.get("year") or "").strip()
    if years:
        out.append(years)
    kind = str(entry.get("type") or "").strip()
    if kind:
        out.append(kind)
    progress = str(entry.get("progress") or "").strip()
    if progress:
        out.append(progress)
    genres = entry.get("genres")
    if isinstance(genres, list) and genres:
        out.extend(str(g).strip() for g in genres[:3] if str(g).strip())
    elif str(genres or "").strip():
        out.append(str(genres).strip())
    country = str(entry.get("country") or "").strip()
    if country:
        out.append(country)
    seen, unique = set(), []
    for item in out:
        low = item.lower()
        if low not in seen:
            seen.add(low)
            unique.append(item)
    return unique[:6]


def fetch_image(token):
    """One picture: read off disk, or fetched with the headers it needs.

    Through net.urlopen/read_bytes, never resp.read(): urlopen's timeout
    bounds one socket operation and not the transfer, so a host sending a
    byte a second holds the connection open forever.
    """
    with _lock:
        found = _sources.get(token)
    if not found:
        return None, None
    where, headers = found

    if headers is None:                      # a file on this machine
        try:
            blob = pathlib.Path(where).read_bytes()
        except OSError:
            return None, None
        return blob, _kind(blob[:8])

    disk = COVERS / ("web_" + token)
    if disk.exists():
        try:
            blob = disk.read_bytes()
            return blob, _kind(blob[:8])
        except OSError:
            pass
    if net is None:
        return None, None
    try:
        request = urllib.request.Request(net.ascii_url(where), headers=headers)
        deadline = net.deadline_in(12.0)
        with net.urlopen(request, timeout=12.0) as response:
            blob = net.read_bytes(response, deadline)
    except Exception:
        return None, None
    if not blob:
        return None, None
    try:
        COVERS.mkdir(parents=True, exist_ok=True)
        disk.write_bytes(blob)
    except OSError:
        pass                                 # a cold cache, never a lost page
    return blob, _kind(blob[:8])


def _kind(head):
    if head.startswith(b"\x89PNG"):
        return "image/png"
    if head.startswith(b"\xff\xd8"):
        return "image/jpeg"
    if head[:4] == b"RIFF":
        return "image/webp"
    if head.startswith(b"GIF8"):
        return "image/gif"
    return "application/octet-stream"


def _readable(value):
    """One stored field as a line a person can read.

    next_release is a record - {at, chapter, episode, season, estimated,
    source} - and printing it raw put a Python dict on the page. It says
    "estimated" out loud because MangaDex announces no dates and that
    number is extrapolated from release history.
    """
    if value is None or value == "":
        return ""
    if isinstance(value, list):
        return ", ".join(str(v) for v in value if v not in (None, ""))
    if isinstance(value, bool):
        return "yes" if value else "no"
    if not isinstance(value, dict):
        return str(value).strip()

    bits = []
    chapter, season, episode = (value.get("chapter"), value.get("season"),
                                value.get("episode"))
    if chapter not in (None, ""):
        bits.append(f"Chapter {chapter}")
    elif episode not in (None, ""):
        bits.append(f"S{int(season or 1):02d}E{int(episode):02d}")
    when = str(value.get("at") or "")
    if when:
        try:
            bits.append(datetime.fromisoformat(
                when.replace("Z", "+00:00")).strftime("%d %b %Y"))
        except ValueError:
            bits.append(when[:10])
    if value.get("estimated"):
        bits.append("estimated")
    source = str(value.get("source") or "")
    if source:
        bits.append(f"via {source}")
    return "  ·  ".join(bits)


def entry_by_id(entry_id):
    if not entry_id:
        return None
    for name in ("tracker.json", "series.json", "games.json"):
        for row in _load(name):
            if str(row.get("id") or "") == entry_id:
                return row
    for row in _load("history.json"):
        if entry_id in (str(row.get("entry_id") or ""),
                        str(row.get("key") or "")):
            return dict(row, id=entry_id)
    return None


def settings():
    """What Settings shows, through app_settings' public getters.

    Not its `_load`: each getter carries the default and the coercion for
    its own key, and reading the raw dictionary would duplicate both.
    """
    rows, keys = [], []
    try:
        from helpers import app_settings as aps
    except Exception:
        return {"rows": [], "keys": [], "data_dir": str(DATA_DIR)}

    for label, getter in (
            ("Full screen on startup", "get_fullscreen_on_startup"),
            ("Preferred resolution", "get_preferred_resolution"),
            ("Pick a source automatically", "get_auto_pick_source"),
            ("Last version seen", "get_last_seen_version"),
            ("Sidebar order", "get_nav_order"),
            ("Hidden sections", "get_hidden_sections")):
        fn = getattr(aps, getter, None)
        if not callable(fn):
            continue
        try:
            value = fn()
        except Exception:
            continue
        if isinstance(value, list):
            value = ", ".join(str(v) for v in value)
        if isinstance(value, bool):
            value = "on" if value else "off"
        rows.append({"label": label, "value": str(value)[:200]})

    # Reported as set or not set, never shown: a page that prints the
    # owner's key is a page that leaks it.
    for name, getter in (("TMDB", "get_tmdb_key"), ("SubDL", "get_subdl_key"),
                         ("Debrid", "get_debrid_key")):
        fn = getattr(aps, getter, None)
        if callable(fn):
            try:
                keys.append({"label": name, "set": bool((fn() or "").strip())})
            except Exception:
                pass
    return {"rows": rows, "keys": keys, "data_dir": str(DATA_DIR)}


# ---- the reader ------------------------------------------------------
# A full chapter list was measured at 21.7s cold on the owner's own
# 249-chapter entry, so `chapters` is its own route and the page asks for
# it after it has drawn something (rule 7). `cached_chapters` is a
# dictionary lookup and answers in the same frame.

def _chapter_row(chapter, index):
    number = chapter.get("number")
    name = str(chapter.get("title") or "").strip()
    label = f"Chapter {number}" if number not in (None, "") else (name or "?")
    return {"i": index,
            "key": f"c{number}" if number not in (None, "") else "",
            "label": label,
            "sub": name if name and str(number) not in name else ""}


def chapters(entry_id, live=False):
    """This entry's chapters. `live` goes to the site; otherwise cache."""
    entry = entry_by_id(entry_id)
    if entry is None or chapter_source is None:
        return {"items": [], "error": "no reader here"}
    try:
        found = (chapter_source.list_chapters(entry) if live
                 else chapter_source.cached_chapters(entry)) or []
    except Exception as exc:
        return {"items": [], "error": str(exc)[:160]}
    return {"items": [_chapter_row(c, i) for i, c in enumerate(found)],
            "title": str(entry.get("title") or ""),
            "cached": not live}


def pages(entry_id, index):
    """The images of one chapter, as URLs this server will proxy.

    Proxied rather than linked because these hosts check the Referer and
    serve a placeholder to anyone who omits it - chapter_pages hands the
    headers back with the list, and they are replayed by fetch_image.
    """
    entry = entry_by_id(entry_id)
    if entry is None or chapter_source is None:
        return {"pages": [], "error": "no reader here"}
    try:
        found = chapter_source.cached_chapters(entry) or []
        if not found or index >= len(found):
            found = chapter_source.list_chapters(entry) or []
        if index < 0 or index >= len(found):
            return {"pages": [], "error": "no such chapter"}
        chapter = found[index]
        answer = chapter_source.chapter_pages(chapter) or {}
    except Exception as exc:
        return {"pages": [], "error": str(exc)[:160]}

    head = dict(answer.get("headers") or {})
    urls = [remote_url(u, head) for u in (answer.get("pages") or [])]
    row = _chapter_row(chapter, index)
    # The medium decides how wide a page is drawn - reader.py's
    # MEDIUM_TARGET_WIDTH, manga 1100 against manhwa/manhua 762. It is
    # the difference between a page and a vertical strip, and reading
    # both at one width is what made the reader feel unlike the old one.
    medium = str(entry.get("type") or "").strip().lower()
    return {"pages": urls, "count": len(urls), "label": row["label"],
            "key": row["key"], "index": index, "total": len(found),
            "medium": medium if medium in READING else "manga",
            "title": str(entry.get("title") or "")}


def mark_read(entry_id, key, read=True):
    """Record a chapter as read, the way the Qt reader does."""
    entry = entry_by_id(entry_id)
    if entry is None or not key:
        return {"ok": False}
    try:
        from helpers import history
        history.set_watched(entry, [key], watched=bool(read))
        history.touch(entry)
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:160]}
    return {"ok": True}


def read_state(entry_id):
    """Which chapter keys this entry already has marked read."""
    entry = entry_by_id(entry_id)
    if entry is None:
        return {"watched": []}
    title = str(entry.get("title") or "").strip().lower()
    for row in _load("history.json"):
        same = (str(row.get("entry_id") or "") == str(entry.get("id") or "")
                or str(row.get("title") or "").strip().lower() == title)
        if same:
            return {"watched": sorted(str(k) for k in (row.get("watched") or []))}
    return {"watched": []}


def hero_meta(entry_id):
    """The banner's fact line, the way the details page builds it.

    Deliberately its own route, and asked for *after* the banner is
    drawn: runtime, years and rating come from Cinemeta and that is a
    network call. The banner shows what the entry already knows first
    and this replaces it when it arrives - never the other way round
    (rule 7).

    The order and the wording are details._fill_facts's, so the two
    surfaces cannot drift: runtime, then years, then the rating with its
    star and "IMDb".
    """
    entry = entry_by_id(entry_id)
    if entry is None:
        return {"bullets": []}
    imdb = str(entry.get("imdb_id") or "").strip()
    if not imdb:
        return {"bullets": []}
    kind = "movie" if str(entry.get("type") or "").lower().startswith("movie")         else "series"
    try:
        from helpers import stremio
        meta = stremio.fetch_meta_cached(imdb, kind) or {}
    except Exception:
        return {"bullets": []}

    bullets = []
    runtime = str(meta.get("runtime") or "").strip()
    if runtime:
        bullets.append(runtime)
    years = str(meta.get("releaseInfo") or meta.get("year") or "").strip()
    if years:
        bullets.append(years)
    rating = str(meta.get("imdbRating") or "").strip()
    if rating:
        bullets.append(f"★ {rating} IMDb")
    genres = meta.get("genres")
    if isinstance(genres, list):
        bullets.extend(str(g).strip() for g in genres[:3] if str(g).strip())
    return {"bullets": bullets}


def featured_art(title, imdb="", kind=""):
    """A discover title's wide still and title treatment.

    The same three calls tracker._featured_backdrop_worker makes: the
    small backdrop first because it is there in a moment, then the
    full-resolution one, then the logo. A discover row has no saved
    entry, so everything is found by name (and by IMDb id where the row
    carries one).
    """
    title = str(title or "").strip()
    if not title:
        return {"backdrop": "", "logo": ""}
    probe = {"title": title, "imdb_id": str(imdb or ""),
             "type": str(kind or "")}
    backdrop = logo = ""
    try:
        from helpers import artwork
        found = artwork.backdrop_path(probe) or artwork.backdrop_fast_path(probe)
        backdrop = local_url(found) if found else ""
    except Exception:
        pass
    try:
        from helpers import artwork
        found = artwork.logo_path(probe) or artwork.logo_path_by_title(title)
        logo = local_url(found) if found else ""
    except Exception:
        pass
    return {"backdrop": backdrop, "logo": logo}
