# Remote Tests

Builds parked here so they can be downloaded and tried on another
machine. **Nothing here is a release**: no tag, no version bump, and the
in-app updater ignores this branch.

**The build ships as `Atomic.zip`, never as a bare `Atomic.exe`** - the
owner's standing rule of 25 August 2026, and it is a measurement, not a
preference: the exe was blocked on download as
`Trojan:Win32/Wacatac.B!ml` while the identical bytes inside a zip
downloaded cleanly. See CLAUDE.md rule 9.

## Current build

| | |
|---|---|
| Version | 1.10.32 (unreleased) |
| Built | 25 August 2026, seventh build |
| Download | `Atomic.zip` - extract, then run `Atomic.exe` |

## What was ruled out when the warning started

Seven builds were compared, extracted from this branch's own history:
the same 193 bundled entries in every one (nothing added, ever), the
same 347 Python modules (none added), a byte-identical bootloader (the
first differing byte is the PE TimeDateStamp at 0x108), and all seven
scanning clean under Defender with cloud protection on and the
Mark-of-the-Web set. `upx=True` in Atomic.spec has never applied -
upx.exe is not installed - so the usual first suspect was never in play.

Nothing in the code was ever shown to cause it, and the zip downloading
cleanly while the same bytes as an .exe did not is the evidence that it
was the container, not the contents.
