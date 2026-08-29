"""Windows player presentation path aligned with Stremio and mpv VRR guidance.

This patch deliberately changes only two player-level facts. It does not alter
Atomic's source resolver, player UI, buffering, watched-state, subtitles,
seeking, colorspace, interpolation, timing mode, or any app scrolling/rendering.

1. Atomic historically hands libmpv a dedicated Qt VideoSurface HWND, so mpv's
   D3D11 child lives one native-window level deeper than the official Stremio
   Shell NG player. Stremio gives libmpv its real player/top-level HWND. Atomic
   now tries that same direct-parent topology first. The old VideoSurface stays
   as the bottom-most black/fallback sibling, and Atomic's existing native
   controls remain siblings above mpv. If a machine rejects direct-parent
   embedding, creation retries the original VideoSurface HWND automatically.

2. Atomic's hwdec=auto-safe resolves to direct d3d11va on Windows. mpv issue
   #13304 demonstrates that direct d3d11va can disable VRR, while d3d11va-copy
   does not. Current mpv exposes auto-copy-safe to restrict auto-probing to safe
   copying hardware decoders. Atomic uses that Windows-only mode, preserving
   hardware decoding without tying decoder surfaces directly to presentation.

There is no SVP, frame generation, display-resample, cadence lock, MMCSS, or
motion interpolation in this path.
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
    """Keep Atomic's old VideoSurface behind mpv's direct child."""
    if os.name != "nt" or not hwnd:
        return
    try:
        ctypes.windll.user32.SetWindowPos(
            ctypes.c_void_p(int(hwnd)),
            ctypes.c_void_p(_HWND_BOTTOM),
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
        # PlayerPage still passes the real VideoSurface handle here. Keep it so
        # fallback is genuine; resolve the Stremio-style direct parent only at
        # the final libmpv creation boundary.
        surface_hwnd = int(window_id)
        root_hwnd = _root_hwnd(surface_hwnd)

        tuned = {
            # mpv's copy-safe auto mode skips direct d3d11va surfaces. This is
            # the sole decoder change; VO/sync/colorspace remain Atomic's
            # already-stable values.
            "hwdec": "auto-copy-safe",
        }
        tuned.update(overrides)

        if root_hwnd != surface_hwnd:
            _lower_native(surface_hwnd)
            try:
                return original_create(root_hwnd, **tuned)
            except video_backend.PlayerError:
                # Restore the original embedding topology, not a different
                # player configuration. A direct-parent incompatibility must
                # never turn into 'player unavailable'.
                return original_create(surface_hwnd, **tuned)

        return original_create(surface_hwnd, **tuned)

    video_backend.create = create


def install() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True
    _patch_backend()
