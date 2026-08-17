# Atomic — Version Description Document

**Version 1.5** · 17 August 2026

---

## 1. Identification

| | |
|---|---|
| System name | Atomic |
| Version | 1.5 |
| Release date | 2026-08-17 |
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
| `Atomic.exe` | The application. 47,763,671 bytes. SHA-256 `440aacd2199e53ccfd0591c3b8d8d01930ce960f67d6159995849ca4189d93c5` |
| `src/` | Full Python source, 15,230 lines across 37 modules (12,036 across 36 at 1.4) |
| `src/filter_icon.png` | The tracker filter button's icon, bundled into the executable |
| `packaging/` | `build.py`, `Atomic.spec` and `check_release_notes.py`, which produce and gate the executable |
| `docs/VDD-1.5.md` | This document |

Build environment unchanged from VDD-1.1 §3.

Twenty-two development builds (1.4.1–1.4.22) stand between 1.4 and this
release. Line counts are measured per file and summed, the same method
as the 1.4 column above.

1.5 was re-cut once after first being tagged, before anyone had it, to
take the framed keys under Settings → Keybinds (§5). The size and hash
above are the re-cut binary's; the first ones described a build that is
no longer what `v1.5` points at. As with 1.4's re-cut, `main` carries
one commit per release rather than a correction on top of one.

---

## 4. Inventory of software contents

One module is new since 1.4:

| Module | Lines | Responsibility |
|---|---|---|
| `helpers/global_search.py` | 248 | One query answered across all six pages, and the panel that shows it |

| Module changed | Lines | What changed |
|---|---|---|
| `windows/tracker.py` | 3,050 | Select mode with batch status and delete; drag repaired; searches and filters kept across a visit; the empty-state crash fixed |
| `helpers/settings_dialog.py` | 1,502 | Eight categories; backup and restore; Check All; the three-state Stremio line; every hint down to two lines |
| `src/main.py` | 1,217 | Home's search field and the Ctrl+K panel; the keyboard map; window geometry remembered; the startup update check and its reminder |
| `helpers/widgets.py` | 1,045 | The clickable undo toast, `search_field` and its drawn magnifier, card labels that no longer clip |
| `windows/home.py` | 878 | The search field beside the greeting; missing app targets read as they do on Apps |
| `windows/link_grid.py` | 782 | Search, selection and batch delete on Apps and Websites; undo and redo |
| `windows/games.py` | 491 | The same search, selection, batch delete, undo and redo |
| `helpers/whats_new.py` | 308 | Notes scroll rather than growing the dialog past the screen; 1.5's notes |
| `helpers/app_settings.py` | 256 | Window geometry and last-seen-version storage |
| `helpers/stremio.py` | 252 | A rejected session told apart from an empty history |

`helpers/updater.py` and `helpers/anime_sites.py` changed by a line
each. In `packaging/`, `check_release_notes.py`, `test_update.py` and
`diagnose_stremio.py` are new, `build.py` gained its verification, and
`diagnose_anilist.py` — which called functions deleted in 1.4 and
crashed before printing anything — was removed.

---

## 5. What this version provides

### One search across everything

Ctrl+K, or the box beside the greeting on Home, searches Anime,
Reading, Movies & Series, Games, Apps and Websites at once. Placement
was argued rather than picked: Quick Open, Spotlight, Raycast, Slack and
Notion agree on a fixed-width panel with results growing downwards, and
620px sits between Quick Open's ~600 and Raycast's ~750. Eight results
maximum, so the panel can never outgrow the window it belongs to.

Choosing a result **opens** it — the anime in Stremio, the game, the
app, the site — rather than walking to its page to be clicked again.
Nothing in the search knows how to open anything: it hands the entry to
the same function that entry's own page uses, so a result behaves
exactly as its card does, including the toast when a target has gone
missing.

The panel is a Tool window shown without activating, not a Popup: a
Popup grabs the keyboard, which would take focus off the field being
typed into. It anchors under Home's field when Home is showing and falls
back to the window-centred placement anywhere else — the shortcut
belongs to the app, the field to one page.

### A keyboard map

Ctrl+K search everything, Ctrl+F this page's search, Ctrl+N add, Ctrl+Z
undo, Ctrl+Y redo, Ctrl+1–9 jump to a sidebar page, Ctrl+, settings, Esc
clear a search. Ctrl+1–9 reads the sidebar's current order, because that
order is draggable and rows can be hidden. Escape clears a page search
before it leaves full screen, but only when the box has something in it.
The list lives in Settings under Keybinds — in the search panel it was a
wall of grey text shown to someone who had just proved they knew the
shortcut.

