# Testing rules

Read this before non-trivial verification work (`test-engineer`, or
anyone verifying their own change). Procedures/code snippets: see the
`test` skill.

## The rule that matters most

**Never touch real user data at `%APPDATA%\Atomic`.** Always copy it to
a temp directory and redirect `storage.DATA_DIR` before importing any
page - order is not negotiable, `DATA_DIR` is read at import time.
Delete temp directories when done.

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
