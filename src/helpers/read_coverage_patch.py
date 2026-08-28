"""Fill incomplete reader chapter lists from Atomic's other read sources.

The reader already knows how to search every configured manga site and MangaDex,
but list_chapters only used those fallbacks when the primary site returned zero
chapters. A primary site returning a paginated/recent-only slice therefore looked
successful and prevented the missing chapters from ever being discovered.
"""

from __future__ import annotations

_INSTALLED = False


def _looks_incomplete(chapters):
    numbers = []
    for chapter in chapters or []:
        value = chapter.get("number") if isinstance(chapter, dict) else None
        if isinstance(value, (int, float)) and float(value).is_integer():
            numbers.append(int(value))
    if len(numbers) < 2:
        return False
    unique = sorted(set(numbers))
    # The common failure is a site's newest-page slice: e.g. chapters 209-248.
    # A real complete numbered run normally reaches chapter 1. Also catch a
    # substantial hole inside an otherwise long list.
    if unique[0] > 1:
        return True
    expected = unique[-1] - unique[0] + 1
    missing = expected - len(unique)
    return expected >= 20 and missing >= max(4, expected // 10)


def install():
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    from . import chapter_source, net, updater

    original_list_chapters = chapter_source.list_chapters

    def list_chapters_complete(entry, *, deadline=None, refresh=False,
                               on_partial=None):
        # Keep one budget for the whole operation, exactly as the original does,
        # so enrichment cannot turn several source fallbacks into unbounded waits.
        if deadline is None:
            deadline = net.deadline_in(45)
        chapters = original_list_chapters(
            entry, deadline=deadline, refresh=refresh, on_partial=on_partial)
        if not chapters or not _looks_incomplete(chapters):
            return chapters

        merged = list(chapters)
        try:
            other = chapter_source._other_site_chapters(entry, deadline)
        except Exception:
            other = []
        if other:
            merged = chapter_source._merged(merged, other)

        # MangaDex is thin for Arabic on some titles, but when it does have the
        # missing range those chapters are useful and should not be suppressed
        # merely because the primary Arabic site returned a partial slice.
        if _looks_incomplete(merged):
            try:
                md = chapter_source._mangadex_chapters(entry, deadline)
            except Exception:
                md = []
            if md:
                merged = chapter_source._merged(merged, md)

        if len(merged) > len(chapters):
            try:
                key = chapter_source._cache_key(entry)
                if key:
                    chapter_source._store(key, merged)
            except Exception:
                pass
        return merged

    chapter_source.list_chapters = list_chapters_complete

    updater.APP_VERSION = "1.10.118"
    try:
        updater._HEADERS["User-Agent"] = f"Atomic/{updater.APP_VERSION}"
    except Exception:
        pass
