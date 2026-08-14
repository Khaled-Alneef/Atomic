"""Anime video-site directory (Settings-managed), mirroring manga_sites.py.

Anime suggestions/covers always come from Stremio's Cinemeta (the only
public, unauthenticated search API in play here) - that part never
changes. What *does* change per entry is where double-click actually
opens: the built-in Stremio deep link, or one of these user-managed
sites instead. Unlike manga_sites.py there's no live per-site search
here (no confirmed common theme/API shape across anime streaming sites
the way manga aggregators share WordPress themes) - a site just needs a
search-URL prefix, and opening an entry appends the title to it.
"""

import urllib.parse
import uuid

from . import storage

SITES_FILE = "anime_sites.json"

# Crunchyroll's own search/content API sits behind an OAuth exchange and
# Cloudflare bot-management this app won't try to bypass - so it isn't a
# search/metadata source, just a search-results page like any other site.
DEFAULT_SITES = [
    {"name": "Crunchyroll", "search_url": "https://www.crunchyroll.com/search?q="},
]


def _normalize(search_url: str) -> str:
    return (search_url or "").strip()


def _load():
    sites = storage.load(SITES_FILE, None)
    if sites is None:
        sites = [{"id": str(uuid.uuid4()), "name": s["name"], "search_url": s["search_url"]}
                  for s in DEFAULT_SITES]
        storage.save(SITES_FILE, sites)
    return sites


def list_sites() -> list:
    return _load()


def get_site(site_id: str):
    return next((s for s in _load() if s["id"] == site_id), None)


def add_site(name: str, search_url: str) -> dict:
    sites = _load()
    site = {"id": str(uuid.uuid4()), "name": name.strip(), "search_url": _normalize(search_url)}
    sites.append(site)
    storage.save(SITES_FILE, sites)
    return site


def update_site(site_id: str, name: str, search_url: str):
    sites = _load()
    for s in sites:
        if s["id"] == site_id:
            s["name"] = name.strip()
            s["search_url"] = _normalize(search_url)
    storage.save(SITES_FILE, sites)


def remove_site(site_id: str):
    sites = [s for s in _load() if s["id"] != site_id]
    storage.save(SITES_FILE, sites)


def search_page_url(search_url: str, query: str) -> str:
    """The configured search-URL prefix with the title appended - e.g.
    "https://www.crunchyroll.com/search?q=" + "one piece"."""
    return f"{search_url}{urllib.parse.quote(query)}"
