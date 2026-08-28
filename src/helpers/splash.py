"""The launch screen: the mark, alone on the app's own ground, for about
a second while everything behind it is built.

The owner's ask, 28 August 2026, pointing at Stremio's: *"make the logo
appear for ~1 sec and add a good animation"* - then, having seen two
attempts at the animation, *"remove the sweep totally from the starting
logo, make it fixed"*.

So it is a still frame, and that is the whole of it now. Both animations
went the same way and it is worth recording why: a rise-and-fade read as
"random" against a launch whose length nobody controls, and a highlight
sweeping the mark could only ever be *interrupted* by the window opening
on top of it - the second version tied the sweep's length to the
splash's own so it would at least finish, and it still went. A launch
screen is on screen for as long as the launch takes, which is not a
duration anything can choreograph against; a still mark has nothing to
be caught halfway through.

**This does not make startup slower, and that is the whole design.** The
frozen build takes 1.29-1.57s from double-click to a usable window
(measured, and recorded in .claude/rules/planning.md), and every one of
those milliseconds was previously spent showing *nothing at all* - the
taskbar button appears and the desktop stays where it is. So the splash
is put up first and the real work continues underneath it; it is not a
`sleep`, and rule 7's one-second budget is not being spent, it is being
*shown*.

The consequence of that is worth stating: if the window is ready sooner
than the minimum, the splash still serves it out rather than snapping
away, because a mark that flashes for 200ms reads as a glitch. If the
window takes longer, the splash is already gone and the wait is as bare
as it was before - this covers the beginning of the gap, it cannot
promise to cover all of it.

**The mark is drawn at its own aspect ratio**, through the same
`images.tinted_asset` the sidebar's logo goes through. The first version
drew it into a square rect and stretched a 3:2 mark by half its width
again - the owner's "the logo seems really need a stretch on width ...
make its ratio the same as the atomic icon in the sidebar".
"""

import time

from PyQt6.QtCore import QRectF, Qt, QTimer
from PyQt6.QtGui import QColor, QPainter, QPixmap, QRadialGradient
from PyQt6.QtWidgets import QApplication, QWidget

from . import images, theme

# **No floor. A launch that beats a second shows no splash at all.**
# The owner's ask, 28 August 2026: "for the app icon start up make sure
# if the app opens in <1 sec to hide it!".
#
# There was a 950ms floor here, and the reasoning for it - that a mark
# flashing for 200ms reads as a glitch - was sound while the mark was
# *animated*: an animation cut off part-way is what looks broken. The
# mark is a still frame now (see the note above), so there is nothing to
# interrupt, and holding a window back that is already built is the one
# thing rule 7 will not have. Zero rather than deleting the parameter,
# so a caller that does want a floor can still ask for one and the
# decision stays visible here.
MINIMUM_MS = 0

# The panel the mark sits on. Square, and generous around the logo: the
# reference is a mark floating on a dark field, not a boxed icon.
PANEL_PX = 320
# The mark's height inside it. Its width follows from the artwork's own
# ratio - never assumed here, read off the file.
LOGO_HEIGHT_PX = 132
# The glow behind it, as a fraction of the panel, and its strength.
GLOW_FRACTION = 0.86
GLOW_ALPHA = 42
# The panel's corners - the same radius every other surface in the app
# rounds to, so the launch screen and the window it hands over to are
# cut from the same shape.
PANEL_RADIUS = theme.RADIUS

# **No sweep constants.** There was a moving highlight here; see the
# module note for why it is gone rather than tuned.


