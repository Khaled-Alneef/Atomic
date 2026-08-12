"""Sidebar nav definitions shared by main.py (builds the sidebar) and
windows/home.py (orders its preview sections to match it) - kept out of
main.py itself to avoid a circular import between the two."""

import app_settings

HOME_ITEM = ("Home", "home", "\U0001F3E0")

# Default order; the sidebar's drag-to-reorder list overrides this once
# the user has customized it (see app_settings.get/set_nav_order).
NAV_ITEMS = [
    ("Anime", "anime", "\U0001F38C"),
    ("Reading", "manga", "\U0001F4D6"),
    ("Series", "series", "\U0001F3AC"),
    ("Games", "games", "\U0001F3AE"),
    ("Apps", "apps", "\U0001F4BB"),
    ("Websites", "websites", "\U0001F310"),
]


def ordered_nav_items():
    """NAV_ITEMS arranged per the user's saved drag order, with any page
    not yet in that order (e.g. added in a later version) tacked onto
    the end."""
    order = app_settings.get_nav_order()
    by_page = {item[1]: item for item in NAV_ITEMS}
    ordered = [by_page[p] for p in order if p in by_page]
    ordered += [item for item in NAV_ITEMS if item[1] not in order]
    return ordered


def nav_position(page_name: str) -> int:
    """Index of `page_name` in the current nav order, used to figure out
    which way a page transition should slide. Home is pinned above the
    drag-to-reorder list, so it's always position -1; anything unknown
    sorts last."""
    if page_name == HOME_ITEM[1]:
        return -1
    pages = [item[1] for item in ordered_nav_items()]
    return pages.index(page_name) if page_name in pages else len(pages)
