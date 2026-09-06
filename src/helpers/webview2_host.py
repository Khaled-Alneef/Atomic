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

from PyQt6.QtCore import QEvent, QPoint, Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import QWidget

from . import logs

# Loaded lazily and never at import: a build without pythonnet, or a
# machine without the WebView2 runtime, must fall back to the Qt page
# rather than fail to start. available() is the question to ask.
_forms = None
_control = None
_load_error = ""
# A one-element box rather than a second `global` name: _load() sets it
# and __init__ reads it, and a plain module global reassigned from a
# nested scope needs its own `global` declaration anyway - this needs
# none.
_creation_properties = [None]


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


# **Chromium's wheel animation, off.** The owner, 1 September 2026:
# "there is a delay between start scrolling in the wheel and the page
# starts scrolling really", and "when I scroll using the laptop touchpad
# it is super smooth, but the mouse wheel ... not smooth in comparison".
#
# Both are the same mechanism, and it is the browser's, not this app's -
# app.js takes no wheel events at all. Chromium animates a *mouse* notch
# over a curve (cc::ScrollOffsetAnimationCurve, the same one app.js
# copies for its sideways rows) while a precision touchpad's pixel
# deltas are applied on the frame they arrive. So the touchpad has been
# showing the untouched path and the wheel the animated one - exactly the
# split widgets._SmoothWheel produced on the Qt side, for exactly the
# same reason.
#
# Measured 1 September 2026 on Home, one real wheel notch driven through
# SendInput and scrollTop read from inside the page, two control runs:
#
#                        animation ON          animation OFF
#   first movement       ~35 ms after          ~18 ms after
#   frames for one notch 42, over ~660 ms      1
#   step size            0.8-6.4 px, med 2.4   100 px
#
# So a notch was being dribbled out over two thirds of a second in
# forty-two sub-pixel-ish steps. That is the delay, and the reason a
# touchpad felt better is that its deltas skipped all of it.
#
# Set through the environment variable rather than by building a
# CoreWebView2Environment by hand: WebView2 reads it when it creates the
# default environment, so it needs none of the async plumbing that
# CreateAsync would drag in here, and a runtime that ignores the flag
# simply scrolls as it did before.
_BROWSER_ARGS = "--disable-smooth-scrolling"

# How many times a navigation that did not arrive is asked for again
# before the view gives up and says so - see _navigated.
NAV_RETRIES = 3


def _load():
    global _forms, _control, _load_error
    if _forms is not None or _load_error:
        return
    try:
        import os
        existing = os.environ.get("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS", "")
        if _BROWSER_ARGS not in existing:
            os.environ["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = (
                f"{existing} {_BROWSER_ARGS}".strip())
        import clr
        for name in ("Microsoft.Web.WebView2.Core",
                     "Microsoft.Web.WebView2.WinForms"):
            clr.AddReference(str(_lib_dir() / name))
        import System.Windows.Forms as winforms
        from Microsoft.Web.WebView2.WinForms import (
            WebView2, CoreWebView2CreationProperties)
        _forms, _control = winforms, WebView2
        _creation_properties[0] = CoreWebView2CreationProperties
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
# Asked on every fit, so the child's real size is read from Windows
# rather than from a cache this process keeps - see _fit.
_u.GetClientRect.argtypes = [w.HWND, ctypes.POINTER(w.RECT)]
_u.GetClientRect.restype = ctypes.c_bool
_u.IsWindowVisible.argtypes = [w.HWND]
_u.IsWindowVisible.restype = ctypes.c_bool
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
SWP_NOMOVE = 0x0002

_pump_timer = None
_burst_timer = None

