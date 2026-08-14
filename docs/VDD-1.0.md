# Atomic — Version Description Document

**Version 1.0** · 14 August 2026

---

## 1. Identification

| | |
|---|---|
| System name | Atomic |
| Version | 1.0 |
| Release date | 2026-08-14 |
| Release commit | `8926b1a` |
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
| `Atomic.exe` | The application. 48,423,173 bytes. SHA-256 `1ea2af60500633a2e1f52f449365678f6fe48faa82e7ef7a3d16bb6c71b65511` |
| `src/` | Full Python source, 7,730 lines across 28 modules |
| `packaging/` | `build.py` and `Atomic.spec`, which produce the executable |
| `docs/VDD-1.0.md` | This document |

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

---

## 4. Inventory of software contents

### Application shell

| Module | Lines | Responsibility |
|---|---|---|
| `main.py` | 695 | Window, sidebar, navigation and page transitions, startup image prewarm, cursor watchdog |

### Feature pages (`src/windows/`)

| Module | Lines | Responsibility |
|---|---|---|
| `tracker.py` | 1,386 | Anime / Reading / Series pages, entry form, progress sync, release schedules |
| `home.py` | 762 | Dashboard, hero carousel, preview sections |
| `link_grid.py` | 429 | Shared grid behind Apps and Websites |
| `games.py` | 360 | Games page, launcher import, icon handling |
| `websites.py`, `apps.py` | 23 | Thin configurations of `link_grid` |

### Support modules (`src/helpers/`)

| Module | Lines | Responsibility |
|---|---|---|
| `settings_dialog.py` | 747 | Settings window — General, Anime & Series, Reading, Games, Data |
| `theme.py` | 633 | Palette and the application-wide stylesheet |
| `manga_sites.py` | 338 | Reading-site directory and live search across four site engines |
| `mangadex.py` | 238 | MangaDex client and next-chapter estimation |
| `launchers.py` | 232 | Game-launcher scanning, icon extraction and caching |
| `widgets.py` | 228 | Shared widgets — cards, toasts, scroll areas, hover-cursor registry |
| `images.py` | 225 | Image loading, thumbnail caching, letter avatars |
| `icon_extract.py` | 202 | Executable icon extraction |
| `stremio.py` | 179 | Cinemeta search / metadata and Stremio account progress |
| `anilist.py` | 160 | AniList search, list progress, airing schedule |
| `release_schedule.py` | 155 | Chooses a schedule source per medium; formats the hover lines |
| `tvmaze.py` | 105 | TVMaze show lookup and next-episode schedule |
| `app_settings.py` | 105 | Persisted application settings |
| `native_cursor.py` | 82 | Windows cursor correction (see §6.2) |
| `storage.py` | 78 | JSON persistence and per-entry updates |
| `anime_sites.py` | 74 | Video-site directory |
| `nav_config.py` | 65 | Sidebar order and section visibility |
| `title_match.py` | 61 | Fuzzy title matching for external catalogues |
| `startup.py` | 59 | Launch-on-Windows-startup registration |
| `uninstall.py` | 45 | Full removal of the app and its data |

---

## 5. Changes in this version

### 5.1 New capability

**Release schedules on hover.** Every tracked card now says when its next
episode or chapter is due, on top of the title, status and progress it
already showed:

```
Bleach: Thousand-Year Blood War        Kingdom (WAN)
Watching                               Reading
Last Season: 4                         Last Chapter: 884
Last Episode: 3                        Next Chapter: 885
Expected: Saturday 5:00 PM             Expected: Tuesday 2:49 AM
Countdown: 1d 3h 50m                   Countdown: 10d 12h 39m
```

The countdown is regenerated on every hover from a cached timestamp, so
it is never stale and never costs a network request to display.
Schedules are looked up in the background and re-checked at most every
12 hours.

**Section visibility on Home.** Settings › General gains *"Hide them from
the Home page too"*. Off by default, where hiding a section only removes
its sidebar entry; on, a hidden section also leaves Home entirely —
preview row, quick list, and carousel slides.

**Visual redesign.** A new palette applied across every page, with all
tones derived from six specified colours. Home, Games, Apps and Websites
use a flat matte card fill; the tracker's poster grids keep a glossy one.

