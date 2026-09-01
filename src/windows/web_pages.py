"""Home and Discover, rendered and scrolled by Edge rather than by Qt.

**The owner's instruction, 31 August 2026:** these two pages scroll
without Qt, and everything else stays exactly as it is. Two entries in
main.PAGES point here; the sidebar, tracker, details, player, games,
settings and every dialog are untouched.

**Why the white block happened**, measured 1 September 2026 after three
wrong guesses at it. This app rebuilds a page from scratch on every
visit, and opening the details page rebuilds the one behind it too - the
Home page object changed identity across a single card click
(0x1a665a5fb10 before, 0x1a605f85db0 after). So a *second* WebView2 was
being built on top of the first, and a WebView2 with nothing loaded
paints white. It was never a z-order or a repaint problem, which is what
the first three attempts addressed.

The fix is in helpers/webview2_host: a view keeps its window hidden until
its page has actually loaded, so a freshly built one shows nothing at all
rather than a white rectangle. Sharing one view between page instances
was tried first and is *not* here - re-parenting the native child between
rebuilt pages killed the process outright (exit 127, no traceback), and a
crash is worse than anything it was fixing.

The pages themselves are in src/web, served over http://.
"""

import os

from PyQt6.QtCore import QEvent, QTimer
from PyQt6.QtWidgets import QWidget

from helpers import logs, storage, webview2_host
from helpers.widgets import GlassPage
from web import server as web_server

# One server for the whole app, started on first use and left running.
# One per page visit would hand out a new port every time.
_server = None
_base = ""

# **How many overlays are open, for the whole app rather than for one
# page.** This is the crux of the white block, and why three fixes that
# suppressed "this page's view" all failed: clicking a card opens the
# details page *and rebuilds the page behind it*, so the instance that
# was suppressed is thrown away and a brand-new WebView2 is built - after
# the overlay was raised, therefore on top of it.
#
# A new view must know an overlay is up before it shows itself, and only
# something outside the page can tell it. Measured 1 September 2026: the
# Home page object changed identity across a single click
# (0x1a665a5fb10 -> 0x1a605f85db0).
_overlay_depth = 0
_live_pages = []


def overlay_opened(page):
    """Register an overlay so no web page paints over it."""
    global _overlay_depth
    _overlay_depth += 1
    _suppress_all(True)
    try:
        page.destroyed.connect(_overlay_gone)
    except Exception:
        _overlay_gone()          # nothing to wait for; do not get stuck


def _overlay_gone(*_args):
    global _overlay_depth
    _overlay_depth = max(0, _overlay_depth - 1)
    if _overlay_depth == 0:
        _suppress_all(False)


def overlay_open() -> bool:
    return _overlay_depth > 0


def _suppress_all(on):
    for page in list(_live_pages):
        try:
            page.view.suppress(on or not page.isVisible())
        except RuntimeError:
            _live_pages.remove(page)     # that page is gone


def base_url():
    global _server, _base
    if _server is None:
        _base, _server = web_server.start()
        logs.info(f"web pages serving at {_base}")
    return _base


def available() -> bool:
    # ATOMIC_WEB_PAGES=0 puts the Qt Home and Discover back without a
    # rebuild - the same insurance the web reader carries.
    if os.environ.get("ATOMIC_WEB_PAGES") == "0":
        return False
    return webview2_host.available()


