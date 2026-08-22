"""Manga reading-site directory (Settings-managed) + live search.

Each site is just a name + base URL - no per-site scraping config to fill
in, because `search_site` tries a handful of known search-endpoint shapes
against whatever base URL is configured (a couple of WordPress manga-theme
AJAX endpoints, plus the REST/HTML search endpoints found by inspecting
the SWAT and TeamX sites directly). Most scanlation-aggregator sites match
one of these, so adding a new site to Settings "just works" for live
suggestions with zero setup; a site that matches none of them simply gets
no live suggestions and falls back to opening a plain on-site search page
instead. Every function fails soft (empty list) so a flaky/unrecognized
site never blocks the others or crashes the UI.
"""

import concurrent.futures
import html
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from . import anilist, mangadex, net, storage, title_match

SITES_FILE = "manga_sites.json"

# Measured live 21 August 2026, browse / search / chapters each:
#   3asq        30 rows  /  works  /  works (madara ajax)
#   TeamX       30 rows  /  works  /  works
#   Lava Scans  30 rows  /  works  /  works
#   SWAT        30 rows  /  works  /  works (v2 REST api)
#   Mangalek    30 rows  /  works  /  **403** - see below
#   Azora       30 rows  /  no     /  works (19 chapters, 0.6s)
#
# Mangalek is kept despite the 403 because browsing and searching it
# both work, and a title found there is read off whichever other site
# does answer (chapter_source._other_site_chapters). Its series pages
# and its chapter ajax refuse every non-browser client - six header
# shapes were tried, up to a full Chrome set with Referer and Sec-Fetch.
DEFAULT_SITES = [
    {"name": "3asq", "base_url": "https://3asq.online/"},
    {"name": "TeamX", "base_url": "https://www.olympustaff.com/"},
    {"name": "Lava Scans", "base_url": "https://lavascans.com/"},
    {"name": "SWAT", "base_url": "https://meshmanga.com/"},
    {"name": "Mangalek", "base_url": "https://mangalik.net/"},
    {"name": "Azora", "base_url": "https://azorafly.com/"},
]


def _normalize(base_url: str) -> str:
    base_url = (base_url or "").strip()
    if base_url and not base_url.endswith("/"):
        base_url += "/"
    return base_url


# Defaults added after the first release that shipped this file. Adding
# a name here adds that site to installs that already have a
# manga_sites.json - once, recorded in ADDED_FILE, so a site the user
# then deletes stays deleted rather than coming back every launch.
LATER_DEFAULTS = ("Mangalek", "Azora")
ADDED_FILE = "manga_sites_added.json"


def _add_later_defaults(sites) -> list:
    """Put newly-shipped default sites into an existing install.

    Keyed on the base URL rather than the name: the user may well have
    renamed one, and adding a second copy of a site they already have is
    worse than not adding it at all."""
    try:
        already = set(storage.load(ADDED_FILE, []) or [])
    except Exception:
        already = set()
    have = {_normalize(s.get("base_url")) for s in sites}
    added = False
    for default in DEFAULT_SITES:
        if default["name"] not in LATER_DEFAULTS:
            continue
        if default["name"] in already:
            continue
        already.add(default["name"])
        added = True
        if _normalize(default["base_url"]) in have:
            continue
        sites.append({"id": str(uuid.uuid4()), "name": default["name"],
                      "base_url": default["base_url"]})
    if added:
        try:
            storage.save(SITES_FILE, sites)
            storage.save(ADDED_FILE, sorted(already))
        except Exception:
            pass
    return sites


def _load():
    sites = storage.load(SITES_FILE, None)
    if sites is None:
        # First run: seed the well-known reading sites so Manga suggestions
        # work out of the box. Once the file exists, an empty list (the
        # user removed every site) is respected as-is - never re-seeded.
        sites = [{"id": str(uuid.uuid4()), "name": s["name"], "base_url": s["base_url"]}
                  for s in DEFAULT_SITES]
        storage.save(SITES_FILE, sites)
        try:
            storage.save(ADDED_FILE, sorted(LATER_DEFAULTS))
        except Exception:
            pass
        return sites
    return _add_later_defaults(sites)


def list_sites() -> list:
    return _load()


def get_site(site_id: str):
    return next((s for s in _load() if s["id"] == site_id), None)


def add_site(name: str, base_url: str) -> dict:
    sites = _load()
    site = {"id": str(uuid.uuid4()), "name": name.strip(), "base_url": _normalize(base_url)}
    sites.append(site)
    storage.save(SITES_FILE, sites)
    return site


def update_site(site_id: str, name: str, base_url: str):
    sites = _load()
    for s in sites:
        if s["id"] == site_id:
            s["name"] = name.strip()
            s["base_url"] = _normalize(base_url)
    storage.save(SITES_FILE, sites)


def record_resolution(site_id: str, resolves: str):
    """Remember what probe_site found, on the site itself. update_entry
    rather than writing the whole list back - see .claude/rules/ui.md."""
    storage.update_entry(SITES_FILE, site_id, {"resolves": resolves})


def remove_site(site_id: str):
    sites = [s for s in _load() if s["id"] != site_id]
    storage.save(SITES_FILE, sites)


def search_page_url(base_url: str, query: str) -> str:
    """Plain on-site search results page - the fallback used when a site
    doesn't match either known live-search shape, or when a manga entry
    is linked to a site but has no specific page URL saved yet."""
    return f"{base_url.rstrip('/')}/?s={urllib.parse.quote(query)}"


_OG_IMAGE_RE = re.compile(
    r'<meta[^>]+(?:property|name)=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']'
    r'|<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\']og:image["\']',
    re.IGNORECASE,
)

# The Madara theme's own cover art, in its portrait aspect ratio - preferred
# over og:image, which WordPress crops to a wide 1200x630 social-share
# shape (fitting that into the app's portrait poster boxes zooms in on the
# middle of the image instead of showing the actual cover).
# The {0,2000} rather than a bare `.*?` is the guard anime_sites already
# carries (see _A4U_TITLE_LINK_RE): a lazy `.*?` hunting for an `<img>`
# that isn't there re-scans to the end of the page one character at a
# time, per `summary_image` on it - and the regex engine holds the GIL
# while it does, so the whole app freezes, not just this lookup. Measured
# at 32.5s on a 0.7MB page in the anime_sites case; Windows offers to
# kill a window unresponsive for ~30s. The wrapper markup between the two
# is a few hundred characters on every real Madara page.
_MADARA_COVER_RE = re.compile(
    r'class=["\']summary_image["\'][^>]*>.{0,2000}?<img[^>]+src=["\']([^"\']+)["\']',
    re.IGNORECASE | re.DOTALL,
)

