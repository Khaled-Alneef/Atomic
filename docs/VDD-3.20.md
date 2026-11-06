# Atomic — Version Description Document

**Version 3.20** · 7 November 2026

---

## 1. Identification

| | |
|---|---|
| System name | Atomic |
| Version | 3.20 |
| Release date | 2026-11-07 |
| Repository | https://github.com/Khaled-Alneef/Atomic |
| Target platform | Windows 10 / 11, 64-bit |
| Delivered as | `Atomic.zip` — one **GitHub release asset** holding the ~11 MB **bridge installer** as its only `Atomic.exe`, and the app's folder packed as `app.zip` |
| At the tag | `Atomic.exe` / `Atomic.zip` — the bridge, for installs predating 2.0 |
| Asset size | 136,139,652 bytes |
| Asset SHA-256 | `79d3efb92027eb3298d23c6642a3da98525e52d5f1dd0d419535d845ea0b6cb4` |
| Bridge SHA-256 | `af7e9469ad06ebbc663a7847b897314f15e9aa632633f1d42e96414cb8ae2cbc` (11,180,386 bytes). The asset's bridge (11,180,229 bytes) is a separate build of the same source; PyInstaller is not byte-identical twice |
| Preceded by | 3.19 — `docs/VDD-3.19.md` |

---

## 2. System overview

Unchanged from 3.19.

---

## 3. Inventory of materials released

Unchanged from 3.19 §3.

**Changes since the last release** (`git log released/3.19..development`):

| Commit | Summary |
|---|---|
| 6eb8999 | Holding Space in the player plays at 2x, YouTube's way |
| 6b5f9eb | Holding the left button on the picture plays at 2x, like holding Space |
| (this)  | Atomic 3.20 |

---

## 4. What this version provides

**Hold Space in the player for 2x.** A tap still plays and pauses; a hold past
350ms plays at 2x for as long as the key is down, with a "2x" pill at the top
centre, and the release puts back the speed that was set before.

**Hold the left button on the picture for the same.** A click still pauses and
a double-click still toggles full screen; a press still down after 350ms is a
hold, and its release pauses nothing.

---

## 5. Design notes

**Space.** While playing, a tap can only be told from a hold at the release, so
the pause lands on key-up, as on YouTube; while paused the press plays at once
and holding on still goes to 2x. The player's keyboard relay forwarded only
presses, so it forwards Space's release now and nothing else's. A release that
went to another window is caught by reading the key's state while a hold runs
(`GetAsyncKeyState`), trusted only after the key was seen down, and never read
as a tap.

**The left button.** mpv's window is not Qt's, so a press on the picture is
seen by the player's 40ms button poll - `regression_fixes_148.poll_mouse`, which
replaces `player.PlayerPage._poll_mouse` outright, so that is where the hold
lives. The hold lands 350-390ms in. A press that would only dismiss a panel or
the episode list, a press a panel owns, the status box and a Space hold in
progress keep it off. The speed change and the pill are one implementation for
both holds (`player_top_bar_live_patch._boost`/`_unboost`, exposed to the poll
as two page methods). From a pause, a mouse hold plays at 2x and the release
leaves it playing, as Space does.

---

## 6. Configuration and user data

Nothing new is written.

## 7. External interfaces

Unchanged from 3.19.

## 8. Installation and removal

Unchanged from 3.19 §8.

---

## 9. Known limitations

Carried forward from 3.19 §9, and:

- **Neither hold was exercised on live playback** before release: playback
  makes sound, which is the owner's to run (CLAUDE.md rule 14).

---

## 10. Verification performed

**The owner asked for this release** after the Space build and the mouse-hold
build were handed to him.

**Harnessed offscreen on the real patched handlers.** Space: tap while playing
pauses, a hold with key repeat goes 2x and back, tap and hold while paused, a
hold from 1.5x, no mpv yet does nothing. Left button, the button state faked
under the real `_poll_mouse`: tap pauses, a hold goes 2x at 0.363s and back,
from 1.5x back to 1.5x, from a pause, a tap right after a hold is not a
double-click, a double-click still toggles full screen, a panel open, during a
Space hold, no mpv yet - 9 of 9.

**The release build, read back:** "3.20" in `helpers.updater`;
`MOUSE_HOLD_S`, `_atomic_mouse_hold` and `_atomic_speed_unboost` in
`helpers.regression_fixes_148`; `_atomic_speed_boost` and "mouse: left button"
in `helpers.player_top_bar_live_patch`. The app folder's exe inside `app.zip`
is byte-identical to the one built.

**Not exercised:** the holds on live playback, screenshots of the frozen
build, a real update through GitHub before publishing.

**Defender**, service Running: no threats on the app `Atomic.exe`, the bridge
`Atomic.exe` and `Atomic.zip`, twice each, full paths.

**The release-notes gate:** 2 notes written for 3.20.

---

## 11. Glossary

Unchanged from 3.19 §11.
