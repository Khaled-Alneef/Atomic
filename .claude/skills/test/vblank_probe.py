"""Measure the PRIMARY panel's real refresh interval from the kernel's vblank
event (D3DKMTWaitForVerticalBlankEvent) - which follows the scanout, so a
VRR panel that has dropped to the video's rate shows here as a longer
interval. Separate process; prints a line per second.
usage: python vblank_probe.py <seconds> [display-name]"""
import ctypes, sys, time, statistics
gdi = ctypes.WinDLL("gdi32")


class LUID(ctypes.Structure):
    _fields_ = [("LowPart", ctypes.c_uint32), ("HighPart", ctypes.c_int32)]


class OPEN(ctypes.Structure):
    _fields_ = [("hDc", ctypes.c_void_p), ("hAdapter", ctypes.c_uint32),
                ("AdapterLuid", LUID), ("VidPnSourceId", ctypes.c_uint32)]


class WAIT(ctypes.Structure):
    _fields_ = [("hAdapter", ctypes.c_uint32), ("hDevice", ctypes.c_uint32),
                ("VidPnSourceId", ctypes.c_uint32)]


gdi.CreateDCW.restype = ctypes.c_void_p
gdi.CreateDCW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_void_p]
name = sys.argv[2] if len(sys.argv) > 2 else "\\\\.\\DISPLAY1"
hdc = gdi.CreateDCW(name, None, None, None)
if not hdc:
    print("CreateDC failed for", name); sys.exit(1)
o = OPEN(); o.hDc = hdc
st = gdi.D3DKMTOpenAdapterFromHdc(ctypes.byref(o))
if st != 0:
    print("open adapter failed", hex(st & 0xffffffff)); sys.exit(1)
wv = WAIT(); wv.hAdapter = o.hAdapter; wv.hDevice = 0; wv.VidPnSourceId = o.VidPnSourceId
seconds = float(sys.argv[1]); t0 = time.perf_counter(); last = t0; sec = []
while True:
    st = gdi.D3DKMTWaitForVerticalBlankEvent(ctypes.byref(wv))
    now = time.perf_counter()
    if st != 0:
        print("wait failed", hex(st & 0xffffffff)); break
    sec.append((now - last) * 1000); last = now
    if now - t0 > seconds:
        break
    if sec and sum(sec) >= 1000:
        s = sorted(sec)
        print(f"t={now - t0:5.1f}s vblanks={len(sec)} interval median {statistics.median(s):.2f}ms "
              f"p10 {s[len(s)//10]:.2f} p90 {s[9*len(s)//10]:.2f} max {s[-1]:.1f} "
              f"-> {1000/statistics.median(s):.1f}Hz", flush=True)
        sec = []
