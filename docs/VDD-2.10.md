# Atomic — Version Description Document

**Version 2.10** · 26 September 2026

---

## 1. Identification

| | |
|---|---|
| System name | Atomic |
| Version | 2.10 |
| Release date | 2026-09-26 |
| Repository | https://github.com/Khaled-Alneef/Atomic |
| Target platform | Windows 10 / 11, 64-bit |
| Delivered as | `Atomic.zip` — one **GitHub release asset** holding the ~11 MB **bridge installer** as its only `Atomic.exe` and the application **as a folder**, packed as `app.zip` (`Atomic/Atomic.exe` + `Atomic/_internal/`) |
| At the tag | `Atomic.exe` / `Atomic.zip` — the same bridge, for installs predating 2.0 |
| Asset size | 137,903,336 bytes |
| Asset SHA-256 | `aba9073fc50bf8a8a21ea099b71ac4b51edb028f68239fb440bd4cc2dd638ab1` |
| Preceded by | 2.9 — `docs/VDD-2.9.md`, which like every VDD from 2.8 on lives on `main` only, in the snapshot it describes |

---

## 2. System overview

Unchanged in purpose and on-disk shape from 2.7 — see `docs/VDD-2.7.md`
§2. 2.10 is a single-defect release on the streaming path.

---

## 3. Inventory of materials released

Unchanged from 2.7 §3. One asset, `Atomic.zip`, named exactly that
because `updater.ZIP_NAME` and `atomic_setup.ZIP_NAME` both look for it;
an asset under any other name is invisible to every install.

**Changes since the last release** (`git log released/2.9..development`):

| Commit | Summary |
|---|---|
| `4c345d1` | A re-read below the served mark no longer streams an unwritten block |

---

## 4. What this version provides

One user-visible fix.

**Skipping forward while an episode is still downloading no longer
freezes the picture while the sound runs on ahead of it.** The owner, 26
September 2026, pressing the right arrow several times back to back: *"the
video gets stuck on exactly 8:00 and the sound jumped like 5 sec ahead ...
then the video continued playing on 8:12"*. The picture would recover
several seconds later with broken frames in between.

---

## 5. Design notes

`torrent_engine._serve` keeps `served_hwm`, a per-file high-water mark
meaning *these bytes were handed to a reader once already, so read them
straight from the file rather than paying `read_piece`'s alert round
trip*. The mark exists for re-reads, and the re-read it was written for
is the one an embedded-subtitle switch causes (2.7 §5).

The defect is that **the mark records "served", not "written"**. It
advances at the write, and most bytes are served out of `read_piece` —
libtorrent's memory — for a piece that completed seconds earlier. The
file holds such a piece as zeros for ~0.5s after `have_piece()` turns
true. So a re-read below the mark can still cross an unwritten block.

mpv re-reads below the mark on **every exact seek**: `precision="exact"`
decodes forward from the keyframe *before* the target. Pressing the right
arrow repeatedly at the download edge does that over and over, on pieces
that landed one to six seconds ago.

The guard on that path rejected only a read that was zeros *whole*
(`not data.strip(b"\x00")`), so a read with a real head and an unwritten
tail went out as video — and the read runs to the end of the piece, which
is megabytes of room for one. It now also applies `_zero_block`, the
fallback path's own test on libtorrent's 16 KB write grid, and falls
through to `read_piece`. A genuinely zero-filled stretch (an mkv Void)
still reaches the viewer: `read_piece` answers it, and if it cannot, the
fallback below serves the file's own bytes after `FALLBACK_SETTLE_S`.

That fast path was the only road in `_serve` with no log line, which is
why the incident left nothing on Atomic's side while mpv filled the log.
It now writes one, once per piece.

This completes the work begun in 2.9 (`2416dd2`), which closed the
per-torrent mark and the `read_piece`-failed fallback but left this road
open.

---

## 6. Configuration and user data

Unchanged from 2.7 §6.

## 7. External interfaces

Unchanged from 2.7 §7.

## 8. Installation and removal

Unchanged from 2.7 §8.

---

## 9. Known limitations

Carried forward from 2.7 §9, plus one this release names rather than
fixes:

- **`served_hwm` still records "served", not "written".** 2.10 narrows
  the guard on the reads that trust the mark; it does not change what the
  mark means. If this signature returns — mkv resyncs, `hevc: Could not
  find ref with POC`, an audio-PTS jump with the sound running on — the
  mark's own semantics are the next place to look.
- Releases remain unsigned; roadmap #8 (code signing) is the durable
  answer to Defender's ML verdicts.

---

## 10. Verification performed

**The report, reproduced from his own log.** `%APPDATA%\Atomic\atomic.log`,
The Angel Next Door Spoils Me Rotten S1E10, torrent `b4a44f10`, with every
piece arriving 1.3–6.1s late:

```
16:25:55,624  [ad] Invalid audio PTS: 481.094500 -> 485.534500
16:25:55,708  Audio/Video desynchronisation detected!
16:25:55,2xx  hevc: Could not find ref with POC 111 … 197
16:21:45 / 16:22:03 / 16:22:25  [mkv] Corrupt file detected. Trying to resync
```

481.09s is 8:01; the jump is 4.44s, the "~5 sec" he heard. This is the
picture 2.9's own commit reproduced deliberately by zeroing 2 MB of a good
file, so a road to zeros was still open.

**The defect, measured through the real handler.** `_Handler._serve`
driven over a stub torrent, one piece carrying two unwritten 16 KB blocks
below the mark. The control is the same run with `_zero_block` forced
False, which is exactly the guard as it shipped in 2.9:

| Run | Wrong bytes | `read_piece` asked for |
|---|---|---|
| before (2.9's guard) | 32,768 | — |
| after | 0 | piece 2 only |
| mkv Void (file really is zeros) | 0, served in 0.00s | piece 2 only |

The middle row carries two findings: the defect is closed, **and** only
the piece that actually held an unwritten block fell through — the other
three still came from the file, so the re-read the mark exists to keep
fast stays fast.

**The frozen build, read back out of its own archive** rather than
trusted from a build log: `unwritten block in piece`, `held_piece`,
`asking libtorrent for it instead`, `_zero_block` referenced twice in
`helpers.torrent_engine`, and the version string.

**Defender**, `MpCmdRun -Scan -ScanType 3 -DisableRemediation`, service
confirmed Running first: exit 0 on the bridge `Atomic.exe`, on the app
`Atomic.exe` and on `Atomic.zip`, and the two executables re-scanned once
to confirm the verdict is stable.

**The release-notes gate**, `packaging/check_release_notes.py`: one note
written for 2.10.

**Version ordering checked before building.** 2.10 is the first version
whose second part exceeds 9, which is where a string or float comparison
would read it as older than 2.9. Every reader goes through
`updater.parse_version`, which returns a tuple of ints — `(2, 10) > (2, 9)`
— and the bridge's `atomic_setup` carries the identical implementation.

**Not measured.** The fix was not caught live on a real swarm: a piece's
disk write cannot be delayed on demand, so the harness above is the proof
of that path. The `read_piece`-answers-nothing branch below it was not
exercised by this release's harness either; it is unchanged from 2.9.

---

## 11. Glossary

Unchanged from 2.7 §11.
