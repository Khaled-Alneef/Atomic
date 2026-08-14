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

from . import storage

SITES_FILE = "manga_sites.json"

DEFAULT_SITES = [
    {"name": "3asq", "base_url": "https://3asq.online/"},
    {"name": "TeamX", "base_url": "https://www.olympustaff.com/"},
    {"name": "Lava Scans", "base_url": "https://lavascans.com/"},
    {"name": "SWAT", "base_url": "https://meshmanga.com/"},
]


def _normalize(base_url: str) -> str:
    base_url = (base_url or "").strip()
    if base_url and not base_url.endswith("/"):
        base_url += "/"
    return base_url


def _load():
    sites = storage.load(SITES_FILE, None)
    if sites is None:
        # First run: seed the well-known reading sites so Manga suggestions
        # work out of the box. Once the file exists, an empty list (the
        # user removed every site) is respected as-is - never re-seeded.
        sites = [{"id": str(uuid.uuid4()), "name": s["name"], "base_url": s["base_url"]}
                  for s in DEFAULT_SITES]
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
_MADARA_COVER_RE = re.compile(
    r'class=["\']summary_image["\'][^>]*>.*?<img[^>]+src=["\']([^"\']+)["\']',
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
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _get(url: str, timeout: int, extra_headers: dict = None) -> str:
    req = urllib.request.Request(url, headers={
        "Accept": "application/json, text/html, */*",
        "User-Agent": "Mozilla/5.0 PC-App/1.0",
        **(extra_headers or {}),
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8")


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


_AJAX_SEARCH_CARD_RE = re.compile(r'<a\s+href="(?P<url>[^"]+)"[^>]*>(?P<body>.*?)</a>', re.DOTALL)
_AJAX_SEARCH_IMG_RE = re.compile(r'<img[^>]*src="(?P<img>[^"]+)"[^>]*alt="(?P<title>[^"]+)"')
_AJAX_SEARCH_CHAPTER_RE = re.compile(r'([0-9]+(?:\.[0-9]+)?)\s*(?:فصل|chapters?)', re.IGNORECASE)

# TeamX's search cards link a tiny (~100x130) "thumbnail_<name>" crop -
# the plain "<name>" at the same path is the real cover, 5-10x the pixel
# dimensions (verified against several live results).
_TEAMX_THUMBNAIL_PREFIX_RE = re.compile(r'/thumbnail_(?=[^/]+$)')


def _search_ajax_html(base_url: str, query: str, timeout: int) -> list:
    """A plain GET endpoint (found on TeamX/olympustaff.com) that returns
    an HTML fragment of result cards rather than JSON - scrape each
    card's anchor/title/cover/chapter-count with regexes instead of
    pulling in an HTML parser dependency for one site shape."""
    body = _get(f"{base_url}ajax/search?keyword={urllib.parse.quote(query)}", timeout,
                extra_headers={"X-Requested-With": "XMLHttpRequest"})
    results = []
    for card in _AJAX_SEARCH_CARD_RE.finditer(body):
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


def search_site(site: dict, query: str, timeout: int = 6) -> list:
    """Try each known engine against one site, stopping at the first that
    returns anything. Returns [] if the site matches no known shape, has
    no matches, or is unreachable."""
    query = (query or "").strip()
    base_url = site.get("base_url")
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
