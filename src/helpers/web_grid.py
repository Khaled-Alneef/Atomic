"""The poster grid, rendered by Chromium instead of painted by Qt.

**The owner's decision, 31 August 2026**, after two weeks on the scroll
and after his own repositories settled the argument: there is no
scrolling code in stremio-web to copy. Its only wheel handler is in the
video player, every scrollTo is a jump, and the layout is
`overflow-y: auto` in thirty-three places. All of Stremio's smoothness
is the browser's compositor, so the only way to have it is to be a
browser - which is what this is.

It is a drop-in for `poster_grid.PosterGrid` on purpose: same three
signals (`clicked`, `needs_cover`, `scrolled`) and the same handful of
methods the category page calls, so the page does not know which of the
two it is holding. That is what makes this reversible - one constructor
call decides, and nothing else in the app changes.

**Nothing here scrolls anything.** No wheel handler, no animation, no
script touching scrollTop. The page is `overflow-y: auto` and Chromium
does the rest, exactly as it does for Stremio.

Two things worth knowing about the seams:

- **Covers arrive as QPixmaps, not paths.** `_GridCover.setPixmap` is
  how every cover in this app is delivered, and a browser cannot be
  handed a QPixmap - so each one is encoded once, as PNG, and pushed
  into the page as a data URL. That encode is the price of the seam; it
  happens once per cover, off the scroll path.
- **The page talks back through its title.** A click and a
  visible-card report become `document.title`, which Qt reports through
  titleChanged, and `_on_message` turns into the signal the category
  page is already connected to. A URL was the obvious channel and does
  not work: Chromium refuses a navigation to an unregistered scheme
  before it becomes a request, so acceptNavigationRequest never saw one
  - measured as needs_cover x0 on a grid whose 180 cards had rendered
  perfectly, and therefore not one cover. Through the title the same run
  reports 114.
"""

from __future__ import annotations

import json

from PyQt6.QtCore import (Qt, QBuffer, QByteArray, QIODevice, QObject,
                          QRunnable, QThreadPool, QUrl,
                          pyqtSignal as Signal)
from PyQt6.QtWebEngineCore import (QWebEnginePage,
                                   QWebEngineUrlRequestJob,
                                   QWebEngineUrlSchemeHandler)
from PyQt6.QtWebEngineWidgets import QWebEngineView

from . import theme

# **The page is loaded from the cover scheme's own origin.** It used
# to load from `atomic:grid`, which is not a registered scheme and so
# an opaque origin - and an opaque origin may not fetch a registered
# secure scheme, so every cover request was refused and every card
# was a black rectangle in the owner's build. Same origin for the
# page and its pictures removes the question entirely.
#
# The card geometry the painted grid uses, so the two pages look the
# same while both exist. See helpers/layout.py.
CARD_W, CARD_H = 160, 216

