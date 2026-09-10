# Atomic — Version Description Document

**Version 2.1** · 10 September 2026

---

## 1. Identification

| | |
|---|---|
| System name | Atomic |
| Version | 2.1 |
| Release date | 2026-09-10 |
| Repository | https://github.com/Khaled-Alneef/Atomic |
| Target platform | Windows 10 / 11, 64-bit |
| Delivered as | `Atomic.zip` — a **GitHub release asset**, holding one self-contained `Atomic.exe` |
| Also at the tag | `Atomic.exe` / `Atomic.zip` — the ~11 MB **bridge installer**, for installs predating 2.0 (§3, §8) |

---

## 2. System overview

Unchanged from 2.0 — see `docs/VDD-2.0.md` §2. A single-window Windows
desktop application over one person's anime, reading, films, series,
games, applications and websites, with a native video player (libmpv)
over its own torrent engine (libtorrent) and debrid support, a chapter
reader, a download queue, and a locally served web UI (WebView2).

**2.1 is one change**, on the video player's episode list.

---

## 3. Inventory of materials released

| Item | Description |
|---|---|
| `Atomic.zip` (release asset) | The application. **125,512,117 bytes**. SHA-256 `25945d3a34b46477ba9e1a805ad97bcc7e9a9f2d90dfe143cc6e42ecb86b8084` |
| `Atomic.exe` (inside that zip) | **126,289,755 bytes**. SHA-256 `b33e07c4fc99d2d282ae37dbe24c114623790aadd503689790c5a1a69906f7fa` |
| `Atomic.exe` (committed at `v2.1`) | The **bridge installer**, 11,098,866 bytes. SHA-256 `a3feb45e364a937f8f52f345bf383318b045cb87891b4b3a4f77455c3781359f` |
| `Atomic.zip` (committed at `v2.1`) | The same bridge installer, zipped, for a zip-preferring updater |
| `src/` | Full source, **125,635 Python lines** across 126 modules, plus 5,917 lines of served static UI |
| `packaging/` | `build.py`, `Atomic.spec`, `check_release_notes.py`, `fetch_libmpv.py`, and `bridge/` |
| `docs/VDD-2.1.md` | This document |

**One commit** stands between `released/2.0` and this release. 2.0's tag
was re-cut onto the tip of `development`, so everything that was on that
branch shipped in 2.0; 2.1 carries this change and nothing else.

The bridge is unchanged in purpose and rebuilt from the same source: it
resolves the newest release carrying an `Atomic.zip` asset **at run
time**, walking `/releases`, so the copy committed at `v2.1` will find
2.2 and everything after it without being rebuilt.

---

## 4. What this version provides

**Every episode in the player's episode list carries its rating**, on
the line under its title, beside the air date:

    Episode 5
    Aug 22, 2026   ·   IMDb 9.7

This is the same column the title page's episode list already carried,
and deliberately the same answer, from the same code:
`helpers/ratings.py`.

- **One source for a whole season, never mixed within a list.**
  Cinemeta's numbers are IMDb's; TMDB's differ by 0.4-0.6 on the same
  episode, so a column carrying some of each would compare unlike
  things. Cinemeta wins the season when it actually rated most of it
  (the share test is `ratings.cinemeta_scores`, 0.6); otherwise TMDB's
  are used and the column says `TMDB`.
- **An unrated episode shows nothing** — not a zero. `"0"` is Cinemeta's
  way of saying unrated, and an episode that has not aired has no rating
  at all: those rows keep the date alone.
- **The label names whose number it is.** A TMDB number is never printed
  as IMDb's.

Why the second source exists at all is measured in `helpers/ratings.py`:
Cinemeta rates western live action and almost none of the anime tracked
here — 0 of 89 episodes on Attack on Titan, 0 of 63 on Demon Slayer,
against 67 of 67 on Breaking Bad.

---

## 5. Design notes

**Nothing new was built for this.** The lookup, its disk index, the
air-date matching that keeps TMDB's seasons from being printed onto
Cinemeta's, and the week-long cache are all `helpers/ratings.py`, which
the title page has used since it was written. What 2.1 adds is the
player reading them, and one function moved so that both lists share it
rather than carrying a copy each (`ratings.cinemeta_scores`).

**The panel never opens a socket to draw.** `cached_episode_ratings`
answers from the disk index the title page usually filled a moment
earlier; only a miss asks the network, on a worker, **once per (series,
season) for the life of the page**. The panel rebuilds on every episode
change and on every keystroke in its search box, so this matters: the
lookup is read once per rebuild and memoised.

**A failure costs the column and nothing else.** The worker's whole body
is wrapped; `ratings.episode_ratings` already fails soft to `{}`; an
empty answer leaves whatever Cinemeta had on screen; an answer arriving
for a season the panel has been flipped away from is kept but does not
redraw it.

---

## 6. Configuration and user data

Unchanged. No new file, no new field, no migration. The rating index in
`%APPDATA%\Atomic\rating_cache` is the one the title page already wrote.

