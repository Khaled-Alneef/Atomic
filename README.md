# Remote Tests

Builds parked here so they can be downloaded and tried on another
machine. **Nothing here is a release**: no tag, no version bump, and the
in-app updater ignores this branch.

**The build ships as `Atomic.zip`, never as a bare `Atomic.exe`** -
the owner's standing rule of 25 August 2026, now CLAUDE.md rule 8. It is
a measurement, not a preference: the exe was refused on download as
`Trojan:Win32/Wacatac.B!ml` - Microsoft's machine-learning classifier,
no signature match - while the identical bytes inside a zip downloaded
cleanly.

## Current build

| | |
|---|---|
| Version | 1.10.45 (unreleased) |
| Built | 26 August 2026, thirteenth build |
| SHA-256 | `4ce81b5d039291e4089c51ffcb511c1f27b2c11ccb9133d183750576128941de` |

Extract, then run `Atomic.exe`.

**New in this one:** Saved, Schedule and History moved out of the
tracker page headers and into the window's own bar - one set instead of
two, reachable from anywhere. Opening one shows a Watch/Read pair inside
it, and pressing a section keeps whichever medium you were already in.

The sidebar mark turns as the rail folds, and four icons were rewritten:
the Anime star is centred now and throws sparkles to the four compass
points, Manga lights comic panels across the book, Websites loads a page
and becomes ready, Apps opens one tile while the rest step back, and the
Games controller takes input without the shell moving.

Also in this build: the top bar survives full screen and lost its Back
button, every sidebar icon has its own animation, three hover states
that could stick are fixed, searching from an episode or chapter list
reaches Discover, the Settings sidebar no longer scrolls, and the hero
banner's picture follows the sidebar fold.

Earlier: seeking that lands where you pressed (measured live on Attack
on Titan S01E02), Motion Smoothing that actually engages, the player and
reader covering the window's own bar, Airing Soon carrying series as
well as anime, and the Harbor navy/teal re-theme throughout.

## What was ruled out when the warning started

Seven builds compared, extracted from this branch's own history: the
same 193 bundled entries in every one (nothing added, ever), the same
347 Python modules (none added), a byte-identical bootloader (the first
differing byte is the PE TimeDateStamp at 0x108), and all seven scanning
clean under Defender with cloud protection on and the Mark-of-the-Web
set. `upx=True` in Atomic.spec has never applied - upx.exe is not
installed - so the usual first suspect was never in play.
