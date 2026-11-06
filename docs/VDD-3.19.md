# Atomic — Version Description Document

**Version 3.19** · 6 November 2026

---

## 1. Identification

| | |
|---|---|
| System name | Atomic |
| Version | 3.19 |
| Release date | 2026-11-06 |
| Repository | https://github.com/Khaled-Alneef/Atomic |
| Target platform | Windows 10 / 11, 64-bit |
| Delivered as | `Atomic.zip` — one **GitHub release asset** holding the ~11 MB **bridge installer** as its only `Atomic.exe`, and the app's folder packed as `app.zip` |
| At the tag | `Atomic.exe` / `Atomic.zip` — the bridge, for installs predating 2.0 |
| Asset size | 136,134,340 bytes |
| Asset SHA-256 | `0203b65e8558cbacd6766edafd2d7f6087ba33ab4cc128a9f7f25ed37fec6a38` |
| Bridge SHA-256 | `66355cc7c2a227cf1ba7d8ccf0d0bc322e1bba6ff79a2109d9cce0b6ed305029` (11,180,607 bytes). The asset's bridge is a separate build of the same source; PyInstaller is not byte-identical twice |
| Preceded by | 3.18 — `docs/VDD-3.18.md` |

---

## 2. System overview

Unchanged from 3.18.

---

## 3. Inventory of materials released

Unchanged from 3.18 §3.

**Changes since the last release** (`git log released/3.18..development`):

| Commit | Summary |
|---|---|
| c18e6a0 | Reading cards open their own site's chapters, and 3asq lists them all |
| (this)  | Atomic 3.19 |

---

## 4. What this version provides

**MangaDar's chapters open their own pages**, including for a title saved from
another site - MangaDar's One Piece while One Piece is saved from 3asq.

**3asq lists every chapter again**: One Piece 43 -> 565, Kingdom 619. Chapters
such as 1155.5 (a `1155-5` link) are in the list.

**A reading site that answers slowly stays in searches.** Before, one slow
answer to the site check hid it from every search for six hours.

**Reading cards on Discover's Top 10 and in search results name their site**
instead of a chapter number, so the same title from two sites reads as two
places to read it.

---

## 5. Design notes

**The reader's entry.** `web_reader._entry_id_for` matched a card to the library
by title alone. The details page drew the card's own list (`web_pages._find`
has compared addresses since 3.17) and `reader._web_chapter_index` took the
chapter's index from it, then the route carried the *saved* entry's id. On a
copy of the owner's data: MangaDar chapter 1100, index 117, against 3asq's 43 ->
`no such chapter`; 1198, index 0 -> 3asq's pages. A row at another address is
no match now; afterwards 1100 opens MangaDar's 17 pages and his 3asq card still
opens his saved entry. `backend.pages` logs an index outside its list.

**3asq's paged ajax.** Madara's `ajax/chapters/` POST answers 44 chapters and a
pager (`?t=2` ... `?t=12`); the same POST with `?t=N` answers the rest, `t=13`
on empty. `_more_ajax_pages` fills the numbers in (the pager prints "1 2 3 ...
12"), fetches six at a time, retries each once and logs a page that never
answered. One Piece: 565 chapters in 4.8s, the newest 50 handed over at 2.2s.

**The serving probe.** `discover._serving_sites_only` checks each site with a
full chapter list of its first row inside 8s; for "one piece" on MangaDar that
is 1,221 chapters, 543KB, 2.70s cold on the owner's line (0.63s warm), and a
miss was stored as "cannot serve" for six hours, on disk. A probe that runs out
of time is inconclusive now and not stored; a refusal (Mangalek's 403) still
is. Both are logged. Harnessed with stubbed sites: slow kept and not cached,
refusing dropped and cached, serving kept.

**The site line.** `server._site_card` sets a reading card's meta to its site
(`site` marks it) on Discover's Top 10 and the search's reading and novel rows;
`app.js progressInto` leaves such a line alone, where it used to write his mark
for the title over it.

---

## 6. Configuration and user data

Nothing new is written. A cached 3asq chapter list from before this version is
replaced at its next refresh.

## 7. External interfaces

3asq's `ajax/chapters/?t=N` is asked for pages 2 upward. Otherwise unchanged
from 3.18.

## 8. Installation and removal

Unchanged from 3.18 §8.

---

## 9. Known limitations

Carried forward from 3.18 §9, and:

- **The friend's missing MangaDar row** is fixed on its likely cause, the probe;
  his log was not seen, so it is not proven. An install without MangaDar in its
  site list would also show none.
- **3asq's own listing has gaps** (One Piece jumps from 722 to 349); those
  chapters are not on the site's list and are not in the app's.
- **The test window took the foreground at launch** twice on this machine while
  the owner was using it, and the guard ended it; nothing in this version was
  photographed on the frozen build.

---

## 10. Verification performed

**The owner tested the build and asked for this release.**

**In-process, on copies of his data:** the reader's id before and after (above);
3asq One Piece, Kingdom and Black Clover lists, and pages of chapters 700, 320
and 1155.5; the search route's Manga row reading "MangaDar" and "3asq" under the
two One Pieces; Discover's Top 10 Manga and Manhwa naming their sites; the probe
with stubbed sites.

**The release build, read back:** "3.19" in `helpers.updater` and
`helpers.development_version_patch`, no "3.18"; `_page_address` in
`windows.web_reader`, `_more_ajax_pages`/`_ajax_page_numbers` in
`helpers.chapter_source`, the probe's log line in `helpers.discover`,
`_site_card` in `web.server`, the reader's log line in `web.backend`, `psite`
in `app.js`; the 3.19 notes present. The app folder's exe inside `app.zip` is
byte-identical to the one built.

**Not exercised:** screenshots of the frozen build (above), the friend's
machine, a real update through GitHub before publishing.

**Defender**, service Running: exit 0 on the app `Atomic.exe`, the bridge
`Atomic.exe` and `Atomic.zip`, twice each, full paths.

**The release-notes gate:** 5 notes written for 3.19.

---

## 11. Glossary

Unchanged from 3.18 §11.
