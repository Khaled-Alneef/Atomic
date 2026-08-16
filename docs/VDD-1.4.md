# Atomic — Version Description Document

**Version 1.4** · 16 August 2026

---

## 1. Identification

| | |
|---|---|
| System name | Atomic |
| Version | 1.4 |
| Release date | 2026-08-16 |
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
| `Atomic.exe` | The application. 46,714,549 bytes. SHA-256 `b71b19a85f72a65cae9dbb6e3cff68aafe6c5b7b45ead5b70098deba54d1a94c` |
| `src/` | Full Python source, 12,036 lines across 36 modules (10,774 across 34 at 1.3) |
| `src/filter_icon.png` | The tracker filter button's icon, bundled into the executable |
| `packaging/` | `build.py` and `Atomic.spec`, which produce the executable |
| `docs/VDD-1.4.md` | This document |

Build environment unchanged from VDD-1.1 §3.

Sixteen development builds (1.3.1–1.3.14, plus two re-releases of the
same numbers) stand between 1.3 and this release. Line counts here are
measured per file and summed; VDD-1.3's figure was taken with a method
that undercounted, so the two are not directly comparable — the 1.3
column above is a recount of that same tree.

---

## 4. Inventory of software contents

Two modules are new since 1.3:

| Module | Lines | Responsibility |
|---|---|---|
| `helpers/net.py` | 91 | The one bounded HTTP read — size cap plus wall-clock deadline |
| `helpers/logs.py` | 114 | File logging and the Qt-slot exception hook |

| Module changed | Lines | What changed |
|---|---|---|
| `windows/tracker.py` | 2,357 | Films as their own type; per-entry last-watched; the filter; both catalogues searched; Stremio as the only progress source |
| `helpers/anime_sites.py` | 1,071 | Whole-chain deadlines, `probe_site`, Netflix as a default site, full-title-only knowledge-base queries |
| `helpers/settings_dialog.py` | 1,024 | Stremio account section; Crunchyroll and AniList progress settings removed; site checking |
| `windows/home.py` | 774 | Respects the per-entry last-watched tick |
| `windows/link_grid.py` | 458 | Page subtitle and Move Up/Down removed |
| `windows/games.py` | 396 | Drag hint and Move Up/Down removed |
| `helpers/images.py` | 265 | `tinted_asset` — a bundled image recoloured from the palette and tagged for DPI |
| `helpers/anilist.py` | 227 | `RateLimited` for 403/429; every progress function removed |
| `helpers/storage.py` | 214 | utf-8-sig, quarantine instead of overwrite, atomic write with `.bak` |
| `helpers/stremio.py` | 184 | Movie catalogue search; bounded reads |
| `helpers/wikidata.py` | 167 | Crunchyroll ids via P11330; full-title queries only |
| `helpers/lookup_pool.py` | 115 | `submit_latest` — one at a time, superseded jobs dropped |

`helpers/mangadex.py`, `helpers/tvmaze.py`, `helpers/manga_sites.py`,
`helpers/app_settings.py`, `helpers/nav_config.py`, `helpers/theme.py`,
`helpers/updater.py` and `src/main.py` also changed.

---

## 5. What this version provides

### Watch progress comes from Stremio, and from nothing else

Four other sources were built and removed across this cycle, in order: an
authenticated Crunchyroll client, a pasted Crunchyroll browser token,
AniList by username, and MAL-Sync feeding AniList. Each worked in
isolation and was unreliable in use, and they shared one failure: a
source that is silently wrong is worse than no source, and with several
of them there is no way to tell which is which. A card once read episode
7 for a show its owner had watched two episodes of.

The Crunchyroll route is closed for reasons that will not change on their
own. Minting a token from an email and password needs a client credential
Crunchyroll issues to no third party, and the published one from the
archived `crunchyroll-go` answers `client_inactive` on both
`www.crunchyroll.com` and `beta-api.crunchyroll.com` — measured without
any account, using deliberately fake credentials, because a live client
answers `invalid_grant` and a dead one refuses before it looks at an
account at all. A token lifted from a browser session works and expires
inside an hour.

So `anilist.py` keeps what it is good at — airing schedules, and
resolving a title to its Crunchyroll or Netflix page — and holds no
progress functions at all. The AniList username setting and every
Crunchyroll progress setting are gone from Settings.

### Films are tracked, and are not pretended to have episodes

`Movie` is a type of its own, with its own watching statuses, searched
against Cinemeta's *movie* catalogue — a film searched against the series
catalogue finds nothing whatever. The page is called **Movies & Series**;
its key remains `series`, so saved sidebar order and `series.json` carry
over untouched.