_SWAT_SERIES_URL_RE = re.compile(r'^(https?://[^/]+/)series/(\d+)/?$')

# WordPress auto-generates a whole family of cropped sizes for every
# upload ("cover-800x1200.jpg", "cover-231x300.jpg", ...) alongside the
# original - sites that link search-result covers to one of those small
# crops still serve the un-suffixed original at the same path, so
# stripping this and re-requesting it gets a much sharper image (verified
# against Lava Scans/TeamX: a "-210x300" or "-231x300" hit is routinely
# 3-10x the pixel dimensions once stripped).
_WP_SIZE_SUFFIX_RE = re.compile(r'-\d{2,4}x\d{2,4}(?=\.[a-zA-Z0-9]+(?:[?#].*)?$)')


def _strip_wp_size_suffix(url):
    return _WP_SIZE_SUFFIX_RE.sub("", url, count=1) if url else url


def fetch_manga_details(page_url: str, timeout: int = 6, title: str = None):
    """Best-effort cover + latest-chapter number for a specific manga
    page - used when the engine that matched it didn't already provide
    them (Madara's search endpoint gives neither; SWAT's search endpoint
    gives neither but its per-series detail endpoint does). Called
    lazily, only for the one result the user actually picks, not every
    search hit. Returns {"cover_url": ..., "latest_chapter": ...}, either
    possibly None.

    When the page itself yields no cover, the site's own Madara endpoint
    is asked for the series card (_madara_card_details), and only then an
    external catalogue (_external_cover) - the site's own art for this
    exact slug before anyone else's art for a matching title. `title` is
    optional because the tracker calls this with a page URL and nothing
    else; the URL slug stands in for it."""
    swat_match = _SWAT_SERIES_URL_RE.match(page_url)
    if swat_match:
        details = _swat_series_details(swat_match.group(1), swat_match.group(2),
                                       timeout)
    else:
        details = _scrape_manga_page(page_url, timeout)
    if not details.get("cover_url"):
        # The site's own art, off the endpoint that still answers when
        # its pages don't - see _madara_card_details. Asked before any
        # catalogue, because it is this site's own cover for this exact
        # series rather than a title match against somebody else's.
        card = _madara_card_details(page_url, timeout)
        if card.get("cover_url"):
            details["cover_url"] = card["cover_url"]
            details["latest_chapter"] = (details.get("latest_chapter")
                                          or card.get("latest_chapter"))
    if not details.get("cover_url"):
        details["cover_url"] = _external_cover(
            title or _title_from_slug(page_url), timeout)
    return details


# A series page path across the shapes these sites use - the slug is what
# the Madara endpoint below is asked for.
_SERIES_SLUG_RE = re.compile(
    r"^/(?:manga|series|comic|comics|webtoon|title)/([^/?#]+)/?$", re.I)

# Series page URL -> what its card gave, for the session. A throttle
# again rather than a speed-up: Discover rebuilds its whole grid on every
# visit, so without this a second look at the same wall re-asks the site
# for thirty cards it has already answered - and this host measurably
# slows down under repeat load (a run of five straight after twelve
# earlier calls took 47.7s where the first took 8.1s).
#
# **Hits only.** _external_covers remembers its misses because a
# catalogue that does not carry a title will not carry it a minute later;
# a miss here is usually one slow response, and remembering it would
# freeze a blank tile in place for the whole session.
_card_details = {}
_CARD_CACHE_MAX = 300


def _madara_card_details(page_url: str, timeout: int) -> dict:
    """Cover (and latest chapter, if the card prints one) for a series
    whose own page yielded nothing, from Madara's `madara_load_more`
    endpoint.

    **Measured 21 August 2026 on the owner's five blank Discover tiles -
    every one of them a Mangalek row.** mangalik.net 403s every page a
    non-browser client asks for: the series page, /manga/, /?s=, the feed
    and the sitemap alike, only the home page answering at all. Its
    admin-ajax.php does not - the search action already used here answers
    200 (which is how those rows reached Discover carrying a title, a URL
    and no cover), and this action answers 200 *with the card's cover
    image in it*. The site has the art; only its HTML pages refuse to
    serve it.

    Asked by post slug (`vars[name]`), never by title, and the card is
    read only if its own link points back at that same slug - an exact
    identity, so unlike the catalogue fallback there is no title match
    involved and no wrong cover to inherit. Fails soft into no cover on
    any site that is not Madara-shaped, which costs one POST on a path
    that had already produced nothing."""
    parts = urllib.parse.urlsplit(page_url or "")
    slug_match = _SERIES_SLUG_RE.match(urllib.parse.unquote(parts.path))
    empty = {"cover_url": None, "latest_chapter": None}
    if not slug_match or not parts.netloc:
        return empty
    if page_url in _card_details:
        return _card_details[page_url]
    slug = slug_match.group(1)
    # A purely numeric slug is an API site's series id (SWAT's
    # /series/1607561), never a WordPress post name - the same reasoning
    # as _title_from_slug. Asking would spend a request to be told no.
    if slug.isdigit():
        return empty
    # Retried once, for the same reason the MangaDex client and the
    # chapter-list pages are: **measured 2 failures in 12 back-to-back
    # calls**, both "response body over the time budget" at ~7s, and both
    # answering in under 3s on the very next attempt. A cover that exists
    # and is skipped over a slow second is the failure worth spending one
    # extra request on.
    body = None
    for attempt in range(2):
        try:
            body = _post_text(f"{parts.scheme}://{parts.netloc}/wp-admin/admin-ajax.php", {
                "action": "madara_load_more",
                "page": 0,
                "template": "madara-core/content/content-archive",
                "vars[post_type]": "wp-manga",
                "vars[name]": slug,
                "vars[posts_per_page]": 1,
                "vars[paged]": 1,
            }, timeout)
            break
        except Exception:
            if attempt:
                return empty
    if not body:
        return empty

    cover_url = None
    for match in _ANCHOR_RE.finditer(body):
        href = html.unescape(match.group(1))
        link = _SERIES_SLUG_RE.match(
            urllib.parse.unquote(urllib.parse.urlsplit(href).path))
        # Identity, not similarity: a different slug means the endpoint
        # answered with a listing rather than this series, and its cover
        # belongs to some other title.
        if not link or link.group(1).lower() != slug.lower():
            continue
        image = _IMG_SRC_RE.search(match.group(2))
        if image:
            cover_url = _strip_wp_size_suffix(
                urllib.parse.urljoin(page_url, html.unescape(image.group(1))))
            break

    # Same heuristic as _scrape_manga_page: the card lists its newest
    # chapters as links under the series' own path.
    prefix = page_url.rstrip("/") + "/"
    numbers = [float(n) for n in
               re.findall(re.escape(prefix) + r"([0-9]+(?:\.[0-9]+)?)/", body)]
    found = {"cover_url": cover_url,
             "latest_chapter": max(numbers) if numbers else None}
    if cover_url:
        if len(_card_details) >= _CARD_CACHE_MAX:
            _card_details.clear()
        _card_details[page_url] = found
    return found


