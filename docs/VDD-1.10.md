# Atomic — Version Description Document

**Version 1.10** · 18 August 2026

---

## 1. Identification

| | |
|---|---|
| System name | Atomic |
| Version | 1.10 |
| Release date | 2026-08-18 |
| Repository | https://github.com/Khaled-Alneef/Atomic |
| Target platform | Windows 10 / 11, 64-bit |
| Delivered as | `Atomic.exe` — a single self-contained executable |

---

## 2. System overview

Unchanged from VDD-1.2 §2.

---

## 3. Inventory of materials released

| Item | Description |
|---|---|
| `Atomic.exe` | The application. 47,785,342 bytes. SHA-256 `7c21d1988c6195cc81f7a6bdc224caed83c7145c62d87baed50b7dc06268fb2a` |
| `src/` | Full Python source, 16,208 lines across 38 modules (16,191 across 38 at 1.9) |
| `packaging/` | `build.py`, `Atomic.spec` and `check_release_notes.py` |
| `docs/VDD-1.10.md` | This document |

Build environment unchanged from VDD-1.1 §3, with one change to
`Atomic.spec` itself — see §6.

One commit stands between 1.9 and this release, taken from `development`
as a single snapshot. No development builds sit in between.

**This release was cut twice, and the source is identical in both.** The
first cut - 47,785,893 bytes, SHA-256 `9aab64e2...` - was flagged
`Trojan:Win32/Wacatac.B!ml` by Microsoft Defender and deleted on
download, while 1.9 was not. `v1.10` was moved to a second cut with the
owner's agreement; the first cut is superseded and is not the release.
The version number did not move, so an install carrying the first cut
reports 1.10 and will not be offered this one - and since Defender
deleted it, none are running. §11 records what the two builds measured;
§6 records why rebuilding is the remedy at all.

**On the version number.** 1.10 follows 1.9, and is not 1.1 written
differently. `updater.parse_version` reads the digits into a tuple, so
`(1, 10)` sorts above `(1, 9)` where a string comparison would put it
below; `RELEASE_TAG_RE` accepts multi-digit parts. Both were checked
before the tag was made, along with the release-notes gate's reading of
a two-digit minor.

---

## 4. Inventory of software contents

| Module changed | Lines | What changed |
|---|---|---|
| `windows/home.py` | 1,019 | `_resort_after_delay`; `GAMES_RESORT_DELAY_MS` → `RESORT_DELAY_MS`; the Quick Apps/Websites re-sort moved onto it |
| `helpers/whats_new.py` | 353 | 1.10's note |
| `helpers/updater.py` | 272 | `APP_VERSION` |
| `packaging/Atomic.spec` | — | `excludes=['numpy']` (§6) |

Nothing else in `src/` differs from 1.9.

---

## 5. What this version provides

### An app or website re-sorts a beat after you open it, not instantly

Home's Quick Apps and Quick Websites lists put the most recently used
entry at the top, and did so on the next tick of the event loop after a
click. The row therefore rearranged itself while the pointer was still
resting on it and the mouse button was still coming up: the entry
clicked jumped to the top, everything else slid down a place, and what
sat under the cursor was a different app. Nothing was mis-opened — the
open happens before the re-sort — but it reads as a misclick, which is
exactly the reason the Games row was given a 2.5 second delay when its
live re-sort was added at 1.8.

Both now share that delay. By the time the list moves, the app or site
is up and the pointer has left the card.

The delay is also what keeps the rebuild safe. The row that was clicked
is one of the widgets the redraw replaces, so it cannot be deleted from
inside its own click handler; that was previously handled by
`defer_grid_rebuild`'s zero-delay timer, and 2.5 seconds covers it just
as well.

---

## 6. Design notes

**Why one helper rather than a delay at each call site.** `_launch_game`
already built its timer inline, with four lines of comment explaining
why it is parented to the page rather than being a `QTimer.singleShot` —
a Home navigated away from before the timer fires takes the timer down
with it, where a bare singleShot fires into a page Qt has already
deleted. Copying that into the quick-list path would have copied the
comment with it, and a second constant to keep in step. The constant
lost its `GAMES_` prefix for the same reason: it is no longer about
games.

**Why rebuilding the same source fixes a Defender verdict.** It should
not be able to, and that is the finding. `Trojan:Win32/Wacatac.B!ml` is
a cloud machine-learning verdict on the binary's shape, not a signature
match on anything in it, and PyInstaller does not build byte-identical
output twice - archive ordering and embedded timestamps differ per run.
So two builds of one source tree are two different files to the
classifier, and measured here, one was flagged and the next was clean.

That makes rebuild-and-rescan the only remedy available without a
certificate, and it is a lottery ticket rather than a fix: the next
release can be flagged again for no reason attributable to its code.
Roadmap #8 is unchanged - the durable answer is code signing. What did
change is that a release is now scanned before it is tagged (§11)
instead of after it has reached the owner, which is how the first cut
got out.

**Why `numpy` is excluded from the build.** Nothing in this app imports
numpy, but Pillow's PyInstaller hook collects it whenever it is
installed, and PyInstaller's dependency analysis then imports every
collected package in an isolated subprocess to resolve binary
dependencies. On this machine that child dies: numpy 2.5.2 on Python
3.15 segfaults on `import numpy.random` — no traceback, reproduced
twice, and confirmed outside PyInstaller with a bare `python -c`
(exit 139). Every build failed there, at analysis, before any of this
release's source was even examined.

