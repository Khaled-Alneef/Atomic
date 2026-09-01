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

from PyQt6.QtCore import Qt, pyqtSignal as Signal
from PyQt6.QtGui import QKeySequence, QShortcut
from PyQt6.QtWidgets import QVBoxLayout, QWidget

from helpers import logs, webview2_host


def available() -> bool:
    # ATOMIC_WEB_READER=0 forces the Qt reader back, without a rebuild.
    # Insurance: this replaces the reader at its one entry point, and a
    # surface the owner reads on every day should always have a way back
    # that does not need me.
    if os.environ.get("ATOMIC_WEB_READER") == "0":
        return False
    return webview2_host.available()


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
        route = (f"read/{entry_id}/{int(chapter_index)}"
                 if chapter_index is not None else f"chapters/{entry_id}")
        self.view = webview2_host.WebView2Page(
            url=f"{base_url}?embed=1#{route}", parent=self)
        self.view.message.connect(self._from_page)
        layout.addWidget(self.view, 1)

        # Escape closes, as it does everywhere else in this app. The
        # shortcut is on this widget rather than the window so it goes
        # away with the reader.
        self._escape = QShortcut(QKeySequence("Escape"), self)
        self._escape.activated.connect(self.close_reader)

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
        if isinstance(body, dict) and body.get("action") == "close":
            self.close_reader()

    def close_reader(self):
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
        host = (window.overlay_host() if hasattr(window, "overlay_host")
                else (window.centralWidget()
                      if hasattr(window, "centralWidget") else window))
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
