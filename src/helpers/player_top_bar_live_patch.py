"""The playing-state bars - the upper one and, since 7 September 2026,
the lower one too: bare glyphs, text and the seek strip over the video.

The owner, four sessions running, most recently with a screenshot of
what the mask version produced: *"remove the black bg for each button
and label!!"*. A region mask cannot do that - whatever stays inside the
mask still paints the bar's background, so every control sat in its own
dark rectangle. What CAN do it, native-over-native, is per-pixel alpha:
the bar window becomes WS_EX_LAYERED and its content is composed by
UpdateLayeredWindow from an ARGB rendering of its children only - text
and glyphs opaque, every other pixel alpha 0, so the video shows
through untouched. A colour key was measured failing under HDR (solid
black boxes - _build_top_bar's note) and a uniform alpha dims the text
with the background; per-pixel alpha is the compositor path and has
neither failure mode.

While the source is loading nothing changes: the layered style is only
applied once a real frame is live, and removed again for the next load,
so the loading look stays exactly as it was. The empty stretch of the
bar becomes genuinely transparent - clicks there fall through to the
video (Windows hit-tests layered pixels by alpha), which also means the
bar-gap window drag is given up while a frame is live; the title and
buttons still take their presses.

Kept as its own module name: verification kept loading a stale copy
under the old transparency patch's identity while the file on disk
carried the fix (three runs flapping); a fresh name has one cache
identity everywhere.
"""

from __future__ import annotations

import ctypes
import sys

_INSTALLED = False
_PATCHED = set()

GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
ULW_ALPHA = 0x00000002
AC_SRC_OVER = 0x00
AC_SRC_ALPHA = 0x01
BI_RGB = 0

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

_user32.GetWindowLongW.restype = ctypes.c_long
_user32.GetWindowLongW.argtypes = (ctypes.c_void_p, ctypes.c_int)
_user32.SetWindowLongW.restype = ctypes.c_long
_user32.SetWindowLongW.argtypes = (ctypes.c_void_p, ctypes.c_int,
                                   ctypes.c_long)
_user32.GetDC.restype = ctypes.c_void_p
_user32.GetDC.argtypes = (ctypes.c_void_p,)
_user32.ReleaseDC.restype = ctypes.c_int
_user32.ReleaseDC.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
_gdi32.CreateCompatibleDC.restype = ctypes.c_void_p
_gdi32.CreateCompatibleDC.argtypes = (ctypes.c_void_p,)
_gdi32.DeleteDC.restype = ctypes.c_int
_gdi32.DeleteDC.argtypes = (ctypes.c_void_p,)
_gdi32.SelectObject.restype = ctypes.c_void_p
_gdi32.SelectObject.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
_gdi32.DeleteObject.restype = ctypes.c_int
_gdi32.DeleteObject.argtypes = (ctypes.c_void_p,)


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", ctypes.c_uint32),
                ("biWidth", ctypes.c_int32),
                ("biHeight", ctypes.c_int32),
                ("biPlanes", ctypes.c_uint16),
                ("biBitCount", ctypes.c_uint16),
                ("biCompression", ctypes.c_uint32),
                ("biSizeImage", ctypes.c_uint32),
                ("biXPelsPerMeter", ctypes.c_int32),
                ("biYPelsPerMeter", ctypes.c_int32),
                ("biClrUsed", ctypes.c_uint32),
                ("biClrImportant", ctypes.c_uint32)]


class _BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_byte),
                ("BlendFlags", ctypes.c_byte),
                ("SourceConstantAlpha", ctypes.c_ubyte),
                ("AlphaFormat", ctypes.c_byte)]


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class _SIZE(ctypes.Structure):
    _fields_ = [("cx", ctypes.c_long), ("cy", ctypes.c_long)]


_gdi32.CreateDIBSection.restype = ctypes.c_void_p
_gdi32.CreateDIBSection.argtypes = (ctypes.c_void_p,
                                    ctypes.POINTER(_BITMAPINFOHEADER),
                                    ctypes.c_uint,
                                    ctypes.POINTER(ctypes.c_void_p),
                                    ctypes.c_void_p, ctypes.c_uint32)
