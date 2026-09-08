"""Separate-process screen sampler: BitBlt a region of the PRIMARY panel as
fast as the screen DC allows (the call is the vblank wait, testing.md) and
record, per sample, the time and whether the pixels changed. Saves every
distinct frame (as uint8 gray arrays) with its first-seen time so a later
pass can compare them with the decoded source.

usage: python sampler.py <seconds> <out.npz> <x> <y> <w> <h>   (physical px)
"""
import ctypes, sys, time
import numpy as np
u = ctypes.WinDLL("user32"); g = ctypes.WinDLL("gdi32")
u.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
for f in (u.GetDC, g.CreateCompatibleDC, g.CreateDIBSection, g.SelectObject):
    f.restype = ctypes.c_void_p
u.GetDC.argtypes = [ctypes.c_void_p]
g.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
g.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
g.BitBlt.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                     ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_uint32]
class BIH(ctypes.Structure):
    _fields_ = [("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_int32), ("biHeight", ctypes.c_int32),
                ("biPlanes", ctypes.c_uint16), ("biBitCount", ctypes.c_uint16), ("biCompression", ctypes.c_uint32),
                ("biSizeImage", ctypes.c_uint32), ("biXPelsPerMeter", ctypes.c_int32), ("biYPelsPerMeter", ctypes.c_int32),
                ("biClrUsed", ctypes.c_uint32), ("biClrImportant", ctypes.c_uint32)]
class BMI(ctypes.Structure):
    _fields_ = [("bmiHeader", BIH), ("bmiColors", ctypes.c_uint32 * 3)]
g.CreateDIBSection.argtypes = [ctypes.c_void_p, ctypes.POINTER(BMI), ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p, ctypes.c_uint32]

seconds = float(sys.argv[1]); out = sys.argv[2]
x, y, w, h = (int(v) for v in sys.argv[3:7])
sdc = u.GetDC(None); mdc = g.CreateCompatibleDC(sdc)
bmi = BMI(); bmi.bmiHeader.biSize = ctypes.sizeof(BIH); bmi.bmiHeader.biWidth = w
bmi.bmiHeader.biHeight = -h; bmi.bmiHeader.biPlanes = 1; bmi.bmiHeader.biBitCount = 32
bits = ctypes.c_void_p()
dib = g.CreateDIBSection(sdc, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
g.SelectObject(mdc, dib)
buf = (ctypes.c_uint8 * (w * h * 4)).from_address(bits.value)
view = np.frombuffer(buf, dtype=np.uint8).reshape(h, w, 4)

times = []; changed = []; frames = []; frame_t = []
prev = None
t0 = time.perf_counter()
while True:
    g.BitBlt(mdc, 0, 0, w, h, sdc, x, y, 0x00CC0020)
    t = time.perf_counter() - t0
    if t > seconds:
        break
    gray = view[:, :, 1].copy()          # green channel is enough to tell frames apart
    if prev is None:
        diff = 255
    else:
        diff = int(np.abs(gray.astype(np.int16) - prev.astype(np.int16)).max())
    times.append(t); changed.append(diff)
    if diff > 8:                         # a new frame (8 levels hides dither/noise)
        frames.append(gray); frame_t.append(t)
    prev = gray
times = np.array(times); changed = np.array(changed)
np.savez_compressed(out, times=times, changed=changed, frames=np.array(frames), frame_t=np.array(frame_t))
d = np.diff(times) * 1000
print(f"samples {len(times)} over {times[-1]:.1f}s; blit period median {np.median(d):.2f}ms "
      f"p5 {np.percentile(d,5):.2f} p95 {np.percentile(d,95):.2f} -> {1000/np.median(d):.1f}Hz; "
      f"distinct frames {len(frames)} -> {len(frames)/times[-1]:.2f} fps")
# hold lengths in samples between frame changes
if len(frame_t) > 2:
    idx = np.nonzero(changed > 8)[0]
    holds = np.diff(idx)
    vals, counts = np.unique(holds, return_counts=True)
    print("hold (samples) distribution:", ", ".join(f"{v}:{c}" for v, c in zip(vals, counts)))
    ft = np.diff(frame_t) * 1000
    print(f"frame interval ms: median {np.median(ft):.1f} p5 {np.percentile(ft,5):.1f} p95 {np.percentile(ft,95):.1f} max {ft.max():.1f}")
