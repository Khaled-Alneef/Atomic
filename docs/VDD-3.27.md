# Atomic — Version Description Document

**Version 3.27** · 19 November 2026

---

## 1. Identification

| | |
|---|---|
| System name | Atomic |
| Version | 3.27 |
| Release date | 2026-11-19 |
| Repository | https://github.com/Khaled-Alneef/Atomic |
| Target platform | Windows 10 / 11, 64-bit |
| Delivered as | `Atomic.zip` — one **GitHub release asset** holding the ~11 MB **bridge installer** as its only `Atomic.exe`, and the app's folder packed as `app.zip` |
| At the tag | `Atomic.exe` / `Atomic.zip` — the bridge, for installs predating 2.0 |
| Asset size | 135,872,184 bytes |
| Asset SHA-256 | `5eee42db714c2f3fa3a194f1f259e0918ab50fa5bb8e1619e09889445a8bfdcf` |
| App exe SHA-256 | `c5bb05a386b4cae46a9b8a06f5967d6c4d53e44050cb6e6c5f5eb33170156ef2` (`app\Atomic.exe`, 8,457,572 bytes) |
| Bridge SHA-256 | `a5ca0f36203ba6e1ff4e706a20a1d99dc912553915b4327918bdba194c5f5cbb` (11,179,947 bytes, at the tag). The asset's bridge (11,178,097 bytes) is a separate build of the same source; PyInstaller is not byte-identical twice |
| Preceded by | 3.26 — `docs/VDD-3.26.md` |

---

## 2. System overview

Unchanged from 3.26.

---

## 3. Inventory of materials released

As 3.26 §3. No module added or removed.

**Changes since the last release** (`git log released/3.26..development`):

| Commit | Summary |
|---|---|
| 2b594fe | The reader's top bar holds over its drag region (3.26.1) |
| 8cfc682 | Kolnovel lists its chapters again; Novel Fire reads after its list |
| f6bff78 | Atomic 3.27 |

---

## 4. What this version provides

**Kolnovel works again.** Every title had listed no chapters since the site's
redesign; the list is back, shown whole in about a second however long the
novel, and filled in behind. Search finds titles again, and a novel's page
shows its own cover, genres and synopsis instead of the site's logo and
slogan.

**Novel Fire chapters open right after their list.** A chapter opened just
after its chapter list loaded, or the read-ahead of the next one, was refused
by the site.

**The reader's top bar holds still.** It no longer slides away and back while
the pointer rests on its empty part (a report from another machine), and the
word count stays fully in view when the bar is folded.

---

## 5. Design notes

**Kolnovel's chapter list.** The new theme has no `eplister`; the series page
shows the newest 24 and the rest is `?chapter_volume=__kol_all__&
chapter_order=oldest&chapter_page=N`, with the page count on the volume's
`<details>`. No per-page setting is honoured, `app/v2` has no chapter route
and `wp/v2` answers 403, so the pages are read — 218 of them for Martial God
Asura, 23.95 s whole. Oldest-first pages are fixed, so page 1 and the last
page give the exact count and each unread chapter is a placeholder, "row r of
page p", which `_kol_resolve` turns into its address on demand. That complete
list is handed over at once (the reader opens chapters by position in the
stored list, so a list with gaps would open the wrong one); the other pages
follow newest first, eight at a time, each retried once.

**Kolnovel's details and search.** Genres are plain `/genre/` links in
`kol-series-editorial__genres`, the synopsis is `kol-series-editorial__prose`,
the cover is the image in `kol-series-editorial__cover`. Under the new theme
`og:image` and `og:description` are the site's logo and slogan on every page,
so they are never a fallback there. Search cards name the title in `<h3>`.
The old theme's parsing stays as a fallback.

**Novel Fire's 429.** The chapter list is 33 of novelfire.net's pages, after
which novelfire.net refuses for about ten seconds (Retry-After 8 down to 0).
A chapter's text lives on the mirror its address hands off to, another host
with its own limit, so `_nf_text` asks the mirror first (learned from the
last hand-off, seeded with novelphoenix.com) and runs the hand-off chain only
when the mirror has nothing.

**The reader's bar.** It was shown by `:hover` alone over a WebView2
non-client drag region, which is hit-tested as the window's caption, so on
some runtimes the page lost the pointer there and the bar oscillated. It now
follows the pointer's position (`readerBarMove`), and a leave out of the page
changes nothing. The log names the WebView2 runtime and says "reader bar held"
when that leave happens.

---

## 6. Configuration and user data

Unchanged from 3.26.

## 7. External interfaces

kolnovel.com: the series page's paginated chapter list
(`chapter_volume`, `chapter_order`, `chapter_page`), its editorial details
blocks and its new search cards. Novel Fire: chapter text from the mirror
host directly. No new service.

## 8. Installation and removal

Unchanged from 3.26 §8.

---

## 9. Known limitations

Carried forward from 3.26 §9, and:

- **A long Kolnovel list is complete only after every page is read** — 13.6 s
  for 3,835 chapters on the frozen build, about 24 s for 5,211 — on its first
  open, on a refresh press, and when Home rechecks it every 6 hours. The list
  is usable throughout.
- **Kolnovel numbers chapters by position**, so a site that numbers "فصل
  3800" can sit at position 3,835 where it has extras.
- **The Novel Fire mirror is learned from the hand-off**; if the seeded one
  goes away, the first chapter after a list can meet the 429 again until a
  hand-off teaches the new one.
- **The reader-bar stutter never reproduced here** (runtime 157); the fix was
  measured with posted pointer moves, and the friend's machine is the real
  test.

---

## 10. Verification performed

**Kolnovel list:** four titles through the real code, the early list at
0.81–1.25 s (5,211 rows at 1.06 s) and the full one at 1.72–22.90 s; every real
row at the same position in both; 24 of 24 sampled placeholders resolving to
the full list's chapter and reading in 0.78–0.95 s. Frozen build, a copy of
his data: 3,835 chapters on screen at 1.5 s, complete at 13.6 s, genres and
synopsis shown, the newest chapter read (38 paragraphs, 0.5 s).

**Kolnovel search and details:** four queries (11 rows for "ملك", every row
with a cover), genres, synopsis and cover on three titles; none of his saved
data held the logo or slogan.

**Novel Fire:** straight after the list on two titles, the newest, second and
first chapters read in 0.16–0.32 s; text identical to the hand-off chain's in
6 of 6. Frozen build: a refreshed list, then the newest chapter 0.9 s later,
67 paragraphs in 471 ms.

**ReadNovelFull and Rewayat:** search, browse, details, chapter lists (3,252
and 1,438) and old and new chapter text, every request under 1.2 s.

**Reader bar:** frozen build, posted pointer moves (which make the page lose
the pointer at once): the bar shown at the top, on its drag area and in the
reach strip, hidden in the text; the old files in the same build never showed
it (7 of 7 frames). The word count tracked frame by frame, rows 11–19 folded
to 53–61 open in 0.19 s.

**Release build:** Defender clean on the app exe, the bridge and the asset;
the archive read back for 3.27 in both version places, the 3.27 notes, and
the novel-site code.

**Not exercised:** opening a placeholder chapter in the frozen app while its
list was still filling (proven in the harness only); the reader bar at his
125% scale and on the friend's runtime.
