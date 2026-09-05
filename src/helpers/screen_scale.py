"""One app size on every monitor, whatever Windows has each one set to.

**The owner, three builds running:** *"the size of everything in the app
is not the same in the 1080P and the 2K, make the 1080P sizes same as
the 2K"*, and then, when both were pinned to 1.25, *"the size of the
1080P now became larger"*.

Every previous answer scaled by something that is not what he is
looking at:

    what was tried            2K            1080p         what he saw
    Floor, one factor 1.1     1.10 (1862w)  1.10 (1745w)  2K too small
    PassThrough (Windows)     1.25 (2048w)  1.00 (1920w)  still not equal
    Floor, one factor 1.25    1.25 (2048w)  1.25 (1536w)  1080p larger

The last row is the tell. With the same factor on both, a card is the
same number of pixels on each screen - but the 1080p panel has fewer
pixels to spend, so that card covers a bigger share of it and the whole
layout reads as zoomed in. Windows' own 125% does not fix it either,
because 2560/1920 is 1.333 and 125% is the nearest step it offers; that
leftover 6% is the "still not the same" of the middle row.

So the factor is taken from the screen's width instead: every monitor is
scaled to the same **logical** width, and the app is then laid out
identically on all of them - same cards per row, same proportions, same
size to the eye. Both of his panels are 27", so equal logical size is
also equal physical size here.

    2560 / 1920 = 1.3333      1920 / 1920 = 1.0000

Qt takes these through QT_SCREEN_SCALE_FACTORS, which must be set before
QGuiApplication exists - so the monitors are enumerated here with the
Win32 API rather than with Qt. **By name, never by index**: Qt lists the
primary screen first while EnumDisplayMonitors returns them in
attachment order, and on this machine those two orders are reversed, so
an ordered list would have handed each monitor the other's factor
(measured 31 August 2026). The names come from the display config's
`monitorFriendlyDeviceName`, which is the same string QScreen.name()
reports.

Fails soft in every direction: any error here leaves the environment
untouched and the app scales the way Qt would have on its own.
"""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes

# The width every layout number in this app was chosen against - see
# helpers/layout.py, which uses the same baseline for card sizes.
BASELINE_WIDTH = 1920.0
# A 4K panel wants 2.0; nothing sane wants more, and a factor under 1.0
# would render the app smaller than it was designed.
SCALE_MIN = 1.0
SCALE_MAX = 2.0

_QDC_ONLY_ACTIVE_PATHS = 2
_DEVICE_INFO_GET_TARGET_NAME = 2


class _LUID(ctypes.Structure):
    _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]


class _PATH_SOURCE_INFO(ctypes.Structure):
    _fields_ = [("adapterId", _LUID), ("id", wintypes.UINT),
                ("modeInfoIdx", wintypes.UINT),
                ("statusFlags", wintypes.UINT)]


class _PATH_TARGET_INFO(ctypes.Structure):
    _fields_ = [("adapterId", _LUID), ("id", wintypes.UINT),
                ("modeInfoIdx", wintypes.UINT),
                ("outputTechnology", wintypes.UINT),
                ("rotation", wintypes.UINT), ("scaling", wintypes.UINT),
                ("refreshRateNumerator", wintypes.UINT),
                ("refreshRateDenominator", wintypes.UINT),
                ("scanLineOrdering", wintypes.UINT),
                ("targetAvailable", wintypes.BOOL),
                ("statusFlags", wintypes.UINT)]


class _PATH_INFO(ctypes.Structure):
    _fields_ = [("sourceInfo", _PATH_SOURCE_INFO),
                ("targetInfo", _PATH_TARGET_INFO), ("flags", wintypes.UINT)]


class _2DREGION(ctypes.Structure):
    _fields_ = [("cx", wintypes.UINT), ("cy", wintypes.UINT)]


class _VIDEO_SIGNAL_INFO(ctypes.Structure):
    _fields_ = [("pixelRate", ctypes.c_uint64),
                ("hSyncFreqNum", wintypes.UINT),
                ("hSyncFreqDen", wintypes.UINT),
                ("vSyncFreqNum", wintypes.UINT),
                ("vSyncFreqDen", wintypes.UINT),
                ("activeSize", _2DREGION), ("totalSize", _2DREGION),
                ("videoStandard", wintypes.UINT),
                ("scanLineOrdering", wintypes.UINT)]


