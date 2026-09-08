# Atomic — Version Description Document

**Version 2.0** · 8 September 2026

---

## 1. Identification

| | |
|---|---|
| System name | Atomic |
| Version | 2.0 |
| Release date | 2026-09-08 |
| Repository | https://github.com/Khaled-Alneef/Atomic |
| Target platform | Windows 10 / 11, 64-bit |
| Delivered as | `Atomic.zip` — a **GitHub release asset**, holding one self-contained `Atomic.exe` |
| Also at the tag | `Atomic.exe` / `Atomic.zip` — the ~10 MB **bridge installer**, for installs predating 2.0 (§3, §9) |

---

## 2. System overview

Atomic is a single-window Windows desktop application over one person's
anime, reading, films, series, games, applications and websites. 1.10
tracked them; **2.0 plays and reads them**. It carries a native video
player (libmpv) with its own torrent engine (libtorrent) and debrid
support, a chapter reader, a download queue, a locally served web UI
(WebView2) for the catalogue and reading pages, and the same JSON files
in `%APPDATA%\Atomic` that 1.10 wrote.

---

## 3. Inventory of materials released

| Item | Description |
|---|---|
| `Atomic.zip` (release asset) | The application. **125,509,127 bytes**. SHA-256 `859451e02d5f25bbe641cc3b53800f0f96d980c3c6ebeeb9681cb3d2515279fa` (sixth cut - see below) |
| `Atomic.exe` (inside that zip) | **126,286,519 bytes**. SHA-256 `6e59fd09517e873be02ac6044657029572496451c424662bc4cf9513076db598` |
| `Atomic.exe` (committed at `v2.0`) | The **bridge installer**, 11,101,411 bytes. SHA-256 `e14661e2dbcc4cf9370a216ac9e842ed17bdf644284c2265d8cc1d26bb3d4545` (second cut - see below) |
| `Atomic.zip` (committed at `v2.0`) | The same bridge installer, zipped, for a zip-preferring updater |
| `src/` | Full source, **125,204 Python lines** across 126 modules, plus 5,748 lines of served static UI |
| `packaging/` | `build.py`, `Atomic.spec`, `check_release_notes.py`, `fetch_libmpv.py`, and `bridge/` |
| `README.md` | New at this release |
| `docs/VDD-2.0.md` | This document |

**364 commits** stand between `released/1.10` and this release, taken
from `development` as a single snapshot.

**The bridge was cut twice, and the tag was moved.** The first cut -
10,922,879 bytes, SHA-256 `9549508a...` - **could not start**: its spec
excluded `email` among other standard-library modules to save a
megabyte, and `urllib.request` imports `email` at module scope, so the
program died on its first line with

    ModuleNotFoundError: No module named 'email'

It reached the owner's other machine, which had already swapped it in as
its `Atomic.exe`, so that install had to be repaired by hand from the
release zip. The application itself was never involved and no data was
touched. `v2.0` was moved to the second cut; §11 records how the
verification missed it and what now stops it.

**A sixth cut - the application is unchanged.** It carries one comment
and nothing else, so that a measurement is not lost: after updating 1.10
to 2.0 in place, Explorer went on drawing 1.10's icon in folder view
while the taskbar drew 2.0's. **The build was never wrong.** Reproduced
with the real binaries - v1.10's exe out of git, replaced by 2.0's the
way the swap script does it - and photographed from Explorer:
`ExtractAssociatedIcon` on the swapped file returned the new icon, and a
copy of the same bytes under a new name in the same folder drew the new
icon, while the replaced path drew the old one. What did **not** shift
it: `SHChangeNotify(SHCNE_UPDATEITEM | SHCNE_ASSOCCHANGED)` - which is
what `packaging/build.py` prints "Asked Explorer to re-read the icon"
for - F5, a fresh Explorer window, `ie4uinit -show`, `ie4uinit
-ClearIconCache`, and a full Explorer restart. What did: deleting
`iconcache*.db` and `thumbcache*.db` (30 files) with the shell stopped.

The app deliberately does none of those: an application has no business
deleting a user's shell caches or restarting their Explorer, and this
only appears when the icon itself changes between versions - once, at
1.10 -> 2.0. The taskbar was right throughout because that icon is Qt's
`setWindowIcon` from the running app, not the shell's file cache.
build.py's docstring now carries all of it.

**A fifth cut, the same day - a filtered page's scrolling.** His
report split the problem where the third cut had not looked: *"the first
grid load when applied the filter is good but when I scroll down to load
more, it loads super slow"*. Two causes, both measured on his data:

- **The scroll asked the wrong route.** A tick's first screenful comes
  from the genre route (index-backed, `_genre_indexed`), but
  `moreOnScroll` went on asking the *medium* for its next thirty
  unfiltered rows and keeping the few that matched - so the page paid a
  Cinemeta page per card it could show. It now continues the route the
  tick opened, with a cursor and a dry count per route so unticking does
  not strand the medium's own position. Head to head from the same cold
  index, counting only rows the filter would draw: the medium top-up
  gave **62 Romance rows in 2.7s** (147 rows a pull, ~7% matching), the
  genre route **115 in 0.1s**, every row a match.
- **The walk read too little of the catalogue.** `LOCAL_GENRE_PAGES`
  was 8. The pages are fetched together under one budget, so reading
  more costs concurrency rather than time - and `LOCAL_GENRE_WORKERS`
  (8, new) holds the socket count to exactly what it was, which
  `max_workers=len(pages)` had not. Measured, one batch from a cold
  cursor: Sport/anime 8 pages **3.12s, 5 rows** against 24 pages
  **2.06s, 13 rows**; Music/anime 0.84s/2 rows against 1.34s/9 rows;
  Documentary/series 0.23s and 30 rows either way, because Cinemeta
  filters that one server-side and no walk happens.

Photographed on the frozen build for the control: the Anime page's
**Sport** tick drew 1 card at +0.5s and 5 at +20s.

**Known and deliberate**: a filtered page now stops when the genre's own
walk comes up empty twice, where the medium walk would have gone on
dribbling a row at a time - the same two-dry-batches rule the medium
route has always used, against a much better rate. **Not this
release's**: deep in Cinemeta's anime catalogue there are rows that are
not anime (telenovelas, "Kyle XY"). That is the source's own tagging and
predates this change.

**A fourth cut, the same day.** Two more, and both are cases this
machine cannot produce:

- **A horizontal wheel still did not scroll a row.** The third cut told
  a wheel from a finger by *cadence*, and every shape a horizontal
  wheel takes looks like a stream: a tilt held down auto-repeats every
  30-50ms, and a free-spinning or high-resolution wheel sends small
  deltas as fast as a touchpad. What no hand does is send the **same
  delta twice running** - a driver's tick is one constant, a swipe
  follows the hand. Measured on the real handler: a repeating tilt at
  35ms is eased 10 of 10 and at 120ms 6 of 6, a hi-res wheel of 12px
  every 10ms 20 of 20 and of 6px every 8ms 19 of 20, while a touchpad
  of varying deltas is eased 0 of 30 and a varying flick 1 of 20, and
  an 8px blip on its own is left to the browser.
- **The player stuttered and seemed to change speed at random.** On his
  laptop only. Nothing in the app writes mpv's `speed` but the speed
  panel, which relabels its own button, so this is the presentation
  clock. `video-sync=display-resample` is the default on a measurement
  taken on his **240Hz desktop panel** (§6 of VDD-1.10's successor
  notes; uneven frames 14.6% -> 0.2%, 4 September 2026) and it works by
  presenting on the display's own clock - which a laptop's is not:
  Windows moves the panel's refresh rate underneath it for power, and
  each move leaves mpv's estimate wrong until it re-converges, in
  bursts of repeated and dropped frames. `PlayerPage._watch_cadence`
  reads mpv's own numbers every save tick (`estimated-display-fps`,
  `frame-drop-count`, `vo-delayed-frame-count`,
  `video-speed-correction`) and steps out of display-resample into
  `video-sync=audio` once, permanently for that session, saying why.
  Harnessed on the real method: a steady panel is never touched, one
  hiccup is not enough, 60 -> 47.9Hz switches, sustained frame loss
  switches, and it never switches twice. **Not reproduced**: this
  panel is steady, so 75s of Reacher S02E01 on the frozen build wrote
  no cadence line, no fallback and no `stopped mid`, and the resume
  record advanced normally - the fix is aimed at the mechanism, and the
  line it writes is what will name the cause on his machine.

**A third cut, the same day.** Three more of his reports, all in the
source this tag carries:

