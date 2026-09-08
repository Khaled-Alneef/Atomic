"""Icons drawn by hand, or taken from the owner's own picture, where a
Segoe glyph was not what he wanted (7 September 2026).

**The globe is his picture, pixel for pixel.** He sent a 55x43 crop of
the icon he wanted "everywhere in the app" - a globe drawn as a network,
curved lines with dots at their joints - and it is in neither Segoe icon
face (every glyph of both was scored against its ink; the nearest were
plain circles). So its white ink is kept here as a 35x35 alpha mask
(GLOBE_PNG_B64) and drawn tinted at whatever size a button needs: the
player's settings button and both readers' site buttons (the web one
through a CSS mask of the same bytes, GLOBE_DATA_URI). Not the
sidebar's Websites icon: that one he asked to have back as it was, the
same evening ("retrieve the websites Icon in the main sidebar ONLY
IT"), so rail_anim keeps its own animated globe. At the 22-27 device
pixels the bars draw it at, that is about the picture's own resolution.

The audio button is the waveform he sent in place of Fluent's audio
glyph: seven bars, drawn as vectors so they are crisp at every ratio.
IconButton paints either itself, on top of the QSS ground the player's
other buttons use - a QIcon on a QPushButton came out soft and clipped
at the edges on his 125% panel, which was his "the Audio is blurred".
"""

import base64

from PyQt6.QtCore import QByteArray, QPointF, QRectF, Qt
from PyQt6.QtGui import QColor, QIcon, QImage, QPainter, QPen, QPixmap
from PyQt6.QtWidgets import QPushButton

from . import theme

# The owner's globe: a 35x35 PNG, white with the ink in its alpha.
GLOBE_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAACMAAAAjCAYAAAAe2bNZAAADpUlEQVR42u2Yz29VRRTHP6/6"
    "N7iTR2l04caIRqVCCD8M6EqFaNRgJJq4IIEQ0VSBlgQi1QUmRhcmNKgloWiMe7VNqUVMSFwa"
    "Q19pS/0r2vf6dfO9cDKde99t7YKFk5y8mTlnZr73nDPnnHkNSdwvrYf7qD24jjVPAA8DzwLP"
    "uV+0eWABmAamgJk17SypLj0paVhSJ1C7C92WtK/uGY2aPvMpcCKZWwBGAQFN4BBwzf3Niexl"
    "4DPgr/+imackTQdNzEk6m8jssRYueTwoqSVpKKOld6vO62aWaJKzGZkCyFCY6zUYJG1JQN2W"
    "dGY9YOYDkP0Z/m4fsDvD+1XSzjB+RNKs5VsJryuYqQDkhRKZtk2S4w1KOpXMbQkaaknaXAfM"
    "sQBktOSwXySNV2h1Vwn/7QDoYh0w85JW/Jvy9vqr25KaFWD6gt+kdDFoZ1cVmOO61w4kvGsh"
    "tox3AXLJctFZmwFIO7dPutF1A7mSzB9NblbH17QvA2YiObAlacT90/aVwcBbBaYpaVPQylWP"
    "C/4Vz6+UROCWr22rJBJPJMAfDbw9xXxPiKavhlh4E2g499zw7x3PAVwHHgAeNx0BhoHFsEcj"
    "0CIwG3i3gN/M25GLwNFfkHTB/RtW7QmPX67wl9GMOTv2tzLZb1PNADwErDjjTgMHgPeBbc7G"
    "HWAM+Kkiu3zuXJXSJmAOOO+sf1cXzmWr6pllM7f54CZwIfCfrpFQm1b9mMdjXnfQQLf7Y7Ym"
    "ZlxlpsPBqcpMcL7CRAet9iMedyRtzcgNO4YVUf5yzkyzNtNKiRbmrLVcewW4ChwHvrJDA/yZ"
    "kf0R+MeXgqiZnqRKK9r2zCazNl/aPgF+AI4BX3iuH/i+BPhN4GgY/5EDcweYdP/5zCYdl5gj"
    "QB+wD2gDrwMvAV8G2X5f/7LWH/pTZcXVgKQlSX9nbL1kisHsXEVGf6zCv4rU8l23RLkU6Jak"
    "n01pVN1fkbHbFUBeDPHnvcjLPVVGQv8bq3ExI7dQYoI3vK6sfeAQMgV8XacGLjQzE+beCUlw"
    "ouLLZyS9WcKLSXRv3UrvGYNZdiGVq1VOZ9a95TW5PUcCkKG11sADFWXiTs8fylSAH2X2Gi+r"
    "YeqCwRtHQKdChXfSDj7gcb+1GdefTJx+ZCMecTN+mDUciSf9YNsBHAYmQiSdBHo9H8uJM8C5"
    "jXrefpiJNctJKCj8LNLHdc9YC5iCXnPciQemYKL52Oi3Nl1Ce6/NB/D7ejdq/P9nUUn7F/xu"
    "e1Le9VOcAAAAAElFTkSuQmCC"
)
GLOBE_DATA_URI = "data:image/png;base64," + GLOBE_PNG_B64
GLOBE_PNG = base64.b64decode(GLOBE_PNG_B64)

