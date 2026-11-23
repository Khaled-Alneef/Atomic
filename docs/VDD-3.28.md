# Atomic — Version Description Document

**Version 3.28** · 23 November 2026

---

## 1. Identification

| | |
|---|---|
| System name | Atomic |
| Version | 3.28 |
| Release date | 2026-11-23 |
| Repository | https://github.com/Khaled-Alneef/Atomic |
| Target platform | Windows 10 / 11, 64-bit |
| Delivered as | `Atomic.zip` — one **GitHub release asset** holding the ~11 MB **bridge installer** as its only `Atomic.exe`, and the app's folder packed as `app.zip` |
| At the tag | `Atomic.exe` / `Atomic.zip` — the bridge, for installs predating 2.0 |
| Asset size | 136,238,916 bytes |
| Asset SHA-256 | `bbbc02ede57324f0699ed54690567f201484f17b915ab3b6ca28be61ce07fd25` |
| App exe SHA-256 | `a29083cca7fbdf79de9943b7c2201490669603005f369bdc1adc0b31561b1d16` (`app\Atomic.exe`, 8,458,709 bytes) |
| Bridge SHA-256 | `220e1fab94d8b9df9af423b27893dd4e56b32aaeae736e7137ff32f906d06c69` (11,179,765 bytes, at the tag). The asset's bridge (11,180,791 bytes) is a separate build of the same source; PyInstaller is not byte-identical twice |
| Preceded by | 3.27 — `docs/VDD-3.27.md` |

---

## 2. System overview

Unchanged from 3.27.

---

## 3. Inventory of materials released

As 3.27 §3. No module added or removed.

**Changes since the last release** (`git log released/3.27..released/3.28`):

| Commit | Summary |
|---|---|
| d317f7a | The freeze watcher no longer crashes the app mid-paint |
| 18b084b | Atomic 3.28 |

---

## 4. What this version provides

**A crash while drawing is gone.** The app could close on its own while
drawing — his of 23 November came 0.1 s after folding the sidebar on Home.
Windows had recorded the same kind of crash on 7, 11 and 13 November.

---

## 5. Design notes

**The cause was the freeze watcher** (`logs.start_stall_watch`, since 6
November). Ten times a second it took the UI thread's frame with
`sys._current_frames()` and kept it through its sleep. A sample that landed
inside a `paintEvent` kept that frame — and its `painter` local — alive after
the paint returned, and the painter was then freed on the watcher's thread,
ending Qt's shared painter while the UI thread was painting something else.
Windows' record: Qt6Gui `QRasterPaintEngine::renderHintsChanged+0x3d`
(23 November) and `fillRect+0x2f` (11 November), both reading a null painter
state; the first Qt6Gui crash in the log is the day after the watcher landed.

**Any reference to a running frame is a reference to its locals**, so the
watcher holds none now: `faulthandler.dump_traceback` writes every thread's
stack to a temporary file from C, and the UI thread's lines are read back as
text. Dropping the frame immediately after reading it was tried first and
still crashed — the paint can return while the frame is being read.
`lookup_pool`'s stuck-job stack dump did the same thing and reads text too.
The five paints that left their painter to close on return (the sidebar's
fold chevron, the player's seek bar and play button, the volume flyout,
`widgets`' painted frame) end it themselves.

---

## 6. Configuration and user data

Unchanged from 3.27.

## 7. External interfaces

Unchanged from 3.27.

## 8. Installation and removal

Unchanged from 3.27 §8.

---

## 9. Known limitations

Carried forward from 3.27 §9, and:

- **The crash was never reproduced on a real fold** — 150 rounds on a source
  run with the collector forced and 100 on the frozen build passed before and
  after alike; it needs a sample to land mid-paint while other threads hold
  the interpreter. The proof is the harness below and the matching
  instruction. If it returns, Windows' Application log names the same Qt6Gui
  offset.

---

## 10. Verification performed

**Mechanism, offscreen:** a frame held by another thread and dropped across a
sibling's paint faults at `renderHintsChanged+61` — Qt6Gui +0x1c11bd, his
instruction — on the first repaint; with the painter ended in its paint, 200
of 200 repaints survive.

**The watcher, offscreen, a busy worker thread forcing interpreter switches
(switch interval 0.2 ms, sampling every 0.5 ms):** the 3.27 watcher crashed 5
of 5 runs; the frame dropped right after reading still crashed; the text-only
watcher survived 3 of 3 runs of 20 s, every paint local freed on the UI
thread. A 2.5 s block is still reported with its stack (`blocker <
inner_caller < <module>`, 25 samples). The stuck-job dump reads the same as
before.

**Frozen build, a copy of his data, the other monitor:** 100 fold/unfold
rounds on Home (201 folds logged), no crash; the fold chevron photographed
open and folded.

**Release build:** Defender clean on the app exe, the bridge and the asset;
the archive read back for 3.28 in both version places, the 3.28 note,
`dump_traceback` in `helpers.logs` and no `_current_frames`.

**Not exercised:** the player's seek bar, play button and volume flyout were
not photographed after gaining their `end()`; each change only closes the
painter after its last draw call.
