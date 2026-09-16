# Atomic — Version Description Document

**Version 2.5** · 16 September 2026

---

## 1. Identification

| | |
|---|---|
| System name | Atomic |
| Version | 2.5 |
| Release date | 2026-09-16 |
| Repository | https://github.com/Khaled-Alneef/Atomic |
| Target platform | Windows 10 / 11, 64-bit |
| Delivered as | `Atomic.zip` — a **GitHub release asset**, holding one self-contained `Atomic.exe` |
| Also at the tag | `Atomic.exe` / `Atomic.zip` — the ~11 MB **bridge installer**, for installs predating 2.0 (§3, §8) |

---

## 2. System overview

Unchanged from 2.0 — see `docs/VDD-2.0.md` §2. A single-window Windows
desktop application over one person's anime, reading, films, series,
games, applications and websites, with a native video player (libmpv)
over its own torrent engine (libtorrent) and debrid support, a chapter
reader, a download queue, and a locally served web UI (WebView2).

**2.5 is two changes, both about how the app behaves rather than what it
shows**: it starts much sooner when Windows signs you in, and opening an
app, website or game from Home no longer makes every card blink.

---

## 3. Inventory of materials released

| Item | Description |
|---|---|
| `Atomic.zip` (release asset) | The application. **125,538,080 bytes**. SHA-256 `e988addb60ad5eb4c236d7e8db9c68da8ea1bcbae805928ab9f25efae4de7138` |
| `Atomic.exe` (inside that zip) | **126,316,784 bytes**. SHA-256 `4842417ac2ac3aa1510460de509f62534f3803d92eb50b405e3c2f8152b91c1a` |
| `Atomic.exe` (committed at `v2.5`) | The **bridge installer**, 11,100,386 bytes. SHA-256 `ac49860bbf89133d8a518380d17d7b91dc9890ba29cf54ecf4383e4805c581f7` |
| `Atomic.zip` (committed at `v2.5`) | The same bridge installer, zipped, for a zip-preferring updater |
| `src/` | Full source, **127,395 Python lines** across 126 modules, plus 6,015 lines of served static UI |
| `packaging/` | `build.py`, `Atomic.spec`, `check_release_notes.py`, `fetch_libmpv.py`, and `bridge/` |
| `docs/VDD-2.5.md` | This document |

One commit stands between `released/2.4` and this release.

The bridge is unchanged in purpose and rebuilt from the same source: it
resolves the newest release carrying an `Atomic.zip` asset **at run
time**, walking `/releases`, so the copy committed at `v2.5` will find
2.6 and everything after it without being rebuilt.

---

## 4. What this version provides

**Atomic starts much sooner at Windows sign-in.** "Launch on Windows
startup" used to be a per-user registry Run entry, and a Run entry is
one of the last things a sign-in does: Windows Explorer does not begin
reading the Run key until the shell has settled, and then runs each
entry one after another. Startup is a **logon scheduled task** now,
which Task Scheduler fires in parallel with the shell as soon as the
interactive sign-in completes — so the app is up while the rest of the
Run list is still being worked through, rather than after all of it.
The registry Run key stays as a fallback if the task cannot be
registered, an existing install converts itself to a task once on its
next launch, and "Fullscreen mode when launch on startup" is unchanged.

**Opening an app, website or game from Home no longer blinks the
cards.** Opening one moves it to the front of its row on Home, and Home
noticed that within about a sixth of a second and redrew — but the
redraw blanked the page and then waited for its data, so the whole page
went empty for the length of that wait before it drew again. The page
now keeps the cards it has on screen and replaces them with the new
ones in one step, so there is no empty flash and nothing jumps.

---

## 5. Design notes

**The sign-in delay was Windows, not Atomic — measured on the owner's
own event log.** On the sign-in of 16 September: boot 17:07:28, sign-in
17:07:48, Explorer began the Run key at 17:08:13 (25 s after sign-in)
and reached `Atomic.exe --startup` at 17:08:27 as the **ninth of nine**
entries, behind Security Health, Realtek, Riot Vanguard, Adobe, LGHUB,
Google Drive (5 s on its own) and ScreenRec; the window was serving at
17:08:32. Forty-five seconds from sign-in to a usable window, of which
Atomic's own code is about five (17:08:27 → 17:08:32). A logon task
fires at ~17:07:48, so the ~35–40 s of Explorer pre-roll and serial
queue is what it removes.

The task is registered through `schtasks.exe` with a UTF-16 XML
definition (`helpers/startup._task_xml`), which is why three of its
settings are load-bearing and set explicitly rather than left to the
tool's defaults: `DisallowStartIfOnBatteries` / `StopIfGoingOnBatteries`
are **false** — the owner runs Atomic on a laptop, and the schtasks
default is true, which would mean the app does not launch at sign-in
whenever he is unplugged; `ExecutionTimeLimit` is `PT0S`, because a
desktop app is not a job the scheduler should decide has run too long;
and the principal is `InteractiveToken` + `LeastPrivilege`, so it runs
as the signed-in user, non-elevated, in the interactive desktop, and
needs no admin rights to register — the same permission the Run key had.
`helpers/startup.reconcile()` runs once on launch, on a timer and off
the hot path: it is a single registry read that returns immediately when
there is nothing to migrate, and converts a Run-key install to a task
otherwise, keeping the Run entry if the task cannot be created.

