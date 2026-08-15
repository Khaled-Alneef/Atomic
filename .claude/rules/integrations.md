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

Some services can't be scraped at all - Crunchyroll (JS-rendered search,
content API 401s without OAuth) and Netflix (search behind a sign-in).
Both resolve via AniList's `externalLinks` instead, which needs no auth
on either service: `anime_sites._STREAMING_SITES` is the table, and
adding another such service is a row there, not new code. An
authenticated Crunchyroll client (for progress sync) was researched and
rejected: dead password-login flow, ToS-prohibited scraping of a paid
service, and redundant with what `anilist.py` already covers. Don't
rebuild that without a real reason to revisit it.

**AniList rate-limits hard, and fails soft into silence.** Sustained
querying gets the whole network a `403` on every POST (not a 429),
which every lookup here swallows and reports as "no result" - so a
block looks exactly like "this title has no link". Measured lasting
over an hour. When a previously working AniList lookup starts returning
nothing, check for the 403 before believing the data changed.

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
