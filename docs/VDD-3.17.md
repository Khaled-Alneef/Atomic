# Atomic — Version Description Document

**Version 3.17** · 5 November 2026

---

## 1. Identification

| | |
|---|---|
| System name | Atomic |
| Version | 3.17 |
| Release date | 2026-11-05 |
| Repository | https://github.com/Khaled-Alneef/Atomic |
| Target platform | Windows 10 / 11, 64-bit |
| Delivered as | `Atomic.zip` — one **GitHub release asset** holding the ~11 MB **bridge installer** as its only `Atomic.exe`, and the app's folder packed as `app.zip` |
| At the tag | `Atomic.exe` / `Atomic.zip` — the bridge, for installs predating 2.0 |
| Asset size | 136,124,161 bytes |
| Asset SHA-256 | `f582b3877884ecc1d2f449721a5090750f0af137e0d165026877889ccd6416d3` |
| Bridge SHA-256 | `b3c5ed15baefb25d2625683e23844e87a7d22959dfe86f99e40da1207e772492` (11,180,602 bytes). The asset's bridge is a separate build of the same source (11,180,275 bytes); PyInstaller is not byte-identical twice |
| Preceded by | 3.16 — `docs/VDD-3.16.md` |

---

## 2. System overview

Unchanged from 3.16.

---

## 3. Inventory of materials released

Unchanged from 3.16 §3.

**Changes since the last release** (`git log released/3.16..development`):

| Commit | Summary |
|---|---|
| fd4c56c | The head-first resume path is gone |
| 90ba362 | The unreachable half of regression_fixes_146 is gone |
| 858c647 | Testing stays off his monitor and never takes the focus |
| 69e62f5 | Atomic 3.17 - MangaDar, search rows by medium, list search, read at the end |

---

## 4. What this version provides

**MangaDar is a reading source**, for manga, manhwa and manhua, added to existing
installs once.

**Search draws Manga, Manhwa, Manhua and Novels as separate rows.** A title with
no known medium goes under "Other Readings".

**The search box in the episode and chapter lists types**, and Escape and R work
on that page without a click.

**A chapter is read when its end is reached**, not when it opens.

**A reading card opens the site it names**, whichever site History last used.

**A reading site with nothing for one search stays in the next one.**

---

## 5. Design notes

**The list search.** A press on a web page's card left the Win32 focus on its
`Chrome_WidgetWin_1`, and `overlay_opened` parked that window off screen with the
focus in it. Read with `GetGUIThreadInfo`: a key sent there vanished, and the same
key posted to the Qt window typed and filtered. `open_details` takes the keyboard
and `_close_now` returns it; a press on any `search_field` takes it as well.

**MangaDar is its REST API and only that** (`manga_sites._is_mangadar`). None of
the four engines fits it. Search 0.12-0.46s, browse 59 rows 0.51s, Kingdom's 939
chapters 0.43s, 20 pages 0.15s. Page urls are signed, so they are asked for on
each open. `light-novel` rows are left out.

**The medium in a search.** `server._with_medium`: verdicts on disk answered 22
of 32 rows at once, the rest took 0-1.48s, bounded at 4s. A site's own label and
then his library fill in for a title no catalogue names.

**A 404 is not a refusal.** Two on `/wp-admin/admin-ajax.php` put a host on the
refusing list for ten minutes: one "one piece" lost TeamX, SWAT and Lava Scans,
and the next "kingdom" drew MangaDar alone. 400/404/405/410 no longer count.
`net.note_host_failure` logs the moment a host goes on the list.

**The read mark.** `app.js` `checkEnd`: the end row on screen and the last page
settled. Black Clover 392, 20 pages: no mark on open, at page 0 or at page 16;
`c392` once the end row came up. `/api/opened` keeps the title moving up History.

**A faster reading search was built and reverted the same day**, at the owner's
word: it measured faster by the route's clock and read no faster on his machine.
None of it ships. `.claude/rules/integrations.md` has the numbers.

---

## 6. Configuration and user data

`manga_sites.json` gains MangaDar once (`manga_sites_added.json` records it, so a
site he removes stays removed). Nothing else new is written.

## 7. External interfaces

New: `mangadar.com/wp-json/mangadar/v1`, keyless. Otherwise unchanged from 3.16.

## 8. Installation and removal

Unchanged from 3.16 §8.

---

## 9. Known limitations

Carried forward from 3.16 §9, except the head-first path, which is gone.

- 3asq's Black Clover is two short listings, 18 and 30 chapters. A card stays on
  its own site, at the owner's word.
- Some 3asq search cards draw no cover: its art is a 1x1 placeholder and the
  fallback answered a web page rather than an image.
- Marks written under the old rule stay; nothing says which were finished.
- The Qt reader, which runs only without WebView2, still marks on open.

---

## 10. Verification performed

**The owner tested the builds and asked for this release.**

**On the frozen build, a copy of his data, on the 1080p monitor with his own
Atomic open and untouched:** the list search on Reacher ("5") and Kingdom ("893");
the search rows photographed on "kingdom" and "lookism"; MangaDar's Black Clover
listing 399 chapters; chapter 392 through to its end with the mark read from
`history.json` at each step; MangaDar's Kingdom 893 in the reader.

**The release build, run after the revert:** the 3.17 notes dialog photographed;
its search route answered kingdom, black clover and lookism in the right rows.

**The release build, read back:** "3.17" in `helpers.updater` and
`helpers.development_version_patch`, no "3.16.3"; `_search_mangadar`,
`_WRONG_SHAPE_CODES`, `_with_medium`, `_page_address`, `chapter_opened`,
`keyboard_back` present; `_recheck_engine`, `preconnect`, `serving_sites`,
`FIRST_COMPLETED` absent; `app.js` inside `app.zip` reads `SEARCH_PULL_MS = 650`.
The app folder's exe inside `app.zip` is byte-identical to the one tested.

**Not exercised:** a real keyboard and mouse (CLAUDE.md rule 14), his 125% panel,
a locked MangaDar chapter, a novel's end, a real update through GitHub.

**Defender**, service Running: exit 0 on the app `Atomic.exe`, the bridge
`Atomic.exe` and `Atomic.zip`, twice each. A relative path fails the scan with
`0x80508023` and exit 2, which is not a detection; pass a full path.

**The release-notes gate:** 6 notes written for 3.17.

---

## 11. Glossary

Unchanged from 3.16 §11.
