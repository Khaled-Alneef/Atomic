# Atomic — Version Description Document

**Version 3.26** · 19 November 2026

---

## 1. Identification

| | |
|---|---|
| System name | Atomic |
| Version | 3.26 |
| Release date | 2026-11-19 |
| Repository | https://github.com/Khaled-Alneef/Atomic |
| Target platform | Windows 10 / 11, 64-bit |
| Delivered as | `Atomic.zip` — one **GitHub release asset** holding the ~11 MB **bridge installer** as its only `Atomic.exe`, and the app's folder packed as `app.zip` |
| At the tag | `Atomic.exe` / `Atomic.zip` — the bridge, for installs predating 2.0 |
| Asset size | 136,231,993 bytes |
| Asset SHA-256 | `b2ac1b1e0feaca4834ae111b4630422c8cb7181f8615b245024bc420be6498c1` |
| App exe SHA-256 | `1a6abc0b541d692b5b0f42e0dc1f42fe07527d24235db5c3e785ff479642473a` (`app\Atomic.exe`, 8,452,947 bytes) |
| Bridge SHA-256 | `7297d6d3e137e3e25d8d06ffc0b519f1876c8c80f62c20085a6f97447c2c14fd` (11,179,989 bytes, at the tag). The asset's bridge (11,180,390 bytes) is a separate build of the same source; PyInstaller is not byte-identical twice |
| Preceded by | 3.25 — `docs/VDD-3.25.md` |

---

## 2. System overview

Unchanged from 3.25.

---

## 3. Inventory of materials released

As 3.25 §3. No module added or removed.

**Changes since the last release** (`git log released/3.25..development`):

| Commit | Summary |
|---|---|
| f3407db | Back from the reader no longer shows the page underneath first |
| d5bf2bb | The chapter list keeps its rows when the reader closes |
| 21884c4 | An arrow press in the player no longer re-parses the seek index |
| 134e65d | A torrent being watched is never paused by libtorrent's queue |
| dfb8cad | Atomic 3.26 |

---

## 4. What this version provides

**Back from a chapter lands on the chapter list.** No frame of the main page
first, and no frame of an empty list.

**The arrow keys in the player move at once.** Each press on a torrent stream
used to spend about 130 ms on the UI thread before the seek was sent.

**Skipping ahead on a slow torrent waits only for the data.** A torrent being
watched is no longer paused between presses, which cost the owner a 40 s
"Skipping To..." on a 2.3 MB/s lane.

---

## 5. Design notes

**The page underneath.** Five of the seven callers of `details.open_details`
(the bell, search results, the Qt tracker's three, a genre page's card) never
registered the chapter list as an overlay, so the reader over it took the
overlay count to 1 and its close to 0, and Home's web view was shown over the
list until the 150 ms geometry tick (113 ms, probed). `open_details` now
registers through `web_pages.overlay_over`, which prepares the first paint
before the views go down; the two callers that swap a view themselves pass
`register=False`, and `overlay_opened` counts a page once.

**The empty list.** `_on_overlay_closed` rebuilds every row in the turn the
reader hides, and `insertWidget` only posts each row's show, after the
window's paint. `_materialise_rows` shows the rows and activates the layout in
the same turn.

**The arrow keys.** `_seek_absolute` asks `torrent_engine.seat_on_disk`, which
walked the whole Cues element in Python on every press (131.8 ms median, 102 ms
of it the walk, on an 868 KB index). The Cues are parsed once per file into a
bisectable table (`_cue_table`/`_cue_lookup`, same answer at 3,000 of 3,000
positions) and parsed off the UI thread at the first frame
(`warm_seek_index`, logged "seek index:"): 0.03 ms a press.

**The pause.** Everything outside a streaming window is priority 0, so a
complete window makes libtorrent count the torrent as finished, queued like a
seed, and the session has `active_seeds` 0 — the torrent was paused and every
peer dropped. `_Torrent.hold_off_queue` takes a torrent off the queue while a
reader is on it or was in the last 120 s, and hands it back after. Raising
`active_seeds` was measured to fix it too and was not taken: it would keep
complete torrents seeding in the background.

---

## 6. Configuration and user data

Unchanged from 3.25.

## 7. External interfaces

Unchanged from 3.25.

## 8. Installation and removal

Unchanged from 3.25 §8.

---

## 9. Known limitations

Carried forward from 3.25 §9, and:

- **A forward press past what a slow lane has sent still waits for that
  piece** at the lane's rate (3–13.5 s for a 4 MiB piece at 0.6 MB/s).
- **A torrent left unread for 120 s goes back on libtorrent's queue** and can
  be paused again; the next read takes it off within a second.
- **Debrid links and finished files on disk** never paid the arrow-key cost;
  any delay felt there is something else, not measured.

---

## 10. Verification performed

**Page underneath:** probed on the source tree through the bell's own
function: overlay depth 0 and Home's view shown 113 ms on every reader close
before (control run alike), depth 1 → 2 → 1 and Home's view never shown after.
Frozen build: banner list, three chapter closes and back to Home, sampled and
photographed.

**Empty list:** frozen build, screen sampled at the panel's rate through five
reader closes: 0 empty-list frames, against 3 of 3 before.

**Arrow keys:** real engine, a local seeder of his Attack on Titan S04E22:
`seat_on_disk` 131.8 ms median before, 0.03 ms after, 0.1 ms on the first press
after the warm-up. The first frozen run caught a shadowed method name
(`_engine_hash`) raising at the first frame; renamed, rebuilt, and the next
frozen run played from the swarm with the warm-up logged and five presses
applied.

**Pause:** real engine and libmpv, a local seeder throttled to 0.6 MB/s, four
+5 s presses: control — three "torrent paused" disconnects and a press still
not playing after 60 s; after — no pause in two runs, every press landing in
the piece's download time. A stub check of the hold and the hand-back. Frozen
build on the live swarm: the hold logged, five presses applied, playback
continuous.

**Release build:** Defender clean on the app exe (twice), the bridge and the
asset; the archive read back for 3.26 in both version places, the 3.26 notes,
and the new code in every changed module.

**Not exercised:** a slow lane on the live swarm (the swarm was fast on the
frozen run); the bell's own row press on the frozen build (it reads the real
pointer). The owner tested the development build before approving it.
