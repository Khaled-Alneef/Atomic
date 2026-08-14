# Atomic — Version Description Document

**Version 1.1** · 14 August 2026

---

## 1. Identification

| | |
|---|---|
| System name | Atomic |
| Version | 1.1 |
| Release date | 2026-08-14 |
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

## 3. Changes since 1.0

A maintenance release: no new sections, no data-format changes, and
nothing to migrate — 1.0's saved entries are read as they are.

| # | Change | Where |
|---|---|---|
| 1 | Anime, Reading and Series say **Updating...** while the refresh button works, then **Updated Successfully** or **There is No New Update** | `windows/tracker.py` |
| 2 | Games says **Scanning...** while importing, then **N Games Was Successfully Added** / **1 Game Was Successfully Added** / **No New Games Found** | `windows/games.py`, `helpers/launchers.py` |
| 3 | The dialog Games raised after an import is gone; the result is a corner message like every other one | `windows/games.py` |
| 4 | Leaving full screen no longer flashes the window at its restored size before zooming back out (§7.5) | `main.py`, `helpers/theme.py` |
| 5 | The executable carries a Windows version resource: its Properties now report the version it runs as, and the shell has a changed file identity to re-read its icon from | `packaging/Atomic.spec` |
| 6 | Corner messages sit in the corner again on a multi-monitor setup with mixed display scaling, instead of floating in the middle of the page (§7.6) | `helpers/widgets.py` |

Two smaller corrections came out of the first two. A tracker page fired
the same background lookups when it opened as the refresh button does,
and one of those landing mid-refresh was counted as one of the refresh's
own results — ending it early, on a verdict belonging to a different
lookup. Lookups now carry the number of the run that asked for them, and
results from any other run are ignored. Separately, a page opened with
schedules due for re-checking reported "Updated" in the corner on its
own, with nothing having been asked of it; only the refresh button
reports now.

---

## 4. Inventory of materials released

| Item | Description |
|---|---|
| `Atomic.exe` | The application. 47,143,062 bytes. SHA-256 `1ff9aef8f4490595e74c7f341539931911a73e22799278abc9c0147f50c5927d` |
| `src/` | Full Python source, 8,419 lines across 29 modules |
| `packaging/` | `build.py` and `Atomic.spec`, which produce the executable |
| `docs/VDD-1.0.md` | The previous version's document, kept as released |
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

---

## 5. Inventory of software contents

### Application shell

| Module | Lines | Responsibility |
|---|---|---|
| `main.py` | 731 | Window, sidebar, navigation and page transitions, full screen, startup image prewarm, cursor watchdog |

### Feature pages (`src/windows/`)

| Module | Lines | Responsibility |
|---|---|---|
| `tracker.py` | 1,487 | Anime / Reading / Series pages, entry form, progress sync, release schedules |
| `home.py` | 764 | Dashboard, hero carousel, preview sections |
| `link_grid.py` | 431 | Shared grid behind Apps and Websites |
| `games.py` | 371 | Games page, launcher import, icon handling |
| `websites.py`, `apps.py` | 23 | Thin configurations of `link_grid` |

### Support modules (`src/helpers/`)

| Module | Lines | Responsibility |
|---|---|---|
| `settings_dialog.py` | 883 | Settings window — General, Anime & Series, Reading, Games, Data |
| `theme.py` | 714 | Palette, application-wide stylesheet, native window tweaks |
| `manga_sites.py` | 338 | Reading-site directory and live search across four site engines |
| `updater.py` | 241 | In-app updates from the GitHub repository |
| `mangadex.py` | 238 | MangaDex client and next-chapter estimation |
| `launchers.py` | 244 | Game-launcher scanning, icon extraction and caching |
| `widgets.py` | 297 | Shared widgets — cards, toasts, scroll areas, hover-cursor registry |
| `images.py` | 225 | Image loading, thumbnail caching, letter avatars |
| `icon_extract.py` | 202 | Executable icon extraction |
| `stremio.py` | 179 | Cinemeta search / metadata and Stremio account progress |
| `anilist.py` | 160 | AniList search, list progress, airing schedule |
| `release_schedule.py` | 155 | Chooses a schedule source per medium; formats the hover lines |
| `tvmaze.py` | 105 | TVMaze show lookup and next-episode schedule |
| `app_settings.py` | 105 | Persisted application settings |
| `native_cursor.py` | 82 | Windows cursor correction (see §7.3) |
| `storage.py` | 78 | JSON persistence and per-entry updates |
| `anime_sites.py` | 74 | Video-site directory |
| `nav_config.py` | 65 | Sidebar order and section visibility |
| `child_process.py` | 62 | Environment and window flags for launched programs (see §7.2) |
| `title_match.py` | 61 | Fuzzy title matching for external catalogues |
| `startup.py` | 59 | Launch-on-Windows-startup registration |
| `uninstall.py` | 45 | Full removal of the app and its data |

