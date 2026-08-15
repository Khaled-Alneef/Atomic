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
OAuth bearer. Netflix is the same shape for a different reason: its
/search is behind a sign-in, so an unauthenticated GET never reaches a
result at all. A site whose results carry no title text near the link
(an image-only grid with opaque slugs) is the other gap.

Those services are nonetheless resolved *without* asking them anything,
because a third party publishes the answer: AniList records each title's
links in `Media.externalLinks`, openly and without a key. An entry on
one of them skips every engine above and goes to `_streaming_page_url`
instead - see `_STREAMING_SITES` for the ones covered and the URL shapes
that come back. When AniList has no match or no link for that service
(it holds neither for a show the service doesn't carry), and for the
image-only-grid case above, both fall back to `search_page_url` - a
plain on-site search page - which is the only honest thing left to do.

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
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from . import anilist, storage, title_match, wikidata

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
    # Netflix's search sits behind a sign-in, so it can't be fetched and
    # confirmed the way the others were - listed anyway because the
    # default "?s=" is definitely wrong for it, and this at least lands
    # a signed-in user on their own results. Only reached when AniList
    # has no Netflix link for the title (see _STREAMING_SITES).
    ("netflix.com", "search?q={q}"),
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

# A search results page is a few hundred KB. This is the ceiling on what
# is worth reading from one, and it is a safety limit rather than a
# tuning knob: everything downstream scans the whole string, so an
# unbounded body is an unbounded parse, and the parse holds the GIL (see
# _candidate_urls). Overshooting it means no result from that site, which
# is a normal outcome here.
_MAX_RESPONSE_BYTES = 5_000_000

# Small on purpose: read1() returns whatever has arrived rather than
# waiting to fill the buffer, which is what lets the deadline below be
# checked while a slow sender is still dribbling.
_READ_CHUNK = 65536


def _read_body(resp, deadline: float) -> str:
    """The response body, given a size ceiling and a wall-clock deadline.

    `urlopen(timeout=...)` bounds each individual socket operation, not
    the transfer - so a host that sends one byte every couple of seconds
    resets that timer forever and `resp.read()` never returns. Measured
    against exactly that: a local server dribbling a chunked body held a
    lookup thread for over 180s with no sign of stopping, and four of
    those would permanently drain lookup_pool's whole worker set.

    read1() rather than read(): read() waits until it has the full amount
    asked for, so a deadline checked around it is never reached while the
    dribble continues. read1() comes back with whatever has arrived, so
    the check below actually gets a turn."""
    chunks, total = [], 0
    while True:
        chunk = resp.read1(_READ_CHUNK)
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > _MAX_RESPONSE_BYTES:
            raise ValueError("response body over the size cap")
        if time.monotonic() > deadline:
            raise TimeoutError("response body over the time budget")
    return b"".join(chunks).decode("utf-8", "replace")


def _get(url: str, timeout: int) -> str:
    req = urllib.request.Request(url, headers={
        "Accept": "application/json, text/html, */*",
        "User-Agent": "Mozilla/5.0 PC-App/1.0",
    })
    deadline = time.monotonic() + timeout
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return _read_body(resp, deadline)


def _post(url: str, fields: dict, timeout: int) -> str:
    data = urllib.parse.urlencode(fields).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json, text/html, */*",
        "User-Agent": "Mozilla/5.0 PC-App/1.0",
        "X-Requested-With": "XMLHttpRequest",
    })
    deadline = time.monotonic() + timeout
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return _read_body(resp, deadline)


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
# The {0,400} on the title, rather than a bare `.*?`, is the same
# unclosed-tag guard _candidate_urls documents at length: a lazy `.*?`
# looking for a `</a>` that isn't there re-scans to the end of the
# fragment one character at a time, per <h3>. A card title is a handful
# of words, so capping the span costs nothing real and stops a card that
# arrived truncated from turning into a quadratic scan.
_A4U_TITLE_LINK_RE = re.compile(
    r'<h3[^>]*>\s*<a[^>]+href=["\'](?P<url>[^"\']+)["\'][^>]*>(?P<title>.{0,400}?)</a>',
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

# Deliberately the *opening tag only*, with the anchor's inner text cut
# out by hand below rather than by a `(.*?)</a>` in the pattern.
#
# That pattern is what froze the whole app. A lazy `.*?` under DOTALL,
# looking for a `</a>` that never comes, walks to the end of the document
# one character at a time before giving up - and it does that once per
# unclosed `<a`, so the cost is quadratic in page size. Measured: 20,000
# unclosed anchors in a 0.7MB body took 32.4s inside a single
# `finditer` call. `re` does not release the GIL, so that is not a slow
# background lookup - it is every Python thread in the process stopped
# dead, the Qt event loop included. Measured in a real window: 15
# heartbeats fired where 649 were due, and a Settings click made during
# the scan sat undelivered for 29.5s, which is the "pressing Settings
# hangs and crashes it" the user reported (Windows offers to kill a
# window that stops answering for ~30s).
#
# Matching just `<a ...>` cannot backtrack past the tag it is in, so the
# scan is linear whatever the markup does. An unclosed anchor now still
# yields its href, its title/alt and its slug - strictly more than the
# old pattern, which threw the whole anchor away.
_ANCHOR_OPEN_RE = re.compile(r'<a\b(?P<attrs>[^>]*)>', re.IGNORECASE)
_HREF_RE = re.compile(r'href\s*=\s*["\'](?P<url>[^"\']+)["\']', re.IGNORECASE)

# How far past an opening tag to look for its `</a>`. A card's anchor can
# legitimately wrap a poster, a genre list and a whole synopsis (animhq
# does), so this is well clear of any real one; past it the inner text is
# a page's worth of markup that could hold no title anyway, since a
# candidate has to come out under _MAX_TITLE_LEN.
_MAX_ANCHOR_INNER = 4000

# Anchors examined per page. A real search results page has tens of
# links, not thousands; this only bites on a page pathological enough
# that scanning all of it would be the freeze this module just fixed.
_MAX_ANCHORS = 4000
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
    for seen, anchor in enumerate(_ANCHOR_OPEN_RE.finditer(body)):
        if seen >= _MAX_ANCHORS:
            break
        href = _HREF_RE.search(anchor.group("attrs"))
        if not href:
            continue
        # The anchor's inner text, found with str.find over a bounded
        # window instead of by the regex - see _ANCHOR_OPEN_RE for why
        # the regex must not be the thing looking for `</a>`. An anchor
        # that is unclosed, or longer than any real card, contributes no
        # inner text and is scored on its attributes and slug alone.
        inner_start = anchor.end()
        close = body.find("</a>", inner_start, inner_start + _MAX_ANCHOR_INNER)
        inner = body[inner_start:close] if close != -1 else ""
        after = close + 4 if close != -1 else inner_start
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

        texts = [_text(inner)]
        texts += _ATTR_TEXT_RE.findall(anchor.group("attrs"))
        texts += _ATTR_TEXT_RE.findall(inner)
        # Headings after the link, stopping at the next link so one
        # card's title can't be read as its neighbour's.
        tail = body[after:after + _NEARBY_WINDOW].split("<a ")[0]
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


# ---- Crunchyroll -----------------------------------------------------
# The one site that can never be scraped (module docstring) and so the
# one site resolved from somewhere other than itself.

_CRUNCHYROLL_ORIGIN = "https://www.crunchyroll.com"

# A locale segment is assigned per visitor by geo-IP, so it says where
# *this machine* asked from, not anything about the title. Stripped only
# when a real content segment follows it, so a title legitimately slugged
# with two letters isn't beheaded.
_CR_LOCALE_RE = re.compile(r'^/[a-z]{2}(?:-[a-z]{2})?/(?=series/|watch/)', re.IGNORECASE)

# Crunchyroll lists some shows twice, subbed and dubbed, and AniList
# records both rows against the one title ("attack-on-titan" alongside
# "attack-on-titan-dubs" - both observed on Shingeki no Kyojin). They
# happen to redirect to the same series page today, but the subbed row is
# the neutral default, so it is preferred rather than left to row order.
_CR_DUB_RE = re.compile(r'-(?:dub|dubs|dubbed)(?:/|$)', re.IGNORECASE)


def _is_crunchyroll(base_url: str) -> bool:
    host = _host_key(urllib.parse.urlsplit(base_url).netloc)
    return host == "crunchyroll.com" or host.endswith(".crunchyroll.com")


class _StopAtRedirect(urllib.request.HTTPRedirectHandler):
    """Records the first Location and declines to follow it - returning
    None from redirect_request is urllib's documented way to stop a
    chain, and the 3xx then falls through to the default handler, which
    raises. The caller expects that and reads `location` regardless."""

    location = None

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if self.location is None:
            self.location = newurl
        return None


def _crunchyroll_normalize(url: str):
    """One canonical shape out of the several AniList holds. Rows added
    over the site's lifetime carry plain `http://`, the retired
    `beta.crunchyroll.com` host, and www-less forms - all still reach the
    site, but each costs an extra redirect, and a saved link shouldn't
    depend on those staying in place. Query strings and fragments go too:
    on these rows they are campaign tracking, never part of the page's
    identity.

    Anything not on crunchyroll.com returns None. AniList's link rows are
    user-submitted, so a mislabelled one is possible, and saving some
    other site's URL onto a Crunchyroll entry would be worse than saving
    nothing."""
    parts = urllib.parse.urlsplit((url or "").strip())
    host = _host_key(parts.netloc)
    if host != "crunchyroll.com" and not host.endswith(".crunchyroll.com"):
        return None
    path = parts.path if parts.path.startswith("/") else "/" + parts.path
    path = _CR_LOCALE_RE.sub("/", path).rstrip("/")
    return _CRUNCHYROLL_ORIGIN + path if path else None


def _crunchyroll_canonical(url: str, timeout: int) -> str:
    """A bare-slug link upgraded to the real /series/<id>/<slug> page.
    AniList holds both shapes - rows written recently carry the full
    series path, older ones only "/one-piece" - and the bare slug does
    still work, so this is best-effort: one 301, and on any failure the
    slug URL comes back unchanged rather than the link being lost.

    Exactly one hop, and only to a Location that is itself a /series/
    path. Following the chain to the end instead lands on
    /ar/series/... - measured, from this machine - because Crunchyroll
    appends the visitor's geo-guessed locale on the *second* hop, and
    that segment must not be what gets saved onto the entry."""
    if urllib.parse.urlsplit(url).path.startswith("/series/"):
        return url
    handler = _StopAtRedirect()
    try:
        request = urllib.request.Request(url, headers={
            "Accept": "text/html, */*",
            "User-Agent": "Mozilla/5.0 PC-App/1.0",
        })
        try:
            urllib.request.build_opener(handler).open(request, timeout=timeout).close()
        except Exception:
            pass  # the declined 3xx raises; the Location was captured first
        upgraded = _crunchyroll_normalize(handler.location or "")
    except Exception:
        return url
    if upgraded and urllib.parse.urlsplit(upgraded).path.startswith("/series/"):
        return upgraded
    return url


