"""Aggressive reader pass: ten titles off the Manga, Manhwa and Manhua
grids, a chapter each, scrolled, then the next and the previous chapter
by keyboard. Usage: py drive_reader_stress.py <exe> <root> <out>"""
import sys, time, random, pathlib
from stress_common import *

exe, root, out = sys.argv[1:4]
root = os.path.abspath(root); out = pathlib.Path(out); out.mkdir(parents=True, exist_ok=True)
random.seed(20260906)
launch(exe, root, out)
CH_Y = [376, 480, 584]
titles = [("6", i) for i in range(4)] + [("7", i) for i in range(3)] + [("8", i) for i in range(3)]
results = []
for n, (page, card) in enumerate(titles, 1):
    k = random.randrange(0, 3)
    mark = log_len()
    rig.key("ctrl+" + page); time.sleep(3.5)
    open_card(card); time.sleep(3.5)
    rig.shot(str(out / f"r{n:02d}_details.png"))
    rig.click(1859, CH_Y[k]); time.sleep(10)
    sized = log_since(mark, r"reader sized")
    if not sized:
        say(f"title {n}: reader did not open")
        rig.key("escape"); time.sleep(1.5); rig.key("escape"); time.sleep(1.5)
        results.append((n, page, card, "NO READER"))
        continue
    rig.shot(str(out / f"r{n:02d}_ch1.png"))
    front(); rig.wheelto(-120, 30, 6.0, 1200, 700); time.sleep(2.5)
    rig.shot(str(out / f"r{n:02d}_ch1_scrolled.png"))
    front(); rig.click(1200, 700); time.sleep(0.5)
    m2 = log_len(); rig.key("right"); time.sleep(10)
    nxt = log_since(m2, r"reader sized")
    rig.shot(str(out / f"r{n:02d}_ch2.png"))
    front(); rig.wheelto(-120, 30, 6.0, 1200, 700); time.sleep(2.5)
    front(); rig.click(1200, 700); time.sleep(0.5)
    m3 = log_len(); rig.key("left"); time.sleep(9)
    prv = log_since(m3, r"reader sized")
    rig.shot(str(out / f"r{n:02d}_ch3.png"))
    bad = log_since(mark, r"image fetch failed|Traceback| ERROR |could not|over the size cap")
    marks = [l[24:120] for l in log_since(mark, r"mark|history")]
    say(f"title {n} [{page}/{card}] first={sized[0][24:140]}\n"
        f"      next={nxt[0][24:140] if nxt else 'NONE'}\n"
        f"      prev={prv[0][24:140] if prv else 'NONE'}\n"
        f"      bad={len(bad)} {[b[24:120] for b in bad[:3]]}\n      marks={marks[:4]}")
    results.append((n, page, card, f"open={bool(sized)} next={bool(nxt)} prev={bool(prv)} bad={len(bad)}"))
    rig.key("escape"); time.sleep(1.5); rig.key("escape"); time.sleep(1.5)

say("DONE")
for r in results:
    print("   ", r)
errors = log_since(0, r" ERROR |Traceback|could not|over the size cap")
say("ERRORS total:", len(errors))
for l in errors[:40]:
    print("   ", l[11:200])
rig.close()
