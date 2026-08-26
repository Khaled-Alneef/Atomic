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
| Version | 1.10.46 (unreleased) |
| Built | 26 August 2026, fourteenth build |
| SHA-256 | `4dfc2f9008ebbd34b7873d17492f760efaf48c16dd98d838ff75cd7b7d2d6e53` |

Extract, then run `Atomic.exe`.

**New in this one:** a wheel notch starts moving on the frame you turn
it. The scrolling was already interpolating properly - 21 steps in a
clean ease-out over 144ms - but the first tick arrived 39ms late while
the shared clock woke up, and a stall followed by a run is what reads as
a jump. The distance per notch is unchanged.

Teal has depth in it at rest: the accent ramp runs corner to corner
rather than top to bottom, with a soft upper-left glint, a 1.5px lift on
hover and a 1px settle on press. Badges like CONTINUE WATCHING carry the
same material one step quieter, so the call to action still wins.

Also in this build: Saved, Schedule and History moved into the window's
bar with a Watch/Read pair inside each, the sidebar mark turns as the
rail folds, every sidebar icon animates as the thing it depicts, the top
bar survives full screen and lost its Back button, three stuck hover
states are fixed, and searching from an episode or chapter list reaches
Discover.

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
