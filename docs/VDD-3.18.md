# Atomic — Version Description Document

**Version 3.18** · 6 November 2026

---

## 1. Identification

| | |
|---|---|
| System name | Atomic |
| Version | 3.18 |
| Release date | 2026-11-06 |
| Repository | https://github.com/Khaled-Alneef/Atomic |
| Target platform | Windows 10 / 11, 64-bit |
| Delivered as | `Atomic.zip` — one **GitHub release asset** holding the ~11 MB **bridge installer** as its only `Atomic.exe`, and the app's folder packed as `app.zip` |
| At the tag | `Atomic.exe` / `Atomic.zip` — the bridge, for installs predating 2.0 |
| Asset size | 136,129,687 bytes |
| Asset SHA-256 | `2016742bc71ee43f896f17afc5ffbb194e3c5da13d4b7cd5c30df014a86301bd` |
| Bridge SHA-256 | `d89105d2fea0cea7077bdc0cdcddcec6a478fad870e3f7fc5549be47c4b32b5a` (11,180,196 bytes). The asset's bridge is a separate build of the same source (11,180,191 bytes); PyInstaller is not byte-identical twice |
| Preceded by | 3.17 — `docs/VDD-3.17.md` |

---

## 2. System overview

Unchanged from 3.17.

---

## 3. Inventory of materials released

Unchanged from 3.17 §3.

**Changes since the last release** (`git log released/3.17..development`):

| Commit | Summary |
|---|---|
| 0d71b8d | The player's windows keep their corners over the loading frame |
| a3d87a2 | A source switch on a stuck torrent no longer freezes the window |
| (this)  | Atomic 3.18 |

---

## 4. What this version provides

**Changing the source while an episode is stuck loading no longer freezes the
app** - which is what read as a crash when Sources was opened on a stalled
Tomodachi Game episode.

**The player's windows keep their corners over the loading frame**, from the
first frame they are open: no lighter, tinted squares at the corners, no white
flash.

**The line beside the Next-season arrow in the episode list is gone**, and the
arrow keeps its whole border, at 125% and at 100%.

**A frozen window says where it is frozen** in `atomic.log` (`ui stall:` lines).

---

## 5. Design notes

**The freeze.** Windows recorded both of the owner's "crashes" as hangs
(Application Hang 1002, AppHangB1) of the installed 3.17. mpv cancels the stream
it replaces and waits for its read; `_EngineStream.cancel` closed the urllib
response, which leaves the socket open, so a read on a 0.00MB/s swarm waited up
to the engine's 45s `PIECE_WAIT_S`, the video child's one request loop waited on
mpv, and every synchronous read from the UI thread waited the proxy's 8s.
Measured with a real child and a server that never sends a byte: `get path` and
`get duration` after a switch 8.00s each, 0.00s before. `cancel()` now shuts the
socket down (`player_seekable_stream_patch._abort`, engine and direct streams):
0.09-0.11s, two runs from source and two on the built exe's own `--mpv-host`.

**The stall watch** (`logs.start_stall_watch`, always on). The UI thread stamps a
beat at 4Hz; a watcher samples its stack every 0.1s into a ring, warns once the
beat is 1.5s late, and on recovery logs the stall's length and its samples by
place. A staged 2.6s freeze (1.0s sleeping, 1.6s busy) logged "2.6s ... 15x
busy_wait || 10x freeze".

**The corners.** A window's DWM alpha blends it against the page, never against
the backdrop, which is another native child: a panel at 180 read a flat 20-21
while the picture behind it ran 40-105. Its smooth corners are per-pixel layered
windows and do blend with the backdrop, so each corner showed the picture at
70% (40,36,34 beside 21,21,21). Over the loading frame the windows are solid
(`_over_loading_frame`, the pause rule's own reason). A first show is painted at
alpha 0 and revealed with its corners on (`_reveal_veiled`); since the pieces
land a composition after the window whatever the order (4ms, 8ms with a
DwmFlush, 20-30ms staged), a solid window keeps a disc one pixel inside the
curve in its own region (`_smooth_cut`). Four openings after: backdrop, then the
finished corner, nothing between; the corner pixel-identical to the build before.

**The line.** A corner piece stretched a 1px column of the panel rendered with
its children across the corner; at 125% the arrow's top border lay in it.
Reproduced at `QT_SCALE_FACTOR` 1.25 (row 82, x 479-498). The column is the
owner alone now, and the episode list's top and bottom margins are the corner
radius (16), so nothing sits inside a corner's square.

---

## 6. Configuration and user data

Nothing new is written.

## 7. External interfaces

Unchanged from 3.17.

## 8. Installation and removal

Unchanged from 3.17 §8.

---

## 9. Known limitations

Carried forward from 3.17 §9, and:

- **Opening a window over a playing video** still uses the square corner cut (the
  window is see-through there), so its first frame may show a bare corner for one
  composition. The loading frame, which was reported, is fixed.
- **A 2.6s freeze on a card press**, caught once by the stall watch inside
  `storage.load` under `player.resume_point`, did not repeat on a rerun. Not
  changed; the stall watch will profile it if it comes back.
- **Tomodachi Game E12 loads slowly because its torrents do**: no release cached
  on Real-Debrid, swarms at 0.00-0.20MB/s against 0.46 needed, the one fast
  release stalled 45s twice on one piece. One attempt (02:30) read the head
  before the seat and was not explained from the log.

---

## 10. Verification performed

**The owner tested the builds and asked for this release.**

**The freeze, measured** without playback (no picture, no sound): a real mpv
child, a server that never sends, the UI thread's calls timed - 8.00s each
before the fix, 0.09-0.11s after, including against the built `Atomic.exe
--mpv-host`.

**On the frozen build, a copy of his data, on the 1080p monitor with his own
Atomic open and untouched:** the panels' corners over the loading frame,
photographed and sampled at the screen's rate (four openings, no bare frame);
the episode list's arrows at `QT_SCALE_FACTOR` 1.25 and at 100%, photographed;
the stall watch's first frozen-build line.

**The release build, read back:** "3.18" in `helpers.updater` and
`helpers.development_version_patch`, no "3.17" or "3.17.2"; `_abort`,
`start_stall_watch`, `_reveal_veiled`, `_smooth_cut`, `_over_loading_frame`
present; the 3.18 notes present. The app folder's exe inside `app.zip` is
byte-identical to the one built.

**Not exercised:** a real torrent switch on the frozen build (it plays audio on
his machine, rule 14), a window opened over a playing video, his 125% panel
itself, a real update through GitHub before publishing.

**Defender**, service Running: exit 0 on the app `Atomic.exe`, the bridge
`Atomic.exe` and `Atomic.zip`, twice each, full paths.

**The release-notes gate:** 3 notes written for 3.18.

---

## 11. Glossary

Unchanged from 3.17 §11.
