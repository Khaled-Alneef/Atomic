"""Sidebar nav definitions shared by main.py (builds the sidebar) and
windows/home.py (orders its preview sections to match it) - kept out of
main.py itself to avoid a circular import between the two.

Every entry shows the same bullet marker (theme.NAV_BULLET) instead of
a different emoji per section, so these tuples don't carry a per-item
icon - just (name, page_name)."""

from . import app_settings

HOME_ITEM = ("Home", "home")

# **The sidebar in three blocks, with a row of air between them** (the
# owner's ask, 22 August 2026: Home / gap / Watch, Read, Games / gap /
# Apps, Websites). Home is pinned above on its own (see main.py's
# home_list); these are the two blocks below it.
#
# The split is what the two halves *are*, not decoration: the first
# block is the libraries this app keeps - things with progress, a
# schedule and a history - and the second is the two launcher grids,
# which keep nothing and only open something else. A gap says that
# faster than a heading would, and it survives the sidebar folding to
# a 68px rail where a heading could not.
#
# Drag-to-reorder still works *inside* a block (see main._on_nav_reordered)
# and no longer across them, which is the whole point of grouping them.
# **One rail, and the sections are on it** - the owner's ask, 25 August
# 2026: *"cancel the second sidebar for the watch and read, make the
# main sidebar: Home / one icon empty space / discover (for all,
# watch/read) / movies / series / anime / manga / manhwa / manhua / one
# icon empty space / games / apps / webs"*.
#
# A row is a **route**, `page` or `page:section`. The six type rows are
# the catalogue browse the section rail used to hold, so Movies is the
# Watch page showing its Movies section and Manga is the Read page
# showing its Manga one; `discover` is a page of its own that browses
# both media at once, which is what "for all, watch/read" asks for.
#
# Saved, Schedule and History are not rows here and are not gone: they
# are the header tabs on whichever of the two pages is showing (see
# tracker.TrackerPage.HEADER_TABS). They are one thing per page rather
# than six rows repeated twice, which is what took them off the rail.
NAV_GROUPS = [
    [
        ("Discover", "discover"),
        ("Movies", "series:cat_movies"),
        ("Series", "series:cat_series"),
        ("Anime", "series:cat_anime"),
        ("Manga", "manga:cat_manga"),
        ("Manhwa", "manga:cat_manhwa"),
        ("Manhua", "manga:cat_manhua"),
    ],
    [
        ("Games", "games"),
        ("Apps", "apps"),
        ("Websites", "websites"),
    ],
]


def route_page(route: str) -> str:
    """The page half of a route - "series" out of "series:cat_anime"."""
    return str(route or "").split(":", 1)[0]


def route_section(route: str) -> str:
    """The section half, or "" when the route names a whole page."""
    parts = str(route or "").split(":", 1)
    return parts[1] if len(parts) == 2 else ""


# Flattened, for everything that only cares about the order - Home's
# preview rows, nav_position, the saved drag order.
NAV_ITEMS = [item for group in NAV_GROUPS for item in group]


def ordered_nav_items():
    """NAV_ITEMS arranged per the user's saved drag order, with any page
    not yet in that order (e.g. added in a later version) tacked onto
    the end."""
    order = app_settings.get_nav_order()
    by_page = {item[1]: item for item in NAV_ITEMS}
    # **A saved order from before the rows were routes is not an order
    # any more.** It named whole pages - games, websites, apps, series,
    # manga - and none of those is a row now except the three launchers,
    # so applying it put Games, Websites and Apps at the *top* of the
    # rail and left every new row appended after them (measured on the
    # owner's own settings.json). An order that knows nothing of the row
    # this layout is built around is stale by definition, so it is
    # dropped rather than half-applied; the first drag writes a fresh
    # one.
    if "discover" not in order:
        order = []
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


def visible_nav_groups():
    """visible_nav_items(), split back into NAV_GROUPS' blocks.

    The saved drag order is one flat list (it was written before the
    blocks existed and is still what Home reads), so a block's rows are
    that list *filtered* to the block - which is exactly the behaviour
    wanted: dragging inside a block reorders it, and nothing can move a
    row from one block to the other.

    A block left with nothing in it - every one of its pages hidden in
    Settings - comes back empty rather than dropped, and main.py hides
    both the list and its gap so the rail does not gain a stray hole."""
    ordered = visible_nav_items()
    return [[item for item in ordered if item[1] in {p for _n, p in group}]
            for group in NAV_GROUPS]


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
    routes = [item[1] for item in ordered_nav_items()]
    if page_name in routes:
        return routes.index(page_name)
    # A bare page name - "series", "manga" - now that the rail holds
    # routes. Its first section's row is where that page lives, which is
    # what a slide direction and Home's row order both want.
    for index, route in enumerate(routes):
        if route_page(route) == page_name:
            return index
    return len(routes)