RATIOS = (1.0, 1.25, 1.5, 1.75, 2.0)
# Bar heights of the waveform, as fractions of the tall bar.
WAVE_BARS = (0.34, 0.62, 1.0, 0.76, 1.0, 0.56, 0.34)

_globe_source = None
_tinted = {}


def globe_image(px: int, colour=None) -> QImage:
    """The globe tinted `colour` (theme.TEXT by default) at `px` device
    pixels a side, cached per size and colour."""
    global _globe_source
    ink = QColor(colour or theme.TEXT)
    key = (int(px), ink.name())
    found = _tinted.get(key)
    if found is not None:
        return found
    if _globe_source is None:
        _globe_source = QImage.fromData(QByteArray(GLOBE_PNG), "PNG")
    scaled = _globe_source.scaled(
        int(px), int(px), Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation)
    image = QImage(scaled.size(), QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(ink)
    painter = QPainter(image)
    painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationIn)
    painter.drawImage(0, 0, scaled)
    painter.end()
    _tinted[key] = image
    return image


def paint_globe(painter, rect: QRectF, colour=None):
    """His globe, fitted into `rect` (logical px) at the painter's ratio."""
    dpr = float(painter.device().devicePixelRatioF() or 1.0)
    px = max(1, int(round(min(rect.width(), rect.height()) * dpr)))
    image = globe_image(px, colour)
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
    side = px / dpr
    target = QRectF(rect.center().x() - side / 2, rect.center().y() - side / 2,
                    side, side)
    painter.drawImage(target, image)
    painter.restore()


def paint_waveform(painter, rect: QRectF, colour=None):
    """Seven bars, the tall ones in the middle, spanning the whole of
    `rect`'s width - his "stretch its width a bit, it seems truncated"."""
    ink = QColor(colour or theme.TEXT)
    size = min(rect.width(), rect.height())
    # Thinner than the first cut (0.11 of the box) at his word, 7
    # September 2026: "make the lines in the symbol a little thinner".
    pen = QPen(ink, size * 0.075)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setPen(pen)
    inset = size * 0.10
    left, right = rect.left() + inset, rect.right() - inset
    gap = (right - left) / (len(WAVE_BARS) - 1)
    tall = size * 0.80
    cy = rect.center().y()
    for index, height in enumerate(WAVE_BARS):
        x = left + gap * index
        half = tall * height / 2
        painter.drawLine(QPointF(x, cy - half), QPointF(x, cy + half))
    painter.restore()


class IconButton(QPushButton):
    """A QPushButton that paints its icon itself, on top of whatever
    ground its stylesheet gives it (hover, pressed)."""

    def __init__(self, paint, icon_px: int = 24, parent=None):
        super().__init__("", parent)
        self._paint = paint
        self._icon_px = int(icon_px)

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        side = float(self._icon_px)
        rect = QRectF((self.width() - side) / 2, (self.height() - side) / 2,
                      side, side)
        colour = theme.TEXT_DIM if not self.isEnabled() else theme.TEXT
        try:
            self._paint(painter, rect, colour)
        finally:
            painter.end()


def _icon_from(paint, px: int) -> QIcon:
    icon = QIcon()
    for ratio in RATIOS:
        pixmap = QPixmap(int(round(px * ratio)), int(round(px * ratio)))
        pixmap.setDevicePixelRatio(ratio)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        try:
            paint(painter, QRectF(0, 0, float(px), float(px)), None)
        finally:
            painter.end()
        icon.addPixmap(pixmap)
    return icon


def globe(colour=None, px: int = 22) -> QIcon:
    """The globe as a QIcon, for a button that cannot paint itself."""
    return _icon_from(lambda p, r, c: paint_globe(p, r, colour), px)


def waveform(colour=None, px: int = 22) -> QIcon:
    return _icon_from(lambda p, r, c: paint_waveform(p, r, colour), px)

