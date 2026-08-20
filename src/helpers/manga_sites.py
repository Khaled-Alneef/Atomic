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
import urllib.parse
import urllib.request
import uuid

from . import net, storage

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


def fetch_manga_details(page_url: str, timeout: int = 6):
    """Best-effort cover + latest-chapter number for a specific manga
    page - used when the engine that matched it didn't already provide
    them (Madara's search endpoint gives neither; SWAT's search endpoint
    gives neither but its per-series detail endpoint does). Called
    lazily, only for the one result the user actually picks, not every
    search hit. Returns {"cover_url": ..., "latest_chapter": ...}, either
    possibly None."""
    swat_match = _SWAT_SERIES_URL_RE.match(page_url)
    if swat_match:
        return _swat_series_details(swat_match.group(1), swat_match.group(2), timeout)
    return _scrape_manga_page(page_url, timeout)


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


def _scrape_manga_page(page_url: str, timeout: int):
    try:
        body = _get(page_url, timeout)
    except Exception:
        return {"cover_url": None, "latest_chapter": None}

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

def _post_json(url: str, fields: dict, timeout: int):
    data = urllib.parse.urlencode(fields).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 PC-App/1.0",
    })
    deadline = net.deadline_in(timeout)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(net.read_text(resp, deadline))


def _get(url: str, timeout: int, extra_headers: dict = None) -> str:
    req = urllib.request.Request(url, headers={
        "Accept": "application/json, text/html, */*",
        "User-Agent": "Mozilla/5.0 PC-App/1.0",
        **(extra_headers or {}),
    })
    deadline = net.deadline_in(timeout)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
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
    for engine in _ENGINES:
        step = net.step_timeout(deadline, timeout)
        if step is None:
            break
        try:
            results = engine(base_url, query, step)
        except Exception:
            results = []
        if results:
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


def search_all(query: str, timeout: int = 6) -> list:
    """Search every configured site in parallel; returns a flat list of
    {title, url, cover_url, site_id, site_name}."""
    sites = list_sites()
    if not (query or "").strip() or not sites:
        return []
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(sites)) as pool:
        future_to_site = {pool.submit(search_site, site, query, timeout): site for site in sites}
        for future in concurrent.futures.as_completed(future_to_site):
            site = future_to_site[future]
            try:
                site_results = future.result()
            except Exception:
                site_results = []
            for r in site_results:
                results.append({**r, "site_id": site["id"], "site_name": site["name"]})
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
