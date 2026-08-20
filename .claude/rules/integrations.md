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
