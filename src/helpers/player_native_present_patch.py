"""Windows player presentation path aligned with Stremio and mpv VRR guidance.

This is the exact 1.10.139 native Windows presentation patch. It changes only
how libmpv is embedded/decoded on Windows; it does not change buffering,
source selection, seeking, resume, UI motion, or scrolling.
"""
from __future__ import annotations

import ctypes
import os

_INSTALLED = False
_BACKEND_PATCHED = False

_GA_ROOT = 2
_HWND_BOTTOM = 1
_SWP_NOSIZE = 0x0001
_SWP_NOMOVE = 0x0002
_SWP_NOACTIVATE = 0x0010
_SWP_NOOWNERZORDER = 0x0200


def _root_hwnd(hwnd: int) -> int:
    if os.name != "nt" or not hwnd:
        return int(hwnd or 0)
    try:
        user32 = ctypes.windll.user32
        user32.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        user32.GetAncestor.restype = ctypes.c_void_p
        root = user32.GetAncestor(ctypes.c_void_p(int(hwnd)), _GA_ROOT)
        value = int(root or 0)
        return value or int(hwnd)
    except Exception:
        return int(hwnd)


def _lower_native(hwnd: int) -> None:
    if os.name != "nt" or not hwnd:
        return
    try:
        ctypes.windll.user32.SetWindowPos(
            ctypes.c_void_p(int(hwnd)), ctypes.c_void_p(_HWND_BOTTOM),
            0, 0, 0, 0,
            _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOACTIVATE | _SWP_NOOWNERZORDER,
        )
    except Exception:
        pass


def _patch_backend() -> None:
    global _BACKEND_PATCHED
    if _BACKEND_PATCHED or os.name != "nt":
        return
    _BACKEND_PATCHED = True
    from . import video_backend
    original_create = video_backend.create

    def create(window_id: int, **overrides):
        surface_hwnd = int(window_id)
        root_hwnd = _root_hwnd(surface_hwnd)
        tuned = {"hwdec": "auto-copy-safe"}
        tuned.update(overrides)
        if root_hwnd != surface_hwnd:
            _lower_native(surface_hwnd)
            try:
                return original_create(root_hwnd, **tuned)
            except video_backend.PlayerError:
                return original_create(surface_hwnd, **tuned)
        return original_create(surface_hwnd, **tuned)

    video_backend.create = create


def install() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True
    _patch_backend()