# **Every WinForms form and WebView2 control built here and not yet
# disposed**, keyed by the Qt widget's id. Nothing disposed them before
# 5 September 2026: a page is rebuilt from scratch on every visit
# (.claude/rules/ui.md) and the old page's Qt widget was deleted, but the
# .NET control and Edge's document under it lived on - the owner's log
# from another device proved it, with "wrapped C/C++ object of type
# WebView2Page has been deleted" raised inside _got_message: a document
# whose page had been deleted was still running its scripts and still
# posting messages. Thirteen views were built in twenty seconds of
# sidebar clicks there, every one of them orphaned, every one still
# fetching catalogue sweeps (app.js moreOnScroll keeps pulling until the
# source is dry) and still sharing Edge's renderer with the page he was
# actually looking at. That is where "the pages become empty, no buttons
# no grid nothing" came from: a live page whose go() had cleared the
# document and was waiting on a fetch, starved behind a dozen dead ones.
#
# So a view is disposed the moment its page is scheduled for deletion
# (event(), on DeferredDelete - before the HWND goes, which is the order
# WebView2 documents) and, failing that, when the Qt object is destroyed
# (the `destroyed` fallback below, which reaches the .NET objects through
# this table rather than through a wrapper Qt has already torn down).
_open = {}


def _dispose_handles(key):
    """Close and release one view's .NET objects. Idempotent: whichever
    of the two hooks gets here first does the work, the other finds
    nothing to do. Returns whether anything was disposed."""
    pair = _open.pop(key, None)
    if pair is None:
        return False
    form, view = pair
    try:
        if view is not None:
            try:
                view.Dispose()          # closes the CoreWebView2Controller
            except Exception:
                logs.exception("WebView2: the view could not be disposed")
        if form is not None:
            try:
                form.Dispose()
            except Exception:
                logs.exception("WebView2: the host form could not be disposed")
    finally:
        logs.info(f"WebView2: disposed, live={len(_open)}")
    return True


def live_views() -> int:
    """How many views exist right now - one per page on screen or
    scheduled behind an overlay; anything above that is a leak."""
    return len(_open)


