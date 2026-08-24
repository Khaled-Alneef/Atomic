"""Chapter lists and page images for the in-app reader, in Arabic.

**Measured before this was written**, against the owner's own reading
sites and real entries - the shapes here are the ones that answered, not
the ones the themes document:

    3asq.online      chapter list is *not* in the series HTML at all.
                     POST to `<series-url>/ajax/chapters/` returns it -
                     560 chapters for One Piece. This is Madara's ajax
                     shape; the admin-ajax.php variant 400s here.
    olympustaff.com  chapters are plain `/series/<slug>/<n>` links on
                     the series page - 248 for The Beginning After the
                     End.
    lavascans.com    240 `...-chapter-<n>/` links straight out of the
                     series HTML.
    MangaDex (ar)    real but thin: One Piece 148 Arabic chapters,
                     Kingdom 0, The Beginning After the End 0.

So the owner's own Arabic scan sites are the primary source and MangaDex
is the fallback, which is the opposite of how it was scoped - worth
saying, because it is why the site shapes below get the detailed
treatment and MangaDex gets one function.

Everything fails soft: a source that dies returns nothing and the reader
says which, rather than spinning. Nothing here raises.
"""

import concurrent.futures
import html
import json
import re
import time
import urllib.parse
import urllib.request

from . import manga_sites, mangadex, net, storage, title_match

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")

DEFAULT_TIMEOUT = 10
# A series page can be a couple of MB of HTML; see _site_chapters.
SERIES_PAGE_TIMEOUT = 25
# Below this a chapter list is treated as a partial one and the ajax
# endpoint is asked as well.
MIN_TRUSTED_CHAPTERS = 3
# And the bar for showing a first page to the reader before the rest of
# a paginated list arrives (see on_partial in _site_chapters). Higher
# than the trust bar above on purpose: that one decides whether to ask
# the ajax endpoint as well, this one decides what a person sees. A real
# first page is forty chapters (olympustaff, measured); Madara's inline
# links are one or two, and one chapter on screen reads as a series with
# one chapter, not as a list still loading.
MIN_PARTIAL_CHAPTERS = 10

# How many extra `?page=N` pages of a chapter list to follow, and how
# many at once. Both bounded on purpose: a series page that paginates
# says how many pages it has, but a themed site can also print a
# hundred, and a chapter list is not worth a hundred requests. Six at a
# time rather than one after another because these hosts answer in
# ~1-3s each - measured on olympustaff, six pages in 3.7s concurrently
# against ~9s serially, which is the difference between fitting inside
# the reader's listing budget and not.
MAX_LIST_PAGES = 24
LIST_PAGE_WORKERS = 6

# How long one *other* site may be searched for this title before the
# sweep moves on - see _other_site_chapters.
OTHER_SITE_BUDGET = 5.0

# Reading direction. Manga is drawn right-to-left; manhwa and manhua are
# vertical scrolls that happen to read left-to-right. The reader turns
# "rtl" into right-to-left paging and "ltr" into a continuous strip,
# which matches how each is actually published.
_RTL_TYPES = ("Manga",)


def _headers(referer=None, accept=None) -> dict:
    headers = {"User-Agent": _UA,
               "Accept": accept or "text/html,application/xhtml+xml,image/webp,*/*"}
    if referer:
        parts = urllib.parse.urlsplit(referer)
        headers["Referer"] = referer
        headers["Origin"] = f"{parts.scheme}://{parts.netloc}"
    return headers


# These platforms content-negotiate on Accept. Asking for HTML - which is
# the sensible default for a scraper - makes the API hand back the
# single-page app's shell instead of JSON, and the parse fails on a
# response that looked fine. Measured on meshmanga: identical URL, HTML
# with the default header, JSON with this one.
_JSON_ACCEPT = "application/json, text/plain, */*"


def _get(url, timeout, referer=None, post=None, accept=None):
    headers = _headers(referer, accept)
    if post is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        headers["X-Requested-With"] = "XMLHttpRequest"
    request = urllib.request.Request(url, data=post, headers=headers)
    deadline = net.deadline_in(timeout)
    with net.urlopen(request, timeout=timeout) as response:
        return net.read_text(response, deadline)


# ------------------------------------------------------------ numbering

_NUMBER_RE = re.compile(r"(\d+(?:\.\d+)?)")


