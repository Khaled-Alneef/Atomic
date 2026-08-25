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
| Version | 1.10.32 (unreleased) |
| Built | 25 August 2026, eighth build |
| SHA-256 | `b45c4dc0120de99f19e197435a086528bee9aeee7dd8620b6b237b9e6442e8ef` |

Extract, then run `Atomic.exe`.

**New in this one:** the updater now installs from a zip. It asks a tag
for `Atomic.zip` first and falls back to `Atomic.exe`, so every
release already published still installs, and a zip is unpacked only
after the download has been checked against the hash GitHub reported. It
refuses an archive holding anything other than exactly one .exe rather
than guessing which to install.

## What was ruled out when the warning started

Seven builds compared, extracted from this branch's own history: the
same 193 bundled entries in every one (nothing added, ever), the same
347 Python modules (none added), a byte-identical bootloader (the first
differing byte is the PE TimeDateStamp at 0x108), and all seven scanning
clean under Defender with cloud protection on and the Mark-of-the-Web
set. `upx=True` in Atomic.spec has never applied - upx.exe is not
installed - so the usual first suspect was never in play.