class _WebPage(GlassPage):
    ROUTE = "home"
    # Laid out on every step of the sidebar fold, exactly like HomePage:
    # the view is a native child window that resizes with the widget, so
    # there is nothing to photograph and blit.
    FOLD_LIVE = True

    def __init__(self, app):
        super().__init__(parent=None)
        self.app = app
        # **Geometry, not a layout.** Measured 1 September 2026: on the
        # animated page swap the page itself came out (0, 0, 1714, 1001)
        # while the view inside it stayed 94x29 - main grabs a pixmap of
        # each page to slide (see navigate_to), and the layout on a page
        # built that way never activated. Home only worked because the
        # first page skips the animation entirely. The document was
        # complete the whole time; the window showing it was a postage
        # stamp, which is why the page read as blank.
        self.view = webview2_host.WebView2Page(
            url=f"{base_url()}?embed=1#{self.ROUTE}", parent=self)
        self.view.message.connect(self._from_page)
        self.view.setGeometry(self.rect())
        _live_pages.append(self)
        if overlay_open():
            # Built while an overlay is up - stay down until it closes.
            self.view.suppress(True)

        # **And checked, not merely told.** Every event-based version of
        # this failed: the overlay's own lifetime is not a reliable
        # signal (the details page can be destroyed and rebuilt straight
        # away, which fired the "closed" handler while it was still on
        # screen), and this page is itself rebuilt while an overlay
        # opens. So the question is asked directly a few times a second:
        # is anything actually covering me? Cheap - a handful of
        # geometry comparisons - and it cannot be fooled by whatever
        # order Qt happens to destroy and rebuild things in.
        self._watch = QTimer(self)
        self._watch.timeout.connect(self._check_covered)
        self._watch.start(150)

    def _check_covered(self):
        try:
            self._fill()
            self.view.suppress(not self.isVisible() or _covered(self))
        except RuntimeError:
            pass                 # this page is on its way out

    def _fill(self):
        """Keep the view exactly over this page.

        The 150ms check above calls this too, because the page swap
        animates a *pixmap* of each page and only sets the real geometry
        when it lands - so a page can be the right size while the view
        inside it is still the wrong one, and no single event marks the
        moment that stops being true.
        """
        try:
            if self.view.geometry() != self.rect():
                self.view.setGeometry(self.rect())
        except RuntimeError:
            pass

    def showEvent(self, event):
        super().showEvent(event)
        self._fill()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fill()

    def hideEvent(self, event):
        super().hideEvent(event)
        # A native child is not hidden with its Qt parent, and would go
        # on painting over whatever replaced this page.
        try:
            self.view.suppress(True)
        except RuntimeError:
            pass

    # ---- what the page asks the app to do ---------------------------
    def _from_page(self, body):
        """A click in the page.

        The kind is decided by the server, where the row's source file is
        known - a game launches, an app or website opens its targets, a
        title opens the details page. Working it out from the row's own
        fields at this end is what sent a clicked game to another title's
        episode list.
        """
        if not isinstance(body, dict):
            return
        if body.get("action") == "key":
            self._app_key(str(body.get("key") or ""))
            return
        if body.get("action") != "open":
            return
        kind = str(body.get("kind") or "title")
        entry_id = str(body.get("id") or "")
        title = str(body.get("title") or "")

        if kind == "game":
            entry = _find_in("games.json", entry_id, title)
            if entry is not None:
                self._run(lambda: _launch_game(entry))
            return
        if kind in ("app", "site"):
            name = "apps.json" if kind == "app" else "websites.json"
            entry = _find_in(name, entry_id, title)
            if entry is not None:
                self._run(lambda: _open_links(self, entry))
            return

        entry = _find(entry_id, title)
        if entry is None:
            # A Discover title is not in the library, so there is nothing
            # to look up - the details page takes the row itself, which
            # carries everything it needs to show a title it has never
            # seen before.
            if title:
                entry = {"title": title,
                         "type": str(body.get("type") or "Series"),
                         "url": str(body.get("url") or ""),
                         "imdb_id": str(body.get("imdb") or ""),
                         "cover_url": ""}
            else:
                return
        if body.get("mode") == "continue":
            # The ring on the cover resumes; the card body opens the
            # list. Same two targets Home always had, and the same call
            # windows/home._continue_entry makes, so the reader, the
            # player and the site handling are whatever it already does.
            self._run(lambda: _continue(self, entry))
            return
        self._open_overlay(entry)

    def _app_key(self, name):
        """A key the window owns, forwarded out of the page.

        The web view holds the keyboard while it has focus, so these
        never reached Qt on their own - see webview2_host._accelerator.
        """
        window = self.window()
        if window is None:
            return
        try:
            if name == "F11" and hasattr(window, "toggle_fullscreen"):
                window.toggle_fullscreen()
            elif name == "Escape" and getattr(window, "isFullScreen", None)                     and window.isFullScreen()                     and hasattr(window, "toggle_fullscreen"):
                window.toggle_fullscreen()
        except Exception:
            logs.exception("Forwarding a key from a web page failed")

    def _run(self, action):
        try:
            action()
        except Exception:
            logs.exception("Acting on a web page click failed")

    def _open_overlay(self, entry):
        from windows import details
        try:
            # Down before the overlay exists. This page is rebuilt while
            # the overlay opens, and the rebuilt one's view stays hidden
            # until it has loaded - so nothing of either is left over the
            # details page.
            self.view.suppress(True)
            page = details.open_details(self.app, entry)
            # Registered app-wide, not on this instance: this page is
            # about to be rebuilt, and the rebuilt one has to know.
            overlay_opened(page)
        except Exception:
            logs.exception("Opening a title from a web page failed")
            self.view.suppress(False)