---

## 6. What this version provides

### 6.1 Tracking with release schedules

Every tracked card says when its next episode or chapter is due, on hover,
alongside the title, status and progress:

```
Bleach: Thousand-Year Blood War        Kingdom (WAN)
Watching                               Reading
Last Season: 4                         Last Chapter: 884
Last Episode: 3                        Next Chapter: 885
Expected: Saturday 5:00 PM             Expected: Tuesday 2:49 AM
Countdown: 1d 3h 50m                   Countdown: 10d 12h 39m
```

The countdown is regenerated on every hover from a cached timestamp, so
it is never stale and never costs a network request to display. Schedules
are looked up in the background and re-checked at most every 12 hours.

Watch progress can be pulled from a connected Stremio account or a public
AniList username; reading progress is kept by hand, with `+`/`-` on each
card.

### 6.2 In-app updates

Settings › General shows the running version with a **Check for Updates**
button. One button runs the whole flow: check, then download and install.
Atomic closes, the executable is replaced, and it reopens on the new
version — saved entries are untouched, since only the `.exe` changes.

A release is identified by a git tag on this repository (`v1.0`, `v1.1`,
…), and what gets downloaded is the `Atomic.exe` committed at that tag.
GitHub's contents API reports that file's git blob hash, and the download
is verified against it before anything is replaced: a truncated or
tampered download is discarded rather than installed.

### 6.2.1 How a release is published

The repository separates the two things `main` was previously doing at
once:

| Branch | Holds |
|---|---|
| `development` | The work, commit by commit, including every intermediate `Rebuild Atomic.exe` step |
| `main` | One commit per *released* version, each tagged — nothing else ever lands here |

`main` is what the updater reads, so this keeps unreleased work from ever
being visible to a running copy of the app: until a version is put on
`main` and tagged, `check_for_update` cannot see it, however many commits
`development` is ahead by.

The two branches deliberately share no history — `main` was restarted at
1.0 as a single commit — so a release is taken as a *snapshot* rather
than merged (a merge would refuse, as unrelated histories):

```
git checkout main
git read-tree -u --reset development    # main's tree becomes development's
git commit -m "Atomic 1.1"
git tag -a v1.1 -m "Atomic 1.1"
git push origin main && git push origin v1.1
git checkout development
```

The resulting commit has the previous release as its parent and the new
version's exact contents as its tree, so `main` reads as a list of
releases and nothing else. `APP_VERSION` in `helpers/updater.py` must be
bumped on `development` first, or the new build goes on offering itself
an update.

### 6.3 Interface

- A dark blue-violet palette, with every tone derived from six specified
  colours. Home, Games, Apps and Websites use a flat matte card fill; the
  tracker's poster grids keep a glossy one.
- A collapsible sidebar, drag-to-reorder, with any section hideable —
  optionally from the Home page as well as the sidebar.
- **F11** enters full screen, **Escape** leaves it, returning a maximized
  window to maximized rather than dropping it to a restored size — and
  going straight there, without the restored-size flash Windows' own
  maximize animation used to put in the way (§7.5).