def _number_from(text: str):
    """The chapter number out of a URL or a label.

    Takes the *last* number in a URL, because the series slug routinely
    carries one of its own - lavascans' `2072267132-the-eternal-supreme`
    would otherwise make every chapter number 2072267132."""
    numbers = _NUMBER_RE.findall(text or "")
    if not numbers:
        return None
    try:
        value = float(numbers[-1])
    except ValueError:
        return None
    return int(value) if value.is_integer() else value


def _sort_key(chapter):
    number = chapter.get("number")
    return number if isinstance(number, (int, float)) else -1


# ------------------------------------------------------------ site path

_HREF_RE = re.compile(r'href="([^"]+)"', re.I)
_TAG_RE = re.compile(r"<[^>]+>")


def _clean(text: str) -> str:
    text = _TAG_RE.sub(" ", text or "")
    text = (text.replace("&#8211;", "-").replace("&amp;", "&")
                .replace("&nbsp;", " ").replace("&#8217;", "'"))
    return " ".join(text.split())


def _madara_ajax(series_url: str, timeout: float) -> str:
    """Madara's chapter list, which the series page does not contain.

    Measured on 3asq: the series HTML holds two chapter links, this
    holds 560. A reader built on the page alone would show a series as
    having no chapters and look broken."""
    url = series_url.rstrip("/") + "/ajax/chapters/"
    return _get(url, timeout, referer=series_url, post=b"")


def _chapters_from_html(body: str, series_url: str, entry_type=None) -> list:
    """Every chapter link in a blob of HTML, deduped and numbered.

    Deliberately generic - an anchor whose URL ends in a number or says
    "chapter" is a chapter on every one of these themes, and matching
    the themes individually is how a per-host scraper zoo starts."""
    base = series_url.rstrip("/")
    slug = urllib.parse.urlsplit(base).path.rstrip("/").split("/")[-1]
    found, seen = [], set()
    # Anchor plus its inner text, so a chapter can carry its own title.
    for match in re.finditer(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', body,
                             re.S | re.I):
        # **Unescaped before anything parses it.** An href written
        # `/series/the-villainess&#x27;s-last-dance/chapter-19` carries a
        # `#` inside that entity, and urlsplit reads everything after a
        # `#` as the fragment - so the path came out as
        # `/series/the-villainess&`, no chapter matched, and Azora's
        # whole catalogue looked chapterless. Any slug holding an
        # apostrophe or an ampersand failed the same way.
        href = html.unescape(match.group(1).strip())
        label = _clean(match.group(2))
        absolute = urllib.parse.urljoin(series_url, href)
        if not absolute.startswith("http"):
            continue
        path = urllib.parse.urlsplit(absolute).path.rstrip("/")
        looks_like_chapter = (
            re.search(r"chapter[-/]?\d", path, re.I)
            # `/series/BATE/248` - the TeamX shape: the series slug then
            # a bare number.
            or (slug and re.search(rf"/{re.escape(slug)}/\d+(?:\.\d+)?$", path))
            # `/manga/one-piece/1190/` - Madara's own shape.
            or re.search(r"/\d+(?:\.\d+)?$", path) and slug and slug in path
        )
        if not looks_like_chapter or absolute in seen:
            continue
        seen.add(absolute)
        number = _number_from(path)
        if number is None:
            continue
        found.append({
            "id": absolute,
            "number": number,
            "title": label if label and not label.isdigit() else "",
            # These are Arabic scan sites - that is what the owner reads
            # them for - but the page never states a language, so this is
            # marked as the site's own language rather than claimed as
            # verified Arabic.
            "lang": "ar",
            "source": urllib.parse.urlsplit(series_url).netloc,
            "url": absolute,
            "group": "",
            "published": "",
            "direction": "rtl" if entry_type in _RTL_TYPES else "ltr",
        })
    found.sort(key=_sort_key, reverse=True)
    return found


# `<a class="page-link" href=".../series/BATE?page=3">3</a>` - a paged
# chapter list. Matched on the query parameter rather than on the theme's
# class names, because the number is the only part every one of these
# Laravel-themed sites agrees on.
_PAGE_LINK_RE = re.compile(r'href="([^"]*[?&]page=(\d+)[^"]*)"', re.I)


