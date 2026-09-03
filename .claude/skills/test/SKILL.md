---
name: test
description: Test harness patterns for Atomic - redirecting storage away from real user data, offscreen vs real-window testing, measuring instead of eyeballing, and reading code back out of a frozen build. Use when verifying a change actually works.
---

# Test

Principles and traps: see `.claude/rules/testing.md`. This is the
mechanics.

## Redirect storage before anything imports

```python
import shutil, sys, tempfile, os
from pathlib import Path
sys.path.insert(0, r"<repo>/src")
from helpers import storage
work = Path(tempfile.mkdtemp(prefix="atomic-test-"))
shutil.copytree(Path(os.environ["APPDATA"]) / "Atomic", work, dirs_exist_ok=True)
storage.DATA_DIR = work          # must happen before `import main` or any page
try:
    ...                          # the harness
finally:
    shutil.rmtree(work, ignore_errors=True)
```

**The `finally` is not optional.** On 23 August 2026 about a hundred of
these copies (50-100MB each) were left in `%TEMP%`, the drive hit 100%,
and the next source patch - `open(path, "w")` truncates before it
writes - left `player.py` at 0 bytes with ~900 uncommitted lines in it.
Recovered by replaying the session transcripts against the surviving
`__pycache__` bytecode, which is not a procedure anyone wants twice.
Two rules that fall out of it: every harness deletes its copy, and a
source patch writes to `path + ".tmp"` and `os.replace`s it.

## Offscreen for logic/flows

`QT_QPA_PLATFORM=offscreen`, fabricated entries, stub network calls
(`release_schedule.fetch`, `stremio.fetch_watch_progress`,
`stremio.fetch_latest_episode`, `anilist.*`, `launchers.scan_all`) with
lambdas. Use a real window instead for anything visual, DPI-related, or
timed - those don't reproduce offscreen.

## Poll, don't sleep

Wait on the page's own state (`page._refresh_toast is None`,
`page._scan_toast is None`) rather than a fixed delay. Wrap
`widgets.Toast.set_text` to record what was shown.

## Reading a frozen build back out

```python
from PyInstaller.archive.readers import CArchiveReader, ZlibArchiveReader
arch = CArchiveReader("Atomic.exe")            # extract "PYZ.pyz" to a temp file
z = ZlibArchiveReader(str(tmp_pyz))
code = z.extract("helpers.widgets")            # then inspect co_names / co_consts
```

For icons/version resources, read PE resources with
`FindResourceW`/`LoadResource` and compare bytes against
`src/app_icon.ico`.

## Pixel measurement

`GetPixel` on the screen DC samples every ~10ms fine; grabbing a full
bitmap doesn't. Take a baseline of the settled state first. If colours
are ambiguous (this app's surfaces are near-black), monkeypatch the
paint event to fill one flat unmistakable colour for the run rather
than guessing from raw pixel values.

## Driving the real window from outside (rig.py, playwatch.py)

`rig.py` beside this file launches the exe (or `src/main.py` under
3.13) against a data copy, finds the window by its exact title, clicks,
hovers, types, sends keys and photographs the window or a crop of it,
all from a separate process. `playwatch.py` watches the player from the
user's side: a screen-diff of the picture every second, reporting
stalls of three seconds or more - a held anime frame reads as a stall,
so confirm with `player_state.json`'s position advancing in real time.

    python rig.py launch <Atomic.exe or src/main.py> <dir holding an Atomic copy>
    python rig.py click 448 664 | move x y | key ctrl+2 | type kingdom
    python rig.py shot name.png [x0 y0 x1 y1]   # window-relative physical px
    python playwatch.py 90 out.csv "Reacher S1E2"

Two traps it already carries, both measured 3 September 2026:

- **Match the title exactly.** "Atomic - File Explorer" (the repo
  folder open in Explorer) matched a startswith test and took the
  clicks meant for the app; a second source run left two windows up.
- **The Claude desktop app confines the cursor to its own monitor.**
  With Atomic closed, any click on the primary monitor still produced
  `GetClipCursor = (-1920, 0, 0, 1080)` and warped the pointer to
  x=-1, so every synthetic click missed the app while keyboard
  shortcuts worked. The rig releases the clip (`ClipCursor(None)`)
  before every pointer move and again between button-down and
  button-up. It is not the app - grep finds no ClipCursor in src/.

## The proof-loop checklist (CLAUDE.md rule 12, testing.md has the why)

    [ ] reproduce on the build he tested, APPDATA -> a copy, screenshot
        of the state he described; read atomic.log around his times
    [ ] number the cause with a harness before editing
    [ ] preview from source against a copy (rig.py launch src/main.py)
    [ ] build; read PYZ.pyz back: added names present, deleted names gone
    [ ] frozen exe, separate process: screenshots of each surface named
        in the ask; playwatch 90-100s per title + player_state.json
        position advancing 1s per second; the log lines named up front
        ((pre-started), no stopped/was gone/could not open/falling back)
    [ ] motion: band burst through the animation, card edges per frame
    [ ] sizes: device px on the screenshot vs the source's own rule
    [ ] every regression found -> fix -> build -> read back -> photograph
    [ ] new log lines read after the run; environment ruled out first
    [ ] say what was NOT exercised, and why
