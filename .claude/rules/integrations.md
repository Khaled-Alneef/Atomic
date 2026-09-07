# Integrations rules

Read this before non-trivial integrations work (`integrations-engineer`).

## The sources

| Module | Service | Answers |
|---|---|---|
| `anilist.py` | graphql.anilist.co | anime search, public list progress, airing schedule, Crunchyroll `externalLinks` |
| `tvmaze.py` | api.tvmaze.com | series by IMDb id, next episode |
| `mangadex.py` | api.mangadex.org | manga matching, release history, *estimated* next chapter |
| `stremio.py` | Cinemeta + api.strem.io | anime/series search, metadata, latest aired episode, account watch progress |
| `release_schedule.py` | - | picks the source per medium, formats hover lines |
| `manga_sites.py` / `anime_sites.py` | user-configured | reading/video-site search across known engine shapes, generic fallback |
| `indexers.py` | feed.animetosho.org, subsplease.org | torrent releases **by title**, for the player |
| `updater.py` | api.github.com | release tags and the update download |

## What a source is allowed to need

**Revised by the owner, 19 August 2026.** The old rule here was "no API
keys anywhere". That is no longer the constraint, and it was blocking
the wrong things. The constraint is now:

> **An API key is fine. Requiring another application to be installed is
> not.**

- **Keys are allowed.** If a source needs one, it goes in Settings for
  the owner to paste, is stored in `settings.json` beside the Stremio
  `authKey`, and the feature stays dark and says why until it is given
  one. Never hardcode a key, never ship one, and never create an account
  to obtain one - that is the owner's to do.
- **A dependency on another installed app is not allowed.** Atomic has
  to work on a clean machine with nothing beside it. This is why the
  torrent engine is libtorrent *inside* the app (`torrent_engine.py`)
  rather than Stremio's streaming server. The same bar applies to
  anything else - no "install X first" features.

