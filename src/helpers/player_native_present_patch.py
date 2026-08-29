"""Windows player presentation path aligned with Stremio and mpv VRR guidance.

This is intentionally narrow. It does not alter Atomic's source resolver,
player UI, buffering policy, watched-state logic, subtitles, seeking, or any
app-wide rendering/scrolling code.

Two Windows-only differences are corrected:

1. Atomic historically handed libmpv a dedicated Qt VideoSurface HWND. Because
   that widget uses WA_DontCreateNativeAncestors, its native parent is the real
   Atomic top-level window; mpv then created another D3D11 child *inside* that
   surface. Official Stremio Shell NG instead hands libmpv the real parent HWND
   directly. We do the same and leave VideoSurface as the bottom-most
   fallback/background sibling. Atomic's native control bars remain siblings
   above mpv, so their existing DWM alpha/stacking code keeps working.

2. Atomic's hwdec=auto-safe resolves to direct d3d11va on Windows. mpv issue
   #13304 demonstrates that direct d3d11va disables VRR while d3d11va-copy does
   not. mpv itself provides auto-copy-safe specifically to choose only safe
   copying hardware decoders. This keeps hardware decoding while decoupling
   decoder surfaces from the presentation swapchain.

No interpolation, frame generation, display-resample, cadence lock, or MMCSS
is involved.
"""

from __future__ import annotations

import ctypes
import os
import sys

_INSTALLED = False
_PLAYER_PATCHED = False
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
    """Keep the old Qt VideoSurface behind mpv's new direct child."""
    if os.name != "nt" or not hwnd:
        return
    try:
        user32 = ctypes.windll.user32
        user32.SetWindowPos(
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
        surface_hwnd = int(window_id)
        root_hwnd = _root_hwnd(surface_hwnd)

        # The surface remains useful as Atomic's black pre-frame background,
        # but it must be behind the direct mpv child rather than wrapping it.
        if root_hwnd != surface_hwnd:
            _lower_native(surface_hwnd)

        tuned = {
            # mpv's copy-safe autoprobe skips direct d3d11va and therefore
            # avoids the Windows VRR regression documented in mpv #13304.
            "hwdec": "auto-copy-safe",
            # Match official Stremio's Windows VO ordering explicitly.
            "vo": "gpu-next,gpu,",
            "gpu_context": "d3d11",
            # Do not force Atomic's previous one-way colorspace hint. Stremio
            # lets D3D11/libplacebo negotiate the output first, then adjusts
            # HDR/SDR only when the display state actually requires it.
            "d3d11_output_format": "auto",
            "d3d11_output_csp": "auto",
            "target_colorspace_hint": "auto",
            "target_colorspace_hint_mode": "target",
        }
        tuned.update(overrides)

        try:
            return original_create(root_hwnd, **tuned)
        except video_backend.PlayerError:
            # A driver/Qt combination that refuses direct-parent embedding must
            # still have a working player. Retry only the embedding location;
            # keep the copy-safe decode path because it is independent of HWND
            # hierarchy and is the VRR-safe choice.
            if root_hwnd == surface_hwnd:
                raise
            return original_create(surface_hwnd, **tuned)

    video_backend.create = create


def _patch_player(player) -> None:
    global _PLAYER_PATCHED
    if _PLAYER_PATCHED or os.name != "nt":
        return
    _PLAYER_PATCHED = True

    Surface = player.VideoSurface
    original_native_handle = Surface.native_handle

    def native_handle(self):
        surface_hwnd = int(original_native_handle(self))
        root_hwnd = _root_hwnd(surface_hwnd)
        try:
            self._atomic_surface_hwnd = surface_hwnd
            self._atomic_direct_parent_hwnd = root_hwnd
        except Exception:
            pass
        if root_hwnd != surface_hwnd:
            _lower_native(surface_hwnd)
        # video_backend.create performs the same resolution defensively, but
        # returning the root here means every player diagnostic sees the actual
        # HWND Atomic intends to use, rather than reporting the legacy surface.
        return root_hwnd

    Surface.native_handle = native_handle


def install() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    if os.name != "nt":
        return

    _patch_backend()

    loaded = sys.modules.get("windows.player")
    if loaded is not None:
        _patch_player(loaded)
        return

    # Chain onto the existing player import hook. This avoids installing a
    # second competing MetaPathFinder and preserves the 85%-watched patch and
    # the Back-button patch that chain through the same hook.
    from . import player_watch_threshold_patch as threshold

    previous = threshold._patch

    def chained(module):
        previous(module)
        _patch_player(module)

    threshold._patch = chained
