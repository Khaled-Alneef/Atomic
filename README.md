# Remote Tests

Builds parked here so they can be downloaded and tried on another
machine. **Nothing here is a release**: no tag, no version bump, and the
in-app updater ignores this branch entirely (`updater.RELEASE_TAG_RE`
matches `^v?\d+\.\d+$`, which a branch name cannot be). Releases still
happen on `main` alone.

## Current build

| | |
|---|---|
| Version | 1.10.32 (unreleased) |
| Built | 25 August 2026, second build of the day |
| SHA-256 | `92ef95e576f2163c051633852e0292f57f4a59a62fac21d17dcffe7dc746bd5a` |

### Fixed since the first build here

- **Sidebar rows no longer disappear on a short window.** Measured at a
  720px-tall window: the reading block was handed 131px for 453px of
  rows, so five of its seven rows were not drawn and nothing said so.
  The rails now sit in a scrolling column - every row is reachable at
  any window height, folded or not.
- **No GitHub token field in Settings** (or in the first-run wizard).
- **Pressing an open Saved/Schedule/History pill goes back** to the
  browse it was opened from.

Windows SmartScreen will warn before it runs: the exe is unsigned and a
fresh upload has no download reputation. Roadmap item #8; it is a false
positive - verify the SHA-256 above if in doubt.

### If artwork, logos or sources do nothing on a work network

The TMDB key is **bundled in this exe** - it is not something to paste.
Blank covers, no title logos and no playing sources together mean the
network is refusing the hosts, not that the app is unconfigured. Check
`%APPDATA%\Atomic\atomic.log`, and try opening
`https://images.metahub.space/poster/medium/tt2560140/img` in a browser
on that machine.
