# Atomic — Version Description Document

**Version 3.12** · 21 October 2026

---

## 1. Identification

| | |
|---|---|
| System name | Atomic |
| Version | 3.12 |
| Release date | 2026-10-21 |
| Repository | https://github.com/Khaled-Alneef/Atomic |
| Target platform | Windows 10 / 11, 64-bit |
| Delivered as | `Atomic.zip` — one **GitHub release asset** holding the ~11 MB **bridge installer** as its only `Atomic.exe` and the application **as a folder**, packed as `app.zip` |
| At the tag | `Atomic.exe` / `Atomic.zip` — the same bridge, for installs predating 2.0 |
| Asset size | 135,713,737 bytes |
| Asset SHA-256 | `3e240bfaa6d6f0e6c1bc56761be328014ad09de7eeafafba04013592a552ff62` |
| Bridge SHA-256 | `bd97cf5208821e5c7f080483fd9de662d7e5131e662024f661f888aef73fac38` (11,180,340 bytes) |
| Preceded by | 3.11 — `docs/VDD-3.11.md` |

---

## 2. System overview

Unchanged from 3.11.

---

## 3. Inventory of materials released

Unchanged from 3.11 §3.

**Changes since the last release** (`git log released/3.11..development`):

| Commit | Summary |
|---|---|
| 88125dd | Atomic 3.12 - Novel Fire chapters open with their text again |

---

## 4. What this version provides

**Novel Fire chapters show their text again.** Since Novel Fire moved its
chapter pages behind a hand-off, every chapter opened from a Novel Fire
novel drew an empty reader. Search, browse, the details page and the
chapter list were never affected.

---

## 5. Design notes

**A chapter address is a hand-off now.** Measured 21 October 2026: every
`novelfire.net/book/<slug>/chapter-<N>` answers a 302 to `readnovel.site`,
whose 2,156-byte "Loading..." page carries no text - only a script setting
`window.location.href` to `novelphoenix.com/novel/<slug>/chapter-<N>`.
The app read the stub and found no `id="content"` (the owner's log:
`paragraphs=0`, `reason=empty`). `novel_sites._nf_text` now follows that
script redirect, at most `_NF_HOPS` (2) hops, reading the target off the
page so a later change of mirror is followed too. `novelphoenix.com` is
the same engine and answers a plain request in 0.3s with no challenge.
Each hand-off writes `novels: novelfire chapter handed off to <host>`.

---

## 6. Configuration and user data

Unchanged from 3.11. Nothing new is written.

## 7. External interfaces

One addition: chapter text for Novel Fire is read from whatever host
Novel Fire's chapter page hands off to - `novelphoenix.com` at release.

## 8. Installation and removal

Unchanged from 3.11 §8.

---

## 9. Known limitations

Carried forward from 3.11 §9. A Novel Fire chapter costs one more request
than before (1.1-2.1s measured); the next chapter is still prefetched
behind the one on screen.

---

## 10. Verification performed

**Live, through the app's own `chapter_text`**: Reverend Insanity 2308
(the chapter in the owner's log) 97 paragraphs in 2.1s, Shadow Slave 1
96 in 1.1s, Shadow Slave 3221 160 in 1.1s - 0 for each before the fix.
Em dashes come through intact.

**The frozen build was handed to the owner to test**; he approved the
release. The reader was not photographed on the frozen build by the
session that made the fix.

**The release build, read back out of its own archive**: "3.12" in
`helpers.updater`, `helpers.development_version_patch` and the what's-new
notes; `_SCRIPT_REDIRECT` in `helpers.novel_sites`. Bridge SELFTEST OK.

**Defender**, service Running: exit 0 on the app `Atomic.exe`, the bridge
`Atomic.exe` and `Atomic.zip`, twice.

**The release-notes gate:** one note written for 3.12.

---

## 11. Glossary

Unchanged from 3.11 §11.