# Normalized title -> cover URL or None, for the session.
#
# **Not an optimization - a throttle.** A Madara search returns rows
# carrying no cover at all, so a reading search in Discover can ask this
# for twenty titles at once, and MangaDex spaces every request 0.35s
# apart behind one shared lock (mangadex._MIN_REQUEST_GAP). Without
# this, revisiting the same row re-asks both catalogues for an answer
# that has not changed and cannot. Bounded, because it is a convenience
# cache and not a library. **A miss is remembered too**: a title neither
# catalogue carries is the case that costs two full requests.
_external_covers = {}
_EXTERNAL_COVER_CACHE_MAX = 300


def _external_cover(title: str, timeout: int):
    """A cover for a title whose own site page yielded none.

    **The owner's ask, on "I Want to Stop Killing"** - measured 21
    August 2026, that title is carried by exactly one configured site
    (Mangalek), whose series pages 403 every non-browser client, so the
    scrape above returns nothing and the card drew a letter avatar.
    MangaDex matches the same title at 1.00 and carries cover art;
    AniList carries it too.

    MangaDex first: it is the closer catalogue for scanlated manga and
    its bar here is the strict 0.85. AniList (0.8, portrait cover rather
    than the wide banner) is the second ask. Both fail soft and a title
    matching neither keeps None, because a confidently wrong cover is
    worse than no cover at all.

    Two extra requests, and only ever for a page that produced no cover
    - a normal search result already carries one and never reaches
    here."""
    title = (title or "").strip()
    if not title:
        return None
    key = title_match.normalize(title)
    if key in _external_covers:
        return _external_covers[key]
    try:
        found = mangadex.fetch_cover_url(title, timeout)
    except Exception:
        found = None
    if not found:
        try:
            found = anilist.fetch_manga_cover(title, timeout)
        except Exception:
            found = None
    if key:
        if len(_external_covers) >= _EXTERNAL_COVER_CACHE_MAX:
            _external_covers.clear()
        _external_covers[key] = found
    return found


def cover_for_title(title: str, timeout: int = 6):
    """A cover for a title with no page to scrape at all, or None.

    `_external_cover` under a public name. `fetch_manga_details` is the
    usual way in, but a Discover row can carry neither a cover *nor* a
    series URL - some site search shapes return a title and nothing else
    - and those cards had no route to art of any kind. Same strict title
    matching, same session cache, same fail-soft: a title neither
    catalogue carries keeps None, because a confidently wrong cover is
    worse than a letter avatar."""
    return _external_cover(title, timeout)


def _swat_series_details(base_url: str, series_id: str, timeout: int):
    try:
        body = json.loads(_get(f"{base_url}v2/api/v2/series/{series_id}/", timeout))
    except Exception:
        return {"cover_url": None, "latest_chapter": None}
    poster = (body.get("poster") or {}) if isinstance(body, dict) else {}
    return {
        "cover_url": poster.get("medium") or poster.get("thumbnail"),
        "latest_chapter": (body.get("chapters_count") if isinstance(body, dict) else None) or None,
    }


# Hosts whose series pages answered 403 this session. Mangalek refuses
# every non-browser client (six header shapes were tried, up to a full
# Chrome set with Referer and Sec-Fetch), and a Discover grid is thirty
# rows of one site - measured, those refusals cost 0.6-24.5s each before
# the fallback below is even reached. Remembered per host rather than per
# URL for that reason, and only for the session, so a site that starts
# answering again is retried on the next launch.
_forbidden_hosts = set()


def _scrape_manga_page(page_url: str, timeout: int):
    empty = {"cover_url": None, "latest_chapter": None}
    host = urllib.parse.urlsplit(page_url or "").netloc
    if host and host in _forbidden_hosts:
        return empty
    try:
        body = _get(page_url, timeout)
    except urllib.error.HTTPError as error:
        if host and error.code in (401, 403):
            _forbidden_hosts.add(host)
        return empty
    except Exception:
        return empty

    madara_match = _MADARA_COVER_RE.search(body)
    if madara_match:
        cover_url = madara_match.group(1)
    else:
        og_match = _OG_IMAGE_RE.search(body)
        cover_url = (og_match.group(1) or og_match.group(2)) if og_match else None
    cover_url = _strip_wp_size_suffix(cover_url)

    # Heuristic: the highest chapter number linked from the manga's own
    # page is its latest chapter - works across Madara-style themes
    # without needing to know their exact chapter-list markup.
    prefix = page_url.rstrip("/") + "/"
    numbers = [float(n) for n in re.findall(re.escape(prefix) + r"([0-9]+(?:\.[0-9]+)?)/", body)]
    latest_chapter = max(numbers) if numbers else None

    return {"cover_url": cover_url, "latest_chapter": latest_chapter}