_SHELL = """<!doctype html><meta charset="utf-8"><style>
  /* The app's palette, from helpers/theme.py - never literals that
     happen to match today. */
  html, body { margin:0; height:100%%; background:%(bg)s; color:%(text)s;
    font:13px/1.35 "Segoe UI", system-ui, sans-serif;
    /* No text selection or drag on a grid of cards - it is a list to
       click, and a stray selection while scrolling looks broken. */
    user-select:none; -webkit-user-drag:none; }
  #g { display:grid; gap:10px; padding:16px 20px 60px;
       grid-template-columns:repeat(auto-fill, minmax(%(cw)dpx, 1fr)); }
  .c { background:%(surface)s; border:1px solid %(border)s;
       border-radius:10px; padding:8px 8px 10px; text-align:center;
       cursor:pointer;
       /* **The card is its own island.** Without this the compositor
          re-rasterises the whole grid as it scrolls - 180 cards of
          layout, paint and image decode for a viewport that shows two
          dozen. `content-visibility` lets it skip everything off
          screen, and the intrinsic size keeps the scrollbar honest
          while it does (a wrong one makes the thumb jump as cards are
          realised, which is its own bug). This is what keeps a long
          list of pictures smooth in a browser. */
       content-visibility:auto;
       contain-intrinsic-size:%(ch)dpx;
       contain:layout paint style; }
  .c:hover { background:%(hover)s; border-color:%(accent)s; }
  .p { width:%(iw)dpx; height:%(ih)dpx; margin:0 auto; border-radius:7px;
       background:%(shade)s; object-fit:cover; display:block; }
  .t { margin-top:7px; font-size:12px; line-height:1.25; height:31px;
       overflow:hidden; }
  .m { color:%(dim)s; font-size:11px; margin-top:2px; min-height:14px; }
  .s { color:%(accent)s; }
  /* The page draws its own bar - see the note in the script. */
  html { scrollbar-width: none; }
  body::-webkit-scrollbar { width: 0; height: 0; }
  #sb { position: fixed; top: 0; right: 0; width: 14px; height: 100%%;
        z-index: 20; }
  #th { position: absolute; right: 3px; width: 8px; border-radius: 4px;
        background: %(dim)s; opacity: .55; }
  #sb:hover #th, #th.on { background: %(accent)s; opacity: 1; }
</style><div id="g"></div><div id="sb"><div id="th"></div></div><script>
(function () {
  var grid = document.getElementById('g');
  var pending = {}, timer = null;

  // One message channel back to Python: the document title, which Qt
  // reports through titleChanged. A URL was tried first and Chromium
  // refuses it - an unregistered scheme never reaches the navigation
  // handler, so nothing arrived at all. The counter matters: setting the
  // same title twice emits nothing, and asking twice for the same cards
  // is normal.
  var seq = 0;
  function tell(what) { document.title = 'atomic:' + what + '#' + (++seq); }

  // Which cards are on screen, reported in batches - the page asks for
  // covers the same way the painted grid did, so the fetching side of
  // the app is untouched.
  var seen = new IntersectionObserver(function (rows) {
    rows.forEach(function (row) {
      if (row.isIntersecting) pending[row.target.dataset.i] = 1;
    });
    if (timer) return;
    // 160ms, not 60: each of these becomes a title change, a Qt signal
    // and a round of Python, and during a slow scroll the old interval
    // fired constantly. The rootMargin below is what keeps covers ahead
    // of the viewport, so asking less often costs nothing visible.
    timer = setTimeout(function () {
      timer = null;
      var want = Object.keys(pending);
      pending = {};
      if (want.length) tell('cover/' + want.join(','));
    }, 160);
  }, { rootMargin: '600px 0px' });

  window.addCards = function (rows) {
    var frag = document.createDocumentFragment();
    rows.forEach(function (row) {
      var card = document.createElement('div');
      card.className = 'c';
      card.dataset.i = row.i;
      var img = document.createElement('img');
      img.className = 'p';
      img.width = %(iw)d; img.height = %(ih)d;
      img.alt = '';
      // Decoded off the thread that scrolls. Without this a
      // cover arriving mid-scroll can decode inline and cost
      // the frame it lands on.
      img.decoding = 'async';
      card.appendChild(img);
      var t = document.createElement('div');
      t.className = 't'; t.textContent = row.title;
      card.appendChild(t);
      var m = document.createElement('div');
      m.className = 'm' + (row.saved ? ' s' : '');
      m.textContent = row.meta;
      card.appendChild(m);
      card.addEventListener('click', function () { tell('pick/' + row.i); });
      frag.appendChild(card);
      seen.observe(card);
    });
    grid.appendChild(frag);
    if (window.__layout) window.__layout();
  };

  window.setCover = function (i, url) {
    var card = grid.querySelector('[data-i="' + i + '"]');
    if (card) card.firstChild.src = url;
  };

  window.resetCards = function (keep) {
    grid.textContent = '';
    if (window.__layout) setTimeout(window.__layout, 0);
    // A refill keeps where the reader was; a new section starts at the
    // top. The painted grid draws the same distinction.
    if (!keep) window.scrollTo(0, 0);
  };

  // ---- the scrollbar, drawn and dragged here ----------------------
  // Chromium's own thumb drag is not smoothed: the content is snapped to
  // wherever the pointer implies, every frame, which on a 9000px page is
  // a large jump per pointer pixel. The wheel above is left entirely to
  // the browser; only the drag is eased, and the easing is bounded so it
  // can never trail the thumb and then catch up in one frame.
  var bar = document.getElementById('sb');
  var thumb = document.getElementById('th');
  var dragging = false, wanted = 0, lastStep = 0, raf = 0;

  function layout() {
    var view = innerHeight, all = document.documentElement.scrollHeight;
    if (all <= view) { thumb.style.display = 'none'; return; }
    thumb.style.display = 'block';
    var h = Math.max(36, view * view / all);
    var y = (view - h) * (scrollY / (all - view));
    thumb.style.height = h + 'px';
    thumb.style.top = y + 'px';
  }

  function follow() {
    raf = 0;
    if (!dragging) return;
    var gap = wanted - scrollY;
    // Two pointer steps of lag at most - past that, go there. Measured
    // on the painted grid: unbounded eased to a 281px trail and paid it
    // off in single 4114px frames; bounded, the worst frame was 212px.
    var bound = Math.max(2 * Math.abs(lastStep), 24);
    if (Math.abs(gap) > bound) window.scrollTo(0, wanted);
    else if (Math.abs(gap) > 0.5) window.scrollTo(0, scrollY + gap * 0.42);
    raf = requestAnimationFrame(follow);
  }

  function aim(clientY) {
    var view = innerHeight, all = document.documentElement.scrollHeight;
    var h = Math.max(36, view * view / all);
    var travel = Math.max(1, view - h);
    var target = (clientY - h / 2) / travel * (all - view);
    target = Math.max(0, Math.min(all - view, target));
    lastStep = target - wanted;
    wanted = target;
    if (!raf) raf = requestAnimationFrame(follow);
  }

  bar.addEventListener('pointerdown', function (e) {
    dragging = true;
    wanted = scrollY;
    lastStep = 0;
    thumb.classList.add('on');
    bar.setPointerCapture(e.pointerId);
    aim(e.clientY);
    e.preventDefault();
  });
  bar.addEventListener('pointermove', function (e) {
    if (dragging) aim(e.clientY);
  });
  function release(e) {
    if (!dragging) return;
    dragging = false;
    thumb.classList.remove('on');
    try { bar.releasePointerCapture(e.pointerId); } catch (err) {}
    window.scrollTo(0, wanted);
  }
  bar.addEventListener('pointerup', release);
  bar.addEventListener('pointercancel', release);

  addEventListener('scroll', layout, {passive: true});
  addEventListener('resize', layout);
  window.__layout = layout;

  // Scrolling itself is the browser's. This only reports it, throttled,
  // because the category page loads more rows near the bottom.
  var ticking = false;
  addEventListener('scroll', function () {
    if (ticking) return;
    ticking = true;
    setTimeout(function () {
      ticking = false;
      var left = document.body.scrollHeight - scrollY - innerHeight;
      if (left < 1400) tell('more');
    }, 120);
  }, { passive: true });
})();
</script>"""


