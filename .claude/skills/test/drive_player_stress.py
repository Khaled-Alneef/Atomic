"""Aggressive player pass: twelve titles off the Anime, Series and Movies
grids, an episode each, a source picked from a different quality group
each time, 30s of playback watched through player_state.json, then Next
and Previous. Usage: py drive_player_stress.py <exe> <root> <out>"""
import sys, time, random, pathlib
from stress_common import *

exe, root, out = sys.argv[1:4]
root = os.path.abspath(root); out = pathlib.Path(out); out.mkdir(parents=True, exist_ok=True)
random.seed(20260906)
launch(exe, root, out)
GROUP_Y = [421, 504, 587, 670]          # the quality headings, sources closed
ROW_STEP = 88                            # a source row under an open heading
EP_Y = [485, 597, 709, 821]              # the episode rows on a details page
titles = [("5", i, g) for i, g in zip(range(5), [1, 0, 2, 1, 3])] + \
         [("4", i, g) for i, g in zip(range(5), [0, 1, 2, 1, 0])] + \
         [("3", i, g) for i, g in zip(range(2), [1, 2])]
results = []


def playing_for(seconds, tag):
    """Does the saved position move one second per wall second?"""
    a = newest_resume(root); time.sleep(seconds); b = newest_resume(root)
    if not a or not b:
        return f"{tag}: no resume record", None
    moved = b[3] - a[3]
    ok = a[0] == b[0] and a[2] == b[2] and seconds * 0.7 <= moved <= seconds * 1.4
    verdict = "PLAYING" if ok else "NOT ADVANCING"
    return f"{tag}: s{b[1]}e{b[2]} pos {a[3]:.0f}->{b[3]:.0f} (+{moved:.1f} in {seconds}s) {verdict}", b


def leave():
    rig.key("escape"); time.sleep(2.5); rig.key("escape"); time.sleep(2)


for n, (page, card, g) in enumerate(titles, 1):
    k = random.randrange(0, 3)
    j = random.randrange(0, 3)
    mark = log_len()
    rig.key("ctrl+" + page); time.sleep(3.5)
    open_card(card); time.sleep(3.5)
    rig.shot(str(out / f"t{n:02d}_details.png"))
    rig.click(2153, EP_Y[0] if page == "3" else EP_Y[k])
    time.sleep(4)
    rig.shot(str(out / f"t{n:02d}_sources.png"))
    streams = log_since(mark, r"streams ")
    if not streams:
        say(f"title {n}: no sources line - skipping")
        results.append((n, page, card, "NO SOURCES", "", ""))
        leave()
        continue
    head = streams[-1][24:120]
    # open a quality group; if that heading is not there, take another
    opened = None
    for gg in (g, 1, 0):
        m2 = log_len(); rig.click(1905, GROUP_Y[gg]); time.sleep(1.6)
        if log_since(m2, r"source group"):
            opened = gg
            break
        if log_since(m2, r"mpv running|prepare|race:|pick_file"):
            opened = -1          # the click landed on a row and the player opened
            break
    if opened is None:
        say(f"title {n}: no group opened")
        results.append((n, page, card, head, "NO GROUP", ""))
        leave()
        continue
    if opened >= 0:
        rig.click(2140, GROUP_Y[opened] + ROW_STEP * (j + 1))
    time.sleep(30)
    rig.move(1100, 1150); time.sleep(0.12); rig.move(1300, 1250); time.sleep(0.3)
    rig.shot(str(out / f"t{n:02d}_play.png"))
    v1, _ = playing_for(12, "play")
    rig.key("n"); time.sleep(30)
    rig.move(1100, 1150); time.sleep(0.12); rig.move(1300, 1250); time.sleep(0.3)
    rig.shot(str(out / f"t{n:02d}_next.png"))
    v2, _ = playing_for(12, "next")
    rig.key("p"); time.sleep(28)
    v3, _ = playing_for(12, "prev")
    rig.shot(str(out / f"t{n:02d}_prev.png"))
    bad = log_since(mark, r"could not open|falling back|stopped mid|was gone|Traceback| ERROR ")
    picks = [l[24:110] for l in log_since(mark, r"pick_file|source group .*opened|race: |mpv running")]
    say(f"title {n} [{page}/{card}] {head}\n      group={opened} row={j} | {v1}\n      {v2}\n      {v3}\n"
        f"      bad={len(bad)} {[b[24:120] for b in bad[:3]]}\n      picks={picks[:8]}")
    results.append((n, page, card, head, f"g{opened} r{j}", f"{v1} | {v2} | {v3} | bad={len(bad)}"))
    leave()

say("DONE")
for r in results:
    print("   ", r)
errors = log_since(0, r" ERROR |Traceback|could not open|falling back|stopped mid|was gone")
say("ERRORS total:", len(errors))
for l in errors[:40]:
    print("   ", l[11:200])
rig.close()
