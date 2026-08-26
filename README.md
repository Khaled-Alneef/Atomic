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
| Version | 1.10.41 (unreleased) |
| Built | 26 August 2026, tenth build |
| SHA-256 | `451136266f6a0d2f9317404bff96df4a327071e597cf39e457c4b2e9a0e05911` |

Extract, then run `Atomic.exe`.

**New in this one:** the sidebar icons move. Not just a pill fading in
behind a brightening glyph - the compass turns, the gear turns further,
the house lifts, the book tilts, each keyed to what the icon is. Eased
at paint, so a reversal is smooth from wherever it currently is.

That work found a real trap worth recording: **QPointF was never
imported into main.py**, so the first version killed the process on the
first hovered frame - a NameError inside a paintEvent, which PyQt6
answers by aborting, leaving no traceback and nothing for faulthandler
to report either. Found by bisecting the paint in three stages behind an
env flag. Measured after the fix: 551 pixels of a Discover row and 442
of an Anime row differ between transform-on and transform-off at
identical tint, against an icon of about 676 - so most of the glyph
genuinely moves, and after three rapid sweeps the shared timer is
stopped with no row holding state.

The player and the reader now both cover the window's own bar; the
episode and chapter lists keep it. Every page follows the sidebar fold
frame by frame rather than snapping when it lands - the heaviest grid
paints 35 times at 4.5ms apart against 2 paints 190ms apart before.
Enter in the search bar reaches Discover even before suggestions arrive.
Disabled controls no longer offer the pointing hand. Settings' Uninstall
row is finally the same shape as the eight above it.

Earlier passes in this build: seeking that lands where you pressed
(measured live on Attack on Titan S01E02), Motion Smoothing that
actually engages, Airing Soon carrying series as well as anime, and the
Harbor navy/teal re-theme throughout.

## What was ruled out when the warning started

Seven builds compared, extracted from this branch's own history: the
same 193 bundled entries in every one (nothing added, ever), the same
347 Python modules (none added), a byte-identical bootloader (the first
differing byte is the PE TimeDateStamp at 0x108), and all seven scanning
clean under Defender with cloud protection on and the Mark-of-the-Web
set. `upx=True` in Atomic.spec has never applied - upx.exe is not
installed - so the usual first suspect was never in play.
