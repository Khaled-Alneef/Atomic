# Testing rules

Read this before non-trivial verification work (`test-engineer`, or
anyone verifying their own change). Procedures/code snippets: see the
`test` skill.

## The rule that matters most

**Never touch real user data at `%APPDATA%\Atomic`.** Always copy it to
a temp directory and redirect `storage.DATA_DIR` before importing any
page - order is not negotiable, `DATA_DIR` is read at import time.
Delete temp directories when done.

**Redirecting `storage.DATA_DIR` is not enough on its own, and the
failure is silent.** `web/backend.py` sets `storage.DATA_DIR` to
`%APPDATA%\Atomic` *at import*, deliberately - the source tree's own
default points at `src/data`, which is empty. So a harness that sets
`storage.DATA_DIR` to a temp copy and then imports `web.server` or
`web.backend` has its redirect quietly undone, and every write lands on
the real files. Measured 1 September 2026, testing drag-reorder: the
copy was made, the redirect was set, and `storage.move_entry` moved a
game in the owner's real `games.json` (Guild Wars 2 from fourth to
first). Nothing failed; the test simply reported no change, because it
was reading the copy and writing the original.

Set **all three**, after every import, and assert before writing:

    from web import backend, server          # imports first
    storage.DATA_DIR = copy
    backend.DATA_DIR = copy
    server.DATA = copy
    assert storage.DATA_DIR == copy          # cheap, and it would have caught this

The general form: anything that re-points a module global can be
re-pointed back by the next import. Check the value at the moment of the
write, not at the top of the script.

## How to test this app

- **Logic and flows:** offscreen, fabricated entries, stub the network.
  Real lookups make a test slow, flaky, and dependent on what's
  actually airing.
- **Visual, DPI-related or timed:** a real window, not offscreen -
  position bugs and animation artifacts don't reproduce offscreen.
- **Background work finishing:** poll the page's own state rather than
  sleeping a fixed time.
- **End-to-end over isolation for anything with timing/UI in the loop.**
  A function can be provably correct and the feature still shipped
  broken - the Crunchyroll resolver was right in isolation, but the
  real save dialog raced a background lookup and stored an empty
  result. Drive the real widget/dialog when the bug (or the risk of
  one) involves user timing, not just the function it calls.

## Measuring, not eyeballing

For animation/window-geometry questions, read the screen directly -
sampling pixels is fast enough, grabbing a bitmap isn't. Take a
baseline of the settled state first, compare against it.

**"Did it change?" is not the same question as "was it smooth?"** The
scrollbar drag was fixed once on the metric *dead refreshes* - refreshes
whose position was identical to the one before - which went from 47.9%
to 1.5% and the fix was called done. It was not: the follow was
delivering 20px in one frame and then a 0.3px tail across the next
seven, and a 0.3px tail counts as "moved" while the eye sees a lurch and
a stall. The owner's screen recording, measured frame by frame nine
months of tuning later, still showed 35% of frames pixel-identical
during a drag. For motion, measure the **distribution of the step
sizes** (and count a frame dead below half a pixel, which is what
int(round()) actually shows) - never just whether a number changed.

**Capturing the screen from inside the app halves the app.** Measured
28 August 2026. A sampler thread doing `BitBlt` out of the screen DC
takes the process's GDI lock and serialises against Qt's own blit: the
painted grid produced **172.3 positions a second with no sampler
running and 73-76 with one**, same page, same input. Every "dead frame"
number the in-process version produced was measuring the instrument.
Run the sampler as a **separate process**; the app's own paint counter
then agrees with the capture.

Three smaller traps from the same rig, all of which silently produced
plausible wrong numbers:
- **`BitBlt` from the screen DC *is* the vblank wait** - 6.06ms per
  call for any size from 1x1 to 3x500 on a 165Hz panel. A `DwmFlush()`
  before it therefore skips every other present (12.12ms captured), and
  half the app's frames look missing.
