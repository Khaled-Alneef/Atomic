"""Pull a file's shell icon (the same icon Explorer shows for it) as a
PIL Image, using only ctypes + the Win32 API - no extra dependencies."""

import ctypes
import os
import sys
from ctypes import wintypes

from PIL import Image

SHGFI_ICON = 0x000000100
SHGFI_LARGEICON = 0x000000000
DIB_RGB_COLORS = 0
BI_RGB = 0


class SHFILEINFO(ctypes.Structure):
    _fields_ = [
        ("hIcon", wintypes.HICON),
        ("iIcon", ctypes.c_int),
        ("dwAttributes", wintypes.DWORD),
        ("szDisplayName", ctypes.c_wchar * 260),
        ("szTypeName", ctypes.c_wchar * 80),
    ]


class BITMAP(ctypes.Structure):
    _fields_ = [
        ("bmType", ctypes.c_long),
        ("bmWidth", ctypes.c_long),
        ("bmHeight", ctypes.c_long),
        ("bmWidthBytes", ctypes.c_long),
        ("bmPlanes", ctypes.c_ushort),
        ("bmBitsPixel", ctypes.c_ushort),
        ("bmBits", ctypes.c_void_p),
    ]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", ctypes.c_long),
        ("biHeight", ctypes.c_long),
        ("biPlanes", ctypes.c_ushort),
        ("biBitCount", ctypes.c_ushort),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", ctypes.c_long),
        ("biYPelsPerMeter", ctypes.c_long),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class ICONINFO(ctypes.Structure):
    _fields_ = [
        ("fIcon", wintypes.BOOL),
        ("xHotspot", wintypes.DWORD),
        ("yHotspot", wintypes.DWORD),
        ("hbmMask", wintypes.HBITMAP),
        ("hbmColor", wintypes.HBITMAP),
    ]


def _configure_prototypes():
    """Declare explicit argtypes/restypes for every Win32 call we make.

    Without this, ctypes guesses argument types from the Python ints we
    pass in, and 64-bit HANDLE/HBITMAP values can overflow that guess
    (c_int) and raise OverflowError. Handles here are always HANDLE-sized.
    """
    shell32 = ctypes.windll.shell32
    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32

    shell32.SHGetFileInfoW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(SHFILEINFO),
        ctypes.c_uint, wintypes.UINT,
    ]
    shell32.SHGetFileInfoW.restype = ctypes.c_void_p

    user32.GetIconInfo.argtypes = [wintypes.HICON, ctypes.POINTER(ICONINFO)]
    user32.GetIconInfo.restype = wintypes.BOOL
    user32.DestroyIcon.argtypes = [wintypes.HICON]
    user32.DestroyIcon.restype = wintypes.BOOL
    user32.GetDC.argtypes = [wintypes.HWND]
    user32.GetDC.restype = wintypes.HDC
    user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    user32.ReleaseDC.restype = ctypes.c_int

    gdi32.GetObjectW.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p]
    gdi32.GetObjectW.restype = ctypes.c_int
    gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
    gdi32.CreateCompatibleDC.restype = wintypes.HDC
    gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    gdi32.SelectObject.restype = wintypes.HGDIOBJ
    gdi32.GetDIBits.argtypes = [
        wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT,
        ctypes.c_void_p, ctypes.POINTER(BITMAPINFOHEADER), wintypes.UINT,
    ]
    gdi32.GetDIBits.restype = ctypes.c_int
    gdi32.DeleteDC.argtypes = [wintypes.HDC]
    gdi32.DeleteDC.restype = wintypes.BOOL
    gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    gdi32.DeleteObject.restype = wintypes.BOOL


if sys.platform == "win32":
    _configure_prototypes()


def extract_icon(path: str, size: int = 64):
    """Return a resized RGBA PIL.Image of `path`'s shell icon, or None if
    extraction fails for any reason (missing file, non-Windows, no icon)."""
    if sys.platform != "win32":
        return None

    # SHGetFileInfoW silently fails to resolve .url/.lnk icons when given
    # a forward-slash path (which is exactly what filedialog and our JSON
    # store) - it needs native backslashes.
    path = os.path.normpath(str(path))

    shell32 = ctypes.windll.shell32
    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32

    hicon = None
    mem_dc = None
    hdc = None
    hbm_color = None
    hbm_mask = None
    try:
        info = SHFILEINFO()
        res = shell32.SHGetFileInfoW(
            str(path), 0, ctypes.byref(info), ctypes.sizeof(info),
            SHGFI_ICON | SHGFI_LARGEICON,
        )
        if not res or not info.hIcon:
            return None
        hicon = info.hIcon

        icon_info = ICONINFO()
        if not user32.GetIconInfo(hicon, ctypes.byref(icon_info)):
            return None
        hbm_color = icon_info.hbmColor
        hbm_mask = icon_info.hbmMask

        bmp = BITMAP()
        gdi32.GetObjectW(hbm_color, ctypes.sizeof(bmp), ctypes.byref(bmp))
        width, height = bmp.bmWidth, bmp.bmHeight
        if width <= 0 or height <= 0:
            return None

        hdc = user32.GetDC(0)
        mem_dc = gdi32.CreateCompatibleDC(hdc)
        old_obj = gdi32.SelectObject(mem_dc, hbm_color)

        header = BITMAPINFOHEADER()
        header.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        header.biWidth = width
        header.biHeight = -height  # negative = top-down DIB
        header.biPlanes = 1
        header.biBitCount = 32
        header.biCompression = BI_RGB

        buffer = ctypes.create_string_buffer(width * height * 4)
        gdi32.GetDIBits(mem_dc, hbm_color, 0, height, buffer,
                         ctypes.byref(header), DIB_RGB_COLORS)
        gdi32.SelectObject(mem_dc, old_obj)

        img = Image.frombuffer("RGBA", (width, height), buffer.raw, "raw", "BGRA", 0, 1)

        # Icons with no real alpha channel come back fully transparent;
        # fall back to the AND mask for those.
        alpha_extrema = img.getchannel("A").getextrema()
        if alpha_extrema == (0, 0):
            mask_buf = ctypes.create_string_buffer(width * height * 4)
            mask_dc = gdi32.CreateCompatibleDC(hdc)
            old_mask_obj = gdi32.SelectObject(mask_dc, hbm_mask)
            gdi32.GetDIBits(mask_dc, hbm_mask, 0, height, mask_buf,
                             ctypes.byref(header), DIB_RGB_COLORS)
            gdi32.SelectObject(mask_dc, old_mask_obj)
            gdi32.DeleteDC(mask_dc)
            mask_img = Image.frombuffer("RGBA", (width, height), mask_buf.raw, "raw", "BGRA", 0, 1)
            alpha = mask_img.getchannel("R").point(lambda p: 0 if p else 255)
            img.putalpha(alpha)

        img = img.resize((size, size), Image.LANCZOS)
        return img
    except Exception:
        return None
    finally:
        if mem_dc:
            gdi32.DeleteDC(mem_dc)
        if hdc:
            user32.ReleaseDC(0, hdc)
        if hbm_color:
            gdi32.DeleteObject(hbm_color)
        if hbm_mask:
            gdi32.DeleteObject(hbm_mask)
        if hicon:
            user32.DestroyIcon(hicon)