### 5.2 Defects corrected

| # | Defect | Resolution |
|---|---|---|
| 1 | The pointing-hand cursor stayed on screen over every page after closing a dialog or launching a game, until the app was restarted | Corrected natively — see §6.2 |
| 2 | Reordering a game silently discarded recent changes to the games list, including freshly-extracted icons and whole batches of imported games | All edits re-read the file and update only the entry that changed |
| 3 | Game icons could show as coloured letters on Home until the Games page was opened | Home re-extracts missing icons itself; the icon cache is keyed by the game's path so re-extraction is idempotent |
| 4 | Opening a page with many entries was slow, and the delay recurred on every visit | Thumbnails are cached and pre-decoded off-thread at startup — see §5.3 |
| 5 | A strip of bare window showed down the right-hand side, resembling a second sidebar | Pages now follow the container's width, not only the window's |
| 6 | Anime and Series stated release times differently from Reading | All three now read `Expected:` / `Countdown:` |

### 5.3 Measured performance change

Page build time, against a library of 12 tracked entries, 8 games and 12
links:

| Page | Before | After |
|---|---|---|
| Reading | 293 ms every visit | 8 ms first, 5 ms after |
| Home | 475 ms every visit | 268 ms once at launch, ~10 ms after |
| Websites | 32 ms | 3 ms |
| Anime | 9 ms | 2 ms |

Each cover or icon cost roughly 14 ms to decode and resize, and pages are
rebuilt on every visit and on every sort change. Those results are now
cached, and the expensive decode — 6.3 ms of the 14, pure Pillow with no
Qt involvement — is performed on a background thread at startup.

---

## 6. Design notes on two decisions

### 6.1 Manga schedules are estimates, and say so

AniList and TVMaze both publish real airing schedules, so Anime and
Series state their release times as fact. MangaDex has no equivalent
field — scanlation release dates are not announced anywhere — so the
Reading page extrapolates from release history and labels the result
*Expected*.

The estimate is deliberately conservative, because a confidently wrong
countdown is worse than none:

- The title must genuinely match. MangaDex's search returns *The
  Beginning After the End* for *The World After The End*, and inheriting
  an unrelated series' schedule is the worst available failure.
- Chapters are read across all languages. Most tracked titles are
  licensed in English, leaving their English feed a near-empty stub while
  other feeds are current.
- Only the single language furthest into the series is measured. Mixing
  them measures translators racing each other, not the series' pace.
- The rhythm must look like one: a plausible interval, a feed that is
  still current, and enough chapters to measure.

Anything failing those checks produces no estimate at all. Against a
real 8-title library this resolves 3; the rest show nothing, which is the
honest answer.

### 6.2 The cursor correction reaches past Qt

`native_cursor.py` sets the Windows cursor through the Win32 API. That is
unusual enough to record why.

After a modal dialog closes, Qt stops issuing native cursor calls for the
main window. Its own state is entirely correct — no override cursor, no
widget claiming one, the widget under the pointer reporting a plain arrow
— yet Windows goes on painting whichever cursor it was last handed, which
is the pointing hand from whatever button was clicked on the way in.

This was established by reading the OS cursor back with `GetCursorInfo`
rather than by inference. Every repair available through Qt was measured
and left it unchanged: pushing and popping override cursors of three
shapes, giving the window an explicit arrow, releasing the hand from
every widget holding one, and polling on a timer. Setting the cursor
through Win32 does correct it.

The correction is applied only when Qt and the OS genuinely disagree, so
Qt retains full control in normal operation. A watchdog checks on pointer
movement: 1.0 µs per tick while the pointer is still, 8 µs when it has
moved, at roughly eight ticks per second.

---

## 7. Configuration and user data

