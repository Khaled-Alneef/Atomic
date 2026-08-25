# Remote Tests

Builds parked here so they can be downloaded and tried on another
machine. **Nothing here is a release**: no tag, no version bump, and
the in-app updater ignores this branch entirely (`updater.RELEASE_TAG_RE`
matches `^v?\d+\.\d+$`, which a branch name cannot be). Releases still
happen on `main` alone.

The branch has no shared history with `development` or `main` - it is a
single orphan commit holding one file, so it costs the repository the
binary and nothing else.

## Current build

| | |
|---|---|
| Version | 1.10.32 (unreleased - `development` at e04b6ba plus uncommitted work) |
| Built | 25 August 2026 |
| SHA-256 | `2fdb83601649d185129ae8c73aa69b424766568291e63b857509738527289a00` |

What is in it beyond 1.10.32: icons on the Saved/Schedule/History pills,
the folded sidebar holding the icon rows at their unfolded positions with
10% larger icons, Atomic user ratings moved to the whole title, the DONE
badge no longer cut off in the episode/chapter list, the statistics
panel's "Refreshes per frame" row named the right way round, and the hero
logo's halo baked into the pixmap so it is not re-blurred on every scroll
frame (Watch > Discover: 207-222 scroll positions/s -> 239, on a 240Hz
panel).

Windows SmartScreen will warn before it runs: the exe is unsigned and a
fresh upload has no download reputation. That is roadmap item #8, and it
is a false positive - verify the SHA-256 above if in doubt.