# ---- Streaming services resolved through AniList ---------------------
# Crunchyroll was the first, and the reasoning in the module docstring
# applies unchanged to any service built the same way: a search page that
# renders client-side, and a content API that wants a logged-in session.
# Netflix is the second - its /search is behind a sign-in, so nothing on
# it can be read either, while AniList records a plain /title/<id> link
# for anime it carries. Adding another such service is a row here, not
# new code.
#
# Netflix's country segment (netflix.com/gb/title/...) is the same kind
# of per-visitor noise as Crunchyroll's locale: it says where the link
# was written, not anything about the title, and www.netflix.com resolves
# it per viewer anyway.
_NETFLIX_LOCALE_RE = re.compile(r'^/[a-z]{2}(?:-[a-z]{2})?/(?=title/|watch/)',
                                re.IGNORECASE)

_STREAMING_SITES = (
    # host suffix, AniList's `site` label, canonical origin, locale
    # segment to strip, rows to deprioritize, extra canonicalizer
    ("crunchyroll.com", "crunchyroll", _CRUNCHYROLL_ORIGIN, _CR_LOCALE_RE,
     _CR_DUB_RE, lambda url, timeout: _crunchyroll_canonical(url, timeout)),
    ("netflix.com", "netflix", "https://www.netflix.com", _NETFLIX_LOCALE_RE,
     None, None),
)


