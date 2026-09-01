# Remote Tests

> **The build on this branch is 1.10.107 and cannot be updated in place.**
> `Atomic.zip` is now **209.3 MB** and GitHub refuses any file in a
> repository over **100 MB**, so a newer build cannot be committed here at
> all. The push is rejected by the server, not by a setting anybody can
> change:
>
>     remote: error: File Atomic.zip is 209.32 MB; this exceeds
>     remote: error: GitHub's file size limit of 100.00 MB
>
> Zipping buys nothing: PyInstaller already zlib-compresses its embedded
> archive, so 210.4 MB of exe becomes 209.3 MB of zip - **0.5%**. The
> 92 MB file sitting in this branch is from 1.10.107, when the exe was
> less than half the size.
>
> **What has to change.** The workflow below publishes the release asset
> *from the copy committed here*, so the zip has to reach the branch
> first, and it no longer can. Dropping QtWebEngine (85.1 MB, and only
> the category grids still use it) would take the exe to about 125 MB -
> still over the limit. So the artifact has to stop travelling through
> git: uploaded straight to the `test-latest` release instead, where the
> ceiling is 2 GB.
>
>     gh release upload test-latest Atomic.zip --clobber
>
> Until that lands, build locally with `python packaging/build.py --zip`.

Builds parked here so they can be downloaded and tried on another
machine. **Nothing here is a release**: no tag, no version bump, and the
in-app updater ignores this branch.

## Install

Download the latest build from the
[**test-latest**](https://github.com/Khaled-Alneef/Atomic/releases/tag/test-latest)
pre-release, or straight from the link in the table.

| Platform | Format |
|---|---|
| **Windows** | [`Atomic.zip`](https://github.com/Khaled-Alneef/Atomic/releases/download/test-latest/Atomic.zip) - extract and run `Atomic.exe`, no installer |

Windows is the only platform Atomic is built for. There is no macOS or
Linux build and no web version.

**What the current build is made of** (measured 1 September 2026, out of
the frozen archive):

| | size | share |
|---|---|---|
| QtWebEngine | 85.1 MB | 41% |
| PyQt6 / Qt | 55.4 MB | 26% |
| libmpv | 43.2 MB | 21% |
| everything else | 26.2 MB | 12% |
| **total** | **209.9 MB** | |

QtWebEngine is still there for the category grids alone. Home, Discover
and the reading viewer render in **WebView2** now - Edge's own
compositor, which is what the whole scroll effort turned out to be
about - and neither they nor the reader need it.

That link never changes and always serves the newest build: a push to
this branch republishes it automatically, through
`.github/workflows/publish-test-build.yml`.

**Take it from the release, not from the file in this repository.**
Downloading `Atomic.zip` out of the branch is a cold CDN miss every
time - raw.githubusercontent has to inflate a 92MB blob out of a git
packfile before it can serve a byte. Measured 26 August 2026, three
requests for the same blob back to back:

    run 1   first byte 4.45s   x-cache MISS
    run 2   first byte 0.45s   x-cache HIT
    run 3   first byte 0.45s   x-cache HIT

The warm case never helps, because `cache-control` is `max-age=300` and
every build is a brand-new blob, so whoever downloads first always pays
the miss. The same file as a release asset is a stored file rather than
a git object: **1.08s cold**.

**The build ships as `Atomic.zip`, never as a bare `Atomic.exe`** -
the owner's standing rule of 25 August 2026, now CLAUDE.md rule 8. It is
a measurement, not a preference: the exe was refused on download as
`Trojan:Win32/Wacatac.B!ml` - Microsoft's machine-learning classifier,
no signature match - while the identical bytes inside a zip downloaded
cleanly.

## Current build

| | |
|---|---|
| Version | 1.10.74 (unreleased) |
| Built | 26 August 2026, twelfth build on this branch |
| Size | 92.2 MB |
| SHA-256 | `d3e7520382e99118c800274d66ce67614bb244331758d849cddc5cb6651fb497` |

Extract, then run `Atomic.exe`.

**New in this one:** asking for an episode no longer returns the same
episode of the *next* season. The wrong-season filter needs to know how
many seasons an entry has and what seasons the answer states, and had
neither for a title watched out of History - so season 2 fell outside
the bound, was read as another index's numbering, and the row was judged
on its episode number alone.

Before that: the Atomic Users Rating was removed. Ratings written by
every install cannot live on GitHub - a write credential shipped inside
the exe is extractable, auto-revoked by GitHub's own secret scanning and
rate-limited per token rather than per user, and with the key in the
client a login cannot mean anything, because the app can write as
anybody.

Also recently: the cat icon is the owner's own artwork with eyes that
close on hover, the Atomic mark carries a rounded play symbol instead of
a star, the search bar rides Home's scroll in full screen and no longer
swallows clicks meant for the page underneath, hero logos are sharp on a
scaled display, and the source list stopped showing one release twice.

## What was ruled out when the Defender warning started

Seven builds compared, extracted from this branch's own history: the
same 193 bundled entries in every one (nothing added, ever), the same
347 Python modules (none added), a byte-identical bootloader (the first
differing byte is the PE TimeDateStamp at 0x108), and all seven scanning
clean under Defender with cloud protection on and the Mark-of-the-Web
set. `upx=True` in Atomic.spec has never applied - upx.exe is not
installed - so the usual first suspect was never in play.
