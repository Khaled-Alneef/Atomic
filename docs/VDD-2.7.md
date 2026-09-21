# Atomic — Version Description Document

**Version 2.7** · 21 September 2026

---

## 1. Identification

| | |
|---|---|
| System name | Atomic |
| Version | 2.7 |
| Release date | 2026-09-21 |
| Repository | https://github.com/Khaled-Alneef/Atomic |
| Target platform | Windows 10 / 11, 64-bit |
| Delivered as | `Atomic.zip` — one **GitHub release asset** holding the ~11 MB **bridge installer** as its only `Atomic.exe` and the application **as a folder**, packed as `app.zip` (`Atomic/Atomic.exe` + `Atomic/_internal/`) |
| At the tag | `Atomic.exe` / `Atomic.zip` — the same bridge, for installs predating 2.0 |

---

## 2. System overview

Unchanged in purpose from 2.0 — see `docs/VDD-2.0.md` §2.

**2.7 changes what Atomic *is* on disk.** Every release before it was one
self-extracting executable. 2.7 is a folder installed to
`%LOCALAPPDATA%\Programs\Atomic`, with `Atomic.lnk` on the Desktop and in
the Start menu. The reason is startup time (§5). The release also carries
the session's fixes: far fuller genre filters on every Watch and Read
page, the details page opening and closing without a blank or stale
frame, sideways touchpad scrolling, a row scrollbar that no longer jumps,
and the "Most watched" line removed under the medium pages' names.

---

## 3. Inventory of materials released

| Item | Description |
|---|---|
| `Atomic.zip` (release asset) | **137,497,645 bytes**. SHA-256 `5f06da2147aaac8fe2dfb0cfc102be114d1df29e20d0a0117a097fca757a1ab6` |
| `app.zip` (inside it) | The application folder, **126,607,376 bytes**. SHA-256 `94b62a2d4741ddb9e601b173b38dfbb7ac9f670f23965fe5fd340e15e478db9c` |
| `Atomic/Atomic.exe` (inside app.zip) | The folder build's launcher, **8,125,398 bytes**. SHA-256 `1f82b5d15781beb9cbea5f26114f3e89813af1e0040ae185212477ed5df0fa2b` |
| `Atomic.exe` (inside Atomic.zip, and committed at `v2.7`) | The bridge installer, **11,130,111 bytes**. SHA-256 `385f10cbaa7dcf92b9351a492f47a9b9eeda31655cfcf1d7101418b3a42e4022` |
| `Atomic.zip` (committed at `v2.7`) | The bridge alone, zipped, for installs predating 2.0: **10,890,179 bytes**. SHA-256 `ce43d7009fb5813592b04d3de2a59ce2997b986ebe7306c801db63f4850f6fc3` |
| `src/`, `packaging/` | Full source; `packaging/build.py --zip` writes the asset |
| `docs/VDD-2.7.md` | This document |

Built with CPython 3.13.15, PyInstaller 6.22.3, PyQt6 6.11.0, on the
machine 2.6 was built on.

---

## 4. What this version provides

- **Startup about three times faster.** Home at 2.2–2.4 s after the
  first launch, against 6.5–7.1 s for 2.6 on the same idle laptop; a
  sign-in launch had been 14.9 s from Task Scheduler's start to Home.
- **An install that moves itself.** Updating from Settings in 2.0–2.6
  installs the bridge, which takes `app.zip` out of the same `Atomic.zip`,
  installs the folder, makes the shortcuts, starts it and deletes the old loose exe.
  The installed app points the startup task at itself on first launch.
- **Updates swap the whole folder** from 2.7 on (`app.zip` out of `Atomic.zip`, `updater.APP_PAYLOAD_NAME`),
  with the 2.6 script's wait / step aside / put back / always relaunch
  order.
