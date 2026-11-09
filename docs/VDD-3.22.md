# Atomic — Version Description Document

**Version 3.22** · 9 November 2026

---

## 1. Identification

| | |
|---|---|
| System name | Atomic |
| Version | 3.22 |
| Release date | 2026-11-09 |
| Repository | https://github.com/Khaled-Alneef/Atomic |
| Target platform | Windows 10 / 11, 64-bit |
| Delivered as | `Atomic.zip` — one **GitHub release asset** holding the ~11 MB **bridge installer** as its only `Atomic.exe`, and the app's folder packed as `app.zip` |
| At the tag | `Atomic.exe` / `Atomic.zip` — the bridge, for installs predating 2.0 |
| Asset size | 136,203,405 bytes |
| Asset SHA-256 | `c78445ef2aa75ccb203a8f0c8a44beae5f679ba9a939b8d2395e597847d57ef7` |
| Bridge SHA-256 | `a83b15e663eb684fa59986156ca826e589f6151a7f320974b323372d439f1314` (11,179,859 bytes). The asset's bridge (11,180,397 bytes) is a separate build of the same source; PyInstaller is not byte-identical twice |
| Preceded by | 3.21 — `docs/VDD-3.21.md` |

---

## 2. System overview

Unchanged from 3.21.

---

## 3. Inventory of materials released

Unchanged from 3.21 §3, plus one module: `helpers/crash_log.py`.

**Changes since the last release** (`git log released/3.21..development`):

| Commit | Summary |
|---|---|
| d6a00f6 | The bell counts unread chapters from the one after the last read |
| 6bdfc85 | The rig says which window tripped its guard, and Uninstall has a sandbox |
| f757d15 | Uninstall removes Atomic from Windows, once it has closed |
| f9c4981 | Settings' buttons are drawn: smooth corners and a red hover |
| fe505ba | A crash that ends the process leaves a record |
| 2255e2b | Atomic 3.22 |

---

## 4. What this version provides

**Uninstall removes Atomic from Windows.** The app, `%APPDATA%\Atomic`, the
Desktop and Start menu shortcuts, the taskbar pin, the Windows startup entry and
what the app keeps in `%TEMP%`. `Downloads\Atomic` stays, at the owner's word.

**Settings' buttons** and the questions it asks have smooth corners, and the red
Uninstall and Yes fill red under the pointer.

**The notifications bell** counts unread chapters from the one after the last
read.

**A crash leaves a record**: `crash-<when>.log` in the data folder, and its stack
in `atomic.log`.

---

## 5. Design notes

**Removal happens after the app has gone.** A detached script waits for the
process, then removes each path, retrying what is still locked for up to 30
passes. Deleting from inside the running app left 74 of 189 files on a throwaway
copy (Edge's profile, the log, the crash record).

**The program had never been removed.** The old command went to `Popen` as a
list, which escaped the quotes round every path as `\"`; `rd` and `del` refused
all of them. The command line is one string now, under `cmd /s /c`, and paths
arrive as arguments so a non-English user name cannot be mangled by the batch
code page.

**A copy only removes itself.** The install folder and shortcuts go only from
the installed copy, the startup entry only when it names this exe, and a source
run removes nothing.

**A ring, not a stroke.** A one-pixel light line on a dark ground spreads over
dimmer pixels on a curve. Light integrated across the line, corner against
straight at 45 degrees and 125%: a stroke 0.41, a ring whose inner curve has the
outer curve's radius 0.99 for the red outline, 0.61 for the faint grey one
(0.37 before).

---

## 6. Configuration and user data

New files in the data folder: `crash_<pid>.log` during a run and, after a crash,
`crash-<when>.log`. Nothing else changed from 3.21.

## 7. External interfaces

Unchanged from 3.21.

## 8. Installation and removal

Installation unchanged from 3.21 §8. Removal is §4 above. Atomic has never been
registered in Windows' Installed apps list, so it is not listed there before or
after Uninstall.

---

## 9. Known limitations

Carried forward from 3.21 §9, and:

- **The Windows Firewall rules stay**: removing them takes administrator rights.
- **Other buttons in the app still use QSS corners**; only Settings and its
  questions are drawn.
- **A taskbar pin's file is deleted**, but Explorer may show the icon until it
  refreshes.

---

## 10. Verification performed

**On the owner's machine:** `uninstall_sandbox.py` on the 3.21.1 test build with
the same removal code: the copy's data, exe, `_internal` and stream cache gone,
an unrelated temp file kept, and his real data, install, shortcuts and startup
task untouched.

**Frozen build, throwaway APPDATA and TEMP:** with the app open the script
removed nothing; 0.6s after a clean exit all 189 files were gone. The Uninstall
and confirm dialogs captured from the running window at 100%.

**Harnessed on fake paths:** a source run removing nothing; a locked file retried;
an Arabic and spaced user name; the installed branch with its folder, `.old`,
`.new-*` and three shortcuts, leaving a sibling program and "Atomic Heart.lnk".

**Release build:** Defender clean on the app exe, the bridge and the asset; the
archive read back for the version, notes and each change; launched on throwaway
data with no errors in the log.

**Not exercised:** Uninstall of a real installed copy, and the red hover under a
real pointer on the frozen build (posted moves do not hover a covered window).
The hover state was measured offscreen on the real dialogs.
