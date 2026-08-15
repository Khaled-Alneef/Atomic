# Atomic — Version Description Document

**Version 1.1** · 15 August 2026

---

## 1. Identification

| | |
|---|---|
| System name | Atomic |
| Version | 1.1 |
| Release date | 2026-08-15 |
| Repository | https://github.com/Khaled-Alneef/Atomic |
| Target platform | Windows 10 / 11, 64-bit |
| Delivered as | `Atomic.exe` — a single self-contained executable |

---

## 2. System overview

Atomic is a personal desktop dashboard: one window that collects the
things you watch, read, play, and open, so they are reachable in a click
instead of scattered across browser tabs, launchers, and bookmarks.

It holds seven sections behind a single sidebar:

| Section | Purpose |
|---|---|
| **Home** | A "Continue Watching" carousel for whatever is in progress, plus a preview row per section |
| **Anime** | Tracked anime, as a poster grid, opening in Stremio or a configured video site |
| **Reading** | Tracked manga / manhwa / manhua, opening on your own reading sites |
| **Series** | Tracked live-action series, opening via Stremio deep links |
| **Games** | A local game launcher, bulk-importable from Steam, Battle.net, Epic, Riot and Xbox install folders |
| **Apps** | A grid of local applications |
| **Websites** | A grid of sites, with icons fetched automatically |

Nothing is a separate "recents" store — every list is derived from the
same saved entries the pages themselves write.

---

## 3. Inventory of materials released

| Item | Description |
|---|---|
| `Atomic.exe` | The application. 47,158,351 bytes. SHA-256 `b9106dc6b84c4c46b3763c0ade57247e440574ec10970bedd3833a7baa4789cb` |
| `src/` | Full Python source, 9,096 lines across 30 modules |
| `packaging/` | `build.py` and `Atomic.spec`, which produce the executable |
| `docs/VDD-1.1.md` | This document |

The executable is self-contained: it bundles the Python runtime and all
libraries, and requires no installer, no Python installation, and no
administrator rights.

### Build environment

| Component | Version |
|---|---|
| Python | 3.15.0b3 |
| PyQt6 | 6.11.0 (Qt 6.11.0) |
| Pillow | 12.3.0 |
| PyInstaller | 6.22.0 |

Unchanged from 1.0.

---

## 4. Inventory of software contents

### Application shell

| Module | Lines | Responsibility |
|---|---|---|
| `main.py` | 761 | Window, sidebar, navigation and page transitions, full screen (including startup full screen), startup image prewarm, cursor watchdog |

### Feature pages (`src/windows/`)

| Module | Lines | Responsibility |
|---|---|---|
| `tracker.py` | 1,569 | Anime / Reading / Series pages, entry form, progress sync, release schedules, drag-to-reorder |
| `home.py` | 764 | Dashboard, hero carousel, preview sections |
| `link_grid.py` | 469 | Shared grid behind Apps and Websites, drag-to-reorder |
| `games.py` | 409 | Games page, launcher import, icon handling, drag-to-reorder |
| `websites.py`, `apps.py` | 23 | Thin configurations of `link_grid` |

### Support modules (`src/helpers/`)

| Module | Lines | Responsibility |
|---|---|---|
| `settings_dialog.py` | 919 | Settings window — General, Anime & Series, Reading, Games, Data |
| `theme.py` | 737 | Palette, application-wide stylesheet, native window tweaks |
| `manga_sites.py` | 338 | Reading-site directory and live search across four site engines |
| `updater.py` | 267 | In-app updates from the GitHub repository |
| `mangadex.py` | 278 | MangaDex client, next-chapter estimation, cached per-manga id |
| `launchers.py` | 244 | Game-launcher scanning, icon extraction and caching |
| `widgets.py` | 507 | Shared widgets — cards, toasts, scroll areas, hover-cursor registry, drag-to-reorder |
| `images.py` | 225 | Image loading, thumbnail caching, letter avatars |
| `icon_extract.py` | 202 | Executable icon extraction |
| `stremio.py` | 179 | Cinemeta search / metadata and Stremio account progress |
| `anilist.py` | 160 | AniList search, list progress, airing schedule |
| `release_schedule.py` | 170 | Chooses a schedule source per medium; formats the hover lines |
| `tvmaze.py` | 105 | TVMaze show lookup and next-episode schedule |
| `app_settings.py` | 123 | Persisted application settings, including fullscreen-on-startup |
| `native_cursor.py` | 82 | Windows cursor correction (see §6.3) |
| `storage.py` | 155 | JSON persistence, per-entry updates, custom-order helpers |
| `anime_sites.py` | 74 | Video-site directory |
| `nav_config.py` | 65 | Sidebar order and section visibility |
| `child_process.py` | 62 | Environment and window flags for launched programs (see §6.2) |
| `title_match.py` | 61 | Fuzzy title matching for external catalogues |
| `startup.py` | 103 | Launch-on-Windows-startup registration, startup-flag detection |
| `uninstall.py` | 45 | Full removal of the app and its data |

---

## 5. What this version provides

Everything in §5 of VDD-1.0 still applies. This section covers what 1.1
adds on top of it.

### 5.1 Drag-to-reorder cards