- Anything that takes long enough to wonder about says so where it
  happens, in the corner of the window, and the same message is then
  replaced by its result rather than a dialog appearing over the page:

  | Where | While working | Afterwards |
  |---|---|---|
  | Anime · Reading · Series, ⟳ | Updating... | Updated Successfully · There is No New Update |
  | Games, ⟳ | Scanning... | 3 Games Was Successfully Added · 1 Game Was Successfully Added · No New Games Found |
  | Settings › Games, per launcher | Scanning... | *(as above, prefixed with the launcher's name)* |

  "Updated Successfully" means something genuinely moved — an episode or
  chapter that was not known before, or watch progress that has advanced
  since the refresh began. A schedule whose *estimated time* drifted
  without the chapter changing is not new information and does not
  count (see §7.1: the manga estimate is projected from a release
  rhythm, so it moves on its own as a predicted slot passes).

### 6.4 Measured performance

Page build time, against a library of 12 tracked entries, 8 games and 12
links:

| Page | First visit | Return visit |
|---|---|---|
| Home | 268 ms once at launch | ~10 ms |
| Reading | 8 ms | 5 ms |
| Websites | 3 ms | 3 ms |
| Anime | 2 ms | 2 ms |

Each cover or icon costs roughly 14 ms to decode and resize, and pages
are rebuilt on every visit and on every sort change. Those results are
cached, and the expensive decode — 6.3 ms of the 14, pure Pillow with no
Qt involvement — is performed on a background thread at startup.

---

## 7. Design notes

Six decisions are recorded here because the reasoning is not obvious
from the code, and each cost real investigation to arrive at.

### 7.1 Manga schedules are estimates, and say so

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

### 7.2 Launched programs get a scrubbed environment

PyInstaller's bootloader records where the running app unpacked itself in
`_PYI_*` environment variables, and those are inherited by every program
Atomic launches. For an ordinary program that is harmless. For another
PyInstaller-built program it is fatal: its bootloader sees the variables
already set, assumes it has been unpacked, and looks for its Python DLL
in *Atomic's* folder. If Atomic has exited, that folder is gone and the
launched program dies with "Failed to load Python DLL".

The updater hit exactly this, since it relaunches Atomic itself. Games
and apps launched from Atomic were vulnerable to the same thing. So
`child_process.clean_env()` strips those variables from anything Atomic
starts, and `child_process.flags()` adds `CREATE_NO_WINDOW` so launching
through the shell never flashes a console window.

### 7.3 The cursor correction reaches past Qt

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

### 7.4 Each entry is saved on its own

Several pages are backed by the same file and each holds its own copy in
memory — Home lists games, apps, websites and tracker entries that other
pages own, and Anime and Reading share `tracker.json`. Writing a whole
list back from one of them therefore restores a snapshot that may be
minutes stale, silently undoing everything saved since.

That was a real defect: reordering a game discarded freshly-extracted
icons and whole batches of imported games. Every edit now re-reads the
file and updates only the entry that changed
(`storage.update_entry`).

### 7.5 Leaving full screen turns off Windows' animation

Windows animates a window being maximized by zooming it out from
wherever its *restored* size sits. A window that went full screen from
maximized is, underneath the full-screen frame, a restored-size window —
so the return trip played that zoom: the window appeared at its small
restored size for a moment, then flew back out to fill the screen.

This was measured rather than judged by eye, by reading four screen
pixels just inside the maximized window's corners every 10 ms across the
transition. The window failed to cover its own maximized area for about
120 ms of it — a dozen consecutive frames showing the desktop where the
window should have been.

Two repairs were tried. Keeping the maximized flag set alongside the
full-screen one, so that leaving changes only one state bit, measured
identically: the same 120 ms. Suppressing the animation for the duration
of the change — `DWMWA_TRANSITIONS_FORCEDISABLED`, set before the state
change and cleared after it — measured zero frames. That is what ships
(`theme.without_window_animation`). It is cleared afterwards rather than
left off, so minimizing to the taskbar and restoring from it still
animate as they always did.

### 7.6 Window positions are read, never mapped

Everything the app places against the window's own corner reads
`window.geometry()`, which is already in global coordinates, rather than
mapping a local point out with `mapToGlobal()`.

The corner messages were placed the second way and landed in the middle
of the page. On two monitors with different scale factors - the primary
at 125%, the app maximized on a 100% one - `mapToGlobal(width, height)`
returned 826 for a window whose bottom edge is genuinely at 1032: the
right answer divided by the *other* screen's scale factor. Every message
was then positioned against a bottom edge some 200px too high.

A coordinate that is already global cannot disagree with itself that way,
which is why it is preferred here even though the mapping call reads more
naturally.

---

## 8. Configuration and user data

All user data lives in `%APPDATA%\Atomic\`. Nothing is written beside the
executable, so the `.exe` can be moved or replaced freely — which is what
makes in-app updating safe.

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

## 9. External interfaces

| Service | Endpoint | Used for |
|---|---|---|
| AniList | `graphql.anilist.co` | Anime search, public list progress, airing schedule |
| TVMaze | `api.tvmaze.com` | Series lookup by IMDb id, next-episode schedule |
| MangaDex | `api.mangadex.org` | Manga matching and release history |
| Stremio Cinemeta | `v3-cinemeta.strem.io` | Anime/Series search, metadata, latest aired episode |
| Stremio Account | `api.strem.io` | Watch progress from a connected account |
| GitHub | `api.github.com`, `raw.githubusercontent.com` | Release tags and the update download |
| Reading sites | user-configured | Title search and chapter pages |
| Favicon lookup | `google.com/s2/favicons` | Website icons, when a site serves none itself |

No API keys are required. Every lookup fails soft: a flaky connection or
an unreachable service means missing suggestions or a missing schedule,
never an error dialog or a crash.

---

## 10. Installation and removal

**Install.** Run `Atomic.exe`. There is no installer and no dependency to
satisfy. Data is created under `%APPDATA%\Atomic\` on first launch.

**Start with Windows.** Settings › General › *Launch on Windows startup*
registers the app under `HKCU\...\CurrentVersion\Run`.

**Update.** Settings › General › *Check for Updates*. Replacing
`Atomic.exe` by hand also works, and saved data is untouched either way —
close the app first, since Windows will not overwrite a running
executable.

**Removal.** Settings › Data › Uninstall removes every saved file, the
startup registration, and the executable itself. Deleting `Atomic.exe`
and `%APPDATA%\Atomic\` by hand achieves the same.

---

## 11. Known limitations

These are current behaviours, not defects scheduled for repair.

1. **Windows only.** Executable icon extraction, the dark title bar,
   startup registration, the console-window suppression and the cursor
   correction are all Win32-specific.
2. **Manga schedules resolve for a minority of titles.** By design — see
   §7.1. A title with no current MangaDex feed shows no estimate.
3. **Anime schedules depend on title matching.** An entry whose title
   does not match an AniList record closely enough gets no schedule; the
   IMDb id is used as a second chance via TVMaze where one exists.
4. **Real watch progress needs a connected source.** Without a Stremio
   account or AniList username, Anime/Series progress must be entered by
   hand. Reading progress is always manual — no site exposes it.
5. **Reading-site search covers four engine shapes.** A site matching
   none of them still opens, but offers no live suggestions.
6. **Updates need the executable to sit somewhere writable.** Atomic
   checks before closing and says so rather than quitting and failing.
   GitHub also rate-limits anonymous callers, which is reported plainly
   when it happens.
7. **Home costs ~270 ms on the first visit of a session**, spent on Qt
   polishing the carousel's stylesheet. It happens while the window is
   already appearing and does not recur.
8. **The executable is committed to the repository.** Deliberate — it is
   both the always-available build and what the updater downloads — at
   the cost of a large binary in history.

---

## 12. Verification performed

- The update path was exercised end to end against a real release: an
  older build checked, downloaded 48 MB, verified the checksum, replaced
  its own executable and reopened on the new version. Rejection of a
  tampered checksum and of a truncated download was confirmed separately.
- The stuck-cursor defect was reproduced against the real OS cursor and
  confirmed corrected across repeated runs, with hover behaviour
  re-checked on cards, buttons and empty page areas.
- Console-window suppression was verified by enumerating window classes
  while the update script ran.
- Full screen was verified in five combinations, including returning a
  maximized window to maximized. For 1.1 the exit was additionally
  measured off the screen itself, at 10 ms intervals, before and after
  the change and against the alternative repair — 12 frames of visible
  gap became 0 (§7.5).
- The refresh and import messages were exercised against fabricated
  libraries with the network stubbed out, twelve cases in all: a chapter
  that moved and one that only had its estimate re-projected, an entry
  with no schedule at all, watch progress that advanced and progress that
  did not, a page with nothing linked to sync, imports finding none, one
  and three games, a second press of the refresh button while the first
  was still running, and the opening message itself. The counting defect
  described in §3 was found by that exercise, not in use.
- Schedule lookups were exercised against a real library for all three
  media, including titles that correctly yield no schedule.
- Section hiding was verified in all four combinations, including the
  case where Anime and Reading share one Home section.
- The saved-list defect was reproduced — an entry imported while a page
  was open, then erased by moving another entry — and confirmed fixed.
- The packaged executable's bundle contents were verified module by
  module, after a stale build once shipped without the updater in it.

---

## 13. Glossary

| Term | Meaning |
|---|---|
| **Cinemeta** | Stremio's public metadata add-on, used for search and episode data |
| **Cour** | A broadcast quarter; long anime are split into several, each a separate catalogue entry |
| **Deep link** | A `stremio://` URL that opens a title directly in the Stremio desktop app |
| **Matte / glossy card** | The two card fills — a flat colour, or a gradient with a highlight |
| **Peek** | The half-scale neighbouring slides flanking Home's carousel |
| **VDD** | Version Description Document — this document |
