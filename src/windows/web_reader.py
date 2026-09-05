"""The reading viewer, scrolled by Edge rather than by Qt.

**The owner's ask, 1 September 2026:** the reader viewer should use the
web scrolling like Home and Discover. A chapter is one long strip of
pictures, which is the single surface in this app where scrolling *is*
the whole experience - so it is the one that benefits most.

Opened the same way windows/reader.py opens: a widget over the central
widget, covering the sidebar as well, rather than a page in the stack.
`reader.open_reader` routes here when this machine has a WebView2 and
falls back to its own Qt reader when it does not, so a build without the
runtime is exactly what it was.

**What the Qt reader still does that this does not** - said plainly
rather than discovered later: fit modes, the reading music, the
keybindings, and the resume-to-exact-position that reader.py keeps in
`reader_position`. This opens at the top of a chapter. Chapter list,
next/previous, and marking a chapter read all work.
"""

import os

from PyQt6.QtCore import QEvent, Qt, pyqtSignal as Signal
from PyQt6.QtGui import QKeySequence, QShortcut
from PyQt6.QtWidgets import QVBoxLayout, QWidget

from urllib.parse import quote

from helpers import logs, webview2_host


def available() -> bool:
    # ATOMIC_WEB_READER=0 forces the Qt reader back, without a rebuild.
    # Insurance: this replaces the reader at its one entry point, and a
    # surface the owner reads on every day should always have a way back
    # that does not need me.
    if os.environ.get("ATOMIC_WEB_READER") == "0":
        return False
    return webview2_host.available()


def _entry_id_for(entry):
    """This entry's id, looked up by title when it is carrying none.

    **A card whose title is not in the library arrives here id-less.**
    windows/web_pages._from_page builds a transient entry when `_find`
    misses - {title, type, url, imdb_id} and no id - which is right for
    the details page, and fatal here: the route is `read/<id>/<n>`, so no
    id makes it `read//0`, and web/backend.pages answers
    `{"error": "no reader here"}` for the empty id. That is the owner's
    "the reader shows no reader here", 2 September 2026 - the reader was
    fine, it was being pointed at nothing.

    A catalogue or search row for something he *does* have saved is the
    common case, and a title match against the saved files recovers the
    real id - the same rule `_find` already uses one level up.

    **A title he has not saved reads too.** The owner, 2 September 2026:
    "why is it showing me that this is not in my lib when I try to read
    a ch directly from the manga/manhwa/manhua pages". When no saved row
    matches, the entry goes into web/backend's in-memory registry and
    its "t-" id comes back, so `read/<id>/<n>` routes to an entry the
    backend can answer for. Nothing is written to a saved file for it.
    """
    entry_id = str(entry.get("id") or entry.get("entry_id") or "")
    if entry_id:
        return entry_id
    title = str(entry.get("title") or "").strip().lower()
    if title:
        try:
            from helpers import storage
            # Only the file on this entry's own side: tracker.json holds
            # what is read, series.json what is watched, and a reading
            # title matched against both is how the 3asq manga "Kingdom"
            # resolved to the anime (see web_pages._find, 3 September
            # 2026). An entry with no usable type still asks both.
            from windows.web_pages import entry_side
            side = entry_side(entry.get("type"))
            files = {"read": ("tracker.json",),
                     "watch": ("series.json",)}.get(
                side, ("tracker.json", "series.json"))
            for name in files:
                for row in storage.load(name, []):
                    if not isinstance(row, dict):
                        continue
                    if str(row.get("title") or "").strip().lower() == title:
                        found = str(row.get("id") or "")
                        if found:
                            return found
        except Exception:
            logs.exception("Could not resolve the reader's entry id")
    try:
        from web import backend
        return backend.register_transient(entry)
    except Exception:
        logs.exception("Could not register the reader's transient entry")
    return ""