Every card grid — Games, Apps, Websites, and all three tracker pages
(Anime, Reading, Series) — now accepts dragging one card onto another to
reorder it there. Doing so switches that page's sort mode to *Custom
Order* automatically, since a manual reorder only means something under a
sort the page isn't about to override. Right-click **Move Up** / **Move
Down** remain available as a non-drag alternative.

Dragging near the top or bottom edge of a grid auto-scrolls it, so
reordering into a card currently off-screen doesn't require a separate
scroll step first.

Reordering is backed by new `storage.py` helpers (`move_in_list`,
`order_by_ids`, `move_entry`, `apply_custom_order`) that all operate by
entry id rather than by list position, for the same reason recorded in
§6.4 of VDD-1.0: re-reading and patching the specific entries that moved,
rather than writing back a whole list that might already be stale.
Because reordering keys off id, `link_grid.py` and `tracker.py` backfill
a missing `id` on any entry saved before this version, during migration.

### 5.2 Fullscreen on startup

Settings › General gains a second checkbox under *Launch on Windows
startup*: *Open full screen*. It only takes effect on a launch Windows
itself starts at sign-in — a hand-launched Atomic always opens maximized,
as before. The checkbox is greyed out unless startup launch is enabled,
since it has no effect without it.

`startup.py` detects a startup-initiated launch via a `--startup` flag
appended to the registered command line, and migrates any
already-registered startup entry to carry that flag the first time
Atomic runs after updating — so the setting works immediately for an
existing startup registration, not only for one created fresh under 1.1.

### 5.3 Settings-gear icon consistency

The sidebar's Settings button drew an emoji gear while the sidebar was
expanded and `theme.SETTINGS_ICON` while it was collapsed, so folding the
sidebar visibly swapped the glyph. Both states now draw
`theme.SETTINGS_ICON`, at different point sizes, so the icon no longer
changes shape on collapse — see §6.5.

---

## 6. Design notes

Sections 6.1–6.4 are unchanged from VDD-1.0 and are not repeated here.
One addition:

### 6.5 The theme's custom checkbox drawing needed its own disabled state

`QCheckBox:disabled` styling is normally handled by Qt's native style,
but Atomic's theme already replaces the checkbox indicator with its own
QSS-drawn box (for the checked/unchecked look used everywhere else in the
app), which also suppresses Qt's native disabled greying. The *Open full
screen* checkbox introduced in §5.2 was the first checkbox in the app
that needed to render disabled, and came up fully opaque instead of
greyed out.

`theme.py` adds explicit `QCheckBox:disabled`,
`QCheckBox::indicator:disabled`, and
`QCheckBox::indicator:checked:disabled` rules so a disabled checkbox
reads as disabled under the custom drawing the same way it would have
under Qt's own.

---

## 7. Configuration and user data

Unchanged from VDD-1.0 §7, with one addition: entries in `games.json`,
`apps.json`, and `websites.json` may now carry an `id` field, backfilled
on migration if absent (see §5.1).

---

## 8. External interfaces

Unchanged from VDD-1.0 §8.

---

## 9. Installation and removal

Unchanged from VDD-1.0 §9.

---

## 10. Known limitations

These carry over from VDD-1.0 §10 unchanged, with one addition:

9. **Fullscreen-on-startup only applies to a Windows-initiated launch.**
   A shortcut, a manual double-click, or running the exe from a terminal
   always opens maximized, never full screen, regardless of the setting.

---

## 11. Verification performed

In addition to the checks recorded in VDD-1.0 §11:

- Drag-to-reorder was exercised on all five card grids (Games, Apps,
  Websites, Anime, Reading, Series), including dragging a card past the
  visible edge of the grid to confirm autoscroll engages, and confirming
  each drag switches that page's sort to Custom Order.
- The custom-order storage helpers were checked against entries missing
  an `id`, to confirm the migration backfill runs before any reorder is
  attempted.
- Fullscreen-on-startup was checked in both states of the checkbox,
  against both a fresh startup registration and one migrated from a
  pre-1.1 install, and confirmed not to affect a hand-launched run.
- The settings-gear icon was checked across sidebar expand/collapse to
  confirm the glyph no longer changes shape.
- The disabled-checkbox QSS rules were checked against the *Open full
  screen* checkbox in both the enabled and disabled state.
- The rebuilt executable's frozen `updater` module was decompiled from
  the packaged archive (not inferred from source) and its `APP_VERSION`
  constant confirmed to read `"1.1"`, ruling out a PyInstaller cache
  reusing a stale build.

---

## 12. Glossary

| Term | Meaning |
|---|---|
| **Cinemeta** | Stremio's public metadata add-on, used for search and episode data |
| **Cour** | A broadcast quarter; long anime are split into several, each a separate catalogue entry |
| **Custom Order** | The sort mode a page switches to automatically once a card is dragged to reorder it |
| **Deep link** | A `stremio://` URL that opens a title directly in the Stremio desktop app |
| **Matte / glossy card** | The two card fills — a flat colour, or a gradient with a highlight |
| **Peek** | The half-scale neighbouring slides flanking Home's carousel |
| **VDD** | Version Description Document — this document |
