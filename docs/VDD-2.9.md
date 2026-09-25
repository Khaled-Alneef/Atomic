# Atomic — Version Description Document

**Version 2.9** · 25 September 2026

---

## 1. Identification

| | |
|---|---|
| System name | Atomic |
| Version | 2.9 |
| Release date | 2026-09-25 |
| Repository | https://github.com/Khaled-Alneef/Atomic |
| Target platform | Windows 10 / 11, 64-bit |
| Delivered as | `Atomic.zip` — one **GitHub release asset** holding the ~11 MB **bridge installer** as its only `Atomic.exe` and the application **as a folder**, packed as `app.zip` (`Atomic/Atomic.exe` + `Atomic/_internal/`) |
| At the tag | `Atomic.exe` / `Atomic.zip` — the same bridge, for installs predating 2.0 |

---

## 2. System overview

Unchanged in purpose from 2.0 — see `docs/VDD-2.0.md` §2. Unchanged in shape
from 2.7 — folder install, bridge transition — see `docs/VDD-2.7.md` §2/§5.

---

## 3. Inventory of materials released

| Item | Description |
|---|---|
| `Atomic.zip` (release asset) | **137,499,914 bytes**. SHA-256 `7a1c9a38dd9f19cf6b62dbbbd47a2ab68967facb152d6a1c3783dc1f04e847a4` |
| `app.zip` (inside it) | The application folder, **126,610,677 bytes**. SHA-256 `1ff12b01efad8b2abcd9c490f71f8c58c0f60f3966b323e4c62e073e6bf712c4` |
| `Atomic/Atomic.exe` (inside app.zip) | The folder build's launcher, **8,127,564 bytes**. SHA-256 `bc1dd61d98ced191c8a6f1b22a723fd1ad1a200eb15882695899bee6255e7a4a` |
| `Atomic.exe` (inside Atomic.zip, and committed at `v2.9`) | The bridge installer, **11,129,265 bytes**. SHA-256 `71158f4d76b0065799ad44b0c469ade5931bf6b0f58d197e2c53ecebb7dde30b` |
| `Atomic.zip` (committed at `v2.9`) | The bridge alone, zipped, for installs predating 2.0: **10,889,147 bytes**. SHA-256 `93861813068932fad95779f74144ed3f1088e50bfc29245c5eeee0fa9f31f600` |
| `src/`, `packaging/` | Full source; `packaging/build.py --zip` writes the asset |
| `docs/VDD-2.9.md` | This document |

Built with CPython 3.13.15, PyInstaller 6.22.3, PyQt6 6.11.0, on the
machine 2.8 was built on.

---

## 4. What this version provides

- **The next episode of a season pack plays cleanly.** Reported: mid-
  episode the video skipped seconds, a seek back would not go back, and
  the picture broke into bad frames while the sound stayed good - all
  cleared by a refresh. The player moves through a pack by switching the
  file it serves on the torrent it already holds, and `served_hwm` (the
  mark below which `_serve` reads straight from the file, trusting that
  those bytes were already handed out once) was one number per torrent.
  The next episode inherited the last one's mark, so its freshly
  completed pieces were read from the file before libtorrent had written
  them - zeros, streamed as video. The mark is per file now, and a zero
  read under it goes through `read_piece` instead.
- **The file fallback no longer streams unwritten blocks.** When
  `read_piece` answers nothing, `_serve` reads the file; a read holding a
  whole 16 KB block of zeros is held back for up to `FALLBACK_SETTLE_S`
  (3 s) while libtorrent is asked again, and served as the file stands
  after that, so a genuinely zero stretch never stalls playback. The
  failure itself is a log line now.
- **mpv's own warnings and errors reach `atomic.log`.** The video process
  had no log handler, so a demuxer resync or a decoder error left nothing
  behind. They are forwarded to the parent now, the same text at most
  once per 10 s and no more than 30 lines a minute.

---

## 5. Design notes

See `docs/VDD-2.7.md` §5 for the folder/bridge design, unchanged here.

What makes zeros look like this and not like a crash: a Matroska
demuxer meeting a zeroed stretch reports the file corrupt, resyncs to
the next cluster and carries on, so the clock jumps forward; the HEVC
decoder then has no reference frames until the next keyframe and draws
garbage; the audio, a separate small stream, resumes at once. A refresh
re-reads the same bytes seconds later, after they are on disk.

The child's socket to the parent now takes a lock per message: replies
go out from its read loop, events, properties and now log lines from
mpv's event thread.

---

## 6. Configuration and user data

No change from 2.7 — see `docs/VDD-2.7.md` §6.

---

## 7. External interfaces

No change from 2.7 — see `docs/VDD-2.7.md` §7.

---

## 8. Installation and removal

No change from 2.7 — see `docs/VDD-2.7.md` §8.

---

## 9. Known limitations

Unchanged from 2.7 — see `docs/VDD-2.7.md` §9. Whether the owner's
second report (below) came through the file fallback is not proven; the
new log lines say so the next time it happens.

---

## 10. Verification performed

Method: `.claude/rules/testing.md`.

**The per-file mark**: the real `_Handler` over a fake two-file pack
whose second file was still zeros on disk while `read_piece` held the
real bytes: the second file served **100% zeros** before the fix, **0%**
after; the control (the same file served first) 0% both times. The
already-served shortcut still skips `read_piece` on a re-read (0 calls).

**The second report**, on the build with the mark fixed - S2E6 of the
same pack, broken picture and good sound at about 3:36, nothing in the
log. The downloaded file decodes clean from 2:30 to 4:40 (6,710 frames,
`d3d11va-copy` and software, no decoder or demuxer complaint). The same
file with 2 MB zeroed at 30 MB reproduced the whole report: "Corrupt
file detected. Trying to resync", the clock 106.0 s -> 119.6 s, "hevc:
Could not find ref with POC ..." with the audio carrying on.

**The fallback guard**: a block written 1 s after the read served correct
at 1.04 s; a genuinely zero block served at 3.02 s; with the settle at 0
(the old behaviour) 32 KB of zeros went out.

**The mpv log**: through the real child process from the source tree,
over the damaged copy: the resync and decoder errors reached the parent's
log, 27 lines for 100+ raw messages; a missing file's open error as the
positive control.

**The owner** tested the frozen build over several pack episodes and
approved it before this release was tagged.

**Build gates**: `check_release_notes.py` — 2 notes for 2.9; `build.py`
— 1,609 entries, 32 bundled files byte-identical to `src/`; the bridge
built and passed its selftest; the release exe read back carries "2.9",
`_zero_block` and the 2.9 notes.

**Defender**: `WinDefend` running; `MpCmdRun -Scan -ScanType 3
-DisableRemediation`, twice each, over the app launcher, the release
asset `Atomic.zip`, and the bridge committed at the tag (taken out of the
asset) — exit 0 every time.

**Not exercised before publishing**: a real update through GitHub from
an installed 2.8 (checked after publishing); any machine but the owner's.

---

## 11. Glossary

See `docs/VDD-2.0.md` §12.
