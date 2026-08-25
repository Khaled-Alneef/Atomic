# Remote Tests

Builds parked here so they can be downloaded and tried on another
machine. **Nothing here is a release**: no tag, no version bump, and the
in-app updater ignores this branch.

## Current build

| | |
|---|---|
| Version | 1.10.32 (unreleased) |
| Built | 25 August 2026, seventh build |
| Atomic.exe SHA-256 | `186de6e68273cc4d25d8b7226680be4403306187758e0f84c804ae14048659fe` |
| Atomic.zip SHA-256 | `d36b611ccc3a686436b5877b1b5731960ae68e494ff9c149adbe45d5040856f7` |

## If the download is blocked as a virus

It is a false positive - **Trojan:Win32/Wacatac.B!ml**, the `!ml`
suffix meaning Microsoft's machine-learning classifier rather than a
signature match. Roadmap item #8 records the bisection that proved it:
1.0 through 1.1.5 clean, 1.2 flagged, and the two differ by one line.

Measured again 25 August 2026 on this build: Defender's on-demand scan
calls it clean, **including with the Mark-of-the-Web set to the
raw.githubusercontent URL it is downloaded from**, so the verdict is
cloud-delivered at download time and is about reputation - a freshly
uploaded, unsigned binary nobody has downloaded before - rather than
about anything in the file.

Two ways past it:

1. **Download `Atomic.zip` instead.** The browser's own
   "not commonly downloaded" block applies to bare .exe files, and the
   download-time scan usually lets an archive through. Extract, then run.
2. **Allow it once**: Windows Security -> Protection history -> the
   Atomic entry -> Actions -> Allow. Check the SHA-256 above first if
   you want to be sure the file is the one built here.

The durable fixes are Microsoft's false-positive form
(microsoft.com/wdsi/filesubmission, free, usually cleared in a day or
two) and code signing (roadmap #16, ~$10/mo, the owner's purchase to
make). Neither is something a build can do by itself.