class WebReader(QWidget):
    """One chapter strip, over the whole window.

    Carries the Qt reader's own `closed` signal (reader.py:2252) because
    both callers rely on it: details._read connects it to redraw the
    entry, and tracker._wire_overlay_refresh does the same so a card
    shows new progress without a page switch. Without it both raised
    AttributeError into an `except` that swallowed it - the reader
    opened and nothing was ever told it had closed.
    """

    closed = Signal()

    def __init__(self, base_url, entry, chapter_index, host):
        super().__init__(host)
        self.entry = entry
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setObjectName("Bare")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        entry_id = str(entry.get("id") or entry.get("entry_id") or "")
        route = self._route_for(entry, chapter_index)
        self.view = webview2_host.WebView2Page(
            url=f"{base_url}?embed=1#{route}", parent=self)
        self.view.message.connect(self._from_page)
        layout.addWidget(self.view, 1)

        # Escape closes, as it does everywhere else in this app. The
        # shortcut is on this widget rather than the window so it goes
        # away with the reader.
        self._escape = QShortcut(QKeySequence("Escape"), self)
        self._escape.activated.connect(self.close_reader)

        # **The reading music.** The owner: "test the reading music URL
        # several times after you fix it, it is not working now at all".
        # It was never broken - it lives on windows/reader.py, and this
        # viewer replaced that reader without carrying it over. So the Qt
        # reader's own functions are called rather than a second copy
        # written here: _open_music_quietly opens the URL in a browser
        # window and then tucks and minimises it (there is a great deal
        # of hard-won behaviour in that, down to which order the window
        # is resolved in), and close_music_now is what the back button
        # already used.
        # Only while a chapter is actually open: the chapter *list*
        # never played it, and WebGenreBrowse borrows this shell with an
        # entry that is a genre name, which is not a title to score.
        self._music = False
        if chapter_index is not None and entry and entry.get("title"):
            try:
                from windows import reader as _qt_reader
                _qt_reader._open_music_quietly(self.window(), entry)
                self._music = True
            except Exception:
                logs.exception("The reading music could not be started")

    def _route_for(self, entry, chapter_index):
        """Which page this shell shows. Overridden by WebGenreBrowse."""
        entry_id = _entry_id_for(entry)
        # **Always a chapter, never a list.** The owner, 4 September
        # 2026: the reader's chapter-list page is gone ("REMOVE IT
        # ENTIRELY AND REMOVE ITS PAGE"), so an open with nothing to
        # resume starts at index 0 - the newest chapter, the list being
        # newest-first - rather than at a route that no longer exists.
        # Every chapter is still one press away from the dropdown in the
        # reader's own top bar, and from the details page's list.
        index = int(chapter_index) if chapter_index is not None else 0
        return f"read/{entry_id}/{index}"

    def follow(self, host):
        """Cover the host exactly, and keep covering it."""
        self.setGeometry(host.rect())
        host.installEventFilter(self)

    def eventFilter(self, watched, event):
        from PyQt6.QtCore import QEvent
        if event.type() == QEvent.Type.Resize:
            self.setGeometry(watched.rect())
        return False

    def _from_page(self, body):
        if not isinstance(body, dict):
            return
        action = body.get("action")
        if action == "close":
            self.close_reader()
        elif action == "key":
            self._app_key(str(body.get("key") or ""))
        elif action == "diag":
            # **The reader's own numbers reach the log from here.** The
            # page has written a "reader sized" line since 4 September
            # 2026 (app.js, where `single` is decided) and nothing was
            # listening: this shell handles four actions and drops the
            # rest, so three rounds of "the size and quality are wrong"
            # were argued with no measurement on either side.
            logs.info("web reader: " + ", ".join(
                f"{k}={v}" for k, v in body.items() if k != "action"))
        elif action == "browser":
            self._open_in_browser(str(body.get("id") or ""),
                                  body.get("i") or 0)
        elif action == "open":
            self._open_card(body)

    def _open_card(self, body):
        """A card clicked on a page drawn in this shell.

        **The owner, 3 September 2026: "Also, the cards do not take me to
        the ep list when I click on them they do nothing."** This shell
        was written for the reader, where the only things to click are
        its own controls, and it handled `close`, `key` and `browser`
        and dropped everything else. WebGenreBrowse and WebCastBrowse
        borrow it to draw a *grid*, and app.js's gridCard sends
        `{action:'open', ...}` like every other card in the app - so
        every click on a genre or cast page reached this method-that-was-
        not-there and was thrown away. The page was never broken; the
        message had nowhere to go.

        A face opens that person's own page in a shell of its own (the
        cast row on Discover and the search results carry faces too);
        anything else opens the details page over this one, which is
        where the episode or chapter list lives.
        """
        title = str(body.get("title") or "")
        if str(body.get("kind") or "") == "person":
            if title:
                open_cast_browse(self.window(), title)
            return
        from windows import details
        from windows.web_pages import _find, overlay_opened
        entry = _find(str(body.get("id") or ""), title, body.get("type"))
        if entry is None:
            if not title:
                return
            # A catalogue row is not in the library, so there is nothing
            # to look up - the details page takes the row itself, the
            # same transient-entry road a Discover card takes. Through
            # web_pages._transient, so it carries its picture: without
            # one the details page opens on flat black, which is exactly
            # what he reported for these two pages.
            from windows.web_pages import _transient
            entry = _transient(body)
        try:
            # **On this shell's own host, and above it.** The shell takes
            # `immersive_host` (the central widget) while the details
            # page's own host is `overlay_host`, the row under the app's
            # bar - and that row is a child of the central widget, so a
            # details page opened the ordinary way was a sibling
            # *underneath* this shell. It opened every time and could
            # only be seen by closing the page it opened from, which is
            # exactly what the owner described.
            host = self.parentWidget() or _overlay_host(self.window())
            page = details.open_details(self.window(), entry, host=host)
            page.raise_()
            # And the view goes down with it: this shell's view is a
            # native child window, and on Windows a native child paints
            # above every non-native sibling whatever raise_() was told
            # (.claude/rules/ui.md). Raising the details page is not
            # enough on its own; the web page has to stop painting.
            self.view.suppress(True)
            try:
                page.destroyed.connect(self._card_closed)
            except Exception:
                self._card_closed()
            overlay_opened(page)
        except Exception:
            logs.exception("Opening a title from a genre or cast page failed")

    def _card_closed(self, *_args):
        """The details page opened from a card has gone - draw again."""
        try:
            if self.isVisible():
                self.view.suppress(False)
        except RuntimeError:
            pass

    def _app_key(self, name):
        """Full screen, from the reader's own bar or its F key.

        The rest of the Reading block in global_search.SHORTCUTS is
        handled in the page - chapter, zoom, reload and every kind of
        scrolling are the page's own, and round-tripping them through Qt
        would only add a frame to each.
        """
        window = self.window()
        # **Escape, forwarded by the page, closes the reader.** The page
        # takes the key (app.js `preventDefault`s it and sends it here),
        # so the QShortcut above never sees it while the view has focus
        # - which it always has once a page has been clicked or
        # scrolled. Measured 5 September 2026 driving the frozen build:
        # two Escapes after a PgDn left the reader exactly where it was.
        if name == "Escape":
            self.close_reader()
            return
        if name in ("F11", "F") and hasattr(window, "toggle_fullscreen"):
            try:
                window.toggle_fullscreen()
            except Exception:
                logs.exception("Full screen from the reader failed")

    def _open_in_browser(self, entry_id, index):
        """The chapter's own page, in the real browser - reader's globe.

        The URL is asked of the same source the reader reads through, so
        a site whose chapter URLs are built rather than stored still
        answers.
        """
        try:
            import webbrowser
            from web import backend
            entry = backend.entry_by_id(entry_id)
            if entry is None:
                return
            found = backend.chapter_source.cached_chapters(entry) or []
            try:
                url = str((found[int(index)] or {}).get("url") or "")
            except (IndexError, TypeError, ValueError):
                url = ""
            webbrowser.open(url or str(entry.get("url") or ""))
        except Exception:
            logs.exception("Opening a chapter in the browser failed")

    def event(self, event):
        # The view is closed with the reader - see web_pages._WebPage.event.
        try:
            if event.type() == QEvent.Type.DeferredDelete:
                self.view.dispose()
        except Exception:
            pass
        return super().event(event)

    def close_reader(self):
        # The music goes with the reader, exactly as it does on the Qt
        # one - reader.close_music_now is what its back button calls.
        if getattr(self, "_music", False):
            try:
                from windows import reader as _qt_reader
                _qt_reader.close_music_now()
            except Exception:
                logs.exception("The reading music could not be stopped")
            self._music = False
        # The view goes down first. Its widget is a native window, and Qt
        # excludes a native child's rectangle from the top-level's own
        # painting - so a reader merely hidden leaves a hole where it was
        # (see helpers/webview2_host.suppress).
        try:
            self.view.suppress(True)
        except Exception:
            pass
        self.hide()
        try:
            self.closed.emit()
        except RuntimeError:
            pass
        self.deleteLater()