**Stremio is no longer a stream source at all** (19 August 2026, the
owner's ask). Both halves are gone from `streams.py` and must not come
back: `ensure_local_server()`/the 127.0.0.1:11470 route, and
`import_account_addons()`, which read the user's own Stremio addon
collection over their account key. The server was not free even as a
fallback - every torrent paid up to a 12s `ensure_local_server()` wait
plus a 6s peer poll before the built-in engine was asked for anything.
A build with no libtorrent now returns `reason="no-engine"` and the
player says so. Torrentio/TorrentsDB stay: they speak the Stremio addon
protocol but are public HTTP endpoints needing nothing installed.
The Stremio *account* in Settings is still read - by `stremio.py`, for
watch progress, and nothing else.

**libtorrent is why `packaging/build.py` re-execs on Python 3.13.** There
is no libtorrent wheel for the machine's default 3.15, so anything run
under 3.15 reports `torrent_engine.available() == False` and every
torrent looks broken. Test the player with the 3.13 interpreter.

The reason the old rule existed still holds and is worth keeping in
mind: a source that quietly stops answering is worse than no source, so
whatever a key unlocks must still fail soft and say so.

## Rules

- **Fail soft, always.** A flaky connection means a missing suggestion
  or schedule - never an error dialog, never a crash. Every lookup is
  wrapped, returns `None` rather than raising.
- **A background thread must never raise** - an uncaught exception kills
  it silently and the UI waits forever. Wrap the whole body in
  `try/except Exception` and emit *something* even on failure - page
  counters depend on every lookup reporting back.
- **Cross the thread boundary with a signal** - never touch a widget
  from a worker thread. Follow the existing pattern: a small `QObject`
  with a typed signal, connected in the page's `__init__`.
- **Carry the run id.** Lookups fired by a refresh carry that refresh's
  run number, so a lookup fired on page load can't be counted toward a
  later refresh's results. Shipped bug: a run ended early on a verdict
  belonging to a different lookup.
- **Cache on the entry, not in memory.** Results store on the entry with
  a checked-at timestamp; `release_schedule.needs_refresh` decides
  whether to look again (12h TTL, or immediately once the stored
  release time has passed). A normal page visit fires no requests at
  all.
- **Read bodies through `net.read_text`/`net.read_bytes`, never
  `resp.read()`.** `urlopen(timeout=)` bounds each socket operation, not
  the transfer: a host sending one byte a second resets that timer
  forever and the read never returns. `helpers/net.py` is the single
  implementation (size cap + wall-clock deadline) - do not copy it into
  a new module, which is exactly how five files were missed the first
  time. Compute the deadline *after* any throttle sleep.
- **Bound a chain, not just its steps.** Three engines at 6s each is not
  a 6s bound. `anime_sites.search_site`/`resolve_page_url` take a
  `deadline` and give each request `min(timeout, remaining)`
  (`_step_timeout`), giving up below 1s rather than opening a connection
  that cannot finish. Budget is 2x the per-request timeout: less would
  mean one dead engine ahead of the right one turns a real hit into a
  saved "no page found".
- **Work fired by typing goes to `lookup_pool.submit_latest`, not
  `submit`.** Every debounced keystroke but the last is stale before it
  answers; `submit_latest` replaces a queued job under the same key and
  runs one at a time. Not the shared queue - that is drained by
  page-load backfill, and a lookup the user is watching would wait
  behind all of it.
- **Cap concurrency - never one thread per entry.** `lookup_pool.py` is
  a shared 4-worker queue used by every per-entry background lookup in
  `tracker.py`. Shipped bug: unbounded per-entry threads on page load
  (up to 651 simultaneous connections measured) saturated the user's
  whole home network. Route new per-entry background work through it,
  don't spawn a bare `threading.Thread` in a loop.
- **A resolved page URL can go stale if fetched before it's ready.**
  Shipped bug: a fast Save read the url field before a ~1.5s background
  lookup finished, permanently storing an empty url with nothing to
  retry it. Anything that saves a background-resolved value must wait
  for it or re-resolve lazily on next use, not just read-and-forget.

## Being conservative on purpose

MangaDex has no "next chapter" field - nobody announces scanlation
dates - so that number is *extrapolated* from release history, labelled
"Expected" and flagged estimated. Deliberately conservative: title must
genuinely match (`title_match`, threshold 0.85 - MangaDex returns *The
Beginning After the End* for *The World After The End*, and inheriting
another series' schedule is the worst failure mode), the feed must
still be current, the interval plausible, and enough chapters to
measure exist. Anything short returns nothing - the honest answer.
Don't loosen these to raise the hit rate.

Respect throttles: MangaDex is spaced ~0.35s between requests and
retried once, since the tracker fires one lookup per entry at once.

**A series page is not the whole chapter list any more.** olympustaff
printed all 248 chapters of The Beginning After the End when
`chapter_source` was written; measured again 19 August 2026 it prints
**40**, plus chapter 1, and hangs the rest off `?page=2..7`. The reader
was therefore showing only the newest forty - which from inside a
chapter reads as a list holding nothing but what was just read.
`_page_urls`/`_more_pages` follow the paginator (six pages at a time,
each retried once, `MAX_LIST_PAGES` 24), and the page numbers are
*filled in* rather than taken as printed, because "1 2 3 ... 12" names
neither 4 nor 11. Retrying matters: one of six concurrent pages failed
on the first run and the list came back missing exactly the forty
chapters it held - a hole in the middle of the numbering, which is worse
than a short list because nothing says it is there. Full list measured
at 21.7s cold, 249 chapters, no gaps; `reader.CHAPTER_LIST_TIMEOUT` was
raised to 45s to fit it and the 6h cache means it is paid once.

**Watch progress comes from Stremio and nowhere else.** This is settled;
do not add a second source without a way to tell which one is right.
Every alternative was tried and removed in turn - an authenticated
Crunchyroll client (twice), a pasted Crunchyroll token, AniList by
username, and MAL-Sync feeding AniList. The failure they shared: a
source that is silently wrong is worse than no source, and with two
sources there is no way to know which is which. A card once showed
episode 7 for a show its owner had watched two episodes of.

`anilist.py` keeps only the airing schedule and streaming-link
resolution. It has no progress functions; that is deliberate.

**Reading Crunchyroll directly was built, shipped, and removed. Do not
rebuild it.** Three measured dead ends, in the order they were hit:

1. **Email/password cannot work.** Minting a token needs a client
   credential Crunchyroll issues to no third party, and the published
   one answers `auth.obtain_access_token.client_inactive` on both
   `www.crunchyroll.com` and `beta-api.crunchyroll.com`.
2. **A pasted browser token works but expires inside an hour**, so an
   account that looked connected quietly stopped answering. (For the
   record, since it is not guessable: the token is in the `token`
   request's *response* - that request's own headers carry `Basic`, not
   `Bearer`, and `Bearer` appears only on `content/v2` requests.)
3. It is against Crunchyroll's ToS, and it broke twice in one day.

Revisit only if Crunchyroll publishes an actual API.

Some services can't be *scraped* at all - Crunchyroll (JS-rendered
search, content API 401s without OAuth) and Netflix (search behind a
sign-in).
Both resolve via AniList's `externalLinks` instead, which needs no auth
on either service: `anime_sites._STREAMING_SITES` is the table, and
adding another such service is a row there, not new code. An
authenticated Crunchyroll client was tried twice and removed both times
(see above); AniList, kept current by MAL-Sync, is what answers instead.

**AniList files each season as its own work; this app's cards do not.**
`fetch_season_progress` finds a franchise's seasons, prefers the season
number written in the title over release order, and reports the furthest
one actually started - so "One-Punch Man" reads S02E05 rather than
sitting at E12 forever. Two traps already paid for, both measured live:
a 1-episode short (*Go! Saitama*) carried the franchise name **only as a
synonym** and took season 3's slot, which is why matching uses the
entry's own titles and never its synonyms; and a cour split
("Season 3 Part 2") is still season 3, which is why `_SEASON_NUMBER_RE`
deliberately does not match "Part".

**AniList rate-limits hard, and used to fail soft into silence.**
Sustained querying gets the whole network a `403` on every POST (not a
429). Measured lasting over an hour. `_post` now raises
`anilist.RateLimited` for 403/429 and `fetch_watch_progress` lets it
propagate - the schedule lookups still fail soft on purpose - so the
tracker can say so instead of reporting "not on your list".
`tracker.REASON_ANILIST_RATE_LIMITED` on the `resolved` signal is the
mechanism; extend that `reason` field rather than inventing another.
When a previously working AniList lookup starts returning nothing, check
for the 403 before believing the data changed.

**Crunchyroll ids come from Wikidata too, property P11330**
("Crunchyroll series ID" - not P4110, which Wikidata deprecates). Asked
before AniList, same shape as Netflix; coverage measured at 4 of 6 real
titles, the rest fall through to AniList. **Not probed before saving,
unlike Netflix: Crunchyroll answers 200 to a bogus id** (measured), so a
probe proves only that the site is up - the strict Wikidata title match
is the whole safeguard, and a free-text P11330 value is rejected by
shape first.

**Ask Wikidata with the full title only - never a shortened variant.**
`_query_variants`' subtitle-stripped head exists for *site* searches
that index romaji; a knowledge base's labels are canonical titles, so
the short form matches the **parent franchise**. Measured: "Bleach:
Thousand-Year Blood War" has no id of its own, so asking for "Bleach"
scored the 2004 series at 1.00 and returned that show's Crunchyroll and
Netflix pages, saved onto the entry permanently. Frieren resolves on its
full title alone, so the variant buys nothing even where it looked
useful.

**Wikidata's streaming-id coverage has a cliff, and current seasonal
anime is on the wrong side of it.** Measured over the owner's real
tracked titles: **0 of 3** carry a Crunchyroll (P11330), Netflix
(P1874) or Prime (P8055/P14440) id, though all three exist as entities
with exact label matches. Headline and older titles carry them; the
current season does not. So the Wikidata path is a genuine fallback, not
a replacement for AniList - and **Amazon Prime was measured and rejected
on exactly this** (roadmap #14). Don't propose a new streaming service
without measuring coverage over real entries first.

**Watch progress is not a Wikidata problem.** It is personal history;
only a list service the user keeps can answer it. AniList is the only
one wired up. Kitsu's public API answers keyless (candidate, roadmap
#17); MyAnimeList v2 403s without a client id.

**Netflix ids come from Wikidata (`wikidata.py`, property P1874), not
AniList.** Keyless and public, two requests per lookup, and it answered
for every title tried while AniList was blocked. AniList's Netflix rows
remain as a fallback. Two traps already paid for:
- *Many entities share one label.* "Frieren" returns a manga, a
  character, a painting, a Norwegian TV film and the anime. Label
  similarity alone cannot separate them; what does is that only the
  anime carries a Netflix id at all. Don't loosen that to "best label
  match" - it would pin an entry to a painting.
- *A correct id can still 404.* Netflix ids are global, its catalogue
  is regional. Frieren's id is right and 404s from this region, so the
  resolved URL is probed before being saved and a confirmed 404 is
  discarded. Only a 404 discards - a failed probe keeps the link.

## The anime indexers, and which ones this network can reach

`indexers.py` asks **by title**, which is the one thing an IMDb-keyed
addon cannot do, so it reaches fansub releases no id-based index
carries. Two are in it, both measured returning real magnets: **Anime
Tosho** (`feed.animetosho.org/json`, 75 rows in 0.4-2.5s, carrying
`info_hash`, a magnet with its trackers and a seeder count) and
**SubsPlease** (`subsplease.org/api/?f=search`, the current season).

**Do not re-add Nyaa, Tokyo Toshokan, AniDex, Erai-raws or ToshiMoe as
direct sources.** Measured 19 August 2026 from the owner's connection:
nyaa.si times out, tokyotosho.info / anidex.info / erai-raws.info reset
the connection before TLS, and tosho.moe / toshi.moe do not resolve at
all. **It is not DNS** - `dns_resolve` answers for the first four
(186.2.163.20, 172.67.214.1, 185.178.208.171, 185.178.208.159) and the
connection still dies, which is upstream filtering nothing in this app
can route around. All of them are reached second-hand anyway: Anime
Tosho *is* an index over Nyaa/TokyoTosho/AniDex (its rows carry
`nyaa_id`, `tosho_id`, `anidex_id`) and carries SubsPlease and Erai-raws
releases under their own group tags, and the "Torrentio Anime" default
addon is pinned to exactly those three providers and scrapes them from
its own server. AniRena answers 200 with an empty 9KB page for every
search shape tried - nothing to parse.

**A title search can return the wrong show, and that is the whole risk.**
`indexers.is_same_title` requires 60% of the entry title's significant
words to appear in the release name, and `episode_match` requires the
episode to be stated (`S01E05`, `- 05`, or a range that covers it) -
anything else is dropped rather than offered. Measured near-miss that
set the threshold: asking Anime Tosho for "Bleach: Thousand-Year Blood
War 05" returns "[BlackRabbit] Bleach (2004) - S05" in **first place**.
Don't loosen either check to raise the hit rate.

## A franchise's season split is the entry's, not TMDB's (5 September 2026)

His report: *"in JJK S01E01 it plays an ep from another season in
almost all sources!!!!!"* Measured on his Jujutsu Kaisen (tt12343534)
at S01E01: 68 rows, and **the head of the list was `[Erai-raws] Jujutsu
Kaisen: Shimetsu Kaiyuu - Zenpen - 01` at 687 seeders** - season 3, no
season number in its name, so `indexers.stated_seasons` saw nothing and
only the arc map (`anime_identity`) could have caught it. It could not:
the map was built on **TMDB's** season split, and TMDB files the whole
show as one 59-episode Season 1, so the AniList works for seasons 2
(2023-07) and 3 (2026-01) had no season to attach to and the stored map
was `{1: [jujutsu, kaisen]}` - the franchise's own name as season 1's
arc words, matching every release of every season.

Cinemeta's meta (`meta-series-<imdb>.json`, the file the details page
keeps) has S1 (24), S2 (23), S3 (12) with air dates that line up with
AniList's exactly, and that is the numbering the addons are asked in.
`anime_identity._entry_seasons` now reads the split from there, TMDB is
the fallback, both TMDB's English names and AniList's romaji are attached
to a season **by air date, never by number**, and the entry's own title
words can never be arc tokens. Rebuilt: S3 = shimetsu/kaiyuu/zenpen/
culling/game, the Erai-raws row dropped, every S01 row kept; Demon
Slayer's five arcs and Bleach TYBW's S17 rows unchanged. `_CACHE_VERSION`
is 2 so every v1 map on disk is rebuilt.

Two locks behind it, both in `streams.py`: `_promote_seeded_head` may no
longer move a row that states less about the season than the ranked head
does (it was what put the 687-seeder row above the S01 head), and an
arc season counts as in range whatever the numeric bound says.

## A big swarm starves the first piece (5 September 2026)

His report: *"the connecting to source takes a while then the buffering
then it goes to connecting to source again."* Measured with the real
solo `streams.prepare` on the release he hand-picked and the Judas S01
batch: **112 peers, 18MB/s, 153MB of the chosen file on disk in 14s, and
its first piece still missing** - so `await_start` returned `no-peers`
at the 12s cap and the player raced the rest ("connecting again"). Every
piece of the file outside the two head pieces sat at priority 1, and
libtorrent hands each block of a piece to one peer, so a hundred peers
filled the file while the head crawled. `torrent_engine.WARMING_FILL_
PRIORITY` (0 while the first piece is missing) and `close_redundant_
connections` off carry the numbers: first piece 12.6-21.1s -> 5.0-7.5s,
peers held through warming. A lane that is delivering bytes is no longer
cut at the cap either (`DELIVERING_CAP_S`).

## A hand pick is waited for, not budgeted (5 September 2026)

His report: *"when I played Obsession the source I select did not play,
instead played some 6 seeds source"*. His log had `pick_file` and then
`source 0 did not start (timeout)` 6.6s later, twice, on a 70-seeder and
an 11,451-seeder release. Traced on the real path: `streams.prepare`
slept `SOLO_METADATA_TIMEOUT + SOLO_DATA_WAIT` (2.5s each after
`requested_fixes_patch`) plus a 3s grace and returned `timeout` at
8.05s, while the engine arm it had started - allowed `data_wait_max`
12s, `DELIVERING_CAP_S` while bytes flow, and a resume's index and
seat-band waits on top - handed back a playable url at 9.81s to nobody.
The torrent stayed in the session at 68 peers and 11MB/s behind the
row the walk played next. `prepare()` now sleeps until its arms have
spoken (`_prepare_cap` is only a ceiling), releases the torrent if that
ceiling ever fires, and a race lane that wins after the race returned
gives its torrent back. Measured after: the same two picks return a
url at 6.8s and 12.6s. `await_start` logs why a lane was given up on.

## An .mp4 seat cannot be resolved, so it is seeked blind (5 September 2026)

His report: *"when I stop at some point in the movie and come watch it
again it starts from the beginning, not like the anime and series!"*
`torrent_engine.resolve_start` maps a time to a byte through Matroska
Cues and answers None for anything else - correctly - but the seat poll
in `player_resume_latency_patch` read None as "not yet" and waited its
whole 180s for a band that was never going to be armed, while the film
played from 0:00. His film sources are .mp4 (`ftyp` + a 5.3GB `mdat`,
the `moov` at the tail); anime and series are .mkv, which is the whole
difference he saw. `_Torrent.seat_resolvable()` now tells "never" from
"not yet" off the first 4KB, the poll seeks without a band in that case
and the engine follows the demuxer's reads (`_serve` refocuses on the
byte mpv asks for), and `await_start_band` stops charging an .mp4 resume
RESUME_BAND_WAIT for nothing. Measured over the engine's own server: a
ranged read at his seat answered in 15.5s cold and 5.8s with the swarm
up - slower than an indexed seat, never instead of it. Parsing the MP4
sample tables for an exact byte was the follow-up, and it landed the
same day - see "An MP4 seat is exact now" below.

