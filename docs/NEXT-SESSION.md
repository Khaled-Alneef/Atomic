# Order for the next session

Written 24 August 2026, at the end of the session that tested a C# rewrite.
Hand this to a fresh session; it is self-contained.

---

## 0. One open item for the owner, not for the agent

- **A GitHub personal access token was pasted in plaintext in chat and must
  be rotated.** GitHub > Settings > Developer settings > Personal access
  tokens > revoke. It was never written to any file, never placed in a
  remote URL, and never used.

**Closed 24 August 2026 - `AtomicApplication` does not exist.** An earlier
version of this file left "the new repo is still undecided", naming an
`AtomicApplication/Atomic` that was to start at 0.0.0. The owner's words:
*"from now on, there is no account called AtomicApplication"*. There is one
remote and it is `origin` = `github.com/Khaled-Alneef/Atomic`. Do not
propose, create or push to any other; do not ask about it again.

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

---

# RESOLVED - and it was the opposite of what this section guessed

**Read the resolution at the end of this file before anything below.**
The section that follows is kept because its measurements are real and
its reasoning is the trail; its conclusion is wrong. The grid was not
painting at two thirds of the refresh rate. It was painting *faster*
than the panel refreshes, which is why the two beat.

---

# The question as it stood: does the grid paint at two thirds of the refresh rate?

This is the one thing on this page that is **not** resolved, and it is the
one the owner can see. His words, and they name the mechanism exactly:
*"when I move or scroll the text labels and image cards seems to be
refreshing on a stiff way"*.

The text half of that is fixed - cards are composited once and blitted
now (`_cell_pixmap`), and the paint fell from 4.21ms median to 3.20-3.29,
p95 6.71 to 6.07-6.32. The frozen exe delivers **143.7 presents/s with
0-1% dead refreshes** on a 144Hz panel. Frame *delivery* is at the
physical ceiling.

**What is left.** Driving a sustained scroll, the velocity is dead
constant - 4953 px/s, a flat trace, so this is not the wheel impulse
model (RAMP/FRICTION) pulsing. Yet the per-present steps are strictly
bimodal:

    13x233  12x186  25x86  26x86  11x85  24x61  23x44

One cluster, and another at **exactly double**. About a third of frames
advance two refreshes' worth instead of one, at constant speed. That is
what a stiff, pulsing scroll is.

It is present with and without the dt snap (checked by toggling it on the
same tree), and with and without the cell cache. It is not something
added on 24 August.

**The contradiction that has to be resolved first.** A third of frames
advancing double implies the widget is painting at roughly 96/s, not 144.
But the compositor reports a new frame on 99% of refreshes and only 1% of
presents show no movement at all. Both numbers are from the validated
Desktop Duplication instrument. **They cannot both be true as stated, and
whichever one is being misread is the whole answer.** Do not build a fix
on either until that is settled.

Untested hypothesis, offered only as a starting point and explicitly not
measured: a paint that finishes, blocks on a vsync-locked present, and
therefore misses the next vblank, settling into an every-other-refresh
cadence. Paint cost is 3.2ms against a 6.94ms budget, so the widget is
not too slow to draw every refresh - which is what makes the cadence, not
the cost, the suspect.

What would settle it: timestamp every paintEvent entry and exit inside
the running app, and line those up against the duplication instrument's
present timestamps in the same run. Nothing so far has put both clocks on
the same timeline; every measurement has had one or the other.

---

# The resolution: both clocks on one timeline

The question above was settled by doing the one thing no measurement had
done - stamping the app's paints and the compositor's presents on the
**same** clock. `QueryPerformanceCounter`, because that is what DXGI
stamps `LastPresentTime` with, and because `time.perf_counter`'s
reference point is documented as undefined and these are two processes.

    app PAINTS          1038 = 137.4/s     interval median 5.80ms
    compositor PRESENTS  827 = 109.5/s     ratio 1.26

**The widget was painting faster than the panel refreshes** - 5.80ms
between paints against a 6.944ms refresh - because `update()` at the end
of `paintEvent` asks for the next frame immediately and Qt's raster path
does not block on vsync. Not missed vblanks. Over-production.

