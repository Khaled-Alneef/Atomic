# Atomic — Version Description Document

**Version 2.6** · 19 September 2026

---

## 1. Identification

| | |
|---|---|
| System name | Atomic |
| Version | 2.6 |
| Release date | 2026-09-19 |
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

**2.6 is one fix**: marking an episode watched or unwatched from the
episode list *inside the player* now does what the same menu does on the
title's own page, and the two lists agree about what has been seen.

---

## 3. Inventory of materials released

| Item | Description |
|---|---|
| `Atomic.zip` (release asset) | The application. **125,115,176 bytes**. SHA-256 `5abc047f6bbafc573a610659fb4d1f82cd3c9b101ac35761dbb73851ed76d8a4` |
| `Atomic.exe` (inside that zip) | **125,911,176 bytes**. SHA-256 `fb0b87f50fea922002313bdb62ea8bac31dcbbd49e5756ebf8d6dae49b4acf16` |
| `Atomic.exe` (committed at `v2.6`) | The **bridge installer**, 11,124,592 bytes. SHA-256 `96ff02d1e4cb60a7f931ce3f1719ecfd2eb68e427cba371afa9e5b2f4bc285c0` |
| `Atomic.zip` (committed at `v2.6`) | The same bridge installer, zipped, for a zip-preferring updater |
| `src/` | Full source, **127,620 Python lines** across 127 modules, plus 6,015 lines of served static UI |
| `packaging/` | `build.py`, `Atomic.spec`, `check_release_notes.py`, `fetch_libmpv.py`, and `bridge/` |
| `docs/VDD-2.6.md` | This document |

One commit stands between `released/2.5` and this release.

**Built on a different machine from 2.5**, with a toolchain installed
for the purpose that day: CPython 3.13.15, PyInstaller 6.22.3, PyQt6
6.11.0 (Qt 6.11.2), libtorrent 2.1.1, python-mpv 1.0.8, pywebview 6.2.1,
Pillow 12.3.0. libmpv is the same Stremio-pinned mpv 0.41.0
(`fetch_libmpv`, DLL SHA-256 `abf3d4b0…9d7749`), so the player engine is
the one 2.5 shipped; the Python-side package versions were not compared
against the machine 2.5 was built on, and the exe is ~0.4 MB smaller
than 2.5's.

The bridge is unchanged in purpose and rebuilt from the same source: it
resolves the newest release carrying an `Atomic.zip` asset **at run
time**, walking `/releases`, so the copy committed at `v2.6` will find
2.7 and everything after it without being rebuilt.

---

## 4. What this version provides

**Marking watched/unwatched inside the player works like the title
page's list.** Right-clicking an episode in the player's episode panel
offers the same four choices it always did; what they do is now the
title page's rule:

- *Mark as Watched* marks that episode **and every episode before it**.
- *Mark as Unwatched* clears that episode **and every episode after
  it**, and progress steps back to the episode before.
- *Mark All as Watched / Unwatched* does the same for the whole season —
  and "all watched" stops at what has aired.

What is marked in the player shows on the title's page, and the other
way round. The first episode of a season can be unmarked from the player
(it could not before), which for season 1 means "nothing watched yet";
and a title that has not been saved to the library can be marked there
too.

---

## 5. Design notes

**Two copies of one decision, and one had drifted.** The title page's
list has used per-episode History ticks as the truth since
`episode_watch_state_patch` ("the ticks win": once a title has any, the
progress number is not consulted). The player's panel kept its own,
older copy of the menu, which moved **only the progress number**
through `tracker.correct_progress`. Reproduced before editing, offscreen
against a fabricated data directory, by driving the real
`PlayerPage._episode_menu` and reading the real `_episode_row` widgets
for the watched glyph, with the list page's rule evaluated on the same
stored state:

| Old player code | Stored | List page draws | Player panel draws |
|---|---|---|---|
| unmark E3, with 1–5 watched | S01E02, ticks 1–5 untouched | 1 2 3 4 5 | 1 2 |
| then mark E7 | S01E07, ticks 1–5 + 7 | 1 2 3 4 5 7 | 1–7 |
| then unmark S01E01 | **nothing changed** | 1 2 3 4 5 7 | 1–7 |
| unmark S02E01 at S02E03 | **nothing changed** | 1 2 3 | 1 2 3 |
| mark all, season 2 | S02E07, one tick (2:7) | 7 | 1–7 |
| clear all, season 1 | number cleared, **no `progress_cleared_at`**, ticks untouched | 1–6 | none |
| unsaved title, mark E4 | one tick (1:4) | 4 | none |
| ticks 1,3 with number at E5 — verb offered on E2 | — | E2 unwatched | "Mark as **Un**watched" |
| unverified number S01E06, no ticks | — | none | 1–6 |

Eleven cases, and in every one the two surfaces disagreed or the click
did nothing. The first-episode case was a literal early return: the
target worked out to "episode 0" and the function returned on it.

**The fix is one rule in one place.** `helpers/watch_marks` holds the
decision, Qt-free and with no storage in it: `known_pairs` (the real
episodes in order, specials excluded for the reason
`episode_watch_state_patch._episode_pairs` records), `is_watched` (ticks
where the title has any, else the verified number — the row's glyph and
the menu's verb are now the same call), and `plan` (what a menu choice
changes: which ticks, which way, and where progress stands after). The
player writes History first and unconditionally, then the number for a
saved title, exactly as the list does.

