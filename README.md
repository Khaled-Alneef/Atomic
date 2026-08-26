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
| Version | 1.10.43 (unreleased) |
| Built | 26 August 2026, eleventh build |
| SHA-256 | `48c20bc8f5d25d8f6d40b13a9149984dceb16e31ae28890242ef485a12b494b6` |

Extract, then run `Atomic.exe`.

**New in this one:** every sidebar icon has its own animation. Not one
transform with different numbers - the compass finds north, sparkles
sprinkle outward one after another, the Atomic mark switches on inside
the monitor, the gear turns and settles, books rearrange, a page turns,
a strip scrolls, a plugin snaps into its frame. Twenty icons are drawn
as cached layers so their parts can move independently; nothing parses
SVG while anything is animating.

Three stuck-state bugs of one family, all fixed: the continue ring stayed
lit on a card after clicking it (the fade is what hides it, and opening
the player over Home stops covered widgets repainting, so the fade froze
partway); a poster strip could keep a hover with the pointer elsewhere;
and a drifting button could stay lit. A Leave is not guaranteed when
something opens *over* a widget.

Searching from an episode or chapter list reaches Discover now - it used
to navigate the page stack underneath a list that was still on top.
Settings' sidebar does not scroll at all, and its Uninstall row finally
matches the eight above it. The hero banner's picture follows the fold
instead of resizing once at the end of it.

Earlier in this build: seeking that lands where you pressed, Motion
Smoothing that actually engages, the player and reader covering the
window's own bar, Airing Soon carrying series as well as anime, and the
Harbor navy/teal re-theme throughout.

## What was ruled out when the warning started

Seven builds compared, extracted from this branch's own history: the
same 193 bundled entries in every one (nothing added, ever), the same
347 Python modules (none added), a byte-identical bootloader (the first
differing byte is the PE TimeDateStamp at 0x108), and all seven scanning
clean under Defender with cloud protection on and the Mark-of-the-Web
set. `upx=True` in Atomic.spec has never applied - upx.exe is not
installed - so the usual first suspect was never in play.
