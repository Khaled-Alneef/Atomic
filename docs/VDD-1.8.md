# Atomic — Version Description Document

**Version 1.8** · 17 August 2026

---

## 1. Identification

| | |
|---|---|
| System name | Atomic |
| Version | 1.8 |
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
| `Atomic.exe` | The application. 47,783,790 bytes. SHA-256 `1b04f705c04dbdecaebf78a154f11ab5234707b33bce3843e2dc6f2a7f4c176c` |
| `src/` | Full Python source, 16,096 lines across 38 modules (15,257 across 37 at 1.7) |
| `packaging/` | `build.py`, `Atomic.spec` and `check_release_notes.py` |
| `docs/VDD-1.8.md` | This document |

Build environment unchanged from VDD-1.1 §3.

Four commits stand between 1.7 and this release, taken from
`development` as a single snapshot. No development builds sit in
between, and this release was cut once.

---

## 4. Inventory of software contents

| Module changed | Lines | What changed |
|---|---|---|
| `helpers/game_launch.py` | 261 | **New.** How to start a game through its own launcher |
| `helpers/settings_dialog.py` | 1,582 | Probe titles from the user's library; probes on their own workers |
| `helpers/widgets.py` | 1,188 | `SideScroller` and its edge fade |
| `helpers/anime_sites.py` | 1,086 | `probe_site` takes several titles |
| `windows/home.py` | 1,000 | The clock; the in-place row/list refreshes |
| `windows/link_grid.py` | 809 | `grid_columns`, `relayout_for_sidebar` |
| `helpers/theme.py` | 789 | `#ScrollArrow`, `#HomeClock` |
| `windows/games.py` | 504 | Launch via `game_launch`; column count; Edit clears a stale command |
| `helpers/manga_sites.py` | 456 | `probe_site` takes several titles |
| `helpers/whats_new.py` | 338 | 1.8's seven notes |
| `helpers/launchers.py` | 305 | Launch commands resolved at scan time, and backfilled |
| `helpers/updater.py` | 272 | `APP_VERSION` |
| `helpers/global_search.py` | 246 | Opens a game through `game_launch` |
| `helpers/storage.py` | 243 | The write lock |
| `helpers/lookup_pool.py` | 136 | `submit_watched` and its own workers |
| `helpers/child_process.py` | 76 | `clean_environ` for ShellExecute |
| `src/main.py` | 1,240 | Re-flow on fold; redraw Home on a sidebar reorder |
| `windows/tracker.py` | 3,053 | Section rows wrapped in `SideScroller` |

Nothing else in `src/` differs from 1.7.

---

## 5. What this version provides

### Games start through their own launcher

Every game was started by running the `.exe` found on disk. That is
wrong for every launcher game and only looked right for a standalone
one:

* a Steam game started outside Steam is invisible to it — the client
  still says the game is not running, so no overlay, no playtime, no
  cloud saves;
* Overwatch, started as `_retail_/Overwatch.exe` with no Battle.net
  ticket, put its own sign-in screen up **inside the game**;
* VALORANT's `VALORANT.exe` shim did nothing at all without the Riot
  Client.

Sons of the Forest worked only because its DRM wrapper relaunches itself
through Steam — which is why the fault looked intermittent.

`helpers/game_launch.py` resolves the command per launcher, every one of
them read off the shortcuts the launchers themselves wrote on this
machine rather than guessed at:

| Launcher | Command | Identifier from |
|---|---|---|
| Steam | `steam://rungameid/<appid>` | `steamapps/appmanifest_*.acf` |
| Battle.net | `<Game> Launcher.exe --productcode=<code>` | the game's `.build.info` |
| Riot | `RiotClientServices.exe --launch-product=… --launch-patchline=…` | `RiotClientInstalls.json` |
| Epic | `com.epicgames.launcher://apps/<AppName>?action=launch&silent=true` | the launcher's `.item` manifests |

