# Atomic — Version Description Document

**Version 3.25** · 18 November 2026

---

## 1. Identification

| | |
|---|---|
| System name | Atomic |
| Version | 3.25 |
| Release date | 2026-11-18 |
| Repository | https://github.com/Khaled-Alneef/Atomic |
| Target platform | Windows 10 / 11, 64-bit |
| Delivered as | `Atomic.zip` — one **GitHub release asset** holding the ~11 MB **bridge installer** as its only `Atomic.exe`, and the app's folder packed as `app.zip` |
| At the tag | `Atomic.exe` / `Atomic.zip` — the bridge, for installs predating 2.0 |
| Asset size | 136,226,150 bytes |
| Asset SHA-256 | `9ce29ee4e4b6df22a152c1c897554c81fc3e7ec2f402ac45254d8804a350bc94` |
| Bridge SHA-256 | `0b2d50358da4e07121fbe4b26e37f755193c4cd9b837c56992be9479536fc968` (11,180,087 bytes). The asset's bridge (11,180,692 bytes) is a separate build of the same source; PyInstaller is not byte-identical twice |
| Preceded by | 3.24 — `docs/VDD-3.24.md` |

---

## 2. System overview

Unchanged from 3.24.

---

## 3. Inventory of materials released

As 3.24 §3. No module added or removed.

**Changes since the last release** (`git log released/3.24..development`):

| Commit | Summary |
|---|---|
| f60da05 | A source that stalls on a fresh open is walked away from |
| ecef44d | Atomic 3.25 |

---

## 4. What this version provides

**A source that stops sending on a fresh open is replaced.** An episode opened
from its start (Next, or a first play) used to wait on a dead swarm for as long
as the viewer did; it now moves to the next source on its own. A season pack
that is still delivering, however slowly, is waited for as before.

**The loading frame says what it is waiting for.** "Buffering... 100%" was
mpv's cache, which is full while the source has sent nothing; a torrent stream
now reads "Waiting for the source... N MB/s from N peers".

---

## 5. Design notes

**The report.** "It says Buffering 100% and stuck there for some time." His
log, Silicon Valley S06E05: the race kept `d862b4d1` at 0.00 MB/s (two lanes
dead, Real-Debrid answering 451 to all six releases), mpv read the tail's 32 MB
piece in 18 s and then waited on the file's head piece through three 45 s
give-ups. The stall watchdog in `player_resume_latency_patch` was armed only
when an open resumed at a seat, so a head open had none.

**The watchdog now runs on every open.** Every 12 s it asks whether the engine
stream delivered at least 256 KB/s across the tick: if so it waits, up to
120 s; if not it calls `_try_next_source` (seat kept when there is one) and
the frame says "That Source Stalled - Trying Another...". A direct link gets
one extra tick when its file is open. The first frame cancels it. Its log
lines read `open:` for a head open and `seat:` for a resume.

**Season packs are not ranked down.** Packs are often the only sources, so the
answer to a slow pack is detecting the stall, not where the pack sorts.

**A full cache no longer drives the words.** `_startup_snapshot` shows mpv's
percentage only below 100; at 100 an engine stream shows its payload rate and
peer count, a direct link "Starting playback...".

---

## 6. Configuration and user data

Unchanged from 3.24.

## 7. External interfaces

Unchanged from 3.24.

## 8. Installation and removal

Unchanged from 3.24 §8.

---

## 9. Known limitations

Carried forward from 3.24 §9, and:

- **A stall after the first frame is not walked away from.** The watchdog ends
  at the first frame; a swarm that dies mid-episode still shows the buffering
  logo until it recovers.
- **A source sending a trickle above 256 KB/s is waited for up to 120 s** before
  the next one is tried.
- **Real-Debrid refuses some releases with HTTP 451**, which the app cannot
  change; those play over the swarm.

---

## 10. Verification performed

**Watchdog, offscreen:** the real patched `load_into_mpv` on a stub page with a
fake engine torrent and the tick at 0.2 s: a dead swarm on a head open walked
to the next source at the first tick, with the stalled note; a delivering swarm
and a landed first frame were left alone. **Control:** the same harness on the
3.24 patch never walked.

**Loading text:** the real unbound `_startup_snapshot` at 100 % on an engine
stream ("Waiting for the source... 0.0 MB/s from 3 peers"), a direct link
("Starting playback...") and at 40 % ("Buffering... 40%").

**Release build:** Defender clean on the app exe, the bridge and the asset; the
archive read back for 3.25 in both version places, the 3.25 notes, and the new
code in both changed modules.

**Not exercised:** a live dead swarm on the frozen build (it cannot be made on
demand); the owner tested the development build before approving it.