# **Covers come over a registered scheme, not as data and not as files.**
# See scratch cover_scheme.py: a data URL costs 28KB of JavaScript per
# cover on the main thread, and a file:// URL is refused outright because
# the page's own origin (`atomic:grid`) is opaque - which is why the
# first build showed no pictures at all. A scheme is fetched by Chromium
# itself, off the main thread, like any other image.
COVER_SCHEME = b"atomicimg"


SCHEME_READY = False


def register_cover_scheme():
    """Declare the scheme. Must run before QApplication exists, so it is
    called from helpers/__init__; calling it twice is harmless.

    **The result is recorded and logged**, because the previous build
    shipped with every cover a black rectangle: the scheme had not
    registered in the frozen app and every image 404'd, while the same
    code checked out from source. `SCHEME_READY` is what set_cover reads
    to decide whether it may use a URL at all."""
    global SCHEME_READY
    try:
        from PyQt6.QtWebEngineCore import QWebEngineUrlScheme
    except Exception:
        SCHEME_READY = False
        return False
    try:
        if not QWebEngineUrlScheme.schemeByName(COVER_SCHEME).name():
            scheme = QWebEngineUrlScheme(COVER_SCHEME)
            scheme.setSyntax(QWebEngineUrlScheme.Syntax.Host)
            scheme.setFlags(
                QWebEngineUrlScheme.Flag.SecureScheme
                | QWebEngineUrlScheme.Flag.LocalAccessAllowed
                | QWebEngineUrlScheme.Flag.ContentSecurityPolicyIgnored)
            QWebEngineUrlScheme.registerScheme(scheme)
        SCHEME_READY = bool(
            QWebEngineUrlScheme.schemeByName(COVER_SCHEME).name())
    except Exception:
        SCHEME_READY = False
    try:
        from . import logs
        logs.info(f"web_grid: cover scheme registered={SCHEME_READY}")
    except Exception:
        pass
    return SCHEME_READY


