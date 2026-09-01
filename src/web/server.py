"""Atomic's Home and Discover, over HTTP, with no Qt in it.

Reads the same JSON files the app writes and hands them to the page as
plain rows. Nothing here is a view, and nothing here knows what a card
looks like.

Served over http:// deliberately. Every origin problem that cost days on
the QtWebEngine attempt - covers refused from an opaque origin, a page
the scheme handler could not answer - simply does not arise when a page
and its images share one ordinary origin.
"""

import json
import pathlib
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import backend

DATA = backend.DATA_DIR
COVERS = DATA / "image_cache"

if getattr(sys, "frozen", False):
    # PyInstaller unpacks datas to _MEIPASS; __file__ points inside the
    # archive, where nothing can be opened.
    STATIC = pathlib.Path(sys._MEIPASS) / "static"
else:
    STATIC = pathlib.Path(__file__).resolve().parent / "static"

MAGIC = {"image/png": b"\x89PNG\r\n\x1a\n", "image/jpeg": b"\xff\xd8"}

# What discover_cache.json calls each section, and what to call it on
# screen. The file is a *dictionary* keyed by these names, each holding
# {at, rows} - reading it as a list walks the keys instead, which are
# strings, so every row is filtered out and the page shows nothing
# against 1,287 cached titles. That was the bug.
DISCOVER_SECTIONS = (
    ("anime", "Anime"),
    ("series", "Series"),
    ("movie", "Movies"),
    ("reading_latest", "Latest chapters"),
    ("reading", "Reading"),
    ("medium:Manga", "Manga"),
    ("medium:Manhwa", "Manhwa"),
    ("medium:Manhua", "Manhua"),
    ("medium:Other", "Other"),
)

# Per section, not per page. The cache holds well over a thousand rows,
# and a strip that long is a thousand pictures fetched for a row nobody
# scrolls sideways.
DISCOVER_LIMIT = 60


def _rows(name):
    try:
        rows = json.loads((DATA / name).read_text(encoding="utf-8-sig"))
    except Exception:
        return []                # a lost list, never a lost page
    return [r for r in rows if isinstance(r, dict)]


def _history(kinds):
    """Watch or read history, newest first.

    series.json holds only what has been *saved* - three entries on his
    machine against 41 in History - so a page built from saved entries
    alone looks empty while he has plainly been watching things.
    """
    wanted = tuple(k.lower() for k in kinds)
    rows = [r for r in _rows("history.json")
            if str(r.get("type", "")).lower() in wanted]
    rows.sort(key=lambda r: str(r.get("last_opened") or ""), reverse=True)
    return rows


def _row(entry, kind="title", resume=False):
    """One entry, as the page wants it.

    `kind` is what the app should *do* with a click, decided here where
    the source file is known rather than guessed at the far end. A game
    launches, an app or website opens its targets, everything else opens
    the details page - and getting that wrong sent a clicked game to
    another title's episode list.
    """
    bits = [str(entry.get(key) or "").strip()
            for key in ("year", "progress", "quality")]
    # Whether the cover offers a continue ring. Home gave every tracker
    # medium two targets (the owner's ask - anime, series and movies used
    # to be one); games, apps and websites have nothing to resume, and
    # neither does a Discover row, which is not in the library at all -
    # `resume` is opt-in per section for exactly that reason.
    return {
        "kind": kind,
        "resume": bool(resume),
        "id": str(entry.get("id") or entry.get("entry_id")
                  or entry.get("key") or ""),
        "title": str(entry.get("title") or entry.get("name") or "").strip(),
        "type": str(entry.get("type") or ""),
        "meta": "  ".join(b for b in bits if b)[:40],
        "cover": backend.cover_url(entry),
        "url": str(entry.get("url") or ""),
    }


# How many titles the banner rotates through. Home shows one at a time
# and pages between them; more than a handful is a pager nobody reads.
HERO_SLIDES = 6


def _heroes(candidates):
    """Every banner-worthy entry, in order, without repeats.

    Saved entries are asked before history, because hero_backdrop and
    hero_logo are written onto the saved entry - a history row for the
    same show has neither.
    """
    found, seen = [], set()
    for entry in candidates:
        hero = backend.hero_for(entry)
        if not hero:
            continue
        key = (hero.get("id") or hero.get("title") or "").lower()
        if key in seen:
            continue
        seen.add(key)
        found.append(hero)
        if len(found) >= HERO_SLIDES:
            break
    return found


