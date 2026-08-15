"""Anime video-site directory (Settings-managed) + live search.

Same deal as manga_sites.py, and for the same reason: a site is just a
name + base URL, and `search_site` tries a handful of known search-
endpoint shapes against it, so adding a site in Settings needs no
hand-typed URL template. What this buys is the point of the module -
double-clicking a tracked Anime entry lands on *that title's own page*
on the site, never on a search-results listing.

The shapes here were found by fetching the real sites and reading what
came back, not by guessing (see each engine's docstring for which site
it was verified against). Behind them sits `_search_generic_html`, which
needs no known shape at all - it scrapes the site's ordinary search
results page for links and the text around them. That is what makes a
newly added site work without anyone adding an engine for it, and it
covers the large majority of ordinary server-rendered sites.

What it cannot cover is a site that renders its results in the browser:
if the HTML that arrives has no results in it, there is nothing to
scrape. Crunchyroll is that case permanently - its search page is a
Next.js shell with no results in the body (the only /series/ links there
are a static promo carousel), and its content API answers 401 without an
OAuth bearer, so no plain unauthenticated GET can resolve a Crunchyroll
title to its own page. A site whose results carry no title text near the
link (an image-only grid with opaque slugs) is the other gap. Both fall
back to `search_page_url` - a plain on-site search page - which is the
only honest thing left to do.

Anime *metadata* (covers, imdb id, episode counts) still comes from
Stremio's Cinemeta, exactly as before - these engines only ever supply
the per-site page URL to open.

Every function fails soft (empty list / None) so a flaky or
unrecognized site never blocks the others or crashes the UI.
"""

import concurrent.futures
import html
import json
import re
import urllib.parse
import urllib.request
import uuid

from . import storage, title_match

SITES_FILE = "anime_sites.json"

DEFAULT_SITES = [
    {"name": "Crunchyroll", "base_url": "https://www.crunchyroll.com/"},
]

# How confident the fuzzy title match has to be before a resolved page
# URL is saved onto an entry. Unlike the manga flow - where the user
# clicks the exact search hit they want - this resolution happens in the
# background off a Cinemeta title, so a near-miss would silently pin the
# entry to the wrong show. Set high on purpose: a site search for "One
# Piece" returns the series *and* fifteen One Piece movies, and only the
# exact title should win.
MATCH_THRESHOLD = 0.75

# The bar for a hit from the generic scraper, set higher than the one
# above on purpose. An engine returns a curated result set - everything
# in it is a title the site's own search chose. The generic scraper
# returns whatever was linked on the page, search hits mixed in with
# sidebar carousels and "related" strips, so a mediocre score there is
# far more likely to be some other show than a differently-worded
# spelling of this one.
GENERIC_MATCH_THRESHOLD = 0.85


def _normalize(base_url: str) -> str:
    base_url = (base_url or "").strip()
    if base_url and not base_url.endswith("/"):
        base_url += "/"
    return base_url


def _to_base_url(site: dict) -> str:
    """Sites used to be stored as a hand-typed search-URL *prefix*
    ("https://www.crunchyroll.com/search?q="). Live search needs the
    site root instead, so an old entry is folded down to its origin -
    everything after the host was only ever the search path."""
    if site.get("base_url"):
        return _normalize(site["base_url"])
    parsed = urllib.parse.urlsplit((site.get("search_url") or "").strip())
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}/"
    return ""


def _load():
    sites = storage.load(SITES_FILE, None)
    if sites is None:
        sites = [{"id": str(uuid.uuid4()), "name": s["name"], "base_url": s["base_url"]}
                  for s in DEFAULT_SITES]
        storage.save(SITES_FILE, sites)
        return sites
    # Migrate anything still carrying the old search_url field, in place
    # and once, so every caller below can assume base_url exists.
    if any("base_url" not in s for s in sites):
        sites = [{"id": s.get("id") or str(uuid.uuid4()), "name": s.get("name", ""),
                   "base_url": _to_base_url(s)} for s in sites]
        storage.save(SITES_FILE, sites)
    return sites


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


def remove_site(site_id: str):
    sites = [s for s in _load() if s["id"] != site_id]
    storage.save(SITES_FILE, sites)


