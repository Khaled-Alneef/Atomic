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
| Version | 1.10.39 (unreleased) |
| Built | 26 August 2026, ninth build |
| SHA-256 | `3bdc7290598d7569ffb51db0811b17e9b73393c0e43aac7b3d7e271e9ea90c3d` |

Extract, then run `Atomic.exe`.

**New in this one:** the Harbor re-theme, and three passes of fixes on
top of it. Navy and teal throughout, a window with no native caption
carrying one search field, the sidebar rail on SVG icons with animated
rows, and the top bar now above the player, the reader and the episode
list rather than under them.

The player: relative seeking lands where you pressed instead of
compounding from a stale target - measured live on Attack on Titan
S01E02, where +5 from 26.65s used to ask for 29.25 and now 4.23 asks
for 9.23. A forward seek past the buffer asks the swarm for those
pieces rather than decoding whatever it can reach, which is what put
frames of somewhere else in the episode on screen. Motion Smoothing,
off by default, actually engages: mpv's own interpolation-threshold was
disabling it at an exact 10.000 vsync ratio, which is every 23.976 file
on a 240Hz panel. The pacing itself measured clean either way - jitter
0.0003, 0 dropped, 0 late, hwdec d3d11va.

Also: Airing Soon carries series as well as anime, artwork is re-cut
when the window moves to a differently-scaled monitor, hero logos are
scaled once instead of twice, and Home's hero animates through a fold
rather than snapping at the end of it - 2 banner paints per fold
before, 26 after.

## What was ruled out when the warning started

Seven builds compared, extracted from this branch's own history: the
same 193 bundled entries in every one (nothing added, ever), the same
347 Python modules (none added), a byte-identical bootloader (the first
differing byte is the PE TimeDateStamp at 0x108), and all seven scanning
clean under Defender with cloud protection on and the Mark-of-the-Web
set. `upx=True` in Atomic.spec has never applied - upx.exe is not
installed - so the usual first suspect was never in play.
