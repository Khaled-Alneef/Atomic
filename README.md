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
| Version | 1.10.44 (unreleased) |
| Built | 26 August 2026, twelfth build |
| SHA-256 | `56bfe223a9b11535504f5a1e77890ded6c337564675b5a8f00867580c3d0be1c` |

Extract, then run `Atomic.exe`.

**New in this one:** the window's top bar stays up in full screen, and
its Back button is gone. Going back is still Alt+Left, mouse button 4,
or Escape out of whatever is open - the button was one route of several
and the only one taking space next to the search field.

Before that, in this build: every sidebar icon animates as itself (the
compass finds north, sparkles sprinkle, the Atomic mark switches on
inside the monitor, the gear turns), three hover states that could stick
are fixed, searching from an episode or chapter list reaches Discover,
the Settings sidebar no longer scrolls and its Uninstall row matches the
rest, and the hero banner's picture follows the sidebar fold instead of
resizing once at the end of it.

Earlier still: seeking that lands where you pressed (measured live on
Attack on Titan S01E02), Motion Smoothing that actually engages, the
player and reader covering the window's own bar, Airing Soon carrying
series as well as anime, and the Harbor navy/teal re-theme throughout.

## What was ruled out when the warning started

Seven builds compared, extracted from this branch's own history: the
same 193 bundled entries in every one (nothing added, ever), the same
347 Python modules (none added), a byte-identical bootloader (the first
differing byte is the PE TimeDateStamp at 0x108), and all seven scanning
clean under Defender with cloud protection on and the Mark-of-the-Web
set. `upx=True` in Atomic.spec has never applied - upx.exe is not
installed - so the usual first suspect was never in play.
