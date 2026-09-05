"""Drive wheel input at the app and measure the motion from the screen.

usage: python scrollmeasure.py <out.npz> <seconds> <band_y_window_px> [delta count interval_ms]
Starts sampler.py (its own process) over a 1600x24 band of the window at
band_y, waits 0.6s, sends the wheel events at the window centre, and when
the sampler finishes prints: samples, distinct frames, the per-frame
vertical step of the band's content (found by correlating each distinct
frame's row profile with the previous), dead frames (a frame that changed
by <0.5px while motion was still under way), and the biggest step."""
import subprocess, sys, time, os
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rig
out, seconds, band_y = sys.argv[1], float(sys.argv[2]), int(sys.argv[3])
delta = int(sys.argv[4]) if len(sys.argv) > 4 else -120
count = int(sys.argv[5]) if len(sys.argv) > 5 else 5
interval = float(sys.argv[6]) if len(sys.argv) > 6 else 250.0
f = rig.find(); h, rect, _ = f
x0, y0 = rect[0] + 400, rect[1] + band_y
W, H = 1600, 160
proc = subprocess.Popen([sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "sampler.py"),
                         str(seconds), out, str(x0), str(y0), str(W), str(H)])
time.sleep(0.8)
rig.wheelto(delta, count, interval, 1290, band_y)
proc.wait()
d = np.load(out, allow_pickle=True)
keys = list(d.keys()); print("npz keys:", keys)
frames = d["frames"] if "frames" in keys else None
ft = d["frame_t"] if "frame_t" in keys else None
times = d["times"] if "times" in keys else None
print(f"samples {len(times) if times is not None else '?'} over {seconds}s; distinct frames {len(frames) if frames is not None else '?'}")
if frames is None or len(frames) < 2:
    sys.exit(0)
# vertical shift between consecutive distinct frames: 1-D column-mean profiles, best offset by SAD
prof = [fr.astype(np.float32).mean(axis=1) for fr in frames]   # per-row means -> vertical profile
steps = []
for i in range(1, len(prof)):
    a, b = prof[i - 1], prof[i]
    best, bestv = 0, None
    for s in range(-140, 141):
        if s >= 0: v = np.abs(a[s:] - b[:len(b) - s]).mean() if s < len(a) else 1e9
        else: v = np.abs(a[:s] - b[-s:]).mean()
        if bestv is None or v < bestv: best, bestv = s, v
    steps.append((float(ft[i] - ft[0]) if ft is not None else i, best))
moving = [s for s in steps if s[1] != 0]
if steps:
    t_first = moving[0][0] if moving else None; t_last = moving[-1][0] if moving else None
    inside = [s for s in steps if t_first is not None and t_first <= s[0] <= t_last]
    dead = [s for s in inside if abs(s[1]) < 1]
    sizes = sorted(abs(s[1]) for s in inside if abs(s[1]) >= 1)
    print(f"motion from {t_first:.3f}s to {t_last:.3f}s: {len(inside)} frames, moving {len(inside) - len(dead)}, dead {len(dead)} ({100.0 * len(dead) / max(1, len(inside)):.0f}%)")
    if sizes:
        print(f"step px: min {sizes[0]} median {sizes[len(sizes)//2]} p90 {sizes[int(len(sizes)*0.9)]} max {sizes[-1]} | total {sum(abs(s[1]) for s in inside)}px")
    print("first 40 steps (s, px):", [(round(t, 3), s) for t, s in steps[:40]])
