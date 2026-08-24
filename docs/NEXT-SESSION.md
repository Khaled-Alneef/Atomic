# Order for the next session

Written 24 August 2026, at the end of the session that tested a C# rewrite.
Hand this to a fresh session; it is self-contained.

---

## 0. Two open items for the owner, not for the agent

- **A GitHub personal access token was pasted in plaintext in chat and must
  be rotated.** GitHub > Settings > Developer settings > Personal access
  tokens > revoke. It was never written to any file, never placed in a
  remote URL, and never used.
- **The new repo is still undecided.** `AtomicApplication/Atomic` was to get
  a `Development` branch starting at 0.0.0, carrying the rules and the
  release guide. The owner answered "wait" on both auth and contents, and
  nothing was pushed. Nothing about it has been done. Ask before acting.

---

## 1. The standing decision: no rewrite. This is settled, with numbers.

A full C# port was requested, started, and cancelled by the owner after the
first measurements came back. **Do not re-propose a rewrite in any language.**

Measured 24 August 2026, .NET 10.0.400 + Avalonia 12.1.1, on the owner's
maximized 1920x1080 window at 144Hz, using the *same* validated vblank
harness and the *same* motion constants as the Qt build, so the numbers are
directly comparable:

| | effective moving fps | stalled refreshes | uneven steps |
|---|---|---|---|
| C# / Avalonia, 4 runs (Debug and Release) | 87.6 - 95.4 | 33-38% | 34-42% |
| Qt raster page | 80 | 43% | 41% |
| **Qt GPU surface, already shipping** | **111** | **~0%** | **5.4-6.5%** |

Avalonia landed *below* what this app already achieves. Two caveats, stated
because silence about them would be a claim: its compositor and rendering
backend options were never tuned, and the Release build measured the same as
Debug (94.6 vs 93.3), so build config was ruled out but framework config was
not.

**The caveats do not matter, and this is the reason why:** `poster_grid.py`
measures a **6.93ms gap median** against a panel that refreshes every
**6.94ms**. It is already at the physical ceiling. No language can beat the
display. A rewrite could only match it.

One more result worth keeping, because it is a general trap rather than a C#
one: Avalonia's `Render()` cost was **median 1.39ms** (well inside a 6.94ms
budget) but **p95 12.07ms, max 29.39ms** - cells building their text as they
scrolled into view. That is the same shape as the Qt build's cover decode
that blocked 68.5ms on the UI thread. Every framework has this failure mode;
none of them prevent it.

`csharp/` may be deleted - every number from it is recorded here. The .NET
SDK is installed and harmless; leave it.

---

## 2. The rule that governs all remaining work

**A surface reaches ~144fps only if it owns its scrolling** - one painted
widget filling its tab, integrating the position *inside its own frame* from
that frame's own timestamp.

A `QOpenGLWidget` placed **inside** a scrolling raster page is **worse than
the raster widget it replaces**: measured 83 frames/s against 93, gap median
9.20ms against 6.78ms. So a nested surface cannot be fixed by swapping in a
GPU widget; it has to be restructured so the surface owns the page.

**Corrected 24 August 2026 - this section was wrong about the shipping
code and sent the next session down a path that does not exist.**
`helpers/poster_grid.py` is **not** a `QOpenGLWidget`. It is a raster
`QWidget` with a `paintEvent`. A GPU version was built and measured at
**142.9 frames/s / 2.0% judder** against the raster widget's 114-118 and
14% - and then **taken out again**, because the player draws through
libmpv into a native child window and Qt does not support a
QOpenGLWidget and native child widgets in one top-level window: the
owner got *"only plays sound, there is just black screen"*. The module's
own docstring carries this and ends with **"Do not reintroduce OpenGL
anywhere in this process without first proving the player still draws."**

What survives, and what "owns its scrolling" actually means here: the
position is integrated **inside the frame that draws it**, from that
frame's own timestamp (`FrameMotion.step`), and the widget fills its tab
so there is no QScrollArea backing store to fight. That half never
needed the GPU.

---

## 3. Where every surface stands

