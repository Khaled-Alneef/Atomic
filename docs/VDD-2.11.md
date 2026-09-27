# Atomic — Version Description Document

**Version 2.11** · 27 September 2026

---

## 1. Identification

| | |
|---|---|
| System name | Atomic |
| Version | 2.11 |
| Release date | 2026-09-27 |
| Repository | https://github.com/Khaled-Alneef/Atomic |
| Target platform | Windows 10 / 11, 64-bit |
| Delivered as | `Atomic.zip` — one **GitHub release asset** holding the ~11 MB **bridge installer** as its only `Atomic.exe` and the application **as a folder**, packed as `app.zip` (`Atomic/Atomic.exe` + `Atomic/_internal/`) |
| At the tag | `Atomic.exe` / `Atomic.zip` — the same bridge, for installs predating 2.0 |
| Asset size | 137,538,105 bytes |
| Asset SHA-256 | `53a75731fcb5675f18e1adcfea8aa1df309058524fea9a3fbf25941818d5b9f0` |
| Preceded by | 2.10 — `docs/VDD-2.10.md` |

---

## 2. System overview

Unchanged in purpose and on-disk shape from 2.7 — see `docs/VDD-2.7.md`
§2. 2.11 carries one reading fix and one change forced by Windows
Defender.

---

## 3. Inventory of materials released

Unchanged from 2.7 §3. One asset, `Atomic.zip`, named exactly that
because `updater.ZIP_NAME` and `atomic_setup.ZIP_NAME` both look for it.

**Changes since the last release** (`git log released/2.10..development`):

| Commit | Summary |
|---|---|
| `06b67b9` | The release snapshot no longer silently deletes the earlier VDDs (procedure only) |
| `e171a39` | Reading a chapter moves the Home card's chapter number |
| `77b3573` | No startup-task work on launch - Defender quarantined the app for it |

---

## 4. What this version provides

**Reading a chapter moves its Home card.** The owner, 27 September 2026:
*"when I read a ch the progress does not change in the home page card ...
but when I mark as read/unread it works"*.

**Atomic no longer touches its Windows startup entry when it opens.** The
same day, Defender quarantined the installed app as
`Behavior:Win32/Persistence.A!ml` and took its shortcuts and startup task
with it. The entry is now written only when the owner changes "Launch on
Windows startup" in Settings (or the setup wizard, or uninstall). An
install whose task was removed by Defender turns it back on by ticking the
setting again.

---

## 5. Design notes

**The reader.** The web reader marks a chapter on open through
`/api/mark` → `backend.mark_read`, which ticked History and stopped. A
saved reading card reads the entry's `last_watched_chapter`
(`server._progress_text`) — the field the Qt reader raised on every open
(`_mark_chapter_read`) and the details page's mark menu writes through
`tracker.correct_progress`. `mark_read` now calls
`tracker.record_progress` for a reading type: the same forward-only write,
with the History tick and `changes.bump` inside it. A type the tracker
does not file as reading keeps the old History-only path, since
`record_progress` would take its episode branch and write no tick.

**The startup task.** `helpers/startup.reconcile()` ran two seconds into
every launch, on a thread: a registry read, then — in the installed copy —
a hidden `schtasks /Query /TN Atomic /XML`, and `schtasks /Create /XML
<temp> /F` when the task named another exe. It existed to migrate a Run-key
entry and to re-point a task left naming the old single-file build (2.7).
Defender's record of the incident:

```
17:23:44 / 17:24:34  id=2010  new dynamic signatures (RtSigs)
17:24:30             Atomic.exe started (pid 20408)
17:24:35             id=1116  Behavior:Win32/Persistence.A!ml, FastPath, Suspicious Behavior
17:24:48             id=1117  Quarantine: Atomic.exe, Desktop\Atomic.lnk, Start menu and
                              taskbar Atomic.lnk, \Atomic task and its TaskCache keys
```

`reconcile()` and its only helper `_task_command` are removed
(`is_installed_copy` stays: uninstall reads it). What is lost: a task
left naming another exe is no longer re-pointed on its own; turning the
setting off and on re-points it.

---

## 6. Configuration and user data

Unchanged from 2.7 §6.

## 7. External interfaces

Unchanged from 2.7 §7.

## 8. Installation and removal

Unchanged from 2.7 §8.

---

## 9. Known limitations

Carried forward from 2.10 §9, plus:

- **The Defender verdict is not proven gone.** It is a cloud ML
  judgement on behaviour and cannot be reproduced on demand. 2.11 removes
  the one persistence write nobody asked for; ticking "Launch on Windows
  startup" still registers a logon task through `schtasks`, and that act
  could be judged the same way. Code signing (roadmap #16) remains the
  durable answer.
- Releases remain unsigned.

---

## 10. Verification performed

**Reader, source tree on a copy of his data.** Kingdom (WAN) at 887:
before, the reader opening c890 left the field at 887 and the card at
"Ch 888"; after, 890 and "Ch 891", and a re-read of c882 left it at 891.
**Frozen build:** Home read "Ch 888", the cover opened chapter 888, Escape,
Home read "Ch 889" with no page switch (screenshots `home_before`,
`reader`, `home_after`).

**Startup, measured as child processes.** A process-table poller at 5ms
(`watch_schtasks.py`), validated first on a hidden `schtasks /Query` it
caught at +1.96s. Control: HEAD's `reconcile()` run as a frozen install at
`%LOCALAPPDATA%\Programs\Atomic` spawned `schtasks.exe` at +1.04s and
created no task. The 2.11 build launched from an installed path
(`LOCALAPPDATA` pointed at a scratch copy) spawned **none in 25s**, and
drew Home normally (screenshot `launch_211_installed`). No Defender
detection during any of it.

**The frozen build, read back out of its own archive:** no `reconcile`
or `_task_command` in `helpers.startup`, no `reconcile` or
`startup-reconcile` in `main`; `record_progress` and `_chapter_of` in
`web.backend`; "2.11" in `updater`, `development_version_patch` and
`whats_new`.

**Defender**, `MpCmdRun -Scan -ScanType 3 -DisableRemediation`, service
Running: exit 0 on the bridge `Atomic.exe`, the app `Atomic.exe` and
`Atomic.zip`; both executables re-scanned, exit 0 again.

**The release-notes gate:** two notes written for 2.11.

**Not measured.** The Settings toggle's own `schtasks /Create` against
Defender (it was not exercised, so as not to invite a second
quarantine); the bridge's install over a Defender-removed 2.10.

---

## 11. Glossary

Unchanged from 2.7 §11.