That made the first snap wrong *in kind*: it added one whole refresh of
motion per paint, which is only correct if paints happen once per
refresh. Two fixes, each measured on its own:

  * **snap to the refresh GRID, not by an increment** - anchor a phase
    and round to it, so two paints inside one refresh carry the same
    timestamp and consecutive refreshes are exactly one frame apart;
  * **schedule the next frame at the next grid boundary** instead of
    immediately, so frames nobody can see are not drawn at all.

        paint interval   5.80ms -> 6.68ms (one refresh)
        paints:presents  1.26   -> 1.13
        step spread      2.0-2.9x -> 1.1x
        local jumps      42-74%   -> 3-7%

The same shape existed in `widgets._Momentum`, whose QTimer runs at
`screen_tick_ms` - 6ms on a 144Hz panel, because a QTimer only takes
whole milliseconds, so ~166 ticks against 144 refreshes. Same phase snap
applied; local step-to-step change fell from 24-28% to 0%.

## Two traps this cost, both worth inheriting

**A harness that measures one route first will make that route look
slow.** "Home -> Watch takes 148.7ms against Read's 48.0" was screen
capture start-up, paid by whichever transition ran first. With an order
control and a throwaway grab, Watch is not consistently slower. The real
difference was cold build cost, and it needed a harness with no screen
capture in it at all to see.

**"% off the global median" is not a judder metric.** It scores an
*accelerating* scroll as uneven. Use the local step-to-step change.

## What the smearing turned out to be

The owner: *"the text labels and the images leave a traces"*. Frames
pulled straight out of the compositor mid-scroll are **individually
sharp** - 10.12 against 11.20 for a still frame, and one was saved and
looked at. The app draws no traces. It is eye-tracked motion blur across
a sample-and-hold panel, and it is `velocity x frame hold time`.

Hold time is the panel's. Velocity, during a sustained scroll, is

    distance_per_notch x notches_per_second

**in which FRICTION cancels out entirely.** So capping `MAX_SPEED`
cannot reduce it - it only defers distance into `_pending`, which drains
after the hand stops, and that deferred distance *is* drift. Removing
acceleration is what worked. See the note on `FRICTION` in
`widgets._Momentum` for the numbers and for the trade that was accepted:
smear down, evenness partly given back, a third more wheel per page.

---

# Research: frames, smoothness, smear and friction

Read this before touching scroll physics again. It is the published
state of the art on the four things this app keeps re-fighting, checked
against the numbers measured here on 24 August 2026. Sources at the end.

## 1. One law governs the smear, and this app's numbers obey it

**Blur Busters Law: 1ms of persistence = 1 pixel of motion blur per
1000 px/s of motion.** A sample-and-hold display *holds* each frame for
the whole refresh while the eye keeps tracking smoothly, so the held
image is smeared across the retina. Nothing about the panel being "fast"
avoids it; it is the hold itself.

    refresh    frame held      blur at 1000 px/s
    60 Hz      16.7 ms         16.7 px
    144 Hz      6.94 ms         6.94 px      <- this machine
    240 Hz      4.2 ms          4.2 px

Measured here, and it lands on the law exactly:

    4953 px/s x 6.944ms = 34.4 px   (before acceleration was removed)
    1872 px/s x 6.944ms = 13.0 px   (after)

It is also why the panel sitting at 60Hz earlier that day was so much
worse: identical content, **2.4x the blur**, for free.

## 2. There are TWO blur sources and only one of them is ours

  * **Persistence (MPRT)** - the hold, 6.94ms here. Velocity scales it.
    This is the one the app can influence.
  * **Pixel response (GtG)** - how fast the crystal physically changes.
    It adds *on top* of persistence.

**On VA panels the dark transitions are the pathological case.** Rated
GtG is a best case; dark-level transitions are measured 50-80% slower,
around 20ms. The symptom has a name in the literature - "black
smearing" - and is described as *a lingering shadow or dark trail
dragging behind objects*. The owner's word for what he sees is
"traces".