Xbox is deliberately not handled: its target is
`shell:appsFolder\<PackageFamilyName>!App`, and the family name (with
its publisher hash) is not in the install folder. A game with no
resolved command — added by hand, or in a Steam folder with no manifest
— still launches by path exactly as before. Existing entries are
backfilled on the Games page and Home, so nothing needs re-importing.

### Home keeps up while you are looking at it

A game played from Home moves to the front of its row 2.5s later, an
opened Quick App or Website moves to the top of its list, and dragging
the sidebar into a new order rearranges Home's sections to match. The
2.5s is deliberate: re-sorting on the click slides the card out from
under the pointer that just pressed it.

### A clock on Home

Top right, on the greeting's own line, as `9:24 AM`. It re-arms itself
to each minute boundary rather than ticking on a fixed 60s interval,
which would show every new minute up to 59s late.

### Wider grids, and arrows on the sideways rows

Games, Apps and Websites lay out 13 cards to a row, 14 while the sidebar
is folded, re-flowing the moment it folds instead of at the next visit.
The status rows on Anime, Reading and Movies & Series carry an arrow at
each end, over a gradient that fades the cards beneath it; an arrow
appears only while there is something that way to reach.

### Checking a website says something true

See §6.

Everything else in this release is 1.7. See VDD-1.7 §5.

---

## 6. Design notes

**Why the launcher commands are read off disk.** A table of Blizzard
product codes would need a row per game and would silently launch
nothing for anything missing from it. `.build.info` already carries the
code (`pro` for Overwatch), Riot's `RiotClientInstalls.json` already
maps each installed patchline to the client that owns it, and Steam's
manifests already map install folder to appid. Blizzard's own desktop
shortcut is `"…\Overwatch Launcher.exe" --productcode=pro`; the resolved
command is that shortcut, derived rather than copied.

**Why a URI is opened with `os.startfile` and not `cmd /c start`.** A
launcher URI carries query parameters — Epic's contains an `&` — that
`cmd` would read as its own syntax. ShellExecute hands the child this
process's environment, so the PyInstaller bootloader variables are
stripped for the duration (`child_process.clean_environ`) for the reason
already documented in that module.

**Why the website check asks for the user's own titles.** It asked every
site for one hardcoded title, "One Piece". Three of the four reading
sites configured here are Arabic scanlation sites that do not carry it
under that name, so the probe found nothing and reported the site as
search-only. It now asks for up to three tracked titles, preferring any
already pointed at the site being checked. Lava Scans resolves only for
*The Eternal Supreme* — the title actually tracked there — so the
per-site preference is load-bearing, not merely having several titles.
One step of the budget is held back for the reachability check, because
spending the last of it on another search is how a slow site produced
"Check failed" where the honest answer was that it is up.

**Why site probes have their own workers.** `lookup_pool`'s shared queue
is drained by three tracker pages' page-load backfill, so a Check
pressed after visiting one waited behind all of it. Crunchyroll made
that visible: its verdict is decided from a table with no request at
all, and the row still never filled in.

**Why the write lock is in `storage` and not in the dialog.** `save()`
writes one fixed `<name>.tmp` and renames it into place, so two threads
saving the same file at once left the second one's `os.replace` with
nothing to rename — and even without that, both had read the same list
before merging their own field, so the later save dropped the earlier
one's change. That is not a Settings problem: the tracker's four lookup
workers all call `update_entry` on `tracker.json`. The lock covers the
whole read-modify-write, in the one place every writer goes through.

---

## 7. Configuration and user data

Unchanged from VDD-1.7 §7, with one addition: a game in `games.json`
may now carry `launcher` and `launch`, describing how to start it
through its launcher. Both are written by the import and by the
backfill, and both are absent for a hand-added game. Editing a game's
path clears them, so a re-pointed entry cannot keep starting the old
game.

---

## 8. External interfaces

Unchanged from VDD-1.7 §8. No new service is contacted: the launcher
identifiers are read from files already on this machine, and the website
check makes the same kind of request it always did.

---

## 9. Installation and removal

Unchanged from VDD-1.2 §9.

---

## 10. Known limitations

In addition to VDD-1.7 §10, including the antivirus note (the durable
remedy remains a code-signing certificate, and nothing has been bought):