Each key there is drawn as a cap of its own rather than as one string
with a plus in it: surface fill, border, and a 2px darker bottom edge,
which is the whole illusion — a flat rectangle reads as a badge, an edge
under it reads as a key with a side. The combination is split on `+`
rather than parsed, since none of the keys is itself a plus; `Ctrl+,`
survives that and `Ctrl+1-9` keeps its range on one cap, which is what
it is — one key, any of nine.

### Selecting several entries, and deleting them

Every page has a Select mode: the tracker sets a status or deletes a
batch, and Games, Apps and Websites — which had no selection at all —
got the same one. One confirmation for the whole batch and one undo
offer for it, never one per entry.

A mode rather than a modifier, because a left click on a card already
means "open this", so Ctrl+click would have meant reworking the click
path every page shares and nothing on screen would ever have advertised
it. Two rules make it safe: only what is on screen can be selected, so a
search, a filter or a status change can never leave something picked
that cannot be seen; and dragging is off while selecting, since both
want the same left press. Deletes go through each page's
reload-then-apply path, so a batch cannot roll back an entry another
page wrote while this one was open.

### Undo, and now redo

Removing an entry asked once and then it was gone. All three removal
paths — tracker, games, link_grid, which share no base class — now keep
the record and offer it back from a toast for eight seconds, restoring
it at its original index with every field intact. Ctrl+Y redoes what
Ctrl+Z just undid, and means the last thing undone and nothing else: set
only by an undo that ran, cleared by being taken, cleared again by the
next removal.

### Backup and restore

Settings can write every live JSON file to an archive and read it back —
above Clear Data, because taking a copy is what precedes clearing.
Covers are 19MB of regenerable images and stay out, as do the log and
storage's own `.bak`/`.tmp`/`.corrupt` files.

The restore validates the whole archive before writing anything, and
rejects before it asks for confirmation. Ten bad archives were tried and
none wrote a byte: a truncated zip, a member failing its CRC,
unparseable JSON, a BOM'd member, a bare scalar, a zip with no JSON, a
`../../evil.json` traversal member, a non-zip file, an empty file, and a
half-good archive whose valid member also stayed out. Writing goes
through `storage.save`, so every overwritten file leaves its `.bak`.

### Settings in eight categories

General, Preferences, Watching, Reading, Games, Data, Keybinds,
Uninstall. Uninstall used to sit one scroll below the buttons that clear
a single content type, which is too close for the one control that
deletes everything and the app with it. The sidebar-section toggles moved
to Preferences, which is what General had turned into a dumping ground
for.

The category is "Watching" rather than "Anime & Series", which predates
films being tracked. The literal reading was measured and rejected:
"Anime, Movies & Series" needs 192px of the sidebar's 182px and elides
mid-word.

### An update that says so

There is one check 4 seconds after launch, last of everything so it does
not compete with the image prewarm, surfaced as a toast and an accent dot
on the Settings button. The toast shows once per available version, so
declining an update does not earn the same message every launch — but an
update still waiting is now offered again at each launch as an Update
Available dialog with Open Settings and Later. A dialog because this one
asks something. A failed check says nothing at all.

### Sideways-scrolling status rows, and the four defects they caused

Each status section is its own row, scrolling sideways on its own rather
than sharing one scroll with the page — the ninth card of a row used to
sit past a viewport whose horizontal bar was switched off, with nothing
that could reach it. Per section and not per page, because a shared
sideways scroll would drag a short section off screen to reach the end of
a long one. On Movies & Series each status also splits by type.

That change (1.4.7) caused four regressions, all fixed here and recorded
because they share one shape — code that read cards or rows off a layout
that had stopped holding them:

- **An empty tracker page took the app down.** The layout became a
  `QVBoxLayout` and the empty-state branch still called `addWidget` with
  grid row and column arguments, raising `TypeError` inside a slot — and
  an exception in a slot ends the process. It survived four versions
  because the harness written for the row work built pages with entries
  in them.
- **Drag-to-reorder was dead on tracker pages.** The drop logic reads
  cards off the container's layout, which now holds a title and a scroll
  area per section. A drag still started, still switched the sort to
  Custom Order, and dropped nothing.
- **A single section stranded mid-page**, its header stretching from
  23px to 272px because nothing else in the layout was elastic.
