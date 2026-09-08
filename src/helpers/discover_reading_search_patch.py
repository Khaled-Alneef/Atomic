"""Make Discover reading search keep configured providers as separate cards.

Two configured sites can legitimately carry the same title. They are separate
reading sources and therefore separate cards; only the MangaDex catalogue
fallback is suppressed when a configured-site card for that title already
exists.
"""

from __future__ import annotations

import importlib.abc
import importlib.machinery
import sys
from urllib.parse import urlparse

_TARGET = "windows.tracker"
_INSTALLED = False
_PATCHED = False


def _provider_key(row):
    site = str(row.get("site_id") or row.get("site_name") or "").strip().casefold()
    url = str(row.get("url") or "").strip()
    try:
        host = (urlparse(url).hostname or "").casefold()
    except Exception:
        host = ""
    return site or host or url.casefold()


def _merge_reading_results(module, query, limit):
    site_rows = []
    catalogue_rows = []
    try:
        site_rows = module.discover.discover_reading_sites(
            query=query, limit=limit
        ) or []
    except Exception:
        site_rows = []
    try:
        catalogue_rows = module.discover.discover_reading(
            query=query, limit=limit
        ) or []
    except Exception:
        catalogue_rows = []

    merged = []
    seen_site = set()
    site_titles = set()

    # Configured sites are distinct sources. Deduplicate only an accidental
    # repeat from the *same provider*, never two sites that carry the same title.
    for original in site_rows:
        if not isinstance(original, dict):
            continue
        row = dict(original)
        title = str(row.get("title") or "").strip()
        if not title:
            continue
        title_key = title.casefold()
        key = (title_key, _provider_key(row))
        if key in seen_site:
            continue
        seen_site.add(key)
        site_titles.add(title_key)
        row["_atomic_reading_source_priority"] = 0
        merged.append(row)

    # MangaDex is a catalogue fallback, not a third provider card beside the
    # user's configured site results. Keep one catalogue row only when no
    # configured provider already answered for that title.
    seen_catalogue = set()
    for original in catalogue_rows:
        if not isinstance(original, dict):
            continue
        row = dict(original)
        title = str(row.get("title") or "").strip()
        if not title:
            continue
        title_key = title.casefold()
        if title_key in site_titles or title_key in seen_catalogue:
            continue
        seen_catalogue.add(title_key)
        row["_atomic_reading_source_priority"] = 1
        merged.append(row)

    try:
        wanted = module.title_match.normalize(query)

        def rank(row):
            raw = str(row.get("title") or "")
            name = module.title_match.normalize(raw)
            if name == wanted:
                match = 0
            elif name.startswith(wanted):
                match = 1
            elif wanted in name:
                match = 2
            else:
                match = 3
            return (
                match,
                int(row.get("_atomic_reading_source_priority") or 0),
                len(raw),
                raw.casefold(),
                str(row.get("site_name") or "").casefold(),
            )

        merged.sort(key=rank)
    except Exception:
        pass

    for row in merged:
        row.pop("_atomic_reading_source_priority", None)
    return merged[:limit]


def _patch(module):
    global _PATCHED
    if _PATCHED:
        return
    _PATCHED = True

    Page = module.DiscoverPage
    old_worker = Page._discover_row_worker

    def discover_row_worker(self, kind, query, run, already_drawn=False):
        query = str(query or "").strip()
        if kind != "reading" or not query:
            return old_worker(self, kind, query, run, already_drawn)

        key = (kind, query)
        module._load_discover_cache()
        stamp_and_rows = module._DISCOVER_CACHE.get(key)
        if stamp_and_rows is not None:
            stamp, cached_rows = stamp_and_rows
            if not already_drawn:
                self._discover_signals.results.emit(
                    kind, list(cached_rows), run
                )
            if module.time.monotonic() - stamp < module._DISCOVER_CACHE_TTL_S:
                return

        rows = _merge_reading_results(module, query, module.DISCOVER_LIMIT)
        if rows:
            module._DISCOVER_CACHE[key] = (
                module.time.monotonic(), list(rows)
            )
        elif stamp_and_rows is not None:
            return

        self._discover_signals.results.emit(kind, rows, run)

    Page._discover_row_worker = discover_row_worker


class _Loader(importlib.abc.Loader):
    def __init__(self, wrapped):
        self._wrapped = wrapped

    def create_module(self, spec):
        creator = getattr(self._wrapped, "create_module", None)
        return creator(spec) if creator is not None else None

    def exec_module(self, module):
        self._wrapped.exec_module(module)
        _patch(module)


class _Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname != _TARGET:
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec is None or spec.loader is None:
            return spec
        spec.loader = _Loader(spec.loader)
        return spec


def install():
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    module = sys.modules.get(_TARGET)
    if module is not None:
        _patch(module)
    else:
        sys.meta_path.insert(0, _Finder())
