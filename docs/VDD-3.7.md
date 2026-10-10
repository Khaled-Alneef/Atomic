# Atomic — Version Description Document

**Version 3.7** · 10 October 2026

---

## 1. Identification

| | |
|---|---|
| System name | Atomic |
| Version | 3.7 |
| Release date | 2026-10-10 |
| Repository | https://github.com/Khaled-Alneef/Atomic |
| Target platform | Windows 10 / 11, 64-bit |
| Delivered as | `Atomic.zip` — one **GitHub release asset** holding the ~11 MB **bridge installer** as its only `Atomic.exe` and the application **as a folder**, packed as `app.zip` |
| At the tag | `Atomic.exe` / `Atomic.zip` — the same bridge, for installs predating 2.0 |
| Asset size | 136,752,117 bytes |
| Asset SHA-256 | `3312e67fca753c2f258d6be2199dbe890bcd2538f53e91de624d2e58f817b20a` |
| Bridge SHA-256 | `18587e65bd7f80fda6531ae005e6880e329978d2e2bf05bd33e274f64d902b21` (11,179,784 bytes) |
| Preceded by | 3.6 — `docs/VDD-3.6.md` |

---

## 2. System overview

Unchanged from 3.6, plus a seventh reading medium: **Novels**, a sidebar
section over four text sites - two Arabic, two English - read as text in
the in-app reader.

---

## 3. Inventory of materials released

Unchanged from 3.6 §3, plus `src/helpers/novel_sites.py` and
`src/assets/icons/novels.svg` (listed in `Atomic.spec`'s datas).

**Changes since the last release** (`git log released/3.6..development`):

| Commit | Summary |
|---|---|
| `3b41a3a` | Novels: a sidebar section over four text sites, two Arabic, two English |
| `333b3d6` | Atomic 3.7 |

---

## 4. What this version provides

**Novels.** A sidebar row under Manhua (`manga:cat_novels`) opens a
catalogue of the four sites' popular lists, interleaved so both
languages are on the first screen, each card naming its site. Sources:
kolnovel.com and rewayat.club (Arabic), ReadNovelFull and Novel Fire
(English). Search gains a Novels section; Discover's Top 10 Other
Readings row is replaced by Top 10 Novels.

**The text reader.** A novel's chapter is drawn as a column of text in
the site's direction (right to left for the Arabic two), with a font
picker in the bar (faces that ship with Windows, remembered separately
for Arabic and English) and a size in px on the −/+ buttons. The next
chapter is fetched behind the one on screen.

**A novel's details page** shows its own summary, genres (in English for
Kolnovel's 154; Rewayat names its own) that open that site's list of the
genre, LANGUAGE and SOURCE. Continue opens chapter 1 or the chapter after
the furthest read. Saving, History, Home's Reading row and the read
marks are the reading ones.

**For every reading medium**: Previous Chapter / Next Chapter at the end
of each chapter; the site under each card's title; LANGUAGE (Arabic or
English) above SOURCE; an Arabic title set from the left.

**Discover** no longer has a Cast row. The rail's Novels book tips left
as a bookmark slips out on hover.

---

## 5. Design notes

**Which sites, by measurement.** Every candidate was fetched through
`helpers/net` from the owner's connection. RoyalRoad, ScribbleHub,
Webnovel, FreeWebNovel, NovelUpdates, AllNovel, NovelHall, NovelFull.net,
LightNovelPub and NovelLive answer `403 cf-mitigated: challenge`;
sunovels and novelbuddy search in the browser; riwyat's search returns
its home page; NovelCool's 2.1MB page held 2,503 of 3,210 chapters.

**Kolnovel's decoys.** Each chapter carries copies of other paragraphs
under random classes its own `<style>` hides (opacity 0, 0.1px height,
-99999px indent) - 9 of 18 classes on the chapter measured. The hidden
set is read out of each page's CSS and those paragraphs dropped.

**Chapters by position.** No site's numbers are an identity (kolnovel's
are volume-relative, rewayat lists announcement posts as chapters
123321213 and 999888 - dropped above 100,000). The site's own words are
the row's label.

**A novel never asks a catalogue.** MangaDex and AniList answer for its
manga: no release schedule (`release_schedule.needs_refresh`), no
catalogue cover (`/api/cover`), no MangaDex genres or TMDB logo on its
page.

**Novel Fire.** Its list is 100 a page (9-14s for 3,000 chapters), so the
whole numbered list is stored from the first page and titles fill in;
its covers refuse urllib's User-Agent, so novel covers go through the
proxy with a browser one.

---

## 6. Configuration and user data

New: `novels_cache.json` (the catalogue's first page, warmed at launch
when older than 6h). Reader preferences in the page's localStorage:
`atomic.reader.textzoom`, `atomic.reader.font.ar`,
`atomic.reader.font.en`. Novel entries are saved to `tracker.json` with
type `Novel`; their history keys are `read:<title>`.

## 7. External interfaces

New: kolnovel.com, rewayat.club (+ api.rewayat.club, media.rewayat.club),
readnovelfull.com (+ img.readnovelfull.com), novelfire.net. All keyless.

## 8. Installation and removal

Unchanged from 3.6 §8.

---

## 9. Known limitations

Carried forward from 3.6 §9. Novels: no downloads (chapter downloads are
page images), no genre filter on the catalogue, not in the search
field's live suggestions, no release dates. With "hide entry names" on,
chapter rows read "Chapter N" by position, which can differ from a
site's own numbering. Continue on an unread *manga* still opens its
newest chapter (unchanged; novels were changed).

---

## 10. Verification performed

**Sites and module, live**: browse, search, details, chapter list and
chapter text on all four (§5 numbers); the decoy filter confirmed on
Kolnovel (a repeated line left is a real triple sound effect).

**Routes in process, on a copy of his data**: the Novels page drawn from
cache in 24ms (88 rows, every one with a cover), scroll paging, search's
Novels section (40 rows), a novel's cover never borrowed, all four sites
listing, serving text in the right direction and recording read marks;
a save landing in Saved/Read and Home's Reading row as "Ch 1".

**Frozen build, photographed** (first round): the rail row and icon;
the Novels page (route drawn 36-49ms, 88 rows) before and after the
cover fix; a Kolnovel and a Novel Fire details page; the Arabic and the
English text reader; Continue opening chapter 1 at 1.1s on an uncached
Novel Fire title and Next at 0.5s from memory; search with its Novels
section.

**Not measured - shipped at the owner's word without a test pass**
("make it fast and do not test it I will"): the wider text column, the
font picker and px size, the bar holding while a picker is open,
end-of-chapter Previous/Next, reading cards naming their site, LANGUAGE,
Kolnovel's English genres and the novel genre pages, the left-set Arabic
title, Top 10 Novels on Discover, the Cast row's removal and the rail
icon's tilt and bookmark.

**The release build, read back out of its own archive**: "3.7" in
`helpers.updater` and `helpers.development_version_patch`; `genre_page`
in `helpers.novel_sites`. Bridge SELFTEST OK.

**Defender**, service Running: exit 0 on the app `Atomic.exe`, the bridge
`Atomic.exe` and `Atomic.zip`.

**The release-notes gate:** six notes written for 3.7.

---

## 11. Glossary

Unchanged from 3.6 §11.