All user data lives in `%APPDATA%\Atomic\`. Nothing is written beside the
executable, so the `.exe` can be moved or replaced freely.

| File | Contents |
|---|---|
| `tracker.json` | Anime and Reading entries |
| `series.json` | Series entries |
| `games.json` | Games, their paths and cached icon paths |
| `apps.json`, `websites.json` | App and website entries |
| `anime_sites.json`, `manga_sites.json` | Configured video and reading sites |
| `settings.json` | Sidebar order, hidden sections, launcher directories, connected accounts |
| `image_cache/` | Downloaded covers, favicons and extracted executable icons |
| `ui_assets/` | Generated interface assets |

**Credentials.** Connecting a Stremio account stores only the session key
returned by Stremio's own login endpoint. The password is used for that
single request and never written to disk. An AniList username is stored
as plain text — it is public information and no password is involved.

---

## 8. External interfaces

| Service | Endpoint | Used for |
|---|---|---|
| AniList | `graphql.anilist.co` | Anime search, public list progress, airing schedule |
| TVMaze | `api.tvmaze.com` | Series lookup by IMDb id, next-episode schedule |
| MangaDex | `api.mangadex.org` | Manga matching and release history |
| Stremio Cinemeta | `v3-cinemeta.strem.io` | Anime/Series search, metadata, latest aired episode |
| Stremio Account | `api.strem.io` | Watch progress from a connected account |
| Reading sites | user-configured | Title search and chapter pages |
| Favicon lookup | `google.com/s2/favicons` | Website icons, when a site serves none itself |

No API keys are required. Every lookup fails soft: a flaky connection or
an unreachable service means missing suggestions or a missing schedule,
never an error dialog or a crash.

---

## 9. Installation and removal

**Install.** Run `Atomic.exe`. There is no installer and no dependency to
satisfy. Data is created under `%APPDATA%\Atomic\` on first launch.

**Start with Windows.** Settings › General › *Launch on Windows startup*
registers the app under `HKCU\...\CurrentVersion\Run`.

**Upgrade.** Replace `Atomic.exe`. Saved data is untouched. Close the app
first — Windows will not let a running executable be overwritten.

**Removal.** Settings › Data › Uninstall removes every saved file, the
startup registration, and the executable itself. Deleting `Atomic.exe`
and `%APPDATA%\Atomic\` by hand achieves the same.

---

## 10. Known limitations

These are current behaviours, not defects scheduled for repair.

1. **Windows only.** Executable icon extraction, the dark title bar,
   startup registration and the cursor correction are all Win32-specific.
2. **Manga schedules resolve for a minority of titles.** By design — see
   §6.1. A title with no current MangaDex feed shows no estimate.
3. **Anime schedules depend on title matching.** An entry whose title
   does not match an AniList record closely enough gets no schedule; the
   IMDb id is used as a second chance via TVMaze where one exists.
4. **Real watch progress needs a connected source.** Without a Stremio
   account or AniList username, Anime/Series progress must be entered by
   hand. Reading progress is always manual — no site exposes it.
5. **Reading-site search covers four engine shapes.** A site matching
   none of them still opens, but offers no live suggestions.
6. **Home costs ~270 ms on the first visit of a session**, spent on Qt
   polishing the carousel's stylesheet. It happens while the window is
   already appearing and does not recur.
7. **The executable is committed to the repository.** Deliberate, so a
   working build is always available, at the cost of a large binary in
   history.

---

## 11. Verification performed

- The reported cursor defect was reproduced against the real OS cursor
  and confirmed corrected across repeated runs, with hover behaviour
  re-checked on cards, the sidebar buttons and empty page areas.
- Schedule lookups were exercised end-to-end against a real library for
  all three media, including titles that correctly yield no schedule.
- Tooltip wording was verified identical across Anime, Series and
  Reading.
- Section hiding was verified in all four combinations, including the
  case where Anime and Reading share one Home section.
- The games-list defect was reproduced — an entry imported while the page
  was open, then erased by moving another entry — and confirmed fixed.
- Page-build timings were measured before and after (§5.3).
- Every page was built without error, and the packaged executable was
  launched from a clean build and confirmed to start.

---

## 12. Glossary

| Term | Meaning |
|---|---|
| **Cinemeta** | Stremio's public metadata add-on, used for search and episode data |
| **Cour** | A broadcast quarter; long anime are split into several, each a separate catalogue entry |
| **Deep link** | A `stremio://` URL that opens a title directly in the Stremio desktop app |
| **Matte / glossy card** | The two card fills — a flat colour, or a gradient with a highlight |
| **Peek** | The half-scale neighbouring slides flanking Home's carousel |
| **VDD** | Version Description Document — this document |