_user32.UpdateLayeredWindow.restype = ctypes.c_int
_user32.UpdateLayeredWindow.argtypes = (
    ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(_POINT),
    ctypes.POINTER(_SIZE), ctypes.c_void_p, ctypes.POINTER(_POINT),
    ctypes.c_uint32, ctypes.POINTER(_BLENDFUNCTION), ctypes.c_uint32)


def _clean_controls(player, page) -> None:
    """Bare glyphs and text: no per-control fills, no borders."""
    bar = getattr(page, "top_bar", None)
    if bar is None:
        return
    try:
        from PyQt6.QtWidgets import QLabel, QPushButton

        for label in bar.findChildren(QLabel):
            style = label.styleSheet() or ""
            if "AtomicTopBarLive" not in style:
                label.setStyleSheet(
                    style + "\n/* AtomicTopBarLive */"
                    + "\nQLabel { background: transparent; border: none; }")
        for button in bar.findChildren(QPushButton):
            style = button.styleSheet() or ""
            if "AtomicTopBarLive" not in style:
                button.setStyleSheet(
                    style + "\n/* AtomicTopBarLive */"
                    + "\nQPushButton { background: transparent;"
                    + " border: none; }"
                    + "\nQPushButton:hover { background: "
                    + f"{player.theme.SURFACE_HOVER}; border: none; }}")
    except RuntimeError:
        pass


def _bars(page):
    """The strips composed over the video: the top bar, and the controls
    bar since the owner's ask of 7 September 2026 ("make the lower bar
    ... fully transparent (even when paused), but keep all items like
    buttons, seek bar and all the same crystal clear") - the same
    per-pixel composition, so the seek strip, the times and the buttons
    are drawn at full strength and every other pixel is the video."""
    found = []
    for name in ("top_bar", "controls"):
        bar = getattr(page, name, None)
        if bar is not None:
            found.append(bar)
    return found


def _compose(page, bar=None):
    """Render the bar's children over transparency and push the result
    through UpdateLayeredWindow. Returns False when the window cannot
    take it (no HWND yet, zero size) - never raises."""
    bar = getattr(page, "top_bar", None) if bar is None else bar
    if bar is None:
        return False
    try:
        from PyQt6.QtCore import QPoint
        from PyQt6.QtGui import QColor, QImage, QPainter
        from PyQt6.QtWidgets import QWidget

        hwnd = int(bar.winId())
        dpr = float(bar.devicePixelRatioF() or 1.0)
        pw = max(1, int(round(bar.width() * dpr)))
        ph = max(1, int(round(bar.height() * dpr)))

        image = QImage(pw, ph, QImage.Format.Format_ARGB32_Premultiplied)
        image.setDevicePixelRatio(dpr)
        # **Alpha 1, not alpha 0.** Windows hit-tests a per-pixel-alpha
        # window by its pixels: alpha-0 pixels are click-through, so
        # with a fully transparent base only the exact glyph strokes
        # took presses - the owner's "the buttons sometimes do not
        # click" (30 August 2026): a press on a button's padding fell
        # through to the video. One part in 255 is invisible over any
        # picture, keeps the whole strip pressable, and gives the
        # DragStrip its window-drag gaps back as a side effect.
        image.fill(QColor(0, 0, 0, 1).rgba())
        painter = QPainter(image)
        for child in bar.children():
            if not isinstance(child, QWidget) or not child.isVisibleTo(bar):
                continue
            child.render(painter, child.pos(),
                         flags=QWidget.RenderFlag.DrawChildren)
        painter.end()

        header = _BITMAPINFOHEADER()
        header.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        header.biWidth = pw
        header.biHeight = -ph            # top-down
        header.biPlanes = 1
        header.biBitCount = 32
        header.biCompression = BI_RGB

        screen_dc = _user32.GetDC(None)
        mem_dc = _gdi32.CreateCompatibleDC(screen_dc)
        bits = ctypes.c_void_p()
        bitmap = _gdi32.CreateDIBSection(screen_dc, ctypes.byref(header), 0,
                                         ctypes.byref(bits), None, 0)
        ok = False
        if bitmap and bits:
            src = image.constBits()
            src.setsize(ph * image.bytesPerLine())
            row_bytes = pw * 4
            if image.bytesPerLine() == row_bytes:
                ctypes.memmove(bits, bytes(src), ph * row_bytes)
            else:
                data = bytes(src)
                stride = image.bytesPerLine()
                for y in range(ph):
                    ctypes.memmove(bits.value + y * row_bytes,
                                   data[y * stride:y * stride + row_bytes],
                                   row_bytes)
            old = _gdi32.SelectObject(mem_dc, bitmap)
            size = _SIZE(pw, ph)
            source = _POINT(0, 0)
            blend = _BLENDFUNCTION(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)
            ok = bool(_user32.UpdateLayeredWindow(
                hwnd, None, None, ctypes.byref(size), mem_dc,
                ctypes.byref(source), 0, ctypes.byref(blend), ULW_ALPHA))
            _gdi32.SelectObject(mem_dc, old)
        if bitmap:
            _gdi32.DeleteObject(bitmap)
        _gdi32.DeleteDC(mem_dc)
        _user32.ReleaseDC(None, screen_dc)
        return ok
    except Exception:
        return False