# ---- Search engines --------------------------------------------------
# Each takes (base_url, query, timeout) and returns a list of
# {title, url, cover_url}, or raises on network/parse failure.

def _post_text(url: str, fields: dict, timeout: int, accept: str = "*/*") -> str:
    data = urllib.parse.urlencode(fields).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": accept,
        "User-Agent": "Mozilla/5.0 PC-App/1.0",
    })
    deadline = net.deadline_in(timeout)
    with net.urlopen(req, timeout=timeout) as resp:
        return net.read_text(resp, deadline)


def _post_json(url: str, fields: dict, timeout: int):
    return json.loads(_post_text(url, fields, timeout, "application/json"))


def _get(url: str, timeout: int, extra_headers: dict = None) -> str:
    # net.ascii_url: these sites are Arabic, and a series URL carrying an
    # Arabic slug makes urllib raise UnicodeEncodeError before it
    # connects - which every fail-soft caller here reads as "the site
    # said nothing".
    req = urllib.request.Request(net.ascii_url(url), headers={
        "Accept": "application/json, text/html, */*",
        "User-Agent": "Mozilla/5.0 PC-App/1.0",
        **(extra_headers or {}),
    })
    deadline = net.deadline_in(timeout)
    with net.urlopen(req, timeout=timeout) as resp:
        return net.read_text(resp, deadline)


def _search_madara(base_url: str, query: str, timeout: int) -> list:
    """The WordPress 'Madara' manga theme's built-in live search. Titles/
    links only - no cover or chapter data, so those stay None here and
    get filled in lazily via fetch_manga_details if this result is picked."""
    body = _post_json(f"{base_url}wp-admin/admin-ajax.php",
                       {"action": "wp-manga-search-manga", "title": query}, timeout)
    if not isinstance(body, dict) or not body.get("success"):
        return []
    return [{"title": r["title"], "url": r["url"], "cover_url": None, "latest_chapter": None}
             for r in body.get("data") or [] if r.get("url") and r.get("title")]


def _search_ajaxy(base_url: str, query: str, timeout: int) -> list:
    """The 'Ajaxy Live Search' widget bundled with some other WP manga
    themes - a different plugin/theme from Madara, same general idea."""
    body = _post_json(f"{base_url}wp-admin/admin-ajax.php",
                       {"action": "ts_ac_do_search", "ts_ac_query": query}, timeout)
    if not isinstance(body, dict):
        return []
    results = []
    for group in body.get("series") or []:
        for r in group.get("all") or []:
            if r.get("post_link") and r.get("post_title"):
                latest = r.get("post_latest")
                try:
                    latest_chapter = float(latest) if latest else None
                except (TypeError, ValueError):
                    latest_chapter = None
                results.append({"title": r["post_title"], "url": r["post_link"],
                                 "cover_url": _strip_wp_size_suffix(r.get("post_image")),
                                 "latest_chapter": latest_chapter})
    return results


def _search_v2_api(base_url: str, query: str, timeout: int) -> list:
    """A REST API served off the site's own domain at /v2/api/v2/ - found
    on SWAT (meshmanga.com), likely a white-label manga platform shared by
    other sites too. The search listing doesn't include a chapter count
    (only the per-series detail endpoint does), so that's left for
    fetch_manga_details to fill in lazily if this result is picked."""
    url = f"{base_url}v2/api/v2/series/?search={urllib.parse.quote(query)}&page_size=8"
    body = json.loads(_get(url, timeout))
    if not isinstance(body, dict):
        return []
    results = []
    for r in body.get("results") or []:
        if not r.get("title") or r.get("id") is None:
            continue
        poster = r.get("poster") or {}
        results.append({"title": r["title"], "url": f"{base_url}series/{r['id']}",
                         "cover_url": poster.get("medium") or poster.get("thumbnail"),
                         "latest_chapter": None})
    return results


# Anchored with match(), never search()/finditer() - see _ajax_search_cards
# for why, and for the {0,2000} on the body.
# \Z, not $: $ also matches just before a trailing newline, which would
# silently drop the last character of a card body that ends in one.
_AJAX_SEARCH_CARD_RE = re.compile(r'<a\s+href="(?P<url>[^"]+)"[^>]*>(?P<body>.{0,2000}?)\Z',
                                  re.DOTALL)
_AJAX_SEARCH_IMG_RE = re.compile(r'<img[^>]*src="(?P<img>[^"]+)"[^>]*alt="(?P<title>[^"]+)"')
_AJAX_SEARCH_CHAPTER_RE = re.compile(r'([0-9]+(?:\.[0-9]+)?)\s*(?:فصل|chapters?)', re.IGNORECASE)

# TeamX's search cards link a tiny (~100x130) "thumbnail_<name>" crop -
# the plain "<name>" at the same path is the real cover, 5-10x the pixel
# dimensions (verified against several live results).
_TEAMX_THUMBNAIL_PREFIX_RE = re.compile(r'/thumbnail_(?=[^/]+$)')


def _ajax_search_cards(body: str):
    """Each <a href="...">...</a> result card in an HTML fragment.

    Split on the closing tag and match each piece from its last opening
    anchor, rather than letting one regex hunt for pairs across the whole
    document. A lazy `.*?` looking for a `</a>` that isn't there re-scans
    to the end once per anchor on the page, and Python's regex engine
    holds the GIL while it does - so a malformed page freezes the entire
    app, not just this lookup. Measured on a 1MB fragment with 20k
    unclosed anchors: 224ms even with the body capped, against 3ms this
    way; uncapped it is the 32.5s class of freeze anime_sites already had
    to fix. Windows offers to kill a window unresponsive for ~30s.

    split() and rfind() are linear and run in C. The cap on the body is
    then belt-and-braces: one malformed piece can no longer cost more
    than one bounded match.

    Innermost anchor rather than outermost, which is what changes if a
    card ever nests one <a> inside another: cards from these endpoints
    don't, and nested anchors aren't valid HTML anyway."""
    for piece in body.split("</a>")[:-1]:
        start = piece.rfind("<a ")
        if start == -1:
            continue
        match = _AJAX_SEARCH_CARD_RE.match(piece, start)
        if match:
            yield match


