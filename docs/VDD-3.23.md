# Atomic — Version Description Document

**Version 3.23** · 14 November 2026

---

## 1. Identification

| | |
|---|---|
| System name | Atomic |
| Version | 3.23 |
| Release date | 2026-11-14 |
| Repository | https://github.com/Khaled-Alneef/Atomic |
| Target platform | Windows 10 / 11, 64-bit |
| Delivered as | `Atomic.zip` — one **GitHub release asset** holding the ~11 MB **bridge installer** as its only `Atomic.exe`, and the app's folder packed as `app.zip` |
| At the tag | `Atomic.exe` / `Atomic.zip` — the bridge, for installs predating 2.0 |
| Asset size | 136,207,327 bytes |
| Asset SHA-256 | `59112ff5c7ceab71a0b35d29210251001283e88c4c152ffced1b272622b924ff` |
| Bridge SHA-256 | `19ed4a006e17b5f204f4fd85146db513b9b4ccd67d14be72fbbde1d247825a91` (11,180,430 bytes). The asset's bridge (11,178,609 bytes) is a separate build of the same source; PyInstaller is not byte-identical twice |
| Preceded by | 3.22 — `docs/VDD-3.22.md` |

---

## 2. System overview

Unchanged from 3.22.

---

## 3. Inventory of materials released

Unchanged from 3.22 §3. No module added or removed.

**Changes since the last release** (`git log released/3.22..development`):

| Commit | Summary |
|---|---|
| 0cc336e | A seek no longer lands at the end of a part-downloaded episode |
| 731f0f9 | The player's Completed, loaded band and Source rows read what is playing |
| 637cc95 | Atomic 3.23 |

---

## 4. What this version provides

**A seek no longer jumps to the end of an episode still downloading.** A
release partly on disk was taken for a finished file and opened directly;
mpv seeking into its missing pieces read zeros and landed on the last frame.

**Completed only goes up.** It is the share of the playing file on disk for a
torrent, how far into the video is loaded for a debrid or direct link (100%
once that reaches the end), and 100% for a finished file.

**The seek bar's light part shows everything on disk ahead of the playhead**,
not only mpv's 64 MiB read-ahead. Loaded Time reads the same reach.

**The statistics name the source playing**: Torrent, Debrid, On disk or Direct
link. A debrid play read "Connecting..." throughout.

---

## 5. Design notes

**"Finished" is libtorrent's record, not the file system's.**
`torrent_engine.finished_file_path` asks the torrent's handle while it is in
the session, and the saved resume data's `have_pieces` otherwise; neither on
hand means the ordinary engine path. `_fully_written` (NTFS allocation, 2%
allowed missing) was removed: on a 240 MB sparse file it read 0.39% for a
complete file before the cache flushed and 100% with a 7 MB hole after, and on
the real engine with one 2 MB piece of 120 missing it said complete.

**Completed was the window's progress.** libtorrent's `status.progress` counts
only wanted pieces, and while streaming only a sliding window is wanted: it read
0% → 1.1% → 25% → 10% with 85% of the file on disk. `torrent_engine.on_disk`
counts the playing file's verified pieces, and finds the unbroken run on disk
from the playhead in one `status(query_pieces)` call (0.07 ms).

**The band is converted byte to time in proportion to the file**, exact for a
constant bitrate and close for a variable one, and drawn as the larger of that
and mpv's own `demuxer-cache-time`. The seek rule still reads mpv's reach alone.

**The panel follows what mpv opened**, recorded at load from the url (the
engine's address carries the hash) and the stream's `engine` / `local_file`,
never the source row's hash: a debrid stream keeps that hash for identity.

---

## 6. Configuration and user data

Unchanged from 3.22.

## 7. External interfaces

Unchanged from 3.22. The panel reads one more mpv property, `cache-speed`.

## 8. Installation and removal

Unchanged from 3.22 §8.

---

## 9. Known limitations

Carried forward from 3.22 §9, and:

- **A finished file with no saved resume data opens through the engine** rather
  than straight off disk: the shortcut now needs libtorrent's record, and a
  session that ended before saving one has none. Playback is unaffected; it
  takes the engine's few seconds.
- **The band's time is proportional to bytes**, so on a strongly variable
  bitrate its edge can sit some seconds off the true reach.
- **Completed for a debrid link can fall after a seek**, since mpv's buffer
  starts again at the new position, and on a large file it reaches 100% only
  near the end (mpv keeps 64 MiB).

---

## 10. Verification performed

**Real engine, local seeder:** the old Completed reading made 69 rises and 23
drops in a 50 s run; `on_disk` never fell and agreed with an independent scan on
every sample. `finished_file_path` with one piece of 120 missing → not finished
(the old check said finished); complete in session and by resume data → the
file; a resume blob missing a piece → not finished. The player's real
`_refresh_disk_reach` on a stub page: band at 1200 s against mpv's 720 s, mpv's
value kept past a gap, a debrid stream left to mpv; both url forms parsed.

**Statistics panel:** its real refresh driven on a stub page for a torrent,
debrid (loaded part, loaded to the end, no duration yet), a finished file, a
direct link, sourcing and an unopened torrent.

**From the friend's log:** the failure traced to the `won (on disk)` →
`landed at 2527.9s (asked 1131.8s)` sequence on an .mp4.

**Release build:** Defender clean on the app exe, the bridge and the asset; the
archive read back for 3.23, the new functions and the removed one; launched on
throwaway data on the second monitor with no errors in the log.

**Not exercised:** the player on screen (playback makes sound, so it is the
owner's to run), mpv's `cache-speed` through the video child process, and his
friend's release replayed end to end.
