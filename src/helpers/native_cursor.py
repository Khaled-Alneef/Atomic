"""Force Windows to show the cursor Qt intends, when Qt itself won't.

Qt normally owns this entirely, and nothing here should be needed. But
after a modal dialog closes, Qt stops issuing native cursor calls for the
main window: its own state is completely correct - no override cursor, no
widget claiming one, the widget under the pointer reporting a plain arrow
- and yet Windows goes on painting whatever cursor was last set, which is
the pointing hand from whichever button or card was hovered on the way
in. That hand then follows the pointer across the entire app.

All of that was measured rather than guessed: the OS cursor is read back
with GetCursorInfo, and every repair available through Qt (pushing and
popping override cursors of several shapes, giving the window an explicit
arrow, taking the hand off every widget that held one) leaves it
unchanged. Setting the cursor through the Win32 API does fix it, so that
is what this does - as a correction applied only when the two genuinely
disagree, never as the normal path.

Windows-only, like the dark title bar in theme.py; everywhere else this
is inert and Qt keeps full control.
"""

import ctypes
import sys

from PyQt6.QtCore import Qt

_IS_WINDOWS = sys.platform == "win32"

# The standard Windows cursors (IDC_*), for the shapes this app actually
# asks for. Anything not listed is left to Qt.
_IDC_FOR_SHAPE = {
    Qt.CursorShape.ArrowCursor: 32512,
    Qt.CursorShape.PointingHandCursor: 32649,
    Qt.CursorShape.IBeamCursor: 32513,
    Qt.CursorShape.WaitCursor: 32514,
    Qt.CursorShape.SizeHorCursor: 32644,
    Qt.CursorShape.SizeVerCursor: 32645,
    Qt.CursorShape.SizeAllCursor: 32646,
    Qt.CursorShape.ForbiddenCursor: 32648,
}


if _IS_WINDOWS:
    _user32 = ctypes.windll.user32

    class _POINT(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

    class _CURSORINFO(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint), ("flags", ctypes.c_uint),
                    ("hCursor", ctypes.c_void_p), ("ptScreenPos", _POINT)]


def _current_handle():
    info = _CURSORINFO()
    info.cbSize = ctypes.sizeof(_CURSORINFO)
    if not _user32.GetCursorInfo(ctypes.byref(info)):
        return None
    return info.hCursor


def enforce(shape) -> bool:
    """Make the OS cursor match `shape` if it doesn't already.

    Returns True only when a correction was actually needed, so callers
    can tell "Qt is behaving" from "Qt had stopped updating". Fails soft:
    a shape with no Windows equivalent, or any API hiccup, just leaves
    things as they are."""
    if not _IS_WINDOWS:
        return False
    idc = _IDC_FOR_SHAPE.get(shape)
    if idc is None:
        return False
    try:
        wanted = _user32.LoadCursorW(None, idc)
        if not wanted or _current_handle() == wanted:
            return False
        _user32.SetCursor(wanted)
        return True
    except Exception:
        return False