def _search_ajax_html(base_url: str, query: str, timeout: int) -> list:
    """A plain GET endpoint (found on TeamX/olympustaff.com) that returns
    an HTML fragment of result cards rather than JSON - scrape each
    card's anchor/title/cover/chapter-count with regexes instead of
    pulling in an HTML parser dependency for one site shape."""
    body = _get(f"{base_url}ajax/search?keyword={urllib.parse.quote(query)}", timeout,
                extra_headers={"X-Requested-With": "XMLHttpRequest"})
    results = []
    for card in _ajax_search_cards(body):
        img_match = _AJAX_SEARCH_IMG_RE.search(card.group("body"))
        if not img_match:
            continue
        chapter_match = _AJAX_SEARCH_CHAPTER_RE.search(card.group("body"))
        cover_url = _TEAMX_THUMBNAIL_PREFIX_RE.sub("/", img_match.group("img"), count=1)
        results.append({
            "title": html.unescape(img_match.group("title")),
            "url": card.group("url"),
            "cover_url": cover_url,
            "latest_chapter": float(chapter_match.group(1)) if chapter_match else None,
        })
    return results


_ENGINES = (_search_madara, _search_ajaxy, _search_v2_api, _search_ajax_html)
_ENGINES_BY_NAME = {engine.__name__: engine for engine in _ENGINES}


def _engine_order(site: dict):
    """The engines to try for this site, the one that answered last time
    first.

    Three of the four are always a wasted round trip to an endpoint the
    site does not have, and until now every search paid all three again.
    Measured 21 August 2026, the whole chain against the one engine that
    actually answers: TeamX 3.24s -> 1.10s and 5.63s -> 4.56s, SWAT
    1.03s -> 0.26s and 2.96s -> 0.36s, Lava Scans 1.93s -> 1.53s.

    Still the whole list, not just the remembered one: a site can change
    theme, and falling back costs exactly what it used to."""
    remembered = _ENGINES_BY_NAME.get(site.get("search_engine") or "")
    if not remembered:
        return _ENGINES
    return (remembered,) + tuple(e for e in _ENGINES if e is not remembered)


def _remember_engine(site: dict, engine):
    """Record which engine answered, on the site itself.

    update_entry rather than writing the whole list back - see
    .claude/rules/ui.md - and only when it changed, so an ordinary
    search writes nothing at all."""
    name = engine.__name__
    if site.get("search_engine") == name:
        return
    site["search_engine"] = name
    try:
        storage.update_entry(SITES_FILE, site.get("id"), {"search_engine": name})
    except Exception:
        pass        # a site being probed before it is saved has no row yet


def search_site(site: dict, query: str, timeout: int = 6, deadline=None) -> list:
    """Try each known engine against one site, stopping at the first that
    returns anything. Returns [] if the site matches no known shape, has
    no matches, or is unreachable.

    `deadline` bounds the whole sequence rather than each engine in it -
    four engines at 6s each is 24s against a dead host. Same shape and
    the same reasoning as anime_sites.search_site; None keeps the old
    per-engine behaviour for existing callers."""
    query = (query or "").strip()
    base_url = site.get("base_url")
    if not query or not base_url:
        return []
    for engine in _engine_order(site):
        step = net.step_timeout(deadline, timeout)
        if step is None:
            break
        try:
            results = engine(base_url, query, step)
        except Exception:
            results = []
        if results:
            _remember_engine(site, engine)
            return results
    return []


# What a site is probed with when the caller offers nothing better. The
# assumption behind it - that every catalogue carries this one, so "no
# results" means the engine didn't match the site's shape - is wrong for
# the sites actually configured here: three of the four are Arabic
# scanlation sites that file series under Arabic titles, and every one of
# them was reported "directed to search page" while opening title pages
# perfectly well. So the caller passes the user's *own* tracked titles
# and this is only the fallback for an empty library (see probe_site).
PROBE_TITLE = "One Piece"

RESOLVES_ENGINE = "engine"
RESOLVES_SEARCH_ONLY = "search-only"
RESOLVES_UNREACHABLE = "unreachable"
RESOLVES_UNKNOWN = "unknown"


def probe_site(site: dict, timeout: int = 6, titles=None) -> str:
    """Whether this site will resolve to per-title pages, or only ever
    fall back to a search link.

    Asked once when a site is added, because until now the only way to
    find out was to add it, use it, and notice which kind of link it
    produced - days later, on an entry that quietly points at a search
    page.

    `titles` is what to look for, in order, and the site passes on the
    first one it resolves. Pass the user's own tracked titles: asking a
    site for a title it simply does not carry proves nothing about
    whether it *can* resolve one (see PROBE_TITLE)."""
    base_url = site.get("base_url")
    if not base_url:
        return RESOLVES_UNREACHABLE
    titles = [t for t in (titles or ()) if (t or "").strip()] or [PROBE_TITLE]
    # The budget grows with the number of titles, but by less than the
    # full per-title amount: only a site that answers for none of them
    # runs all the way through, and one that hangs is cut off by the
    # deadline whichever title it was asked for.
    deadline = net.deadline_in(timeout * (2 + len(titles)))
    for title in titles:
        # One step is held back for the reachability check at the end -
        # `deadline - timeout` is that same deadline brought forward by
        # one. Spending the last of the budget on another search is how a
        # slow site produced "Check failed" (measured: 29s, no verdict)
        # when the honest answer was that it is up and resolved nothing.
        if net.step_timeout(deadline - timeout, timeout) is None:
            break
        if search_site(site, title, timeout, deadline=deadline):
            return RESOLVES_ENGINE
    step = net.step_timeout(deadline, timeout)
    if step is None:
        return RESOLVES_UNKNOWN
    try:
        _get(base_url, step)
        # Answers, but no engine here recognises its search: usable, and
        # every link will be a search link.
        return RESOLVES_SEARCH_ONLY
    except Exception:
        return RESOLVES_UNREACHABLE