def _home():
    watching = _history(("Anime", "Series", "Movie"))
    reading = _rows("tracker.json")
    saved = _rows("series.json")

    # Match a history row to its saved entry so the banner can use the
    # art, and the row order can decide which show gets it.
    by_id = {str(e.get("id") or ""): e for e in saved + reading}
    by_title = {str(e.get("title") or "").strip().lower(): e
                for e in saved + reading}
    ordered = []
    for row in watching:
        entry = by_id.get(str(row.get("entry_id") or "")) \
            or by_title.get(str(row.get("title") or "").strip().lower())
        if entry is not None:
            ordered.append(entry)
    ordered.extend(saved)
    ordered.extend(reading)

    def _linked(name, kind):
        """Apps and websites: what they open is the useful second line."""
        out = []
        for entry in _rows(name):
            row = _row(entry, kind)
            # What it opens lives in `targets`, a list of
            # {type, target} - not in a flat `url` field like every
            # other kind of entry here.
            target = ""
            for item in (entry.get("targets") or []):
                if isinstance(item, dict) and item.get("target"):
                    target = str(item["target"])
                    break
            row["meta"] = (target or str(entry.get("url") or ""))[:60]
            row["url"] = target
            out.append(row)
        return out

    heroes = _heroes(ordered)
    return {"kind": "rows", "note": "",
            "hero": heroes[0] if heroes else None,
            "heroes": heroes, "sections": [
        {"title": "Watching",
         "rows": [_row(e, resume=True) for e in watching]},
        {"title": "Reading",
         "rows": [_row(e, resume=True) for e in reading]},
        {"title": "Games",
         "rows": [_row(e, "game") for e in _rows("games.json")]},
        # `style: list` - these two were a list of icon, name and link
        # in the Qt Home, not a shelf of posters, and a website has no
        # poster to show anyway.
        {"title": "Apps", "style": "list",
         "rows": _linked("apps.json", "app")},
        {"title": "Websites", "style": "list",
         "rows": _linked("websites.json", "site")},
    ]}


def _discover():
    try:
        cached = json.loads(
            (DATA / "discover_cache.json").read_text(encoding="utf-8-sig"))
    except Exception:
        cached = {}
    if not isinstance(cached, dict):
        cached = {}

    sections, total, newest, banner = [], 0, 0.0, None
    for key, label in DISCOVER_SECTIONS:
        block = cached.get(key)
        if not isinstance(block, dict):
            continue
        rows = [r for r in (block.get("rows") or [])
                if isinstance(r, dict) and r.get("title")]
        if not rows:
            continue
        total += len(rows)
        try:
            newest = max(newest, float(block.get("at") or 0))
        except (TypeError, ValueError):
            pass
        if banner is None:
            # Discover rows carry no hero art - they are search results,
            # not saved entries - so the banner is the first row's own
            # poster, widened and dimmed by the page. Better than no
            # banner, and honest about being the poster.
            first = rows[0]
            picture = backend.cover_url(first)
            if picture:
                banner = {"title": str(first.get("title") or ""),
                          "imdb": str(first.get("imdb_id") or ""),
                          "type": str(first.get("type") or ""),
                          "url": str(first.get("url") or ""),
                          # The same picture twice on purpose: blurred
                          # and blown up as the wide backdrop, and sharp
                          # at its own size as the cover. A discover row
                          # has no separate wide art to use.
                          "backdrop": picture, "cover": picture,
                          "logo": "",
                          "hide_title": False, "poster": True,
                          "meta": str(first.get("year") or ""),
                          "id": ""}
        sections.append({"title": f"{label}  ({len(rows)})",
                         "rows": [_row(e) for e in rows[:DISCOVER_LIMIT]]})

    note = f"{total} titles in {len(sections)} sections"
    if newest:
        age = (time.time() - newest) / 3600.0
        note += f", found {age:.0f}h ago" if age >= 1 else ", found just now"
    if not sections:
        note = "nothing cached - run a discover in the app first"
    return {"kind": "rows", "sections": sections, "note": note, "hero": banner}