A film has no episode to be on, so it carries no progress at all: no
number on its card or on Home, no season/episode fields, no hover
columns, and it is skipped by the Stremio sync rather than costing a
request per film for something nothing displays.

### The two words "Last Episode" no longer mean two different things

The hover tooltip labelled `latest_available` — how far the release has
got — "Last Season / Last Episode", while the Add/Edit spinners directly
below used the same two labels for `progress`, which is what you watched.
Manga was worse: its "Last Chapter" box held the reading site's latest
release and was editable, so a number typed there sat in the one field
the next lookup overwrites.

Hover now reads **Last Released** Season/Episode/Chapter; the spinners
read **Last Watched**; and the released chapter is read-only with its
arrows removed, because a spinner that ignores clicks still invites them.

### Whether a last-watched number is shown is per entry

A tick in Add/Edit — "Show Last Watched …" — hides the spinners and the
card's +/- together. New entries start with it off, since nothing is
known about them yet and a visible E00 is a claim nobody has made; an
entry saved before the tick existed defaults to on, because it already
displayed a number.

That also restores the +/- buttons where they were never the problem.
They were removed wholesale in 1.3.8 because Stremio overwrites a
hand-set number on its next sync — true of a Stremio entry, false of one
pinned to Netflix or Crunchyroll, which has no source that will ever fill
it in. Stremio entries now show the number they sync without handing it
over to be edited; everything else is yours to set. Switching the Video
Website mid-dialog flips that live.

### Films appear when you search for them

Typing a film's name in Add/Edit returned only series, because the form
opens on Series and films live in a separate catalogue. Cinemeta's movie
catalogue was never at fault: measured directly, it answers 7 matches for
"Inception" and 16 for "Your Name". A page offering both types now asks
both catalogues, each suggestion names its type when both were asked, and
picking a film sets Type to Movie with it — otherwise it saved as a
Series, with statuses and episode tracking a single video has no use for.

### Opening a page is the refresh

The refresh button is gone. Schedules already refreshed on page load and
are cached for twelve hours; what is new is the progress sync, throttled
to once per ten minutes per page so that navigating back and forth does
not re-ask Stremio about every entry. The long "Updating…" toast is gone
rather than shortened — it held until the slowest source answered, which
made a refresh feel like something to wait for. Results now land on cards
as they arrive, and a page with nothing to sync says nothing.

### Filtering, and the end of a second way to reorder

A filter button sits beside each tracker page's search box, icon only:
statuses on every page, plus types where there is a choice of them. Its
menu stays open while ticks are used — `QMenu` dismisses itself on mouse
release, so a checkable item is triggered and the release swallowed —
with the grid redrawing underneath as each tick lands. Dragging is
disabled while a filter narrows the grid, for the reason a search already
disabled it: a drop saves the order that is on screen, which is not the
whole order then.

Move Up / Move Down are gone from every page, along with the methods
behind them, and so are the drag hints and the Apps/Websites page
subtitles. Dragging a card onto the slot you want is how reordering
works, on all six pages.

### Netflix reaches installs that already existed

Netflix shipped as a default video site in 1.3.8 and nobody saw it:
`DEFAULT_SITES` only ever seeded an *empty* sites file, so anyone who
already had Crunchyroll saved kept exactly what they had. Each default is
now offered once to an existing install too, tracked by name so deleting
it afterwards makes it stay deleted. Any watched type can be pinned to a
video website now, not only Anime — that rule had been written as
`== "Anime"` in twelve separate places, which is why a series could never
be set to open on Netflix.

### Correctness work carried from the 1.3.1 pass

Eight defects, each invisible in its own way, were fixed together early
in this cycle and are described in full in that commit; in summary:

- Unbounded reads. `urlopen`'s timeout bounds one socket operation, not a
  transfer, so a host sending a byte per second held a lookup thread
  forever. `helpers/net.py` is now the single bounded read, used by nine
  call sites; measured at 6.0s each against such a server, where nothing
  returned before.
- A catastrophic-backtracking regex in `manga_sites.py` that could freeze
  the UI, since the regex engine holds the GIL: 224ms on a malformed 1MB
  fragment, 0ms after splitting on `</a>` instead of scanning.
- A dead host costing ~24s per resolution: one deadline now spans the
  whole chain, measured at 12s.
- A bare thread per debounced keystroke — the shape that once put 651
  connections in flight — replaced by `lookup_pool.submit_latest`.
