# Atomic — Version Description Document

**Version 3.5** · 5 October 2026

---

## 1. Identification

| | |
|---|---|
| System name | Atomic |
| Version | 3.5 |
| Release date | 2026-10-05 |
| Repository | https://github.com/Khaled-Alneef/Atomic |
| Target platform | Windows 10 / 11, 64-bit |
| Delivered as | `Atomic.zip` — one **GitHub release asset** holding the ~11 MB **bridge installer** as its only `Atomic.exe` and the application **as a folder**, packed as `app.zip` |
| At the tag | `Atomic.exe` / `Atomic.zip` — the same bridge, for installs predating 2.0 |
| Asset size | 136,713,246 bytes |
| Asset SHA-256 | `e8990433818cb7e23bf78bf281a89c9377bf2ef78092ebc7158c16603de3bd73` |
| Bridge SHA-256 | `6091e3bd307c0f040ce53c5d01c60709e3ba72e49511df04bc112867f3274122` (11,180,313 bytes) |
| Preceded by | 3.4 — `docs/VDD-3.4.md` |

---

## 2. System overview

Unchanged from 3.4. Episode lists gain a second source for what Cinemeta
has not filed yet, and keep themselves current; the title page's Save
button is restyled; the window reopens where it was last used.

---

## 3. Inventory of materials released

Unchanged from 3.4 §3.

**Changes since the last release** (`git log released/3.4..development`):

| Commit | Summary |
|---|---|
| `141896b` | The window reopens on the monitor it was last on |
| `f1d9145` | The Save to My List button wears the redesign's faces |
| `70678e1` | New seasons and episodes appear before Cinemeta files them |
| `232cf92` | Atomic 3.5 |

---

## 4. What this version provides

**New seasons and episodes before Cinemeta has them.** On 5 October
2026 Black Clover's second season had aired (AniList: releasing since 3
October; TMDB: Season 2, E1 aired) while Cinemeta - the source of every
episode list in the app - still said "Ended 2017-2021", Season 1 only.
`stremio.fill_from_tmdb` now adds TMDB's episodes past the end of
Cinemeta's list, and puts TMDB's date on Cinemeta's undated rows for an
announced season so it reads UPCOMING, only where the two sources number
the show alike. A row Cinemeta has always wins, and once Cinemeta catches
up nothing is added.

**Lists keep themselves current.** Home's draw refreshes every saved and
recently watched show's list in the background once it is six hours old,
and the page redraws only when a list changed. A title page draws
Cinemeta's list the moment it answers and asks TMDB again on every open.

**Save to My List** takes the redesign's three faces: white Play when
unsaved, grey secondary when saved, red with a bin under the pointer.

**The window reopens on the monitor it was last on**, whether it was
closed restored, maximised or full screen.

---

## 5. Design notes

**When TMDB may speak for a show.** The newest Cinemeta seasons must be
TMDB's: same number, first episode within two days, at least as many
episodes, and Cinemeta's last dated episode on TMDB's episode of the
same number and day (Arafta: S2 aligned on all else while TMDB's E27
aired seven weeks before Cinemeta's E26 - refused). A show TMDB numbers
absolutely is lined up on the dates of its last two aired episodes and
continued one number at a time until a four-week break or a date that
runs backwards. Nothing older than a year is added - revivals IMDb files
as their own titles (Twin Peaks 2017, The Kingdom 2022) are not a lag.
Every writer of a meta file keeps a fill already on disk, so a reader
that does not ask TMDB never takes a list back to Cinemeta's.

---

## 6. Configuration and user data

No new settings. The `meta-series-<id>.json` files may now carry rows
marked `"source": "tmdb"` and dates marked `"dated_by": "tmdb"`; both
are recomputed on every refresh.

## 7. External interfaces

TMDB (`api.themoviedb.org`) gains two uses through the existing bundled
token: `/tv/{id}` and `/tv/{id}/season/{n}`. Unreachable TMDB leaves
every list as it was.

## 8. Installation and removal

Unchanged from 3.4 §8.

---

## 9. Known limitations

Carried forward from 3.4 §9. A season TMDB itself does not list cannot
appear. A show TMDB numbers absolutely is continued only up to a break
of more than four weeks; a new part after that waits for Cinemeta. An
added episode can be listed before any source carries it (Tyler Perry's
Assisted Living S7E1: no sources on 5 October). Titles never opened or
watched are refreshed only when opened.

---

## 10. Verification performed

**Census, live, over the 921 series lists on the owner's disk:** 16
titles gain rows or dates, 905 unchanged; Jujutsu Kaisen, Bleach,
Bleach TYBW and Twin Peaks byte-identical. Sources answer for the added
episodes - Black Clover S02E01 46 rows; Doraemon's continuation matches
release numbering (S22E28 = 923, E29 = 924, aired on the dated day).

**Harnessed, source tree against a copy of his data:** an upcoming
season dated and no longer hidden; a writer without TMDB keeping the
fill; a TMDB outage keeping it; Home's draw refreshing a stale list in
1.2s and moving the card from S01E170 to S02E01 with one redraw; an
unsaved title's first rows at 0.55s (was 0.94s), TMDB asked on every
open and not by the player's cached reads.

**The frozen build, against a copy of his data:** Black Clover's page
listing Season 2 (E1 Oct 3, E2 onward UPCOMING); High Potential's
Season 3 reading UPCOMING Jan 6, 2027; Home refreshing Black Clover's
list at launch with no page opened; the Save button photographed in all
three faces.

**The release build, read back out of its own archive**: "3.5" in
`helpers.development_version_patch` and `helpers.whats_new`;
`fill_from_tmdb` in `helpers.stremio`, `_refresh_episode_lists` in
`web.server`. `build.py`: 1,608 entries, 32 bundled files
byte-identical to `src/`. Bridge SELFTEST OK.

**Defender**, service Running: exit 0 on the app `Atomic.exe`, the bridge
`Atomic.exe` and `Atomic.zip`.

**The release-notes gate:** three notes written for 3.5.

**Not measured.** The player stepping from a season's last episode into
a TMDB-filled season on the frozen build.

---

## 11. Glossary

Unchanged from 3.4 §11.