# ---------------------------------------------------------------------
# Browsing a site, rather than searching it
#
# Discover's reading rows come from the user's own sites now (the
# owner's ask - "all the manga from the 4 sites, not MangaDex"), which
# needs a *listing*, and every engine above is a search. Measured live
# 21 August 2026 against the four configured sites:
#
#   3asq       /            186KB  1.4s   31 series links
#   TeamX      /            294KB  0.7s   57 series links
#   LavaScans  /manga/      218KB  3.4s   36 series links
#   SWAT       (no HTML)                  Next.js app, 0 links in 69KB
#
# So three are scraped from a listing page and SWAT is asked through the
# same /v2/api/v2/series/ REST endpoint _search_v2_api uses - with the
# search term dropped, which is exactly a browse. That split is why this
# tries the API first for every site: a site that answers it needs no
# HTML at all, and the ones that do not fall through in one failed
# request.

# Where a listing lives, in the order worth trying. The site's own front
# page leads deliberately: on all three HTML sites it carries the most
# series links of any path measured, because it is the "latest updates"
# wall. The rest are the conventional archive paths.
_BROWSE_PATHS = ("", "manga/", "series/", "manga-list/")

# A link that points at a series rather than a chapter, a tag or a page.
# Anchored at the end so ".../manga/slug/chapter-12" - which every card
# on a latest-updates wall also carries - is not mistaken for the series.
_SERIES_URL_RE = re.compile(
    r"/(?:manga|series|comic|comics|webtoon|title)/[^/?#]+/?$", re.I)
# Bounded like _AJAX_SEARCH_CARD_RE: an anchor body is small, and an
# unbounded .*? across a 300KB page is how a scraper starts costing
# seconds (see the catastrophic-backtracking note in planning.md).
_ANCHOR_RE = re.compile(r'<a\b[^>]*?href="([^"]{1,400})"[^>]{0,400}>(.{0,1200}?)</a>',
                        re.DOTALL | re.I)
_IMG_SRC_RE = re.compile(
    r'<img\b[^>]*?\b(?:data-src|data-lazy-src|src)="([^"]{1,600})"', re.I)
_IMG_ALT_RE = re.compile(r'<img\b[^>]*?\balt="([^"]{1,300})"', re.I)
_TAG_RE = re.compile(r"<[^>]+>")
# Words that mean this anchor is a chapter link, not a series - checked
# against the *title*, since a card's chapter row sits inside the same
# listing block.
_CHAPTER_WORD_RE = re.compile(r"^\s*(?:chapter|ch\.?|فصل|الفصل)\b", re.I)


# An alt/anchor text that is really a filename, not a title. Measured:
# 3asq's cards carry alt="cover_250x350" / "01-02" / "00" and TeamX's
# carry the slider's file stem ("tigers", "aura", ".Teamx"), so trusting
# alt text alone filled a whole Discover row with junk names.
_FILENAME_RE = re.compile(
    r"^(?:[\w.\-]+\.(?:jpe?g|png|webp|gif)"      # any image filename
    r"|.*\d+\s*[x×]\s*\d+.*"                      # "cover_250x350"
    r"|[\d\s.\-_]+"                               # "01-02", "00"
    r"|\.\w+)$", re.I)
_ANCHOR_TITLE_ATTR_RE = re.compile(r'<a\b[^>]*?\btitle="([^"]{1,300})"', re.I)


# A long digit run leading a card's text - LavaScans prefixes each one
# with a ten-digit id ("2072267132 Super God Pet Beast Shop"). Six or
# more digits, so a title that genuinely starts with a number ("86",
# "20th Century Boys") keeps it.
_LEADING_ID_RE = re.compile(r"^\d{6,}[\s.\-:]*")


def _clean_text(value: str) -> str:
    text = " ".join(html.unescape(_TAG_RE.sub(" ", value or "")).split())
    return _LEADING_ID_RE.sub("", text).strip()


def _is_gibberish(text: str) -> bool:
    """A single long ASCII word with almost no vowels - a slug that was
    never a title.

    TeamX carries a few of these ("Gchfdtbnquj", which the owner met in
    Discover on a series with 239 chapters), and nothing else on the
    listing page names them, so the honest move is to drop the card
    rather than show a keyboard-mash as a series.

    Deliberately narrow: one word, at least eight characters, ASCII
    only, under 15% vowels. Real one-word titles clear it comfortably -
    "Lookism" is 43% vowels, "Berserk" 29% - and non-Latin titles are
    never even considered, since the ratio means nothing there."""
    word = (text or "").strip()
    if " " in word or len(word) < 8 or not word.isascii() or not word.isalpha():
        return False
    vowels = sum(1 for character in word.lower() if character in "aeiouy")
    return vowels / len(word) < 0.15


def _looks_like_title(text: str) -> bool:
    """Whether this string is plausibly a series name rather than an
    image filename, a chapter label or an unreadable slug."""
    text = (text or "").strip()
    if len(text) < 2 or len(text) > 120:
        return False
    if _CHAPTER_WORD_RE.match(text) or _FILENAME_RE.match(text):
        return False
    if _is_gibberish(text):
        return False
    # Must carry at least one letter in some alphabet - these sites are
    # Arabic as often as not, so this cannot be an ASCII test.
    return any(character.isalpha() for character in text)


def _title_from_slug(url: str) -> str:
    """"Kengan Ashura" out of ".../manga/kengan-ashura/".

    The one title source every one of these sites agrees on, and the
    fallback when a card's alt text turns out to be a filename. A purely
    numeric slug (SWAT's ids) yields nothing, which is correct - that
    site is read through its API, where real titles come with the row."""
    slug = urllib.parse.unquote(
        urllib.parse.urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1])
    words = [w for w in re.split(r"[-_+]+", slug) if w]
    if not words or all(w.isdigit() for w in words):
        return ""
    # Only ASCII words are title-cased: .title() on Arabic is a no-op at
    # best and mangles nothing, but leaving it alone is the honest move.
    pretty = " ".join(w.title() if w.isascii() else w for w in words)
    return pretty if _looks_like_title(pretty) else ""


def _browse_v2_api(base_url: str, limit: int, timeout: int) -> list:
    """SWAT's REST listing - _search_v2_api with no search term."""
    url = (f"{base_url}v2/api/v2/series/?page_size={max(1, min(int(limit), 60))}"
           f"&ordering=-views")
    body = json.loads(_get(url, timeout))
    if not isinstance(body, dict):
        return []
    rows = []
    for record in body.get("results") or []:
        if not record.get("title") or record.get("id") is None:
            continue
        poster = record.get("poster") or {}
        rows.append({"title": record["title"],
                     "url": f"{base_url}series/{record['id']}",
                     "cover_url": poster.get("medium") or poster.get("thumbnail"),
                     "latest_chapter": None})
    return rows


