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

import json
import re
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
    with urllib.request.urlopen(request, timeout=timeout) as response:
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
        href, label = match.group(1).strip(), _clean(match.group(2))
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


def _site_chapters(entry, deadline) -> list:
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
    query = urllib.parse.urlencode({"title": title, "limit": 5})
    body = json.loads(_get(f"{mangadex.BASE_URL}/manga?{query}", timeout))
    for row in (body or {}).get("data") or []:
        titles = (row.get("attributes") or {}).get("title") or {}
        alternatives = [v for v in titles.values() if isinstance(v, str)]
        if title_match.best_similarity(title, alternatives) >= 0.85:
            return row.get("id")
    return None


# ------------------------------------------------------------- public

CACHE_FILE = "reader_cache.json"
CACHE_TTL_S = 6 * 3600


def _cache_key(entry) -> str:
    return str((entry or {}).get("id") or (entry or {}).get("url") or "")


def _cached(key):
    """A chapter list from the last few hours.

    A revisit must not re-fetch: opening the reader on a series whose
    list was just read should be instant, and these sites are slow."""
    import time
    store = storage.load(CACHE_FILE, {})
    row = store.get(key) if isinstance(store, dict) else None
    if not row or time.time() - (row.get("at") or 0) > CACHE_TTL_S:
        return None
    return row.get("chapters")


def _store(key, chapters):
    import time
    store = storage.load(CACHE_FILE, {})
    if not isinstance(store, dict):
        store = {}
    store[key] = {"at": time.time(), "chapters": chapters}
    # Bounded: this is a convenience cache, not a library.
    if len(store) > 40:
        for stale in sorted(store, key=lambda k: store[k].get("at", 0))[:len(store) - 40]:
            store.pop(stale, None)
    storage.save(CACHE_FILE, store)


def list_chapters(entry, *, deadline=None, refresh=False) -> list:
    """Chapters for this entry, newest first. Never raises."""
    if deadline is None:
        deadline = net.deadline_in(25)
    key = _cache_key(entry)
    if key and not refresh:
        cached = _cached(key)
        if cached:
            return cached
    chapters = []
    try:
        chapters = _site_chapters(entry, deadline)
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
