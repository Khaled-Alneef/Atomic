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
| `updater.py` | api.github.com | release tags and the update download |

No API keys anywhere. Keep it that way (the Stremio `authKey` is a
first-party login token, not a key, and is the one accepted exception).

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

**The supported route for Crunchyroll progress is MAL-Sync → AniList.**
The browser extension updates the user's AniList list as they watch, and
`anilist.py` reads it - nothing in Atomic to break when Crunchyroll
changes anything. Settings > Crunchyroll Progress carries the five
steps. Reach for this before proposing any direct Crunchyroll work.

**Reading Crunchyroll directly still exists, and is second on purpose**
(`crunchyroll.py`),
with a token pasted from the browser session. It is exact and immediate,
which is the one thing MAL-Sync is not - and it **expires inside an
hour**, which is why it is offered as a one-off read rather than an
account. Never present it as "connected".

Two measured facts, so they are not rediscovered: **email/password
cannot work** - minting a token needs a client credential Crunchyroll
issues to no third party, and the published one answers
`auth.obtain_access_token.client_inactive` on both hosts (`login`/
`refresh` remain, unreachable, and would work if a live credential were
put in settings.json). And **the token lives in the `token` request's
*response*** - that request's own headers carry `Basic`, not `Bearer`;
`Bearer` appears only on `content/v2` requests. Wrong instructions here
already cost the owner an evening. One shared history fetch serves a
whole page (`cached_history`); don't make it per entry. Using it at all
is against Crunchyroll's ToS, which is the account holder's call and was
made.

Some services can't be *scraped* at all - Crunchyroll (JS-rendered
search, content API 401s without OAuth) and Netflix (search behind a
sign-in).
Both resolve via AniList's `externalLinks` instead, which needs no auth
on either service: `anime_sites._STREAMING_SITES` is the table, and
adding another such service is a row there, not new code. An
authenticated Crunchyroll client was once rejected as redundant with
`anilist.py` - **that turned out to be wrong and it now exists** (see
above). AniList only knows what some other tracker wrote to it, so an
entry watched to episode 2 on Crunchyroll sat there reading episode 7.

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
