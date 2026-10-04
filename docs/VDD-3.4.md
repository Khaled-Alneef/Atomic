# Atomic — Version Description Document

**Version 3.4** · 4 October 2026

---

## 1. Identification

| | |
|---|---|
| System name | Atomic |
| Version | 3.4 |
| Release date | 2026-10-04 |
| Repository | https://github.com/Khaled-Alneef/Atomic |
| Target platform | Windows 10 / 11, 64-bit |
| Delivered as | `Atomic.zip` — one **GitHub release asset** holding the ~11 MB **bridge installer** as its only `Atomic.exe` and the application **as a folder**, packed as `app.zip` |
| At the tag | `Atomic.exe` / `Atomic.zip` — the same bridge, for installs predating 2.0 |
| Asset size | 136,695,727 bytes |
| Asset SHA-256 | `e39b4c95ae29451710be10dd7a8c7143330718a0cd7ae0bca66865332f061c43` |
| Preceded by | 3.3 — `docs/VDD-3.3.md` |

---

## 2. System overview

Unchanged from 3.3. One correctness fix: ticking "Launch on Windows
startup" no longer registers the sign-in task the way Microsoft Defender
quarantined the app for.

---

## 3. Inventory of materials released

Unchanged from 3.3 §3.

**Changes since the last release** (`git log released/3.3..development`):

| Commit | Summary |
|---|---|
| `db43b02` | Atomic 3.4 |

---

## 4. What this version provides

**The startup task is registered through Task Scheduler's own API.**
On 4 October 2026 Defender quarantined the installed app as
`Behavior:Win32/Persistence.A!ml` - the exe, its Desktop, Start menu and
taskbar shortcuts and the `\Atomic` task - immediately after the owner
ticked "Launch on Windows startup". Defender's event 1116 named
Atomic.exe as the process and listed exactly what that tick writes. The
tick had spawned a hidden `schtasks.exe /Create /XML <temp file> /F`.
The same task, with the same definition, is now registered, queried and
deleted in-process through `ITaskService` / `ITaskFolder`; nothing is
spawned and nothing is written to disk. Sign-in launches keep the task's
speed advantage over the Run key.

---

## 5. Design notes

**Raw COM vtables, no new dependency.** Neither pywin32 nor comtypes is
in the bundle, and four calls did not justify either: `helpers/startup`
calls `CoCreateInstance(CLSID_TaskScheduler)`, `Connect`, `GetFolder("\")`
and `GetTask` / `DeleteTask` / `RegisterTask` by slot, through private
`WinDLL` instances. `RegisterTask` takes the XML as text with
`TASK_CREATE_OR_UPDATE`, which is what `/F` was. The Run-key fallback is
kept for a machine whose scheduler refuses.

---

## 6. Configuration and user data

Unchanged. The task keeps its name, `\Atomic`, and its definition.

## 7. External interfaces

Unchanged from 3.3 §7.

## 8. Installation and removal

Unchanged from 3.3 §8. Uninstall removes the task through the same API.

---

## 9. Known limitations

Carried forward from 3.3 §9. Defender's verdict is a cloud model and
cannot be reproduced on demand, so the absence of a detection is
evidence, not proof; code signing (roadmap #16) remains the durable
answer to every machine-learning verdict on this unsigned binary.

---

## 10. Verification performed

**The COM path, source tree:** a throwaway disabled task registered,
found, read back by `schtasks` with its definition intact, and deleted,
from the main thread and a worker thread (15-25 ms); no child process.

**The frozen build, against a copy of the owner's data**, with a
separate process-table watcher validated on a hidden `schtasks` it
caught with its parent: four untick/tick cycles in the real Settings
dialog, eight logged registrations and removals, the registered task
reading battery flags off, `PT0S`, `InteractiveToken`. Atomic.exe
spawned nothing but WebView2; no Defender event of any kind in the
7.5 minutes after the first tick. The owner's original task was
restored afterwards, identical line for line.

**The release build, read back out of its own archive**: "3.4" in
`helpers.updater`, `helpers.development_version_patch` and
`helpers.whats_new`; `helpers.startup` carries the COM path and no
`subprocess`. `build.py`: 1,608 entries, 32 bundled files byte-identical
to `src/`. Bridge SELFTEST OK.

**Defender**, service Running: exit 0 on the app `Atomic.exe`, the bridge
`Atomic.exe` and `Atomic.zip`.

**The release-notes gate:** one note written for 3.4.

**Not measured.** The old build as a control (it would mean triggering a
Severe quarantine on purpose); a real sign-in launch through the task
registered this way.

---

## 11. Glossary

Unchanged from 3.3 §11.