def _set_layered(page, on: bool, bar=None) -> None:
    bar = getattr(page, "top_bar", None) if bar is None else bar
    if bar is None:
        return
    try:
        hwnd = int(bar.winId())
        style = _user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        veiled = getattr(page, "_veiled", None)
        if on:
            # **The controls bar arrives in the veil's mode.** _veil put
            # it under SetLayeredWindowAttributes (one alpha for the whole
            # window), and UpdateLayeredWindow will not take a window in
            # that mode: the style bit is cleared and set again, once,
            # and the veil's record dropped so the veil is not laid back
            # over the composition. "Record present" is how that mode is
            # told from this one - the style bit alone looks the same.
            if (isinstance(veiled, dict) and veiled.pop(id(bar), None) is not None
                    and (style & WS_EX_LAYERED)):
                _user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style & ~WS_EX_LAYERED)
                style &= ~WS_EX_LAYERED
            if not (style & WS_EX_LAYERED):
                _user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style | WS_EX_LAYERED)
        elif style & WS_EX_LAYERED:
            _user32.SetWindowLongW(hwnd, GWL_EXSTYLE,
                                   style & ~WS_EX_LAYERED)
            if isinstance(veiled, dict):
                veiled.pop(id(bar), None)     # the next wake veils it again
            try:
                bar.update()
            except RuntimeError:
                pass
    except Exception:
        pass


def _bar_is_layered(page, bar=None) -> bool:
    bar = getattr(page, "top_bar", None) if bar is None else bar
    if bar is None:
        return False
    try:
        style = _user32.GetWindowLongW(int(bar.winId()), GWL_EXSTYLE)
        return bool(style & WS_EX_LAYERED)
    except Exception:
        return False


def _layered_children(page):
    """Every child of the player that is a *layered* native window.

    All of these are given `WS_EX_LAYERED` somewhere in player.py -
    `_set_window_alpha` for the backdrop (255), the status box and the
    skip button, `_veil` for the bars and the episode bar, and
    `_compose` for the two bars. That flag is what makes DWM composite
    the window from a bitmap of its own, and therefore what makes it
    outlive `hide()` on its parent - so this list is the set that has to
    be taken down by hand when the page goes."""
    found = []
    for name in ("top_bar", "controls", "backdrop", "status", "skip_btn",
                 "_episode_bar", "_panel"):
        child = getattr(page, name, None)
        if child is not None:
            found.append(child)
    return found


