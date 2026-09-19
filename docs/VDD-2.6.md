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

**2.6 is two fixes.** Marking an episode watched or unwatched from the
episode list *inside the player* now does what the same menu does on the
title's own page, and the two lists agree about what has been seen. And
the in-app updater installs an update on a PC where Windows refuses to
overwrite the old executable - where it used to close the app, leave it
closed, and leave it un-updated.

**This version was cut twice.** The first 2.6 build was published for a
few hours on the day of release and replaced by this one, under the same
number at the owner's direction, once the updater fault was found by
updating into it. Its asset had been downloaded 3 times, every one of
them from the owner's own PC (his two attempts and a reproduction), so
no install was left on it. §3 records both builds.

---

## 3. Inventory of materials released

| Item | Description |
|---|---|
| `Atomic.zip` (release asset) | The application. **125,115,986 bytes**. SHA-256 `ac0828ab94ff715732a699294a3731b590464a4b19c1054bdfd232fb5229647c` |
| `Atomic.exe` (inside that zip) | **125,912,151 bytes**. SHA-256 `0971ead34464e32d90998ed06db0afdd0742f25d0ed3c40281848991d05258cc` |
| `Atomic.exe` (committed at `v2.6`) | The **bridge installer**, 11,124,592 bytes. SHA-256 `96ff02d1e4cb60a7f931ce3f1719ecfd2eb68e427cba371afa9e5b2f4bc285c0` |
| `Atomic.zip` (committed at `v2.6`) | The same bridge installer, zipped, for a zip-preferring updater |
| `src/` | Full source, **127,767 Python lines** across 127 modules, plus 6,015 lines of served static UI |
| `packaging/` | `build.py`, `Atomic.spec`, `check_release_notes.py`, `fetch_libmpv.py`, and `bridge/` |
| `docs/VDD-2.6.md` | This document |

Two commits stand between `released/2.5` and this release: the player's
watched marks, and the updater.

**The first cut, replaced** - recorded so the two are never confused:
`Atomic.zip` 125,115,176 bytes, SHA-256
`5abc047f6bbafc573a610659fb4d1f82cd3c9b101ac35761dbb73851ed76d8a4`;
`Atomic.exe` 125,911,176 bytes, SHA-256
`fb0b87f50fea922002313bdb62ea8bac31dcbbd49e5756ebf8d6dae49b4acf16`. It is
the same source without the updater fix and without the `_current_page`
guard (§5). The `v2.6` tag was moved from that snapshot to this one; the
bridge installer committed at the tag is byte-identical in both.

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

**An update installs where Windows will not let the old exe be
overwritten - and a failed one no longer costs the app.** Updating from
Settings downloads and verifies the new build, closes Atomic, and a small
script puts the new executable in place and reopens it. That script now
waits for the app to be gone, tries the plain replace, and where Windows
refuses it, steps the old executable aside, moves the new one into its
name and removes the old one. If the new one cannot be put in place at
all, the old one is put back; and whatever happened, Atomic is reopened.
A launch that follows an update which did not land says so, and what
failed updates left in the temp folder is cleared at the next start.

**This takes effect for updates made *from* 2.6 onward.** The script that
runs during an update belongs to the version being replaced, so an update
*into* 2.6 from 2.5 or earlier still runs the old script (§9).

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

**The update that closed the app and never came back.** The owner,
updating 2.5 to the first 2.6: *"it finishes then close the app then
never re-open!!! and when I open the app manually I found it did not
update!!!!"*. The swap script was one command, `move /y new old`, retried
once a second for a minute on the theory that the only thing in the way
is the app not having exited yet - and then a silent exit, with no
relaunch. His `%TEMP%` held both verified downloads from his two
attempts, byte-identical to the release, so nothing before the swap had
failed.

Reproduced by driving a copy of the real 2.5 executable through its own
Settings > Install v2.6 in a sandbox folder, against a copy of his data,
sampling every process twice a second:

| | |
|---|---|
| 8.9 s | download verified, windows closed, the script started |
| 11.6 s | both Atomic processes gone |
| 69.5 s | the script gave up after 60 refusals and deleted itself |

So for 58 of its 60 seconds it was not waiting for anything. After that
quit, Windows answers the overwrite with *Access is denied* (error 5) and
goes on doing so for minutes; the same executable closed from its window,
with no update in flight, was replaceable 6 s later. In the refused
state, on the same file, measured: renaming the old exe aside works,
moving the new one into the freed name works, deleting the renamed one
works. The new script is that, in that order, behind a wait on the two
process ids (the rename would succeed while the app is still running, and
the new build must not start beside the old one) and ahead of an
unconditional relaunch.

**What keeps the overwrite refused was not found**, and two theories were
ruled out by test rather than kept. Defender: Controlled Folder Access
off, no block or detection events, and a never-seen executable was
replaced at once. Leaked process handles: a service on his machine
(Nahimic audio) holds handles to 96 dead processes, nine of them dead
`Atomic.exe` runs - but a handle deliberately held on a finished process
did not stop its exe being overwritten, and the same service held one
for the sandbox copy that was *not* refused.

**The script names its tools by full path.** The harness that tested the
process wait ran with Git's `find` ahead of Windows' on `PATH`, the wait
silently saw nothing alive, and the script swapped under a running
process. `tasklist`, `find` and `ping` are `%SystemRoot%\System32\...`
now, so whatever is first on a user's `PATH` cannot change what the
updater does.

