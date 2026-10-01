# Atomic — Version Description Document

**Version 3.1** · 2 October 2026

---

## 1. Identification

| | |
|---|---|
| System name | Atomic |
| Version | 3.1 |
| Release date | 2026-10-02 |
| Repository | https://github.com/Khaled-Alneef/Atomic |
| Target platform | Windows 10 / 11, 64-bit |
| Delivered as | `Atomic.zip` — one **GitHub release asset** holding the ~11 MB **bridge installer** as its only `Atomic.exe` and the application **as a folder**, packed as `app.zip` |
| At the tag | `Atomic.exe` / `Atomic.zip` — the same bridge, for installs predating 2.0 |
| Asset size | 136,676,524 bytes |
| Asset SHA-256 | `f6ba329381cfa47bd5095b44a23fa56184bcdb06db8811f2166430b2c2f169f2` |
| Preceded by | 3.0 — `docs/VDD-3.0.md` |

---

## 2. System overview

Unchanged from 3.0. One fix: the new icon reaches the Desktop, Start menu
and taskbar after an update.

---

## 3. Inventory of materials released

Unchanged from 3.0 §3.

**Changes since the last release** (`git log released/3.0..development`):

| Commit | Summary |
|---|---|
| `334db8e` | An update tells Explorer the icon changed |
| `74199e5` | Atomic 3.1 |

---

## 4. What this version provides

**The icon follows an update.** After 3.0 the owner's Desktop and taskbar
kept the old teal "A". On the first launch after an update the app now
tells Explorer the exe's icon changed.

---

## 5. Design notes

**The stale icon is Explorer's, not the file's.** Measured 2 October
2026 on the owner's install: the installed exe carried the new icon in
all seven sizes, byte-equal to `app_icon.ico`, and a fresh process asking
the shell for the exe or the Desktop shortcut was handed the new picture.
Explorer files icons by path; an update replaces the exe at the same path
and nothing told it. `updater.refresh_shell_icons` sends
`SHCNE_UPDATEITEM` for the exe and every Atomic shortcut (Desktop, public
Desktop, Start menu, pinned taskbar), then `SHCNE_ASSOCCHANGED`, and
starts `ie4uinit -show` detached. `main` calls it off the UI thread only
when the launch followed an update. Nothing waits on it and it never
raises.

---

## 6. Configuration and user data

Unchanged from 3.0 §6.

## 7. External interfaces

Unchanged from 3.0 §7.

## 8. Installation and removal

Unchanged from 3.0 §8.

---

## 9. Known limitations

Carried forward from 3.0 §9. The refresh runs on the launch *after* the
update, so an install updating from 3.0 gets it on its first 3.1 launch.

---

## 10. Verification performed

**Run once against the owner's install** from the source tree (frozen
check and exe path stubbed): 3 shortcuts found, the notifications sent,
`ie4uinit` started, 14 ms.

**The release build, read back out of its own archive**: `helpers.updater`
carries `refresh_shell_icons` and `ie4uinit.exe`; the entry script calls
it on the `update-icons` thread; "3.1" in `development_version_patch` and
`whats_new`. `build.py`: 1,608 entries, 32 bundled files byte-identical
to `src/`. Bridge SELFTEST OK.

**Defender**, service Running: exit 0 on the app `Atomic.exe`, the bridge
`Atomic.exe` and `Atomic.zip`.

**The release-notes gate:** one note written for 3.1.

**Not measured by me.** Explorer's own redraw of the Desktop and taskbar
icons on screen: the taskbar was hidden and the owner was watching a
video, so no window was opened over it. The proof is his first 3.1
launch.

---

## 11. Glossary

Unchanged from 3.0 §11.
