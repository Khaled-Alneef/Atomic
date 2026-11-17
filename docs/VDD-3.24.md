# Atomic — Version Description Document

**Version 3.24** · 17 November 2026

---

## 1. Identification

| | |
|---|---|
| System name | Atomic |
| Version | 3.24 |
| Release date | 2026-11-17 |
| Repository | https://github.com/Khaled-Alneef/Atomic |
| Target platform | Windows 10 / 11, 64-bit |
| Delivered as | `Atomic.zip` — one **GitHub release asset** holding the ~11 MB **bridge installer** as its only `Atomic.exe`, and the app's folder packed as `app.zip` |
| At the tag | `Atomic.exe` / `Atomic.zip` — the bridge, for installs predating 2.0 |
| Asset size | 136,226,028 bytes |
| Asset SHA-256 | `b930c65bb908280fb41e33f9589779138b6f98e6ec88160a9c02cb15718e7b02` |
| Bridge SHA-256 | `7816b8c48076249b3cc1f3e6f8387cb9b83104528b4c27d400ebda8be1cfe4ee` (11,180,522 bytes). The asset's bridge (11,181,037 bytes) is a separate build of the same source; PyInstaller is not byte-identical twice |
| Preceded by | 3.23 — `docs/VDD-3.23.md` |

---

## 2. System overview

Unchanged from 3.23.

---

## 3. Inventory of materials released

As 3.23 §3, plus one module: `helpers/sub_sync.py` (subtitle timing).

**Changes since the last release** (`git log released/3.23..development`):

| Commit | Summary |
|---|---|
| 7f46fae | A rebuilt player panel no longer frees its timers or leaves its corners |
| dd8b132 | A crash record writes the raising thread only, and test playback is silent |
| 8748610 | Closing the app ends the process without Qt's teardown |
| 825ab8f | An outside subtitle is timed to the video it is laid over |
| e7b4cc9 | Atomic 3.24 |

---

## 4. What this version provides

**Outside subtitles line up with the video by themselves.** A subtitle picked
from OpenSubtitles, SubDL or the other sources is measured against the playing
file's own text tracks, and Delay is set from the shift that fits. One cut for
another version of the episode is not shifted, and the player says so.

**Each OpenSubtitles file is listed once**, under its release name; the
Stremio addon and the site's own search used to list it twice.

**Fixed: the crash on picking an embedded subtitle, changing the source or
changing the subtitle while watching**, and the panel corners that could stay
on the video after a source pick.

**Fixed: the crash on closing the app.**

---

## 5. Design notes

**The reference is the file's Cues index.** Every text track in one file is
timed to that file, whatever its language. mkvmerge writes a cue point for every
subtitle block, so `mkv_subs.cue_starts` reads every line's start from the index
at the file's tail (133 KB, 21 ms for eight tracks on the release that prompted
this), which the engine fetches before playback. The SeekHead can be two hops:
that file's first SeekHead names only a second one at the tail.

**The match.** A histogram of every (reference − subtitle) start difference
within ±300 s, 0.1 s bins, a 0.35 s window; the median residual refines the
shift. Applied when at least 20 lines and half of those in reach match and the
runner-up more than 2 s away scores under 60 % of the best. Measured on Silicon
Valley S04E08 (MAX WEB-DL): the Arabic he had picked −50.22 s (97 %), a second
Arabic file −46.22 s (97 %), a third rejected (35 %, a different cut), and the
file's own English as a control 0.00 s (100 %).

**When it runs.** On each fresh load of an outside subtitle, not on the re-add
after a reload. A delay already stored for the same pick on that episode is kept;
a hand edit of the delay while it measures wins; a newer pick supersedes it.

**Rows are folded by file id** (`subtitles.row_identity`), and a remembered pick
is matched by it, so a pick remembered under the addon's url is found under the
site's.

**The panel's slots are bound methods.** A rebuilt panel's hover animations and
the player page's own timers were wired to lambdas the cyclic collector could
free while they ran (python313 +0x6955b, COPY_FREE_VARS). The corners are taken
down by `_close_panel` rather than a slot on `destroyed`.

**A close skips Qt's teardown**, which crashed (Control Flow Guard in
`~QApplication`, and sip's object-map walk with the player open): threads are
joined, the indexes and the log flushed, the crash record removed, and the
process ends with `TerminateProcess` (`os._exit` deadlocked in a DLL detach).

**The crash record writes the raising thread only.** With every thread, the
dump of LuaJIT's handled 0xe24c4a02 walked running threads' frames and itself
killed the video child.

---

## 6. Configuration and user data

Unchanged from 3.23. A synced delay is stored in the episode's existing
subtitle preferences.

## 7. External interfaces

Unchanged from 3.23.

## 8. Installation and removal

Unchanged from 3.23 §8.

---

## 9. Known limitations

Carried forward from 3.23 §9, and:

- **Subtitle timing has nothing to measure against** for a debrid or direct
  link (no file on this disk), most .mp4 files, or any file without a text
  subtitle track; the delay is left alone there.
- **A steady drift** (a frame-rate mismatch) is rejected, not corrected.
- **A shown secondary embedded track moves with Delay**, as it did with a hand
  nudge before, so a sync shifts it too.
- **`torrent_engine.resolve_start` does not follow a two-hop SeekHead**, so the
  seat band is not armed for such a file; playback still seeks there.

---

## 10. Verification performed

**Subtitle sync, source:** the matcher against the real S04E08 file and its
three Arabic files and own English (numbers in §5); the player's real
`_start_sub_sync` / `_sub_sync_worker` / `_on_sub_synced` / `_keeps_own_delay`
on a stub page in ten cases - applied and saved, other cut, control, kept own
delay, no local file, hand edit meanwhile, superseded, index not on disk, the
saved prefs, and the addon/site url of one file. Live search: 10 rows to 7, the
three Arabic named.

**Subtitle sync, frozen build:** on a copy of his data on the second monitor,
silent: S04E08 on the TURG release, Arabic picked at 27:28 - Delay −50.2 s and
the toast 0.2 s later, the log's `subtitle sync: synced: shift -50.22s, 518 of
537 lines (96%)`, and the line timed to 28:17.9 on screen at 27:28; each Arabic
file listed once; `player_state.json` holding the delay with the pick.

**Panel and close fixes:** as measured when they were made (14-15 November):
the freed-lambda fault reproduced under `-X dev` and gone with bound methods;
corners 4 of 4 left with a collection before, 0 after; 6 of 6 frozen closes
exited 0 in 0.1-0.2 s.

**Release build:** Defender clean on the app exe, the bridge and the asset; the
archive read back for 3.24 in both version places and the new code in every
changed module.

**Not exercised:** the "other version" toast and a kept own delay on the frozen
build (harness only); a debrid play; the fixes of 14-15 November on this build
beyond its launch and the S04E08 run.