def _page_urls(body: str, series_url: str) -> list:
    """Every further page of a paginated chapter list, page 2 upward.

    **A series page is no longer the whole list.** olympustaff used to
    print all 248 chapters of The Beginning After the End in one page;
    measured again on 19 August 2026 it prints **40**, plus chapter 1,
    and hangs the remaining 200 off `?page=2..7`. The reader was
    therefore showing only the newest forty - which from inside a
    chapter looks like a list holding nothing but what was just read.

    Only pages of *this* URL count: these themes also paginate comments
    and "latest releases" strips with the same parameter, and following
    those adds requests that can only return other series' chapters."""
    base = series_url.split("?", 1)[0].rstrip("/")
    numbers = set()
    for href, number in _PAGE_LINK_RE.findall(body or ""):
        # Unescaped for the same reason the chapter links are - a `#`
        # inside an entity turns the rest of the path into a fragment.
        absolute = urllib.parse.urljoin(series_url, html.unescape(href))
        if absolute.split("?", 1)[0].rstrip("/") != base:
            continue
        try:
            value = int(number)
        except ValueError:
            continue
        if 2 <= value <= MAX_LIST_PAGES:
            numbers.add(value)
    if not numbers:
        return []
    # Filled in rather than taken as-is: a paginator that prints
    # "1 2 3 ... 12" lists neither 4 nor 11, and asking only for the
    # numbers it printed leaves holes in the middle of the list.
    return [f"{base}?page={n}" for n in range(2, max(numbers) + 1)]


def _more_pages(body: str, series_url: str, entry_type, deadline) -> list:
    """The chapters on every page after the first, fetched together.

    Concurrent, and bounded at LIST_PAGE_WORKERS - one thread per page
    would be the same unbounded-thread mistake lookup_pool exists to
    stop. Each page is an ordinary request against the shared deadline,
    and a page that fails is simply skipped: a partial list is still
    better than the forty chapters this replaces."""
    urls = _page_urls(body, series_url)
    if not urls:
        return []
    timeout = net.step_timeout(deadline, DEFAULT_TIMEOUT)
    if timeout is None:
        return []

    def fetch(url):
        # Retried once, and that is not belt-and-braces: measured on
        # olympustaff, one of six concurrent pages failed on the first
        # run and the list came back missing exactly the forty chapters
        # that page held - a hole in the middle of the numbering, which
        # is worse than a short list because nothing says it is there.
        for attempt in range(2):
            step = net.step_timeout(deadline, timeout)
            if step is None:
                return ""
            try:
                return _get(url, step, referer=series_url)
            except Exception:
                continue
        return ""

    bodies = []
    workers = min(LIST_PAGE_WORKERS, len(urls))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        bodies = list(pool.map(fetch, urls))
    chapters = []
    for page_body in bodies:
        if page_body:
            chapters.extend(_chapters_from_html(page_body, series_url, entry_type))
    return chapters


def _merged(*lists) -> list:
    """Several chapter lists as one, deduped by id, newest first."""
    seen, merged = set(), []
    for chapters in lists:
        for chapter in chapters or []:
            key = chapter.get("id")
            if key in seen:
                continue
            seen.add(key)
            merged.append(chapter)
    merged.sort(key=_sort_key, reverse=True)
    return merged


# SWAT (meshmanga.com) and anything else on the same white-label
# platform: a REST API behind a single-page front end. Its series page
# contains no chapter anchors at all - the HTML scan above finds nothing
# and the site looks broken - so the API is the only way in. Measured:
# 65 chapters for series 1702615, and 16 images for chapter 1757864.
_V2_SERIES_RE = re.compile(r"^(https?://[^/]+/)series/(\d+)", re.I)


def _v2_api_chapters(series_url: str, entry_type, deadline) -> list:
    match = _V2_SERIES_RE.match(series_url or "")
    if not match:
        return []
    base, series_id = match.group(1), match.group(2)
    timeout = net.step_timeout(deadline, DEFAULT_TIMEOUT)
    if timeout is None:
        return []
    url = f"{base}v2/api/v2/series/{series_id}/chapters/?page_size=1000"
    try:
        body = json.loads(_get(url, timeout, referer=series_url, accept=_JSON_ACCEPT))
    except Exception:
        return []
    rows = body.get("results") if isinstance(body, dict) else body
    chapters = []
    for row in rows or []:
        chapter_id = row.get("id")
        if chapter_id is None:
            continue
        number = _number_from(str(row.get("chapter") or row.get("slug") or ""))
        if number is None:
            continue
        title = str(row.get("title") or "")
        chapters.append({
            "id": f"{base}v2/api/v2/chapters/{chapter_id}/",
            "number": number,
            # The API's `title` is usually just the number repeated;
            # showing "65" as the name of chapter 65 is noise.
            "title": "" if title.strip() == str(row.get("chapter") or "").strip() else title,
            "lang": "ar",
            "source": urllib.parse.urlsplit(series_url).netloc,
            "url": f"{base}chapter/{chapter_id}",
            "api_url": f"{base}v2/api/v2/chapters/{chapter_id}/",
            "group": "",
            "published": row.get("created_at") or "",
            "direction": "rtl" if entry_type in _RTL_TYPES else "ltr",
        })
    chapters.sort(key=_sort_key, reverse=True)
    return chapters


