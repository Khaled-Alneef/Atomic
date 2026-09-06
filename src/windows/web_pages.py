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

from PyQt6.QtCore import QEvent, Qt, QTimer
from PyQt6.QtGui import QKeyEvent
from PyQt6.QtWidgets import QApplication, QWidget

from helpers import changes, logs, storage, webview2_host
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


# **Which route the next web page built should open on.** Set by
# main._show_page immediately before it constructs the page, consumed by
# the first _WebPage built after it, and cleared either way.
#
# It exists because the route is known before the page is and the view
# is created inside __init__: without it a page whose route names a
# section loaded its class default first (`series` for Watch, `manga`
# for Read) and was then pointed at the real route by
# set_active_section - two document loads for Movies, Anime, Manhwa and
# Manhua, one for Series and Manga. PyQt refuses an instance attribute
# assigned before QWidget.__init__ has run (windows/web_reader carries
# the same note), which is why this is not simply passed in.
_START_ROUTE = None


def start_at(route):
    global _START_ROUTE
    _START_ROUTE = str(route or "") or None


def _take_start_route():
    global _START_ROUTE
    route, _START_ROUTE = _START_ROUTE, None
    return route


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
        # The route this page is being opened for, if the window named
        # one - see start_at. Honoured only when this class actually
        # serves that section, so a handoff meant for a tracker page
        # cannot point Home at #movies.
        wanted = _take_start_route()
        if wanted:
            try:
                from helpers.nav_config import route_section
                section = route_section(wanted)
                # getattr, because only WebTrackerPage has sections -
                # Home, Discover and the shelves simply ignore a handoff
                # that is not theirs.
                route = getattr(self, "SECTION_ROUTES", {}).get(
                    str(section or ""), "")
                if route:
                    self.ROUTE = route
            except Exception:
                logs.exception("could not open a web page on its own route")
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
        # What the files looked like when this page was drawn - see
        # _check_covered.
        self._data_stamp_at = self._data_stamp()
        self._watch = QTimer(self)
        self._watch.timeout.connect(self._check_covered)
        self._watch.start(150)

    # **Only history.json, and only Home.** A mark written by the
    # reader, the player or the details page lands there
    # (history.set_watched), which is the whole of what he asked to see
    # appear without changing pages.
    #
    # Watching the other five was a mistake with teeth: the tracker's
    # background lookups write `next_release_checked_at` onto entries in
    # series.json and tracker.json while a page is simply being looked
    # at, so the stamp moved every few seconds, and every move reloaded
    # the page - throwing away every row the scroll had loaded and
    # putting the view back at the top. That is "the watch and read
    # pages do not load when scrolling down", 2 September 2026, and it
    # was this timer undoing the load rather than the load failing.
    # **The three shelf files are here for Home's sake.** The owner, 4
    # September 2026: "make the apps and games in the main pages when
    # opened become the first in sort in the main page after ~1.5 sec".
    # Launching writes `last_played`/`last_used` and Home now orders by
    # it (server._recent_first); this is the half that makes the page
    # notice, at the 150ms tick below. Still only Home redraws - the
    # shelves override both of these with their own file and route, and
    # every other page ignores them.
    _WATCHED_FILES = ("history.json", "games.json", "apps.json",
                      "websites.json")
    # **What a card's *look* depends on, which is more than the marks.**
    # The numbers come from history.json; the accent on a catalogue
    # card's meta line comes from series.json/tracker.json (is this
    # title in the library). Both are patched in place by the page, so
    # unlike _WATCHED_FILES - which triggers a full redraw and therefore
    # may only be history.json, see the note above it - watching these
    # two costs nothing when a background lookup touches them: the page
    # re-reads /api/progress, finds the same stamp, and stops.
    _LIVE_FILES = ("history.json", "series.json", "tracker.json")
    # Which pages redraw themselves on a mark. Home is the one he named,
    # and the one with nothing to lose from a redraw - a catalogue page
    # holds pages of scrolled-in rows and a shelf holds a sort and a
    # selection.
    _WATCH_DATA_ROUTES = ("home",)
    # Pages that are lists of what is saved or done, and so redraw on a
    # user change (helpers/changes); every other route holds scrolled
    # rows and is patched in place instead.
    _RELOAD_ON_CHANGE = ("home", "discover", "saved", "history", "schedule")

    def _data_stamp(self, files=None):
        """A cheap fingerprint of the files named."""
        marks = []
        for name in (files or self._WATCHED_FILES):
            try:
                marks.append(int((storage.DATA_DIR / name).stat().st_mtime_ns))
            except OSError:
                marks.append(0)
        return tuple(marks)

    def _check_covered(self):
        try:
            self._fill()
            # **Redraw when the data under the page has changed.** The
            # owner, 2 September 2026: "when I read/mark as read/unread
            # or watch or mark as watch/unwatch make it show immediately
            # on the home page, not when I change pages then come back".
            #
            # A web page is fetched once and then it is a document; the
            # marks are written by the reader, the player and the details
            # page, all of which are overlays over this one, and none of
            # them has any way to reach in here. Rather than wire each
            # writer to each page - four writers by six pages, and the
            # next one forgotten - the page watches the files it drew
            # from and re-reads when their timestamps move.
            #
            # Only while it is on screen and uncovered: reloading a page
            # underneath the reader would throw away the scroll position
            # he comes back to, and the check is six stat() calls, which
            # is nothing against the 150ms it already spends here.
            covered = (not self.isVisible() or overlay_open()
                       or _covered(self))
            # **Told once whenever this page comes back into view, not
            # only when a timestamp moved.** The owner, 3 September
            # 2026: "number 9 is not showing live directly when I
            # change". The stamp comparison alone cannot see it - this
            # app *rebuilds the page behind an overlay* (the note at the
            # top of this module measured the Home object changing
            # identity across one card click), so the page that was
            # watching when the reader wrote a mark is gone, and the one
            # that replaces it takes its first stamp **after** the write
            # and therefore sees no change at all.
            #
            # A push costs a fetch of /api/progress and, when nothing
            # moved, one string comparison in the page (app.js
            # progressInto returns on an unchanged stamp). So the edge
            # into view is enough on its own.
            was = getattr(self, "_was_covered", True)
            self._was_covered = covered
            # **A change the user made shows at once - the owner's rule
            # of 6 September 2026 (helpers/changes).** The counter moves
            # on every user write; this page acts on it the first tick
            # it is on screen and uncovered: a list page redraws, a
            # catalogue grid patches its numbers and saved marks in
            # place, and a page behind an overlay does it the moment
            # the overlay closes. The background lookups never bump it,
            # so this cannot become the redraw storm the file watch did.
            changed = changes.version()
            if changed != getattr(self, "_change_seen", None):
                if getattr(self, "_change_seen", None) is not None:
                    self._change_pending = True
                self._change_seen = changed
            if not covered and getattr(self, "_change_pending", False):
                self._change_pending = False
                if str(self.ROUTE).split("/")[0] in self._RELOAD_ON_CHANGE:
                    self._data_stamp_at = self._data_stamp()
                    self._live_stamp_at = self._data_stamp(self._LIVE_FILES)
                    self.reload()
                    return
                self._live_stamp_at = self._data_stamp(self._LIVE_FILES)
                self.view.tell({"marks": 1})
            if not covered and was:
                # The live stamp is taken here so the push below does
                # not immediately fire a second time. `_data_stamp_at`
                # is deliberately *not* touched: Home still redraws for
                # a mark written while it was covered, which is what
                # changes which titles it lists and in what order.
                self._live_stamp_at = self._data_stamp(self._LIVE_FILES)
                self.view.tell({"marks": 1})
            if not covered:
                live = self._data_stamp(self._LIVE_FILES)
                if live != getattr(self, "_live_stamp_at", None):
                    self._live_stamp_at = live
                    self.view.tell({"marks": 1})
                stamp = self._data_stamp()
                if stamp != self._data_stamp_at:
                    self._data_stamp_at = stamp
                    if str(self.ROUTE) in self._WATCH_DATA_ROUTES:
                        self.reload()
                    else:
                        # **Every other page moves its numbers without
                        # being redrawn.** The owner, 3 September 2026:
                        # "make the ep and season / ch numbers change
                        # immediately when marked as watched/unwatched
                        # in home page, and saved and all pages that
                        # shows progress."
                        #
                        # A redraw is what Home gets and what a
                        # catalogue page must not get: it holds hundreds
                        # of rows paged in a batch at a time and a shelf
                        # holds a sort and a selection, and the note
                        # above records what throwing those away looked
                        # like. So the page is told the marks moved and
                        # patches the four places a number is drawn
                        # (app.js progressInto) - the scroll, the rows
                        # and the selection all stay exactly as they
                        # are.
                        self.view.tell({"marks": 1})
            # **overlay_open() as well as the geometry test.** This tick
            # runs every 150ms and used to decide on its own, so an
            # overlay that registered with overlay_opened() was undone a
            # sixth of a second later - the player put the web pages
            # down, and this put them straight back up over the video.
            # The owner photographed the result twice: his episode
            # playing behind a full copy of Home.
            self.view.suppress(covered)
        except RuntimeError:
            pass                 # this page is on its way out

    def _fill(self):
        """Keep the view exactly over this page.

        The 150ms check above calls this too, because the page swap
        animates a *pixmap* of each page and only sets the real geometry
        when it lands - so a page can be the right size while the view
        inside it is still the wrong one, and no single event marks the
        moment that stops being true. That tick is also what puts the
        full-screen strip back when the state changes.
        """
        try:
            rect = self._fold_rect() or self.rect()
            top = self._reserved_top()
            if self.view.geometry() != rect.adjusted(0, top, 0, 0):
                self.view.setGeometry(rect.x(), rect.y() + top,
                                      rect.width(), rect.height() - top)
        except RuntimeError:
            pass

    def _fold_rect(self):
        """The width to hold while the sidebar animates, or None.

        **One re-flow per fold, not one per frame, and at the width the
        page finishes at.** main publishes that rect on the window for
        the whole animation (see main._toggle_sidebar, which carries the
        reasoning and the two versions this replaced). A web page cannot
        be photographed and blitted the way a Qt page is - the view is a
        native child and Qt excludes its rectangle from the top-level's
        painting - so pinning is the whole of its half of the freeze.

        Both directions, unlike the version before it: an unfold pinned
        to the *widest* rect held the outgoing layout and then snapped,
        and a snap at the end of a fold is exactly as visible as one
        during it.
        """
        try:
            target = getattr(self.window(), "_fold_target", None)
            if target is None:
                return None
            rect = self.rect()
            rect.setWidth(target.width())
            return rect
        except (AttributeError, RuntimeError):
            return None

    # ---- the sidebar fold ------------------------------------------
    # The three calls main._toggle_sidebar makes on a live page while
    # the rail animates. Measured before them, 2 September 2026, on the
    # Watch page (scratchpad driver.py/analyze.py, screen band across
    # the poster row at 240Hz): the cards stood dead still for every one
    # of the 43 samples inside the 180ms rail motion, then moved 198
    # device pixels in a single sample at 196ms and re-flowed 53px in the
    # next - because Qt never moves a native child for an ancestor's
    # layout move (webview2_host.sync_position), and the page was pinned
    # so its own geometry never changed until the fold landed.

    def follow_fold(self):
        """One animation step: put the view where the page now is."""
        try:
            self.view.sync_position()
        except RuntimeError:
            pass

    def offer_fold(self, to_width, ms, on_ack, on_sized=None):
        """Tell the page the width it will be laid out at, so it can
        carry its cards there itself (app.js hostFold). `on_ack` runs
        when the page says it has, `on_sized` when it has drawn itself
        at the size the window then gives it; False if it could not be
        told.

        `view` is the view's width in Qt's logical pixels beside `to`,
        because the page's CSS pixel is not Qt's: measured 3 September
        2026, a 2266-device-pixel child was 1700 to Qt and 1813.6 to the
        page (Qt 1.3333, Chromium 1.25), and a grid laid out at Qt's
        number snapped 145 device pixels wider when the fold landed. The
        page scales `to` by innerWidth / view and is right at any ratio.
        """
        self._fold_seq = getattr(self, "_fold_seq", 0) + 1
        self._fold_ack = on_ack
        self._fold_sized = on_sized
        told = self.view.tell({"fold": {
            "seq": self._fold_seq, "to": int(to_width), "ms": int(ms),
            "view": max(1, int(self.view.width())),
            # widgets.ease_out_cubic, as CSS spells it - the rail and
            # the cards run the same curve for the same time.
            "curve": "cubic-bezier(0.215, 0.61, 0.355, 1)"}})
        if not told:
            self._fold_ack = self._fold_sized = None
        else:
            # The answer arrives on WinForms' queue; turn it fast for the
            # moment the rail is waiting on it.
            webview2_host.pump_burst(60)
        return told

    def go_fold(self, started_ms):
        """The rail has started, at `started_ms` on the wall clock
        (time.time() * 1000): the page runs its cards from that instant
        - Date.now() reads the same clock - so a late ack costs nothing
        but the frames already gone, never a lag for the rest."""
        try:
            self.view.tell({"fold": {"go": 1, "seq": self._fold_seq,
                                     "at": float(started_ms)}})
        except RuntimeError:
            pass

    def fold_done(self):
        """The fold has landed (or was cut short): the page drops the
        widths it held for it."""
        self._fold_ack = self._fold_sized = None
        try:
            self.view.tell({"fold": {"done": 1}})
        except RuntimeError:
            pass

    def _reserved_top(self):
        """How much of the page the window's bar needs to itself.

        **Full screen puts the bar over the page, and a native child
        cannot be overlaid.** main._apply_fullscreen_chrome re-parents
        the bar onto the page's own header row as a transparent overlay,
        which worked while every page was Qt. This view is a native child
        window, and on Windows a native child paints above every
        non-native sibling whatever raise_() was told - the same trap
        .claude/rules/ui.md records for the player's loading logo, which
        had never once been visible. Measured 1 September 2026 on Home in
        full screen: Qt reported the bar shown at (345, 29, 1010, 48) and
        the screenshot has nothing there at all, so the owner had no
        search field and no Saved/Schedule/History.

        Rather than promote the bar to a native window of its own - which
        then needs the DWM alpha route player._set_window_alpha exists
        for, because a native child paints its own opaque background -
        the view simply starts below it. Windowed the bar is a strip in
        the layout above this page, so nothing is reserved.
        """
        try:
            window = self.window()
            bar = getattr(window, "title_bar", None)
            if (bar is None or not window.isFullScreen()
                    or not bar.isVisible()):
                return 0
            bottom = bar.mapTo(window, bar.rect().bottomLeft()).y()
            mine = self.mapTo(window, self.rect().topLeft()).y()
            return max(0, min(bottom - mine + 6, self.height() // 3))
        except (AttributeError, RuntimeError):
            return 0

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

    def event(self, event):
        # **The view dies with the page, and before it.** main._show_page
        # deleteLater()s the outgoing page; the DeferredDelete arrives
        # here first, while this widget and its native window still
        # exist, which is when WebView2 wants its controller closed. The
        # alternative - nothing - left Edge's document running behind
        # every page ever visited (helpers/webview2_host._open).
        try:
            if event.type() == QEvent.Type.DeferredDelete:
                self.view.dispose()
        except Exception:
            pass
        return super().event(event)

    # ---- what the page asks the app to do ---------------------------
    def reload(self):
        """Draw this page again, after Qt has changed what it reads.

        **A message, not a navigation, and that was the bug.** The
        owner, 3 September 2026: *"in the history make it when I clear
        the history it immediately clears not when I change pages or
        tabs then come back!"*

        This used to be `show_url(<the url the view is already on>)`,
        and a URL identical to the current one - fragment included - is
        a *same-document* navigation: Chromium fires no `hashchange`,
        app.js's router never runs, and the document keeps every row it
        already drew. So `history.clear()` emptied the file and the page
        went on showing the list until something else changed the hash,
        which is exactly "when I change pages or tabs then come back".
        The same silence swallowed every other reload here - a shelf
        after an add, a download folder change - whenever the route had
        not moved.

        `{"redraw": 1}` is answered by app.js's hostMessage with
        `go(currentRoute())`, which re-fetches and redraws whatever is
        showing. show_url stays as the fallback for a view that has not
        finished starting, where `tell` cannot land.
        """
        self._data_stamp_at = self._data_stamp()
        try:
            if self.view.tell({"redraw": 1}):
                return
            self.view.show_url(f"{base_url()}?embed=1#{self.ROUTE}")
        except RuntimeError:
            pass

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
        if body.get("action") == "fold":
            # The page has taken the fold (offer_fold); the window may
            # now start the rail. Only the fold it was asked about - an
            # answer to an earlier, superseded offer is dropped.
            if body.get("seq") != getattr(self, "_fold_seq", 0):
                return
            if body.get("sized"):
                sized, self._fold_sized = getattr(self, "_fold_sized", None), None
                if sized is not None:
                    sized()
                return
            ack = getattr(self, "_fold_ack", None)
            if ack is not None and body.get("ok"):
                self._fold_ack = None
                # **The fold's own numbers, on his machine.** The page
                # already measures them (app.js hostFold: how many cards
                # it holds, how many it will slide, and what preparing
                # the slide cost) and they were being dropped here. "The
                # cards transition is a bit delayed" is a claim about
                # exactly this, and a page that has been scrolled deep
                # carries hundreds of cards - so the count belongs in
                # the log beside the cost rather than only in a rig.
                logs.info(f"fold {self.ROUTE}: cards={body.get('n')} "
                          f"near={body.get('near')} "
                          f"moved={body.get('moved')} "
                          f"prep={body.get('cost')}ms")
                ack()
            return
        if body.get("action") == "key":
            self._app_key(str(body.get("key") or ""))
            return
        if body.get("action") == "pagepress":
            self._leave_search()
            return
        if body.get("action") == "diag":
            # The page reporting on itself - see app.js sweepLazy.
            logs.info("web page: " + ", ".join(
                f"{k}={v}" for k, v in body.items() if k != "action"))
            return
        if body.get("action") == "history":
            self._history_action(body)
            return
        if body.get("action") == "saved":
            self._saved_action(body)
            return
        if body.get("action") == "list":
            self._list_action(body)
            return
        if body.get("action") != "open":
            return
        kind = str(body.get("kind") or "title")
        entry_id = str(body.get("id") or "")
        title = str(body.get("title") or "")

        if kind == "person":
            # **A face opens that name's own page**, not a details page
            # it has no entry for - the same page a cast chip on the
            # details page opens (details._open_cast_browse), reached
            # here because Discover, the search results and the search
            # suggestions now carry faces too (the owner, 3 September
            # 2026).
            self._run(lambda: self._open_cast(title))
            return
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

        entry = _find(entry_id, title, body.get("type"))
        if entry is None:
            # A Discover title is not in the library, so there is nothing
            # to look up - the details page takes the row itself, which
            # carries everything it needs to show a title it has never
            # seen before.
            if title:
                entry = _transient(body)
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

    def _open_cast(self, name):
        """One cast member's titles, over this page."""
        name = str(name or "").strip()
        if not name:
            return
        from windows import web_reader
        self.view.suppress(True)
        page = web_reader.open_cast_browse(self.window(), name)
        if page is None:
            self.view.suppress(False)
            return
        overlay_opened(page)

    def _list_action(self, body):
        """Save the banner's title to the library, or take it out.

        The owner, 3 September 2026: *"in the discovery page, instead of
        the continue btn in the banner make it 'Save to My List' or
        'Remove from My List'"*.

        **The details page's own save, not a write of our own.** A saved
        entry is not just a row: it belongs in series.json or
        tracker.json depending on its medium, and it needs an id, a
        status and two timestamps before anything else in the app counts
        it as saved. All five were inside DetailsPage._save_entry, bound
        to a page with a button to re-face; they are now
        details.save_to_library / remove_from_library at module scope
        and *that page calls them too*, so there is one implementation
        rather than a copy here that would drift.

        The saved row carries the banner's own `cover_url`, which is
        what the cards then draw. It does not carry a `cover_path`: the
        details page queues a download for that off its own signals, and
        a save with no page open has nowhere for the answer to land -
        the tracker's own cover backfill picks it up on its next visit.
        """
        entry = {"title": str(body.get("title") or "").strip(),
                 "type": str(body.get("type") or "Series"),
                 "url": str(body.get("url") or ""),
                 "imdb_id": str(body.get("imdb") or ""),
                 "cover_url": str(body.get("poster") or "")}
        if not entry["title"]:
            return
        try:
            from helpers.widgets import show_toast
            from windows import details
            want = bool(body.get("save"))
            found = _find("", entry["title"], entry["type"])
            if want:
                if found is not None and found.get("id"):
                    show_toast(self.window(), "Already In Your List")
                    return
                if not details.save_to_library(entry):
                    show_toast(self.window(), "Could Not Save")
                    return
                show_toast(self.window(), "Saved To Your List")
            else:
                if found is None or not found.get("id"):
                    show_toast(self.window(), "Not In Your List")
                    return
                _file, _at, removed = details.remove_from_library(found)
                if removed is None:
                    show_toast(self.window(), "Could Not Remove")
                    return
                show_toast(self.window(), "Removed From Your List")
        except Exception:
            logs.exception("Saving a banner title to the list failed")

    def _history_action(self, body):
        """Clear History, or forget one title, asked from the page.

        Behind the same confirmation the Qt page used, with the same
        words: the episode and chapter marks live in this file too, so
        emptying it is not only a list of names being forgotten. The
        marks on saved entries are elsewhere and are untouched.
        """
        want = str(body.get("do") or "")
        if want == "forget":
            self._forget_one(body)
            return
        if want != "clear":
            return
        try:
            from helpers import history
            from helpers.widgets import confirm, show_toast
            if not confirm(self.window(), "Clear History",
                           "Forget every title in History? The episode and "
                           "chapter marks stored here go with them. Saved "
                           "entries and their progress are untouched.",
                           yes_text="Clear", danger=True):
                return
            history.clear()
            show_toast(self.window(), "History Cleared")
            self.reload()
        except Exception:
            logs.exception("Could not clear the history")

    def _saved_action(self, body):
        """Remove several titles from the library at once.

        The owner, 4 September 2026: "add a select button in saved to
        select multi then delete!" - the page picks, this writes.

        **Behind a question, unlike History's single row.** Removing a
        saved entry takes its progress, its site and its artwork choices
        with it and there is no undo here, so several at once is asked
        about the way the shelves ask (games._delete_selected). The
        marks in history.json are left alone: they are a record of what
        was watched, not of what is in the library, and History still
        lists the title afterwards as "Not saved".

        Written with storage.save on a *fresh* read rather than a list
        the page is holding - .claude/rules/ui.md's rule, and the reason
        reordering a game once erased freshly imported ones.
        """
        want = str(body.get("do") or "")
        if want == "menu":
            self._saved_menu(body)
            return
        if want != "delete":
            return
        ids = {str(i) for i in (body.get("ids") or []) if i}
        if not ids:
            return
        name = ("tracker.json" if str(body.get("tab") or "") == "read"
                else "series.json")
        try:
            from helpers import storage
            from helpers.widgets import confirm, show_toast
            rows = storage.load(name, [])
            going = [r for r in rows
                     if isinstance(r, dict) and str(r.get("id") or "") in ids]
            if not going:
                return
            what = (going[0].get("title") or "this title"
                    if len(going) == 1 else f"{len(going)} titles")
            if not confirm(self.window(), "Remove From Saved",
                           f"Remove {what} from your library? Their progress "
                           "and settings go with them. History is untouched.",
                           yes_text="Remove", danger=True):
                return
            storage.save(name, [r for r in rows
                                if not (isinstance(r, dict)
                                        and str(r.get("id") or "") in ids)])
            show_toast(self.window(), "Removed From Saved")
            self.reload()
        except Exception:
            logs.exception("Could not remove the picked saved entries")

    def _saved_menu(self, body):
        """Right-click on a saved card: move it to another status.

        The owner, 4 September 2026: "add some way to change from
        watching to other status like right click on the card in the
        saved page, then change status or something."

        The list is the one for that entry's own type - you watch an
        anime and read a manga, so the wording differs
        (tracker.STATUSES_BY_TYPE). The current one is ticked and does
        nothing when chosen. One field on one entry, so
        storage.update_entry rather than a list written back
        (.claude/rules/ui.md).
        """
        entry_id = str(body.get("id") or "").strip()
        if not entry_id:
            return
        try:
            from PyQt6.QtCore import QPoint
            from PyQt6.QtWidgets import QMenu
            from helpers import storage
            from helpers.widgets import show_toast
            from windows.tracker import STATUSES_BY_TYPE, _WATCHING_STATUSES

            found = name = None
            for file_name in ("series.json", "tracker.json"):
                for row in storage.load(file_name, []):
                    if isinstance(row, dict) and str(row.get("id") or "") == entry_id:
                        found, name = row, file_name
                        break
                if found is not None:
                    break
            if found is None:
                return

            statuses = STATUSES_BY_TYPE.get(found.get("type"),
                                            _WATCHING_STATUSES)
            now = str(found.get("status") or "").strip() or statuses[0]
            menu = QMenu(self)
            actions = {}
            for status in statuses:
                act = menu.addAction(status)
                act.setCheckable(True)
                act.setChecked(status == now)
                actions[act] = status
            # The page's coordinates are its own, so they are offset by
            # where this page sits - never mapToGlobal, which divides by
            # the other screen's factor on a mixed-DPI desktop.
            window = self.window()
            here = self.mapTo(window, self.rect().topLeft())
            frame = window.geometry()
            chosen = menu.exec(QPoint(
                frame.x() + here.x() + int(float(body.get("x") or 0)),
                frame.y() + here.y() + int(float(body.get("y") or 0))))
            picked = actions.get(chosen)
            if not picked or picked == now:
                return
            if storage.update_entry(name, entry_id, {
                    "status": picked, "updated_at": storage.now_iso()}):
                show_toast(self.window(), f"Moved to {picked}")
                self.reload()
        except Exception:
            logs.exception("Could not change a saved entry's status")

    def _forget_one(self, body):
        """One title dropped out of History, from its right-click menu.

        The owner, 4 September 2026: "make when I right click on any item
        in the history show a Remove from History button ... and make it
        remove it from history immediately!"

        No confirmation, unlike Clear: one row is undoable by opening the
        title again, and a question in front of a single deletion is what
        he was asking to be rid of. The page has already taken the row
        off screen (app.js histMenu) - this is only the write, and
        `reload` is deliberately *not* called: redrawing here would take
        the list back to the top of a page he is part-way down.
        """
        key = str(body.get("key") or "").strip()
        if not key:
            return
        try:
            from helpers import history
            from helpers.widgets import show_toast
            if history.forget(key):
                self._data_stamp_at = self._data_stamp()   # not a redraw
                show_toast(self.window(), "Removed From History")
        except Exception:
            logs.exception("Could not forget a history row")

    # What each forwarded name is, as Qt. Sent to the window as a real
    # key event rather than each being wired to the method it happens to
    # call today: main.keyPressEvent is where the app's keyboard is
    # defined - Ctrl+1-9 reads the *visible* sidebar order, Escape leaves
    # the search field before it leaves full screen - and a second copy
    # of that here would be wrong the first time either changed.
    _AS_QT = {
        "F11": (Qt.Key.Key_F11, Qt.KeyboardModifier.NoModifier),
        "Escape": (Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier),
        "Ctrl+F": (Qt.Key.Key_F, Qt.KeyboardModifier.ControlModifier),
        "Ctrl+N": (Qt.Key.Key_N, Qt.KeyboardModifier.ControlModifier),
        "Ctrl+Z": (Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier),
        "Ctrl+Y": (Qt.Key.Key_Y, Qt.KeyboardModifier.ControlModifier),
        "Ctrl+,": (Qt.Key.Key_Comma, Qt.KeyboardModifier.ControlModifier),
        "Alt+Left": (Qt.Key.Key_Left, Qt.KeyboardModifier.AltModifier),
        "Alt+Right": (Qt.Key.Key_Right, Qt.KeyboardModifier.AltModifier),
    }
    _AS_QT.update({
        f"Ctrl+{n}": (getattr(Qt.Key, f"Key_{n}"),
                      Qt.KeyboardModifier.ControlModifier)
        for n in range(1, 10)
    })

    def _app_key(self, name):
        """A key the window owns, forwarded out of the page.

        The web view holds the keyboard while it has focus, so these
        never reached Qt on their own - see webview2_host._accelerator.
        """
        window = self.window()
        if window is None:
            return
        combo = self._AS_QT.get(name)
        if combo is None:
            return
        key, modifier = combo
        try:
            # Straight to the window, not posted to whatever has focus:
            # focus is inside the web view, which is exactly why the key
            # had to be forwarded in the first place.
            event = QKeyEvent(QEvent.Type.KeyPress, int(key), modifier)
            QApplication.sendEvent(window, event)
        except Exception:
            logs.exception("Forwarding a key from a web page failed")

    def _leave_search(self):
        """A press on the page leaves the title bar's search field.

        **The owner, 4 September 2026:** *"the global search box, when I
        click anywhere outside it make it leave the search box"*. The
        panel's own filter handles a press on a Qt widget, by way of the
        field's focus (global_search_visual_patch._AnchorFilter) - but a
        page is a WebView2 native child, and Windows hands it the click
        without Qt ever hearing about it, so nothing there moves Qt's
        focus at all. The page therefore says so itself: app.js sends
        `pagepress` on any press that is not on a card.

        Nothing happens unless a panel is actually open, so an ordinary
        click on a page costs one dictionary lookup.
        """
        window = self.window()
        if window is None:
            return
        panel = getattr(window, "_search_panel", None)
        if panel is not None:
            try:
                from helpers import global_search_list_polish_patch as polish
                polish._exit_search_field(panel)
                return
            except Exception:
                try:
                    panel.close()
                except RuntimeError:
                    pass
                return
        # **And with no panel there is still a field to leave.** The
        # owner, 4 September 2026: "the search bar exit only when I click
        # on the upper bar of the app, make it exit when I click anywhere
        # in the whole app in any page!!!". A panel exists only while
        # something is typed, so this returned early for an empty field -
        # and a page is a WebView2 native child, so the application
        # filter that handles the Qt side never sees the press either.
        # The two halves together are why the title bar was the only
        # place that worked.
        try:
            bar = getattr(window, "title_bar", None) or getattr(window, "_title_bar", None)
            field = getattr(bar, "search", None) if bar is not None else None
            if field is None:
                # Whatever the bar is called on this window, the field is
                # the one QLineEdit named TopSearch (window_chrome).
                from PyQt6.QtWidgets import QLineEdit
                for candidate in window.findChildren(QLineEdit):
                    if candidate.objectName() == "TopSearch":
                        field = candidate
                        break
            if field is not None and (field.hasFocus() or field.text()):
                field.clear()
                field.clearFocus()
        except (RuntimeError, AttributeError, ImportError):
            pass

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
    # **Both hosts, not just the overlay one.** The details page opens
    # into overlay_host; the player and the reader take immersive_host,
    # which is the whole window including the app's own bar (see
    # main.immersive_host). Walking only the first meant the one overlay
    # that covers everything was the one this could not see.
    hosts = []
    for name in ("overlay_host", "immersive_host"):
        getter = getattr(window, name, None)
        if callable(getter):
            found = getter()
            if found is not None and found not in hosts:
                hosts.append(found)
    if not hosts and hasattr(window, "centralWidget"):
        central = window.centralWidget()
        if central is not None:
            hosts.append(central)
    if not hosts:
        return False

    mine = set()
    walker = page
    while walker is not None:
        mine.add(id(walker))
        walker = walker.parentWidget()

    rect = page.rect()
    if rect.isEmpty():
        return False
    for host in hosts:
        try:
            area = rect.translated(page.mapTo(host, rect.topLeft()))
        except (RuntimeError, TypeError):
            continue
        for child in host.children():
            if not isinstance(child, QWidget) or id(child) in mine:
                continue
            if not child.isVisible():
                continue
            # **Most of the page, not a sliver.** An overlay covers this
            # page whole; the sidebar merely *overlaps* it for a few
            # frames of a fold, while the page still holds its old width
            # and the rail has already grown. Treating that as "covered"
            # suppressed the view mid-animation and the page went black
            # until the fold landed - the owner's "when I unfold the
            # sidebar the page goes totally black for a moment".
            hit = child.geometry().intersected(area)
            if hit.width() * hit.height() >= area.width() * area.height() * 0.6:
                return True
    return False

READING_MEDIA = ("manga", "manhwa", "manhua", "other")


def _next_chapter_index(entry):
    """Where "continue" should open a reading entry, or None.

    **The chapter list runs newest first** - measured on the owner's
    Kingdom, index 0 is chapter 886 and index 380 is chapter 1 - so the
    chapter *after* the furthest one he has read is at a *lower* index.
    Nothing read at all opens the oldest, which is where a series starts.
    """
    try:
        from web import backend
        from windows import web_reader
        # The reader's own resolution, not `entry["id"]`: a Discover or
        # catalogue card for an unsaved title carries no id, and Continue
        # on it answered None here (measured 2 September 2026) while the
        # same entry read fine once the reader registered it.
        entry_id = web_reader._entry_id_for(entry)
        items = backend.chapters(entry_id).get("items") or []
        if not items:
            return None
        marks = set(backend.read_state(entry_id).get("watched") or [])
        read_at = [item["i"] for item in items if item.get("key") in marks]
        if not read_at:
            return len(items) - 1
        following = min(read_at) - 1
        # Already on the newest: re-open it rather than refusing.
        return following if following >= 0 else min(read_at)
    except Exception:
        logs.exception("Working out the next chapter failed")
        return None


def _continue(page, entry):
    """Resume. For reading that means the *next chapter*, opened.

    The owner, 1 September 2026: "when I press continue or the resume btn
    on the reading make it takes me to the next ch directly, not the ch
    list". open_tracker_entry lands on the list for a reading entry, so
    the reader is opened here instead and that call is the fallback.
    """
    medium = str(entry.get("type") or "").strip().lower()
    if medium in READING_MEDIA:
        index = _next_chapter_index(entry)
        if index is not None:
            from windows import web_reader
            if web_reader.available():
                window = page.window()
                if web_reader.open_reader(window, entry, index) is not None:
                    return
    from windows.tracker import open_tracker_entry
    if not open_tracker_entry(page, entry, resume=True):
        logs.info(f"nothing to resume for {entry.get('title')!r}")


def _launch_game(entry):
    from helpers import game_launch
    game_launch.run(entry)


def _open_links(parent, entry):
    from windows import link_grid
    link_grid.open_link_entry(parent, entry)


def _transient(body):
    """A card that is not in the library, as an entry.

    **Carrying its picture.** The owner, 3 September 2026: "when I press
    on a card on the genre and cast pages, it shows me the ep list
    correctly but the bg image and the logo are not loading." This used
    to hardcode `cover_url: ""`, and the details page's ground is a blur
    of the entry's own cover (details._seed_backdrop_from_cover walks
    cover_path, then cover_url/poster) - so every title opened from a
    catalogue, a genre or a cast page opened on flat black. The card the
    user clicked had already downloaded that very file; `poster` is now
    the picture's real address rather than this server's proxy token,
    which is what helpers/images' cache is keyed by (web/server._row).
    """
    art = str((body or {}).get("poster") or "")
    return {"title": str((body or {}).get("title") or ""),
            "type": str((body or {}).get("type") or "Series"),
            "url": str((body or {}).get("url") or ""),
            "imdb_id": str((body or {}).get("imdb") or ""),
            "cover_url": art if art.startswith("http") else "",
            "cover_path": "" if art.startswith("http") else art}


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


def entry_side(kind) -> str:
    """'read', 'watch', or '' for a type this app does not file."""
    kind = str(kind or "").strip().lower()
    if kind in READING_MEDIA:
        return "read"
    if kind in ("anime", "series", "movie", "movies"):
        return "watch"
    return ""


def _find(entry_id, title, kind=""):
    """The saved entry behind a clicked card, by id and then by title.

    **A title only matches on its own side of the Watch/Read split.**
    Measured 3 September 2026 on the owner's data: the search page's
    Reading row carried the 3asq manga "Kingdom" (no id, type Manga),
    and clicking it opened the *anime* Kingdom's details page - Season
    6, cast, Continue Watching - because history.json holds the anime
    under the same title and this matched the first "kingdom" it saw.
    `kind` is the clicked card's own type; a reading card cannot resolve
    to a video row or the reverse. A card with no type keeps the old
    any-side match, since nothing better is known about it.
    """
    wanted = title.strip().lower()
    side = entry_side(kind)

    def agrees(row):
        other = entry_side(row.get("type"))
        return not side or not other or other == side

    for name in ("tracker.json", "series.json", "games.json"):
        rows = storage.load(name, [])
        for row in rows:
            if isinstance(row, dict) and entry_id \
                    and str(row.get("id") or "") == entry_id:
                return row
        if wanted:
            for row in rows:
                if isinstance(row, dict) and agrees(row) and \
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
        if wanted and agrees(row) and \
                str(row.get("title") or "").strip().lower() == wanted:
            return row
    return None


class WebHomePage(_WebPage):
    ROUTE = "home"


class _WebShelfPage(_WebPage):
    """Games, Apps or Websites, drawn by the page and acted on by Qt."""

    SHELF = "games"

    def _qt_shelf(self):
        """The real Qt page for this shelf, built on first use.

        Not built with the web page: GamesPage's constructor migrates
        records and backfills icons and launch commands, which is work
        nobody asked for on a visit that only scrolls.

        **Built, never shown.** The owner, 3 September 2026, with a
        screenshot: *"in the games, apps and websites pages, when I
        click right click on the cards or click refresh or any button in
        the page, the page becomes like duplicated"* - and it was
        literally two pages. This used to `show()` the Qt page under the
        view so its dialogs would land where the Qt page's do, and the
        web view is a native child that only covers `self.rect()` minus
        `_reserved_top()`: in full screen that leaves a strip at the top
        where the Qt page's own header - its "Games" title, its Sort
        row, its refresh and + buttons - painted above the web page's
        identical header. One right-click was enough to build it, and it
        then stayed for the life of the page.
        
        Nothing needs it visible. A QMenu pops at a screen point, a
        modal dialog centres on the window, and a toast is raised on
        `self.window()` - none of them read the parent's visibility. So
        it is parented here (for ownership and for `window()` to
        resolve) and left hidden.
        """
        page = getattr(self, "_qt_page", None)
        if page is not None:
            return page
        # Each lives in its own module - link_grid holds the shared
        # base, not the three pages.
        from windows.apps import AppsPage
        from windows.games import GamesPage
        from windows.websites import WebsitesPage
        builders = {"games": GamesPage, "apps": AppsPage,
                    "websites": WebsitesPage}
        builder = builders.get(self.SHELF)
        if builder is None:
            return None
        try:
            # **Parented to the window, not to this page.** The owner,
            # 3 September 2026: "the games page shifts when pressing
            # buttons or right click on the cards (only happened once
            # with me then never again)". Building a full Qt page as a
            # child of this one - even hidden - puts a widget with its
            # own layout inside the widget the WebView2 is sized
            # against, and the geometry pass that follows is what moves
            # the view. The window is the right owner anyway: nothing
            # about this page is drawn, only its dialogs and its menu,
            # and both of those resolve their position from the window.
            page = builder(self.app)
            page.setParent(self.window())
            page.hide()          # see the docstring: never shown
        except Exception:
            logs.exception(f"Building the Qt {self.SHELF} page failed")
            return None
        self._qt_page = page
        return page

    def _entries(self):
        return _rows_for(self.SHELF)

    def _entry(self, entry_id):
        for row in self._entries():
            if str(row.get("id") or "") == str(entry_id):
                return row
        return None

    # **No reload of its own.** The owner, 4 September 2026: "in the
    # games app and webs pages when I remove item make it removed
    # immediately not when I switch pages then come back, also when I
    # refresh the games and it added new games make them show
    # immediately".
    #
    # This class carried an override that did `show_url(<the url the
    # view is already on>)` - which is the exact same-document
    # navigation the base class's own docstring records as the bug it
    # was written to fix: a URL identical to the current one, fragment
    # included, fires no hashchange, so app.js's router never runs and
    # the document keeps every card it had. So a delete, an add or an
    # import wrote the file, this "reloaded", and nothing changed until
    # the route moved - "when I switch pages then come back", to the
    # word. The base sends `{"redraw": 1}` instead, which app.js answers
    # by re-drawing the current route, and it keeps the scroll.

    def _from_page(self, body):
        if not isinstance(body, dict):
            return
        action = body.get("action")
        if action in ("add", "import", "menu", "delete", "reorder"):
            self._shelf_action(action, body)
            return
        super()._from_page(body)

    def _shelf_action(self, action, body):
        try:
            if action == "reorder":
                # **Back at his word, 4 September 2026:** "in the games,
                # apps and webs pages allow dragging the cards to sort
                # them, and make the sort type auto change to custom
                # order like in the Qt before!!". Removed earlier the
                # same day and restored with the sort switch the page
                # now makes on the drop (app.js shelfCard).
                #
                # The only shelf action that needs no dialog: storage
                # owns the move, and move_entry re-reads and writes the
                # file itself.
                from helpers import storage
                name = _SHELF_FILES.get(self.SHELF)
                if name and storage.move_entry(name, body.get("moved"),
                                               body.get("target")):
                    self.reload()
                return
            page = self._qt_shelf()
            if page is None:
                return
            if action == "add":
                opener = (getattr(page, "_add_game", None)
                          or getattr(page, "_open_add_form", None))
                if callable(opener):
                    opener()
            elif action == "import":
                opener = getattr(page, "_import_from_launchers", None)
                if callable(opener):
                    opener()
            elif action == "menu":
                entry = self._entry(body.get("id"))
                if entry is not None:
                    self._open_menu(page, entry, body)
            elif action == "delete":
                ids = [str(i) for i in (body.get("ids") or []) if i]
                if ids:
                    # The page's own batch delete: one question, one undo
                    # offer, and the removals applied to a fresh read.
                    page._selected_ids = set(ids)
                    page._delete_selected()
            # Whatever it did, the file may have changed under the page.
            QTimer.singleShot(400, self.reload)
        except Exception:
            logs.exception(f"A {self.SHELF} action failed")

    def _open_menu(self, page, entry, body):
        """The card's right-click menu, at the pointer.

        The Qt menu wants an object with `globalPosition()` - games.py
        already carries that shim for its virtual grid and says why: the
        business logic must not learn which renderer it is being called
        from. The page's coordinates are its own, so they are offset by
        where this page sits, never mapToGlobal (.claude/rules/ui.md).
        """
        from PyQt6.QtCore import QPointF
        window = self.window()
        here = self.mapTo(window, self.rect().topLeft())
        frame = window.geometry()
        point = QPointF(frame.x() + here.x() + float(body.get("x") or 0),
                        frame.y() + here.y() + float(body.get("y") or 0))

        class _Where:
            def globalPosition(self):
                return point

        opener = getattr(page, "_show_context_menu", None)
        if callable(opener):
            opener(_Where(), entry)


_SHELF_FILES = {"games": "games.json", "apps": "apps.json",
                "websites": "websites.json"}


def _rows_for(shelf):
    name = _SHELF_FILES.get(shelf)
    if not name:
        return []
    try:
        return storage.load(name, [])
    except Exception:
        return []


class WebGamesPage(_WebShelfPage):
    ROUTE = "games"
    SHELF = "games"
    _WATCHED_FILES = ("games.json",)
    _WATCH_DATA_ROUTES = ("games",)


class WebAppsPage(_WebShelfPage):
    ROUTE = "apps"
    SHELF = "apps"
    _WATCHED_FILES = ("apps.json",)
    _WATCH_DATA_ROUTES = ("apps",)


class WebSitesPage(_WebShelfPage):
    ROUTE = "websites"
    SHELF = "websites"
    _WATCHED_FILES = ("websites.json",)
    _WATCH_DATA_ROUTES = ("websites",)


class WebDownloadsPage(_WebPage):
    """The queue. Every button is the queue's own function, called here.

    No Qt page behind this one: helpers/downloads is the whole of what
    DownloadsPage's buttons call, and unlike the shelves none of it opens
    a dialog or offers an undo - a cancelled download cannot be
    un-cancelled, which is why that page asks nothing either.
    """

    ROUTE = "downloads"

    def _from_page(self, body):
        if isinstance(body, dict) and body.get("action") == "dl":
            self._queue_action(str(body.get("do") or ""),
                               str(body.get("id") or ""))
            return
        super()._from_page(body)

    def _queue_action(self, what, job_id):
        try:
            from helpers import downloads as queue
            if what == "pause" and job_id:
                queue.pause(job_id)
            elif what == "resume" and job_id:
                queue.resume(job_id)
            elif what == "cancel" and job_id:
                queue.cancel(job_id)
            elif what == "clear":
                queue.clear_finished()
            elif what == "folder":
                self._pick_folder()
        except Exception:
            logs.exception("A downloads action failed")

    def _pick_folder(self):
        """Where finished files go - the page's own picker, not Qt's.

        downloads_page.choose_folder both asks and remembers, and it
        passes DontUseNativeDialog, which that module is explicit is "not
        a style choice - it is the fix for the app freezing here".
        """
        try:
            from windows import downloads_page
            if downloads_page.choose_folder(self.window()):
                self.reload()
        except Exception:
            logs.exception("Choosing a download folder failed")

    def reload(self):
        try:
            self.view.show_url(f"{base_url()}?embed=1#{self.ROUTE}")
        except RuntimeError:
            pass


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
        # The window's bar opens these three (main.open_section). Without
        # them set_active_section returned early and left the page on its
        # own ROUTE, which is why every one of them showed Series.
        "saved": "saved", "history": "history", "schedule": "schedule",
    }

    def set_active_section(self, section):
        route = self.SECTION_ROUTES.get(str(section or ""), "")
        if not route:
            return
        # **Already there is not a navigation.** main._show_page hands
        # the route in before this page is built (web_pages.start_at),
        # so the view is loading the right route by the time this is
        # called and there is nothing to re-aim.
        #
        # **This is a simplification, not a measured speed-up, and the
        # measurement said so.** The theory it was written for was that
        # Movies, Anime, Manhwa and Manhua each paid a second document
        # load (the class default first, then their own) while Series
        # and Manga paid one - two of those four being the pages the
        # owner called delayed. A control run of the source tree with
        # the handoff switched off, counting the page's own render line
        # (app.js sayRender), showed **one render per arrival either
        # way**: navigating a view to the URL it is already on is a
        # same-document navigation, so the old shape's second Navigate
        # fired no hashchange and drew nothing. The redundant call is
        # gone and the view is now pointed at the right route from its
        # first byte, which is worth having on its own - but it is not
        # what he is seeing.
        #
        # A later return to the same section from a *different* one
        # still navigates, because ROUTE tracks where the view is.
        if route == self.ROUTE:
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