- **ctypes signatures are process-global.** `ctypes.windll.gdi32` is one
  cached object, and Atomic's own icon extraction declares `GetDIBits`
  with its own `BITMAPINFOHEADER` pointer - so importing `main` silently
  broke the sampler's call and it captured zero frames. Use a private
  `ctypes.WinDLL("gdi32")`, and set `restype`/`argtypes` for every
  handle-returning call (they default to `c_int`, which truncates a
  64-bit HDC).
- **A correlator's search range must cover the biggest real step.** The
  reader's thumb drag moves 38px of strip per pointer pixel; with a
  72px range the correlation saturated and reported **41 backwards
  frames on a monotonic downward drag**. Widening it to 200 showed
  zero.

**The panel this repository is on is not the one the old notes were
taken on.** Measured 28 August 2026: one display, 1920x1080 **165Hz**,
device pixel ratio 1.0. Most of the scroll measurements written into
`helpers/widgets.py` and `helpers/poster_grid.py` were taken on a
2560x1440 240Hz panel at DPR 1.25. Check `QScreen.refreshRate()` before
comparing a new number against an old one - 120Hz is exactly half of
240 and nothing like a divisor of 165, and that difference is what
`widgets.present_frame_s` exists for.

**A classifier you haven't validated is not a measurement.** This app's
surfaces are near-black, close enough to a dark desktop that "is the
window covering this point" can't be answered from a raw pixel - the
first attempt reported every frame bad in *both* the broken and fixed
case, which was the test failing, not the app. When colours are
ambiguous, instrument: monkeypatch the paint event to fill one flat
unmistakable colour for the run.

## Verifying a frozen build

PyInstaller caches aggressively and a no-op rebuild silently re-copies
the previous binary - a "succeeded" build proves nothing when it
matters that the build is real (any release, any claim about exe
contents). Read the code back out of the frozen archive instead of
trusting the build log - see the `test` skill for the extraction
snippet. Check the function actually calls what you expect - a string
can match because it's in a docstring.

## How deep to dig

Small, obvious fix → quick targeted check. Reserve pixel-probe suites,
exe archaeology and multi-pass runs for cases actually in doubt: a
critical bug, an effect that can't be eyeballed, or a result that
contradicts expectation.

## The proof loop (CLAUDE.md rule 12) - what each step actually means

**His rule, 3 September 2026: "the testing methods you used for
testing make them rules!!! it is perfect!"** These are those methods,
in the order they ran that day, with what each one caught.

### 1. Reproduce on the build he tested, on a copy of his data

Launch the exe he reported against (not the source tree) with `APPDATA`
pointed at a directory holding a copy of `%APPDATA%\Atomic`, navigate
to the page he named, and photograph it - `rig.py` in the `test` skill
does all three. Compare what is on screen with the words of the report
before forming a theory. That day: "apps and games images are not there"
drew every cover inside 1s cold and after a heavy page; what actually
reproduced was two blank tiles (a trimmed exe icon, a trimmed favicon)
and a portability bomb (every game cover path pointing into the source
tree). The fix that followed was for those, not for a lazy loader that
had already been "fixed" twice on the strength of the report alone.

### 2. Split the report until the log names the cause