**The first row was the GPU spike that was reverted, not what ships.**
See the correction in section 2, and the 24 August measurements at the
end of this file for what the shipping build actually does.

| surface | frames/s | judder | state |
|---|---|---|---|
| category grids (GPU spike, REVERTED) | 143 | 5-6% | not shipping |
| category grids (raster, shipping) | 114-118 | 14% | see below |
| horizontal rows, wheel | 126 | 4.8-6.1% | done |
| horizontal rows, arrows | 128 (was 72) | 7% (was 11.5%) | done |
| Home vertical | 123 | ~18% | **see the warning below** |
| **reader strip** | **116** | **16.2%** | **Phase 2** |
| **video player** | **unmeasured** | **unmeasured** | **Phase 1** |

**Home's ~18% is probably an artifact, not a defect.** Its scroll range is
only 719px, so the number is dominated by clamps and reversals, and its paint
cost is **1.03ms/frame** across every receiver (the most expensive single
painter is HeroBanner at 0.65ms per paint, 0.4 paints/frame). **Home is not
paint-bound - do not go looking for expensive painting there.** Measure it on
a long page before touching it or you will optimise the artifact.

---

## Phase 1 - Measure the video player

**Do this first.** It is the thing the owner actually named ("even the vid
player has low fps!!"), it has **never been measured**, and it is a
completely separate mechanism from scrolling - none of the scroll work
touched it.

Form a theory only after the numbers. Rule 8, step 1.

- **What it is:** mpv rendering into a native child window.
  `windows/player.py:1168` `VideoSurface`, `player.py:1188` `StartupBackdrop`,
  `player.py:931` `_make_native`; the mpv options live in
  `helpers/video_backend.py:106` and already carry measured notes (the
  `video-sync` choice records 0.000, 0 dropped / 0 delayed / 0 mistimed over
  9s - so *some* configuration measured clean at some point, which is worth
  reproducing before assuming it is broken now).
- **Split the number** into at least: frames mpv decoded, frames mpv
  presented, and frames the *panel* actually showed. mpv can report the first
  two itself - `frame-drop-count`, `vo-delayed-frame-count`,
  `estimated-vf-fps`, `display-fps`, `estimated-display-fps` - and the vblank
  harness answers the third. "The player is slow" is not yet a measurement.
- **Candidates, in no particular order and none of them verified:** `hwdec`,
  the `vo` backend, swap interval, and the native child window being resized
  or re-created per frame. Test one at a time, re-measure each time.
- **Trap already paid for:** on Windows a native child window paints above
  every non-native sibling whatever `raise_()` was told. The loading logo had
  therefore never once been visible - drawn every frame, underneath the video
  surface. Anything that must appear over the video is `_make_native`'d
  itself or is a child of something that is.
- **libtorrent is why the build re-execs on Python 3.13.** There is no wheel
  for 3.15, so anything run under 3.15 reports
  `torrent_engine.available() == False` and every torrent looks broken. Test
  the player with the 3.13 interpreter.

## Phase 2 - Reader strip to a GPU surface

Known cause, known fix, and the number is already proven.

- Current: **116 frames/s, 16.2% judder**.
- A spike with reader-shaped 800x1400 pages measured **144fps / 6.4%** on a
  GPU surface, with **no texture-cache problem**.
- The strip is `_StripView`, `windows/reader.py:1526`.
- Model it on `helpers/poster_grid.py` (see section 2).
- **The reader's physics are deliberately different and must stay different:**
  it passes a high friction so the strip **stops when the wheel stops** - the
  owner's ask, 24 August 2026, *"while in reader mode remove the scrolling
  movement after the mouse scroll stops ... ONLY IN READER MODE"*. Reading is
  aiming at a panel, not travelling, and a coast overshoots what you were
  looking at. Do not unify it with the shared momentum model.

## Phase 3 - Home and the Discover strips, only if measurement justifies it

Read the Home warning in section 3 first. If a long-page measurement does
justify work here, note that these are **nested inside scroll areas**, so
section 2's rule applies: this is a restructuring job, not a widget swap.

## Phase 4 - Verify

Its own phase, never folded into the phase that made the change.

- **Run a control** - two runs of *unchanged* code, to find out what moves on
  its own. This is the only reason a 37% screenshot diff was once correctly
  read as live network content rather than a bug.
- **Prove it on the same measurement**, then prove it changed nothing else.
- **Against the frozen exe**, not only the source tree. PyInstaller caches
  aggressively and a no-op rebuild silently re-copies the previous binary, so
  a "succeeded" build proves nothing; read the code back out of the archive.
- **Write the number into the code** at the place it explains, with the date.

---

## How to measure - the harness is already written and validated

    C:\Users\Br3e\AppData\Local\Temp\claude\C--Users-Br3e-Desktop-Atomic\
      18a3323e-492a-4761-8654-3cd1bd773a36\scratchpad\vsync.py

A thread blocks on `IDXGIOutput::WaitForVBlank` (ctypes, dxgi.dll, ~40 lines)
and records the scroll position at each real refresh. The per-refresh
*difference* is what the eye integrates.

**Paint counts lie.** This is the single most important thing in this
document. Before the fix, the app reported 134 paints/s while **43% of
refreshes showed no movement at all** - an effective 80 moving frames/s on a
143.9Hz panel. Both numbers were true; the paint count was the useless one.

Report: refreshes that moved nothing, per-refresh step median/p95/max, steps
more than 40% off the median, and effective moving frames/s.

Two harness traps, both already paid for:
- a `processEvents`-spin harness throttles the frame rate and lies; measure
  under `app.exec()` with a QTimer;
- the scroll-feel harness counted stalls via `wheel._timer/_tick`, and the
  timer now lives on `wheel._motion`.

Home at 1600x900 has only 828px of range and its SideScrollers have maximum
0 - **Home measures the clamp, not the model. Use the Anime page.**

---

## Do not re-derive these - all measured, all dead ends

| tried | result |
|---|---|
| vblank-driven clock (thread on `WaitForVBlank` posting queued ticks) | judder **62-64%** against the timer's 12-15%. Queued ticks arrive in bursts, so movement *between paints* varies more. Built as `helpers/vsync.py`, measured, deleted. The finding is recorded in `widgets._Momentum`. |
| `QT_WIDGETS_RHI=1` (and `=d3d11`) | 75-76 frames/s against 94 |
| phase-locking the timer to paints | judder unchanged (17.9-18.7% vs 19.2-19.4%), 105 frames/s against 121 |
| caching `screen_tick_ms`; opaque viewport | inside control variance |
| `QOpenGLWidget` nested in a raster scroll page | 83 frames/s against 93 - **worse** |
| rewriting in C# / Avalonia | 87.6-95.4 fps against the Qt GPU surface's 111 - see section 1 |

**The momentum constants are measured and must not be "tidied":** FRICTION
7.0, MAX_SPEED 5200 with the excess kept in `_pending` (discarding it made an
8-notch flick travel *less* than slow scrolling), STOP_SPEED 30, RAMP 40,
ACCEL_MAX 1.7, MAX_DT 2/144. Each carries its measurement in
`helpers/widgets._Momentum`. Read them before changing one.

---

## House rules that bite in this work

- **Measuring comes first, then the fix.** A change nobody measured is a
  guess, however good the reasoning reads.
- **Say what was not measured.** "I did not exercise the browser launch live"
  is a finding; silence about it is a claim.
- Screen-measuring work owns the display - it cannot run concurrently with
  anything else, or the numbers are wrong rather than merely slower.
- Never test against real user data in `%APPDATA%\Atomic`; copy it and
  redirect `storage.DATA_DIR` before importing anything.
- Close any running Atomic before a build touches the binary.
- Implement, rebuild locally, then **stop** - the owner tests before code
  lands. Do not commit or push on your own initiative.

---

# 24 August 2026 - measured against Stremio, and what it changed

## The panel was at 60Hz

Found sitting at **1920x1080 @ 60 Hz** on a panel that offers 144. Three
sources agreed: Windows `EnumDisplaySettings`, Qt `QScreen.refreshRate`,
and the DXGI vblank thread (16.67ms). Every number above was taken at
144Hz, so the whole app was running at 42% of the rate it was tuned for.
The owner set it back. **Check this first, before believing any
"the app got slower" report.**

## Screen capture: two methods that lie, one that does not

Validated against ground truth - a window painting 256 distinct colours
at a known rate:

| method | changes seen | verdict |
|---|---|---|
| BitBlt from `GetDC(0)` | **0.0/s, 1 distinct value** | stale, always |
| `PrintWindow(PW_RENDERFULLCONTENT)` | 62.3/s, 250 distinct | works on Qt, **stale on Chromium** |
| **DXGI Desktop Duplication** | **61.8/s, 255 of 256** | works on anything |

Every measurement taken with the first two was discarded. Duplication
asks the compositor itself (`AcquireNextFrame` returns only on a real
present, with its own `LastPresentTime`), so it measures Atomic and
another app the same way. Three traps inside it, all paid for:
`ShowWindow(SW_RESTORE)` before reading the window rect un-maximizes the
window and the sample box lands on static desktop (confidence exactly
1.000, shift 0, every frame); the matcher needs a third of the box to
overlap, so a 420-row box cannot resolve a step over ~140px; and
`DXGI_ERROR_ACCESS_LOST` kills the duplication permanently unless it is
rebuilt, which reads as "0 presents" for a window that is visibly moving.

## What the shipping build actually measures

`Atomic.exe`, frozen, on the Anime grid at 144Hz:

| | Atomic.exe | Stremio (its whole range) |
|---|---|---|
| presents/s | **141-143** | 110-133 |
| dead refreshes | **1-2%** | 7-24% |
| step spread p95/med | 2.0x | 1.9-2.9x |
| local step-to-step, med / p95 | 8% / 107% | 8% / 108% |

**Atomic delivers frames better than Stremio does.** Two things to carry
forward rather than re-derive:

* **Stremio's numbers swing enormously with content-loading state** -
  12% uneven freshly loaded, 51% a few screens deeper. Measuring it once,
  in its best state, produces a comparison that is simply wrong. Ask for
  its range, not its number.
* **"% off the global median" is not a judder metric.** It scores an
  *accelerating* scroll as uneven, and Atomic accelerates on purpose
  (`ACCEL_MAX 1.7`) while Stremio does not. Use the local step-to-step
  change instead. This mistake produced a confident "Atomic 35% vs
  Stremio 12%" that did not survive a fair metric.

## The one change that landed

`FrameMotion.step` computed the position from `perf_counter()` at paint
time, but the frame is shown at the **next vblank**, and that gap
jitters. The jitter lands in the position, and the difference between two
presented positions is the step the eye integrates. The timestep is now
snapped to whole refreshes, read from the actual screen:

    step spread   3.1x  ->  2.0x     (two controls each way)
    scroll speed  2297  ->  2352 px/s (unchanged)

`MAX_DT` now derives from the real refresh interval too - hardcoded
`2/144` is 13.89ms, **shorter than one 60Hz frame**, so on a 60Hz panel
it would clamp the snap away. On its own that clamp measured as changing
nothing (47.8 fps either way); it matters only now that something
depends on dt.

## The player: not broken, and now unblocked

Measured for the first time. Over 10s of 1080p60: **0 dropped, 0 delayed,
0 mistimed, vsync-jitter 0.0001, vsync-ratio exactly 1.000**. The video
path has no defect - it was the panel, like everything else.

And the blocker on GPU scrolling is gone: **libmpv's render API is
present** in the vendored DLL (all seven entry points) and in python-mpv
(`MpvRenderContext`). A spike drew mpv into a `QOpenGLWidget` FBO with
**no native child window** - verdict "DRAWS, AND MOVES", 8 framebuffer
reads, 0 flat, 7 consecutive changes. That removes the exact conflict
that forced the GPU grid's revert, and would also retire the layered
native overlay bars.

**Not tested:** hwdec through the render API - the spike's clip is
uncompressed raw video, so `hwdec-current: no` proves nothing. Test that
before building on this.
