"""A Qt widget whose inside is a WebView2 - Edge's engine, not Qt's.

**Why this exists.** Two weeks went into making a QScrollArea scroll like
Stremio's before the cause was found, and it was never the scroll model:
Stremio has no scroll code at all. Its smoothness is the browser's, and
which browser decides it. Measured on the owner's 240Hz panel,
QtWebEngine - which this app already bundles - reached 151fps with smooth
scrolling on and the frame cap lifted, because Chromium draws into a
texture that Qt then composites. WebView2 presents through Edge's own
compositor and never enters Qt's paint path. His verdict on the same
pages was "better even than stremio".

**Proven by pixels before anything was built on it**, 31 August 2026: a
WebView2 parented into a Qt widget put 66.6% of the Qt window's client
area on screen as the web page - 57.2% of it the page's row colour, 9.4%
its background - with an ordinary Qt label above it in the same layout.
The check was pixels rather than an absent exception on purpose: a view
that reports "loaded" while compositing nowhere is a failure this project
has already shipped once, when WA_NativeWindow made a child view paint
into nothing and every DOM check still passed.

**The native-child rule applies exactly as it does to the video
surface.** This is a real native window, and on Windows a native child
paints above every non-native sibling whatever raise_() was told - so
anything that must appear over one (a toast, a dialog, a floating
control) has to be native too, or it is drawn every frame underneath and
never once seen.
"""

import ctypes
import ctypes.wintypes as w
import json
import pathlib
import sys
import time

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import QWidget

from . import logs

# Loaded lazily and never at import: a build without pythonnet, or a
# machine without the WebView2 runtime, must fall back to the Qt page
# rather than fail to start. available() is the question to ask.
_forms = None
_control = None
_load_error = ""


def _lib_dir():
    """Where the WebView2 .NET assemblies are.

    Frozen, they are unpacked to sys._MEIPASS and `webview.__file__`
    points inside the archive where nothing can be opened - so the bundle
    is asked first and the installed package is only the development
    case, exactly as video_backend finds libmpv.
    """
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        bundled = pathlib.Path(meipass) / "webview" / "lib"
        if bundled.is_dir():
            return bundled
    import webview
    return pathlib.Path(webview.__file__).parent / "lib"


def _load():
    global _forms, _control, _load_error
    if _forms is not None or _load_error:
        return
    try:
        import clr
        for name in ("Microsoft.Web.WebView2.Core",
                     "Microsoft.Web.WebView2.WinForms"):
            clr.AddReference(str(_lib_dir() / name))
        import System.Windows.Forms as winforms
        from Microsoft.Web.WebView2.WinForms import WebView2
        _forms, _control = winforms, WebView2
    except Exception as error:
        _load_error = str(error)
        logs.info(f"webview2_host unavailable: {_load_error[:160]}")


def available() -> bool:
    _load()
    return _forms is not None


def unavailable_reason() -> str:
    _load()
    return _load_error


_u = ctypes.WinDLL("user32", use_last_error=True)
# Every handle call gets a restype and argtypes. They default to c_int,
# which truncates a 64-bit HWND - the trap that silently broke a screen
# sampler in this repository once already.
_u.SetParent.restype = w.HWND
_u.SetParent.argtypes = [w.HWND, w.HWND]
_u.GetWindowLongPtrW.restype = ctypes.c_longlong
_u.GetWindowLongPtrW.argtypes = [w.HWND, ctypes.c_int]
_u.SetWindowLongPtrW.restype = ctypes.c_longlong
_u.SetWindowLongPtrW.argtypes = [w.HWND, ctypes.c_int, ctypes.c_longlong]
_u.SetWindowPos.argtypes = [w.HWND, w.HWND, ctypes.c_int, ctypes.c_int,
                            ctypes.c_int, ctypes.c_int, w.UINT]

GWL_STYLE = -16
GWL_EXSTYLE = -20
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOOLWINDOW = 0x00000080
WS_CHILD = 0x40000000
WS_VISIBLE = 0x10000000
WS_POPUP = 0x80000000
WS_CAPTION = 0x00C00000
WS_THICKFRAME = 0x00040000
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_DEFERERASE = 0x2000
SWP_NOSIZE = 0x0001

_pump_timer = None


def _start_pump():
    """Turn WinForms' own message pump, once, for the whole app.

    WebView2 finishes its asynchronous initialisation on WinForms posted
    work, which Qt's event loop does not run - without this the view is
    created, parented, sized, and never navigates.

    A QTimer is correct here and not affected by the player: mpv damages
    Qt timer *delivery rate*, and this only has to run often enough to
    drain a queue, not to pace an animation.
    """
    global _pump_timer
    if _pump_timer is not None or _forms is None:
        return
    _pump_timer = QTimer()
    _pump_timer.timeout.connect(_pump_once)
    _pump_timer.start(8)