- **Epic's launch path is unverified.** No Epic titles are installed
  here, so that branch was written to the documented manifest shape and
  falls back to launching by path rather than guessing an `AppName`.
- **Xbox / Game Pass games still launch by path**, for the reason in
  §5, and nothing was installed to measure against.
- **The grids do not measure the space they have.** 13 and 14 are the
  owner's chosen widths, verified to fit this display; a much smaller
  window will run a row past the panel edge rather than wrapping it.
- **Movies & Series has no entries**, so its rows — and therefore its
  arrows — were exercised on Anime and Reading only.

---

## 11. Verification performed

In addition to VDD-1.0 §11 through VDD-1.7 §11:

- **The shipped code was read back out of the frozen executable**, not
  out of the source tree. `helpers.game_launch` in the bundled PYZ
  carries `steam://rungameid/`, `--productcode=` and
  `--launch-product=`; `helpers.updater` carries `"1.8"`;
  `helpers.whats_new` this release's notes; `helpers.storage` the write
  lock; `helpers.lookup_pool` `submit_watched`; `helpers.widgets`
  `SideScroller`; `windows.home` `HomeClock`; `windows.link_grid`
  `GRID_COLS_SIDEBAR_OPEN`. `windows.games` no longer contains a
  `Popen` call at all, which is what proves the launch path actually
  moved. A build log is not evidence here — PyInstaller caches, and a
  no-op rebuild re-copies the previous binary.
- **The bundle gate passed**: every file `Atomic.spec` promises in
  `datas` present in the archive and byte-identical to the one on disk,
  174 entries.
- **`check_release_notes.py` passed** for 1.8, with seven notes written.
- **Launcher resolution measured over the owner's real library**: 10 of
  10 games resolve — 8 Steam titles to `steam://rungameid/<appid>`,
  Overwatch to `Overwatch Launcher.exe --productcode=pro`, VALORANT to
  the Riot client with `--launch-product=valorant --launch-patchline=live`.
  Already-imported entries backfilled and persisted without a
  re-import. `run()` was driven with `Popen`/`os.startfile` intercepted,
  so the commands were checked without starting anything, and an entry
  with no resolved command still launches by path.
- **Grid widths measured, not assumed**: 13 per row with the sidebar
  open (last card ending at x=1688 in a 1744px grid) and 14 folded
  (x=1818 in 1896px) on the owner's maximized window; Apps and Websites
  hold fewer than 13 entries, so both were re-run with 20 fabricated
  entries and gave 12/13/12 before the widths were raised and 13/14/13
  after.
- **The row arrows measured on Reading's long row**: range 0..680, the
  right arrow scrolled to 680 and hid itself, the left arrow appeared
  and returned to 0. The fade was corrected to 397px so it stops above
  the scrollbar instead of tinting its ends.
- **The clock's format checked at 09:24, 10:09, 12:58, 00:05, 13:00 and
  23:59** — `9:24 AM`, `10:09 AM`, `12:58 PM`, `12:05 AM`, `1:00 PM`,
  `11:59 PM` — and its label measured on the greeting's line (both at
  y=45), with the widest time it can show, 131px, inside its 210px box.
- **The website check re-measured against the owner's real sites**:
  TeamX, Lava Scans and SWAT all move from "search-only" to "engine",
  3asq unchanged, Crunchyroll and Netflix instant. A Check answered in
  0.04s with 8 backfill jobs queued ahead of it on the shared pool.
- **The concurrent-save fix measured directly**: 8 threads × 25 updates
  each against one file — nothing raised, zero lost updates, no leftover
  temp files. Before it, Check All on the video list recorded Netflix's
  verdict and lost Crunchyroll's, leaving that row permanently blank.

Not verified: the release executable has not been exercised by its owner
against real data before publication, and no Defender scan was
performed. The updater's own `check_for_update()` against the live
repository is recorded in §11 of this document only if it ran — see the
note below.

---

## 12. Glossary

Unchanged from VDD-1.2 §12.