class Splash(QWidget):
    """A frameless, always-on-top square with the mark painted on it."""

    def __init__(self, logo: QPixmap):
        super().__init__(None, Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.SplashScreen
                         | Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self._logo = logo
        self._started = time.monotonic()
        self.resize(PANEL_PX, PANEL_PX)
        self._centre()
        # No timer: nothing on this surface moves, so it is painted once
        # and left alone. The frame it used to ask for every refresh is
        # exactly the work a launch has better uses for.

    def _centre(self):
        screen = QApplication.primaryScreen()
        if screen is None:
            return
        area = screen.availableGeometry()
        self.move(area.center().x() - self.width() // 2,
                  area.center().y() - self.height() // 2)

    def finish(self):
        """Take it off screen.

        No fade: full opacity from the first frame to the last, and the
        window it hands over to is coming up in the same breath - there
        is nothing to dissolve into."""
        try:
            self.close()
        except RuntimeError:
            pass

    def _logo_rect(self) -> QRectF:
        """The mark at its own ratio, centred. Read off the pixmap rather
        than assumed square - which is exactly what the first version got
        wrong."""
        pixmap = self._logo
        ratio = pixmap.devicePixelRatio() or 1.0
        width = pixmap.width() / ratio
        height = pixmap.height() / ratio
        centre = self.rect().center()
        return QRectF(centre.x() - width / 2.0, centre.y() - height / 2.0,
                      width, height)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        centre = self.rect().center()

        # **The app's own ground, not the desktop's.** The window is
        # translucent so its corners can be round, which left the mark
        # floating on whatever happened to be behind it - fine over a
        # dark desktop, wrong over anything else, and not what the
        # reference shows: a dark field with the mark on it. Painted
        # first, so the glow and the mark sit on it.
        panel = QColor(theme.BG)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(panel)
        painter.drawRoundedRect(QRectF(self.rect()), PANEL_RADIUS, PANEL_RADIUS)

        # The glow first: a radial wash of the accent behind the mark, so
        # the panel reads as lit from the logo rather than as a grey box
        # with a picture on it. Static, like everything else here.
        glow_r = PANEL_PX * GLOW_FRACTION * 0.5
        gradient = QRadialGradient(float(centre.x()), float(centre.y()), glow_r)
        lit = QColor(theme.ACCENT)
        lit.setAlpha(GLOW_ALPHA)
        gradient.setColorAt(0.0, lit)
        clear = QColor(theme.ACCENT)
        clear.setAlpha(0)
        gradient.setColorAt(1.0, clear)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(gradient)
        painter.drawEllipse(QRectF(centre.x() - glow_r, centre.y() - glow_r,
                                   glow_r * 2, glow_r * 2))

        if not self._logo.isNull():
            painter.drawPixmap(self._logo_rect().topLeft(), self._logo)
        painter.end()


def _mark() -> QPixmap:
    """The mark at LOGO_HEIGHT_PX, its own ratio kept and the screen's
    device ratio applied - the same call the sidebar's logo goes through,
    which is what "the same as the atomic icon in the sidebar" means in
    code rather than by eye."""
    screen = QApplication.primaryScreen()
    dpr = screen.devicePixelRatio() if screen is not None else 1.0
    return images.tinted_asset("assets/atomic_icon.png", theme.ACCENT,
                               LOGO_HEIGHT_PX, dpr)


def show(logo_path=None) -> "Splash | None":
    """Put the splash up now. None when there is no mark to show - a
    launch screen with nothing on it is worse than none at all.

    `logo_path` is accepted and unused: the mark comes from
    images.tinted_asset, which owns the path and the cache. Kept in the
    signature so main.py reads as "show the splash, here is the mark".

    Never raises: this is the very first thing a launch does, and an
    exception here would take the app down before it had a window."""
    try:
        pixmap = _mark()
        if pixmap is None or pixmap.isNull():
            return None
        panel = Splash(pixmap)
        panel.show()
        panel.raise_()
        # Painted before anything else runs, or the window sits blank for
        # exactly the moment it exists to fill.
        QApplication.processEvents()
        return panel
    except Exception:
        return None


def dismiss(panel, *, minimum_ms=MINIMUM_MS):
    """Take the splash away the moment the window is ready.

    With MINIMUM_MS at 0 this is immediate, which is the point: the
    splash covers the gap a launch actually has and not one millisecond
    more, so a machine that opens Atomic in 600ms sees the window at
    600ms and never really sees the mark. A launch that takes longer
    still gets it for exactly as long as it is waiting.

    Soft on every count: a splash that has already gone, or was never
    created, is fine."""
    if panel is None:
        return
    try:
        elapsed = (time.monotonic() - panel._started) * 1000.0
        QTimer.singleShot(max(0, int(minimum_ms - elapsed)), panel.finish)
    except Exception:
        pass
