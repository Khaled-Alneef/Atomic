"""Sidebar nav definitions shared by main.py (builds the sidebar) and
windows/home.py (orders its preview sections to match it) - kept out of
main.py itself to avoid a circular import between the two.

Every entry shows the same bullet marker (theme.NAV_BULLET) instead of
a different emoji per section, so these tuples don't carry a per-item
icon - just (name, page_name)."""

from . import app_settings

HOME_ITEM = ("Home", "home")

# Default order; the sidebar's drag-to-reorder list overrides this once
# the user has customized it (see app_settings.get/set_nav_order).
NAV_ITEMS = [
    # "Read" and "Watch" (the owner's ask), not "Reading" and "Movies &
    # Series" - the verbs; Home's rows keep the longer "Reading"/
    # "Watching" headings, also the owner's ask.
    ("Read", "manga"),
    # Page key stays "series" - it is what saved nav orders, hidden-section
    # lists and series.json already refer to. Anime merged into this page
    # (the owner's ask - one watch page under the camera glyph), so there
    # is no "anime" nav entry any more; a saved order or hidden-sections
    # list still naming it is simply filtered out by the by_page lookups
    # below rather than migrated.
    ("Watch", "series"),
    ("Games", "games"),
    ("Apps", "apps"),
    ("Websites", "websites"),
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
    """ordered_nav_items() minus whatever the user has hidden in
    Settings > General (see app_settings.get/set_hidden_sections) -
    hidden sections keep their saved data, they just don't get a sidebar
    entry until toggled back on."""
    hidden = set(app_settings.get_hidden_sections())
    return [item for item in ordered_nav_items() if item[1] not in hidden]


def home_hidden_sections() -> set:
    """Page-names Home should leave out entirely - its carousel slides,
    preview rows and quick lists alike.

    Empty unless the user has ticked "hide them from the Home page too"
    in Settings, since hiding a section has always meant only dropping
    its sidebar entry; Home kept previewing it either way."""
    if not app_settings.get_hide_sections_from_home():
        return set()
    return set(app_settings.get_hidden_sections())


def nav_position(page_name: str) -> int:
    """Index of `page_name` in the current nav order, used to figure out
    which way a page transition should slide. Home is pinned above the
    drag-to-reorder list, so it's always position -1; anything unknown
    sorts last."""
    if page_name == HOME_ITEM[1]:
        return -1
    pages = [item[1] for item in ordered_nav_items()]
    return pages.index(page_name) if page_name in pages else len(pages)