- **A horizontal wheel could not scroll a row.** The second cut taught
  the strip handler to stand aside for a finger *by size*, and a
  horizontal wheel tilt is not as big as a vertical notch - so his
  tilt ticks were read as finger events and met Chromium's unanimated
  jump. The question is cadence, not size: a touchpad streams, a tick
  arrives alone. Measured on the real handler - an isolated tick of
  20, 30, 40, 50 and 100px is eased **5 of 5** at every size; a
  touchpad stream of 14px x40 and a flick of 60px x30 are eased once
  each (the first, isolated event) and otherwise left to the browser;
  an 8px blip is left alone as too small to be a tick.
- **A filtered page loaded its first cards fast and then crawled.**
  `_genre_video` read the catalogue index only for the *first* answer,
  so every scroll-down went to the Cinemeta walk - which measured on
  his own data hands back **nothing at all** while it runs, so the page
  pulled into an empty answer every 700ms for as long as
  LOCAL_GENRE_TOTAL_S. His index holds far more than the 200 rows the
  first answer carries: Drama **964**, Comedy **637**, Mystery 248,
  Thriller 225, Action 131. Measured through the real routes on a copy
  of his data, Mystery: the four continuations answered **0 new rows**
  each before, and **47 new in 0.02s** on the first pull after. The
  reading side already answered from its caches (0.01-0.03s a pull,
  30 new rows on the first) and is untouched.
- **A free-spinning wheel froze the page and then jumped.** The glide's
  curve started at `performance.now()` taken in the wheel handler,
  while an animation frame carries the timestamp of when the frame
  *began* - so a notch that lands during a frame's work leaves the next
  frame with a time earlier than the start, t clamped to 0, and nothing
  applied. One notch pays that once; a wheel that spins sends a notch
  before nearly every frame, so every frame applied nothing while the
  distance accumulated, and the whole of it arrived when the spin
  stopped. A chained notch now starts its curve from the last frame's
  own timestamp. **Not reproduced here**: at 165Hz the skew is zero and
  the control measured 130 moving frames with 0 dead, exactly as the
  same trap failed to reproduce on 6 September - the fix is proven
  against the mechanism, by no regression locally (the same 5,005px of
  travel over **162** frames instead of 132, median step 33px instead
  of 46, 0 dead in both), and by a new `skew=` number on the glide line
  so his laptop can say it.

**The application was cut twice before this one.**
The first cut - 126,279,591 bytes, SHA-256 `ed1c84ab...` inside a
125,502,375-byte zip - shipped two defects the owner met within the
hour, both fixed in the second:

- **Continue from Home could ask for an episode that does not exist.**
  `_starting_episode` stepped past a season's last episode on
  `_season_episode_count`'s guess when Cinemeta's map was not yet on
  disk. On his Reacher (progress S01E08 verified, `latest_available`
  S04E05, the details page never opened on that machine) the count
  answered **12** and Continue asked for **S01E09**, whose honest
  answer is "No playable source was found for this episode". Opening
  the episode list wrote the map, and the same press then played
  S02E01 - which is exactly the asymmetry he reported. `_change_episode`
  had been taught this rule on 6 September; `_starting_episode` was the
  caller that was missed, and now holds the start ("Checking the
  season's episode list...") rather than guessing.
- **A touchpad could not scroll a sideways row.** The vertical glide
  has stood aside for a finger since 5 September; the strip handler
  never did, so a two-finger swipe streamed 60-120 events a second into
  an ease built for one mouse notch. Measured against the shipped
  app.js, 40 finger events: **40 of 40 taken before, 0 of 40 after**,
  the mouse notch still eased in both, the vertical case identical.

Neither the bridge installer nor the delivery route changed with this
cut. **An install already running the first cut is not offered this
one** - the version number is the same by the owner's decision, so that
machine re-downloads the zip once; every install on 1.10 or older gets
the second cut directly.

### Why the application is not committed at the tag

Every release from 1.0 to 1.10 shipped as a file committed in this
repository, and the in-app updater read it through GitHub's contents
API. Measured 8 September 2026:

| | |
|---|---|
| `Atomic.exe` | 126,279,591 bytes |
| `Atomic.zip` | 125,502,375 bytes |
| GitHub's per-file limit on push | **100 MiB, a hard refusal** |
| GitHub's release-asset limit | 2 GiB |

The application therefore *cannot* be committed, at this size or any
size near it, and the delivery route had to move to release assets. The
composition is not fixable by trimming: `libmpv-2.dll` alone is
**45.26 MB packed** of the 125.8 MB archive (117.99 MB unpacked), and it
is the video player.

---

## 4. Inventory of software contents

2.0 is 364 commits of work and a module-by-module table would be the
whole tree. What is new as a *capability*:

| Area | What arrived since 1.10 |
|---|---|
| `windows/player.py`, `helpers/streams.py`, `helpers/torrent_engine.py`, `helpers/debrid.py`, `helpers/indexers.py` | The video player, the stream engine, the piece store, debrid, the title-based indexers |
| `windows/reader.py`, `windows/web_reader.py`, `helpers/chapter_source.py` | The chapter reader, in Qt and in WebView2 |
| `windows/downloads_page.py`, `helpers/downloads.py` | The download queue, episodes and chapters |
| `web/` | Home, Discover, the catalogue pages, search, schedule — served locally, rendered in WebView2 |
| `windows/details.py` | The title page: cast, seasons, source groups, downloads |
| `helpers/changes.py` | Every write bumps a counter; every page redraws off it |
| `helpers/catalog_index.py`, `helpers/catalog_seed.py`, `helpers/reading_seed.py` | What makes a genre tick answer in 0.05 s and a fresh install start warm |
| `helpers/subtitles.py`, `helpers/ai_translate.py` | Arabic subtitles, and AI translation of an English track |
| `helpers/setup_wizard.py`, `helpers/whats_new.py` | The setup window, and the release summary it now carries |

**Changed for this release specifically:**

| Module | What changed |
|---|---|
| `helpers/updater.py` | `_from_releases` / `_from_repository`; asks both, newest wins, asset wins a tie; sha256 verification beside the git-blob check; `APP_VERSION` → `2.0` |
| `helpers/development_version_patch.py` | `APP_VERSION` → `2.0` (it is the *last* writer at startup — see §6) |
| `helpers/setup_wizard.py` | `SETUP_VERSION`, `will_offer`, `_already_answered`; the welcome step carries the release notes; Skip is not gated for an existing install |
| `helpers/app_settings.py` | `get_setup_shown_for` / `set_setup_shown_for` |
| `helpers/whats_new.py` | `show_if_updated(skip_dialog=)` returns its sections; 2.0's 18 notes |
| `main.py` | Asks `will_offer()` first, so one window opens instead of two |
| `packaging/bridge/` | New: `atomic_setup.py`, `AtomicSetup.spec`, `version_info.txt`, `build_bridge.py` |
| `README.md`, `docs/RELEASING.md`, `CLAUDE.md`, `.claude/skills/release` | The delivery change, written down |

---

## 5. What this version provides

The user-facing summary is `helpers/whats_new.NOTES["2.0"]`, 18 lines,
shown once after the update. In one paragraph: Atomic plays and reads
now. It resolves an episode's sources from every addon, indexer and
configured site at once, races several releases and judges them on
transfer rate rather than first byte, plays through libmpv from a
swarm, a debrid link or a file, resumes on the exact frame, carries
Arabic subtitles and can translate an English track, downloads episodes
over four connections and chapters as `.cbz`, reads chapters at the
source site's own size, and redraws every page the moment anything
changes.

---

## 6. Design notes

### The delivery route, and the bridge

`updater.check_for_update` now asks two places and takes the newest,
the release asset winning a tie:

- `/releases` — a published release's `Atomic.zip` asset. Drafts and
  pre-releases are skipped, so a test build can never be offered.
- `/tags` + `/contents` — the old route, kept because it is the *only*
  thing an install running 1.0–1.10 can read.

A 1.10 install asks for `/contents/Atomic.exe?ref=v2.0` and installs
whatever comes back. What comes back is `packaging/bridge/` built as
`Atomic.exe`: a 10.9 MB tkinter program that downloads the real
`Atomic.zip` from the release, checks it against the asset's SHA-256,
unpacks the single executable inside and replaces itself with it,
through the same swap-script mechanism the app's own updater uses. It
touches no user data. On failure it says why and offers *Try Again* and
the download page, and since it is still `Atomic.exe`, reopening Atomic
retries.

It is deliberately standalone — nothing from `src/`, nothing outside the
standard library — because it has to be small enough to live in the
repository at all. `build_bridge.py` refuses a build over 40 MB or one
missing its markers.

### Two constants carry the version

`updater.APP_VERSION` is the number, and
`development_version_patch.install()` overwrites it at startup —
`_ui_startup` installs that patch **last**, on purpose, because several
older patches also set it. A release that bumped only `updater.py`
would ship a build calling itself 1.10.285, offering itself an update
for ever and showing the wrong notes. Both were bumped and the frozen
archive was read back to prove it (§11).

### The setup window is gated on a version now

`setup_completed_at` answers "has the wizard ever been dealt with", and
every install predating 2.0 carries it — stamped silently the first time
an existing profile met the wizard. That could not answer 2.0's
question, which is "has this profile seen *this* release's setup". The
new stamp is `setup_shown_for` (`SETUP_VERSION`, currently `"2.0"`), and
raising that number is what shows the window again.

For an install that is not fresh the window speaks differently — it
names the version, carries the release notes, and does not gate *Skip*
on a TMDB key, which for an existing install would leave only the
window's X and read as a trap. The What's New dialog stands down when
the wizard is going to open, so one window appears instead of two
stacked over a just-relaunched app.

---

## 7. Configuration and user data

Unchanged in location and format: `%APPDATA%\Atomic`, plain JSON plus a
cover cache. 2.0 adds `setup_shown_for` to `settings.json` and writes
`downloads.json`, `player_state.json`, `catalog_index.json` and
`reading_meta.json` as features require them. **No migration is needed
and none is performed**: a 1.10 profile opens in 2.0 as it stands.

---

## 8. External interfaces

Cinemeta, AniList, TVMaze, MangaDex, Wikidata, TMDB, AnimeTosho,
SubsPlease, Torrentio/TorrentsDB, OpenSubtitles, SubDL, SubSource,
Real-Debrid, GitHub (updates), and the reading/anime sites the user
configures. Every key is optional; every lookup fails soft. Full table:
`README.md` and `.claude/rules/integrations.md`.

---

## 9. Installation and removal

**New install.** Download `Atomic.zip` from the release, unzip anywhere
writable, run `Atomic.exe`. No installer, no dependencies, no
administrator rights.

**Updating from 1.10 or older.** Settings → Check for Updates, one
press. The old updater downloads the bridge installer from the tag,
verifies it against GitHub's git-blob hash, swaps it in and starts it;
it fetches the real build and replaces itself. Entries, settings,
history and covers are untouched throughout.

**Updating from 2.0 on.** Settings → Check for Updates. The release
asset is downloaded and verified against its SHA-256 directly.

**Removal.** Delete the executable; Settings → Data → Uninstall also
removes `%APPDATA%\Atomic`.

---

## 10. Known limitations

1. **Unsigned binary** — Defender's ML classifier may refuse a browser
   download of a bare `.exe` (`Trojan:Win32/Wacatac.B!ml`, no signature
   match). Shipping inside a zip is the working answer; **code signing
   is the durable one and is not done** (roadmap #8).
2. **The bridge is a one-time stub in place of the app.** Between the
   old updater's swap and the bridge finishing, `Atomic.exe` is the
   installer. A machine that loses its connection at that moment reopens
   to the installer, which retries — recoverable, but it is a real state
   the app can be found in.
3. **Windows only** (§1). WebView2, the native child windows, the
   swap-in-place updater and the launcher integrations are all
   Windows-specific.
4. **A watch-progress source other than Stremio does not exist here**,
   and no public API supplies one — see `.claude/rules/planning.md`.
5. **Wikidata's streaming-id coverage has a cliff**: current seasonal
   anime carries no Crunchyroll/Netflix/Prime id at all.
6. **The service's "still downloading" branch is not exercised live** —
   every release tried was already held.
7. **A real swarm stall has not been reproduced on a frozen build** —
   a swarm cannot be starved on demand; the harness is the proof of that
   path.

---

## 11. Verification performed

Method: `.claude/rules/testing.md` — reproduce, fix, preview from
source, build, **read the archive back**, drive the frozen exe from
outside, photograph it.

**Harness, 23 checks, all passing** (`h_setup.py`, offscreen, against
its own temp data directory):

- a fresh profile is offered the window; a 1.10-shaped profile
  (`setup_completed_at` set, no `setup_shown_for`) is offered it; a
  profile stamped `2.0` is not; a profile stamped `1.9` is;
- the notes are handed over with the dialog skipped, and the marker is
  cleared so a second launch is silent;
- the window names the version, carries the notes, offers Skip to an
  existing install and withholds it from a fresh one;
- closing it stamps `2.0` and it never offers again;
- the updater offers `v2.0` to a 1.10 build **from the release asset**,
  with its sha256, and never from the pre-release or the draft; a 2.0
  build is told it is current; with the releases API failing, the
  repository route still answers `Atomic.exe`.

**Build.** `packaging/build.py --zip` — bundle verified, 1,621 entries,
32 bundled files byte-identical to `src/`.

**Frozen archive read back** — `helpers.updater` holds `_from_releases`,
`_from_repository`, `sha256`, `from_release` and `2.0`;
`helpers.setup_wizard` holds `will_offer`, `SETUP_VERSION`,
`_already_answered`, `set_setup_shown_for`; `helpers.app_settings` holds
both accessors and the `setup_shown_for` key; `helpers.whats_new` holds
`skip_dialog`; `main` holds `will_offer` and `skip_dialog`;
`helpers.development_version_patch` holds `2.0` and **no longer holds
`1.10.285`**.

**Defender** — `MpCmdRun -Scan -ScanType 3` on the release exe, service
confirmed Running: *"found no threats"*, exit 0. Run again on the second
cut, same verdict.

**The second cut's own proof.** Fifteen checks on the real unbound
methods (`h_step.py`): cold, Continue waits for the map, says so and
looks nothing up; the map arriving plays S02E01; Cinemeta answering
with nothing falls back to the guess rather than hanging; warm, S02E01
at once; `latest_available` naming the season proves it without the
map; caught up on the last episode out stays there; an explicit episode
from the list is untouched; an unverified number still starts at the
season's first. On the frozen build against a copy of his data with the
map deleted, Continue **played S02E01 with Arabic subtitles 14 s after
the press** (`streams Reacher S2E1: 76 rows`). The sideways scroll was
A/B'd against the shipped app.js in a real browser over the app's own
server. **Not measured**: a real precision touchpad (this machine has
none - the fix is proven on synthetic wheel events through the real
handler), and the *shipped* build could not be forced into the cold-map
state on the frozen exe here, because Home rewrites the map within ten
seconds of launch on this machine.

**Frozen build driven from outside**, against a copy of the owner's real
data (`copy_real_data.py`, 6,305 files) edited to look like a 1.10
install that has just updated:

- the setup window opens over a fully drawn Home, reading **"Welcome to
  Atomic 2.0"** with *"What's new in 2.0"* and the notes under it, and
  *Skip for now* offered (`setup20.png`);
- closing it writes `setup_shown_for: 2.0`;
- a second launch draws Home with **no wizard and no summary**
  (`second_launch.png`);
- Settings → General reads **"Atomic 2.0"** beside *Check for Updates*
  (`settings20.png`).

**The live update path**, against the published release — §12.

### What this verification missed, and the gate that replaces it

The bridge was checked by **importing `atomic_setup` in the development
tree** and driving its functions - `newest_release`, `download`,
`exe_from_zip`, `install` - end to end against the live release. Every
one passed, and none of it ran the built program, where `email` was not
there to import. `.claude/rules/testing.md` has said since 3 September
that a source run is a preview and never the proof; this pass took the
preview as the proof for the one binary that had to work on somebody
else's machine.

`build_bridge.py` now **runs the exe it just built** (`--selftest`): it
resolves the real release, reads four bytes of the asset and constructs
the window, writing its report to a file because a windowed build has
no stdout. A build that does not answer `SELFTEST OK` is refused. The
second cut was then driven as a *program* - copied into a temp
directory as `Atomic.exe`, `APPDATA` pointed at a temp profile, started
the way the old updater starts it - and **installed the release in 10
seconds**: 126,279,591 bytes, SHA-256 `ed1c84ab...`, and the Atomic it
launched wrote `last_seen_version: 2.0`.

---

## 12. The 1.10 → 2.0 update, proved live

Run after publishing, with **v1.10's own `updater.py` taken out of git**
(`git show v1.10:src/helpers/updater.py`) rather than a re-creation of
it, so what answers is the code those installs are actually running.

Recorded in the release session's log: the tag list resolves `v2.0` as
the newest release tag, `/contents/Atomic.exe?ref=v2.0` answers with the
bridge installer's `download_url`, size and git-blob `sha`, and the
bridge's own `newest_release()` resolves the published `Atomic.zip`
asset with its sha256. See §11's harness for the offline half.

---

## 13. Glossary

Unchanged from VDD-1.2 §12, with three additions:

| Term | Meaning |
|---|---|
| **Release asset** | A file attached to a GitHub Release rather than committed in the repository. 2 GiB limit against 100 MiB. |
| **Bridge installer** | The ~10 MB program committed as `Atomic.exe` at the `v2.0` tag, whose only job is to fetch and install the real build for an install too old to read an asset. |
| **`setup_shown_for`** | The release whose setup window a profile has already seen. |