def _v2_api_pages(chapter, deadline) -> list:
    """Page images for a v2-API chapter, in `order`, not feed order."""
    api_url = chapter.get("api_url") or chapter.get("id") or ""
    if "/v2/api/v2/chapters/" not in api_url:
        return []
    timeout = net.step_timeout(deadline, DEFAULT_TIMEOUT)
    if timeout is None:
        return []
    try:
        body = json.loads(_get(api_url, timeout, referer=chapter.get("url"),
                              accept=_JSON_ACCEPT))
    except Exception:
        return []
    images = (body or {}).get("images") or []
    ordered = sorted(images, key=lambda i: i.get("order") or 0)
    return [i.get("image") for i in ordered if i.get("image")]


def _site_chapters(entry, deadline, on_partial=None) -> list:
    series_url = (entry or {}).get("url") or ""
    if not series_url.startswith("http"):
        return []
    entry_type = (entry or {}).get("type")

    # Tried before the HTML scan, not after: this platform's series page
    # is an empty shell, so scanning it first spends a request to learn
    # nothing.
    from_api = _v2_api_chapters(series_url, entry_type, deadline)
    if from_api:
        return from_api
    # A series page is the biggest fetch here - lavascans' is ~2MB of
    # HTML holding 600 anchors - and cutting it off at the ordinary
    # per-request timeout loses the whole chapter list rather than part
    # of it. Measured: that page needs more than DEFAULT_TIMEOUT.
    timeout = net.step_timeout(deadline, SERIES_PAGE_TIMEOUT)
    if timeout is None:
        return []
    try:
        body = _get(series_url, timeout)
    except Exception:
        body = ""
    chapters = _chapters_from_html(body, series_url, entry_type) if body else []
    if chapters:
        # **The first page is handed over before the rest are fetched.**
        # A full list is six more page fetches at a time up to
        # MAX_LIST_PAGES, and on olympustaff that measured 21.7s for 249
        # chapters - during which the reader showed nothing at all. The
        # first page is already the newest forty, which is what someone
        # opening a series is nearly always reaching for, so it goes to
        # the caller now and the rest arrives underneath it.
        # **Few is as wrong as none here too.** Madara serves a couple of
        # chapter links in the page body and the rest only from its ajax
        # endpoint, so this "first page" is *one chapter* on 3asq -
        # measured on the owner's own Kingdom (WAN), which has 380. Shown
        # early, that reads as a series with one chapter rather than as a
        # list still loading, which is worse than showing nothing.
        if on_partial is not None and len(chapters) >= MIN_PARTIAL_CHAPTERS:
            try:
                on_partial(list(chapters))
            except Exception:
                pass        # a caller that cannot draw must not stop the fetch
        # The rest of a paginated list - see _page_urls. Only asked for
        # once the first page actually parsed as chapters, so a dead or
        # unrecognised page costs no extra requests.
        chapters = _merged(chapters, _more_pages(body, series_url, entry_type,
                                                 deadline))
    # **Few is as wrong as none.** Madara serves a couple of chapter
    # links in the page itself and the rest only from its ajax endpoint,
    # so returning early on a non-empty list gave 3asq a one-chapter
    # list for a series with 560 - which reads as the series being
    # nearly empty rather than as a failure. Ask ajax whenever the page
    # looks thin, and keep whichever answer is bigger.
    if len(chapters) >= MIN_TRUSTED_CHAPTERS:
        return chapters
    timeout = net.step_timeout(deadline, DEFAULT_TIMEOUT)
    if timeout is None:
        return chapters
    try:
        from_ajax = _chapters_from_html(_madara_ajax(series_url, timeout),
                                        series_url, entry_type)
    except Exception:
        from_ajax = []
    return from_ajax if len(from_ajax) > len(chapters) else chapters


# --------------------------------------------------------- mangadex path