def _pump_once():
    try:
        _forms.Application.DoEvents()
    except Exception:
        pass


class WebView2Page(QWidget):
    """One page, rendered and scrolled by Edge inside a Qt layout."""

    # Read by main's cursor watchdog: everything inside this widget is
    # drawn by Edge, which sets the pointer itself. Qt's idea of the
    # cursor here is the default arrow, and enforcing it fights the
    # page's - the flicker the owner reported over the banner.
    owns_cursor = True

    # What the page asked the app to do - a card was clicked. Carried as
    # a dict rather than positional arguments so the page can add a field
    # without every connection having to change.
    message = pyqtSignal(dict)

    def __init__(self, url="", parent=None):
        super().__init__(parent)
        self._form = None
        self._view = None
        self._child = 0
        self._sized = (0, 0)
        self._suppressed = False
        self._loaded = False
        self._last_fit = 0.0
        self._url = url

        if not available():
            return
        _start_pump()

        # Fires once after a run of resizes stops - see resizeEvent.
        self._settle = QTimer(self)
        self._settle.setSingleShot(True)
        self._settle.timeout.connect(self._fit)

        # Native first, winId() *after*. Read the other way round and the
        # handle is one Qt later replaces, and the content ends up in its
        # own detached window - the rule the video surface follows too.
        self.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, True)
        self.setAttribute(Qt.WidgetAttribute.WA_DontCreateNativeAncestors,
                          True)
        self._host = int(self.winId())

        try:
            self._form = _forms.Form()
            # getattr: the WinForms enum member really is called None,
            # and `.None` is a syntax error in Python.
            self._form.FormBorderStyle = getattr(_forms.FormBorderStyle,
                                                 "None")
            self._form.BackColor = _forms.Form().BackColor
            try:
                from System.Drawing import Color
                # The app's --bg, so a frame before the page
                # paints is dark rather than a white flash.
                self._form.BackColor = Color.FromArgb(10, 14, 22)
            except Exception:
                pass
            self._form.ShowInTaskbar = False
            self._form.StartPosition = _forms.FormStartPosition.Manual
            # Off the side of every monitor until it is adopted in
            # _ready. It has to be a *shown* window for the controller to
            # be built against it, and a visible one here would flash.
            self._form.SetBounds(-32000, -32000, 900, 700)
            self._view = _control()
            self._view.Dock = _forms.DockStyle.Fill
            self._form.Controls.Add(self._view)

            # **Shown without taking the foreground.** Form.Show()
            # activates, and this app builds a page - and therefore a
            # host window - whenever one is opened over another. The new
            # top-level window stealing focus is what made opening a
            # title look like the whole app reopening; measured
            # 1 September 2026, the main window was no longer the
            # foreground window immediately after a details page opened.
            #
            # Reading .Handle creates the window without showing it, so
            # the style can be set before it is ever on screen, and
            # SW_SHOWNA then shows it without activation.
            handle = int(self._form.Handle.ToInt64())
            ex = _u.GetWindowLongPtrW(handle, GWL_EXSTYLE)
            _u.SetWindowLongPtrW(handle, GWL_EXSTYLE,
                                 ex | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW)
            _u.ShowWindow(ctypes.c_void_p(handle), 8)      # SW_SHOWNA

            self._view.CoreWebView2InitializationCompleted += self._ready
            self._view.EnsureCoreWebView2Async(None)
        except Exception:
            logs.exception("Creating the WebView2 page failed")
            self._form = self._view = None
            self._child = 0

    def ok(self) -> bool:
        return bool(self._child)

    def set_child_visible(self, visible: bool):
        """Show or hide the native child window itself.

        **Not optional, and not cosmetic.** On Windows a native child
        paints above every non-native sibling whatever raise_() was told
        - the same rule that kept the player's loading logo invisible
        underneath the video surface for months. So an overlay opened
        over this page is drawn *behind* it, and what the user sees is
        the old page with only the sidebar changing.

        Hidden *and* moved off every monitor. Hiding alone was not
        enough: widgets.freeze_covered disables updates on the covered
        siblings, other code shows and hides pages around it, and any
        one of those paths brought the child straight back over the
        overlay - a white block where the page had been. A window parked
        at -32000 cannot come back by accident.
        """
        if not self._child:
            return
        child = ctypes.c_void_p(self._child)
        try:
            if visible:
                _u.ShowWindow(child, 5)          # SW_SHOW
                self._sized = (0, 0)             # force the next _fit
                self._fit()
            else:
                _u.ShowWindow(child, 0)          # SW_HIDE
                _u.SetWindowPos(child, None, -32000, -32000, 0, 0,
                                SWP_NOZORDER | SWP_NOACTIVATE
                                | SWP_DEFERERASE | SWP_NOSIZE)
                self._sized = (0, 0)
        except Exception:
            return
        if not visible:
            # **Repaint what the child was covering.** Qt never draws
            # under a native child - it excludes that region - so the
            # pixels it last painted stay on screen until something
            # explicitly repaints them.
            try:
                parent = self.parentWidget()
                if parent is not None:
                    parent.update(self.geometry())
                self.update()
            except Exception:
                pass

    def suppress(self, on: bool):
        """Hold the view down while an overlay is up.

        Separate from set_child_visible because showEvent also calls
        that, and an overlay that opens while Qt happens to re-show this
        page put the view straight back on top.
        """
        self._suppressed = bool(on)
        self.set_child_visible(not self._suppressed and self._loaded)
        # **And the Qt widget itself, which is the part that mattered.**
        # This widget is a native window (WA_NativeWindow), and Qt
        # excludes a native child's rectangle from the top-level's own
        # painting - so anything drawn behind it, an overlay included,
        # simply has no pixels there. Hiding the *content* inside it
        # changes nothing about that exclusion; only hiding this widget
        # does. Measured 1 September 2026: the details page's geometry
        # was the full (0, 0, 1934, 1001) while only its left 296px had
        # been painted, and the missing region was exactly this widget.
        try:
            if self._suppressed:
                if self.isVisible():
                    self.setVisible(False)
            elif not self.isVisible():
                self.setVisible(True)
        except RuntimeError:
            pass

    def showEvent(self, event):
        super().showEvent(event)
        if self._suppressed or not self._loaded:
            return       # an overlay is up, or there is nothing to show
        self.set_child_visible(True)

    def hideEvent(self, event):
        super().hideEvent(event)
        # Navigating to another page hides this widget, but a native
        # child is not hidden with its parent - it would carry on
        # painting over whatever replaced it.
        self.set_child_visible(False)

    def _alive(self) -> bool:
        """Whether the Qt half of this widget still exists.

        Initialisation is asynchronous and a page can be navigated away
        from before it finishes - Qt deletes the widget, and the
        completion handler then touches a dead C++ object:
        "wrapped C/C++ object of type WebView2Page has been deleted",
        raised inside a slot, which aborts the process rather than
        merely failing.
        """
        try:
            from PyQt6 import sip
            return not sip.isdeleted(self)
        except Exception:
            return True

    def _ready(self, _sender, args):
        if not self._alive():
            return
        # **Ask whether it worked.** The event fires on failure too, and
        # CoreWebView2 is then None - which surfaced as
        # "'NoneType' object has no attribute 'WebMessageReceived'" and
        # said nothing about the actual cause. The exception the runtime
        # hands back does.
        try:
            if args is not None and not bool(args.IsSuccess):
                reason = ""
                try:
                    reason = str(args.InitializationException)
                except Exception:
                    pass
                logs.info(f"WebView2 init failed: {reason[:300]}")
                return
        except Exception:
            pass
        try:
            core = self._view.CoreWebView2
            if core is None:
                logs.info("WebView2 init completed with no core")
                return

            # **Adopted only now.** Re-parenting the form before this
            # point destroys the very HWND the controller is being built
            # against - WinForms recreates a control's handle when its
            # parent changes - and creation then fails with E_ABORT
            # (0x80004004). That is exactly what the frozen build did,
            # while running from source happened to win the race.
            child = int(self._form.Handle.ToInt64())
            style = _u.GetWindowLongPtrW(child, GWL_STYLE)
            style = (style & ~WS_POPUP & ~WS_CAPTION & ~WS_THICKFRAME) \
                | WS_CHILD | WS_VISIBLE
            _u.SetWindowLongPtrW(child, GWL_STYLE, style)
            _u.SetParent(child, self._host)
            self._child = child
            self._fit()

            # **Keys the app owns, not the page.** A WebView2 takes the
            # keyboard while it has focus, so F11 reached Edge and never
            # Qt - the owner's "F11 does not work like I am clicking on
            # another app". AcceleratorKeyPressed sees them first.
            try:
                self._view.AcceleratorKeyPressed += self._accelerator
            except Exception:
                logs.info("WebView2: accelerator keys unavailable")
            core.NavigationCompleted += self._navigated
            core.WebMessageReceived += self._got_message
            # Nothing in these pages needs a context menu, a browser
            # zoom, or dev tools; leaving them on is how an embedded view
            # stops looking like part of the app.
            core.Settings.AreDefaultContextMenusEnabled = False
            core.Settings.AreDevToolsEnabled = False
            core.Settings.IsZoomControlEnabled = False
            core.Settings.IsStatusBarEnabled = False
            if self._url:
                core.Navigate(self._url)
        except Exception:
            logs.exception("Preparing the WebView2 page failed")

    # Keys that belong to the window rather than to the page. F11 is
    # full screen and Escape leaves it; both are the app's, everywhere
    # else in it.
    _APP_KEYS = {0x7A: "F11", 0x1B: "Escape"}

    def _accelerator(self, _sender, args):
        try:
            if int(args.KeyEventKind) not in (0, 2):     # KeyDown, SysKeyDown
                return
            name = self._APP_KEYS.get(int(args.VirtualKey))
            if not name:
                return
            args.Handled = True
            self.message.emit({"action": "key", "key": name})
        except Exception:
            logs.exception("A web page key could not be forwarded")

    def _navigated(self, _sender, args):
        """The page has content; only now is it safe to show.

        A WebView2 that has not loaded anything paints white, and this
        app rebuilds a page from scratch on every visit - so a newly
        built view showed a white rectangle over whatever had just
        opened, which is the block the owner reported three times. Kept
        hidden until here, it shows nothing at all instead.
        """
        # **Only a navigation that actually arrived counts.** A second
        # Navigate cancels the first, and NavigationCompleted then fires
        # with IsSuccess false - so marking the view loaded here
        # regardless showed an empty document. That is what left a
        # converted watch/read page blank while the view reported
        # loaded, visible and uncovered: WebTrackerPage.set_active_section
        # navigates moments after construction, cancelling the first.
        ok = True
        try:
            ok = bool(args.IsSuccess)
        except Exception:
            pass
        if not ok:
            try:
                core = self._view.CoreWebView2
                if core is not None and self._url:
                    core.Navigate(self._url)
            except Exception:
                logs.exception("Could not retry the page")
            return

        self._loaded = True
        if self._suppressed:
            return              # an overlay is up; stay hidden
        self.set_child_visible(True)

    def _got_message(self, _sender, args):
        """A click in the page. Never trusted as anything but data."""
        try:
            raw = args.TryGetWebMessageAsString()
            body = json.loads(raw)
            if isinstance(body, dict):
                self.message.emit(body)
        except Exception:
            logs.exception("A web page message could not be read")

    def show_url(self, url):
        """Point the view at `url`.

        A URL differing only in its fragment is a same-document
        navigation: Chromium fires hashchange rather than reloading, and
        the page listens for it (app.js). Navigate is still the right
        call - it is what makes the browser fire that event at all.
        """
        self._url = url
        try:
            if self._view is not None and self._view.CoreWebView2 is not None:
                self._view.CoreWebView2.Navigate(url)
        except Exception:
            pass                     # not initialised yet; _ready will run

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # **Coalesced, because the sidebar fold sends one of these per
        # animation frame.** Each one is a SetWindowPos on a native
        # window plus a full re-layout inside Edge, and at 60-240 frames
        # a second most of that work is thrown away by the next frame.
        # The size is applied at most every 33ms during the burst, and
        # once more when it stops so the final size is always exact.
        now = time.monotonic()
        if now - self._last_fit >= 0.033:
            self._last_fit = now
            self._fit()
        self._settle.start(120)

    def _fit(self):
        if not self._alive():
            return
        """Size the native child to this widget, in device pixels.

        Device, not logical: the child window is sized in real pixels
        while Qt's width()/height() are logical, and mixing the two
        leaves the view short of the widget on any display above 100%.
        """
        if not self._child:
            return
        # Device pixels: the child window is sized in them while Qt's
        # width()/height() are logical, and mixing the two leaves the
        # view short of the widget on any display above 100%.
        ratio = self.devicePixelRatioF()
        width = int(self.width() * ratio)
        height = int(self.height() * ratio)
        if (width, height) == self._sized:
            return              # the fold sends many resizes per frame
        self._sized = (width, height)
        # NOACTIVATE and DEFERERASE as well as NOZORDER: the sidebar fold
        # resizes this on every frame of its animation, and the default
        # flags make Windows erase the background and re-activate on
        # each one - work that is thrown away 60+ times a second and is
        # felt as the cards stuttering as the rail opens.
        _u.SetWindowPos(self._child, None, 0, 0, width, height,
                        SWP_NOZORDER | SWP_NOACTIVATE | SWP_DEFERERASE)
