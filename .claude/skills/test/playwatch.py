"""Watch the player from the user's side of the glass.

usage: python playwatch.py <seconds> <out.csv> [name]
Every 1.0s: grab the middle of the Atomic window (a 600x340 box), compare
with the previous grab (mean absolute difference over a 1/4-scale copy),
and log it. Video that is playing changes every second; a frozen picture
does not. Prints a summary: samples, moving samples, stalls (>=3
consecutive still samples), the longest stall, and when it began.
Runs as its own process so nothing inside the app is measured.
"""
import sys, time, csv
sys.path.insert(0, __file__.rsplit("\\", 1)[0].rsplit("/", 1)[0])
from rig import find
from PIL import ImageGrab, ImageChops, ImageStat

seconds = float(sys.argv[1]); out = sys.argv[2]; name = sys.argv[3] if len(sys.argv) > 3 else ""
f = find()
if not f:
    print("no window"); sys.exit(1)
h, rect, _ = f
cx, cy = (rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2
box = (cx - 300, cy - 170, cx + 300, cy + 170)
prev = None; rows = []; t0 = time.time()
still_run = 0; stalls = []; stall_start = None
while time.time() - t0 < seconds:
    t = time.time() - t0
    im = ImageGrab.grab(bbox=box, all_screens=True).convert("L").resize((150, 85))
    diff = ImageStat.Stat(ImageChops.difference(im, prev)).mean[0] if prev is not None else None
    prev = im
    moving = diff is None or diff > 1.0
    rows.append((round(t, 1), None if diff is None else round(diff, 2), int(moving)))
    if diff is not None and not moving:
        still_run += 1
        if still_run == 1: stall_start = t
        if still_run == 3: stalls.append([stall_start, 3])
        elif still_run > 3: stalls[-1][1] = still_run
    else:
        still_run = 0
    time.sleep(max(0.0, 1.0 - ((time.time() - t0) - t)))
with open(out, "w", newline="") as fh:
    w = csv.writer(fh); w.writerow(["t", "diff", "moving"]); w.writerows(rows)
moving_n = sum(r[2] for r in rows[1:])
longest = max((s[1] for s in stalls), default=0)
print(f"{name}: {len(rows)} samples over {seconds:.0f}s, moving {moving_n}/{len(rows)-1}, "
      f"stalls>=3s: {len(stalls)} (longest {longest}s" + (f", first at {stalls[0][0]:.0f}s" if stalls else "") + ")")
