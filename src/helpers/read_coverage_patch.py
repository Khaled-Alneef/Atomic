"""Keep chapter providers isolated instead of merging them into one title.

A reading result already carries the site URL it came from. The former coverage
patch noticed an incomplete list and appended chapters from Atomic's *other*
reading sites. Different providers use different chapter IDs/URLs, so the same
Chapter 560 survived that merge twice and appeared as two rows in one card.

Provider fallback still belongs to chapter_source itself: if the selected site's
list is empty it can try another source. What is removed here is only the
cross-provider enrichment of a list that already succeeded.
"""

from __future__ import annotations

from urllib.parse import urlparse

_INSTALLED = False


def _host(value):
    try:
        text = str(value or "").strip()
        if not text:
            return ""
        parsed = urlparse(text)
        host = (parsed.hostname or "").lower()
        return host[4:] if host.startswith("www.") else host
    except Exception:
        return ""


def _chapter_provider(chapter):
    if not isinstance(chapter, dict):
        return ""
    return _host(chapter.get("url") or chapter.get("id"))


def _isolate(entry, chapters):
    rows = list(chapters or [])
    providers = []
    for row in rows:
        provider = _chapter_provider(row)
        if provider and provider not in providers:
            providers.append(provider)
    if len(providers) <= 1:
        return rows

    wanted = _host((entry or {}).get("url"))
    if wanted not in providers:
        # The first row comes from the primary source in chapter_source's
        # newest-first result. Unknown-provider rows stay with it because some
        # parsers use opaque chapter IDs while still belonging to that site.
        wanted = providers[0]
    return [row for row in rows
            if not _chapter_provider(row) or _chapter_provider(row) == wanted]


def install():
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    from . import chapter_source, updater

    original_list = chapter_source.list_chapters
    original_cached = chapter_source.cached_chapters

    def provider_list(entry, *, deadline=None, refresh=False, on_partial=None):
        chapters = original_list(
            entry, deadline=deadline, refresh=refresh, on_partial=on_partial)
        return _isolate(entry, chapters)

    def provider_cached(entry):
        # Old merged cache entries can survive a code update for hours. Filter
        # those too so the duplicate rows disappear immediately after upgrade.
        return _isolate(entry, original_cached(entry))

    def provider_stale(entry):
        # The details page's any-age read of the same store; isolated for
        # the same reason as the fresh one.
        return _isolate(entry, original_stale(entry))

    chapter_source.list_chapters = provider_list
    chapter_source.cached_chapters = provider_cached
    original_stale = getattr(chapter_source, "stale_chapters", None)
    if original_stale is not None:
        chapter_source.stale_chapters = provider_stale

    updater.APP_VERSION = "1.10.142"
    try:
        updater._HEADERS["User-Agent"] = f"Atomic/{updater.APP_VERSION}"
    except Exception:
        pass