- **Card names wrapping to two lines lost the second line** and the
  Added line under it — `QLabel` picks the wrap width it thinks looks
  best, reports that width's height, and the grid then narrows the label
  without revisiting the height.

### Smaller fixes this cycle

- Settings said "Connected as X" for the whole life of a saved Stremio
  key, including after Stremio had begun refusing it. Three states now,
  the third read off the tracker's last attempt rather than re-measured,
  and the sign-in form comes back with it.
- The window reset to 1280x840 every launch. Size, position and maximized
  state are remembered as plain numbers — not Qt's opaque blob —
  precisely so they can be clamped against the monitors that exist now.
- A suggestion's chapter number could outlive the suggestion: searching
  "Kingdom (WAN)" filled in the other Kingdom's 798 because the
  pass-through match landed first and "never overwrite what is there"
  then refused the real 884.
- Ticking Show Last Watched revealed season and episode already filled in
  from the app's own released figures, which a save would have written
  down as the owner's.
- The Add menu positioned itself with `mapToGlobal` — the documented trap
  that put toasts 200px off a mixed-DPI screen. It uses `setMenu` now.
  That was the last live `mapToGlobal` in `src/`.
- Title suggestions fired a bare thread per debounce pause; they go
  through `lookup_pool.submit_latest` now, as does the per-site check,
  which was a bare thread per click.
- Site check verdicts are the owner's own words, and clear on restart
  rather than claiming something still true.
- Nothing says "double-click" any more, because nothing opens on one.

---

## 6. Design notes

**Why the category list is sized from its rows.** Eight rows need 453px
in a 302px list with its scrollbars off, so Data, Keybinds and Uninstall
were not merely out of view — there was no way to reach them, which from
the outside looks exactly like their having been removed. `QListWidget`'s
own `sizeHint` is bounded whatever it holds; it asked for 192px, and at
five categories that happened to be enough. Same shape as the ninth
tracker card: a container asked to hold more than it did when it was
written, with no scrollbar to admit it.

**Why the red row is a label and not a foreground role.**
`item.setForeground(theme.DANGER)` is the obvious way and does nothing
here: the nav list's QSS sets a colour on `::item`, and a stylesheet
colour beats the model's `ForegroundRole`. The first check read the value
that had been set rather than the pixel painted, which is why it passed.
A transparent-background label on the row is the one thing a stylesheet
colour does not override — measured at `#eb513c` against General's
`#f1f1f7`.

**Why a clickable toast lives in `widgets.py`.** An ordinary toast is
built `WA_TransparentForMouseEvents`, and Qt folds that into
`Qt::WindowTransparentForInput` when it creates the native window, so a
toast that can be clicked has to be built without the attribute from the
start. Clearing it afterwards leaves the real window still deaf.

**Why undo has no history of its own.** Ctrl+Z reaches the undo offer
already on screen. The offer knows what was removed, how to restore it
and when it expires; a second record would be a second answer to what
undo means right now. With nothing to undo it says so rather than
appearing to work.

**Why searches and filters are kept in memory and not on disk.** Pages
rebuild from scratch on every visit, so a query survived nothing. It now
lives in a module-level dict keyed by page class for the life of the
process, restored before `textChanged` is connected so the restore does
not kick the debounce. Nothing is written to disk on purpose: a filter
still narrowing the grid after a restart, with no visible reason some
entries are missing, would be worse than resetting.

**Why the magnifier is drawn rather than bundled.** A 14px glass is a
circle and a stroke; a PNG would be another asset to keep in step with
the palette. It is drawn at the screen's ratio, not the widget's — a
widget reports 1.0 until it has been shown, and the display then upscaled
a 14px pixmap, which was the blur.

**Why Settings was not rebuilt into tabs.** The roadmap item was written
against "1,024 lines of one long scrolling form"; that premise had
expired. Measured at the default 920x640, the worst page was 1.41 screens
— 2.1 wheel notches — and nothing scrolled at all above a 900px window.
The reopen trigger is recorded as a number rather than a feeling: a page
past about two screens at the default size. The eight-category split that
followed was the owner's request, not a re-derivation of this.

**Why the release notes gate reads a two-part version as "this
release".** The release procedure sets `APP_VERSION` to the two-part
number *before* the snapshot, so treating it as a development build would
demand notes for 1.6 while shipping 1.5 — blocking every release.

---

## 7. Configuration and user data

As VDD-1.4 §7, with these changes:

- Window size, position and maximized state are stored as plain numbers
  in settings, and clamped against the monitors present at launch. A
  geometry saved off-screen reopens on a real screen with its title bar
  reachable; an implausibly small one is refused as corrupt.