def open_reader(window, entry, chapter_index=None):
    """Put the web reader over `window`. Returns it, or None."""
    if not available():
        return None
    try:
        from windows.web_pages import base_url
        # **immersive_host, not overlay_host.** The reader is one of the
        # two surfaces that take the whole window, the app's own bar
        # included - main.immersive_host names it and the player, and
        # says why: both are the content rather than a page showing
        # content, both draw their own top bar with the title and the way
        # out, and "a search field over the top is furniture in the way".
        #
        # Laid over overlay_host instead, this reader sat *under* that
        # bar, which cost it more than the look: its own top bar is
        # revealed by reaching for the top of the window, and the top
        # 72px of the reach zone were behind the Qt bar, so the reach
        # never landed and the bar could not be brought back at all.
        # Measured 1 September 2026 - the cursor put at the reader's own
        # y=20 is over the search field.
        host = None
        for name in ("immersive_host", "overlay_host"):
            getter = getattr(window, name, None)
            if callable(getter):
                host = getter()
                if host is not None:
                    break
        if host is None:
            host = (window.centralWidget()
                    if hasattr(window, "centralWidget") else window)
        host = host if host is not None else getattr(window, "container", window)
        page = WebReader(base_url(), entry, chapter_index, host)
        page.follow(host)
        page.show()
        page.raise_()
        page.setFocus()
        # Registered app-wide so Home's and Discover's views stay down
        # while this is up - including one rebuilt underneath it. See
        # web_pages._overlay_depth for why per-page suppression cannot
        # work here.
        from windows import web_pages
        web_pages.overlay_opened(page)
        return page
    except Exception:
        logs.exception("Opening the web reader failed")
        return None