class _CoverHandler(QWebEngineUrlSchemeHandler):
    """Serves every grid: the page at `atomicimg://grid/<id>` and covers
    at `atomicimg://c/<id>/<index>`.

    **One handler for the whole application, not one per widget.** A
    profile keeps a single handler per scheme, so a second grid installed
    over the first and destroying either took the live one with it -
    after which the page URL answered nothing and the grid came up blank.
    That is the owner's "they load at first but when I change the page
    and go back it goes totally empty". The grids register themselves
    here by id and are held weakly, so a closed page cannot keep one
    alive and cannot break the next one either.
    """

    def requestStarted(self, job):
        url = job.requestUrl()
        parts = [p for p in url.path().split("/") if p]
        owner = None
        if parts:
            try:
                owner = _grid_by_id(int(parts[0]))
            except ValueError:
                owner = None
        if owner is None:
            job.fail(QWebEngineUrlRequestJob.Error.UrlNotFound)
            return
        if url.host() == "grid":
            body = (owner._html or "").encode("utf-8")
            if not body:
                job.fail(QWebEngineUrlRequestJob.Error.UrlNotFound)
                return
            buffer = QBuffer(job)
            buffer.setData(QByteArray(body))
            buffer.open(QIODevice.OpenModeFlag.ReadOnly)
            job.reply(b"text/html; charset=utf-8", buffer)
            return
        try:
            generation, index = int(parts[1]), int(parts[2])
        except (IndexError, ValueError):
            generation, index = -1, -1
        if generation != getattr(owner, "_gen", 0):
            # A request left over from the section before this one.
            job.fail(QWebEngineUrlRequestJob.Error.UrlNotFound)
            return
        blob = owner._cover_bytes.get(index)
        if not blob:
            owner._served_missing += 1
            job.fail(QWebEngineUrlRequestJob.Error.UrlNotFound)
            return
        owner._served_ok += 1
        buffer = QBuffer(job)
        buffer.setData(QByteArray(blob))
        buffer.open(QIODevice.OpenModeFlag.ReadOnly)
        job.reply(b"image/png", buffer)


_GRIDS = {}
_NEXT_ID = [0]
_HANDLER = [None]


def _grid_by_id(gid):
    ref = _GRIDS.get(gid)
    return ref() if ref is not None else None


def _install_handler(profile):
    """Once per process, on the profile every grid shares."""
    if _HANDLER[0] is None:
        _HANDLER[0] = _CoverHandler()
        profile.installUrlSchemeHandler(COVER_SCHEME, _HANDLER[0])
    return _HANDLER[0]


class _EncodeSignals(QObject):
    """A worker cannot touch the widget; it emits this instead."""

    done = Signal(int, bytes)


class _EncodeCover(QRunnable):
    """One cover, PNG-encoded off the UI thread. See
    WebPosterGrid.set_cover for why this is not done inline."""

    def __init__(self, signals, index, image):
        super().__init__()
        self.setAutoDelete(True)
        self._signals = signals
        self._index = index
        self._image = image

    def run(self):
        blob = b""
        try:
            buffer = QBuffer()
            buffer.open(QIODevice.OpenModeFlag.WriteOnly)
            if self._image.save(buffer, "PNG"):
                blob = bytes(buffer.data())
            buffer.close()
        except Exception:
            blob = b""
        try:
            self._signals.done.emit(self._index, blob)
        except Exception:
            pass            # the grid went away mid-encode


class _Page(QWebEnginePage):
    """The page's console is not this app's log."""

    def javaScriptConsoleMessage(self, level, message, line, source):
        return