def _covered(page) -> bool:
    """Is anything sitting over `page` right now?

    Walks the widgets that overlays are opened into - the window's
    overlay host, which is the central widget - and answers yes if any
    visible one that is not this page (nor an ancestor of it) overlaps
    it. That is precisely the condition under which the page's native
    child must not paint, and it is true whether the thing on top is the
    details page, the reader, the player or a dialog.
    """
    window = page.window()
    if window is None:
        return False
    host = (window.overlay_host() if hasattr(window, "overlay_host")
            else (window.centralWidget()
                  if hasattr(window, "centralWidget") else None))
    if host is None:
        return False

    mine = set()
    walker = page
    while walker is not None:
        mine.add(id(walker))
        walker = walker.parentWidget()

    rect = page.rect()
    if rect.isEmpty():
        return False
    top_left = page.mapTo(host, rect.topLeft())
    area = rect.translated(top_left)
    for child in host.children():
        if not isinstance(child, QWidget) or id(child) in mine:
            continue
        if not child.isVisible():
            continue
        if child.geometry().intersects(area):
            return True
    return False

def _continue(page, entry):
    from windows.tracker import open_tracker_entry
    if not open_tracker_entry(page, entry, resume=True):
        logs.info(f"nothing to resume for {entry.get('title')!r}")


def _launch_game(entry):
    from helpers import game_launch
    game_launch.run(entry)


def _open_links(parent, entry):
    from windows import link_grid
    link_grid.open_link_entry(parent, entry)


def _find_in(name, entry_id, title):
    """One entry from one file, by id and then by name."""
    wanted = title.strip().lower()
    rows = [r for r in storage.load(name, []) if isinstance(r, dict)]
    for row in rows:
        if entry_id and str(row.get("id") or "") == entry_id:
            return row
    for row in rows:
        if wanted and str(row.get("title") or row.get("name")
                          or "").strip().lower() == wanted:
            return row
    return None


def _find(entry_id, title):
    """The saved entry behind a clicked card, by id and then by title."""
    wanted = title.strip().lower()
    for name in ("tracker.json", "series.json", "games.json"):
        rows = storage.load(name, [])
        for row in rows:
            if isinstance(row, dict) and entry_id \
                    and str(row.get("id") or "") == entry_id:
                return row
        if wanted:
            for row in rows:
                if isinstance(row, dict) and \
                        str(row.get("title") or "").strip().lower() == wanted:
                    return row
    # A history row is a real entry for the details page's purposes - it
    # carries title, type, url and imdb_id - and is what a Watching card
    # points at when the show was never saved.
    for row in storage.load("history.json", []):
        if not isinstance(row, dict):
            continue
        if entry_id and str(row.get("entry_id") or "") == entry_id:
            return row
        if wanted and str(row.get("title") or "").strip().lower() == wanted:
            return row
    return None


class WebHomePage(_WebPage):
    ROUTE = "home"


class WebTrackerPage(_WebPage):
    """One of the six catalogue pages, on WebView2.

    nav_config.NAV_GROUPS routes these as `series:cat_movies` and the
    like, so main._apply_route_section hands the section here and the
    view is pointed at that medium. The page half only decides which
    medium the row lands on before a section arrives.
    """

    SECTION_ROUTES = {
        "cat_movies": "movies", "cat_series": "series", "cat_anime": "anime",
        "cat_manga": "manga", "cat_manhwa": "manhwa", "cat_manhua": "manhua",
    }

    def set_active_section(self, section):
        route = self.SECTION_ROUTES.get(str(section or ""), "")
        if not route:
            return
        self.ROUTE = route
        self.view.show_url(f"{base_url()}?embed=1#{route}")


class WebWatchPage(WebTrackerPage):
    ROUTE = "series"


class WebReadPage(WebTrackerPage):
    ROUTE = "manga"


class WebDiscoverPage(_WebPage):
    ROUTE = "discover"

    def start_search(self, query):
        """Show `query`'s results here.

        main._search_in_discover navigates to Discover and then calls
        this; a page without it left the user on the ordinary Discover
        with no results, which is what pressing Enter did.
        """
        text = str(query or "").strip()
        if not text:
            return
        import urllib.parse
        route = "search/" + urllib.parse.quote(text, safe="")
        self.view.show_url(f"{base_url()}?embed=1#{route}")
