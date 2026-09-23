# Atomic — Version Description Document

**Version 2.8** · 24 September 2026

---

## 1. Identification

| | |
|---|---|
| System name | Atomic |
| Version | 2.8 |
| Release date | 2026-09-24 |
| Repository | https://github.com/Khaled-Alneef/Atomic |
| Target platform | Windows 10 / 11, 64-bit |
| Delivered as | `Atomic.zip` — one **GitHub release asset** holding the ~11 MB **bridge installer** as its only `Atomic.exe` and the application **as a folder**, packed as `app.zip` (`Atomic/Atomic.exe` + `Atomic/_internal/`) |
| At the tag | `Atomic.exe` / `Atomic.zip` — the same bridge, for installs predating 2.0 |

---

## 2. System overview

Unchanged in purpose from 2.0 — see `docs/VDD-2.0.md` §2. Unchanged in shape
from 2.7 — folder install, bridge transition — see `docs/VDD-2.7.md` §2/§5.

---

## 3. Inventory of materials released

| Item | Description |
|---|---|
| `Atomic.zip` (release asset) | **137,499,266 bytes**. SHA-256 `0bd09c4f0d038bb1abac7871643aa2b0306e5815f680f6c75fa52673e3403598` |
| `app.zip` (inside it) | The application folder, **126,609,611 bytes**. SHA-256 `6c82c82e9823038c078e2c7c30b229cac391e09c43050c96a3fd2c526ce02f1f` |
| `Atomic/Atomic.exe` (inside app.zip) | The folder build's launcher, **8,125,522 bytes**. SHA-256 `c1e227e8a6de6e70bc82980beb7755242610f3b61f205417831cd38a58dcf7b2` |
| `Atomic.exe` (inside Atomic.zip, and committed at `v2.8`) | The bridge installer, **11,127,282 bytes**. SHA-256 `970593bc43ca52eeddd518ec8e580fe67d0ed55e06370fc7bdb5905d51df9d18` |
| `Atomic.zip` (committed at `v2.8`) | The bridge alone, zipped, for installs predating 2.0: **10,887,603 bytes**. SHA-256 `c4382008044b9583d93e951fd8ea768275e5f8b697a048f259ba9d68729a8241` |
| `src/`, `packaging/` | Full source; `packaging/build.py --zip` writes the asset |
| `docs/VDD-2.8.md` | This document |

Built with CPython 3.13.15, PyInstaller 6.22.3, PyQt6 6.11.0, on the
machine 2.7 was built on.

---

## 4. What this version provides

- **The Quick Apps and Websites lists on Home own their mouse wheel.**
  Each list is its own `.tiles` scroller (app.css, since the lists were
  first split into their own scrollable frames) with `overflow-y: auto`
  and `overscroll-behavior: contain` - but the page-wide wheel glide took
  every non-finger notch before the browser ever saw it, so a notch over
  either box scrolled the whole Home page instead of the list under the
  pointer. A notch over `.row.quick .tiles` now eases that element's own
  `scrollTop`, contained at the list's own ends, never handed on to the
  page.
- **Scrolling either list fast is as smooth as scrolling it slowly.**
  The first cut of the fix above restarted its eased tween's clock on
  every notch (the same shape `.strip`'s sideways scroller already used);
  a real spin sends several notches inside one ease's ~130ms window, and
  restarting always re-entered the curve at its slowest instant, so a
  burst never got past the opening for as long as it kept arriving -
  reported directly: "when I scroll it seems super slow but when I
  scroll slowly it seems good". Replaced with the same chained-clock,
  ease-out shape the page's own scroll glide uses (`glideStep`, "A
  chained notch keeps the frames' own clock", 8 September 2026),
  generalised off one element's `scrollTop` instead of the page's, so a
  burst of notches keeps its momentum instead of restarting.

---

## 5. Design notes

See `docs/VDD-2.7.md` §5 for the folder/bridge design, unchanged here.

The two Home fixes are one mechanism split by symptom: the first made
the wheel move the right element, the second made that element's own
motion behave the same under a fast burst as under a slow one. Both
live in one function pair in `src/web/static/app.js` (the `.tiles`
branch of the page's global `wheel` listener, and `tilesGlide`), not
copied per box - Quick Apps and Websites share the same `.row.quick
.tiles` markup and so share the same handler without a per-box branch.

---

## 6. Configuration and user data

No change from 2.7 — see `docs/VDD-2.7.md` §6.

---

## 7. External interfaces

No change from 2.7 — see `docs/VDD-2.7.md` §7.

---

## 8. Installation and removal

No change from 2.7 — see `docs/VDD-2.7.md` §8.

---

## 9. Known limitations

Unchanged from 2.7 — see `docs/VDD-2.7.md` §9.

---

## 10. Verification performed

Method: `.claude/rules/testing.md`, on the frozen build, against copies
of the owner's data.

**The scroll-target fix**: driven from outside (`rig.py`, real
`WM_MOUSEWHEEL` posted to the WebView2 window under the pointer, not
`SendInput`) against a copy of the owner's data padded to 15 websites
and 6 quick apps to force real overflow. A notch over either box
scrolled only that box's own list, stopped cleanly at the list's own
ends, with the Home banner, the Watching row and the other box
untouched in every screenshot. The owner then tested the frozen build
himself and reported the scroll-speed regression below.

**The scroll-speed fix**: reproduced the report (fast wheel spin felt
sluggish, a slow single notch did not) by reading the first fix's
tween against the page's own already-debugged glide and finding the
same restart-on-every-notch shape the page's glide was fixed against on
8 September 2026. Rebuilt with `tilesGlide` (continuous clock, ease-out
curve, chained rather than restarted). The owner tested the rebuilt
frozen exe himself and approved it before this release was tagged.

**Build gates**: `check_release_notes.py` — 2 notes for 2.8; `build.py`
— 1,609 entries, 32 bundled files byte-identical to `src/`; `build_bridge.py`
— built, selftest reached the live releases API (resolved v2.7, the
newest tag at build time).

**Defender**: `WinDefend` running; `MpCmdRun -Scan -ScanType 3
-DisableRemediation` over the app launcher (`app/Atomic.exe`), the
bridge exe and the committed `Atomic.zip`, and the release asset
`Atomic.zip` — exit 0 each, no threats.

**Not exercised before publishing**: a real update through GitHub from
an installed 2.7 (checked after publishing, per the marker-tag
procedure); any machine but the owner's.

---

## 11. Glossary

See `docs/VDD-2.0.md` §12.