def _browse_html(base_url: str, path: str, limit: int, timeout: int) -> list:
    """Series cards scraped off one listing page.

    Deliberately shape-agnostic rather than one parser per theme: every
    one of these sites renders a card as an anchor to the series with
    the cover image inside it, and the alt text on that image is the
    title. Where an anchor carries no image, its own text is the title -
    that is the second anchor of the same card (the heading link), which
    is why results are merged by URL rather than taken one per anchor."""
    body = _get(base_url + path, timeout)
    found = {}
    order = []
    for match in _ANCHOR_RE.finditer(body):
        href, inner = match.group(1), match.group(2)
        url = urllib.parse.urljoin(base_url, html.unescape(href))
        if not _SERIES_URL_RE.search(urllib.parse.urlsplit(url).path):
            continue
        if url not in found:
            found[url] = {"title": "", "url": url, "cover_url": None,
                          "latest_chapter": None}
            order.append(url)
        record = found[url]

        # Title candidates in descending trust, and the order is
        # measured rather than assumed. The anchor's own text and its
        # title attribute name the series when they exist, and are
        # preferred because they carry the *Arabic* name on the Arabic
        # sites. The URL slug comes next: it is the one source all three
        # HTML sites agree on. **alt text is last on purpose** - 3asq
        # renders alt="cover_250x350" and TeamX the slider's file stem
        # ("tigers", "aura", "ops"), so trusting it above the slug filled
        # two of four Discover rows with filenames.
        if not record["title"]:
            attr = _ANCHOR_TITLE_ATTR_RE.search(match.group(0))
            alt = _IMG_ALT_RE.search(inner)
            for candidate in (_clean_text(inner),
                              _clean_text(attr.group(1) if attr else ""),
                              _title_from_slug(url),
                              _clean_text(alt.group(1) if alt else "")):
                if _looks_like_title(candidate):
                    record["title"] = candidate
                    break
        image = _IMG_SRC_RE.search(inner)
        if image and not record["cover_url"]:
            record["cover_url"] = _strip_wp_size_suffix(
                urllib.parse.urljoin(base_url, html.unescape(image.group(1))))

    # **A text used by many cards is a label, not a title.** Azora's
    # cards lead with the series *type* ("مانهوا"), so the anchor text -
    # otherwise the most trustworthy candidate - named every one of its
    # thirty rows the same thing. Counting is the general form of that
    # check: no listing page carries the same series three times, so a
    # repeated string is a category, a badge or a status, and the slug
    # is what to fall back to.
    counts = {}
    for record in found.values():
        if record["title"]:
            counts[record["title"]] = counts.get(record["title"], 0) + 1
    for url, record in found.items():
        if counts.get(record["title"], 0) >= 3:
            record["title"] = _title_from_slug(url) or record["title"]

    rows = [found[url] for url in order if found[url]["title"]]
    # Cards with artwork fill the row first, the rest behind them and
    # both in the page's own order: this feeds a wall of poster tiles,
    # and a letter avatar among real covers reads as a broken image
    # rather than as a title with no art on its site.
    covered = [r for r in rows if r.get("cover_url")]
    bare = [r for r in rows if not r.get("cover_url")]
    return (covered + bare)[:limit]


def browse_site(site: dict, limit: int = 30, timeout: int = 8,
                deadline=None) -> list:
    """What this site is currently publishing, as search-shaped rows.

    Returns [] for a site that answers neither the API nor any listing
    path - a Discover row that cannot reach a site shows nothing from
    it, never an error. Bounded by `deadline` across the whole attempt
    chain for the same reason search_site is: four paths at 8s each is
    not an 8s wait."""
    base_url = site.get("base_url")
    if not base_url:
        return []
    step = net.step_timeout(deadline, timeout) if deadline else timeout
    if step is None:
        return []
    try:
        rows = _browse_v2_api(base_url, limit, step)
        if rows:
            return rows
    except Exception:
        pass            # not an API site; fall through to the HTML paths
    for path in _BROWSE_PATHS:
        step = net.step_timeout(deadline, timeout) if deadline else timeout
        if step is None:
            break
        try:
            rows = _browse_html(base_url, path, limit, step)
        except Exception:
            rows = []
        # Three or more is a listing; one or two is a stray link on a
        # page that is not one.
        if len(rows) >= 3:
            return rows
    return []


def browse_all(limit: int = 30, timeout: int = 8, deadline=None) -> list:
    """Every configured site's current listing, in parallel, each row
    tagged with the site it came from - which is what lets a Discover
    card open its chapters on that site and no other."""
    sites = list_sites()
    if not sites:
        return []
    rows = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(sites)) as pool:
        jobs = {pool.submit(browse_site, site, limit, timeout, deadline): site
                for site in sites}
        for job in concurrent.futures.as_completed(jobs):
            site = jobs[job]
            try:
                found = job.result()
            except Exception:
                found = []
            for row in found:
                rows.append({**row, "site_id": site["id"],
                             "site_name": site["name"]})
    return rows


# What an interactive search is allowed to cost. Measured 21 August
# 2026 over the owner's six configured sites, *before* any of this:
# search_all took 9.37s ("kingdom"), 2.83s ("solo") and 11.04s ("I Want
# to Stop Killing"), because it waited for every site and handed each
# one no deadline at all - so four engines at a full timeout apiece
# could cost 24s on one dead host. Per-site worst measured: TeamX 12.0s,
# Azora 10.6s, both while returning nothing.
SEARCH_TIMEOUT = 4
SEARCH_BUDGET = 6.0