Settings > Watching > **"Show episode and chapter numbers only"** still
hides episode names in this panel; the rating sits on the date line, not
the title, so the two do not interact.

---

## 7. External interfaces

Unchanged from 2.0 (§8 there). This release adds no host: TMDB is
already asked for artwork and for the title page's ratings, under the
bundled key.

---

## 8. Installation and removal

Unchanged — `docs/VDD-2.0.md` §9. An install running 2.0 updates in
place from the release asset; an install running 1.10 or older is handed
the bridge committed at the tag, which fetches the asset.

---

## 9. Known limitations

Carried forward from 2.0 §10. Nothing on that list was fixed or added to
by this release. Two worth repeating here:

- **A rating is only as current as the week-long cache**, and a season
  airing now picks up its first votes as it goes. An episode aired hours
  ago will usually show nothing until TMDB has votes on it.
- Code signing (roadmap #8) is still open; the release is unsigned, and
  shipping the zip rather than the bare exe is what makes the download
  come through.

---

## 10. Verification performed

Method: `.claude/rules/testing.md` — measure first, fix, read the
archive back, drive the frozen exe from outside, photograph it.

**The refactor changed nothing.** The season-source test moved out of
`details._season_ratings` into `ratings.cinemeta_scores`. Run over every
season of every Cinemeta record cached on this machine — **3,362 seasons
across 907 titles** — the old code and the new agree on both the scores
and the verdict: **0 differences**.

**The panel, driven by its own real methods** (offscreen, on a copy of
the owner's data, `h_player_ratings.py`), over his three tracked series:

| Title | Season | Source | Rows rated | Lookups fired |
|---|---|---|---|---|
| The Angel Next Door Spoils Me Rotten | 1 | TMDB | 12 of 12 | 0 |
| Bleach: Thousand-Year Blood War | 1 | IMDb | 13 of 13 | 0 |
| Reacher | 1 | TMDB | 8 of 8 | 0 |

Zero, because the title page had already filled the index. Five further
refills of each fired none, and three season flips fired none.

**Cold, with no index on disk** (`h_cold.py`): the panel draws at once
with the date alone and fires **exactly one** lookup; six refills while
that is in the air fire none; the answer landing redraws the rows with
`TMDB 8.4`, and an episode the answer had nothing for keeps its date
alone. An answer for season 1 arriving while season 2 is on screen
leaves it alone. A worker made to raise reports `{}` and the panel still
draws its 12 rows. A title with no Cinemeta rows at all still draws the
plain numbered list.

**Cost to the refill** (`h_cost.py`, 25 runs each, against a control
with the column switched off):

| Title | Rows | Lookup | Refill before | Refill after |
|---|---|---|---|---|
| The Angel Next Door | 12 | 0.002 ms | 6.2 ms | 6.5 ms |
| Bleach TYBW | 13 | 0.002 ms | 6.7 ms | 6.8 ms |
| Reacher | 8 | 0.002 ms | 4.3 ms | 4.2 ms |
| Bleach (2004), 366 episodes | 20 | 0.003 ms | 10.6 ms | 10.4 ms |

**Build.** `packaging/build.py --zip` — bundle verified, 1,621 entries,
32 bundled files byte-identical to `src/`. The archive was read back
(`CArchiveReader` → `ZlibArchiveReader`): `2.1` in `helpers.updater`
with `2.0` gone, `2.1` in `development_version_patch`, the 2.1 note in
`helpers.whats_new`, `cinemeta_scores` in `helpers.ratings`, and
`_season_ratings` / `_ratings_worker` / `_on_episode_ratings` /
`_rating_map` in `windows.player`.

A note on that reading, since it is the kind of mistake this document
exists to catch: the first pass reported the 2.1 note **missing**. The
instrument was wrong, not the build — a dict's keys compile into a tuple
constant, which the walker was skipping. Re-run over tuple constants,
the note and both version keys are there.

**Defender.** `MpCmdRun -Scan -ScanType 3` on the release exe: **no
threats, exit 0**, twice.

**Photographed on the frozen build**, against a copy of the owner's data
at 2578x1398 — both sources, from the player's own episode panel:

- **Reacher S02**, playing E03: every row `Dec 15, 2023 · TMDB 7.2` down
  to `Jan 19, 2024 · TMDB 6.4`, E01-E02 ticked watched, E03 in accent.
- **Bleach: Thousand-Year Blood War S04**, playing E05: `Jul 25, 2026 ·
  IMDb 9.1` through `Sep 5, 2026 · IMDb 8.9` — and **E08, E09, E10
  marked UPCOMING carry their date and no rating**, which is the rule
  above, on screen.

Both were drawn with the owner's "numbers only" setting on, so the rows
read `Episode 5` rather than the episode's name, with the rating on the
line below.

The app's own log for that run: 289 lines, no traceback, no exception,
and no rating lookup line — every number came off the disk index.

**What was not exercised:** a rating fetched live from TMDB on the
frozen build. Every title tried was already indexed, so the network
branch is proved by the cold harness above and not by the release
binary.

---

## 11. Glossary

See `docs/VDD-2.0.md` §13.