## A genre tick walks its pages together, and reads its caches first (5 September 2026)

His report: *"in the watch and read pages, when I apply a filter like
romance it takes ages to load the cards!!!"* Two causes, one per side.

**Watch.** Anime is Cinemeta's series catalog filtered by `genre=Anime`,
so a second genre is applied to the rows and `discover_video` walks
catalog pages to fill a batch - serially, four pages per batch, at
0.3-10s a page cold, with 1-8 Romance anime in a page of 50. His log:
batches at +5.9, +9.2, +12.3, +22.8, +24.5, +25.2, +31.1 and +42.6s.
`LOCAL_GENRE_PAGES` (8) are now fetched together, so a batch scans 400
rows for the cost of its slowest page.

**Read.** The first `/api/genre` answers from disk (`reading_genre_
cached`, 0.03s, 35 Romance rows), but every `/api/more` continuation went
straight to `reading_genre_sites`: measured **19.1s** - 3.0s browsing
his six sites, 0.7s probing them, 16.0s classifying 150 titles against
MangaDex - and `pullGenre` asks up to four times. `_more_browse` now
answers from the caches and only sweeps live when they hold nothing
beyond what is on screen.

## Every source is asked at once

`find_streams` fans its addons, the indexers and `resolve_page` out over
one short-lived thread pool (`_run_all`, 6 workers) under one deadline.
It used to walk the addon table serially inside a loop over the episode
numberings, so a lookup cost the sum of every request and one slow host
delayed everything behind it - measured 3.7-4.5s serial against 1.3-2.6s
for the same answers. **Only the fallback numbering stays sequential and
conditional**, and that is deliberate: a later guess returns a
*different episode*, so it may run only when nothing else answered.