Excluding it changes nothing in what ships. The released 1.9 executable
carried zero numpy entries, because the analysis discarded it as unused
anyway; this build has the same 174 outer archive entries and the same
81 PIL modules in its PYZ. The broken numpy remains broken for anything
else on this machine that wants it — this only stops Atomic's build
depending on it.

---

## 7. Configuration and user data

Unchanged from VDD-1.9 §7. No stored field changed: the re-sort reads
the same `last_used` timestamp it always did, written the moment the
entry is opened rather than when the list moves.

---

## 8. External interfaces

Unchanged from VDD-1.9 §8. Nothing in this release contacts a service.

---

## 9. Installation and removal

Unchanged from VDD-1.2 §9.

---

## 10. Known limitations

In addition to VDD-1.9 §10, including the antivirus note (the durable
remedy remains a code-signing certificate, and nothing has been bought):

- **2.5 seconds is a fixed delay, not a hover check.** A pointer still
  resting on the list at 2.5 seconds will see it move. The alternative —
  waiting for the cursor to leave the row — was not built: it would
  never re-sort at all for someone who clicks and walks away, which is
  the ordinary case.
- **The delay is per click, not per list.** Opening two apps in quick
  succession starts two timers, and the list redraws twice, 2.5 seconds
  after each. Harmless (each redraw shows the same correct order) but
  visible as a second small rearrangement.
- **numpy's segfault is unfixed**, and is not Atomic's to fix. Should a
  future dependency genuinely need numpy, the exclude has to come out
  and the environment repaired first.

---

## 11. Verification performed

In addition to VDD-1.0 §11 through VDD-1.9 §11:

- **The delay measured offscreen against a copy of the owner's real
  data**, with `storage.DATA_DIR` redirected to a temp copy (never the
  live `%APPDATA%\Atomic`), driving `_open_quick_link` and `_launch_game`
  on a real `HomePage` with the open/launch calls stubbed out. Reading
  the labels of the widget on screen: Battle.net (last of five apps)
  reached the top at 2.51s, Kick (last of five websites) at 2.52s, and
  Call of Duty Black Ops III (last of six games) at 2.49s, with every
  list still in its original order when probed at 0.5, 1.0, 2.0 and
  2.4 seconds.
- **The first attempt at the games half was wrong and was discarded.**
  It polled `_recent_games()`, which re-sorts the moment `last_played` is
  set, and so reported 0.01s — measuring the data, not the redraw. The
  figure above comes from the labels inside `_games_grid`.
- **The shipped code was read back out of the frozen executable**, not
  out of the source tree. `windows.home` in the bundled PYZ carries
  `_resort_after_delay` and the constant 2500, and both `_open_quick_link`
  and `_launch_game` call it; `helpers.updater` carries `"1.10"`;
  `helpers.whats_new` this release's note. A build log is not evidence
  here — PyInstaller caches, and a no-op rebuild re-copies the previous
  binary.
- **The bundle gate passed**: every file `Atomic.spec` promises in
  `datas` present in the archive and byte-identical to the one on disk,
  174 entries.
- **The numpy exclude was checked against what it could have removed**:
  174 outer entries and 81 PIL modules, identical to 1.9's counts, and
  zero numpy entries in either.
- **`check_release_notes.py` passed** for 1.10, with one note written,
  and its two-part reading of `APP_VERSION` confirmed to demand 1.10's
  notes rather than 1.11's.
- **The version numbering checked before tagging**:
  `parse_version("v1.10")` is `(1, 10)` and compares above
  `parse_version("1.9")`; `RELEASE_TAG_RE` matches `v1.10`.
- **Both cuts scanned with Microsoft Defender before tagging the second**
  (`MpCmdRun -Scan -ScanType 3 -File ... -DisableRemediation`, engine
  1.1.26070.7, signatures 1.457.225.0). The first cut returned
  `Trojan:Win32/Wacatac.B!ml`, the shipped cut returned no threats, and
  1.9 - which the owner reported as unaffected - returned no threats.
  Every verdict re-run twice and identical both times, so the classifier
  is stable per file even though it disagrees between builds of one
  source tree. The service had to be confirmed running first: a stopped
  `WinDefend` fails these scans with `0x800106ba`, which reads as
  success to anything only checking that no threat was named.
- **Defender's own detection history read back** rather than relying on
  the report: `Get-MpThreatDetection` recorded the first cut at 20:30 and
  20:33 on the day of release, against tag times of 17:31 for 1.9 and
  20:25 for 1.10. The same ThreatID had also hit an Atomic download five
  times three days earlier, on 1.7 - so the flag is not new to this
  version, and the owner's report that 1.9 was clean is nonetheless
  exactly right.
- **The tag and its executable were confirmed on `origin`**:
  `refs/tags/v1.10` present remotely, the blob at `v1.10:Atomic.exe`
  matching the object built and hashed here.
- **The updater resolved this release from the live repository.** Run
  with `APP_VERSION` lowered to 1.9, `check_for_update()` returned
  `v1.10`; run as 1.10 it returned `None`, meaning already current.

Not verified: no antivirus other than Microsoft Defender was consulted,
and the executable has not been exercised by its owner against real data
since the re-cut.

---

## 12. Glossary

Unchanged from VDD-1.2 §12.
