# Atomic — Version Description Document

**Version 2.13** · 28 September 2026

---

## 1. Identification

| | |
|---|---|
| System name | Atomic |
| Version | 2.13 |
| Release date | 2026-09-28 |
| Repository | https://github.com/Khaled-Alneef/Atomic |
| Target platform | Windows 10 / 11, 64-bit |
| Delivered as | `Atomic.zip` — one **GitHub release asset** holding the ~11 MB **bridge installer** as its only `Atomic.exe` and the application **as a folder**, packed as `app.zip` (`Atomic/Atomic.exe` + `Atomic/_internal/`) |
| At the tag | `Atomic.exe` / `Atomic.zip` — the same bridge, for installs predating 2.0 |
| Asset size | 137,544,206 bytes |
| Asset SHA-256 | `4ac30a5cf9743af07c7e6ec513bd40081f4d4b028f60b2b3f23c8bea470e8db7` |
| Preceded by | 2.12 — `docs/VDD-2.12.md` |

---

## 2. System overview

Unchanged in purpose and on-disk shape from 2.7 — see `docs/VDD-2.7.md`
§2. 2.13 carries a video-timing fix and a player-bar label fix. One new
file can appear in the data folder, `video_sync.json` (§6).

---

## 3. Inventory of materials released

Unchanged from 2.7 §3. One asset, `Atomic.zip`, named exactly that
because `updater.ZIP_NAME` and `atomic_setup.ZIP_NAME` both look for it.

**Changes since the last release** (`git log released/2.12..development`):

| Commit | Summary |
|---|---|
| `52c82dc` | A picture running away from its sound starts and stays in video-sync=audio |
| `ade9f44` | The speed button grows to fit its number - "0.25x" was cut off |
| `3eb77e1` | Atomic 2.13 |

---

## 4. What this version provides

**Video plays at its own speed on machines that could not pace it.** A
friend's first run on a Windows 10 machine: *"the vid player was always
on X2 even if we change the playback speed!"*, the sound normal.

**The speed button shows its whole number.** The owner's screenshot read
".25x" at 0.25x speed, on every device.

---

## 5. Design notes

**The runaway.** The player's sync mode is `video-sync=display-resample`
(measured 4 September 2026: 14.6% of frames uneven under mpv's default,
0.2% under display-resample, on the owner's 240Hz panel). It presents one
frame per counted vsync; where a present is not paced by the display the
picture runs at whatever rate the presents go, and `speed` cannot touch
it. Reproduced with `d3d11-sync-interval=0`: the picture advanced 30.8s
in 10s of wall clock against the sound's 10.0s, avsync −26.6s; under
`video-sync=audio` 10.01s / 10.00s, avsync 0.000. His log carried mpv's
"Audio/Video desynchronisation detected!" within half a second of the
first frame on all six opens, and no `player cadence:` line — the
existing watch looks for a moving display rate and lost frames, and a
runaway loses none.

Three test pre-releases on his machine (test-2.12.1 to test-2.12.3)
shaped the fix:

- Catching it from avsync after the fact put it right after ~2s of fast
  picture, on every episode (each is a new mpv child).
- In audio mode mpv reports no display timing at all: a paced and an
  unpaced display read identically there.
- His display estimate read right (180Hz against 180Hz, 60 against 60)
  while the picture ran away; what was wrong was vsync-ratio, 68–82
  where 7.5 is right, and the picture-to-sound gap.

So an mpv core now **starts in `audio`** (`video_backend.default_options`),
and at the first frame `PlayerPage._begin_smooth_sync` moves it to
display-resample and watches one second: the picture more than 0.25s from
its sound (a paced display held −30..+61ms, an unpaced one 306–527ms by
83ms), the estimate outside 0.8–1.25× the display's rate, or vsync-ratio
over 2.5× its right answer (paced up to 1.6× while the average settles;
his 9–11×) sends it back to audio. The verdict is written to
`video_sync.json` under the computer's name, and every later open on that
machine never leaves audio. `_watch_runaway` (avsync above 0.5s and
widening on two samples) stays behind it for a display that changes
later; a gap over 0.3s is re-seated at the sound, because switching alone
froze the picture while the sound caught up (4s at a 3.8s gap).

**The speed label.** The button is a 40px square, the owner's ask of 7
September to match the icons beside it, and "0.25x" at 14pt is wider.
`_show_speed_label` keeps the square for anything that fits and grows
sideways for the rest; the stretch to its left takes the difference, so
nothing to its right moves.

---

## 6. Configuration and user data

As 2.7 §6, plus **`video_sync.json`**: `{computer name: {audio, detail,
at}}`, written once when a machine fails the first-frame check. Deleting
it makes that machine try display-resample again on its next episode.
Keyed by machine because a data folder can be carried between computers.

## 7. External interfaces

Unchanged from 2.7 §7.

## 8. Installation and removal

Unchanged from 2.7 §8.

---

## 9. Known limitations

Carried forward from 2.12 §9, plus:

- On a machine that fails the check, the **first** episode ever shows up
  to ~0.2s of fast picture before it is caught (measured 195–234ms of
  gap at worst); every later episode starts in audio.
- A machine in audio mode gives up display-resample's even cadence —
  which an unpaced display was never delivering.

---

## 10. Verification performed

**Real player methods over the app's libmpv**, a real QTimer, the verdict
file in a temp directory: paced display twice — kept display-resample,
worst gap 60 / 62ms, 1.00×; unpaced with the estimate made to read right
(his signature) — back to audio at 78ms on the gap, worst 198ms, then
1.00×; unpaced — back at 62ms; next episode with the verdict on disk —
never left audio, worst 47ms. Normal playback and a +30s seek never
tripped the avsync watch.

**His machine**, test-2.12.1 to test-2.12.3: the avsync watch fired
(`player runaway:` lines in his log) and the picture returned to normal
speed. His report on test-2.12.3 had not come back when this was
released — the widened first-frame check is proven on the reproduction
above, not yet on his hardware.

**The speed label**, photographed in the real player window (source
tree, a true copy of the owner's data, 125%): 1× 40px, 0.25× 60px, 1.5×
47px, 3.95× 60px, 4× 40px, the right edge at 1571 in every case, and
"0.25x" whole on screen.

**The frozen build, read back out of its own archive:** `windows.player`
names `_begin_smooth_sync`, `SYNC_PROBE_GAP_S`, `_show_speed_label`;
"2.13" in `updater` and `development_version_patch`; the 2.13 note in
`whats_new`.

**Defender**, `MpCmdRun -Scan -ScanType 3 -DisableRemediation`, service
Running: exit 0 on the app `Atomic.exe`, the bridge `Atomic.exe` and
`Atomic.zip`, each scanned twice.

**The release-notes gate:** two notes written for 2.13.

**Not measured by me.** The frozen build driven through an episode with
screenshots; a Windows 10 machine.

---

## 11. Glossary

Unchanged from 2.7 §11, plus:

| Term | Meaning |
|---|---|
| display-resample | mpv presenting on the display's clock and resampling audio to it |
| runaway | the picture advancing faster than its sound under display-resample |