def _mangadex_chapters(entry, deadline) -> list:
    """Arabic chapters from MangaDex, matched by title.

    Reuses mangadex.py's own matching rather than repeating it - that
    module already refuses a near-miss on purpose (0.85), because
    inheriting a different series' chapters is the worst failure here."""
    title = (entry or {}).get("title") or ""
    if not title:
        return []
    timeout = net.step_timeout(deadline, DEFAULT_TIMEOUT)
    if timeout is None:
        return []
    try:
        manga_id = _mangadex_id(title, timeout)
    except Exception:
        return []
    if not manga_id:
        return []
    chapters, offset = [], 0
    while offset < 500:
        timeout = net.step_timeout(deadline, DEFAULT_TIMEOUT)
        if timeout is None:
            break
        query = urllib.parse.urlencode(
            [("limit", 100), ("offset", offset), ("translatedLanguage[]", "ar"),
             ("order[chapter]", "desc"), ("includeExternalUrl", "0")])
        try:
            body = json.loads(_get(f"{mangadex.BASE_URL}/manga/{manga_id}/feed?{query}",
                                   timeout))
        except Exception:
            break
        rows = (body or {}).get("data") or []
        for row in rows:
            attributes = row.get("attributes") or {}
            # An external chapter lives on someone else's site and has no
            # pages to serve; listing it produces a chapter that opens to
            # nothing.
            if attributes.get("externalUrl"):
                continue
            number = _number_from(attributes.get("chapter") or "")
            if number is None:
                continue
            chapters.append({
                "id": row.get("id"),
                "number": number,
                "title": attributes.get("title") or "",
                "lang": "ar",
                "source": "MangaDex",
                "url": f"https://mangadex.org/chapter/{row.get('id')}",
                "group": "",
                "published": attributes.get("publishAt") or "",
                "direction": "rtl" if (entry or {}).get("type") in _RTL_TYPES else "ltr",
            })
        if len(rows) < 100:
            break
        offset += 100
    # Several groups upload the same number; keep one per number.
    best = {}
    for chapter in chapters:
        best.setdefault(chapter["number"], chapter)
    return sorted(best.values(), key=_sort_key, reverse=True)


def _mangadex_id(title: str, timeout: float):
    """MangaDex's id for a title, or None.

    Two things measured 21 August 2026, both of which had to change for
    this to answer at all:

      * **Follower order, not MangaDex's default relevance.** Asked for
        "Kingdom" with the default ordering, the top five are five
        unrelated isekai and *Kingdom* is not among them - so this
        returned None for the clean title just as it did for the tagged
        one. Ordered by followedCount the real series is second. Same
        finding, same fix, as discover.discover_reading.
      * **The site's group tag has to come off on the retry.** "Kingdom
        (WAN)" is the owner's own entry title; see
        title_match.search_variants.

    Each variant is tried on its own: a failed request on the first must
    not cost the second, since the caller's `except` would swallow both."""
    for query in title_match.search_variants(title):
        url = f"{mangadex.BASE_URL}/manga?" + urllib.parse.urlencode(
            [("title", query), ("limit", 5), ("order[followedCount]", "desc")])
        try:
            body = json.loads(_get(url, timeout))
        except Exception:
            continue
        for row in (body or {}).get("data") or []:
            titles = (row.get("attributes") or {}).get("title") or {}
            alternatives = [v for v in titles.values() if isinstance(v, str)]
            # Scored against the full title the user has - normalize()
            # drops the tag on both sides, so nothing is lost by it.
            if title_match.best_similarity(title, alternatives) >= 0.85:
                return row.get("id")
    return None


# ------------------------------------------------------------- public

CACHE_FILE = "reader_cache.json"
CACHE_TTL_S = 6 * 3600

# The parsed cache file, held in memory for the life of the process.
#
# It is the only thing that writes this file, so a copy in hand cannot
# go stale under it - and reading it back is not cheap: measured on the
# owner's machine, 22 August 2026, the file is **1.88 MB** of JSON (40
# chapter lists, one of them 380 chapters) and parsing it cost 34-47ms
# *per call*, on the path between opening a title and its chapter list
# appearing. The budget for that whole move is 200ms (the owner's ask).
_store_cache = None


def _load_store() -> dict:
    global _store_cache
    if _store_cache is None:
        store = storage.load(CACHE_FILE, {})
        _store_cache = store if isinstance(store, dict) else {}
    return _store_cache


