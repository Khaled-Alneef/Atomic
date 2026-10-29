# Atomic — Version Description Document

**Version 3.14** · 29 October 2026

---

## 1. Identification

| | |
|---|---|
| System name | Atomic |
| Version | 3.14 |
| Release date | 2026-10-29 |
| Repository | https://github.com/Khaled-Alneef/Atomic |
| Target platform | Windows 10 / 11, 64-bit |
| Delivered as | `Atomic.zip` — one **GitHub release asset** holding the ~11 MB **bridge installer** as its only `Atomic.exe` and the application **as a folder**, packed as `app.zip` |
| At the tag | `Atomic.exe` / `Atomic.zip` — the same bridge, for installs predating 2.0 |
| Asset size | 135,742,226 bytes |
| Asset SHA-256 | `596de8bf36950d7a734d3cf4250cf020e7f9c8a522f59f3adc77c354845c0402` |
| Bridge SHA-256 | `a0d34330bf1edbcf8158495491cd487b07a7cdfe76935cf8ed8f053fe961e383` (11,178,249 bytes) |
| Preceded by | 3.13 — `docs/VDD-3.13.md` |

---

## 2. System overview

Unchanged from 3.13.

---

## 3. Inventory of materials released

Unchanged from 3.13 §3.

**Changes since the last release** (`git log released/3.13..development`):

| Commit | Summary |
|---|---|
| 489daee | Atomic 3.14 - the volume flyout keeps its rounded, see-through panel while an episode loads |

---

## 4. What this version provides

**The volume flyout looks the same while an episode loads as while it
plays.** Pressing Up or Down during loading showed the flyout as a square,
solid black box; it is now the rounded, see-through panel it is over a
playing picture.

---

## 5. Design notes

**Per-pixel alpha blends with the video, not with Qt's windows.** The
flyout (`player_top_bar_live_patch._VolumeOsd`) is a native child composed
by `UpdateLayeredWindow`. Over mpv's swapchain that blends correctly; over a
sibling native window that Qt paints - `StartupBackdrop`, the loading frame
- it blends with the page's ground instead, so its transparent corners and
its 150/255 panel came out black. Measured in an isolated test window: the
same rounded panel drew a solid black square over a Qt-painted native
window, whether that window was plain or SLWA-layered, and drew rounded and
see-through as a plain child of it.

So while the backdrop is visible (not in its stall-badge mode, and large
enough to hold the 220px panel) the same image is painted by a plain child
of the backdrop (`_Inner`), positioned at the page's centre by the
parent-to-child offset, the way the backdrop's logo is drawn. Otherwise the
native window is used, as before. The hide timer hides both.

---

## 6. Configuration and user data

Unchanged. Nothing new is written.

## 7. External interfaces

Unchanged from 3.13.

## 8. Installation and removal

Unchanged from 3.13 §8.

---

## 9. Known limitations

Carried forward from 3.13 §9.

- If the loading frame comes up while the flyout is already showing over
  the picture, or goes down while it shows inside the loading frame, the
  flyout is not moved across for the rest of its 1.1s.
- OpenSubtitles' download server answered **HTTP 503** ("Site will be
  online soon. We are doing some necessary backups and upgrades") on 29
  October 2026 for every file tried, through its own links and the Stremio
  addon's alike; the player says "That Subtitle Could Not Be Downloaded".
  An outage on their side, not a change here.

---

## 10. Verification performed

**The frozen build was handed to the owner to test**; he approved it and
asked for the release.

**Isolated test window, source tree**: the per-pixel panel over a plain and
an SLWA-layered Qt-painted native window drew a black square (both
screenshotted); the same image as a plain child of that window drew rounded
and see-through.

**Not photographed by the session on the frozen build**: the flyout over the
loading frame in the real player - the owner tested it himself.

**The release build, read back out of its own archive**: "3.14" in
`helpers.updater`, `helpers.development_version_patch` and the what's-new
notes, and no "3.13" in the first two; `_over_backdrop` and `_Inner` in
`helpers.player_top_bar_live_patch`. Bundle verified, 1604 entries. Bridge
SELFTEST OK.

**Defender**, service Running: exit 0 on the app `Atomic.exe`, the bridge
`Atomic.exe` and `Atomic.zip`, twice each.

**The release-notes gate:** 1 note written for 3.14.

---

## 11. Glossary

Unchanged from 3.13 §11.
