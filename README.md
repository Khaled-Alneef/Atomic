# Remote Tests

Builds parked here so they can be downloaded and tried on another
machine. **Nothing here is a release**: no tag, no version bump, and the
in-app updater ignores this branch.

## Current build

| | |
|---|---|
| Version | 1.10.32 (unreleased) |
| Built | 25 August 2026, fourth build |
| SHA-256 | `ea2a8bc7b59e7fbf17bae40553e9fc489576eec433637bea1442489cc985e5eb` |

### Fixed since the third build

- **Covers were being thrown away after they arrived.** A grid cover is
  requested when a cell scrolls into view and the answer comes back
  seconds later; both ends were gated on a *run number* that advances on
  every category switch, every search keystroke and every page visit, so
  a late-but-still-correct cover was discarded and the cell stayed blank
  until something rebuilt the grid. That is why walking away and coming
  back "fixed" it, and why History and Schedule - which do not use runs
  this way - were always fine. The answer is now matched to the *row*
  it was fetched for: a cover five runs late is applied, one stamped for
  a different row is still rejected (both checked).
- **Source lookups skip hosts that are refusing**, like the covers
  already do. On a network blocking the indexers a lookup now gives up
  in 0.4-0.8s instead of waiting on each dead host in turn.
- **The sidebar fits the window**: half-height separators, the logo band
  giving way, rows down to 44px - every row on screen without scrolling
  at 720 / 768 / 900 / 1132px, folded and unfolded.

### Still open, not in this build

The loading logo appearing the instant an episode is pressed, the
statistics panel while sourcing, the reader's upward scrolling, and the
white window that flashes when Discover is pressed - that last one I
could not reproduce here (no stray Qt window appears, and every
subprocess this app starts already suppresses its console).