class _TARGET_MODE(ctypes.Structure):
    _fields_ = [("targetVideoSignalInfo", _VIDEO_SIGNAL_INFO)]


class _POINTL(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class _SOURCE_MODE(ctypes.Structure):
    _fields_ = [("width", wintypes.UINT), ("height", wintypes.UINT),
                ("pixelFormat", wintypes.UINT), ("position", _POINTL)]


class _MODE_UNION(ctypes.Union):
    _fields_ = [("targetMode", _TARGET_MODE), ("sourceMode", _SOURCE_MODE)]


class _MODE_INFO(ctypes.Structure):
    _fields_ = [("infoType", wintypes.UINT), ("id", wintypes.UINT),
                ("adapterId", _LUID), ("mode", _MODE_UNION)]


class _DEVICE_INFO_HEADER(ctypes.Structure):
    _fields_ = [("type", wintypes.UINT), ("size", wintypes.UINT),
                ("adapterId", _LUID), ("id", wintypes.UINT)]


class _TARGET_DEVICE_NAME(ctypes.Structure):
    _fields_ = [("header", _DEVICE_INFO_HEADER), ("flags", wintypes.UINT),
                ("outputTechnology", wintypes.UINT),
                ("edidManufactureId", wintypes.USHORT),
                ("edidProductCodeId", wintypes.USHORT),
                ("connectorInstance", wintypes.UINT),
                ("monitorFriendlyDeviceName", wintypes.WCHAR * 64),
                ("monitorDevicePath", wintypes.WCHAR * 128)]


def _screen_widths():
    """[(friendly name, width in physical pixels)] for every active
    monitor, or [] when the display config cannot be read."""
    user32 = ctypes.WinDLL("user32")
    paths = ctypes.c_uint32()
    modes = ctypes.c_uint32()
    if user32.GetDisplayConfigBufferSizes(
            _QDC_ONLY_ACTIVE_PATHS, ctypes.byref(paths),
            ctypes.byref(modes)) != 0:
        return []
    path_array = (_PATH_INFO * paths.value)()
    mode_array = (_MODE_INFO * modes.value)()
    if user32.QueryDisplayConfig(
            _QDC_ONLY_ACTIVE_PATHS, ctypes.byref(paths), path_array,
            ctypes.byref(modes), mode_array, None) != 0:
        return []

    found = []
    for path in path_array[:paths.value]:
        name = _TARGET_DEVICE_NAME()
        name.header.type = _DEVICE_INFO_GET_TARGET_NAME
        name.header.size = ctypes.sizeof(_TARGET_DEVICE_NAME)
        name.header.adapterId = path.targetInfo.adapterId
        name.header.id = path.targetInfo.id
        if user32.DisplayConfigGetDeviceInfo(ctypes.byref(name)) != 0:
            continue
        friendly = str(name.monitorFriendlyDeviceName or "").strip()
        # The source mode carries the desktop resolution this monitor is
        # actually running - the number the factor is computed from.
        index = path.sourceInfo.modeInfoIdx
        width = 0
        if index < modes.value:
            entry = mode_array[index]
            if entry.infoType == 1:             # SOURCE
                width = int(entry.mode.sourceMode.width)
        if friendly and width > 0:
            found.append((friendly, width))
    return found


def factors() -> dict:
    """{screen name: scale factor}, empty when it cannot be computed."""
    out = {}
    try:
        for name, width in _screen_widths():
            factor = max(SCALE_MIN, min(SCALE_MAX, width / BASELINE_WIDTH))
            out[name] = round(factor, 4)
    except Exception:
        return {}
    return out


def apply() -> str:
    """Set QT_SCREEN_SCALE_FACTORS for this process. Returns what was
    set, or "" when nothing could be determined - in which case Qt keeps
    its own behaviour and the app is no worse off than before."""
    if os.name != "nt" or os.environ.get("QT_SCREEN_SCALE_FACTORS"):
        return ""
    try:
        # Physical pixels, not scaled ones: without this the enumeration
        # answers in the primary monitor's scaled units and a 2560-wide
        # panel at 125% reads as 2048.
        user32 = ctypes.WinDLL("user32")
        user32.SetProcessDpiAwarenessContext.argtypes = (ctypes.c_void_p,)
        user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        pass
    mapping = factors()
    if not mapping:
        return ""
    value = ";".join(f"{name}={factor}" for name, factor in mapping.items())
    os.environ["QT_SCREEN_SCALE_FACTORS"] = value
    return value