- **Uninstall** removes the installed folder and both shortcuts.
- **Stale unpack folders** that single-file builds left in `%TEMP%`
  (18, ~5 GB, on the owner's laptop) are removed at startup, only where
  they hold Atomic's own files and nothing has them open.
- **Genre filters** (Romance, measured): Anime 94 → 357 titles
  (AniList's genres added by exact title), Series and Movies four
  catalogue pages at a time (Series 48 → 876 within 1.9 s), reading
  pages from kept rows and deeper listing pages (43 → 130 on a first
  tick, 123 rows in 28 ms on the next).
- **Details page:** opening a card and going back swap in one step — no
  blank page and no stale page with the sidebar over it.
- **Rows:** a sideways touchpad swipe stays native for the whole swipe;
  a row's scrollbar keeps its height (309 px throughout, where it fell
  from 340 px at ~1,200 px in) - by giving every row card one exact
  height (a one-line meta line; 9 of 443 cut with "…"), not by laying
  every card out, which was tried first and cost frames.
- **Discover scrolls smoothly.** At 144 Hz with a real wheel, frames over
  10 ms while moving: the first row fix 22–30 of ~355, this one 0 of
  378–381 warm and 3 of 377 cold with 359 covers still loading. The
  owner's own 2.6 log had Discover losing a frame in 44 % of glides
  against 4–11 % elsewhere; 2.6 measured 0–1 of ~380 here warm, so its
  loss in real use was not reproduced warm, and 2.6 cold was not measured.
- The "Most watched" note is gone from the six medium pages.
- **Arabic subtitles from opensubtitles.org itself**
  (`subtitles._opensubtitles_org`). The Stremio addon the app asked had
  nothing in any language for Dagashi Kashi, which the site carries three
  Arabic files for; across the owner's library the site returned more
  Arabic on every title sampled (Attack on Titan S01E05 16 against 4).

---

## 5. Design notes

**Why a folder.** Measured on the owner's laptop: the single exe
unpacked 290 MB in 1,609 files into `%TEMP%` on every launch before any
app code ran — first log line at 5.1–5.3 s, Home at 6.5–7.1 s idle; at
his 13:09 sign-in Task Scheduler started it 3 s after logon and Home
drew 14.9 s later. The same tree as a folder: 0.8–1.0 s to the first
line and 2.0–2.4 s to Home from the second launch (the first pays one
Windows scan of the new files). Two alternatives were offered to the
owner; he chose the folder, installed under AppData with shortcuts.

**Why the bridge is the transition.** An update is performed by the
*old* version's updater, and every single-file version takes the one
`.exe` out of `Atomic.zip` and swaps it in for itself. It cannot install
a folder. So the bridge is the only `.exe` in `Atomic.zip`, and the app
rides beside it packed as `app.zip`, which only the folder build and the
bridge open. One asset, one name - the owner's ask - and every install
takes the right half of it.

**Three safety rules in the new code**: the startup task is re-pointed
only by the copy running from the install folder (a repository build
must never take over the owner's task — Task Scheduler is not
sandboxed); probing a reading site's page address treats a 404 as an
answer, because two failures put a site on the refusing list for ten
minutes; the unpack-folder cleanup renames before deleting, so a folder
a running copy still uses is refused by Windows rather than emptied.

Details of the genre, swap and scroll fixes are in
`.claude/rules/integrations.md` (21 September 2026) and in the code at
each change.

---

## 6. Configuration and user data

`%APPDATA%\Atomic` is untouched by the move. New files there:
`reading_index.json` (every reading row browsed, bounded at 6,000). The
application folder is `%LOCALAPPDATA%\Programs\Atomic`; during an update
`Atomic.old` and `Atomic.new-<pid>` may exist beside it briefly and are
cleared at the next start.

---

## 7. External interfaces

No new host. AniList is **not** asked for anime genres at run time — the
table is shipped (`helpers/anime_genre_seed`). Reading sites are asked
for pages 2–12 of their listings when a genre filter runs out of known
rows.

---

## 8. Installation and removal

- **New install:** download `Atomic.zip`, extract it, and run
  `Atomic.exe` - the bridge installs the `app.zip` beside it.
- **From 2.0–2.6:** Settings > Install — the bridge does the rest.
- **From 1.0–1.10:** the tag route hands over the same bridge.
- **Removal:** Settings > Uninstall removes the folder, both shortcuts,
  the startup task and the data folder.

---

## 9. Known limitations

- **The first launch from the new folder raises one Windows Firewall
  prompt** — the torrent engine listens for peers, and firewall rules
  are per path.
- **A taskbar pin of the old `Atomic.exe` stops working** after the move;
  pin the new shortcut.
- The anime genre table does not refresh itself; a show outside AniList's
  top 2,000 keeps Cinemeta's genres only.
- Mangalek refuses listing pages past its front page; SWAT was not
  reachable on the day the page addresses were measured.
- Code signing (roadmap #8) is still open.

---

## 10. Verification performed

Method: `.claude/rules/testing.md`, on the frozen build, against copies
of the owner's data. **The owner tested the build and released it**
("it is perfect").

**Build gates**: `check_release_notes.py` — 5 notes for 2.7; `build.py`
— 1,609 entries, 32 bundled files byte-identical to `src/`;
`build_bridge.py` — built, selftest reached the live releases API.
Read back from the frozen archive: both version constants read **2.7**,
no `2.6.1` left, the 2.7 notes present.

**Defender**: `WinDefend` running; `MpCmdRun -Scan -ScanType 3
-DisableRemediation` over the app launcher, the bridge exe, `app.zip`
and `Atomic.zip` — exit 0 each.

**The move, against a stand-in for the releases API serving the real
zips** (sandboxed install and shortcut folders): 2.6's own updater code
(from `git HEAD`) was offered `Atomic.zip` and extracted the bridge
byte-identical; the bridge installed at 8.7 s, started the app at 11.2 s
and removed itself at 13.8 s, both shortcuts pointing at the installed
exe; the installed app photographed running. The folder swap waited for
the running app, kept the old copy while a file inside was held open,
swapped 2 s after release and relaunched, leaving nothing beside it. The
startup re-point decision: a repository build leaves the task alone, the
installed copy re-points it; the owner's real task was unchanged by
every test.

**The one-asset layout, re-tested on the build released** (a stand-in
for the releases API serving only `Atomic.zip`): 2.6's updater took the
bridge out of it byte-identical; the bridge installed from the inner
`app.zip` at 8.2 s, started the app and deleted itself at 9.9 s; the zip
extracted by hand installed with the API unreachable (the local
`app.zip`) at 6.3 s; the folder build's updater staged the folder
(1,730 entries) from the same asset. Found on the way: the bridge's
self-delete used `find` off PATH, which under a Git Bash PATH is GNU
find - the wait ended at once and the delete met a running exe. It
names System32's tools and retries the delete now.

**Screens and measurements** for the fixes: §4, each taken on the frozen
build and photographed where visible (rule 10).

**Not exercised before publishing**: a real update through GitHub (the
live release was checked after publishing - `updater.check_for_update`
and the bridge's own lookup both resolve it); a real sign-in with the
folder build; any machine but the owner's laptop.

---

## 11. Glossary

See `docs/VDD-2.0.md` §12.