- Any exception escaping a Qt slot aborted the process through `qFatal`
  with no traceback and no log. It logs and survives; the control run
  without the hook still dies `0xC0000409`.
- `storage.load` returned `{}` for a file it could not parse and the next
  save made that true, which is how a BOM'd `settings.json` destroyed a
  stored setting. utf-8-sig, quarantine, atomic write, `.bak`.
- AniList's `403` reported as "not on your list"; it raises `RateLimited`
  and reaches the dialog as itself.
- Crunchyroll had no second source for links; Wikidata's P11330 answers
  for 4 of 6 real titles with AniList stubbed to 403 on every request.

### Two shipped defects found and fixed since

**A sequel was pinned to its predecessor's page.** Asking Wikidata with
the subtitle-stripped title variant matched the parent franchise:
"Bleach: Thousand-Year Blood War" has no id of its own, so the lookup
asked for "Bleach", scored the 2004 series at 1.00, and returned that
show's Crunchyroll and Netflix pages to be saved permanently. Netflix
shipped that way in 1.3. Both now ask with the full title only.

**Mojibake on screen.** Eight lines across two files had their non-ASCII
characters re-encoded from their own mojibake — `—` became `â€”`, `⟳`
became `âŸ³` — visible on the Last Season placeholder, the site
dropdown's "— None —", the refresh button and the Settings site list. The
cause was editing those files through PowerShell's
`Get-Content`/`Set-Content`, which reads in the system codepage and
writes UTF-8. Nothing complains: the file still parses and the tests
still pass, and the damage is only visible on screen. Repaired per line
via cp1252, per line rather than per file because `manga_sites.py` holds
Arabic that cannot round-trip that way at all. `CLAUDE.md` now forbids
those cmdlets for source files.

Settings' section list also drew "Movies  Series" with a hole in it —
not a typo in the name, but `QCheckBox` reading a single `&` as a
mnemonic marker and swallowing it.

---

## 6. Design notes

**Why the season is read per AniList season while the schedule lookup
lasted.** AniList files each season as its own work and counts episodes
from 1 within it, while this application's cards use one title and a
season number, so asking about "One-Punch Man" answered about season 1
forever. Two traps were paid for live: a one-episode short carried the
franchise name *only as a synonym* and took season 3's slot, which is why
matching uses an entry's own titles and never its synonyms; and a cour
split ("Season 3 Part 2") is still season 3, which is why the season
pattern deliberately does not match "Part". The progress half of that
work was later removed with the rest of AniList progress; the reasoning
is recorded because the same shapes recur in the schedule lookup.

**Why `tracks_progress()` is a question and not a comparison.** A film's
absence of progress is asked as one predicate rather than spelled
`!= "Movie"` where it matters — the same mistake `== "Anime"` was before
`VIDEO_TYPES` existed, which is what stopped a series ever opening on
Netflix.

**Why the filter menu is attached with `setMenu`.** Qt places a button's
menu against the button itself, so there is no `mapToGlobal` here to
return coordinates divided by the other screen's scale factor — the trap
`.claude/rules/ui.md` records from toasts landing 200px off.

**Why the filter icon is recoloured rather than shipped in colour.**
Every other glyph on those buttons is text drawn from the palette, so a
white PNG sat brighter than its neighbours. A `SourceIn` fill replaces
colour and keeps alpha, so the shape and its antialiased edges survive;
it is scaled before it is filled, since filling first leaves a flat block
for the scaler to blur.

**Why visibility of the Stremio note has one owner.** It was set in two
places — from the entry type in `_update_labels`, and from the tick in
`_update_progress_visibility` — and the first runs after the second, so
on a new entry it re-showed the note under boxes that were hidden. Found
by the harness, not by reading.

---

## 7. Configuration and user data

As VDD-1.2 §7, with these changes:

- Entries carry `show_last_watched` (boolean). Absent means shown, so
  entries written by earlier versions are unaffected.
- `Movie` is a valid entry `type` in `series.json`. The file name and the
  page key are unchanged.
- Defaults offered to an existing install are recorded by name in
  settings, so a deleted default stays deleted.
- The stored AniList username and every stored Crunchyroll credential or
  token are no longer read or written. Existing values are left in place
  rather than deleted.
- `atomic.log` is written beside the other data files.

---

## 8. External interfaces

As VDD-1.3 §8, with these changes:

- Cinemeta's **movie** catalogue is queried in addition to `series`.
- Wikidata's **P11330** (Crunchyroll series id) is queried alongside
  P1874. Asked with the full title only.