class WebPosterGrid(QWebEngineView):
    """A poster grid that is a web page. See the module docstring."""

    clicked = Signal(int)
    needs_cover = Signal(int)
    scrolled = Signal()

    def __init__(self, cover_size=None, ground=None, parent=None):
        super().__init__(parent)
        self._records = []
        self._ready = False
        self._queued = []
        # The encoded covers this grid is serving, by index - handed to
        # Chromium by _CoverHandler when the page asks for them.
        self._cover_bytes = {}
        # The page's own body, served by _CoverHandler. Defined
        # before the handler can possibly be asked for it.
        self._html = ""
        # **Bumped on every fill, and it is in every cover URL.** The
        # owner, 31 August 2026: going Movies -> Anime inside the same
        # page showed the Movies pictures, while reaching Anime via
        # another page was correct. Both sections reuse this widget, so
        # the covers had identical URLs (same grid id, same index) and
        # Chromium served the ones it had already cached. A generation
        # makes each fill's URLs new, and the cache cannot answer for the
        # section before it.
        self._gen = 0
        self._encoding = set()
        # What the handler actually answered, so a frozen build
        # can be asked instead of assumed - see set_items.
        self._served_ok = 0
        self._served_missing = 0
        # Two workers: enough to keep ahead of a scroll, few enough that
        # encoding never competes with the page for the machine.
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(2)
        self._signals = _EncodeSignals(self)
        self._signals.done.connect(self._on_encoded)
        width, height = (cover_size or (CARD_W, CARD_H))
        self._cover_size = (int(width), int(height))
        # **Not a native window.** Making this view native was an
        # attempt to have Chromium present straight to its own HWND and
        # keep Qt's UI thread out of the path. It works when the view is
        # a top-level window and it is exactly when the owner's Anime
        # page went blank: as a *child*, the native surface is not
        # composited into the layout and the page renders where nothing
        # is shown. The DOM is fine throughout, which is why three checks
        # that asked the DOM all passed - they never asked whether any
        # pixel had changed.
        self.setPage(_Page(self))
        import weakref
        _NEXT_ID[0] += 1
        self._gid = _NEXT_ID[0]
        _GRIDS[self._gid] = weakref.ref(self)
        self.destroyed.connect(
            lambda *_a, gid=self._gid: _GRIDS.pop(gid, None))
        _install_handler(self.page().profile())
        # The page speaks through its title - see the note on `tell` in
        # the shell for why not a URL and not a web channel.
        self.page().titleChanged.connect(self._on_message)
        self.page().setBackgroundColor(
            self.page().backgroundColor().__class__(ground or theme.BG))
        self.loadFinished.connect(self._on_ready)
        # Built here, served by _CoverHandler - not handed to setHtml.
        # See that class for the two ways setHtml got the origin wrong.
        self._html = _SHELL % {
            "bg": ground or theme.BG,
            "surface": theme.SURFACE,
            "hover": theme.SURFACE_HOVER,
            "border": theme.BORDER,
            "accent": theme.ACCENT,
            "text": theme.TEXT,
            "dim": theme.TEXT_DIM,
            "shade": theme.BG,
            "cw": self._cover_size[0] + 16,
            "iw": self._cover_size[0],
            "ih": self._cover_size[1],
            # cover + title + meta + padding, so a card that
            # has not been realised still measures right.
            "ch": self._cover_size[1] + 66,
        }
        self.setUrl(QUrl(f"atomicimg://grid/{self._gid}"))

    def _on_message(self, title):
        """One message from the page: `atomic:<body>#<counter>`."""
        title = str(title or "")
        if not title.startswith("atomic:"):
            return
        body = title[len("atomic:"):].split("#", 1)[0]
        try:
            if body.startswith("pick/"):
                self.clicked.emit(int(body[5:]))
            elif body.startswith("cover/"):
                for part in body[6:].split(","):
                    if part:
                        self.needs_cover.emit(int(part))
            elif body == "more":
                self.scrolled.emit()
        except Exception:
            pass            # a malformed message must never break the page

    # ---- the painted grid's API -------------------------------------
    def count(self) -> int:
        return len(self._records)

    def record(self, index):
        """One record, the way the painted grid hands them out -
        `_refill_grid` carries covers across a refill with this."""
        try:
            return self._records[index]
        except (IndexError, TypeError):
            return {}

    def set_items(self, records, keep_position=False):
        self._records = [dict(r) for r in records]
        self._keep_position = bool(keep_position)
        self._gen += 1          # see _gen: new URLs, no stale cache hits
        try:
            from . import logs
            logs.info(f"web_grid: covers served ok={self._served_ok} "
                      f"missing={self._served_missing} "
                      f"encoded={len(self._cover_bytes)}")
        except Exception:
            pass
        self._cover_bytes.clear()
        self._encoding.clear()
        self._run(f"window.resetCards({str(bool(keep_position)).lower()})")
        if self._records:
            self._push(0, self._records)

    def append_items(self, records):
        first = len(self._records)
        rows = [dict(r) for r in records]
        self._records.extend(rows)
        self._push(first, rows)

    def set_cover(self, index, pixmap, generation=None):
        """Take this cover and have it encoded away from the UI thread.

        **Unless it belongs to the section before.** `generation` is the
        value of `_gen` when the fetch was started (see
        tracker._GridCoverSlot). A cover arriving after the grid has been
        refilled is addressed by an index that now means a different
        title - which is how a Manga page came to show Manhwa covers.

        **The encode is what made the wheel lag.** Doing it here, inline,
        put Qt's event loop at a p99 of 18.5ms and a worst of 50.4ms
        while scrolling, against 1.07ms idle - and a QWebEngineView
        presents through that same thread, so every one of those
        milliseconds is a frame Chromium had already drawn and nobody
        saw. See scratch offthread_covers.py for the measurement.

        QPixmap belongs to the UI thread; QImage does not, and converting
        is cheap because the buffer is shared. The worker encodes, and
        the page hears about the cover when its bytes exist.
        """
        if generation is not None and generation != self._gen:
            return
        if pixmap is None or index < 0 or index >= len(self._records):
            return
        index = int(index)
        if index in self._cover_bytes or index in self._encoding:
            return              # already served or already on its way
        try:
            image = pixmap.toImage()
        except Exception:
            return
        self._encoding.add(index)
        self._pool.start(_EncodeCover(self._signals, index, image))

    def _on_encoded(self, index, blob):
        """Bytes back from a worker: keep them and point the page at the
        scheme. Runs on the UI thread, and does nothing but a dict write
        and a short string."""
        self._encoding.discard(index)
        if not blob or index >= len(self._records):
            return
        self._cover_bytes[index] = bytes(blob)
        if SCHEME_READY:
            self._run(f"window.setCover({index}, "
                      f"'atomicimg://c/{self._gid}/{self._gen}/{index}')")
            return
        # **No scheme, so the bytes go inline.** This is the slow road -
        # a cover is ~28KB as a base64 string - but it cannot 404, and a
        # black grid is worse than a heavier one. The encode already
        # happened on a worker, so this costs the string and nothing
        # else. See register_cover_scheme for why this path exists.
        import base64
        url = "data:image/png;base64," + base64.b64encode(blob).decode("ascii")
        self._run(f"window.setCover({index}, {json.dumps(url)})")

    def set_scroll_offset(self, value):
        self._run(f"window.scrollTo(0, {float(value):.1f})")

    def reset_scroll(self):
        self.set_scroll_offset(0)

    # ---- talking to the page ----------------------------------------
    def _on_ready(self, ok):
        self._ready = bool(ok)
        queued, self._queued = self._queued, []
        for script in queued:
            self._run(script)

    def _run(self, script):
        """Queued until the shell has loaded - set_items can be called
        before the page is up, and dropping those rows would leave an
        empty grid that never fills."""
        if not self._ready:
            self._queued.append(script)
            return
        try:
            self.page().runJavaScript(script)
        except Exception:
            pass

    def _push(self, first, rows):
        payload = []
        for offset, record in enumerate(rows):
            year = str(record.get("year") or "").strip()
            rating = str(record.get("rating") or "").strip()
            meta = "  ".join(part for part in (year, rating) if part)
            payload.append({"i": first + offset,
                            "title": str(record.get("title") or ""),
                            "meta": meta,
                            "saved": bool(record.get("saved"))})
        if payload:
            self._run(f"window.addCards({json.dumps(payload)})")