- The last version whose "what's new" notes were shown, and the last
  update version announced, are recorded in settings.
- A backup is a zip of the live JSON files only. Restoring writes through
  `storage.save`, so every overwritten file leaves a `.bak` beside it.

---

## 8. External interfaces

As VDD-1.4 §8. No source was added or removed this cycle; the only change
is that the GitHub release check now also runs once, 4 seconds after
launch, rather than only when Settings is opened.

---

## 9. Installation and removal

Unchanged from VDD-1.2 §9.

---

## 10. Known limitations

Carried unchanged from VDD-1.4 §10 — Stremio as the only progress
source, a Stremio entry's number not being editable, Wikidata's
streaming-id cliff, Amazon Prime measured and rejected, a region-blocked
Netflix title falling back to search, AniList's `403`, and Crunchyroll
ids not being probed — with these changes:

- **Filter and search selections now persist across a page visit** but
  deliberately not across a restart, so the previous entry on this list
  is half resolved.
- **Undo lasts eight seconds and is withdrawn by navigating away.**
  Letting it expire leaves the removal final, as before.
- **Redo remembers exactly one step**, the last thing undone.
- **A backup is not scheduled or automatic.** It happens when the owner
  asks for it.
- **Nothing on screen explains why dragging is unavailable** while a
  search, filter or Select mode is active. Unchanged from 1.4.

### Antivirus note

As VDD-1.4: the 1.2 executable was flagged `Trojan:Win32/Wacatac.B!ml`,
established by bisection as a false positive attached to that binary
rather than to any code. The durable remedy is a code-signing
certificate; nothing has been bought, so this can recur.

---

## 11. Verification performed

In addition to VDD-1.0 §11 through VDD-1.4 §11:

- **Restore was attacked before it was trusted.** Ten malformed archives
  were tried and none wrote a byte (§5). Testing caught a process-killer
  on the way: `zipfile.testzip()` on a truncated archive raises
  `zlib.error`, which is neither `BadZipFile` nor `OSError`, and an
  exception escaping a Qt slot takes the app down.
- **Undo and redo were round-tripped against the file on disk** —
  delete, undo, redo, undo — on tracker singles and batches, on Games and
  on Apps, with real key presses rather than synthesised events.
- **Batch deletes were checked against concurrent writes**: a game
  appended and a Manga entry written to `tracker.json` mid-session, after
  the page was built, both survived a batch delete on another page.
- **The sideways rows were measured in a real window at 1280px**: twenty
  cards want 3866px against a 1167px viewport, the last card sits at
  x=3686 at rest and x=987 scrolled to the end, a short section grows no
  bar, and scrolling one row leaves the others where they were.
- **The red Uninstall row was measured as a pixel**, not as the value
  that had been set — the first check passed against a row that drew
  `#9d9db1`.
- **The keybind column was remeasured once the keys were framed**: the
  longest row wants 111px against the 92 the column held when they were
  one plain label, and a fixed width narrower than its content clips
  rather than wraps. Measured across all eight rows in a real window,
  and the page screenshotted from one.
- **The search field's centring was measured** at 1100, 1400, 1920 and
  2400px window widths, centred to within 2px, after a fixed 520px field
  had overflowed a 1400px window and been clipped.
- **The chapter-suggestion fix was verified against live data**, with a
  control run: the fix removed reproduces the reported 798, the fix in
  place returns 884.
- **`build.py` now proves what it produced** — every file `Atomic.spec`
  promises in `datas` present *and byte-identical* to the one on disk,
  plus a check that PyInstaller's work directory is not older than
  `src/`. Measured against the binary 1.4 actually shipped, which it
  rejects.
- **`check_release_notes.py` refuses to let a version be tagged without
  notes.** 1.4 shipped without them and recorded itself as seen anyway,
  so its notes could never be shown to anyone who had already updated.
- `APP_VERSION` reads `"1.5"` in the released executable, confirmed by
  reading the module back out of the frozen archive rather than trusting
  the build log; the 1.5 notes are in that same archive.
- No test read or wrote the real user data at `%APPDATA%\Atomic`. The
  update test copies the data directory before pointing the app at it,
  and refuses the download in every mode — a test of the updater must not
  be able to replace the binary it is testing.

Not verified: the release executable has not been exercised by its owner
against real data before publication, and no Defender scan was performed
on it prior to this release.

---

## 12. Glossary

Unchanged from VDD-1.2 §12.