Not `lookup_pool`: that pool is four workers wide and shared with the
tracker's page-load backfill, so the one lookup the user is watching
would queue behind all of it.

## Short waits beat long ones

`streams.METADATA_TIMEOUT` and `DATA_WAIT` are both 12s, down from 45s
and 25s. **Waiting longer never rescued a dead release; it only delayed
a live one** - a swarm with the trackers in its magnet answers in
seconds. What makes short waits safe is that `prepare_fastest` is now a
*rolling* race: `RACE_WIDTH` workers pull from the candidate list and a
worker whose release fails takes the next one immediately, so being
wrong costs one more attempt rather than one more minute. The old
version started a fixed batch of three and waited for it, then the
player re-ran `prepare()` serially on the release that had just failed.
Measured after: `find_streams` 3.4s + `prepare_fastest` 6.7s to a
playable URL on Attack on Titan S01E05.

## Subtitles: where Arabic for anime actually comes from

**AnimeTosho release attachments** - measured 19 August 2026, live, on
the owner's own titles. Multi-sub groups (ToonsHub above all, Erai-raws)
publish every language track of a release as individually-downloadable
attachments on the release page: Solo Leveling S02E05 carries 19
including **Arabic [ara, ASS]**; Bleach TYBW and Wistoria answered too.
`subtitles._animetosho` searches the feed, opens up to 4 matching
release pages, and returns direct attachment URLs. Two traps paid for
in the same pass:
- attachments are **xz-compressed whatever the extension says**
  (`\xfd7zXZ` magic, handled in `_unpack`);