class WebGenreBrowse(WebReader):
    """One genre's titles, over the whole window.

    The same shell the reader is - a WebView2 covering the host, Escape
    to leave, `closed` when it goes - because the two pages differ only
    in what they draw. details.GenreBrowsePage drew a painted PosterGrid
    and was the last full page still scrolling through Qt; the owner, 2
    September 2026: "make the same generes page use the same scroll as
    other pages (WebView2)".
    """

    def __init__(self, base_url, genre, is_reading, host):
        # Carried in the entry dict rather than set on self beforehand:
        # PyQt refuses an attribute assigned before QWidget.__init__ has
        # run, and _route_for is called from inside it.
        super().__init__(base_url,
                         {"title": str(genre), "genre": str(genre),
                          "reading": bool(is_reading)},
                         None, host)

    def _route_for(self, entry, chapter_index):
        return (f"genre?name={quote(str(entry.get('genre') or ''))}"
                f"&reading={'1' if entry.get('reading') else '0'}")


class WebCastBrowse(WebReader):
    """One cast member's titles, over the whole window - the genre shell
    with a different route. The owner, 2 September 2026: "make the cast
    also clickable as the genres". No Qt fallback behind it, unlike the
    genre page: the cast page never existed as a painted grid, so where
    WebView2 is missing the chip logs and does nothing."""

    def __init__(self, base_url, name, host):
        super().__init__(base_url,
                         {"title": str(name), "person": str(name)},
                         None, host)

    def _route_for(self, entry, chapter_index):
        # safe="" so a name with a slash in it does not split the hash
        # route the page reads (app.js go() splits on "/").
        return f"cast?name={quote(str(entry.get('person') or ''), safe='')}"


def _overlay_host(window):
    host = None
    for name in ("immersive_host", "overlay_host"):
        getter = getattr(window, name, None)
        if callable(getter):
            host = getter()
            if host is not None:
                break
    return window if host is None else host


def _show_over(page, host):
    from windows.web_pages import overlay_opened
    page.follow(host)
    page.show()
    page.raise_()
    page.setFocus()
    overlay_opened(page)
    return page


def open_genre_browse(window, genre, is_reading):
    """Put the genre page over `window`. Returns it, or None."""
    if not available():
        return None
    try:
        from windows.web_pages import base_url
        host = _overlay_host(window)
        return _show_over(WebGenreBrowse(base_url(), genre, is_reading, host),
                          host)
    except Exception:
        logs.exception("Opening the web genre browse failed")
        return None


def open_cast_browse(window, name):
    """Put a cast member's page over `window`. Returns it, or None."""
    if not available():
        logs.warning("cast page needs WebView2, which is unavailable")
        return None
    try:
        from windows.web_pages import base_url
        host = _overlay_host(window)
        return _show_over(WebCastBrowse(base_url(), name, host), host)
    except Exception:
        logs.exception("Opening the web cast browse failed")
        return None