def pump_burst(ms=60):
    """Drain WinForms' queue every millisecond for `ms`.

    The regular pump below turns every 8ms, which is fine for a click
    and too slow for an answer the window is standing still for: a
    page's reply to a sidebar fold (web_pages.offer_fold) sat in the
    WinForms queue for up to a pump interval before Qt heard it.
    Measured 3 September 2026, offer to ack, Watch page: 22.0ms with
    the 8ms pump alone, 4.7-8.4ms with this burst running.
    """
    global _burst_timer
    if _forms is None:
        return
    if _burst_timer is None:
        _burst_timer = QTimer()
        _burst_timer.setTimerType(Qt.TimerType.PreciseTimer)
        _burst_timer.timeout.connect(_pump_once)
    _burst_timer.start(1)
    QTimer.singleShot(int(ms), _burst_timer.stop)


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
            self._parked = True          # off screen until _navigated reveals it
            self._view = _control()
            # **Into %APPDATA%\Atomic, not beside the exe.** Left unset,
            # WebView2 creates its "EBWebView" profile folder next to
            # whatever launched it - the repo root for a source run, the
            # install directory for the frozen exe - which is where the
            # owner found it (25 September 2026 wasn't a real date, but
            # the ask was: keep every local file in one place, next to
            # settings.json and the caches). storage.DATA_DIR is that
            # place, already created before this ever runs.
            properties_cls = _creation_properties[0]
            if properties_cls is not None:
                try:
                    from . import storage
                    properties = properties_cls()
                    properties.UserDataFolder = str(
                        storage.DATA_DIR / "WebView2")
                    self._view.CreationProperties = properties
                except Exception:
                    logs.exception("WebView2 user data folder not set")
            self._view.Dock = _forms.DockStyle.Fill
            # **The control's own colour before anything is drawn.** A
            # WebView2 paints white until its first document paints -
            # the 25ms white frame measured above, and the same white
            # any resize or navigation shows for a frame. This is the
            # colour WebView2 documents for exactly that gap; the app's
            # ground, the same the form wears.
            try:
                from System.Drawing import Color
                self._view.DefaultBackgroundColor = Color.FromArgb(10, 14, 22)
            except Exception:
                logs.exception("WebView2 default background not set")
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
            # Registered only now, so a control that failed to build is
            # never "live". The fallback hook closes over the key, not
            # over self: by the time `destroyed` fires the Python wrapper
            # is already a dead object.
            self._key = id(self)
            _open[self._key] = (self._form, self._view)
            key = self._key
            self.destroyed.connect(lambda *_args: _dispose_handles(key))
        except Exception:
            logs.exception("Creating the WebView2 page failed")
            self._form = self._view = None
            self._child = 0

    def ok(self) -> bool:
        return bool(self._child)

    def dispose(self):
        """Close Edge's document and release the .NET control now.

        Called by the page that owns this view when that page is
        scheduled for deletion (windows/web_pages and web_reader override
        event() for DeferredDelete), which is before Qt destroys the
        native window the control is parented into - the order WebView2
        asks for. See `_open` for what happened when nothing called it.
        """
        self._child = 0
        self._loaded = False
        self._view = self._form = None
        key = getattr(self, "_key", None)
        if key is not None:
            _dispose_handles(key)

    def event(self, event):
        # A view deleted on its own (deleteLater on the view itself) is
        # disposed here; one deleted with its page is disposed by the
        # page's own hook, which fires first.
        try:
            if event.type() == QEvent.Type.DeferredDelete:
                self.dispose()
        except Exception:
            pass
        return super().event(event)

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
        # **Showing an already-shown window is not free, and this is
        # asked seven times a second.** windows/web_pages._check_covered
        # runs on a 150ms timer and calls suppress() on every tick
        # rather than on a change - deliberately, because no event
        # reliably says "something is covering me". Measured 2 September
        # 2026 on the Watch page: 27 SetWindowPos calls on the child
        # across four idle seconds, 6.75 a second, every one of them a
        # ShowWindow plus a full re-size of Edge's composition surface
        # for a window that was already visible at that size. Two of
        # them land inside a 220ms sidebar fold. After: 4 across the
        # same four seconds, and none at all during a fold.
        #
        # Only the visible case is skipped. Hiding stays unconditional:
        # the docstring above records that other paths bring the child
        # back over an overlay, and re-parking it is the insurance that
        # was measured to be needed.
        # **Asked of Windows, not of a flag this process keeps.** The
        # first version of this skipped on a `self._shown` boolean, and
        # a cached answer to an outside question is exactly the bug it
        # was meant to make cheaper: anything that hid or parked the
        # child by another route left the flag saying "shown", the
        # re-show was skipped, and the page stayed invisible until it
        # was rebuilt. That is the owner's "sometimes the Home page goes
        # empty suddenly then I need to change page then come back", and
        # the same shape as the size cache below it.
        #
        # IsWindowVisible is a syscall of about a microsecond, against
        # the ShowWindow plus full composition-surface resize it guards.
        if visible and _u.IsWindowVisible(self._child):
            # Shown already, so only the geometry may need correcting -
            # and _fit returns immediately when it does not.
            self._fit()
            return
        child = ctypes.c_void_p(self._child)
        try:
            if visible:
                _u.ShowWindow(child, 5)          # SW_SHOW
                self._sized = (0, 0)
                self._fit(force=True)            # also un-parks it
            else:
                _u.ShowWindow(child, 0)          # SW_HIDE
                _u.SetWindowPos(child, None, -32000, -32000, 0, 0,
                                SWP_NOZORDER | SWP_NOACTIVATE
                                | SWP_DEFERERASE | SWP_NOSIZE)
                self._parked = True
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
        if not self._alive() or self._view is None:
            return                  # deleted, or disposed, before Edge answered
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
            # **A crashed renderer was never noticed, let alone recovered
            # from** (5 September 2026, chasing "the app goes to empty
            # pages" - not on first launch only, and not reproduced on
            # this machine). WebView2 exposes exactly this as
            # ProcessFailed and nothing in this file was listening for
            # it - a render-process crash (a GPU driver fault is the
            # common real-world cause, and a laptop's hybrid graphics is
            # exactly where those show up) left the native child painting
            # nothing, forever, with no error and no way back short of
            # restarting Atomic. The sidebar and window chrome kept
            # working throughout, because they are Qt, not this control -
            # which is why the report was "empty pages", not "the app
            # froze".
            try:
                core.ProcessFailed += self._process_failed
            except Exception:
                logs.exception("WebView2 ProcessFailed unavailable")
            # Nothing in these pages needs a context menu, a browser
            # zoom, or dev tools; leaving them on is how an embedded view
            # stops looking like part of the app.
            core.Settings.AreDefaultContextMenusEnabled = False
            core.Settings.AreDevToolsEnabled = False
            core.Settings.IsZoomControlEnabled = False
            core.Settings.IsStatusBarEnabled = False
            # **A page region can move the window.** The owner, 6
            # September 2026: "make the window draggable from the upper
            # bar in the reader mode while in not fullscreen". The reader
            # covers the window's own bar (main.immersive_host), and the
            # bar it draws is inside Edge, where a press never reaches
            # Qt. WebView2's non-client region support is Microsoft's
            # answer: an element styled `app-region: drag` is treated as
            # the host window's caption and Windows runs its own move
            # loop, snap and all - the same thing window_chrome.
            # begin_window_drag gets from startSystemMove. Measured
            # present here: SDK 1.0.3856, runtime 152. A runtime without
            # it says so once and the bar simply does not drag.
            try:
                core.Settings.IsNonClientRegionSupportEnabled = True
            except Exception:
                logs.info("WebView2: non-client regions unavailable; "
                          "the reader's bar will not move the window")
            if self._url:
                core.Navigate(self._url)
        except Exception:
            logs.exception("Preparing the WebView2 page failed")

    def _process_failed(self, _sender, args):
        """A WebView2-owned process died. Said out loud - the kind names
        which one (RenderProcessExited is the everyday crash; a GPU
        driver fault is the common real cause) - and recovered where
        recovery is a single documented call.

        `Reload()` is Microsoft's own answer for a dead renderer or an
        unresponsive one: the controller and its CoreWebView2 survive,
        only the content process is gone, and Reload() gets a fresh one
        and re-navigates to what was showing. A dead *browser* process
        (BrowserProcessExited) takes the controller down with it - this
        widget's next `show_url` will find CoreWebView2 gone and fail
        quietly, same as before this existed. Rebuilding the whole
        control for that case is real surgery on a path nothing here has
        exercised against an actual crash; logging it plainly is the
        honest half of this fix until it is."""
        try:
            kind = str(getattr(args, "ProcessFailedKind", ""))
            reason = str(getattr(args, "Reason", ""))
            exit_code = getattr(args, "ExitCode", "")
            description = str(getattr(args, "ProcessDescription", ""))[:200]
            logs.info(f"WebView2 process failed: kind={kind} reason={reason} "
                      f"exit_code={exit_code} process={description}")
        except Exception:
            logs.exception("WebView2 ProcessFailed handler itself failed")
            kind = ""
        if not self._alive() or self._view is None:
            return
        if "RenderProcess" in kind:   # Exited or Unresponsive
            try:
                self._view.CoreWebView2.Reload()
                logs.info("WebView2: reloaded after a render process failure")
            except Exception:
                logs.exception("WebView2 reload after process failure failed")

    # Keys that belong to the window rather than to the page. F11 is
    # full screen and Escape leaves it; both are the app's, everywhere
    # else in it.
    _APP_KEYS = {0x7A: "F11", 0x1B: "Escape"}

    # **The rest of global_search.SHORTCUTS' "Anywhere" block.** The web
    # view holds the keyboard while it has focus, so on a web page none
    # of these reached the window at all - the owner's "re-add all of the
    # shortcuts and make them functional... as the settings keybinds
    # says". Only ever taken with their modifier held, so ordinary typing
    # in the page is untouched: bare F is a letter, Ctrl+F is the app's.
    _CTRL_KEYS = {
        0x46: "Ctrl+F", 0x4E: "Ctrl+N", 0x5A: "Ctrl+Z", 0x59: "Ctrl+Y",
        0xBC: "Ctrl+,",                                  # VK_OEM_COMMA
    }
    _CTRL_KEYS.update({0x30 + n: f"Ctrl+{n}" for n in range(1, 10)})
    _ALT_KEYS = {0x25: "Alt+Left", 0x27: "Alt+Right"}

    # Private handle with a declared signature - `ctypes.windll.user32`
    # is one process-wide cached object whose prototypes other code in
    # this app has already redeclared (see .claude/rules/testing.md).
    _user32 = ctypes.WinDLL("user32")
    _user32.GetKeyState.restype = ctypes.c_short
    _user32.GetKeyState.argtypes = [ctypes.c_int]

    @classmethod
    def _modifier_held(cls, virtual_key) -> bool:
        """Whether a modifier is down right now.

        WebView2's accelerator args carry the key and its physical
        status but not the modifier state, and the app is the foreground
        window whenever this fires, so asking Windows is both the
        simplest route and the correct one.
        """
        try:
            return bool(cls._user32.GetKeyState(virtual_key) & 0x8000)
        except Exception:
            return False

    def _accelerator(self, _sender, args):
        try:
            if int(args.KeyEventKind) not in (0, 2):     # KeyDown, SysKeyDown
                return
            code = int(args.VirtualKey)
            name = self._APP_KEYS.get(code)
            if name is None and self._modifier_held(0x11):        # VK_CONTROL
                name = self._CTRL_KEYS.get(code)
            if name is None and self._modifier_held(0x12):        # VK_MENU
                name = self._ALT_KEYS.get(code)
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
            # **Said out loud, and not retried forever.** A failed
            # arrival used to re-navigate silently, every time, so a
            # page that could not load left nothing in atomic.log and a
            # view that never showed - exactly what an empty page looks
            # like from the other side of the glass (5 September 2026,
            # the owner's log from another device had nothing to say).
            # The status is Edge's own word for why; the cap stops a
            # dead server from being asked at full tilt for the rest of
            # the session.
            status = ""
            try:
                status = str(args.WebErrorStatus)
            except Exception:
                pass
            self._nav_failures = getattr(self, "_nav_failures", 0) + 1
            logs.info(f"WebView2: navigation failed ({status or 'no status'}), "
                      f"attempt {self._nav_failures} of {NAV_RETRIES + 1} "
                      f"for #{self._url.rpartition('#')[2][:40]}")
            if self._nav_failures > NAV_RETRIES:
                return
            try:
                core = self._view.CoreWebView2 if self._view is not None else None
                if core is not None and self._url:
                    core.Navigate(self._url)
            except Exception:
                logs.exception("Could not retry the page")
            return

        self._nav_failures = 0
        self._loaded = True
        if self._suppressed:
            return              # an overlay is up; stay hidden
        self.set_child_visible(True)

    def tell(self, body) -> bool:
        """Post `body` (a dict) into the page - window.chrome.webview's
        'message' event, app.js hostMessage. False when there is no page
        to hear it yet; the caller then does whatever it did before the
        page could be told (main._toggle_sidebar falls back to the pin)."""
        try:
            core = self._view.CoreWebView2 if self._view is not None else None
            if core is None or not self._loaded:
                return False
            core.PostWebMessageAsJson(json.dumps(body))
            return True
        except Exception:
            return False

    def sync_position(self):
        """Put the native window where this widget now is.

        **Qt does not do this on its own.** Measured 3 September 2026
        (scratchpad native_move2.py): a layout moving an alien ancestor
        from x=220 to x=150 left this widget's HWND exactly where it was
        - GetWindowRect unchanged through three event-loop passes, while
        mapTo() already answered 150 - and only a change to *this*
        widget's own geometry made Qt re-place it. That is why, for the
        whole of a sidebar fold, the web pages stood still on screen and
        then leapt the full width of the rail when the fold landed (the
        page is pinned at one width during a fold, so its own geometry
        never changes until the end - see main._toggle_sidebar).

        Setting the QWindow's position is Qt's own route, so its record
        of where the window is stays right and the next real resize
        agrees with it. Same widget: 0.29ms per step across 40 steps,
        one SetWindowPos with the size untouched - Edge does not lay the
        document out again for a move.
        """
        try:
            handle = self.windowHandle()
            parent = self.nativeParentWidget()
            if handle is None or parent is None:
                return
            handle.setPosition(self.mapTo(parent, QPoint(0, 0)))
        except RuntimeError:
            pass

    def _got_message(self, _sender, args):
        """A click in the page. Never trusted as anything but data."""
        if not self._alive():
            # A document outliving its page - see `_open`. Dropped, not
            # raised: the emit below on a deleted widget is the
            # "wrapped C/C++ object ... has been deleted" his log showed.
            return
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
        # **Every frame, not every 33ms.** This used to coalesce to
        # 0.033s on the reasoning that a fold sends one resize per
        # animation frame and most of that work is thrown away. What it
        # actually produced was 30 sizes a second against a 165Hz panel -
        # the view held still for four to eight frames and then jumped,
        # which is the owner's "they move in a clear steps".
        #
        # Nothing is thrown away now, because _fit already returns
        # immediately when the device size has not changed: one
        # SetWindowPos per real change is the fewest possible, and
        # anything less than that is a step the eye can see. The settle
        # timer stays, so the final size is exact even if the last
        # resizeEvent arrives with the animation still mid-flight.
        self._fit()
        self._settle.start(120)

    def _fit(self, force=False):
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
        if width <= 0 or height <= 0:
            return
        # **Ask Windows what size the child actually is.** This used to
        # compare against a `self._sized` written by whoever last called
        # here, which is a cache of an outside fact - and it went stale
        # the moment anything sized the child by another route, or when
        # a fit ran while the widget was hidden and therefore the wrong
        # size. The view then kept that size, and a WebView2 with a
        # stale (or zero) box lays nothing out: the page is there, the
        # cards are there, and every picture is waiting for a viewport
        # that never arrives - which is the owner's "the apps and games
        # images are not showing", and the fold looking broken.
        #
        # It stayed hidden for as long as it did because set_child_visible
        # reset _sized to (0, 0) every 150ms and re-fitted, so the stale
        # value was never more than a sixth of a second old. Removing
        # that storm (measured: 6.75 SetWindowPos a second, for nothing)
        # is right; keeping a cache it was quietly repairing is not.
        #
        # GetClientRect is a syscall costing about a microsecond, against
        # the SetWindowPos it guards, so this is cheaper than the
        # bookkeeping it replaces and cannot be wrong.
        #
        # **`force` skips the check, and set_child_visible needs it.**
        # This call sets the child's position as well as its size, and
        # hiding parks it at -32000 with SWP_NOSIZE - so a window that
        # is off screen at the *right size* has to be moved back, and a
        # size test alone says there is nothing to do. Screenshotting
        # the real window is what caught it: every page came back 100%
        # flat background (rules/ui.md, and the reason that rule exists).
        if not force:
            box = w.RECT()
            if _u.GetClientRect(self._child, ctypes.byref(box)):
                if ((box.right - box.left, box.bottom - box.top)
                        == (width, height)
                        and not (getattr(self, "_parked", False) and self._loaded)):
                    return      # the fold sends many resizes per frame
        self._sized = (width, height)
        # NOACTIVATE and DEFERERASE as well as NOZORDER: the sidebar fold
        # resizes this on every frame of its animation, and the default
        # flags make Windows erase the background and re-activate on
        # each one - work that is thrown away 60+ times a second and is
        # felt as the cards stuttering as the rail opens.
        # **A child with no document yet is sized where it is parked.**
        # The owner, 6 September 2026: "while I am scrolling in the 3asq
        # readings there is a black stutter". Sampled at the screen's
        # rate on a chapter entered a second time: the reader widget's
        # ground for 57ms, then a pure white frame for 25ms, then the
        # form's ground, then the details page showing through again,
        # then a fade to dark - all before the document had navigated.
        # The form is created parked at -32000 and _navigated reveals it
        # (set_child_visible, "kept hidden until here"), but the reader's
        # follow() resizes the widget first, and this call sized the
        # child *at 0,0* - which dragged the empty control on screen a
        # third of a second early. Until the document has loaded the
        # size is applied with the position left alone; the reveal's
        # forced fit moves it back as it always did.
        flags = SWP_NOZORDER | SWP_NOACTIVATE | SWP_DEFERERASE
        if not self._loaded and not force:
            flags |= SWP_NOMOVE
        _u.SetWindowPos(self._child, None, 0, 0, width, height, flags)
        if not flags & SWP_NOMOVE:
            self._parked = False
