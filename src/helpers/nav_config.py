"""Sidebar nav definitions shared by main.py (builds the sidebar) and
windows/home.py (orders its preview sections to match it) - kept out of
main.py itself to avoid a circular import between the two."""

from . import app_settings

# One uniform marker used for every sidebar entry instead of a different
# emoji per section - modern flat-nav style, not a per-category pictogram.
NAV_ICON = "✦"  # ✦

HOME_ITEM = ("Home", "home", NAV_ICON)

# Default order; the sidebar's drag-to-reorder list overrides this once
# the user has customized it (see app_settings.get/set_nav_order).
NAV_ITEMS = [
    ("Anime", "anime", NAV_ICON),
    ("Reading", "manga", NAV_ICON),
    ("Series", "series", NAV_ICON),
    ("Games", "games", NAV_ICON),
    ("Apps", "apps", NAV_ICON),
    ("Websites", "websites", NAV_ICON),
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


def visible_nav_items():
    """ordered_nav_items() minus whatever the user has hidden from the
    main sidebar in Settings > General (see app_settings.get/set_hidden_
    sections) - hidden sections keep their saved data, they just don't
    get a sidebar entry until toggled back on."""
    hidden = set(app_settings.get_hidden_sections())
    return [item for item in ordered_nav_items() if item[1] not in hidden]


def nav_position(page_name: str) -> int:
    """Index of `page_name` in the current nav order, used to figure out
    which way a page transition should slide. Home is pinned above the
    drag-to-reorder list, so it's always position -1; anything unknown
    sorts last."""
    if page_name == HOME_ITEM[1]:
        return -1
    pages = [item[1] for item in ordered_nav_items()]
    return pages.index(page_name) if page_name in pages else len(pages)