**A startup exception that only the first 2.6 build had.** His log shows
`'MainWindow' object has no attribute '_current_page'`, from
`resizeEvent`, on both launches of that build and on neither launch of
the official 2.5 between them. Same source: the difference is the Qt the
build machine carries (6.11.2 here), which delivers a resize before
`__init__` has assigned the attribute. It was caught by the app's own
handler and cost nothing visible; `_fit_current_page` reads it with
`getattr` now.

**Deliberately not done:** the list page's own menu
(`episode_watch_state_patch.fixed_episode_menu`) still carries its copy
of the decision rather than calling `watch_marks.plan`. It is working
code and replacing it is a rule-11 question for the owner. The two were
measured to agree (§10), and one known difference is intended: the list
page's "Mark All as Watched" ticks an unaired episode of the season; the
player's does not.

---

## 6. Configuration and user data

No new file and no migration. During an update the old executable may
briefly exist as `Atomic.exe.old` beside the new one; it is removed by
the script, and by the next launch if Windows would not release it yet.
At startup, `Atomic-update-*.exe` and `atomic-update-*.bat` files older
than 15 minutes are removed from the temp folder. The player now **writes** per-episode
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

- **Updating *into* 2.6 from 2.5 or earlier still runs the old swap
  script**, because the script belongs to the version being replaced. On
  a PC where Windows refuses the overwrite - the owner's, reproducibly -
  that update closes the app and leaves it on the old version. The cure
  there is one manual replacement: rename the old `Atomic.exe`, put the
  new one from the release's `Atomic.zip` in its place. Every update made
  from 2.6 onward is covered.
- **Why Windows refuses the overwrite after an update-path quit was not
  found** (§5), so how many machines it affects is unknown. It happened
  on 3 of 3 update-path quits on the owner's PC and on 0 of 1 ordinary
  closes.
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
-DisableRemediation` over this build's exe twice and its zip - exit 0
every time; the bridge, unchanged and byte-identical to the first cut's,
was scanned clean then.

**The updater, second cut.** Reproduced first (§5): the real 2.5
executable, its own Settings > Install v2.6, a sandbox folder and a copy
of the owner's data made from outside the desktop app's package - both
processes gone at 11.6 s, the script refused 60 times and gone at 69.5 s,
executable unchanged. Then the new script, from the working tree, against
four cases:

| Case | Result |
|---|---|
| target idle (stand-in exes) | swapped in 0.2 s, nothing left behind |
| target still running 9 s, its id passed | waited 8.4 s for it, then swapped; no `.old` left |
| the sandbox exe Windows refuses to overwrite (precondition re-checked: error 5) | new build in place, `.old` and download both gone |
| target held open with no delete sharing for the whole run - cannot be swapped at all | after 20 refusals the **existing app was relaunched** (window up at 31.7 s), download kept |

**End to end on a frozen build**: the fixed source, numbered 2.5.1 for the
test only so that GitHub would offer it the published 2.6, driven through
Settings > Install v2.6 in a fresh sandbox folder. Download verified and
script started at 8.7 s; both processes gone and the executable already
the 2.6 bytes at 11.4 s *with `Atomic.exe.old` present* - the plain
overwrite had been refused again and the step-aside did the job; new
process at 15.5 s, `.old` and script gone; the main window and the
"Atomic is now version 2.6" dialog at 25.3 s. Photographed: the update
prompt, Settings reading "Atomic 2.5.1 - Install v2.6", and that dialog.
The test build's startup tidy removed the owner's two dead downloads from
his real `%TEMP%` (252 MB) as designed - the temp folder is not
sandboxed.

**The failed-update notice**, from source against a temporary data
directory: a marker equal to the running version sets the flag and shows
no notes; an older marker shows 2.6's notes and leaves it clear; no marker
leaves it clear; the marker is consumed either way.

**Two things the verification itself got wrong, and what caught them**: a
run that "succeeded" while the relaunched app sat off-screen with
WebView2 failing - the harness had passed its own
`QT_QPA_PLATFORM=offscreen` down to the app (an A/B of the two launch
methods on the same exe drew pages both ways, which cleared the product);
and a process wait that saw nothing alive because Git's `find` shadowed
Windows' on the harness's `PATH` - which became the full-path rule in §5.

**Read back out of this build** (14 of 14, beside the 15 above): the
script steps aside, waits on both ids, reaches `start` on its only way
out, has no `goto cleanup`, names its tools through `%SYS%` with no
mangled escape; `apply_update` passes `getpid` and `getppid`;
`tidy_leftovers` exists and `main` calls it and shows the notice;
`whats_new` sets the flag; `_fit_current_page` uses `getattr`; both
version constants read **2.6** with no `2.5.1` left from the test build;
the 2.6 notes carry the updater line.

**Not exercised here**: the failed-update notice on a frozen build (its
logic only, above); an update *from* this 2.6 to a later version, which
cannot exist until there is one - the frozen test stood in for it; any
machine but the owner's; the running app for the player fix (left to the
owner, above);
playback with the panel open across the 85% mark on the frozen build; a
Stremio sync after a clear made from the player (§5); the no-Cinemeta
corner of §9; his second machine.

---

## 11. Glossary

See `docs/VDD-2.0.md` §12.
