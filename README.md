# Remote Tests

Builds parked here so they can be downloaded and tried on another
machine. **Nothing here is a release**: no tag, no version bump, and the
in-app updater ignores this branch.

## Current build

| | |
|---|---|
| Version | 1.10.32 (unreleased) |
| Built | 25 August 2026, fifth build |
| SHA-256 | `efa32eec935bb7f977e7d20f537eaa870e7f2228bed0a537c06357a2a5090895` |

### Fixed since the fourth build

- **The white window that flashes.** Found: the details page made its
  "Save to My List" button visible *before* adding it to the layout, and
  a parentless widget that is shown is a window to Qt - title bar and
  all - until something reparents it a frame later. Every unsaved title
  opened from Discover flashed one. Fixed at the source, and a guard now
  suppresses the whole class of it and names the widget in the log.
- **Pill chains return to the browse they started from.** Anime ->
  Saved -> Schedule -> History -> History now lands back on Anime, not
  on Discover: a pill pressed from another pill no longer overwrites
  what "back" means.
- **Covers have their own queue, newest first.** Discover's cover
  fetches shared the four workers that fetch every other page's covers,
  its chapter lists and its schedules - one visit leaves ~114 queued
  jobs behind, and the next page's covers waited behind all of them.
- **The statistics panel says what startup is doing** instead of a
  panel of dashes while the loading screen is up.
- **The reader's upward scrolling**: one real desync fixed (a page
  settling into its real height moved the scrollbar without telling the
  glide). The rest is measured but not fixed - see below.

### Measured, not fixed

Scrolling up in the reader produces ~166 frames a second against 240
down. The motion model and the paint are both innocent (paint costs
0.26-0.51ms either way, and the position maths stays exact); the UI
thread stalls ~6ms per frame going up, two vblank ticks queue, and the
second collapses into the same frame slot. Finding what stalls it needs
a sampling profiler on the UI thread.
