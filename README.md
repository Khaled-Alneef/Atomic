<div align="center">

<img src="src/assets/atomic_icon.png" alt="Atomic" width="120">

# Atomic

**One desktop app for everything you watch, read, play and open.**

Anime, manga, manhwa, manhua, films, series, games, applications and
websites — tracked in one place, with a real video player, a real
chapter reader, and the schedules and artwork filled in for you.

![Version](https://img.shields.io/badge/version-2.0-2fb9a6?style=flat-square)
![Platform](https://img.shields.io/badge/platform-Windows%2010%20%7C%2011-0a0e16?style=flat-square)
![Python](https://img.shields.io/badge/Python-3.13-3776ab?style=flat-square)
![PyQt6](https://img.shields.io/badge/UI-PyQt6-41cd52?style=flat-square)
![mpv](https://img.shields.io/badge/player-libmpv-6a3b8f?style=flat-square)
![No installer](https://img.shields.io/badge/install-unzip%20and%20run-2fb9a6?style=flat-square)

[Download](#download) · [Feature tour](#feature-tour) ·
[Where the data comes from](#where-the-data-comes-from) ·
[Privacy](#privacy) · [Updating](#updating) ·
[Configuration](#configuration) ·
[Build from source](#build-from-source) ·
[Architecture](#architecture) · [FAQ](#faq)

</div>

---

## Why Atomic

A library that lives in six different apps is six different apps'
problem. Atomic is one window over all of it, and it is built around a
few decisions that the rest of the app follows from.

**One second.** Anything you started and are now watching — a page
opening, a search answering, sources listing, the reader or the player
opening — is finished, or is showing what it has so far, inside a
second. Where an answer genuinely cannot arrive in that time, the part
that has arrived is drawn and the rest fills in behind it. Nothing here
is a promise; every number in this repository was measured, and the
measurements are written into the code beside what they explain.

**A native player.** libmpv decodes the file, so codecs, subtitle
formats and audio tracks are mpv's problem rather than a browser's. It
plays from a local file, from an HTTPS link, or from a swarm that the
app itself is downloading, with a piece store built for playback rather
than for completion.

**A real reader.** Chapters are drawn at exactly the size the source
site draws them, sharpened once at the pixel size they land on, and
paged or strip-scrolled according to what the medium actually is.

**Your files, on your machine.** Every entry, setting, mark and cover
lives in `%APPDATA%\Atomic` as plain JSON. There is no account, no
server, and nothing to sign into for the app itself to work.

**No installer, no dependencies.** One `Atomic.exe` in a zip. It brings
its own Python, its own Qt, its own mpv and its own torrent engine, and
it updates itself from Settings.

---

## Download

| | |
|---|---|
| **Windows 10 / 11 (64-bit)** | [**Atomic.zip** — latest release](https://github.com/Khaled-Alneef/Atomic/releases/latest) |
| **Install** | Unzip it anywhere you can write to, and run `Atomic.exe` |
| **Uninstall** | Delete the exe. Settings → Data → Uninstall also removes `%APPDATA%\Atomic` |

> **Why a zip and not a bare .exe?** Downloading an unsigned executable
> from a browser gets it refused by Microsoft Defender's machine-learning
> classifier (`Trojan:Win32/Wacatac.B!ml`) with no signature match —
> while the *identical bytes* inside a zip download cleanly. Seven builds
> were compared to establish that: same bundled entries, same modules,
> byte-identical bootloader, every one of them scanning clean locally
> with cloud protection on. It is the container that gets refused, not
> anything in the file. The durable fix is code signing, and it is on the
> roadmap.

The first launch opens a short setup window: optional API keys, a
preferred resolution, a downloads folder. Everything in it can be
changed later in Settings, and Atomic works without any of it.

---

## Feature tour

### The rooms

| Room | What you get |
|---|---|
| **Home** | A banner for what is next, then Watching, Reading, Games, Quick Apps and Websites — each row in the order you last touched it |
| **Discover** | Cinemeta's catalogues for films, series and anime, browsable by genre, with a search that answers across every source at once |
| **Movies · Series · Anime** | Your tracked titles, with progress, next-episode countdowns, and a details page carrying the cast, the seasons and every source |
| **Manga · Manhwa · Manhua** | The same for reading, sourced from the scanlation sites you configure, classified by medium and genre |
| **Games** | Imported from Steam, Epic, EA, Ubisoft, GOG, Battle.net and Riot, launched through their own launchers so they see you playing |
| **Apps · Websites** | Anything else you open often, with real icons and favicons |
| **Downloads** | A queue with pause, resume, cancel, per-row and all-rows, running oldest first |
| **Saved · Schedule · History** | Header tabs on the watch and read pages: what you kept, what airs when, what you actually watched and read |
| **Settings** | Nine pages — general, preferences, anime sources, reading sources, games, API keys, data, key bindings, uninstall |

### The stream engine

Pressing play on an episode does this, and does all of it at once:

| Stage | What happens |
|---|---|
| **Ask** | Every addon, both anime indexers and the configured sites are asked in parallel under one deadline — not one after another |
| **Identify** | A release is matched to *your* season split, taken from the catalogue's own numbering rather than from a database that files a franchise as one long season |
| **Rank** | Resolution, language, seeders and whether your debrid service already holds the file; an Arabic release and a cached one lead their group |
| **Race** | Several releases start at once, and a lane is judged on its **transfer rate against the file's own bitrate**, not on which one produced a first byte |
| **Serve** | The piece store fetches the head first, follows the demuxer's reads, and hands mpv a stream that reopens itself rather than reporting a false end of file |

A hand-picked source is waited for rather than budgeted — if you chose
it, it plays.

### The player

Native mpv in a native window: audio and subtitle tracks, per-file
remembered picks, playback speed, a volume flyout that goes past 100%,
frame-accurate seeking, picture-in-a-window statistics, and a bar
composed per pixel over the video so the picture shows through it.

Subtitles come from OpenSubtitles, SubDL, SubSource and — for anime —
from the release's own attachments on AnimeTosho, which is where Arabic
tracks for multi-language groups actually live. With an AI key
configured, an English track can be translated to Arabic on the fly.

Resume is exact: a Matroska seat is resolved through the file's cues,
and an MP4's through its sample tables, so continuing a film lands on
the keyframe you left rather than at the beginning.

### The reader

Chapters are listed by following the site's own paginator, not by
reading the first page and stopping — a 249-chapter series lists 249
chapters. Pages are drawn at the width the source site draws them,
resampled once at their real device-pixel size, with a spread filling
the column, keyboard and mouse-button navigation, a remembered zoom, and
optional background music.

A chapter that has no pages says why — locked behind the site's coins,
empty, or unreachable — and is not marked read.

### Downloads

Episodes and chapters both. An episode is asked of your debrid service
first and pulled over four ranged connections into a preallocated file
(**measured 14.7 MB/s overall with 25 MB/s peaks** on a 499 MB episode);
without a service, the swarm is read in order, and a swarm that stalls
under 60 KB/s for 90 seconds asks the service again and then moves to
another release rather than sitting there. Chapters are downloaded as a
`.cbz`, at a page width you pick.

---

## Where the data comes from

Everything below is public and keyless unless the table says otherwise.

| Source | What it adds |
|---|---|
| **Cinemeta** | Film, series and anime catalogues, metadata, episode lists |
| **AniList** | Anime search, airing schedule, streaming links, season mapping |
| **TVMaze** | Series by IMDb id, next episode |
| **MangaDex** | Manga matching, release history, and a deliberately conservative *estimate* of the next chapter |
| **Wikidata** | Netflix (P1874) and Crunchyroll (P11330) page ids |
| **TMDB** | Title logos, wide backdrops, cast and filmographies — a token is bundled, your own key optional |
| **AnimeTosho · SubsPlease** | Anime releases by title, and the subtitle attachments that carry Arabic |
| **Stremio addons** | Torrentio and TorrentsDB — public HTTP endpoints; **Stremio itself is not required and is not used as a stream source** |
| **Stremio account** | Watch progress only, if you connect one |
| **OpenSubtitles · SubDL · SubSource** | Subtitles |
| **Real-Debrid** | Cached releases played and downloaded over HTTPS, if you have an account |
| **Your own sites** | Reading and anime sites you add in Settings, searched through their real engine shapes |
| **Steam · Epic · EA · Ubisoft · GOG · Battle.net · Riot** | Installed games, their artwork, and launching them properly |

**An API key is fine; requiring another application is not.** Atomic has
to work on a clean machine with nothing beside it — which is why the
torrent engine is libtorrent *inside* the app rather than a streaming
server you have to install first. Every key is optional, lives in
Settings, and the feature it unlocks stays dark and says why until you
paste one.

---

## Privacy

- **No telemetry.** Nothing is reported anywhere, ever.
- **No account and no server.** Your library is JSON files in
  `%APPDATA%\Atomic`.
- **Keys stay local**, in `settings.json` on your machine, and are never
  sent anywhere except to the service that issued them.
- **Requests go only where a lookup needs them** — the sources in the
  table above, and the sites you configured yourself.
- **Atomic hosts and indexes nothing.** It searches public sources you
  choose and plays what you point it at.

---

## Updating

**Settings → General → Check for Updates.** Atomic reads this
repository's releases, offers anything newer than the running build,
verifies the download against GitHub's own checksum, replaces itself and
reopens on the new version. Your entries are untouched.

**Updating from 1.10 or older works, in one press.** Every release up to
1.10 shipped as a file committed in this repository, and that route is
now closed by physics rather than by choice: the 2.0 build is 126 MB and
GitHub refuses any file over 100 MiB. Releases from 2.0 on ship as
**release assets**, where the limit is 2 GB.

So the file committed as `Atomic.exe` at the `v2.0` tag is a small
**bridge installer** (`packaging/bridge/`, 10.4 MB): an old install
downloads it exactly as it always did, it opens with a progress bar,
fetches the real `Atomic.zip` from the release, checks it against the
asset's SHA-256, unpacks it and replaces itself with it. If anything
fails it says so and offers both *Try Again* and the download page — and
it is still `Atomic.exe`, so simply opening Atomic again tries once
more. It never touches your data.

---

## Configuration

Settings, in nine pages. The ones worth knowing about:

| Setting | Default | What it does |
|---|---|---|
| Sidebar order and visible sections | All shown | Drag to reorder; hidden sections can be hidden from Home too |
| Preferred resolution | Best available | What the source list leads with, and what a download picks |
| Auto-pick a source | On | Play without choosing a release by hand |
| Full screen on startup | Off | Only for a launch Windows itself started at sign-in |
| Blur episode stills | Off | For spoilers in thumbnails |
| Hide entry names | Off | Cover-only cards |
| Downloads folder | `%USERPROFILE%\Downloads\Atomic` | Where episodes and `.cbz` files land |
| Reading music | Off | A URL played, minimised, while you read |
| Reading and anime sites | A seeded set | Add, check and remove the sites searched for chapters and episodes |
| Real-Debrid | Empty | Cached releases stream and download over HTTPS |
| Stremio account | Empty | Watch progress only |

**API keys** — every one optional, each with a *Get a key* link beside
its field:

| Key | Unlocks |
|---|---|
| TMDB | Title logos and backdrops (a token is already bundled) |
| SubDL · SubSource | Subtitle sources |
| OpenAI · DeepSeek · Gemini · Anthropic | AI subtitle translation |

---

## Build from source

Windows only, and **Python 3.13 specifically** — libtorrent publishes
wheels for CPython 3.9–3.13 and the build re-execs into 3.13 for exactly
that reason. A build under a newer interpreter reports no torrent engine
and every stream looks broken.

```bash
git clone https://github.com/Khaled-Alneef/Atomic.git
cd Atomic
py -3.13 -m pip install -r packaging/requirements.txt
py -3.13 packaging/fetch_libmpv.py     # vendor/libmpv-2.dll, ~120MB, not in the repo
py -3.13 src/main.py                   # run it
```

Build the distributable:

```bash
py -3.13 packaging/build.py --zip      # Atomic.exe and Atomic.zip in the repo root
```

The build verifies that the produced exe holds every file the spec
declares, and fails loudly on a stale or incomplete one — PyInstaller
caches aggressively, and a "succeeded" log has shipped a binary missing
its assets before.

Build the bridge installer (only needed at a release):

```bash
py -3.13 packaging/bridge/build_bridge.py
```

---

## Architecture

```
                    ┌──────────────────────────────────┐
   Qt window ──────▶│  main.py — sidebar, navigation   │
   (PyQt6)          │  page stack, full screen         │
                    └───────────┬──────────────────────┘
                                │
             ┌──────────────────┼───────────────────┐
             ▼                  ▼                   ▼
      windows/*.py        web/ (WebView2)     three overlays
      Qt pages —          Home, Discover,     player (libmpv)
      games, apps,        catalogues,         reader  (WebView2)
      websites,           search, schedule    details (Qt)
      tracker             served locally
             │                  │                   │
             └──────────────────┼───────────────────┘
                                ▼
                       helpers/ — 109 modules
        net (one pooled connection)  ·  storage (atomic JSON)
        changes (every write bumps; every page redraws)
        lookup_pool (4 workers)      ·  torrent_engine (libtorrent)
                                ▼
        AniList · Cinemeta · MangaDex · TVMaze · TMDB · Wikidata
        AnimeTosho · SubsPlease · Torrentio · Real-Debrid · your sites
```

| Path | Holds |
|---|---|
| `src/main.py` | Window, sidebar, navigation, page transitions, full screen |
| `src/windows/` | The pages and the three full-window overlays |
| `src/web/` | The locally served pages (backend, server, static) |
| `src/helpers/` | Theme, widgets, storage, every external source, the updater |
| `packaging/` | `build.py`, `Atomic.spec`, and `bridge/` — the installer old versions download |
| `docs/` | `RELEASING.md`, and one design document per released version |

Two conventions the whole app rests on:

- **Every HTTP request goes through `helpers/net.py`.** One pooled,
  keep-alive connection with a real wall-clock deadline. Six GETs to one
  host took **40.3 s** opening a connection each time and **0.75 s**
  through the pool.
- **Every write the user makes calls `changes.bump()` at the write
  itself**, and every page reads that counter on a 150 ms tick — so
  un-saving a title on its details page removes its card from Home
  behind it, with no page switch.

---

## FAQ

**Do I need Stremio installed?**
No. Atomic speaks the addon protocol to public HTTP endpoints and runs
its own torrent engine. Nothing has to be installed beside it.

**Do I need a Real-Debrid account?**
No — it makes cached releases play and download over HTTPS instead of
over a swarm, and the app is fully usable without one.

**Why does Defender flag the download?**
Because the binary is unsigned and brand new, not because of anything in
it. See [Download](#download). Ship-as-a-zip is the working answer;
code signing is the durable one.

**Where is my data?**
`%APPDATA%\Atomic` — plain JSON files plus a cover cache. Copy the folder
to move your library to another machine.

**Does it run on macOS or Linux?**
No. It is a Windows app: WebView2, native child windows, the swap-in-
place updater and the launcher integrations are all Windows-specific.

**Can I use my own reading sites?**
Yes — Settings → Reading Sources. Atomic knows the common engine shapes
and falls back to a generic search, and it tells you which of the two a
site resolved as.

**Why is anime a separate page from series?**
Because the sources treat it as one: an anime catalogue is a genre
filter server-side, and a franchise's season split rarely matches the
one a film database records. Keeping them apart is what lets the season
mapping be correct.

---

## Disclaimer

Atomic is an independent personal project. It is not affiliated with,
endorsed by, or connected to Stremio, AniList, MangaDex, MyAnimeList,
TMDB, Crunchyroll, Netflix, Real-Debrid, Valve, Epic Games or any other
service named here.

**Atomic hosts nothing, indexes nothing and provides no content.** It
searches public sources and the sites you configure yourself, and plays
or reads what you point it at. What you do with it is your
responsibility, and local law is yours to know.

---

## Acknowledgements

[mpv](https://mpv.io) and libmpv · [libtorrent](https://libtorrent.org) ·
[Qt](https://www.qt.io) and [PyQt6](https://riverbankcomputing.com/software/pyqt/) ·
[PyInstaller](https://pyinstaller.org) ·
[Pillow](https://python-pillow.org) ·
[AniList](https://anilist.co) · [MangaDex](https://mangadex.org) ·
[TVMaze](https://www.tvmaze.com) · [TMDB](https://www.themoviedb.org) ·
[Wikidata](https://www.wikidata.org) ·
[Cinemeta and the Stremio addon protocol](https://github.com/Stremio/stremio-addon-sdk) ·
[AnimeTosho](https://animetosho.org) · [SubsPlease](https://subsplease.org) ·
[OpenSubtitles](https://www.opensubtitles.org)

This product uses the TMDB API but is not endorsed or certified by TMDB.

---

<div align="center">

© 2026 Khaled Alneef · Built for one library, and it shows

</div>