- `_parse_ass` used to take the **first** `Format:` line in the file,
  which is the [V4+ Styles] one (~23 columns), so every real .ass parsed
  to **zero cues**. Only the Format line naming Start/End is the events
  one. This bug silently broke every .ass subtitle from every source.

English results ride along (OpenSubtitles addon has 1-2 per anime
episode) as feedstock for the AI translator: picking one with an AI key
configured translates it to Arabic on the fly (`player.
_fetch_subtitle_worker` → `ai_translate`). SubDL is implemented and
dark until its key is pasted. TMDB ships a bundled key
(`packaging/tmdb_token.txt`, gitignored - the owner's own).

## When a lookup looks wrong

Check the title match before the network - most "wrong schedule"
reports are a near-miss title resolving to a different series.
Reproduce with the real title string, not a tidied one. That's usually
the whole investigation; reserve deeper tracing for a genuinely
critical failure, not one wrong schedule.

## Testing

Never hit real APIs in a test - stub module-level functions, assert on
what the page does with the answer. See the `test` skill for the
harness pattern. Verify the real end-to-end flow (the actual dialog/
save path), not just the resolver function in isolation - a resolver
that's provably correct in isolation still shipped broken once because
the UI raced it.

## Covers, and the four ways one goes missing (3 September 2026)

Measured on his own configured sites while fixing "in the searching page
the readings images are not loading" and "the reading cover images from
the 3asq site are not clear at all".

- **A Madara search endpoint returns no art at all.** "solo leveling"
  comes back with thirteen rows and **3asq's carries no `cover_url`** -
  titles and URLs only. `fetch_manga_details`' docstring has said so
  since it was written; nothing was calling it for a *search* row, only
  for the one entry the tracker saved. That is the whole of the blank
  reading card. "kingdom" measured 16 rows of 55 without art.
- **A site's own cover can be too small for a card.** Hunter X Hunter's
  3asq cover is the file `cover_250x350.jpg` - 250px against a card that
  draws 201 device pixels and a details page that draws more. Read off
  the *file name* (`manga_sites.named_cover_width`), because probing
  each cover's header is a request per row and a catalogue page is 41 to
  900 of them.
- **A cover URL that works can stop working.** Two of Manhwa's first
  twenty-four cards ended blank on a cold cache: the URL 404s, the
  window's error handler strips the src, and nothing asks for anything
  better because the row *had* a cover.
- **MangaDex refuses this server's default User-Agent.** `uploads.
  mangadex.org` answers **400** to urllib's own UA and 200 with a
  browser one (measured, 148,640 bytes). Its covers were the fallback
  for every case above, so the fallback was silently dead - visible in
  the log only as `image fetch failed for uploads.mangadex.org:
  HTTPError`.

**All four are answered by one route, `/api/cover`, asked by the card.**
Filling covers *inside* the search was tried first and measured: three
runs of `_search` went from a **1.33s median to 2.23s and 3.34s with a
5.62s worst case**, because the chain is site round trips. Rule 7's
answer is to draw the row at once and fill it in - so the card asks,
the server walks the chain (the site's own card art, then MangaDex, then
AniList), **fetches each candidate before offering it**, and caches the
answer per (title, url). A replacement is also decoded in a detached
`Image()` before it is swapped in, because the first version replaced a
small-but-working cover with a 404 and left a blank tile.

Results, cold: Hunter X Hunter 250x350 → **600x642**; "Nano machine" and
"The Maid With a Child" (his two blank Manhwa tiles) → **512x742** and
**460x658**; the 3asq row in a "solo leveling" search → **720x972**.

## Warm a page's covers as it is answered

A reading catalogue meets cold covers constantly - the sweep brings
titles this machine has never seen - and a scanlation host takes
0.2-3.0s. Measured on the first screenful of 24 cards, cold: **Movies
94ms, Anime 154ms, Series 460ms, Manga 4.6s, Manhwa and Manhua over
14s**. Warm, all of them are 31-1669ms, so the cost is entirely the
first sight.

`web/backend.warm` takes the tokens of whatever a route just answered
and fetches them on four workers behind the response
(`server._warm_covers`, called once for every `/api/` answer). Bounded
to the first 200 and skipping anything already on disk. After it, cold
Manhwa's whole first screenful is decoded by the time the grid first
paints (993ms, 0 blanks).

## A chapter page can be 20MB

Every page of Kingdom (WAN) ch.886, fetched directly: twenty at 1.3-2.4MB
and **page 20 at 21,386,176 bytes** (7659x5500). `web/backend`'s image
cap was 16MB, so `net.read_bytes` raised "response body over the size
cap", the proxy answered 404, and the chapter had a hole in its last
page with nothing in the log to say why. The cap is 32MB, the wall clock
25s, and a size drop now names the size.

## A debrid stream keeps its info_hash and is not a torrent

`streams._prepare_with_debrid` hands back `kind="direct"` with a plain
HTTPS URL and **keeps `info_hash` for identity** - its own docstring
says so. So anything deciding "is this served out of the piece store"
must read `kind`/`engine`, never the hash: a guard that read the hash
sent every debrid play down the slow resume path, and his log says
`debrid: on - releases it has cached play over HTTPS`.

## Cinemeta takes one genre per catalog, and "anime" is one of them

`discover._KIND_TYPES` maps anime to the **series** content type, and what
makes the anime section anime is asking for `genre=Anime` - the
distinction exists only server-side (see `_video_catalog_urls`). So
"anime AND romance" cannot be asked for at all.

Asking `genre=Romance` on the series catalog and labelling the answer
Anime is what the code did, and it is wrong in the way that is hardest to
see: measured 4 September 2026 on the Anime page's Romance tick, **50
rows, every one labelled Anime**, headed by Off Campus, My Life with the
Walter Boys and Grey's Anatomy.

The fix is to ask the anime catalog and apply the wanted genre to the
**rows** (`local_genre` in `discover_video`), whose `genres` are real. One
page of 49 holds few of any one genre - Romance answered 1 - so a bounded
walk of further pages follows, and it needs a wall-clock budget of its
own: unbounded it took **17.8s**, at 4s it returns 14 Romance anime and
stops. A Cinemeta page is 0.3-10s depending on whether its CDN holds it,
so any loop over pages must be timed, not counted.

## An MP4 seat is exact now, and the index band is the container's (5 September 2026)

The follow-up above is done. `torrent_engine._mp4_find_moov` walks the
top-level atoms from the head (ftyp, then an mdat whose 64-bit size says
where a tail moov starts - his Obsession release: `moov` at
5,314,433,909, 6,784,467 bytes, ten tracks), `_mp4_parse_moov` reads the
first video track's stts/stss/stsc/stsz/stco tables, and
`_mp4_seat_for` answers the byte of the last sync sample at or before
the seat. Validated against ffprobe's packet positions on a 217MB test
file in both layouts: 10 of 10 timestamps equal the keyframe's `pos`.
`seat_resolvable` is the same tri-state for MP4 as for Matroska (None
while the moov's header is not on disk), and `_index_pieces` wants the
whole moov at the index's priority - **the 4MB tail band never covered a
6.78MB moov**, which is why an MP4 open blocked on pieces nothing had
prioritised.

**Found on the way: `_apply_windows` read a name no line ever bound.**
`for index, piece in enumerate(reversed(tail))` - `tail` was never
assigned, in the committed file too; the NameError landed in the
function's outer `except Exception: pass`, so the index deadlines and
the resume band's deadlines after it had never once been applied. Both
bands still arrived, on priority alone. Proved with `ast` (the name is
in no Store and no argument list), fixed by binding `tail` to the index
band. When a measured ordering "does nothing", check the function's
names before its numbers.

## A race lane is judged by its rate, not its first byte (5 September 2026)

`prepare_fastest` used to play whichever lane completed a first piece
first. His Obsession trace: an 11,451-seeder lane at 2.0-2.6MB/s won
three seconds ahead of a 390-seeder lane at 3.4-4.3MB/s, on a 3.1MB/s
film, and stalled. A lane now reports `torrent_engine.payload_rate` at
its first byte against `_playable_rate` (the chosen file's size over the
runtime the player passes, `runtime_s`, times PLAYABLE_RATE_MARGIN 1.5).
Fast enough wins at once; too slow is held as *provisional* for
RACE_SETTLE_S (3s) while a better lane may replace it, and plays if none
does. A debrid answer outranks any provisional lane. Without a size or a
runtime the threshold is zero and the first byte wins as before. Every
decision is one `race:` line in the log. Harnessed with mocked lanes
(scratch `race_harness.py`): fast-second beats slow-first at 1.2s, a
healthy first lane wins at 0.4s with no settle paid, a lone slow lane
is kept.

## The engine stream handed mpv a false end-of-file (5 September 2026)

His "seeking on the swarm path ... not pressing any buttons to play each
time". `_serve` ends a response on purpose when a piece does not arrive
inside PIECE_WAIT_S, expecting the reader to reopen. urllib answers a
body cut short with b"" and no exception (measured: 100 bytes declared,
10 sent -> b'0123456789', b'', b''), and `_EngineStream.read` reopened
only on an exception - so a tidily ended response reached mpv as EOF,
and under `keep-open=yes` mpv paused on its last frame until he pressed
play. The read now reopens from its offset on any empty read short of
the file's end (REOPEN_SILENCE_S bounds a dead swarm; a 404 is a real
stop); measured against a server that ends every response after 10
bytes, 1024 bytes came back identical over 104 reopens. Two locks in
the player behind it: `_ensure_playing_after_seat` lifts a pause mpv
took by itself while a seat was owed (never one the viewer pressed, and
never when the viewer was paused when they seeked - `_paused_at_seek`),
and `_seek_absolute` asks `torrent_engine.seat_on_disk` so a jump
*backwards* into never-fetched pieces takes a seat too.

## The cached debrid row leads its group (5 September 2026)

`_default_pick_key` sorts `debrid_cached` after `arabic` and before the
seeders, `_promote_seeded_head` leaves a cached head alone, and partials
are flagged from the library already in memory (`debrid.known_cached`,
no request) and ranked with the season term - they were ranked without
it, so a wrong-season row could lead a partial the player then started
on. The hand pick and the race's debrid lane are unchanged.

## Placeholder-aware covers, and what the census found (5 September 2026)

`server._placeholder_reason` rejects a fetched candidate that is named
like a stand-in, is not an image, is under COVER_MIN_EDGE (80px) on a
side, or is byte-identical to another title's accepted cover; the chain
then continues to the catalogue and the log says why. Measured first
over his six sites (browse and search rows, `cover_census.py`): 3asq 21
rows, TeamX 57, Lava Scans 82, SWAT 60, Mangalek 30, Azora 70 - no
repeated cover URL, no duplicated bytes, no placeholder-named file -
none of the *sites* does it. **The catalogue does.** Photographed on the
frozen build the same day: his Manga page drew Hunter X Hunter's card
as MangaDex's "You can read this at MANGADEX" picture, filed by
MangaDex as the title's cover art - 512x807, portrait, 369 distinct
colours, an ordinary title to the API - taken because the site's own
cover was a thin 250x350. No rule but its picture tells it from a real
cover - and not its bytes either: the proxy serves a re-encoding out
of its cache (59,480 bytes for the 148,640-byte CDN file), so a byte
hash never fired. A 16x16 average hash (`server._ahash`) is identical
across the three size variants and 72 bits from the nearest of 300 real
cached pictures; the two renditions seen (512x807 on the CDN, 600x642
in his cache, 110 bits apart) are seeded in
`server.KNOWN_PLACEHOLDER_AHASH`, hashes the duplicate rule proves later
are kept in `cover_placeholders.json`, and the chain now asks MangaDex
and AniList as two candidates so a refused MangaDex cover falls to
AniList's rather than back to the thin file. Lava Scans serves four
covers at 160-190px (real, and `thin` handles them) and Azora lists 33
rows with no art at all, which the chain already fills.

## A reading sweep answers with the rows it has (5 September 2026)

His report: *"the grid loading in reading pages on other device is super
super slow"*. Measured cold from his six sites: browse 180 rows 2.9s,
probe 0.8s, **classify 150 titles 53.2s** (22 by 8s, 56 by 20s) - the
MangaDex throttle (`_MIN_REQUEST_GAP` 0.35s, one lock) caps it at three
titles a second whatever `MEDIUM_WORKERS` says, and
`reading_sites_by_medium_all` returned nothing until every title had a
verdict. On the frozen build the cold Manga page's first batch never
came inside 70s. Every visit fired two sweeps (`liveBrowse` and the
first `moreOnScroll` pull) and the undisposed pages kept firing more.

Now one sweep runs at a time (`_SWEEP_INFLIGHT`; a second caller waits
for the running one), it returns at `CLASSIFY_BUDGET_S` (6s) with the
titles that have a medium while the rest keep classifying into the
cache, `_classify` asks MangaDex once per title however many sweeps
overlap (`_CLASSIFY_INFLIGHT`), and `server._more` answers `pending` so
app.js keeps pulling instead of calling an empty batch dry. Measured
after: first Manga batch at **8.6s** cold, 16 cards by 30s, and the log
says `reading sweep: 16 of 149 titles have a medium inside 6s; 133 still
classifying`.

## The anime search's second witness is Cinemeta's own meta (5 September 2026)

AniList answered 403 all evening, and `_anime_confirmed` had just been
changed to empty the section on that - which was "the Kingdom Anime is
not showing at all when searching". Cinemeta's search rows carry no
genres, but each row's meta does (`tt2404499`: Animation, Japan; the
Korean, American and 1994 Kingdoms none of it), so `_anime_by_meta` asks
`stremio.fetch_meta_cached` for every row at once, keeps `looks_anime`
rows, and waits `ANIME_META_WAIT` (5s: cold CDN misses answered in
3.7-6.0s, warm ones 0.5s, the disk cache 7ms). Only when that too has
nothing is the section empty. On the frozen build: `Cinemeta's meta kept
1 of 12 rows (12 answered inside 5s)` and the Anime section shows the
2012 Kingdom while AniList still refuses.

## A pull does not browse again, and a fresh install starts warm (6 September 2026)

His report the morning after the sweep learned to answer early: *"the
loading of the grids in the reading pages is super slow in the other
device!"*. Two costs a cold device still paid. Every pull re-ran the
six-site browse and the probe before waiting on a verdict (2.9s here,
the slow half on a slow connection): the browsed rows are kept for
`SWEEP_ROWS_TTL_S` (90s) and a pull they can still answer pays only
`CLASSIFY_PULL_BUDGET_S` (3s). And every verdict was paid from nothing:
the 936 title -> medium/genre verdicts this machine had collected from
the same six sites ship as `helpers/reading_seed` (a Python module, so
no spec change; regenerate, never edit) and are read under
`reading_meta.json` - the file wins, only unknown titles are asked.
`mangadex._MIN_REQUEST_GAP` is 0.25s (four a second, a fifth under the
documented five). When the cache already answers at least as many
titles as are being asked, the first budget is
`CLASSIFY_KNOWN_BUDGET_S` (1.5s) - measured, the seeded first sweep
knew 137 of 178 titles and still waited 6s for the rest.

Measured on the frozen build, cold copy (no reading cache, no
verdicts): first Manga batch **25 rows** at 8.6s with the 6s budget
(the night before: 3 rows), then 3s pulls with no browse - `149 of
178`, `161`, `174` - and a full grid on screen at 12s.

## A reading genre tick answers early too (6 September 2026)

His report: *"when I apply the filter in the other device it takes ages
to load the grid in the watch and read pages"*. Measured through
`server.answer` on a cold copy: `/api/genre?name=Romance&reading=1`
**23.6s**, and on a warm copy the continuation past the cache
(`/api/more` for `genre:Romance:1:all`) **17.6s** - `reading_genre_sites`
browsed the six sites again and classified every title before answering
a row. It shares the medium sweep's browse (`_browsed_rows`, kept 90s)
and budget (`_classify_pairs`, the same verdict the medium sweep reads)
now, `server._genre`/`_more_browse` answer `pending`, and app.js
`pullGenre` keeps walking while that is above zero
(GENRE_PENDING_BUDGET_MS) instead of calling an empty batch dry. After:
4.7s cold for the first page, 0.01s for the continuations, and 21
Romance rows on screen 12s after the tick on a cold Manga page. The
video genre pages are Cinemeta's own speed (1-5s a batch here) and are
untouched.

## A chapter with no pages says why, and is not marked read (6 September 2026)

Found by the aggressive reader pass: two of ten chapters opened a reader
that showed nothing and said nothing. Lava Scans lists its **paid
chapters** in the same list as the free ones (the "30" in a row's title
is the coin price, "مجاني" marks a free one) plus an "احدث فصل" ("latest
chapter") placeholder row, and answers **200 with no `<img>`** for any of
them. `chapter_source.chapter_pages` now says why when it has no pages
(`reason`: `locked` when the page carries purchase wording - "buy",
"purchase", the Arabic for coins - which measured present on both locked
pages and absent from the free chapter 211 of the same title; `empty`
otherwise; `unreachable` when the fetch failed), `backend.pages` logs
`chapter has no pages: host=... label=... reason=...`, and app.js
`openChapter` draws the reason with an "Open on the site" button and
**skips the read mark** - the pass had marked c174 and c214 read on two
blank readers. Photographed on the frozen build: row 173 of The Holy
Power Of Modern Medicine shows the message; row 168 reads and marks.

## The in-app download asks the service first, over four connections (7 September 2026)

His asks, in order that day: *"the downloading takes so long and many
times it does not download the ep, I want it to be like Stremio, it
somehow opens the browser"*, then *"the issue is the SPEED ... purely
on the internet speed"*, then *"retrieve the only in app download, and
make it as super fast as possible, cancel the browser download!"*. The
browser hand-off built for the first two (2-7 September: a direct link
opened in the system browser, then the engine's own URL served
patiently under `dl=1`) was **removed at his word** - the details
dialog's button, the player panel's, their bridges, and the engine's
browser-reader mode - and nothing references it now.

What was measured before the fix, source tree, copy of his data, Attack
on Titan S1E4: the queue **failed in 4s** - "Nothing could be
downloaded for this". The race's debrid lane wins with a direct HTTPS
link and keeps the hash for identity only, and `_run_video` then asked
the engine for a torrent it had never added. With a key in Settings
that is what most of his downloads met - the "many times it does not
download". And the speeds:

    a plain HTTPS file (proof.ovh.net)        7.9 MB/s   one stream
    the swarm, Attack on Titan S1E4, 28 peers 15.5 MB/s  peak, 0.4 in the first 10s
    Real-Debrid's CDN, the same episode       21.1 MB/s
    Real-Debrid's CDN, Adults S2E3            16.8 MB/s

`debrid.cached_hashes` called 4 of Attack on Titan's 30 releases held
and 0 of Adults' 16 while the service served every one at once, so
anything that asked the cache check first fell to the swarm.

`downloads._run_video` now asks the service for the best candidates
outright (`debrid.fetch_url`: the same addMagnet -> selectFiles -> link
as a play, but queued/downloading is polled up to FETCH_BUDGET_S with
the percentage in the job's detail line, and the torrent stays in the
account so a fetch that outran the budget is found ready next time),
and pulls the link over HTTP_WORKERS ranged connections into a
preallocated file in HTTP_PART_BYTES parts (`_fetch_ranged`), each part
retried on its own and the whole ones kept on the job so a pause
resumes where it stood. Only then the swarm, and the swarm is read in
order (`torrent_engine.sequential`) because with every piece at one
priority the head arrived last. A direct answer from the race is pulled
the same way. After, the same episode: **done in 34s, 499MB at
14.7MB/s overall with peaks of 25MB/s** on four connections. Not
exercised: the service's waiting branch - it held every release tried.