def warm_cache_async():
    """Parse the cache file off the UI thread, ahead of the first reader.

    The first cached_chapters() of a session paid the 1.88 MB parse
    (37-42ms measured, 22 August 2026) on the UI thread, inside the
    open-to-first-rows budget. main preloads windows.reader ~400ms after
    the window is up, and that import calls this - so by the time a
    title is clicked the store is already in memory and a cache hit is
    the dictionary lookup it claims to be.

    A single one-off daemon thread, not lookup_pool: chapter_source has
    no Qt in it and nothing waits on this. Racing a UI-thread
    _load_store() is benign - both parse the same file and publish the
    same answer, the pattern reader._default_browser_path already
    documents. Never raises."""
    if _store_cache is not None:
        return
    import threading

    def parse():
        try:
            _load_store()
        except Exception:
            pass        # the first real caller will just pay the parse

    threading.Thread(target=parse, name="reader-cache-warm",
                     daemon=True).start()


def _cache_key(entry) -> str:
    return str((entry or {}).get("id") or (entry or {}).get("url") or "")


def _cached(key):
    """A chapter list from the last few hours.

    A revisit must not re-fetch: opening the reader on a series whose
    list was just read should be instant, and these sites are slow."""
    import time
    store = _load_store()
    row = store.get(key)
    if not row or time.time() - (row.get("at") or 0) > CACHE_TTL_S:
        return None
    return row.get("chapters")


def _store(key, chapters):
    import time
    store = _load_store()
    store[key] = {"at": time.time(), "chapters": chapters}
    # Bounded: this is a convenience cache, not a library.
    if len(store) > 40:
        for stale in sorted(store, key=lambda k: store[k].get("at", 0))[:len(store) - 40]:
            store.pop(stale, None)
    storage.save(CACHE_FILE, store)


def cached_chapters(entry):
    """This entry's chapter list if a fresh one is already in hand, or
    None. Touches no network and no site.

    Split out of list_chapters so the *caller* can tell the two cases
    apart: a cache hit is a dictionary lookup and belongs on the UI
    thread, where it lands before the reader's first frame and the list
    appears in that frame rather than in a second one. Measured on the
    owner's own 380-chapter entry, 22 August 2026: going through a
    worker for a hit cost 50-84ms of signal latency on a 200ms budget,
    because the thread that had to deliver the answer was busy showing
    the reader."""
    key = _cache_key(entry)
    if not key:
        return None
    return _cached(key)


def list_chapters(entry, *, deadline=None, refresh=False,
                  on_partial=None) -> list:
    """Chapters for this entry, newest first. Never raises.

    `on_partial(chapters)` is called with the first page as soon as it
    parses, before the rest of a paginated list is fetched - see
    _site_chapters. A cached answer is complete already and reports
    nothing partial."""
    if deadline is None:
        deadline = net.deadline_in(45)
    key = _cache_key(entry)
    if key and not refresh:
        cached = _cached(key)
        if cached:
            return cached
    chapters = []
    try:
        chapters = _site_chapters(entry, deadline, on_partial)
    except Exception:
        chapters = []
    if not chapters:
        try:
            chapters = _other_site_chapters(entry, deadline)
        except Exception:
            chapters = []
    if not chapters:
        try:
            chapters = _mangadex_chapters(entry, deadline)
        except Exception:
            chapters = []
    if chapters and key:
        _store(key, chapters)
    return chapters