**"Nothing watched" is one write now.** Both surfaces clear progress
through `tracker.clear_video_progress`, which is the list page's write
moved onto the entry. The player's own copy of it had left
`progress_cleared_at` out — the field `_on_progress_synced` checks before
letting a Stremio sync write a number over an empty one. That was read
off the two writers side by side; it was **not** reproduced against a
live sync. The new function also bumps `helpers/changes`, so a clear
shows on every open page at once (rule 13); the list page's clear
previously relied on the tick write beside it to do that.

**The panel's ticks are cached on the change counter.** The panel
refills on every episode change and every keystroke in its search box,
and reading the ticks means parsing `history.json`. Every tick write
already bumps `helpers/changes`, so the panel re-reads only when that
counter has moved.

**Deliberately not done:** the list page's own menu
(`episode_watch_state_patch.fixed_episode_menu`) still carries its copy
of the decision rather than calling `watch_marks.plan`. It is working
code and replacing it is a rule-11 question for the owner. The two were
measured to agree (§10), and one known difference is intended: the list
page's "Mark All as Watched" ticks an unaired episode of the season; the
player's does not.

---

## 6. Configuration and user data

No new file and no migration. The player now **writes** per-episode
ticks into `history.json` on a manual mark (it only ever wrote one tick
at the 85% point before), and a clear made from the player now stamps
`progress_cleared_at` on the entry in `series.json`, as a clear made from
the list page already did.

---

## 7. External interfaces

Unchanged from 2.5. No network host is added or removed.

---

## 8. Installation and removal

Unchanged — `docs/VDD-2.0.md` §9. An install running 2.0 or later
updates in place from the release asset; an install running 1.10 or
older is handed the bridge committed at the tag, which fetches the
asset.

---

## 9. Known limitations

Carried forward from 2.0 §10 and earlier. Those that belong to this
version:

- **A title Cinemeta cannot answer for, on season 2 or later:** with no
  episode list and no aired map, the player knows only the season on
  screen, so unmarking that season's first episode reads as "nothing
  watched" and clears the number — the list page does the same with an
  empty list. Rare: it needs a title with no IMDb id whose progress is
  already past season 1.
- **Automatic marks are still single ticks.** Crossing 85% of an episode
  ticks that episode only; it is the manual mark that fills in
  everything before it. Unchanged, and the same on both surfaces.
- The list page's menu still has its own copy of the rule (§5).
- Code signing (roadmap #8) is still open; the release is unsigned, and
  shipping the zip rather than the bare exe is what makes the download
  come through.

---

## 10. Verification performed

Method: `.claude/rules/testing.md`. **The owner tested the running build
himself** — he stopped the automated verification after the first build
("I will test it my self") and released it on his own result
("perfect"). So nothing here was driven or photographed in the real
window (rule 10): no screenshot of the player's panel, and the frozen
exe was not run by the rig. What was verified before tagging:

**Reproduce, then prove, on the same harness** (offscreen Qt, fabricated
entries in a temporary data directory — the owner's `%APPDATA%\Atomic`
was never read): the eleven cases of §5 against the unmodified `HEAD`
tree and then the fixed tree. Old: 11 of 11 disagree or do nothing. New:
**11 of 11 agree** — list and panel draw the same episodes, S01E01 and
S02E01 unmark, an unsaved title takes a cascade of ticks, the unaired
S02E08 is not ticked by "mark all", a stored half-watched position is
zeroed with its release kept, and the change counter moves.

**Same input, both surfaces**: the list page's *installed* menu
(confirmed by name to be `fixed_episode_menu`, the cascade) and the
player's, on ten identical inputs — unmark mid-season, mark forward,
mark from nothing, unmark S01E01, unmark S02E01, a mark that crosses a
season, a toggle against out-of-order ticks, mark all, clear all on
season 1 and on season 2. **10 of 10 leave identical stored state**
(progress, `progress_cleared_at`, ticks). That run also exercises the
list page's clear through its new delegation.

**Read back out of the frozen archive** (15 of 15): `helpers.watch_marks`
is present with `known_pairs`, `is_watched`, `plan`; `windows.player`
carries `_episode_ticks` and `_known_episodes`; its `_episode_menu` calls
`plan`, `set_watched` and `clear_video_progress` and **no longer** calls
`update_entry` or `parse_episode_progress`; `_episode_row` asks
`is_watched`; `_watched_through` reads `progress_verified`;
`DetailsPage._clear_video_progress` delegates. `APP_VERSION` reads
**2.6** in both `helpers.updater` and `helpers.development_version_patch`,
with no `2.5` constant left in either, and `whats_new` carries the 2.6
notes.

**Build gates**: `check_release_notes.py` — 3 notes for 2.6;
`build.py` — 1,622 archive entries, 32 bundled files byte-identical to
`src/`, not a cached build; `build_bridge.py` — built, and it runs.

**Defender**: `WinDefend` running; `MpCmdRun -Scan -ScanType 3
-DisableRemediation` over the release exe twice, the release zip, and
the bridge — exit 0 every time.

**Not exercised here**: the running app (left to the owner, above);
playback with the panel open across the 85% mark on the frozen build; a
Stremio sync after a clear made from the player (§5); the no-Cinemeta
corner of §9; his second machine.

---

## 11. Glossary

See `docs/VDD-2.0.md` §12.