def _search(text):
    """Every source's results for one query, as sections.

    The window's search field sends Enter here through
    WebDiscoverPage.start_search - main._search_in_discover navigates to
    Discover and then calls `start_search`, and a page without that
    method silently showed the ordinary Discover instead, which is
    exactly what the owner reported.

    Anime, series, movies and reading are asked in parallel: serially
    this is the sum of four site searches, and one slow source would
    hold up every other.
    """
    text = str(text or "").strip()
    if not text:
        return {"kind": "rows", "sections": [], "note": ""}

    from concurrent.futures import ThreadPoolExecutor
    from helpers import discover as finder

    def video(kind):
        try:
            return finder.discover_video(kind, text, limit=30) or []
        except Exception:
            return []

    def reading():
        try:
            return finder.discover_reading(text, limit=30) or []
        except Exception:
            return []

    with ThreadPoolExecutor(max_workers=4) as pool:
        jobs = {"Anime": pool.submit(video, "anime"),
                "Series": pool.submit(video, "series"),
                "Movies": pool.submit(video, "movie"),
                "Reading": pool.submit(reading)}
        found = {name: job.result() for name, job in jobs.items()}

    sections, total = [], 0
    for name in ("Anime", "Series", "Movies", "Reading"):
        rows = [r for r in found.get(name) or [] if isinstance(r, dict)
                and r.get("title")]
        if not rows:
            continue
        total += len(rows)
        sections.append({"title": f"{name}  ({len(rows)})",
                         "rows": [_row(e) for e in rows]})
    note = (f"{total} results for “{text}”" if total
            else f"nothing found for “{text}”")
    return {"kind": "rows", "sections": sections, "note": note, "hero": None}


def answer(route, query=None):
    query = query or {}
    one = lambda k, d="": (query.get(k) or [d])[0]      # noqa: E731

    if route == "home":
        return _home()
    if route == "discover":
        return _discover()
    if route == "featured":
        return backend.featured_art(one("title"), one("imdb"),
                                    one("type"))
    if route == "search":
        return _search(one("q"))
    if route == "settings":
        return backend.settings()
    if route == "hero":
        return backend.hero_meta(one("id"))
    if route == "chapters":
        return backend.chapters(one("id"), live=one("live") == "1")
    if route == "pages":
        try:
            index = int(one("i", "0"))
        except ValueError:
            index = 0
        return backend.pages(one("id"), index)
    if route == "read_state":
        return backend.read_state(one("id"))
    if route == "mark":
        return backend.mark_read(one("id"), one("key"),
                                 one("read", "1") == "1")
    return {"kind": "rows", "sections": [], "note": "not built yet"}


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        return                    # the console is not a request log

    def _send(self, body, kind, cache=False):
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        # Pictures never change under a given URL and are worth keeping.
        # Everything else must not be cached: the app writes these JSON
        # files while the page is open, and a cached answer is a page
        # showing what was true when it last looked. Measured - a fixed
        # discover route kept reporting the old count from cache while
        # the server returned the new one to curl.
        self.send_header("Cache-Control",
                         "max-age=86400" if cache else "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path, _, raw = self.path.partition("?")
        query = urllib.parse.parse_qs(raw)

        if path.startswith("/api/"):
            body = json.dumps(answer(path[5:], query), ensure_ascii=False)
            self._send(body.encode("utf-8"),
                       "application/json; charset=utf-8")
            return

        if path.startswith("/cover/"):
            target = COVERS / pathlib.Path(path[7:]).name
            try:
                blob = target.read_bytes()
            except OSError:
                self.send_error(404)
                return
            kind = "image/webp"
            for mime, magic in MAGIC.items():
                if blob.startswith(magic):
                    kind = mime
                    break
            self._send(blob, kind, cache=True)
            return

        if path.startswith("/img/"):
            blob, kind = backend.fetch_image(path[5:])
            if blob is None:
                self.send_error(404)
                return
            self._send(blob, kind, cache=True)
            return

        if path.startswith("/static/"):
            target = STATIC / pathlib.Path(path[8:]).name
            if not target.exists():
                self.send_error(404)
                return
            kind = ("text/css" if target.suffix == ".css"
                    else "application/javascript" if target.suffix == ".js"
                    else "text/plain")
            self._send(target.read_bytes(), kind + "; charset=utf-8")
            return

        self._send((STATIC / "index.html").read_bytes(),
                   "text/html; charset=utf-8")


def start(port=0):
    """Serve on a free port; returns the URL and the server."""
    server = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{server.server_address[1]}/", server