- AniList is used for airing schedules and streaming links only. No
  progress query is made against it.
- No Crunchyroll endpoint is contacted at all. The client, its token and
  its diagnostic were removed.

No API keys anywhere. The Stremio `authKey` remains a first-party login
token, and is the one accepted exception.

---

## 9. Installation and removal

Unchanged from VDD-1.2 §9.

---

## 10. Known limitations

- **Watch progress is only ever as good as Stremio's record.** A title
  watched anywhere else does not advance on its own; the number is set by
  hand, and on a Stremio entry the next sync overwrites a hand-set value
  by design. Stremio records a specific episode only once one has
  actually been played through Stremio itself.
- **A Stremio entry's last-watched number cannot be edited.** This is
  deliberate — the sync owns it — but a title Stremio has no history for
  therefore shows nothing until it does, unless the entry is pinned to
  another site.
- **Nothing on screen explains why dragging is unavailable** while a
  search or filter narrows the grid. The hint line that used to say so
  was removed at the owner's request.
- **Filter selections do not persist.** Pages rebuild from scratch on
  every visit, so a filter is cleared by navigating away.
- **Wikidata's streaming-id coverage has a cliff.** Measured over the
  owner's real tracked titles, 0 of 3 current-season entries carry a
  Crunchyroll, Netflix or Prime id, though all three exist as entities
  with exact label matches. Headline and older titles carry them. A title
  with no id resolves through AniList if it has a row there, and
  otherwise falls back to a search page.
- **Amazon Prime was measured and rejected** on that coverage, and unlike
  Netflix it has no second public source.
- **A Netflix title unavailable in the user's region falls back to the
  search page**, as in 1.3.
- **AniList may still be rate-limited into a `403`**, which now affects
  schedules and Crunchyroll link resolution rather than progress.
- **Crunchyroll ids are not probed before being saved**, because
  Crunchyroll answers 200 to a bogus id; the strict title match is the
  whole safeguard.

### Antivirus note

As VDD-1.3: the 1.2 executable was flagged `Trojan:Win32/Wacatac.B!ml`
and this was established by bisection to be a false positive attached to
that specific binary rather than to any code. The durable remedy is a
code-signing certificate. Azure Artifact Signing was priced at roughly
$10/month with no hardware token, against $249–325/year for an EV
certificate whose instant SmartScreen pass was withdrawn in March 2024.
Nothing has been bought, so this can recur.

---

## 11. Verification performed

In addition to VDD-1.0 §11, VDD-1.1 §11, VDD-1.2 §11 and VDD-1.3 §11:

- **176 offscreen checks** across four harnesses (57, 44, 37 and 38) cover this cycle's
  tracker work: each entry kind's dialog state, the Video Website
  dropdown flipping the last-watched spinners between editable and
  read-only while the dialog is open, the tick surviving a save, `+/-`
  moving the episode without touching the season or falling below zero,
  a film reporting no hover columns, both catalogues being asked, a
  picked film carrying its type across, the status line ending where it
  should, filtering by status and by type down to an empty grid and back,
  and all six pages constructing with no drag hint or subtitle on any of
  them.
- The filter menu was driven with **real `QMouseEvent` releases** over
  its actions, asserting the popup is still visible after each tick —
  the behaviour complained about, not merely the handler beneath it.
- The recoloured filter icon was checked by **reading its pixels back**:
  every opaque pixel is the palette colour, not white.
- `filter_icon.png` was confirmed **inside the frozen executable**,
  byte-identical to source at 444,390 bytes, by reading the archive's
  table of contents rather than trusting the build log — a bundled data
  file missing from the package shows only in the packaged app.
- Cinemeta's movie catalogue was measured **live** before any code was
  changed, which is what established that the missing-films report was a
  discovery problem in the form rather than a broken search.
- Two defects were found by the harnesses rather than by review: the
  Stremio note's two owners (§6), and a stale assertion that had been
  passing against explanatory comments rather than against code.
- `APP_VERSION` reads `"1.4"` in the released executable, and its
  Windows version resource reports 1.4.
- No test read or wrote the real user data at `%APPDATA%\Atomic`; every
  harness pointed `storage.DATA_DIR` at a throwaway directory, and the
  ones needing a real site list copied only the small JSON files.

Not verified: the release executable has not been exercised by its owner
against real data before publication, and no Defender scan was performed
on it prior to this release.

---

## 12. Glossary

Unchanged from VDD-1.2 §12.