def _streaming_site_for(base_url: str):
    """The _STREAMING_SITES row matching this site, or None for an
    ordinary site that can just be searched."""
    host = _host_key(urllib.parse.urlsplit(base_url or "").netloc)
    for row in _STREAMING_SITES:
        suffix = row[0]
        if host == suffix or host.endswith("." + suffix):
            return row
    return None


def _streaming_normalize(url: str, suffix: str, origin: str, locale_re):
    """One canonical shape out of the several AniList holds. Rows added
    over a site's lifetime carry plain `http://`, retired hosts (Crunchy-
    roll's `beta.`), and www-less forms - all still reach the site, but
    each costs an extra redirect, and a saved link shouldn't depend on
    those staying in place. Query strings and fragments go too: on these
    rows they are campaign tracking, never part of the page's identity.

    Anything off the expected host returns None. AniList's link rows are
    user-submitted, so a mislabelled one is possible, and saving some
    other site's URL onto this entry would be worse than saving
    nothing."""
    parts = urllib.parse.urlsplit((url or "").strip())
    host = _host_key(parts.netloc)
    if host != suffix and not host.endswith("." + suffix):
        return None
    path = parts.path if parts.path.startswith("/") else "/" + parts.path
    if locale_re is not None:
        path = locale_re.sub("/", path)
    path = path.rstrip("/")
    return origin + path if path else None