def _other_site_chapters(entry, deadline) -> list:
    """The same title on a *different* configured site.

    The middle rung between "this title's own site" and MangaDex, and it
    exists because a site can be perfectly good at listing series and
    unable to serve one. Measured on Mangalek: its catalogue browses and
    searches fine, while every series page and its chapter ajax answer
    403 to any client that is not a browser (six header shapes tried,
    including full Chrome + Referer + Sec-Fetch). Without this, a title
    discovered there opens an empty chapter list; with it, the same
    title is read off whichever configured site does answer.

    Title-matched at 0.85 - the same bar the schedule lookups use -
    because reading someone else's series is a worse failure here than
    showing nothing. The *query* is retried with the source site's group
    tag stripped, which is a different thing: measured 21 August 2026,
    searching the five other sites for the owner's own "Kingdom (WAN)"
    found the title on none of them, because "(WAN)" is 3asq's
    scanlation group and no other site's catalogue has heard of it. The
    0.85 scoring below is unaffected either way - normalize() already
    drops the tag on both sides."""
    title = (entry or {}).get("title") or ""
    if not title.strip():
        return []
    own_site = (entry or {}).get("site_id")
    others = [s for s in manga_sites.list_sites() if s.get("id") != own_site]
    if not others:
        return []

    def ask(site):
        """One site's best match for this title, or None. Never raises -
        it runs on a pool worker."""
        # One budget per site, covering both title variants together.
        # Without it a site answering neither spends four engines twice
        # over out of the reader's single deadline, and the MangaDex rung
        # below never gets asked at all: measured 21 August 2026, the
        # six-site sweep for "Kingdom (WAN)" cost 35.6s of a 45s budget.
        # 5s is generous against what a site that *does* answer costs
        # now that the working engine is tried first - every measured hit
        # was under 2s (3asq 0.5s, TeamX 1.1s, SWAT 0.3s, Lava 1.5s).
        site_deadline = min(deadline, net.deadline_in(OTHER_SITE_BUDGET))
        found = []
        for query in title_match.search_variants(title):
            try:
                found = manga_sites.search_site(site, query,
                                                deadline=site_deadline)
            except Exception:
                found = []
            if found:
                break
        # **Ties are broken by the shortest title, not by search order.**
        # normalize() drops bracketed tags on both sides, which is what
        # makes "Kingdom (WAN)" match "Kingdom" - and it makes a
        # *language edition* match just as perfectly. Measured 21 August
        # 2026: 3asq answers "One Piece" with One Piece (French), One
        # Piece and One Piece (English) among seven rows, the first
        # three all scoring 1.00, and taking the first 1.00 read the
        # French edition - 14 chapters for a series that has 508.
        #
        # The normalized length ranks first (base series over sequel,
        # the rule anilist.fetch_external_urls already uses) and the raw
        # length breaks what is left, because that is the only one of
        # the two that can see the bracket at all: "One Piece (French)"
        # and "One Piece" both normalize to nine characters.
        best, best_key = None, None
        for row in found or []:
            name = row.get("title") or ""
            key = (-title_match.similarity(title, name),
                   len(title_match.normalize(name)), len(name))
            if best_key is None or key < best_key:
                best, best_key = row, key
        if best is None or -best_key[0] < 0.85 or not best.get("url"):
            return None
        return (best_key, site, best)

    # **Every other site is asked at once.** This walked them one at a
    # time, and the walk is the whole cost: measured 21 August 2026 on
    # "Celebrity Lady" - a title Mangalek carries and refuses to serve
    # (see the 403 note above) - the sweep took **13.0s** on its own and
    # the whole of list_chapters 9.6s before answering "nothing". A
    # reader that sits blank for ten seconds and then says it found
    # nothing is the owner's "either takes a long time to show the ch
    # list, or never shows them".
    #
    # Searching is what parallelises; *reading* the winner does not, and
    # must not - the candidates are tried strongest-match-first and the
    # first that yields chapters wins, so trying them together would
    # spend requests on sites whose answer will be thrown away.
    candidates = []
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=len(others))
    try:
        jobs = [pool.submit(ask, site) for site in others]
        try:
            for job in concurrent.futures.as_completed(
                    jobs, timeout=max(0.0, deadline - time.monotonic())):
                try:
                    row = job.result()
                except Exception:
                    row = None
                if row is not None:
                    candidates.append(row)
        except concurrent.futures.TimeoutError:
            pass        # the budget is the answer; use whatever arrived
    finally:
        # Stragglers are abandoned, never waited for - the same rule as
        # manga_sites.search_all. They are bounded by the deadline their
        # own requests were given.
        pool.shutdown(wait=False, cancel_futures=True)

    candidates.sort(key=lambda row: row[0])
    for _key, site, best in candidates:
        if net.step_timeout(deadline, DEFAULT_TIMEOUT) is None:
            break
        probe = dict(entry)
        probe["url"] = best["url"]
        probe["site_id"] = site.get("id")
        try:
            chapters = _site_chapters(probe, deadline)
        except Exception:
            chapters = []
        if chapters:
            return chapters
    return []


# Images that are furniture, not pages.
#
# **Every token here is anchored to a path-segment boundary, and that is
# not tidiness.** The first version matched anywhere in the URL and
# included `ads?[-_/]` - which matches the "ads/" inside "**uploads/**".
# Every page image on every one of these sites is served from an
# /uploads/ path, so the filter rejected all 87 images on a 3asq chapter
# and all 12 on a TeamX one, and the reader showed an empty chapter with
# nothing saying why. Measured on both before and after.
_NOT_A_PAGE = re.compile(
    r"(?:^|[/_.-])"
    r"(?:logos?|avatars?|icons?|favicons?|banners?|ads?|placeholder|"
    r"loading|thumb(?:nail)?s?|discord|telegram|whatsapp|facebook|"
    r"twitter|patreon)"
    r"(?:[/_.-]|$)", re.I)