# Enough rows to fill the strip, from enough sites that the answer is
# not one site's catalogue. **Below EARLY_SITES nothing returns early**,
# and that is the whole safeguard: a title carried by exactly one site -
# measured, "I Want to Stop Killing" is on Mangalek and nowhere else -
# must not be dropped for being alone, so a thin answer waits out the
# full budget instead.
#
# 12 originally, raised to 24 once the connection pool made waiting for
# a fourth and fifth site nearly free. Measured over three queries, two
# runs each, on the owner's six sites:
#
#            12          24          48
#   kingdom  12 / 0.54s  26 / 1.00s  26 / 0.97s
#   solo     17 / 0.27s  35 / 1.09s  35 / 0.94s
#   tower    15 / 0.25s  31 / 1.05s  31 / 0.89s  <- and one run 19 / 6.01s
#
# 24 roughly doubles what the owner sees for about half a second, and
# stays inside the one-second rule. 48 buys nothing at all - it is above
# what the sites actually hold - and is actively worse: a cap that can
# never be reached means every search waits out the whole budget, which
# is the 6.01s run above returning *fewer* rows than the 0.89s one.
EARLY_RESULTS = 24
EARLY_SITES = 3


# What a finished search found, keyed by the query that found it.
#
# **This is what stops a longer query showing less than its own prefix**
# - the owner's report, in his words: "why is there less results while
# searching (kingdom) than searching (kingdo) while the results
# increased has the name kingdom on them". Measured per site, both
# queries return *identical* rows (3asq 4 and 4, TeamX 2 and 2, Mangalek
# 14 and 14): what differed was which sites answered inside the budget.
# Three runs of the very same query returned 22, then 10, then 16 rows.
# It was a race, never the query.
#
# The transport fix (net.py's pool) is most of the cure - the worst site
# went from 25.4s to 0.9s - but a race that is merely unlikely still
# runs sometimes, and the failure is silent. So a query also inherits
# what its own prefixes already found: every one of those rows was
# returned by a real site for a real query, and the ones whose titles
# still contain what is now typed are still answers. Typing forward can
# then only ever *narrow*, which is what someone typing expects.
_SEARCH_CACHE = {}
_SEARCH_CACHE_TTL_S = 180.0
_SEARCH_CACHE_MAX = 40


def _cached_prefix_rows(query: str) -> list:
    """Rows already found for a prefix of `query`, still matching it.

    Longest prefix first, so "kingdom" prefers what "kingdo" found over
    what "k" found - both are valid, the longer one is closer to what
    the sites were actually asked."""
    now = time.monotonic()
    for cached_query in sorted(_SEARCH_CACHE, key=len, reverse=True):
        if not query.startswith(cached_query) or cached_query == query:
            continue
        at, rows = _SEARCH_CACHE[cached_query]
        if now - at >= _SEARCH_CACHE_TTL_S:
            continue
        return [row for row in rows
                if query in (row.get("title") or "").lower()]
    return []


def _remember_search(query: str, rows: list):
    if not rows:
        return          # an empty answer is usually a race, not a fact
    _SEARCH_CACHE[query] = (time.monotonic(), [dict(r) for r in rows])
    while len(_SEARCH_CACHE) > _SEARCH_CACHE_MAX:
        oldest = min(_SEARCH_CACHE, key=lambda k: _SEARCH_CACHE[k][0])
        _SEARCH_CACHE.pop(oldest, None)


def _row_identity(row):
    """What makes two rows the same result. The url where there is one -
    the same title on two sites is two genuine places to read it."""
    return (row.get("url") or "").strip().lower() or (
        (row.get("site_id"), (row.get("title") or "").strip().lower()))


def search_all(query: str, timeout: int = SEARCH_TIMEOUT,
               budget: float = SEARCH_BUDGET, deadline=None) -> list:
    """Search every configured site in parallel; returns a flat list of
    {title, url, cover_url, site_id, site_name}.

    Bounded twice over, because this is what runs while someone is
    typing. Every site shares one `deadline`, so no site can cost more
    than the budget however many engines it works through; and the fan-
    out stops as soon as there is honestly enough to show (see
    EARLY_RESULTS / EARLY_SITES) rather than waiting on the slowest.

    Stragglers are abandoned, never waited for - the same rule as
    streams._run_all. They cannot linger: they are bounded by the same
    deadline their requests were given.

    Whatever this run misses, it inherits from what the query's own
    prefixes already found (see _SEARCH_CACHE)."""
    sites = list_sites()
    query = (query or "").strip()
    if not query or not sites:
        return []
    if deadline is None:
        deadline = net.deadline_in(budget)
    key = query.lower()

    results, answered = [], 0
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=len(sites))
    try:
        jobs = {pool.submit(search_site, site, query, timeout, deadline): site
                for site in sites}
        try:
            for job in concurrent.futures.as_completed(
                    jobs, timeout=max(0.0, deadline - time.monotonic())):
                site = jobs[job]
                try:
                    found = job.result()
                except Exception:
                    found = []
                if found:
                    answered += 1
                for row in found:
                    results.append({**row, "site_id": site["id"],
                                    "site_name": site["name"]})
                if len(results) >= EARLY_RESULTS and answered >= EARLY_SITES:
                    break
        except concurrent.futures.TimeoutError:
            pass    # the budget is the answer; whatever arrived is the result
    finally:
        # wait=False on purpose: see the straggler note above. Waiting
        # here would put the whole point of the early return back.
        pool.shutdown(wait=False, cancel_futures=True)

    # This run first, then anything a prefix found that this run did not
    # reach: a site that answered now is fresher than the same site's
    # older answer, and dedup keeps the first of each.
    seen = {_row_identity(row) for row in results}
    for row in _cached_prefix_rows(key):
        identity = _row_identity(row)
        if identity not in seen:
            seen.add(identity)
            results.append(row)
    _remember_search(key, results)
    return results


def upgrade_cover_url(url):
    """Re-derive the sharp original for a cover_url saved before the
    engines above started stripping WordPress's size-suffixed crops /
    TeamX's thumbnail_ prefix - same transforms, applied to an
    already-saved URL instead of a fresh search result, so existing
    tracker entries can be backfilled without re-searching. Returns the
    url unchanged if neither pattern matches (nothing to upgrade)."""
    if not url:
        return url
    url = _strip_wp_size_suffix(url)
    if "olympustaff.com" in url:
        url = _TEAMX_THUMBNAIL_PREFIX_RE.sub("/", url, count=1)
    return url
