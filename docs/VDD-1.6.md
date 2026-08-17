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
| `Atomic.exe` | The application. 47,764,713 bytes. SHA-256 `5ed579a7377ef91c039b6aec4fa7cc13331c05f6db9ffcdf47da673f3a397313` |
| `src/` | Full Python source, 15,253 lines across 37 modules (15,230 at 1.5) |
| `packaging/` | `build.py`, `Atomic.spec` and `check_release_notes.py` |
| `docs/VDD-1.6.md` | This document |

Build environment unchanged from VDD-1.1 §3.

No development builds stand between 1.5 and this release: 1.6 is two
changes taken straight off `development`, released the same day as 1.5.
It was re-cut twice after first being tagged, before anyone had it:
once for the Escape and keybind-wording work in §5, and once for the
empty-box half of the Escape fix that the first of those missed. The
size and hash above are the last binary's.

---

## 4. Inventory of software contents

| Module changed | Lines | What changed |
|---|---|---|
| `src/main.py` | 1,223 | Escape leaves a page's search box, empty or not |
| `windows/home.py` | 886 | Escape leaves the global search field, focus included |
| `helpers/theme.py` | 761 | The Anime and Movies & Series nav glyphs swapped |
| `helpers/whats_new.py` | 314 | 1.6's notes |
| `helpers/updater.py` | 272 | `APP_VERSION` |
| `helpers/global_search.py` | 248 | The shortcut list's wording and capitals |

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

### Escape leaves a search box, rather than only emptying it

Escape in Home's global search field cleared the text, closed the
results and left the caret blinking in a box the user had just said they
were done with. Measured before it was changed: focus stayed on the
`QLineEdit`, while the same key on a tracker page already dropped focus
onto the page. The two paths were written apart, and only one of them
finished the job; Home now ends the same way — clear, close, drop focus.

The keybind list says so: the Esc row reads **Exit search** rather than
"clear a search", which is what the key now does.

A page's own search box needed the same fix a second time, and this is
the more interesting half. The window's handler tested `box.text()`, so
an empty box matched nothing and Escape fell through to the full-screen
branch with the caret still in it — the case you land in after clearing
by hand. It tests *focused or holding text* now. The text half stays on
purpose: a query still narrowing the grid is worth escaping from after
the focus has moved onto a card, so keying on focus alone would have
traded one gap for another.

### The keybind descriptions start with capitals

Every row under Settings → Keybinds now reads "Search everything",
"Undo the last action", "Exit search". They were the only
lowercase-first user-facing strings in the app: a scan over every string
literal in `src/` turned up three placeholders
(`https://example.com/`, `e.g. G:\{label}`) and mid-sentence fragments,
none of which should change — so nothing else did.

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
- **The Escape behaviour was measured over six cases** — a tracker page,
  the Games page and Home's global bar, each empty and with text — in a
  real window with real key events, asserting on *focus* rather than on
  the field being empty, since the text was already being cleared and
  that is precisely why the defect survived being "fixed" once.
- **The Escape fix was run with a control**: with it removed, the harness
  reproduces exactly the two reported failures (a tracker page and Games
  with an empty box) while Home passes both, which is why the defect
  looked intermittent from the outside. A check that cannot fail proves
  nothing, and this one was made to fail first.
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