def drop_composition(page) -> None:
    """Take every layered child out of that mode and hide it, now.

    **Measured off his own screen recording, 11 September 2026.** A
    `WS_EX_LAYERED` child window is composited by DWM from a bitmap it
    owns, and hiding the *parent* does not make DWM stop presenting it -
    so `close_player`'s `self.hide()` took the page away and left the
    controls bar painted over whatever the page had been covering.
    Frame by frame at 25fps: the Reacher episode ends at 8.32s, Home is
    fully drawn at 8.44s, and the player's seek strip, pause button,
    volume and "Resumed From 33:44" are still across the bottom of it
    at 8.44, 8.80, 9.20 and 9.52s - **1.1 seconds** - until the next
    player opened over it. That is his "shows some frozen page for < 1
    sec then it shows the real player page", and the second is not a
    coincidence: the bar lives until Qt processes `deleteLater`, and the
    event loop spends exactly that long building the next page and
    starting mpv.

    The top bar never showed the fault because it is only layered while
    a frame is live (`refresh_live_bar`); the controls bar is composed
    from the start, which is the 7 September change this pays for.

    **Every layered child, not just the bars.** Asked what the held
    picture actually was, he answered: "a video player page, but it
    seems old not for the current played" - the *previous* title's
    loading frame, which `_set_window_alpha(self.backdrop, 255)` also
    makes layered, so it survives its page's `hide()` exactly as the
    bars did (see _layered_children).

    Style first, then hide: a window still in layered mode can be
    presented after `ShowWindow(SW_HIDE)`, and clearing the bit is what
    makes DWM let go of the surface."""
    for bar in _layered_children(page):
        try:
            _set_layered(page, False, bar)
        except Exception:
            pass
        try:
            bar.hide()
        except Exception:
            # Already deleted on the C++ side, or not a widget at all.
            # `Exception`, not `RuntimeError`: this runs in a teardown
            # and the loop must reach the *other* bar whatever the first
            # one does - a narrower catch let one bad bar leave the
            # second one composed, which is the whole fault being fixed.
            pass
    try:
        page._atomic_bar_layered = False
    except Exception:
        pass


def refresh_live_bar(player, page) -> None:
    """Loading keeps the plain bar; a live frame gets the layered one.

    `_awaiting_first_frame` alone is NOT the gate: it initialises False
    in __init__ and only goes True when a load begins, so the first
    wake during _start found the bar already flipped to per-pixel mode
    and the core veil's SetLayeredWindowAttributes then faulted on it
    (faulthandler's stack: _start -> _wake_controls -> _veil ->
    _set_window_alpha). Live means a stream has started AND its first
    frame has actually arrived."""
    live = (getattr(page, "_streams_started", False)
            and not getattr(page, "_awaiting_first_frame", True))
    # **The controls bar is composed from the start** - the owner, 7
    # September 2026: "make the lower bar also transparent when the vid
    # is still loading before it plays". Only the top bar keeps the
    # loading look until a frame is live.
    controls = getattr(page, "controls", None)
    composed = True
    for bar in _bars(page):
        if live or bar is controls:
            _set_layered(page, True, bar)
            composed = _compose(page, bar) and composed
        else:
            _set_layered(page, False, bar)
    page._atomic_bar_layered = bool(live and composed)