Read `atomic.log` around the times he was using the app before reading
the code. "The player freezes at ~28s", "does not start when I
continue" and "refresh freezes" were one line: `the video process
failed ([WinError 10053] ...); falling back to an in-process core` - a
child dying 20s after the parent's last message. A 26-second harness
(spawn the child, send nothing, poll it) turned the theory into a
number before any code changed.

### 3. Source run as a preview, never as the proof

`py -3.13 src/main.py` against a copy, driven by the same rig, is where
a wrong click path is cheapest to find and fix (the manga "Kingdom"
card opening the *anime's* details page was found there and fixed
before the first build). It proves nothing about the exe: the frozen
child's DLL path, PyInstaller's cache, the bundle's data files are all
different there. A screenshot from a source run is a step, not the
evidence (rule 10).

### 4. Read the archive back before photographing it

After every build, extract `PYZ.pyz` (`CArchiveReader` →
`ZlibArchiveReader`) and check the changed modules' `co_names` and
string constants for **both** the names the change added and the names
it deleted. A no-op rebuild silently re-copies the previous binary, and
a screenshot of a stale exe proves the old code. The version string
sits inside a function's constants, so walk nested code objects.

### 5. Drive the frozen exe from outside

- **Input and screenshots come from a separate process** - the rig.
  Match the window title exactly and take the largest window with that
  title: "Atomic - File Explorer" took a batch of clicks once, and the
  search suggestions popup is a second window called "Atomic", 72px
  lower, that a screenshot cropped against.
- **Keyboard is the fallback.** Ctrl+1..9 reach every sidebar page
  without a pointer, which is how a dead mouse was told apart from a
  dead app.
- **A click is not a click until the button-up lands on the same
  element.** Between down and up, put the cursor back where it was.
- **Playback is watched from the screen, then confirmed from the
  app.** `playwatch.py` diffs the picture once a second and reports
  runs of still samples; a stall of three seconds or more is a stall
  *unless the content holds a frame* - anime does, for seconds at a
  time, so the ground truth is `player_state.json`: the saved position
  must advance one second per wall second (measured 15.0 in 15.0 and
  14.9 in 15.0). Run each title for 90-100 seconds - the child used to
  die at 20 and the owner's freeze was at 28.
- **The log lines that prove the claim are named in advance.** For the
  player: one `mpv running in process N` per title, `(pre-started)` on
  the second and later ones, and none of `stopped mid-episode`, `was
  gone`, `could not open`, `falling back`. Stop, continue, and R are
  three separate runs, each with its own line.
- **Motion is measured as positions per frame.** Grab a thin band
  across the moving row as fast as PIL allows (~40ms) through the whole
  animation, find the card edges in each frame, and print the sequence.
  Continuous motion reads as a different position in every frame and
  no single jump larger than a frame's travel; the shipped fold read as
  eleven identical frames and one 198px jump.
- **Sizes are compared against the source's own rule.** A reader page
  is measured in device pixels on the screenshot and compared with the
  file's natural width times the DPR (828 CSS px → 1035 device px,
  measured 1035), not eyeballed as "looks right".

### 6. Every regression found goes back through the loop

The verification is not a gate at the end; it is where the next bug
comes from. That day step 5 found a broken image proxy (a local
`import` that shadowed a module name), a chapter row opening the list
instead of the reader (two caches disagreeing), and three covers over
a size cap - three builds, each read back and photographed again.

### Let the log find the next bug

A silent `except ... pass` on a path the user triggers becomes a log
line (host and error kind, never a URL that can carry a token), and
**the line is then read**: the covers and the proxy above were both
found by lines that had not existed an hour earlier. A fix that logs
its failure is how the next report arrives with its cause attached.

### Rule out the environment before the app

When input dies but timers run, ask Windows before reading code:
`GetClipCursor`, `WindowFromPoint`, `GetGUIThreadInfo`, the process's
own top-level windows. The dead pointer that day was another
application confining the cursor to its monitor on every click, and
`grep ClipCursor src/` proved it was not Atomic in one line. A screen
recorder's watermark in a screenshot is likewise not the app's.

### Verify the verification

A review finding is fixed only after two independent readers have
tried to refute it against the code *as it stands now* (line numbers
move) and failed; a finding neither could confirm is left alone and
said so. A fix the Manager applied gets a checker of its own that reads
the code after the fact and hunts for the site the fix missed: one of
nine found a wrapper that replaced the patched function outright, so
the fix had never run. Dead-code removal is verified mechanically:
count whole-word references across every `.py`, `.js`, `.html`,
`.css` and `.spec` (patches look functions up by string), delete only
at zero, re-scan until nothing new is orphaned, import every module,
and read the archive back for the absence.

### Copies go out, never back

Every harness copies `%APPDATA%\Atomic` to a temp dir and deletes it.
Nothing ever copies a temp dir back: the day after a long pass the live
`discover_cache.json` was a day-old 1053-row snapshot while its `.bak`
held the newer 2667 rows, which only a copy with a preserved
timestamp can produce. If a harness must touch his files it says so
before it runs, and the fix is left to him.

## A screen metric can be measuring the network (3 September 2026)

Chasing "in manhwa and movies pages the cards transition is a bit
delayed", three screen metrics were built and all three answered the
wrong question. The record, so the same ground is not re-dug:

- **"When does the picture stop changing?"** - a band of screen scored
  against the last frame of the run. It put Manhwa at **3.56s** against
  0.26-0.30s for its five neighbours, which looked like the bug. A
  **control run of the same build gave 2.59s and then 3.58s**: the
  number was whichever second the site sweep happened to answer in, and
  it could not tell a code change from the weather.
- **The lazy sweep's rectangle reads** - 0.6ms median for 900 pending
  images with layout dirty. Not the cause.
- **The fold's own preparation** - 4.0ms at 60 cards, 5.2ms at 930. Not
  the cause. A control with the suspected double document load switched
  off drew **one page per arrival either way**, because navigating a
  view to the URL it is already on is a same-document navigation.

What answered it was **making the page report itself**: app.js
`sayRender`/`sayBatch` put "route drawn, route=manhwa, ms=12, rows=66"
and "batch appended, ms=2956, rows=6" into atomic.log. Every one of the
six pages renders in **8-22ms**; the difference was the first *sight* of
a cover (a reading row's art comes from the scanlation site, 0.2-3.0s a
host, against Cinemeta's CDN for a video row) - 24 covers on the first
screenful took **94ms on Movies and over 14s on Manhwa**, cold.

Two rules out of it:

1. **Where a screen number moves on its own, instrument instead.** A
   line the app writes on his machine beats a rig that only runs here,
   and it keeps answering after the pass is over.
2. **Run the control before believing the delta.** Two runs of
   *unchanged* code, every time - testing.md has said this since 21
   August and it is the step that saved this one.

### Validate the "done" test, not just the "changed" test

The cover-fill metric waited for `loaded === onScreen` and reported
`>14,000ms` for a page that had finished in about a second. Two of the
twenty-four images had **failed**: the window's own capture-phase error
handler hides an `<img>` and strips its `src` (app.js, top of file), so
they were `complete === true`, `naturalWidth === 0`, `src === ""` and
could never satisfy the test. A settle test must count *resolved*
(decoded **or** given up on), not decoded - and the two blank tiles it
accidentally found were a real bug worth fixing.

## A save can be lost to a reader, and the rename says so (6 September 2026)

His log carried `Could not save the resume position ... PermissionError:
[WinError 32] The process cannot access the file because it is being
used by another process: player_state.json.tmp -> player_state.json`,
twice. `storage.save` writes a temp file and `os.replace`s it over the
target; Windows refuses that rename while any handle is open on the
target, and a server thread reading the same file for a page draw holds
one for a few hundred microseconds. Measured (`h_storage_race.py`):
three threads reading in a loop while another saved 300 times lost
**294 saves**, and 25 reads landed in the moment the target did not
exist (MoveFileEx with replace is not atomic for an existing target).
`storage.load` takes the writer's RLock now, so a reader through it
never overlaps a rename (0 errors in the same race); `save` retries the
rename for up to 400ms for the direct readers that remain (0 of 300
lost at a realistic thousand reads a second); the server's four direct
reads of discover_cache.json go through `load`. A direct `read_text` of
a file the app also writes is a race - route it through `storage.load`.

`ATOMIC_MEMTRACE=1` turns on `logs.start_memtrace`: the process's
working set and Python's eight biggest allocation sites every ten
seconds, plus `logs.memtrace_mark` lines from the page slide and
`_show_page`. Measured with it: Python's own objects grew 11MB -> 16MB
over sixteen switches, so a process growth is native; and the frozen
build's 255MB -> 681MB was two processes summed by name until the
bootloader (8MB) was listed apart from the app (248MB -> 616MB).

The conclusion of that measurement, 6 September 2026: over sixteen
switches on the frozen build the process's **private** bytes held at
170-223MB while the working set stepped once from 263MB to 624MB at the
first page switch and then only oscillated by the 22MB a slide holds
until it lands. A one-time step in shared, file-backed pages (the .NET
host and WebView2 mappings the first web page touches), not a leak.
Read private bytes, not the working set, before calling growth a leak.
