"""Windows-only DWM scheduling for the embedded libmpv player.

Atomic already gives mpv a native HWND and lets mpv own the D3D11 swapchain.
Do not add another renderer, frame pump, interpolation layer, refresh override, or
Qt-driven presentation path here.

The remaining Windows composition hop is DWM. Media applications can opt DWM
into Multimedia Class Scheduler Service (MMCSS) scheduling with
DwmEnableMMCSS(TRUE); Microsoft's frame-timing documentation calls this out for
video presentation, and Stremio's Qt/libmpv shell does the same when its mpv
renderer is created.

Keep the opt-in scoped to live mpv handles. The first player enables it, the
last shutdown disables it. Failure is deliberately soft: unsupported/broken
Windows environments fall back to Atomic's previous behavior.
"""

from __future__ import annotations

import ctypes
import sys
import threading

_INSTALLED = False
_LOCK = threading.Lock()
_ACTIVE_HANDLES: set[int] = set()
_DWM_MMCSS_ON = False
_DWM_ENABLE_MMCSS = None


def _dwm_enable_mmcss(enable: bool) -> bool:
    """Ask DWM to participate in MMCSS scheduling; never raise."""
    global _DWM_ENABLE_MMCSS
    if sys.platform != "win32":
        return False
    try:
        if _DWM_ENABLE_MMCSS is None:
            fn = ctypes.WinDLL("dwmapi").DwmEnableMMCSS
            fn.argtypes = [ctypes.c_int]
            fn.restype = ctypes.c_long  # HRESULT
            _DWM_ENABLE_MMCSS = fn
        hr = int(_DWM_ENABLE_MMCSS(1 if enable else 0))
        return hr >= 0
    except Exception:
        return False


def _register_handle(handle) -> None:
    """Enable DWM MMCSS for the first live mpv instance."""
    global _DWM_MMCSS_ON
    if handle is None or sys.platform != "win32":
        return
    key = id(handle)
    with _LOCK:
        if key in _ACTIVE_HANDLES:
            return
        if not _ACTIVE_HANDLES and not _DWM_MMCSS_ON:
            _DWM_MMCSS_ON = _dwm_enable_mmcss(True)
        _ACTIVE_HANDLES.add(key)


def _unregister_handle(handle) -> None:
    """Drop one player lease and restore the pre-player DWM policy."""
    global _DWM_MMCSS_ON
    if handle is None or sys.platform != "win32":
        return
    key = id(handle)
    with _LOCK:
        if key not in _ACTIVE_HANDLES:
            return
        _ACTIVE_HANDLES.discard(key)
        if not _ACTIVE_HANDLES and _DWM_MMCSS_ON:
            _dwm_enable_mmcss(False)
            _DWM_MMCSS_ON = False


def install():
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    from . import video_backend

    old_create = video_backend.create
    old_shutdown = video_backend.shutdown

    def create(window_id: int, **overrides):
        # Keep Atomic's already-tested mpv option set exactly as-is. In
        # particular: no video-sync override, no interpolation, no tscale, no
        # cadence lock. mpv continues to own native D3D11 presentation.
        handle = old_create(window_id, **overrides)
        _register_handle(handle)
        return handle

    def shutdown(handle):
        try:
            return old_shutdown(handle)
        finally:
            _unregister_handle(handle)

    video_backend.create = create
    video_backend.shutdown = shutdown
