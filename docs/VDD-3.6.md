# Atomic — Version Description Document

**Version 3.6** · 8 October 2026

---

## 1. Identification

| | |
|---|---|
| System name | Atomic |
| Version | 3.6 |
| Release date | 2026-10-08 |
| Repository | https://github.com/Khaled-Alneef/Atomic |
| Target platform | Windows 10 / 11, 64-bit |
| Delivered as | `Atomic.zip` — one **GitHub release asset** holding the ~11 MB **bridge installer** as its only `Atomic.exe` and the application **as a folder**, packed as `app.zip` |
| At the tag | `Atomic.exe` / `Atomic.zip` — the same bridge, for installs predating 2.0 |
| Asset size | 137,074,502 bytes |
| Asset SHA-256 | `3c546581c0fa5db268d9d6678460a3112f86fc13164007b1ee476606119febf5` |
| Bridge SHA-256 | `56c5ad0d2417c86b1ab9e6628e209c48606bae807cb78586487368f6e12aa482` (11,180,453 bytes) |
| Preceded by | 3.5 — `docs/VDD-3.5.md` |

---

## 2. System overview

Unchanged from 3.5. Launcher games start from an Atomic opened at
Windows startup; Home stops flickering when something is opened from
it; the shelves keep their sort; the reading-site editor leaves Settings
and a reading title names its site instead.

---

## 3. Inventory of materials released

Unchanged from 3.5 §3.

**Changes since the last release** (`git log released/3.5..development`):

| Commit | Summary |
|---|---|
| `14b7736` | Epic games launch from a job, Home stops flickering, shelf sorts stick |
| `17deb45` | A clicked game, app or website moves to the front after 1.5s |
| `65da993` | Reading sites leave Settings; a reading page names its site |
| `e11e7a7` | Atomic 3.6 |

---

## 4. What this version provides

**Epic games start.** The Witcher 3, the owner's first Epic title, did
nothing when clicked. Atomic is started at sign-in by Task Scheduler,
which runs it inside a job object, and a program opened with
`os.startfile` is born in the same job - where Epic's launcher will not
run. Launcher URIs are now handed to Explorer (quoted, or Explorer drops
a URI carrying a query), which opens them from the shell, outside the
job.

**Home no longer flickers** when a game, app or website is opened from
it. The launch's redraw rebuilt every card with an empty picture and the
banner from its first slide; a redraw now hands the decoded pictures to
the rebuilt cards and keeps an unchanged banner where it is.

**The opened card moves to the front 1.5 seconds after the click**, on
Home and on the Games, Apps and Websites pages under Last Played / Last
Used, rather than jumping from under the pointer.

**Games, Apps and Websites remember their sort** (`settings.json`,
`shelf_sorts`) instead of opening on Custom Order every visit.

**A reading title's chapter page names its website** under the genres
(SOURCE). **The Reading Websites editor is removed from Settings** at the
owner's word - list, Add, Edit, Check, Check All, Remove and the form.
The saved sites are untouched and still used everywhere.

The what's-new dialog's Got It shows the pointing hand.

---

## 5. Design notes

**Why Explorer and not a job breakaway.** Breaking away needs the job to
allow it, which Task Scheduler's does not promise; Explorer is already
running outside any job of ours. Measured from a job: `os.startfile` →
no Epic process and no line in Epic's log after 20s; `explorer.exe
"<uri>"` → the launcher up and its log reading the whole URI back.
Unquoted, Explorer opened Epic's store URI but silently dropped one with
`?action=launch&silent=true`, with no job at all.

**Why the stamp is delayed rather than the redraw.** Home and the shelf
pages all re-order off the `last_played` / `last_used` stamp they watch
at 150ms, so writing it 1.5s later (`game_launch.stamp_later`, carrying
the click's own time) delays every surface at once.

**The banner key ignores its countdown.** A banner's `meta` carries
"Countdown: …", which changes every minute; compared whole it never
matched and the banner was rebuilt on every redraw. The drawn banner
already rewrites its countdown itself.

---

## 6. Configuration and user data

`settings.json` gains `shelf_sorts` ({"games"|"apps"|"websites": sort
name}). No other data changes.

## 7. External interfaces

None changed. Launcher URIs (Steam, Epic) now go through `explorer.exe`.

## 8. Installation and removal

Unchanged from 3.5 §8.

---

## 9. Known limitations

Carried forward from 3.5 §9. Reading sites can no longer be added,
edited, checked or removed from inside the app, by design. Quitting
within 1.5s of opening something loses that one "last played" stamp.

---

## 10. Verification performed

**The Witcher 3, reproduced and fixed on the frozen build.** His click
at 13:04:24 left Epic's log untouched. The fixed build, started outside
the Claude package and inside a job object with no breakaway (as Task
Scheduler starts it), with the entry's URI pointed at a bogus app name
so no game started: `game launch: ... (explorer)` logged, Epic up 2.4s
later, its LogUriHandler reading the full URI with its query.

**Flicker, sampled at the panel's rate.** 3.5, a Home click: one frame
with every cover of Watching, Reading and Games empty (64-74 levels off)
and the banner back on 01 / 06. 3.6: no zone moved more than 0.7 levels
frame to frame across the redraw, the banner held on 04 / 06. Control
runs without a click for both.

**Delay**: Rocket League clicked on Home at 13:43:26.747, Home redrawn
13:43:28.294 with it first and every cover intact; Games under Last
Played, TheWitcher3 still second at 0.7s and first by 2.5s.

**Sort**: Name (A-Z) picked on Games, Apps visited, Games back on Name
(A-Z); `shelf_sorts` in the copy's settings.json.

**Settings and SOURCE**: the Reading page photographed with only the
music URL; Kingdom (WAN)'s page photographed with SOURCE · 3asq; all 8
of his reading entries name a site. Removed names: none left in a
whole-tree scan of `.py/.js/.html/.css/.spec`. Got It: the hand over the
button, the arrow over the notes (control).

**The release build, read back out of its own archive**: "3.6" in
`helpers.development_version_patch` and `helpers.whats_new`;
`stamp_later` and the Explorer hand-off in `helpers.game_launch`,
`get_shelf_sort` in `helpers.app_settings`, `_show_source` in
`windows.details`; no `SiteForm` in `helpers.settings_dialog`; the
redraw hand-over in the bundled `app.js`. Bridge SELFTEST OK.

**Defender**, service Running: exit 0 on the app `Atomic.exe`, the bridge
`Atomic.exe` and `Atomic.zip`; both exes confirmed on a second pass.

**The release-notes gate:** six notes written for 3.6.

**Not measured.** A Steam URI through Explorer (it would have started a
real game); the delay and sort on Apps and Websites (same code as Games,
not clicked - they open real apps and sites); the SOURCE line appearing
after a site is picked for a Discover title.

---

## 11. Glossary

Unchanged from 3.5 §11.
