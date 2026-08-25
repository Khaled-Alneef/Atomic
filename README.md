# Remote Tests

Builds parked here so they can be downloaded and tried on another
machine. **Nothing here is a release**: no tag, no version bump, and the
in-app updater ignores this branch entirely.

## Current build

| | |
|---|---|
| Version | 1.10.32 (unreleased) |
| Built | 25 August 2026, third build of the day |
| SHA-256 | `4438d972a8d08edde535eddb9b02f43caf5a68aa80917b3d1da9ceed7aa668a5` |

### Fixed since the second build

- **Every sidebar row visible without scrolling.** The column now fits
  itself to the window: separators are half a row (down from a whole
  one), the logo band gives way when the rows need its space, and the
  rows shrink to as little as 44px before anything scrolls. Measured at
  720 / 768 / 900 / 1132px, folded and unfolded, three fold cycles each
  - fits at all of them, and stable (no oscillation between passes).
- **A host that refuses twice is not asked again for ten minutes.**
  Reproduced this laptop's network by refusing the hosts its own log
  names; two visits each to Watch and Read used to make **592 doomed
  connection attempts** (344 to images.metahub.space, 187 to
  api.themoviedb.org), each paying an 8s timeout and then a 20s retry
  before falling back. Now **23**, and a second visit to a page draws
  from the cache in 0.1-0.2s with no attempts at all. On a working
  network nothing is marked refusing and covers arrive exactly as
  before (91 + 60 on a first-run profile, checked).

### What this does not fix

If the network blocks every image host, there is still nothing to draw
- the app just fails fast and stays responsive instead of hanging for
minutes. Covers that did arrive are now kept and redrawn instantly.