def _netflix_available(url: str, timeout: int):
    """True/False for whether Netflix serves this title page here, or
    None when that couldn't be determined.

    Netflix ids are global but its catalogue is regional, so a perfectly
    correct id 404s from a country the title isn't licensed in -
    measured on Frieren, whose id (81726714) is the right one and still
    404s from here. Saving that would pin the entry to a dead page
    forever, which is worse than the search page it would otherwise
    fall back to.

    The body is deliberately never read: Netflix truncates these
    responses (a plain read raises IncompleteRead on pages that are
    perfectly fine), and the status line is the whole question."""
    try:
        request = urllib.request.Request(url, headers={
            "Accept": "text/html,*/*",
            "User-Agent": "Mozilla/5.0 PC-App/1.0",
        })
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status == 200
    except urllib.error.HTTPError as exc:
        return False if exc.code == 404 else None
    except Exception:
        return None


def _netflix_page_url(title: str, timeout: int):
    """Netflix's own page for `title`, from Wikidata's published id.

    Asked before AniList, not instead of it. AniList does carry Netflix
    rows, but it answers 403 to an ordinary connection once it decides
    the connection has asked too often - for hours, and indistinguishably
    from "no link exists", because every lookup here fails soft. Wikidata
    answered for every title tried and is not the same dependency as the
    rest of the app's anime metadata, so a bad afternoon on one doesn't
    take the other down with it.

    A confirmed 404 is treated as no answer. Anything else - including a
    failed check - keeps the link: the id came from a source that says
    the title is on Netflix, so a transient network error is not reason
    enough to throw it away."""
    for variant in _query_variants(title):
        netflix_id = wikidata.fetch_netflix_id(variant, timeout)
        if not netflix_id:
            continue
        url = wikidata.page_url(netflix_id)
        if _netflix_available(url, timeout) is False:
            return None
        return url
    return None


def _streaming_page_url(row, title: str, timeout: int):
    """`title`'s own page on one of the _STREAMING_SITES, via whatever
    third party publishes the link, or None so the caller falls back to
    that site's search page. The query variants are the same ones the
    engines get, tried in the same order and only while the previous
    came back empty, so the ordinary case is one request."""
    suffix, keyword, origin, locale_re, deprioritize_re, canonical = row
    if keyword == "netflix":
        found = _netflix_page_url(title, timeout)
        if found:
            return found
        # else fall through to AniList, which has its own Netflix rows
    for variant in _query_variants(title):
        try:
            urls = anilist.fetch_external_urls(variant, keyword, timeout)
        except Exception:
            continue
        normalized = [u for u in
                      (_streaming_normalize(u, suffix, origin, locale_re) for u in urls) if u]
        if not normalized:
            continue
        if deprioritize_re is not None:
            # Stable, so among equally-preferred rows AniList's own order holds.
            normalized.sort(key=lambda u: bool(deprioritize_re.search(u)))
        return canonical(normalized[0], timeout) if canonical else normalized[0]
    return None


def _crunchyroll_page_url(title: str, timeout: int):
    """Kept as the name the Crunchyroll path has always had; the work is
    the shared one now that a second service resolves the same way."""
    return _streaming_page_url(_STREAMING_SITES[0], title, timeout)


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
    always better evidence than links read off a page.

    The _STREAMING_SITES take neither path. Nothing on their search
    pages can be read at all (module docstring), so scraping one would
    only ever burn two requests to return None; AniList's record of the
    link is the whole answer there."""
    base_url = _to_base_url(site or {})
    streaming = _streaming_site_for(base_url)
    if streaming is not None:
        return _streaming_page_url(streaming, title, timeout)

    variants = _query_variants(title)
    for variant in variants:
        results = search_site(site, variant, timeout)
        if not results:
            continue
        match = _best_match(variant, results)
        if match:
            return match["url"]

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