# Sites whose on-site search lives somewhere other than WordPress'
# universal "?s=". Only hosts actually fetched and confirmed to return a
# results page for these are listed - everything else gets "?s=", which
# is right for the WordPress-themed aggregators and harmless elsewhere.
_SEARCH_PATHS = (
    ("crunchyroll.com", "search?q={q}"),
    ("animeflv", "browse?q={q}"),
    ("jkanime", "buscar/{q}/"),
    ("monoschinos", "buscar?q={q}"),
)


def search_page_url(base_url: str, query: str) -> str:
    """Plain on-site search results page - the fallback for a site that
    matches no engine below, or whose search finds nothing for a title.
    Never the *preferred* target: the whole point of the engines is to
    land on the title's own page instead."""
    base_url = _normalize(base_url)
    quoted = urllib.parse.quote(query)
    for host_fragment, path in _SEARCH_PATHS:
        if host_fragment in base_url:
            return base_url + path.format(q=quoted)
    return f"{base_url}?s={quoted}"


# ---- Search engines --------------------------------------------------
# Each takes (base_url, query, timeout) and returns a list of
# {title, url, cover_url}, or raises on network/parse failure.

def _get(url: str, timeout: int) -> str:
    req = urllib.request.Request(url, headers={
        "Accept": "application/json, text/html, */*",
        "User-Agent": "Mozilla/5.0 PC-App/1.0",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


def _post(url: str, fields: dict, timeout: int) -> str:
    data = urllib.parse.urlencode(fields).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json, text/html, */*",
        "User-Agent": "Mozilla/5.0 PC-App/1.0",
        "X-Requested-With": "XMLHttpRequest",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


# WordPress generates cropped copies of every upload ("cover-231x300.jpg")
# next to the original; stripping the suffix re-requests the full-size
# one. Same transform manga_sites.py applies, for the same reason.
_WP_SIZE_SUFFIX_RE = re.compile(r'-\d{2,4}x\d{2,4}(?=\.[a-zA-Z0-9]+(?:[?#].*)?$)')

# One result card in the Anime4up/vnxweb WordPress theme. Both the
# current markup and the older one this theme family shares (witanime,
# animelek and the anime4up mirrors all ship variations of it) wrap each
# hit in .anime-card-container, put the canonical link in the card's
# <h3> anchor, and the poster in an <img> that lazy-loads from either
# data-image or src.
_A4U_CARD_SPLIT_RE = re.compile(r'anime-card-container', re.IGNORECASE)
_A4U_TITLE_LINK_RE = re.compile(
    r'<h3[^>]*>\s*<a[^>]+href=["\'](?P<url>[^"\']+)["\'][^>]*>(?P<title>.*?)</a>',
    re.IGNORECASE | re.DOTALL)
_A4U_IMG_RE = re.compile(
    r'<img[^>]+(?:data-image|data-src|src)=["\'](?P<img>https?://[^"\']+)["\']',
    re.IGNORECASE)
_TAG_RE = re.compile(r'<[^>]+>')


def _text(fragment: str) -> str:
    return html.unescape(_TAG_RE.sub("", fragment or "")).strip()


def _search_anime_cards(base_url: str, query: str, timeout: int) -> list:
    """The Anime4up-family WordPress theme (theme header credits
    vnxweb.com). Verified against the live anime4up mirror
    4p.r9x5n2m.shop: a plain GET of "?search_param=animes&s=one+piece"
    returns the fully server-rendered results grid - no JS needed - with
    each hit's own /anime/<slug>/ page URL in its card. search_param is
    what the site's own gateway page appends; plain "?s=" returns the
    same grid, so the extra parameter costs nothing and keeps mirrors
    that use it to filter out blog posts working."""
    body = _get(f"{base_url}?search_param=animes&s={urllib.parse.quote(query)}", timeout)
    results = []
    for card in _A4U_CARD_SPLIT_RE.split(body)[1:]:
        link = _A4U_TITLE_LINK_RE.search(card)
        if not link:
            continue
        title = _text(link.group("title"))
        if not title:
            continue
        img = _A4U_IMG_RE.search(card)
        cover_url = _WP_SIZE_SUFFIX_RE.sub("", img.group("img"), count=1) if img else None
        results.append({"title": title, "url": html.unescape(link.group("url")),
                         "cover_url": cover_url})
    return results


def _search_animeflv_api(base_url: str, query: str, timeout: int) -> list:
    """AnimeFLV's own JSON search - POST "api/animes/search" with
    value=<query>, answering [{id, title, type, slug}]. Verified against
    www4.animeflv.net; the per-title page is /anime/<slug>. (The same
    path answers 500 to a GET, hence the POST.)"""
    body = json.loads(_post(f"{base_url}api/animes/search", {"value": query}, timeout))
    if not isinstance(body, list):
        return []
    return [{"title": r["title"], "url": f"{base_url}anime/{r['slug']}", "cover_url": None}
             for r in body if isinstance(r, dict) and r.get("title") and r.get("slug")]


def _search_madara(base_url: str, query: str, timeout: int) -> list:
    """The WordPress 'Madara' theme's built-in AJAX search. Sold as a
    manga theme but shipped on anime sites too, and the endpoint is
    identical to the one manga_sites.py already proves against real
    sites - kept here so an anime site running it resolves without
    needing its own engine. Titles/links only, no covers."""
    body = json.loads(_post(f"{base_url}wp-admin/admin-ajax.php",
                             {"action": "wp-manga-search-manga", "title": query}, timeout))
    if not isinstance(body, dict) or not body.get("success"):
        return []
    return [{"title": r["title"], "url": r["url"], "cover_url": None}
             for r in body.get("data") or [] if r.get("url") and r.get("title")]


_ENGINES = (_search_anime_cards, _search_animeflv_api, _search_madara)


# ---- Generic fallback ------------------------------------------------
# Everything above knows a site's exact search shape. This one knows
# nothing about the site: it fetches whatever `search_page_url` builds
# and reads the links out of the HTML. It exists so that adding a site
# nobody has written an engine for still lands on the title's own page.

_ANCHOR_RE = re.compile(r'<a\b(?P<attrs>[^>]*)>(?P<inner>.*?)</a>', re.IGNORECASE | re.DOTALL)
_HREF_RE = re.compile(r'href\s*=\s*["\'](?P<url>[^"\']+)["\']', re.IGNORECASE)
# title=/alt= carry the title on themes that show only a poster image.
_ATTR_TEXT_RE = re.compile(r'(?:title|alt)\s*=\s*["\']([^"\']{2,120})["\']', re.IGNORECASE)
_HEADING_RE = re.compile(r'<h[1-4][^>]*>(.*?)</h[1-4]>', re.IGNORECASE | re.DOTALL)

# First path segments that are never a title's own page. Matched against
# the first segment only, so a title legitimately slugged "login-again"
# under /serie/ isn't thrown away with the site's actual login link.
_JUNK_SEGMENTS = frozenset((
    "feed", "rss", "comments", "cdn-cgi", "login", "register", "signup",
    "author", "page", "tag", "category", "genre", "plans", "contactus",
    "about", "contact", "privacy", "dmca", "search",
))

# How far past a link to look for the text naming it. Sized to cross one
# card's closing markup, not to reach the next card - animhq puts the
# title in an <h3> about 120 characters after the anchor closes.
_NEARBY_WINDOW = 700

# Titles are short. Anything longer is a synopsis or a whole card's worth
# of text, and letting one through would let a plot summary that happens
# to mention the query resolve the entry to the wrong show.
_MAX_TITLE_LEN = 120


# A site that changed domain often keeps the old host answering while
# every result on the page links to the new one - monoschinos2.com serves
# the search page and links all 20 results to monoschinos.st, which made
# a same-host-only rule find exactly zero candidates there. Cross-host
# links are otherwise ads, socials and CDNs, so the only extra host
# trusted is the one the page itself declares as its canonical origin.
_CANONICAL_RE = re.compile(
    r'<link[^>]+rel=["\']canonical["\'][^>]+href=["\'](https?://[^"\']+)["\']'
    r'|<meta[^>]+property=["\']og:url["\'][^>]+content=["\'](https?://[^"\']+)["\']',
    re.IGNORECASE)


def _host_key(netloc: str) -> str:
    return (netloc or "").lower().removeprefix("www.")


def _self_hosts(body: str, base_url: str) -> set:
    hosts = {_host_key(urllib.parse.urlsplit(base_url).netloc)}
    for canonical, og_url in _CANONICAL_RE.findall(body):
        host = _host_key(urllib.parse.urlsplit(canonical or og_url).netloc)
        if host:
            hosts.add(host)
    return hosts


def _candidate_urls(body: str, base_url: str) -> list:
    """Every on-site link in `body` paired with each piece of text that
    might be naming it. One link can yield several candidates - the
    anchor's own text, its title/alt attributes, a heading next to it,
    and its slug - because sites disagree about where the title goes.
    Anime4up puts it inside the anchor; animhq puts it in a sibling <h3>
    while the anchor itself wraps the poster, the genre list and the full
    synopsis. Emitting each separately means the fuzzy matcher scores the
    one that is really the title, instead of one blob that scores badly
    however good the title inside it was."""
    hosts = _self_hosts(body, base_url)
    candidates = []
    for anchor in _ANCHOR_RE.finditer(body):
        href = _HREF_RE.search(anchor.group("attrs"))
        if not href:
            continue
        url = urllib.parse.urljoin(base_url, html.unescape(href.group("url")).strip())
        parts = urllib.parse.urlsplit(url)
        path = parts.path.strip("/")
        # A query string means a listing or a filter, never a title's own
        # page on any site seen here - and it is how the search page
        # links back to itself.
        if parts.scheme not in ("http", "https") or _host_key(parts.netloc) not in hosts:
            continue
        if not path or parts.query or "wp-" in path:
            continue
        if path.split("/")[0].lower() in _JUNK_SEGMENTS:
            continue

        texts = [_text(anchor.group("inner"))]
        texts += _ATTR_TEXT_RE.findall(anchor.group("attrs"))
        texts += _ATTR_TEXT_RE.findall(anchor.group("inner"))
        # Headings after the link, stopping at the next link so one
        # card's title can't be read as its neighbour's.
        tail = body[anchor.end():anchor.end() + _NEARBY_WINDOW].split("<a ")[0]
        texts += [_text(h) for h in _HEADING_RE.findall(tail)]
        # ...and before it, for themes that put the heading first.
        head = body[max(0, anchor.start() - _NEARBY_WINDOW):anchor.start()].rsplit("</a>", 1)[-1]
        texts += [_text(h) for h in _HEADING_RE.findall(head)]
        # The slug, which is title-derived on nearly every site and is
        # the only text left when the card is a bare poster image.
        texts.append(urllib.parse.unquote(path.split("/")[-1]).replace("-", " ").replace("_", " "))

        for candidate in texts:
            candidate = " ".join((candidate or "").split())
            if 2 <= len(candidate) <= _MAX_TITLE_LEN:
                candidates.append({"title": candidate, "url": url, "cover_url": None})
    return candidates


def _search_generic_html(base_url: str, query: str, timeout: int) -> list:
    """Scrape the site's own search-results page. Works on any site that
    renders its results server-side and names them in text near the link
    - verified against animhq.com, whose custom WordPress theme has no
    AJAX search at all (its search box is a plain GET form posting `s` to
    the site root) and whose result cards keep the title in an <h3>
    outside the anchor, so no engine above can see it."""
    return _candidate_urls(_get(search_page_url(base_url, query), timeout), base_url)


def search_site(site: dict, query: str, timeout: int = 6) -> list:
    """Try each known engine against one site, stopping at the first that
    returns anything. Returns [] if the site matches no known shape, has
    no matches, or is unreachable."""
    query = (query or "").strip()
    base_url = _to_base_url(site or {})
    if not query or not base_url:
        return []
    for engine in _ENGINES:
        try:
            results = engine(base_url, query, timeout)
        except Exception:
            results = []
        if results:
            return results
    return []


_WORD_BOUNDED = r'(?:^|\s)%s(?:\s|$)'


def _query_variants(title: str) -> list:
    """The title, then the part before a subtitle separator. Cinemeta
    names a show "Frieren: Beyond Journey's End" while a site indexes it
    under the romaji "Sousou no Frieren" - searching the full string
    finds nothing, searching "Frieren" finds it. Tried in order, and only
    while the previous one came back empty, so the common case is still
    one request."""
    variants = [title_match.search_query(title).strip()]
    for separator in (":", " - ", " – "):
        head = variants[0].split(separator)[0].strip()
        if len(head) >= 3 and head not in variants:
            variants.append(head)
    return [v for v in variants if v]


def _best_match(query: str, results: list, threshold: float = MATCH_THRESHOLD):
    """The result that is really this title, or None. Two ways to
    qualify, both deliberately narrow:

    1. A high fuzzy score - the ordinary case, and the only one that can
       match across differently-worded titles.
    2. The query appearing whole inside the result's title, which is how
       a site's romaji naming ("Sousou no Frieren") relates to what was
       typed ("Frieren"). Among those, the *shortest* title wins: it is
       the base series rather than a sequel or a movie spin-off
       ("Sousou no Frieren" over "Sousou no Frieren 2nd Season").

    Everything else returns None, because a search page is a much
    smaller failure than opening someone else's show."""
    scored = [(title_match.similarity(query, r["title"]), r) for r in results]
    # Shortest title breaks a score tie, for the same reason it decides
    # the containment case below. Searching a big franchise ties a whole
    # shelf of spin-offs at one score - title_match caps everything that
    # merely starts with the query at 0.95 - and picking by list order
    # there means picking whichever movie the site listed first.
    scored.sort(key=lambda pair: (-pair[0], len(title_match.normalize(pair[1]["title"]))))
    if scored and scored[0][0] >= threshold:
        return scored[0][1]

    normalized_query = title_match.normalize(query)
    # Length floor: a two- or three-letter query is a substring of far
    # too much of any catalog to mean anything.
    if len(normalized_query) < 5:
        return None
    pattern = re.compile(_WORD_BOUNDED % re.escape(normalized_query))
    contained = [r for r in results if pattern.search(title_match.normalize(r["title"]))]
    if not contained:
        return None
    return min(contained, key=lambda r: len(title_match.normalize(r["title"])))


def _corroborated(query: str, candidates: list) -> list:
    """Generic candidates whose URL backs up their text. Only the generic
    scraper needs this: an engine returns titles the site's own search
    chose, while the scraper also picks up the *page's* chrome, and a
    search page names the query in its own heading. jkanime titles its
    results page "Resultados del busqueda: Solo Leveling", which sits a
    few hundred characters from the header's logout link - so searching
    it for "Solo Leveling" resolved the entry to jkanime.net/salir,
    scoring only 0.52 but qualifying under `_best_match`'s containment
    rule, which has no threshold to fail.

    Measured across every generic hit on animhq, animeflv and jkanime,
    each genuine one is either an exact title ("Solo Leveling" on a card
    linking .../ore-dake-level-up-na-ken/, or "Attack on Titan" on one
    linking the opaque .../aot1234/) or has the query in its slug
    ("Frieren" -> .../sousou-no-frieren). Chrome is neither, since its
    text is a sentence and its link is whatever was nearby. Filtering
    before scoring rather than after, so dropping a chrome candidate lets
    a real result behind it win instead of losing the whole search."""
    normalized_query = title_match.normalize(query)
    if not normalized_query:
        return []
    pattern = re.compile(_WORD_BOUNDED % re.escape(normalized_query))
    kept = []
    for candidate in candidates:
        if title_match.normalize(candidate["title"]) == normalized_query:
            kept.append(candidate)
            continue
        path = urllib.parse.urlsplit(candidate["url"]).path
        slug = title_match.normalize(
            urllib.parse.unquote(path).replace("-", " ").replace("_", " "))
        if pattern.search(slug):
            kept.append(candidate)
    return kept


def resolve_page_url(site: dict, title: str, timeout: int = 6):
    """The one thing the tracker actually wants: the URL of `title`'s own
    page on `site`, or None if the site can't resolve it. None is the
    honest answer for a site with no matching engine or a genuine
    no-match - the caller then falls back to search_page_url rather than
    saving a link to the wrong show.

    The known engines get every query variant first, and only if none of
    them resolved anything does the generic scraper get a turn. A site
    with a real engine therefore never sees the generic path: the
    engine's result set is the site's own answer to the search, which is
    always better evidence than links read off a page."""
    variants = _query_variants(title)
    for variant in variants:
        results = search_site(site, variant, timeout)
        if not results:
            continue
        match = _best_match(variant, results)
        if match:
            return match["url"]

    base_url = _to_base_url(site or {})
    if not base_url:
        return None
    for variant in variants:
        try:
            candidates = _search_generic_html(base_url, variant, timeout)
        except Exception:
            continue
        match = _best_match(variant, _corroborated(variant, candidates),
                            GENERIC_MATCH_THRESHOLD)
        if match:
            return match["url"]
    return None


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
