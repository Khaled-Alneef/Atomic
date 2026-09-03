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
    _WATCHED_FILES = ("history.json",)
    # Which pages redraw themselves on a mark. Home is the one he named,
    # and the one with nothing to lose from a redraw - a catalogue page
    # holds pages of scrolled-in rows and a shelf holds a sort and a
    # selection.
    _WATCH_DATA_ROUTES = ("home",)

    def _data_stamp(self):
        """A cheap fingerprint of everything this page reads."""
        marks = []
        for name in self._WATCHED_FILES:
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
            if not covered and str(self.ROUTE) in self._WATCH_DATA_ROUTES:
                stamp = self._data_stamp()
                if stamp != self._data_stamp_at:
                    self._data_stamp_at = stamp
                    self.reload()
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

    # ---- what the page asks the app to do ---------------------------
    def reload(self):
        """Draw this page again, after Qt has changed what it reads."""
        self._data_stamp_at = self._data_stamp()
        try:
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
                ack()
            return
        if body.get("action") == "key":
            self._app_key(str(body.get("key") or ""))
            return
        if body.get("action") == "diag":
            # The page reporting on itself - see app.js sweepLazy.
            logs.info("web page: " + ", ".join(
                f"{k}={v}" for k, v in body.items() if k != "action"))
            return
        if body.get("action") == "history":
            self._history_action(body)
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

        entry = _find(entry_id, title, body.get("type"))
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

    def _history_action(self, body):
        """Clear History, asked from the page.

        Behind the same confirmation the Qt page used, with the same
        words: the episode and chapter marks live in this file too, so
        emptying it is not only a list of names being forgotten. The
        marks on saved entries are elsewhere and are untouched.
        """
        if str(body.get("do") or "") != "clear":
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
        nobody asked for on a visit that only scrolls. Sized to this page
        and left under the view, so its dialogs, menus and undo toasts
        land where they would on the Qt page itself.
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
            page = builder(self.app)
            page.setParent(self)
            page.setGeometry(self.rect())
            page.lower()
            page.show()
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

    def reload(self):
        """Draw the shelf again after Qt has changed the file."""
        try:
            self.view.show_url(f"{base_url()}?embed=1#{self.ROUTE}")
        except RuntimeError:
            pass

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
                # The only one that does not need a dialog: storage owns
                # the move, and move_entry re-reads and writes the file
                # itself (see storage.move_entry).
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


class WebAppsPage(_WebShelfPage):
    ROUTE = "apps"
    SHELF = "apps"


class WebSitesPage(_WebShelfPage):
    ROUTE = "websites"
    SHELF = "websites"


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