**Atomic is a near-black UI** (BG #0e0c09, SURFACE #1a1712). Stremio's
is a lighter navy. On the same panel at the same velocity the dark app
smears worse, for reasons no code here can reach.

The monitor reports only a generic `LED MONITOR`, vendor code `FSI`
(unregistered), 2025, 1080p/144Hz - a class that is very commonly VA.
**The panel type could not be confirmed from software.**

**The test that decides it, and it takes ten seconds:** scroll a bright
region and a dark region at the same speed. Bright smearing noticeably
less means GtG/black smearing - the monitor's overdrive setting, not the
app - and it means velocity work is addressing the smaller term.
testufo.com/eyetracking does the same discrimination with a controlled
pattern.

**If the panel term dominates, the budget looks like this**, and the
app's share is the smallest of the three:

    dark-transition GtG    possibly ~20ms    monitor overdrive setting
    persistence at 144Hz   6.94ms            fixed by refresh rate
    velocity               scales both       the app

## 3. `update()` inside `paintEvent` is a documented anti-pattern

The bug this session found the hard way is a known one. Published
guidance: *"If you put update() inside paintEvent(), you're telling Qt
to schedule another repaint every time it finishes painting ... it will
consume as much CPU as it can ... The fix is to never call update()
inside paintEvent."*

Measured here with the app's paints and the compositor's presents on one
QPC clock: **137.4 paints/s against 109.5 presents**, paint interval
5.80ms against a 6.944ms refresh. Qt's raster path does **not** block on
vsync - only the GL path double-buffers to it - so the widget free-ran.

## 4. The 14px/28px steps were textbook judder

Judder is *the fingerprint of a frame-rate conversion that does not
divide evenly*. The classic is 24fps on 60Hz: 2.5 refreshes per frame is
impossible, so it alternates 3:2 and some frames are held a beat too
long. A 137:109 ratio is the same mechanism with worse numbers -
constant velocity arriving at the eye unevenly. **The fix is pacing, not
painting more.**

## 5. The motion model is already what the industry moved *to*

Two deliberate Chromium/Edge changes, both of which this app already
matches - so the model is not the thing to rewrite:

  * **impulse-style scroll** - *"each tick of the mouse wheel tries to
    mimic a physical world where content starts moving quickly (an
    impulse) and then slows due to friction"*. That is `FrameMotion` and
    `widgets._Momentum`.
  * **percent-based scrolling** - away from a fixed 100px per tick,
    toward a percentage of the scroller's height. That is
    `NOTCH_FRACTION = 0.0924`.

Android's `OverScroller` uses the same exponential-decay family.

## 6. Friction and smear are in fundamental tension - do not re-derive

During a sustained scroll,

    velocity  ~=  distance_per_notch  x  notches_per_second

**FRICTION cancels out.** It changes how a notch is spent, not the
average rate. Three consequences, all of which cost time to learn:

  * capping `MAX_SPEED` cannot reduce the average - it defers distance
    into `_pending`, which drains after the hand stops, and **that
    deferred distance is drift**. A request for less smear *and* no
    drift cannot be answered with the cap.
  * higher friction gives the same average with a **peakier** profile,
    which is why removing drift cost evenness (grid spread 1.1x -> 2.1x).
    Structural, not a tuning miss.
  * the only lever that lowers peak *and* average is **distance per
    notch**, and it costs travel one-for-one.

## 7. What could not be established

  * the panel type, and therefore whether GtG or persistence dominates;
  * any software technique that removes hold-blur without cutting
    velocity or raising the refresh rate. There is none. Backlight
    strobing / black frame insertion is the only real answer and it is a
    monitor feature.

## Sources

  * Blur Busters Law - blurbusters.com/blur-busters-law-amazing-journey-to-future-1000hz-displays-with-blurfree-sample-and-hold/
  * GtG versus MPRT FAQ - blurbusters.com/gtg-versus-mprt-frequently-asked-questions-about-display-pixel-response/
  * TestUFO eye-tracking demo - testufo.com/eyetracking
  * VA panel response time and black smearing - us.ktcplay.com/blogs/technology-hub/va-panel-response-time-explained
  * What is VA smearing - displayninja.com/what-is-va-smearing/
  * Chromium compositor thread architecture - chromium.org/developers/design-documents/compositor-thread-architecture/
  * Scrolling personality improvements in Microsoft Edge - blogs.windows.com/msedgedev/2020/04/02/scrolling-personality-improvements/
  * Percent-based scrolling (Intent to Ship) - groups.google.com/a/chromium.org/g/blink-dev/c/5Mt8RZyf-pc
  * Qt paintEvent / update guidance - pythonguis.com/faq/creating-a-new-widget-very-heavy-paintevent/
  * Refresh rates and frame pacing - digitechbytes.com/tech-basics-evergreen-fundamentals/refresh-rate-frame-pacing-basics/

---

# 24 August 2026, overnight - the display clock was not a clock

Five asks, worked in named phases. The one that matters to everything
else is the first.

## The finding: `WaitForVBlank` returns S_OK in 0.6 microseconds here

`widgets._VBlankTicker` drives **every** scrolling surface in the app -
`_Momentum` (so every `scroll_area`, Home, the Discover rows, the
reader strip) and `PosterGrid` alike. It waited on
`IDXGIOutput::WaitForVBlank` and treated a zero return as proof of a
refresh. Measured on the owner's machine (2560x1440, **240Hz**):

    IDXGIOutput::WaitForVBlank   2000 calls in 1.2ms = 1,677,149/s, all S_OK
    DwmFlush                      240 calls in 999.4ms = 240.1/s

So on this hardware the "vblank ticker" was a spin loop posting queued
signals into the UI thread as fast as a core could produce them, and
because it never returned an error, `failed` stayed False and the
millisecond-timer fallback was never reached. **This was not a
regression in the motion model - the motion model is fine. It was the
clock underneath it.**

Fixed by *timing* each candidate before trusting it
(`_VBlankTicker._plausible`), with DwmFlush preferred and DXGI kept as
the fallback. The ticker now measures 239 ticks in a second.

Measured on the real pages, real window, seeded 240-title library,
position sampled once per compositor present, wheel driven at a fixed
20 notches/s. "Before" is the same build with `ATOMIC_VBLANK_TRUST=1`:

| surface | before | after |
|---|---|---|
| Watch categories (PosterGrid) | 104.9 fps, 56.3% dead, x2.22 | **221.8, 7.6%, x1.20** |
| Saved grid (240 widget cards) | 148.6 fps, 38.1% dead, x2.00 | **207.9, 13.4%, x1.50** |
| Home | 194.0 fps, 19.2% dead, x2.00 | **230.8, 3.8%, x1.33** |

Three runs each way on the category grid, all consistent
(159.7/33.4%, 104.9/56.3%, 161.2/32.8% against 225.0/6.3%, 224.2/6.6%,
221.8/7.6%, 220.1/8.3%).

**Two measurement switches now exist and are worth knowing about:**
`ATOMIC_VBLANK_CLOCK=dwm|dxgi` picks which candidate is tried first and
`ATOMIC_VBLANK_TRUST=1` skips the plausibility check - the only way to
reproduce the old fault, and therefore the only way to A/B a fix for
it. `ATOMIC_NO_VBLANK=1` still forces the timer path.

**The millisecond timer measured *better* still and is deliberately not
what ships:** 236.0 fps, 1.7% dead, x1.25 on the same surface. It wins
because it schedules the paint before the boundary rather than reacting
after one. Its worst case is a cliff rather than a slope (this file
records 65.2 steps/s and 72.9% dead on one run in three, from Windows
quantising a 0-4ms request to ~13.9ms), and a clock that cannot be
quantised is worth eleven frames a second. Also measured, and contrary
to what the constant's own docstring says: `startup.allow_precise_timers()`
**returns False on this machine**, yet a 4ms Qt PreciseTimer fires at
4.003ms median / 4.122 p95 anyway. Do not read that failure as the
cause of anything without re-measuring.

## Two traps in the instrument, both paid for here

* **The handover's own harness does not work on this machine.** It
  describes a DXGI vblank thread; that is the exact call that lies here.
  The rebuilt harness (`vsync.py` in the session scratchpad) uses
  DwmFlush and validates at 240.0Hz to four figures.
* **Wrapping `paintEvent` on every class in `sys.modules` crashes the
  process** (exit 127, no traceback) as soon as a widget-built page
  scrolls. Restricting the wrap to classes whose `__module__` starts
  with `windows`/`helpers` fixes it. Worth knowing before writing the
  same counter again.

## Still open on scrolling, and measured rather than guessed

The widget-card grids remain the worst surface (13.4% dead against
PosterGrid's 7.6%). Instrumented, the Saved grid pays **19
`ContinueCover` paints per refresh, 4,149/s** - each one repainting its
whole 216px cover rather than the exposed strip. The page behind it
does *not* repaint per frame (`GlassPage`, 26 paints in 2.5s), so the
opaque-ground fix is holding; the cost is in the cards themselves.
`PosterGrid` already exists as the virtualized answer and the Saved tab
does not use it. Not attempted tonight - it is a real rewrite of the
hover tooltip, the continue button and the selection badges, and the
dominant fault was elsewhere.

Also worth recording: the owner's library **on this machine** is 8
titles and 0 series, so every saved-grid measurement without a seeded
library measures the clamp, not the motion - the same trap this file
already records for Home.

---

# The other four asks

## Cover art on a fresh install (the friend's report)

*"the images of all read/watch did not load except in the schedule
page, he did add his TMDB key"*.

Reproduced by naming the hosts rather than guessing: every Watch
Discover row's art comes from **one** host, `images.metahub.space` (35
of 36 rows across anime/series/movie), while the Watch schedule's comes
from `s4.anilist.co`. A machine that cannot reach that one host loses
every cover on the page and keeps the schedule's - exactly the shape of
the report. Driven on the real page with a cold `DATA_DIR` and that
host blackholed:

    host reachable                    90 cards, 90 covers
    host unreachable, before          90 cards,  0 covers
    host unreachable, after           90 cards, 90 covers

`cover_fetch.resolve` is the single answer now - the row's own URL, a
retry, then a catalogue that does not share a host with it (TMDB by
IMDb id for video, MangaDex/AniList by strict title for reading). Every
surface that draws a cover goes through it: Discover, Saved, Home,
Schedule, History.

**Not confirmed:** that this *is* what the friend hit. It could not be -
his machine was not available and the app logs nothing about a failed
cover. What is confirmed is that the failure mode existed, was
reachable, and is now covered. If it recurs, the next thing to add is a
log line naming the host that refused.

## Sourcing speed, on Attack on Titan

Split before touching anything (S01E05, the owner's own addon list):

    first partial       0.65-0.71s     53 rows
    find_streams total  1.97-4.16s     79 rows
    prepare_fastest     3.1-9.5s       to a playable url

Per stage: Torrentio 0.63s/59 rows, Torrentio Anime 0.71-2.32s/50,
indexers 0.38-0.42s/16, WatchHub 0.42-2.44s/**0 rows**, TorrentsDB
0.29s/0, and `arc_map_soon` **1.21s** on a cold franchise (nothing on a
warm one - the details page prewarms it).

**Two plans were killed by measurement and should not be re-proposed
without new evidence.** Both were A/B'd on one fixed candidate list,
interleaved with controls:

* *A seeder floor inside the Arabic tier.* The ranking does put an
  83-seeder release above an 836-seeder one, and that looks
  indefensible - but racing the big swarms first measured **worse**:
  8.1s and 8.1s to nothing, against controls of 5.2s, 9.5s, 3.1s, 3.1s
  that all found a url.
* *A wider race* (`RACE_WIDTH` 8 -> 14): 4.5s and 11.3s, inside the
  control's own spread.

The variance between two runs of the *same* config (3.1s vs 9.5s) is
larger than any difference between configs. Do not tune race constants
on fewer than a dozen runs.

**What landed instead is deterministic**: the details page warms
`find_streams` for the episode Continue would play, 700ms after it
opens, on a background thread (`details._prefetch_sources`). Measured
on the real page, same title, same 79 rows: **press-to-sources 4850ms
-> 0ms.** It deliberately does not warm the torrent race - that would
mean joining swarms for something nobody has pressed.

## Statistics while the video plays

The panel held the swarm and nothing else, so an episode on a direct
URL opened it to one sentence about there being no peers. It now has
two groups, and the second reads mpv's own properties: resolution, fps,
video/audio format, hwdec, bitrate, dropped-and-late frames, buffer
seconds. Verified on the real player over a real torrent of Attack on
Titan S01E05:

    Source    Peers 7   Speed 2.55 MB/s   Completed 24.00 %
    Playback  1920x1080   24.0   hevc   aac   d3d11va   3.7 s buffer
              Dropped 0 (0 late)

`video-format` rather than `video-codec` - the latter renders as
"H.264 / AVC / MPEG-4 AVC / MPEG-4 part 10" in a column a few dozen
pixels wide, measured on that same run.

## Sidebar icons

`RAIL_ICON_SIZE` 20 -> **26**. 20 x 1.28 = 25.6, and 26 is exactly the
delegate ceiling this file already derives (folded row 32px, margin 3,
32 - 2*3 = 26) - the largest size that keeps a folded icon centred.
Verified in the frozen exe (26 present in `main`'s consts, 20 absent)
and by ink measurement on a screenshot of the running app: the tall
glyphs measure 24-26px.