**The blink was a blank frame, not the data.** `static/app.js`'s router
`go()` cleared the page and *then* awaited its `/api/home` fetch, so an
empty page painted for the length of the await — measured at about 50 ms
on Home. A launch stamps `last_used` / `last_played`
(`link_grid._stamp_used`, `game_launch._stamp_played`), Home's 150 ms
file watch turns that into a `{redraw}` (`web_pages._check_covered`),
and so every launch flashed the whole page white before drawing it
again. The blank now waits until *after* the fetch, at the swap: the old
cards stay on screen and are replaced in one synchronous task, with no
frame between them. The two routes that intentionally draw a "Back"
button before their slow fetch (genre, cast — drawn in the reader's
shell, which has no cards to lose) are the exception and still blank
early; search was already handled separately. The redraw's scroll
restore was moved from a frame later into the resolve microtask, so the
page does not bounce to the top for a frame either.

---

## 6. Configuration and user data

No user-data change, no migration, no new file under `%APPDATA%\Atomic`.

The one new piece of external state is the **scheduled task** registered
in the user's own Task Scheduler when "Launch on Windows startup" is on
(name `Atomic`, in the root folder). It is created and removed by the
Settings toggle exactly as the Run-key entry was, removed by Uninstall,
and reversible by hand from Task Scheduler. An install that had the Run
entry keeps working and is converted to a task once; if Task Scheduler
refuses, the Run entry is written instead and startup still works.

---

## 7. External interfaces

Unchanged from 2.4. No network host is added. The one facility now used
that was not before is the Windows **Task Scheduler**, through
`schtasks.exe`, and only to register, query or remove the app's own
startup task — a local operation as the current user.

---

## 8. Installation and removal

Unchanged — `docs/VDD-2.0.md` §9. An install running 2.0 or later
updates in place from the release asset; an install running 1.10 or
older is handed the bridge committed at the tag, which fetches the
asset. The one behavioural note: after updating into 2.5, an install
that had "Launch on Windows startup" on converts its registry Run entry
to a logon scheduled task on its next launch, with the Run entry kept
as a fallback.

---

## 9. Known limitations

Carried forward from 2.0 §10 and earlier. Those that belong to this
version:

- **The sign-in timing improvement is projected, not measured on the
  2.5 build.** It is derived from the owner's Run-key event log (§5) and
  from the fact that a logon task fires in parallel with the shell;
  measuring the actual saving requires a real sign-in on the installed
  build, which is the owner's to run.
- **The task registration itself was not exercised in this
  environment.** The build sandbox refused to run `schtasks`, so the
  branch logic was proven by unit test with `schtasks` mocked (§10) and
  the definition proven well-formed, but a live register/migrate was not
  performed here.
- **If Task Scheduler is disabled** (the `Schedule` service stopped),
  the app falls back to the registry Run entry — the slower path this
  release exists to leave, but startup still works rather than silently
  turning off.
- Code signing (roadmap #8) is still open; the release is unsigned, and
  shipping the zip rather than the bare exe is what makes the download
  come through.

---

## 10. Verification performed

Method: `.claude/rules/testing.md`. **The owner asked to run the
behavioural test himself for this release**, so the runtime behaviour of
both changes — the actual sign-in time, and the absence of the blink on
his window — was left to his own testing rather than driven and
photographed here. What was verified before tagging:

**Startup module**, in process (`helpers/startup` loaded standalone,
`schtasks` and the registry mocked):

| Case | Result |
|---|---|
| enable, task registers | task created, Run-key entry removed |
| enable, task registration fails | falls back to the Run-key entry, does not drop it |
| disable | task and Run-key entry both removed |
| `reconcile` with no Run entry | returns after one registry read, no work |
| `reconcile` with a Run entry, task registers | migrated, Run entry dropped |
| `reconcile` with a Run entry, task fails | Run entry kept |
| the task XML | well-formed; battery flags `false`, `PT0S`, `InteractiveToken`, `LeastPrivilege` |
| `launched_on_startup` | unchanged — `--startup` still recognised |

**Read back out of the frozen archive**: `helpers.startup` carries
`_task_xml`, `_create_task`, `_delete_task`, `reconcile` and the strings
`schtasks.exe`, `InteractiveToken`, `LeastPrivilege`,
`DisallowStartIfOnBatteries`, `PT0S`; `main` carries `_reconcile_startup`
and calls it; `static/app.js` carries the deferred-swap change (the
old cards are kept until the new ones are built) and the microtask
scroll restore; `APP_VERSION` reads **2.5** in both `helpers.updater`
and `helpers.development_version_patch`, with no bare `2.4` left in
either.

**Static parse of `app.js`**: the change is syntactically clean — a JS
engine parses past the edited router without error; the one construct it
rejects is pre-existing and identical before and after the change.

**Not exercised here**: the running app for either change (left to the
owner, as asked); a live scheduled-task registration and a real sign-in
timing (§9); the second machine, his laptop, these reports usually come
from.

**Defender**: `MpCmdRun -Scan -ScanType 3` over the release exe twice,
clean both times (`WinDefend` running, `-DisableRemediation`).

---

## 11. Glossary

See `docs/VDD-2.0.md` §12.