_IMAGE_URL_RE = re.compile(r"\.(?:jpe?g|png|webp|gif)(?:\?[^\s\"']*)?$", re.I)
_IMG_TAG_RE = re.compile(r"<img\b[^>]*>", re.I | re.S)
# Attributes a page image can hide behind. `src` last on purpose: on a
# lazy-loading theme it holds a 1px placeholder and the real URL is in
# one of the data- attributes, which is the single most common reason a
# reader shows a chapter of blank pages.
_SRC_ATTRS = ("data-src", "data-lazy-src", "data-original", "data-url",
              "data-image", "srcset", "src")


def _image_urls(body: str, page_url: str) -> list:
    """The page images of a chapter, in reading order.

    Two stages, because a name-based filter alone cannot tell a chapter
    page from a site logo and guessing at names is what broke this once
    already (see _NOT_A_PAGE):

      1. collect every image the markup points at, dropping the obvious
         furniture,
      2. **keep only the largest group sharing a directory.**

    Stage 2 is what actually works, and it needs no per-host knowledge:
    a chapter's pages are uploaded together, so they share one folder
    (`/uploads/manga_a5bfc/248/` on TeamX, one WP-manga data folder on
    3asq), while the logo, the advert and the reaction image are
    singletons scattered across other paths. Measured: 12 of 15 and 16
    of 87 respectively, with the strays dropped."""
    found, seen = [], set()
    for tag in _IMG_TAG_RE.findall(body):
        for attribute in _SRC_ATTRS:
            match = re.search(rf'{attribute}\s*=\s*["\']([^"\']+)["\']', tag, re.I)
            if not match:
                continue
            value = match.group(1).strip()
            if attribute == "srcset":
                value = value.split(",")[0].strip().split(" ")[0]
            url = urllib.parse.urljoin(page_url, value)
            if (not url.startswith("http") or url in seen
                    or _NOT_A_PAGE.search(url) or not _IMAGE_URL_RE.search(url)):
                continue
            seen.add(url)
            found.append(url)
            break
    if len(found) < 2:
        return found
    groups = {}
    for url in found:
        folder = url.rsplit("/", 1)[0]
        groups.setdefault(folder, []).append(url)
    largest = max(groups.values(), key=len)
    # A chapter is more than one image. If no folder holds two, this page
    # is not laid out the way any of them are and everything found is a
    # better answer than nothing.
    return largest if len(largest) > 1 else found


def chapter_pages(chapter, deadline=None) -> dict:
    """The page images of one chapter, plus the headers they need.

    `headers` is not decoration: these hosts 403 an image without the
    chapter page as Referer, which is why a link that works in a browser
    shows nothing in an app that fetched it bare."""
    if deadline is None:
        deadline = net.deadline_in(25)
    chapter = chapter or {}
    direction = chapter.get("direction") or "ltr"

    if chapter.get("source") == "MangaDex":
        pages = _mangadex_pages(chapter, deadline)
        return {"pages": pages, "headers": {"User-Agent": _UA},
                "direction": direction}

    if chapter.get("api_url"):
        pages = _v2_api_pages(chapter, deadline)
        return {"pages": pages, "headers": _headers(chapter.get("url")),
                "direction": direction}

    url = chapter.get("url") or ""
    timeout = net.step_timeout(deadline, DEFAULT_TIMEOUT)
    if not url.startswith("http") or timeout is None:
        return {"pages": [], "headers": {}, "direction": direction}
    try:
        body = _get(url, timeout)
    except Exception:
        return {"pages": [], "headers": {}, "direction": direction}
    return {"pages": _image_urls(body, url),
            "headers": _headers(url),
            "direction": direction}


def _mangadex_pages(chapter, deadline) -> list:
    timeout = net.step_timeout(deadline, DEFAULT_TIMEOUT)
    if timeout is None:
        return []
    try:
        body = json.loads(_get(f"{mangadex.BASE_URL}/at-home/server/{chapter.get('id')}",
                               timeout))
    except Exception:
        return []
    base = (body or {}).get("baseUrl")
    data = (body or {}).get("chapter") or {}
    files = data.get("data") or data.get("dataSaver") or []
    quality = "data" if data.get("data") else "data-saver"
    if not base or not data.get("hash"):
        return []
    return [f"{base}/{quality}/{data['hash']}/{name}" for name in files]