class _VolumeOsd:
    """The volume flyout for the arrow keys - the owner's image: a
    rounded dark panel, a speaker, a level bar - composed per-pixel by
    the same UpdateLayeredWindow plumbing as the bar, because it too
    must sit over mpv's native child without a rectangle around it."""

    WIDTH, HEIGHT = 220, 220
    HIDE_MS = 1100
    # Softer corners and a see-through frame, both his asks of 30 August
    # 2026. 26 is a visible round on a 220px panel; 150/255 leaves the
    # picture readable behind it while the white icon and the accent bar
    # stay solid on top.
    RADIUS = 26.0
    PANEL_ALPHA = 150

    def __init__(self, player, page):
        from PyQt6.QtCore import QTimer
        from PyQt6.QtWidgets import QWidget

        self._player = player
        self._page = page
        self.widget = QWidget(page)
        player._make_native(self.widget)
        self.widget.resize(self.WIDTH, self.HEIGHT)
        self.widget.hide()
        self._timer = QTimer(self.widget)
        self._timer.setSingleShot(True)
        self._timer.setInterval(self.HIDE_MS)
        self._timer.timeout.connect(self.widget.hide)

    def show(self, volume, muted=False):
        from PyQt6.QtCore import QPointF, QRectF, Qt
        from PyQt6.QtGui import QColor, QImage, QPainter, QPen

        page, widget = self._page, self.widget
        theme = self._player.theme
        widget.move((page.width() - self.WIDTH) // 2,
                    (page.height() - self.HEIGHT) // 2)
        dpr = float(widget.devicePixelRatioF() or 1.0)
        pw, ph = int(self.WIDTH * dpr), int(self.HEIGHT * dpr)
        image = QImage(pw, ph, QImage.Format.Format_ARGB32_Premultiplied)
        image.setDevicePixelRatio(dpr)
        image.fill(0)
        painter = QPainter(image)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        panel = QColor(theme.BG)
        # **The frame alone is see-through; the icon and the bar are
        # not** - the owner, 30 August 2026: "make the frame BG more
        # transparent (ONLY THE FRAME NOT THE SOUND ICON OR THE BAR
        # ITSELF)". Per-pixel alpha is what makes that separable at all:
        # the panel is drawn at this alpha and everything after it at
        # full, where a window-wide alpha would have dimmed the lot.
        panel.setAlpha(self.PANEL_ALPHA)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(panel)
        painter.drawRoundedRect(QRectF(0, 0, self.WIDTH, self.HEIGHT),
                                self.RADIUS, self.RADIUS)
        white = QColor(theme.TEXT_OVER_MEDIA if hasattr(
            theme, "TEXT_OVER_MEDIA") else "#ffffff")
        # **His picture, 7 September 2026** ("make the vol small window
        # ... shows like 4th image exactly Icon and Bar"): an outline
        # speaker with three waves, and a bar whose reached part is white
        # and whose rest is warm (theme.VOLUME_OSD_REST), a round white
        # knob between the two. The frame's alpha above is unchanged.
        from PyQt6.QtGui import QPolygonF
        pen = QPen(white, 5.0)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        cx, cy = self.WIDTH / 2 - 10, 88.0
        body = [QPointF(cx - 30, cy - 12), QPointF(cx - 14, cy - 12),
                QPointF(cx + 6, cy - 30), QPointF(cx + 6, cy + 30),
                QPointF(cx - 14, cy + 12), QPointF(cx - 30, cy + 12)]
        painter.drawPolygon(QPolygonF(body))
        top_v = float(getattr(self._player, "VOLUME_MAX", 200) or 200)
        loud = max(0.0, min(1.0, float(volume) / top_v))
        if not muted and volume > 0:
            # Three waves from the cone's tip; the ordinary 100% (the
            # middle of 0-200) shows all three, a whisper the first.
            for radius, floor in ((14.0, 0.0), (26.0, 0.2), (38.0, 0.4)):
                if loud >= floor:
                    painter.drawArc(
                        QRectF(cx + 10 - radius, cy - radius,
                               2 * radius, 2 * radius), -40 * 16, 80 * 16)
        # **The bar's colours do not move with the knob** - the owner, 7
        # September 2026: "do not make the right side of the dot
        # orange!! make the colour fixed ... its idea is to show the
        # levels of vol after the 100% (yellow orange red in a smooth
        # way)". White to the middle (100% of the 0-200 range, VOLUME_MAX
        # - mpv can amplify), then theme.VOLUME_OSD_GAIN to the end; the
        # knob rides over the gradient at the level.
        from PyQt6.QtGui import QLinearGradient
        track_y = 170.0
        left, right = 34.0, self.WIDTH - 34.0
        at = left + (right - left) * loud
        gradient = QLinearGradient(QPointF(left, track_y), QPointF(right, track_y))
        gradient.setColorAt(0.0, white)
        gradient.setColorAt(0.5, white)
        yellow, orange, red = theme.VOLUME_OSD_GAIN
        gradient.setColorAt(0.66, QColor(yellow))
        gradient.setColorAt(0.83, QColor(orange))
        gradient.setColorAt(1.0, QColor(red))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(gradient)
        painter.drawRoundedRect(QRectF(left - 2.5, track_y - 2.5,
                                       right - left + 5.0, 5.0), 2.5, 2.5)
        painter.setPen(QPen(QColor(theme.VOLUME_OSD_KNOB_RING), 1.5))
        painter.setBrush(white)
        painter.drawEllipse(QPointF(at, track_y), 9.0, 9.0)
        painter.end()

        widget.show()
        widget.raise_()
        _push_layered(widget, image, pw, ph)
        self._timer.start()


def _push_layered(widget, image, pw, ph):
    """One ULW update for an arbitrary native child - the bar's compose
    tail, shared."""
    try:
        hwnd = int(widget.winId())
        style = _user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        if not (style & WS_EX_LAYERED):
            _user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style | WS_EX_LAYERED)
        header = _BITMAPINFOHEADER()
        header.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        header.biWidth = pw
        header.biHeight = -ph
        header.biPlanes = 1
        header.biBitCount = 32
        header.biCompression = BI_RGB
        screen_dc = _user32.GetDC(None)
        mem_dc = _gdi32.CreateCompatibleDC(screen_dc)
        bits = ctypes.c_void_p()
        bitmap = _gdi32.CreateDIBSection(screen_dc, ctypes.byref(header), 0,
                                         ctypes.byref(bits), None, 0)
        ok = False
        if bitmap and bits:
            src = image.constBits()
            src.setsize(ph * image.bytesPerLine())
            data = bytes(src)
            row_bytes = pw * 4
            stride = image.bytesPerLine()
            if stride == row_bytes:
                ctypes.memmove(bits, data, ph * row_bytes)
            else:
                for y in range(ph):
                    ctypes.memmove(bits.value + y * row_bytes,
                                   data[y * stride:y * stride + row_bytes],
                                   row_bytes)
            old = _gdi32.SelectObject(mem_dc, bitmap)
            size = _SIZE(pw, ph)
            source = _POINT(0, 0)
            blend = _BLENDFUNCTION(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)
            ok = bool(_user32.UpdateLayeredWindow(
                hwnd, None, None, ctypes.byref(size), mem_dc,
                ctypes.byref(source), 0, ctypes.byref(blend), ULW_ALPHA))
            _gdi32.SelectObject(mem_dc, old)
        if bitmap:
            _gdi32.DeleteObject(bitmap)
        _gdi32.DeleteDC(mem_dc)
        _user32.ReleaseDC(None, screen_dc)
        return ok
    except Exception:
        return False


