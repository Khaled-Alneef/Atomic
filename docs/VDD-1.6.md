# Atomic — Version Description Document

**Version 1.6** · 17 August 2026

---

## 1. Identification

| | |
|---|---|
| System name | Atomic |
| Version | 1.6 |
| Release date | 2026-08-17 |
| Repository | https://github.com/Khaled-Alneef/Atomic |
| Target platform | Windows 10 / 11, 64-bit |
| Delivered as | `Atomic.exe` — a single self-contained executable |

---

## 2. System overview

Unchanged from VDD-1.2 §2.

---

## 3. Inventory of materials released

| Item | Description |
|---|---|
| `Atomic.exe` | The application. 47,764,441 bytes. SHA-256 `2ba42c0fdfb401dd106afba09e70e04b2e6641c025bace8a8d7d77fadf3994be` |
| `src/` | Full Python source, 15,237 lines across 37 modules (15,230 at 1.5) |
| `packaging/` | `build.py`, `Atomic.spec` and `check_release_notes.py` |
| `docs/VDD-1.6.md` | This document |

Build environment unchanged from VDD-1.1 §3.

No development builds stand between 1.5 and this release: 1.6 is one
change taken straight off `development`, released the same day as 1.5.

---

## 4. Inventory of software contents

| Module changed | Lines | What changed |
|---|---|---|
| `helpers/theme.py` | 761 | The Anime and Movies & Series nav glyphs swapped |
| `helpers/whats_new.py` | 312 | 1.6's note |
| `helpers/updater.py` | 272 | `APP_VERSION` |

Nothing else in `src/` differs from 1.5.

---

## 5. What this version provides

### The Anime and Movies & Series icons have swapped

Anime carried `U+E714` (Video, a camera) and Movies & Series carried
`U+E7F4` (TVMonitor); they now hold each other's. A camera reads as
filming and a monitor as watching, which is the wrong way round for
anime against films and series.

Raised from the folded sidebar, which is the case that matters: folded,
the icon is all there is to go on, and the two rows sit three apart with
no labels to correct a wrong guess.

The glyphs swap; the page keys do not. `"anime"` and `"series"` stay as
they are, because saved nav orders, hidden-section lists and
`series.json` all refer to them — the same reason the key stayed
`"series"` when the label became "Movies & Series" in 1.4.

Everything else in this release is 1.5. See VDD-1.5 §5.

---

## 6. Design notes

**Why the icons are Segoe glyphs and not images.** Unchanged from the
reasoning recorded in `theme.py`: these are monochrome and inherit
whatever colour the QSS row gives them, so they follow the nav's
normal/hover/selected states on their own. An emoji or a PNG would
render in its own fixed colours and clash with the theme, and would be
another asset to keep in step with the palette.

**Why the source edit went through Python and not PowerShell.** These
codepoints are private-use characters. `Get-Content`/`Set-Content` reads
in the system codepage and writes UTF-8, which is exactly how eight
lines of this project were mangled before 1.4 — the file still parses
and the tests still pass, and the damage shows only on screen. The swap
was made with an explicit `encoding="utf-8"` and checked byte by byte in
the diff.

---

## 7. Configuration and user data

Unchanged from VDD-1.5 §7. No stored value changes, and no saved nav
order is affected by this release.

---

## 8. External interfaces

Unchanged from VDD-1.5 §8.

---

## 9. Installation and removal

Unchanged from VDD-1.2 §9.

---

## 10. Known limitations

Unchanged from VDD-1.5 §10, including the antivirus note: the durable
remedy remains a code-signing certificate, and nothing has been bought.

---

## 11. Verification performed

In addition to VDD-1.0 §11 through VDD-1.5 §11:

- **The swap was checked as bytes, not as a rendering.** The diff shows
  `"anime"` now holding the sequence `"series"` held and vice versa
  (`U+E714` ↔ `U+E7F4`), with no other line in `theme.py` altered — which
  matters on a file full of private-use characters, where a mangling
  re-encode looks like nothing at all in a review.
- **The keys each glyph drives were confirmed** rather than assumed:
  `main.py` reads `theme.NAV_ICONS` by page key, and `nav_config` maps
  `"anime"` → Anime and `"series"` → Movies & Series.
- **The bundle gate passed**: every file `Atomic.spec` promises in
  `datas` present in the archive and byte-identical to the one on disk.
- **`check_release_notes.py` passed** for 1.6, with one note written.
- The updater was confirmed to resolve `v1.6` against the live
  repository with `APP_VERSION` lowered, at the size and blob hash of the
  committed executable.

Not verified: the folded sidebar has not been photographed at this
version, and the release executable has not been exercised by its owner
against real data before publication. No Defender scan was performed.

---

## 12. Glossary

Unchanged from VDD-1.2 §12.
