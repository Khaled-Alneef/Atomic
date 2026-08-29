"""Work around mpv's Windows/NVIDIA presentation-stutter path.

Atomic already uses mpv's default video-sync=audio. In mpv issue #15196,
multiple Windows 11/NVIDIA users with visible stutter but no reported frame
drops confirmed that swapchain-depth=1 fixes playback in that exact sync mode.
The reports of A/V drift were tied to display-resample, which Atomic does not
use.

Keep this surgical: only NVIDIA Windows systems get depth 1. AMD/Intel retain
mpv's own default. No interpolation, frame generation, renderer switch,
hardware-decoder change, playback-speed change, or app-wide rendering change.
"""

from __future__ import annotations

import ctypes
import os

_INSTALLED = False
_PATCHED = False
DEPTH = 1


class _DISPLAY_DEVICEW(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_ulong),
        ("DeviceName", ctypes.c_wchar * 32),
        ("DeviceString", ctypes.c_wchar * 128),
        ("StateFlags", ctypes.c_ulong),
        ("DeviceID", ctypes.c_wchar * 128),
        ("DeviceKey", ctypes.c_wchar * 128),
    ]


def _has_nvidia() -> bool:
    """Detect an NVIDIA display adapter without spawning another process."""
    if os.name != "nt":
        return False
    try:
        enum = ctypes.windll.user32.EnumDisplayDevicesW
        enum.argtypes = [ctypes.c_wchar_p, ctypes.c_ulong,
                         ctypes.POINTER(_DISPLAY_DEVICEW), ctypes.c_ulong]
        enum.restype = ctypes.c_int
        index = 0
        while True:
            device = _DISPLAY_DEVICEW()
            device.cb = ctypes.sizeof(device)
            if not enum(None, index, ctypes.byref(device), 0):
                break
            text = f"{device.DeviceString} {device.DeviceID}".upper()
            if "NVIDIA" in text or "VEN_10DE" in text:
                return True
            index += 1
    except Exception:
        pass

    # Driver installations normally expose nvidia-smi here even when it is not
    # on PATH. This is a fallback only; EnumDisplayDevices is the main route.
    root = os.environ.get("WINDIR") or r"C:\Windows"
    return os.path.isfile(os.path.join(root, "System32", "nvidia-smi.exe"))


def _patch_backend() -> None:
    global _PATCHED
    if _PATCHED or os.name != "nt" or not _has_nvidia():
        return
    _PATCHED = True

    from . import video_backend

    original_create = video_backend.create

    def create(window_id: int, **overrides):
        # One in-flight frame: wait until the current frame is visible before
        # rendering the next. This is the confirmed NVIDIA workaround for
        # mpv's default audio-sync path. Explicit diagnostic overrides still win.
        tuned = {"swapchain_depth": DEPTH}
        tuned.update(overrides)
        return original_create(window_id, **tuned)

    video_backend.create = create


def install() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True
    _patch_backend()