def _patch(player) -> None:
    key = id(player)
    if key in _PATCHED:
        return
    _PATCHED.add(key)

    from PyQt6.QtCore import QEvent, QObject, QTimer

    Page = player.PlayerPage
    old_build = Page._build_top_bar
    old_layout = Page._layout_overlays
    old_wake = Page._wake_controls
    old_veil = Page._veil

    class _Recompose(QObject):
        """Recompose on the interactions the timer is too slow for.

        The 150ms fallback made hover fills and presses land a beat
        late - the owner's "the upper bar is glitching". Enter/Leave
        and press/release are the only events whose visual must land
        the same frame; everything else (title text, layout) is covered
        by the timer. Coalesced through a 0ms shot so an enter+leave
        burst costs one composition."""

        KINDS = (QEvent.Type.Enter, QEvent.Type.Leave,
                 QEvent.Type.MouseButtonPress,
                 QEvent.Type.MouseButtonRelease)
        # A drag on the seek strip moves its knob under the pointer, so
        # a held-button move recomposes too; a bare hover is left to the
        # timer and the Enter/Leave pair - one composition per pointer
        # event over a 2560px strip would be most of a core.
        DRAG = QEvent.Type.MouseMove

        def __init__(self, page):
            super().__init__(page)
            self._page = page
            self._pending = False

        def _fire(self):
            self._pending = False
            try:
                refresh_live_bar(player, self._page)
            except RuntimeError:
                pass

        def eventFilter(self, obj, event):
            kind = event.type()
            wanted = kind in self.KINDS or (
                kind == self.DRAG and bool(event.buttons()))
            if wanted and not self._pending:
                self._pending = True
                QTimer.singleShot(0, self._fire)
            return False

    def build_top_bar(self):
        old_build(self)
        _clean_controls(player, self)
        # The composition rides a timer for the slow-changing content
        # (title text, layout) and an event filter for the interactions
        # that must not lag - see _Recompose.
        timer = QTimer(self)
        timer.setInterval(150)
        timer.timeout.connect(lambda: refresh_live_bar(player, self))
        timer.start()
        self._atomic_bar_timer = timer
        relay = _Recompose(self)
        self._atomic_bar_relay = relay
        bar = getattr(self, "top_bar", None)
        if bar is not None:
            from PyQt6.QtWidgets import QWidget
            bar.installEventFilter(relay)
            for child in bar.findChildren(QWidget):
                child.installEventFilter(relay)
        refresh_live_bar(player, self)

    def _relay_controls(self):
        """The controls bar is built after the top bar, so its relay is
        installed on the first layout instead of at build time."""
        if getattr(self, "_atomic_controls_relayed", False):
            return
        relay = getattr(self, "_atomic_bar_relay", None)
        bar = getattr(self, "controls", None)
        if relay is None or bar is None:
            return
        try:
            from PyQt6.QtWidgets import QWidget
            bar.installEventFilter(relay)
            for child in bar.findChildren(QWidget):
                child.installEventFilter(relay)
            self._atomic_controls_relayed = True
        except RuntimeError:
            pass

    def layout_overlays(self):
        result = old_layout(self)
        _clean_controls(player, self)
        _relay_controls(self)
        refresh_live_bar(player, self)
        return result

    def wake_controls(self):
        result = old_wake(self)
        refresh_live_bar(player, self)
        return result

    def veil(self, widget, alpha):
        # SetLayeredWindowAttributes and UpdateLayeredWindow are
        # exclusive modes on one window: the veil's SLWA call on a bar
        # already in per-pixel mode is exactly the fault faulthandler
        # caught. Guarded by the WINDOW'S OWN style, not this patch's
        # bookkeeping - the style is the truth whatever order the wake,
        # the load and the composition happened in.
        # Per-pixel mode is "the bit is set and the veil holds no record
        # for this window" - the veil's own SLWA mode sets the same bit,
        # and re-veiling a veiled window is harmless.
        if (widget in _bars(self) and _bar_is_layered(self, widget)
                and id(widget) not in getattr(self, "_veiled", {})):
            return
        return old_veil(self, widget, alpha)

    old_keys = Page.keyPressEvent

    def key_press(self, event):
        # **The arrows never touch the bars** - the owner's ask, 30
        # August 2026: seek and volume from the keyboard must not show
        # the lower bar. Handled here, before the core handler whose
        # _WAKING_KEYS list would wake everything; volume additionally
        # gets its own flyout (the image he sent), which lives over the
        # video without waking anything else.
        from PyQt6.QtCore import Qt

        key = event.key()
        if key in (Qt.Key.Key_Left, Qt.Key.Key_Right):
            self._seek_relative(
                -player.SEEK_STEP_S if key == Qt.Key.Key_Left
                else player.SEEK_STEP_S)
            return
        if key in (Qt.Key.Key_Up, Qt.Key.Key_Down):
            step = player.VOLUME_STEP if key == Qt.Key.Key_Up \
                else -player.VOLUME_STEP
            value = max(0, min(player.VOLUME_MAX, self._volume + step))
            try:
                self.volume_slider.setValue(value)
            except RuntimeError:
                return
            osd = getattr(self, "_atomic_volume_osd", None)
            if osd is None:
                try:
                    osd = _VolumeOsd(player, self)
                    self._atomic_volume_osd = osd
                except Exception:
                    osd = None
            if osd is not None:
                try:
                    osd.show(value, muted=getattr(self, "_muted", False))
                except Exception:
                    pass
            return
        return old_keys(self, event)

    Page._build_top_bar = build_top_bar
    Page._layout_overlays = layout_overlays
    Page._wake_controls = wake_controls
    Page._veil = veil
    Page.keyPressEvent = key_press


def install() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True
    try:
        from . import requested_fixes_patch as requested

        previous = requested._patch_player

        def chained(module):
            previous(module)
            _patch(module)

        requested._patch_player = chained
        loaded = sys.modules.get("windows.player")
        if loaded is not None:
            _patch(loaded)
    except Exception:
        pass
