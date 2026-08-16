# Roadmap

Written from released version **1.3** on `development`. Covers roughly
the next month of work. Format and update rules: `.claude/skills/roadmap`.
Ordering rules and the standing facts this plan draws on (8 diagnosed
defects, usability gaps, the Amazon Prime coverage note):
`.claude/rules/planning.md`.

## Index

| # | Item | Owner | Size | Status |
|---|---|---|---|---|
| 1 | Survive an exception in a Qt slot, and log something | ui-engineer | spans modules | done |
| 2 | Bound manga_sites' regex and reads | integrations-engineer | contained | done |
| 3 | Bound `resp.read()` on every page-load lookup | integrations-engineer | spans modules | done |
| 4 | Give a dead host a deadline across the whole resolve chain | integrations-engineer | contained | done |
| 5 | Route video-site resolution through `lookup_pool` | integrations-engineer | contained | done |
| 6 | Back up settings/entries before an overwrite, and stop swallowing a corrupt file silently | ui-engineer | spans modules | done |
| 7 | Tell AniList's rate-limit apart from "no result" | integrations-engineer | contained | done |
| 8 | Investigate a second source for Crunchyroll | integrations-engineer | shape unknown - investigate first | done |
| 9 | Say so when Netflix/Crunchyroll watch progress can't be read | ui-engineer | spans modules | done |
| 10 | Surface a missing AniList username where it actually matters | ui-engineer | spans modules | done |
| 11 | Search and filter on tracker pages | ui-engineer | spans modules | done |
| 12 | Show whether an added site will resolve to title pages or only ever fall back to search | integrations-engineer | spans modules | done |
| 13 | Quick +1 for Anime/Series progress | ui-engineer | contained | done |
| 14 | Investigate Amazon Prime coverage before building it | integrations-engineer | shape unknown - investigate first | done - **not building it** |
| 15 | Investigate startup and page-rebuild performance | test-engineer | shape unknown - investigate first | done - **nothing to fix** |
| 16 | Investigate code signing to stop the antivirus false positive | release-engineer | shape unknown - investigate first | done - **decision needed from the user** |
| 17 | Investigate Kitsu as a second source for watch progress | integrations-engineer | shape unknown - investigate first | todo |
| 18 | Read Crunchyroll progress from Crunchyroll, and stop Stremio answering for entries watched elsewhere | integrations-engineer | spans modules | done - direct reading later **removed**, see #19 |
| 19 | Route Crunchyroll through MAL-Sync → AniList, and read AniList per season | integrations-engineer | spans modules | done |

Items 1-8 landed together as the correctness pass. Each block below
records what was actually built and what it measured - which in several
cases is not what the item originally assumed.

Antivirus false positive (defect #8) is listed at #16, ordered last of
the investigate-first items: it's the one item on this list resting on
a purchase decision the agent cannot make (a code-signing certificate
costs money), so the most useful thing an agent can do this month is
price it out and hand the user a decision, not implement anything.

---

### 1. Survive an exception in a Qt slot, and log something

**What** - Any exception escaping a Qt slot currently kills the whole
process (`qFatal`, exit `0xc0000409`, no traceback), and the app has no
logging at all - not even to a file - so a crash like this leaves no
evidence of what happened.
**Why now** - Correctness/stability outranks every feature on this
list. A single bad response from any of five external services, or one
bug in any slot, currently means a silent full-app kill with nothing to
diagnose it from afterward. This is defect #5 in `planning.md`, already
diagnosed - a ~20s startup crash was one measured instance.
**Owner** - ui-engineer (main.py owns app startup/shell; the exception
guard has to wrap the whole Qt event loop, and the file-logging setup
belongs next to it).
**Where** - `src/main.py`: `main()` at line 735, `QApplication(sys.argv)`
at line 736, `if __name__ == "__main__":` at line 767. No existing
`sys.excepthook` or logging import anywhere in the file today (checked
`main.py`; grep for `except Exception`/`qFatal`/`logging` returns
nothing). Add a `sys.excepthook` that logs the traceback to a file under
`storage.DATA_DIR` (never `%APPDATA%\Atomic` directly in a test - see
`.claude/rules/testing.md`) before Qt tears the process down, and audit
call sites feeding into slots for bare re-raises.
**Done when** - an exception deliberately raised inside a connected slot
(e.g. a test button wired to `raise ValueError`) no longer crashes the
window, and a log file exists afterward with the traceback in it.
Existing behaviour (every other slot, startup, navigation) is
unaffected - regression-checked by running the app end to end, not just
importing it offscreen.
**Risk** - a `sys.excepthook` that swallows *too much* would hide a
crash that should actually stop the app (e.g. corrupted Qt state);
scope it to log-and-continue for slot exceptions specifically, not a
blanket catch-all around the event loop.
**Landed** - `src/helpers/logs.py` (rotating file log at
`DATA_DIR/atomic.log`, 512KB x 2) plus `logs.install_excepthook()` as
the first line of `main.main()`, before `QApplication`. Verified with a
real Qt event loop and a button whose slot raises: with the hook the app
survives and the traceback is in the log; the **control run without it
died with exit `0xC0000409`** - the same code the original crash
reported, which is what makes the passing run meaningful rather than
self-confirming. KeyboardInterrupt still goes to the default hook.

### 2. Bound manga_sites' regex and reads

**What** - `manga_sites.py` can freeze the whole app on a malformed
search result, the same way `anime_sites.py` could before 1.3.
**Why now** - Python's regex engine holds the GIL, so this stops every
thread including the UI. `anime_sites.py` measured 32.5s on a 0.7MB
page before its fix; Windows offers to kill a window unresponsive for
~30s. Reachable from the Add/Edit form. Diagnosed defect #1.
**Owner** - integrations-engineer
**Where** - `src/helpers/manga_sites.py`: `_AJAX_SEARCH_CARD_RE` (~252),
the `.*?` at ~102, `resp.read()` at ~184/194. Copy the fixed pattern
from `anime_sites._read_body` (read1 + size cap + wall-clock deadline) -
that fix already shipped and is the reference implementation.
**Done when** - a malformed 2MB page parses in under 0.1s, a slow-drip
host returns inside the timeout, and the existing search results for
the known reading-site shapes are byte-identical to before.
**Risk** - the parity check is the point: these engines are the only
thing standing between a reading site and no suggestions at all.
**Landed** - and the fix went further than the item asked. Capping the
lazy body (`.{0,2000}?`) alone still measured **224ms** on a 1MB
fragment with 20k unclosed anchors, because the scan is quadratic in the
number of anchors, not just in body length - so the cap shrinks the
constant without removing the class of bug. `_ajax_search_cards` now
splits on `</a>` and matches each piece from its last opening anchor
(`str.split`/`rfind`, both linear and in C), with the cap kept as a
second line of defence: **224ms -> 0ms**. `_MADARA_COVER_RE` took the
cap alone (3ms). Reads went to `net.read_text` with item #3. Parity
checked against well-formed samples of both markup shapes - captures
identical to the old patterns.

### 3. Bound `resp.read()` on every page-load lookup

**What** - An unbounded `resp.read()` on the page-load path means a
slow-drip host never returns, and since `lookup_pool` has only 4
workers, 4 stuck lookups drain it completely - every other entry's
lookup queues behind them indefinitely.
**Why now** - This is the same class of bug as #2 but on the hot path
(every tracker page visit that needs a refresh), not just the Add/Edit
form. Diagnosed defect #2.
**Owner** - integrations-engineer
**Where** - `anilist.py` ~110 (`_post`, the `with urlopen(...) as resp:
return json.loads(resp.read()...)` shared by every AniList call),
`stremio.py` ~35/89/117, `tvmaze.py` ~47, `mangadex.py` ~94,
`images.py` ~44. Five files, one shared fix shape (size cap + deadline,
same as #2's reference).
**Done when** - each of the five sites has a bounded read with a size
cap and wall-clock deadline; a synthetic slow-drip response (a socket
that sends 1 byte/sec) returns or raises inside the stated timeout
instead of hanging, verified per file.
**Risk** - touches five files feeding the shared `lookup_pool` - a
partial fix (four done, one missed) still leaves the pool as
exploitable as before, so treat this as one item, not five, and don't
call it done until all five are verified.
**Landed** - as one shared `src/helpers/net.py` (`read_bytes`,
`read_text`, `deadline_in`) rather than a sixth copy of the pattern -
copying it once already (anime_sites -> manga_sites) is exactly how
these five were missed. `anime_sites._read_body` and `wikidata._get_json`
now delegate to it too, so there is one implementation left, not four.
Two extra call sites were bounded beyond the item's list: **stremio's
account API** (`_api_post`, which `login` uses) and **`updater._get_json`**
- the update check the user actually waits on, where an endless body
would have hung the Settings dialog. Verified against a local server
dribbling one byte per second forever (the shape a socket timeout never
catches): every one of the seven returned or raised in **6.0s**, where
the old code never returned at all. Size cap verified separately.

### 4. Give a dead host a deadline across the whole resolve chain

**What** - Resolving a video site currently tries 3 engines plus a
generic scraper in sequence, each with its own independent 6s timeout
and no deadline across the whole chain - a dead host costs roughly 24s
per resolve.
**Why now** - Diagnosed defect #3, and it compounds with #12 below:
until this is fixed, the "will this site resolve" signal a user gets
by adding one is "wait 24 seconds and see."
**Owner** - integrations-engineer
**Where** - `src/helpers/anime_sites.py`, `resolve_page_url` (the
per-engine loop that tries each of the 3 engines then the generic
fallback).
**Done when** - a dead host's total resolve time is bounded by one
overall deadline (e.g. 8s) shared across all engines, not per-engine;
measured against a real dead host, not asserted.
**Risk** - cutting the deadline too aggressively could turn a slow-but-
real hit into a false miss; measure real engine response times for
working hosts first so the shared deadline doesn't undercut them.
**Landed** - one deadline for the whole chain, threaded through
`search_site(..., deadline=)` so it bounds the three engines too, not
just the loop over them; `_step_timeout` gives each request
`min(timeout, remaining)` and gives up below 1s rather than opening a
connection that cannot finish. **Budget is 2x timeout (12s), not the 8s
the item suggested**, and deliberately: a real hit needs one request
that is itself allowed the full 6s, so anything under 2x means one dead
engine ahead of the right one turns a slow-but-real answer into a miss -
and a wrong "no page found" gets saved on the entry, while slowness is
only ever slowness. Measured against a host that accepts and never
answers: **12.0s, previously ~24s** for one query variant and twice that
for a title with a subtitle. `deadline=None` keeps every other caller's
behaviour unchanged.

### 5. Route video-site resolution through `lookup_pool`

**What** - Typing in the Video Website search box fires a bare
`threading.Thread` per debounced keystroke, outside the shared 4-worker
`lookup_pool` - so this path has no concurrency cap at all.
**Why now** - This is the exact shape of bug that already shipped once
(up to 651 simultaneous connections measured, saturating the user's home
network) - `lookup_pool.py` exists specifically to prevent it, and this
call site doesn't use it. Diagnosed defect #4.
**Owner** - integrations-engineer (background threading is this role's
scope per `CLAUDE.md`, even though the call site lives in a UI page
file).
**Where** - `src/windows/tracker.py`, `_start_video_site_resolution`.
**Done when** - typing quickly in the Video Website search box never
opens more than `lookup_pool`'s worker count in flight simultaneously
(verified by counting live threads/connections during rapid typing, not
just reading the diff).
**Risk** - debounce timing interacts with pool queueing; verify the
search box still feels responsive (results still land before the user
moves on) after routing through the shared queue, not just that the
thread count is bounded.
**Landed** - **not on the shared queue**, and that risk line is why. The
shared pool is drained by page-load backfill of every tracked entry, so
a lookup the user is watching a status line for would have waited behind
it - the same reason `_sync_progress` deliberately keeps its own thread
(tracker.py ~842). `lookup_pool.submit_latest(key, ...)` instead: one
dedicated worker, and a newer request under the same key *replaces* a
queued older one, which is right because every debounced keystroke but
the last is already stale by the time it answers. Caps this path at one
connection - tighter than the pool's four. Measured over 60 rapid
submissions: peak in flight **1**, **1 of 60** actually ran, and the
newest always ran.

### 6. Back up settings/entries before an overwrite, and stop swallowing a corrupt file silently

**What** - `storage.load` returns `{}` on any `JSONDecodeError` or
`OSError` with no logging, and the very next `storage.save` overwrites
the file with that empty default - silently destroying whatever was
there. This already happened for real: a BOM'd `settings.json` failed
to parse and the app overwrote it, destroying the stored AniList
username (this is *why* the username is empty right now, per this
month's other two usability items).
**Why now** - Data loss outranks every feature on this list. Diagnosed
defect #6, and its blast radius is anything in `%APPDATA%\Atomic` - not
just settings.
**Owner** - ui-engineer (storage.py is shared plumbing with no other
clear owner; closest to the pages/dialogs that call it, and pairs
naturally with the logging added in item #1).
**Where** - `src/helpers/storage.py`: `load` (~41-49) and `save`
(~52-55). Add: (a) on a parse failure, log it (via #1's logging setup)
instead of failing silently; (b) before `save` overwrites a file, copy
the existing file to a `.bak` alongside it, so a bad write is
recoverable; (c) consider refusing to overwrite a real file with an
empty/near-empty default when the existing file was non-trivial, since
that's the exact shape of the destructive case already hit.
**Done when** - a deliberately corrupted `settings.json` (BOM'd or
truncated) logs the parse failure instead of silently returning `{}`,
and a `.bak` of the pre-overwrite file exists after the next save;
tested against a temp-directory copy per `.claude/rules/testing.md`,
never real user data.
**Risk** - this is the highest-value item on the list given it already
cost the user real data once; don't scope it down to "just add
logging" - the backup-before-overwrite half is what actually prevents
recurrence.
**Landed** - four changes, and one of them fixes the original incident
outright: `load` now reads **utf-8-sig**, so a BOM'd `settings.json` -
the exact file that lost the AniList username - parses instead of being
declared unreadable. Beyond that: a file that genuinely can't be parsed
is moved to `<name>.corrupt` rather than left for the next `save` to
overwrite; `save` writes a temp file and `os.replace`s it into place, so
a crash mid-write can no longer leave a truncated file (which read back
as "empty" and was then made empty for real); and the previous contents
are kept as `<name>.bak`. Every failure is logged through item #1.
**Deliberately not done**: refusing to write an empty list over a full
one. Emptying a page is something the user is allowed to do, and a rule
guessing at intent would eventually block a legitimate save - the `.bak`
covers the accident without ever standing in the way. Verified on a
temp copy: BOM'd file reads, truncated file quarantined with its bytes
intact after a following save, `.bak` present, no `.tmp` left behind,
`update_entry` still round-trips.

### 7. Tell AniList's rate-limit apart from "no result"

**What** - AniList returns a `403` on every POST (not a `429`) when
rate-limited, lasting over an hour by measurement. Every AniList call in
`anilist.py` uses a bare `except Exception: return None`, so a block is
indistinguishable from "this title genuinely has no AniList entry" -
both look like silence to the user and to the code calling it.
**Why now** - Diagnosed defect #7's first half (the second half, no
alternate Crunchyroll source, is item #8 below - investigation, not a
fix). This part is a contained, mechanical fix: catch the specific
error instead of everything.
**Owner** - integrations-engineer
**Where** - `src/helpers/anilist.py`: `_post` (~96-110) is the one
shared HTTP call every public function routes through
(`fetch_watch_progress` ~113-137, `fetch_next_episode` ~146-164, and one
more ~215). Catch `urllib.error.HTTPError` with `code == 403`
specifically in `_post` (or let it propagate a distinguishable
exception type) instead of blending into the general `except Exception`
in each caller.
**Done when** - a simulated 403 response (stub the socket layer, not a
live rate-limit) surfaces as a distinct condition to
`tracker._fetch_real_progress`, which is where the Sync Progress message
from items #9/#10 can then say "AniList looks rate-limited right now"
instead of "no real progress found."
**What happens when AniList says no** - already fails soft (returns
`None`) for a genuine no-match; this item only makes the *rate-limited*
case distinguishable from that, it doesn't add a fallback - there isn't
one for Crunchyroll (see #8).
**Landed** - `anilist.RateLimited`, raised by `_post` for 403 **and**
429 (the documented code and the one that actually arrives), propagated
out of `fetch_watch_progress` only - the schedule lookups stay soft,
because a missing countdown on a card has nowhere to say why and nobody
waiting on it. `tracker._fetch_real_progress` catches it and carries a
`reason` string on the `resolved` signal, which the Sync Progress dialog
turns into "AniList is refusing requests from this connection right now
... this says nothing about whether the title is on your list". That
`reason` field is the mechanism items #9 and #10 should extend rather
than reinvent. Verified with a stubbed 403 (raises `RateLimited`) and a
stubbed 404 (still fails soft, returns None).

### 8. Investigate a second source for Crunchyroll progress

**What** - Crunchyroll-provider progress sync has exactly one source
(AniList) and no fallback; when AniList is rate-limited or a title
simply isn't on a public AniList list, Crunchyroll progress cannot sync
at all. Netflix had the identical problem and was solved by moving to
Wikidata (`wikidata.py`, property P1874) as a keyless, public second
source - the question is whether an equivalent property exists for
Crunchyroll.
**Why now** - Diagnosed defect #7's second half. Not attempted yet
because it needs the same kind of research Netflix's fix needed
(finding and validating a Wikidata property, or another keyless
source) before any code gets written.
**Owner** - integrations-engineer
**Where to start** - `src/helpers/wikidata.py` (the Netflix pattern to
copy if a Crunchyroll property is found and holds up); check Wikidata
for a Crunchyroll-series-id property analogous to P1874, and validate
it the same way Netflix's was validated (does a real tracked title
resolve; is the id specific enough to not collide across a shared
label, the exact trap `wikidata.py`'s docstring already records for
Netflix).
**Done when** - either a working property is found, validated against
several real tracked titles, and implemented following the Netflix
shape (probe-before-save, confirmed-404-only-discards), or the
investigation concludes no usable property exists and that's recorded
here so it isn't re-investigated next month.
**What happens when the service says no** - if no such property exists,
this item ends in "not possible today," same honest-negative shape as
every other fail-soft path in this codebase - not a half-built partial
fix.
**Risk** - this is research first, implementation second; don't let the
research half get skipped in favor of guessing at a property id.

**Landed - and the item as written conflated two different things.**
Netflix's Wikidata fix solved *link resolution*, not *progress*. No
public knowledge base can hold personal watch history, so "a second
source for Crunchyroll **progress**" via Wikidata was never possible in
principle. Both halves were answered separately:

*The link (done).* **P11330 "Crunchyroll series ID" exists** and is
live - `crunchyroll.com/series/<id>`. P4110 is the older slug form and
Wikidata marks it deprecated; it is not used. Implemented as
`wikidata.fetch_crunchyroll_id` / `crunchyroll_page_url`, asked before
AniList exactly like Netflix, with `wikidata.py` generalized to one
`_fetch_id` rather than a Netflix copy (`page_url` is now
`netflix_page_url`). Coverage measured over six real titles: Frieren,
Jujutsu Kaisen, One Piece and Hunter x Hunter carry one; Vinland Saga
and Kaiju No. 8 don't and fall through to AniList as before. **End to
end with AniList stubbed to 403 on every request: 4 of 6 resolved,
where previously all 6 would have resolved to nothing** - Crunchyroll
had no second source at all. **Not probed before saving, unlike
Netflix**: Crunchyroll answers **200 to a deliberately bogus id**
(measured, `GZZZZZZZZ`), so a probe there proves only that the site is
up, and the strict Wikidata title match carries the weight instead. A
loose free-text P11330 value is rejected by shape.

*Correction, from item #14's measurement.* The "4 of 6" above is real
but was measured on headline titles. Against the owner's **actual**
three tracked titles the same lookup answers **0 of 3** - Wikidata
carries no Crunchyroll id for any of them (see item #14). The fallback
is still worth having and costs nothing when it misses, but it will
rarely fire on this library: AniList remains the practical source.

*A wrong-show bug this investigation found, and fixed.* Asking Wikidata
with `_query_variants`' subtitle-stripped head matched the **parent
franchise**: "Bleach: Thousand-Year Blood War" has no id of its own, so
the fallback asked for "Bleach", scored the 2004 series at 1.00, and
returned Crunchyroll `G63VGG2NY` and Netflix `70204957` - both the wrong
show, saved onto the entry permanently. **This affected Netflix too and
predates this month's work** (the Netflix path shipped in 1.3 with the
same loop). Both now ask with the full title only: a knowledge base's
labels are canonical titles, so the shorter string finds the franchise
rather than the season. Frieren still resolves on its full title, so
nothing was lost; Bleach TYBW now correctly returns nothing and falls
through to AniList and then to a search page.

*The progress (moved to item #17).* AniList remains the only source
wired up. The keyless candidate is **Kitsu** - its public JSON:API
answered without any key on both a user lookup and an anime search
during this investigation. MyAnimeList's v2 API **403s without a client
id** and is therefore out. That is a new integration plus a Settings
field, not a correctness fix, so it is item #17 rather than something
smuggled into this one.

### 9. Say so when Netflix/Crunchyroll watch progress can't be read

**What** - Neither Netflix nor Crunchyroll publishes watch history
without an authenticated session (no public API, login/ToS wall), so an
anime watched there never advances progress on its own. Today this is
invisible: an Anime entry sitting on Netflix/Crunchyroll with no
Stremio/AniList match just shows no progress, identical in appearance
to "not synced yet" or "nothing to show." The owner kept believing the
app itself was broken.
**Why now** - Named explicitly by the owner as a problem this month.
It's a correctness-adjacent usability gap: the app has complete
information (it knows the entry's site is Netflix or Crunchyroll) and
is choosing not to use it.
**Owner** - ui-engineer
**Where** - `src/windows/tracker.py`: the "no real progress found"
message at `_on_progress_synced` (~912-922, the single-entry Sync
Progress dialog) is the primary spot - branch it: if the entry's site
resolves to Netflix or Crunchyroll, say plainly that these sites don't
publish watch history and progress has to be set by hand, instead of
the generic "no real progress found" wording. Also add the same
distinction to the always-visible card state (`_progress_display`
~805-816, currently returns `""` whenever `progress_verified` is
false - indistinguishable from "not checked yet") and to the hover
tooltip (`_tooltip_html` ~690-699 /
`release_schedule.tooltip_lines`). To detect the site: `anime_sites.py`
has `_streaming_site_for(base_url)` (~721-729, currently private) and
the `_STREAMING_SITES` table (~711-718) with `"crunchyroll"`/`"netflix"`
keywords already defined - add a public wrapper (e.g.
`anime_sites.streaming_provider(site_id)`) rather than reaching into the
underscore-prefixed function from `tracker.py`.
**Done when** - an Anime entry on a Crunchyroll or Netflix site with no
verified progress shows a distinct, honest message (in the Sync
Progress dialog at minimum; card/tooltip if it fits without cluttering
the always-visible label) saying these sites can't be read
automatically - verified by building an entry with a Crunchyroll
`site_id` and no Stremio/AniList match, and confirming the message
differs from a plain "not synced" entry on an ordinary site.
**Risk** - none of this is network-dependent, so no "service says no"
case beyond what #7/#8 already cover - this item is purely about
surfacing information the app already has.
**Landed** - in three places, deliberately not on the card face.
`anime_sites.streaming_provider`/`streaming_provider_map` make the
private `_streaming_site_for` askable from a page; the Sync Progress
dialog now names the service and says progress there has to be set by
hand; the hover tooltip says the same, and only while progress is
unverified. **The card label was left alone on purpose** - a permanent
"can't be read" line under every Crunchyroll cover is clutter on cards
that are otherwise working, and the page-level notice from #10 covers
the same ground once instead of per card.

### 10. Surface a missing AniList username where it actually matters

**What** - The AniList username field in Settings is currently the
*only* place that says anything about it; leaving it blank silently
skips AniList sync with no indication anywhere else in the app that
this is why Anime progress isn't tracking. The owner's own username was
empty (destroyed by the storage.py bug in item #6) and nothing told
them this was the cause - they kept believing the app itself was
broken.
**Why now** - Named explicitly by the owner. Directly follows from #6:
fixing the data-loss bug prevents a recurrence, but doesn't help anyone
notice *this* time that it happened.
**Owner** - ui-engineer
**Where** - `src/helpers/settings_dialog.py` ~463-479 is the existing
(buried) hint text - leave it, but don't rely on it being found. Add
the missing-username case to the same places as item #9, since they
share the exact same code paths: `tracker.py`'s single-entry Sync
Progress message (~912-922) - for an Anime entry with no Stremio match
and no AniList username configured, say specifically "no AniList
username is set in Settings" rather than the generic "no real progress
found"; and consider a one-line note on the Anime tracker page itself
(e.g. near the refresh button, or as an empty-state note when the first
few Anime entries all have unverified progress) pointing at Settings.
Reuses `app_settings.get_anilist_username()` (already called at
`tracker.py` ~879) - the check just needs its own branch in the
messaging instead of being folded into the same "not found" bucket as
every other miss.
**Done when** - an Anime entry synced with no Stremio match and a blank
AniList username produces a message that names the blank username
specifically (not the generic message), and that message differs
visibly from the case where a username *is* set but the title genuinely
isn't on the list.
**Risk** - coordinate with item #9 if picked up separately - both touch
the same `_on_progress_synced` message-building code, and doing them in
two uncoordinated passes risks one overwriting the other's branch. Best
done by the same person in one pass, or explicitly sequenced.
**Landed** - done in one pass with #9, as that risk line suggested. The
several not-found causes are now separate `REASON_*` codes carried on
the `resolved` signal and turned into text by `_not_found_message`; a
blank username produces its own paragraph naming the setting, and an
entry that is *both* on an unreadable service and missing a username
says both rather than picking one. Above that sits a page-level notice
under the sort row (`_update_sync_notice`), recomputed on every redraw
so filling the username in from Settings clears it without a restart -
that notice is the part that answers "why is nothing happening?" without
the user having to sync an entry first to find out.

### 11. Search and filter on tracker pages

**What** - Anime/Reading/Series pages have no way to search or filter -
sorting only changes order, not what's shown. A long list has no way to
jump to one title.
**Why now** - Named usability gap in `planning.md`; grows more painful
the larger a tracked list gets, and none of this month's other items
touch it, so it's a clean independent slice of value.
**Owner** - ui-engineer
**Where** - `src/windows/tracker.py`: `_visible_entries` (~662-671, the
existing sort dropdown lives here) and `_sections`/`_refresh_grid`
(~702-744, where the grid is actually built) are the two places a
filter has to plug into - a search box narrowing `_visible_entries`'
output before it's grouped into sections is the natural shape, since
`_sections` already re-groups whatever list it's given.
**Done when** - typing in a new search field narrows the visible grid to
matching titles (case-insensitive substring match, minimum) across all
three tracker pages (Anime/Reading/Series share this same base), and
clearing it restores the full list without needing a page revisit.
**Risk** - `_sections` groups by status and the drag-to-reorder Custom
Order path saves whatever order is on screen (`_begin_custom_order`,
per the comment at ~706-709) - make sure an active filter doesn't let a
drag-reorder silently drop the hidden entries' saved order. Worth an
explicit check: filter active, reorder, clear filter, confirm nothing
moved unexpectedly.
**Landed** - search box in the sort row, narrowing `_visible_entries`
before `_sections` groups it, so all three pages get it from the shared
base. Debounced at 150ms because every redraw rebuilds every card from
scratch. **The reorder risk was resolved by removing the combination
rather than reconciling it**: cards are not draggable while a search is
active (`_build_card` skips attaching the handler) and the hint text
says why. Reconciling a partial on-screen order with the saved one is
solvable but it is the kind of subtlety that ships as a data bug, and
"clear the search to reorder" costs the user nothing.

### 12. Show whether an added site will resolve to title pages or only ever fall back to search

**What** - Adding a Video/Reading site gives no signal about whether it
will actually resolve to per-title pages or just fall back to a search
link every time - the user finds out only by trying it later.
**Why now** - Named usability gap in `planning.md`. Directly related to
item #4 (the 24s dead-host cost) - together they're the difference
between "adding a site is informative" and "adding a site is a 24-second
guess."
**Owner** - integrations-engineer (determining resolution capability
needs the actual engine-detection logic; a UI-only badge without real
detection behind it would be worse than nothing - it would assert
something unverified).
**Where** - `src/helpers/anime_sites.py` and `src/helpers/manga_sites.py`
own the per-engine detection (the 3 engines + generic fallback
`resolve_page_url` touches, see item #4). The site-adding UI lives in
`src/windows/tracker.py` around `_populate_site_options`/site dialog
(~1400+) and the equivalent in `websites.py`/`link_grid.py` for the
generic site list - check which of those actually own "add a site" for
Video Websites specifically before starting.
**Done when** - adding a site runs a one-time resolution probe against
a known title (or the site's own homepage/search shape) and records
whether it hit a real engine or fell through to generic-search-only,
surfaced as a label/icon in the site list; a site that later stops
resolving (host changed) isn't required to re-flag itself automatically
- only the at-add-time signal is in scope this month.
**Risk** - depends on #4 landing first (or at least being sized) so the
probe itself doesn't cost another 24s per site added; sequence this
after #4.
**Landed** - `probe_site` in both `anime_sites` and `manga_sites`
searches the site for a title every catalogue carries ("One Piece") and
reports `engine` / `generic` / `search-only` / `unreachable` /
`streaming`; Settings stores it on the site and shows it in the list
("opens title pages", "search links only - no title pages", "didn't
answer when checked"). Runs on add, on edit (the URL may be what
changed), and from a new **Check** button so sites added before this
existed can be checked too. Verified against local servers of each
shape: engine, search-only and unreachable all classified correctly,
four probes in 10.3s total. Depended on #4 as predicted - and needed the
same deadline in `manga_sites.search_site`, which didn't have one, so
`net.step_timeout` is now shared by both.

### 13. Quick +1 for Anime/Series progress

**What** - Manga/Reading entries already have +/-1 chapter buttons
directly on the card (`_bump_watched_chapter`); Anime/Series has no
equivalent - bumping episode progress by hand requires opening the full
Edit dialog.
**Why now** - Named usability gap in `planning.md`. The Manga pattern
already exists and works; this is extending a proven shape to the other
two entry types, not inventing one.
**Owner** - ui-engineer
**Where** - `src/windows/tracker.py`: `_build_card` (~745-796) currently
gates the +/-1 control row behind `if entry["type"] in MANGA_TYPES:`
(~777); `_bump_watched_chapter` (~798-803) is the pattern to mirror for
episode progress, writing to `entry["progress"]` via
`format_episode_progress`/`parse_episode_progress` instead of
`last_watched_chapter`.
**Done when** - an Anime/Series card has a working +1 control that bumps
the episode number, saves via `storage.update_entry` (never a whole-list
write - see `.claude/rules/ui.md`), and sets `progress_verified`
correctly (a hand-bumped number is the user's own input, same trust
level as typing it into Edit today - check what Edit currently sets
`progress_verified` to and match it, don't invent a new rule here).
**Risk** - Anime/Series progress is season+episode, not a flat number
like manga chapters - +1 needs to roll over correctly at a season
boundary using the existing `format_episode_progress`/
`parse_episode_progress` helpers rather than naive integer increment.
**Landed** - `_bump_episode`, sharing one `_bump_controls` builder with
the Manga path so both rows stay identical. It sets `progress_verified`,
matching what a hand edit in the form does - without that the card would
still show nothing after being clicked. **It deliberately does not roll
a season over**, contrary to the risk line above: nothing in the app
knows how many episodes a season has (`latest_available` is the newest
episode *out*, not a season length), so a rollover would be a guess that
silently files progress under the wrong season. The season is changed in
Edit, where it is typed. A freeform legacy note is left untouched rather
than overwritten, and -1 stops at zero.

### 14. Investigate Amazon Prime coverage before building it

**What** - Whether Amazon Prime can be added as a tracked source the
same way Netflix was, via Wikidata.
**Why now** - Requested capability, but `planning.md` already flags the
open question: Wikidata property P8055 ("Amazon Prime Video ID", US)
works in isolation (*The Boys* → `B07QQQ52B3`), and a newer P14440
exists too, but anime coverage looks thin - Hunter x Hunter and Vinland
Saga both carry a Netflix id (P1874) and neither carries a Prime id.
Building the feature before knowing coverage risks shipping something
that resolves for almost nothing.
**Owner** - integrations-engineer
**Where to start** - `src/helpers/wikidata.py` is the pattern to copy if
coverage holds up (same probe-before-save shape Netflix uses,
`_netflix_available`/`_netflix_page_url` in `anime_sites.py` ~755-806).
Before writing any of that: query P8055 and P14440 for a real sample of
the owner's actually-tracked Anime entries (not a cherry-picked few),
and record the hit rate.
**Done when** - either the measured hit rate across real tracked titles
is high enough to justify building it (implemented following the
Netflix shape, added as a row-equivalent to how Netflix/Crunchyroll are
special-cased in `anime_sites.py`), or the investigation concludes
coverage is too thin and that conclusion is recorded here so it isn't
re-investigated next month without new evidence.
**What happens when the service says no** - no P8055/P14440 recorded
for a title falls back exactly like Netflix's missing-P1874 case
already does: search-page fallback, not an error.
**Risk** - this is explicitly investigate-before-build per
`planning.md`; don't let "P8055 works for one title I tried" stand in
for measuring the real sample.

**Landed - measured, and the answer is no. Not building it.**

Measured against the owner's *actual* tracked titles (read from a copy
of `%APPDATA%\Atomic`, never the live data). Three Anime/Series entries:
*The Angel Next Door Spoils Me Rotten*, *Bleach: Thousand-Year Blood
War*, *Wistoria: Wand and Sword*.

| Property | Service | Hits |
|---|---|---|
| P8055 | Prime (US) | **0 / 3** |
| P14440 | Prime | **0 / 3** |
| P1874 | Netflix | 0 / 3 |
| P11330 | Crunchyroll | 0 / 3 |

The entities exist on Wikidata with exact 1.00 label matches - they
simply carry **no streaming ids of any kind**. This is not a Prime
problem, it is a coverage cliff: Wikidata's streaming ids are on older
and headline titles (*Bleach* 2004 has both Crunchyroll and Netflix ids;
*Bleach: Thousand-Year Blood War* 2022 has neither), and a library of
current seasonal anime falls entirely outside it. Building Prime on top
of P8055 would resolve nothing for this user, and there is no second
public source for Prime ids the way AniList backs Netflix.

**Revisit only with new evidence** - a materially larger tracked library
whose titles do carry these ids, or Wikidata coverage visibly improving.
"Prime would be nice" is not new evidence.

### 15. Investigate startup and page-rebuild performance

**What** - No performance measurement exists yet beyond the
already-diagnosed threading defects (#3/#4/#5 above, which are
correctness bugs with a performance symptom, not a general profile).
This item is the general question: where does Atomic actually spend
time on startup and on a tracker page visit, and is any of it worth
fixing beyond what's already queued.
**Why now** - Requested this month ("app efficiency/performance") but
has no diagnosed cause yet - unlike everything above it, this is
genuinely unknown shape. Pages intentionally rebuild from scratch on
every visit (`.claude/rules/ui.md` - "never keep state in a widget that
must survive"), so a chunk of any measured cost may be by design, not a
bug; profiling has to happen before anything gets changed so a
deliberate design choice doesn't get "fixed" into a data-loss bug like
the ones `ui.md` already warns about.
**Owner** - test-engineer (measurement first, per
`.claude/rules/testing.md` - "a classifier you haven't validated is not
a measurement"; hand findings to ui-engineer or integrations-engineer
once a real cause is identified).
**Where to start** - cold-start time to first paint (`main.py`
`main()`), and per-visit cost of `tracker.py`'s `_refresh_grid` /
`_build_card` on a large real-sized entry list (image
loading/`images.py` is a candidate - it's already on the unbounded-read
list at item #3).
**Done when** - a measured breakdown exists (startup: N seconds to
first paint; tracker page visit: N ms to rebuild for a list of realistic
size) with the top 1-2 costs identified by name, not guessed. Whether
anything gets *fixed* this month depends on what's found - this item's
"done" is the measurement and a follow-up item (or explicit "not worth
fixing") for whatever it finds, not a performance improvement itself.
**Risk** - the real risk is skipping measurement and guessing - this
codebase has already shipped a pixel-classifier that reported every
frame broken in both the broken and fixed case because it wasn't
validated first; don't repeat that shape here with an unvalidated
timing method.

**Landed - measured. There is no performance problem to fix.**

Cold start of the frozen exe, launch to a visible window, three runs
with `%APPDATA%` redirected to a copy: **1.42s / 1.29s / 1.57s**. In
process, the phases behind that are app-module imports 137ms, PyQt6
import 23ms, `MainWindow()` 32ms, `apply_theme` under 1ms - so most of
the wall clock is PyInstaller's onefile unpack and Qt's own
initialisation, neither of which is application code.

Tracker page redraw, which was the other suspect:

| Entries | First build | Redraw | Per card |
|---|---|---|---|
| 10 | 39ms | 4ms | 0.4ms |
| 50 | 58ms | 17ms | 0.3ms |
| 200 | 197ms | 66ms | 0.3ms |
| 500 | 412ms | 164ms | 0.3ms |

Linear, and 500 entries is over a hundred times this library. **Covers
were the named suspect and are not the cost**: repeating the run with a
real 800x1200 PNG on every card moved it from 0.3 to 0.4ms per card
(200 entries: 66ms → 77ms), because scaled pixmaps are already cached.

**No follow-up item.** The only lever that would move the 1.3s is a
onedir build instead of onefile, which trades away the single-file
distribution the whole update mechanism is built on - a bad trade for
about a second. Rebuilding pages from scratch on every visit stays as
it is; `.claude/rules/ui.md` requires it, and it costs 4ms at this
library's size.

### 16. Investigate code signing to stop the antivirus false positive

**What** - The release build has been flagged by Microsoft Defender as
`Trojan:Win32/Wacatac.B!ml`, proven a false positive by bisection (1.0
through 1.1.5 scanned clean; 1.2 - differing by exactly one line, the
version string - was flagged). Unsigned executables with no download
reputation are what these heuristics are tuned to distrust. Diagnosed
defect #8; durable fix is a code-signing certificate.
**Why now** - Lowest-cost thing that can happen this month on this item
is pricing it out, since a certificate is a real purchase the agent
cannot make on its own (see `CLAUDE.md`'s purchase-permission rule) -
put the decision in front of the user with real numbers instead of
leaving it as a permanently-known-but-untouched defect.
**Owner** - release-engineer
**Where to start** - research code-signing certificate options
(standard vs. EV, typical first-year cost, issuance turnaround, and
whether Microsoft's SmartScreen reputation-building applies
differently to each) and how the signing step would fit into the
existing `build`/`release` skills' pipeline.
**Done when** - a short comparison (options, cost, what changes in the
release process) is written up and put in front of the user as a
decision - not purchased, not implemented. If the user approves a
specific option, that becomes next month's implementation item.
**Risk** - none from investigating; the risk is entirely in the
purchase step, which stays gated behind explicit user approval per the
standing purchase-permission rule regardless of what this investigation
finds.

**Landed - researched. Nothing bought; this is a decision for the user.**

The landscape moved since this defect was first written down, and it
moved in our favour:

| Option | Cost | Hardware | Notes |
|---|---|---|---|
| **Azure Artifact Signing** (was Trusted Signing) | **$9.99/mo** Basic, 5,000 signatures | none - cloud HSM | Open to *individual* developers after identity validation; GA in US/Canada/Europe |
| EV certificate (Sectigo/SSL.com/DigiCert) | ~$249-325/yr | USB token or HSM **mandatory** | 1-year terms as of Feb 2026 |
| OV certificate | ~$200-300/yr | same hardware rule | |

**The reason to buy EV specifically is gone.** Since March 2024 EV no
longer grants an instant SmartScreen pass; EV and OV now build
reputation the same way, by download volume. Paying triple for the token
ceremony buys nothing this project needs.

**Recommendation: Azure Artifact Signing, ~$120/year.** Cheapest, no
hardware token to plug in before a release, and it fits the existing
`build`/`release` pipeline as a signing step rather than a manual one.

**Two things to confirm before committing**, neither of which an agent
can settle: whether the owner's identity validation passes (individuals
need verifiable history), and whether it is offered in the owner's
region - GA is listed for US/Canada/Europe.

**Honest caveat**: signing makes a heuristic flag much less likely and
gives a real identity to appeal with, but no signature *guarantees*
Defender never flags a build again. The bisection already proved 1.2's
flag was a false positive on an unsigned, reputation-less binary; that
is the condition signing removes.

### 17. Investigate Kitsu as a second source for watch progress

**What** - A second place a user's own episode progress can be read
from, so AniList is not the only one. Fell out of item #8: watch history
is personal data, so no knowledge base can supply it - only another list
service the user keeps.
**Why now** - AniList is currently a single point of failure for every
Anime entry's progress, and it fails *silently* for an hour at a time
(item #7 now names that, but naming it doesn't sync anything). Kitsu is
the only keyless candidate found: its public JSON:API answered a user
lookup and an anime search with no key during item #8's research.
MyAnimeList's v2 API 403s without a client id, so it is out.
**Owner** - integrations-engineer
**Where to start** - `helpers/anilist.py` `fetch_watch_progress` is the
shape to match (username in, `(season, episode)` out, fails soft).
Kitsu needs `users?filter[name]=` then that user's `libraryEntries` with
the anime included; confirm a *public* profile is readable without auth
before anything else, since that is the whole premise.
**Done when** - either progress for a real title reads back from a
public Kitsu profile and is wired in as a fallback beside AniList (with
a username field in Settings, which makes item #10's "no username set"
messaging cover two services, not one), or the investigation records
that public profiles aren't readable without auth and that's the end of
it.
**What happens when the service says no** - it is a *fallback*: no Kitsu
username, no match, or a refusal all fall through to exactly today's
behaviour. It must never replace AniList as the primary.
**Risk** - two sources can disagree about the same title. Decide the
rule before writing the code (highest progress wins is the obvious one)
rather than discovering it as a bug later.

### 18. Read Crunchyroll progress from Crunchyroll itself

**What** - Two faults the owner hit on one card. One-Punch Man showed
S01E07 while Crunchyroll's own history said E2, because Stremio was
asked first for *every* entry and whatever it answered won - for an
entry watched on Crunchyroll that is a different viewing entirely. And
the only Anime source besides Stremio was AniList, which knows only what
some other tracker wrote to it.
**Why now** - Reported directly, with the evidence: crunchyroll.com/
history showing E2 next to a card claiming E7. A confidently wrong
number is worse than a blank one, and nothing on the card said where it
came from.
**Owner** - integrations-engineer
**Where** - `src/helpers/crunchyroll.py` (new), `app_settings`,
`settings_dialog` (Crunchyroll Account section), `tracker.
_fetch_real_progress`.
**Done when** - a Crunchyroll entry reads Crunchyroll's own number, and
never Stremio's.
**What happens when the service says no** - Crunchyroll → AniList →
nothing. Never Stremio for an entry watched elsewhere, which was the
whole defect.

**Landed, with one part that could not be tested here.**

Source order is now decided by where the entry is actually watched.
Crunchyroll entries ask Crunchyroll (signed in), then AniList; ordinary
entries still ask Stremio first, unchanged. Every synced number now
carries the source that gave it (`progress_source`), shown on hover -
"Progress from Crunchyroll" / "from AniList" / "set by you" - because an
episode number the user disagrees with is unarguable while it is
anonymous.

Sign-in follows `stremio.login`: password sent once, **only the refresh
token stored**. One shared history fetch serves a page of cards.
Verified against a local stand-in speaking Crunchyroll's response shapes
- with Stremio saying E7 and AniList saying E4, the entry reads **S01E02
from Crunchyroll**; 14/14 checks including that the password never
reaches settings.json.

**The email/password sign-in was built, then measured dead, then
replaced by a pasted token.** The password grant needs a client
credential Crunchyroll issues to no third party. The published one (from
the archived `crunchyroll-go`) was tested without any account - a login
with deliberately fake details separates `invalid_grant` (credential
alive, login wrong) from `client_inactive` (credential dead) - and
**both `www.crunchyroll.com` and `beta-api.crunchyroll.com` answered
`auth.obtain_access_token.client_inactive`**. No password can work
through it, for anyone.

So Settings now takes **the token from the browser session the user is
already signed into**, with five numbered steps in the dialog. That
needs no client credential and no password at all. `login`/`refresh`
remain in the module, unreachable from the UI, and start working if a
live credential is ever put in settings.json.

**Still untested: a real Crunchyroll token**, which needs the owner's
own browser. Everything up to that boundary is verified against a local
stand-in speaking Crunchyroll's shapes, including that an expired token
says to paste a fresh one rather than looking like "nothing watched".

**Known cost, accepted by the owner**: using Crunchyroll's internal API
is against their terms of service, and it will break whenever they
change it. The MAL-Sync route (Crunchyroll → AniList, then AniList as
today) was offered as the durable alternative and declined.

### 19. Crunchyroll through MAL-Sync, and AniList read per season

**What** - Two things, found by the owner actually using #18. Reading
Crunchyroll directly could not be made to last: the password grant is
impossible and a browser token dies inside an hour, so the section
promised an account and delivered something that stopped answering
silently. And once progress *did* arrive from AniList, a One-Punch Man
card sat at E12 while its owner was part-way through season 2.
**Why now** - The second one is the more important bug of the two, and
it was invisible until the first was out of the way. AniList files each
season as its own work and counts episodes from 1 within it; Crunchyroll
and this app's cards use one title with a season number. Asking AniList
about "One-Punch Man" therefore answers about season 1 forever.
**Owner** - integrations-engineer
**Where** - `helpers/anilist.py` (`fetch_season_progress`),
`helpers/settings_dialog.py`, `helpers/crunchyroll.py` (deleted).
**Done when** - a franchise reports the season you are on, and Settings
explains the MAL-Sync route step by step.
**What happens when the service says no** - unchanged: AniList → nothing,
and the +1 button on the card is always there.

**Landed.**

*Direct Crunchyroll reading removed entirely* - `crunchyroll.py`, its
settings section, its stored token and its diagnostic are gone. The
rules file records the three dead ends so it isn't rebuilt a third time.

*Settings > Crunchyroll Progress* now carries six steps matching
MAL-Sync's real install flow, including the two that are easy to get
wrong: tick **AniList** (not MyAnimeList - Atomic can't read MAL), and
it only saves after **85%** of an episode.

*AniList is read per season.* `fetch_season_progress` collects a
franchise's seasons, prefers the season number written in the title over
release order, and reports the furthest one actually started - so season
1 finished plus season 2 at episode 5 reads **S02E05**. A single-season
show keeps the flat `E12` shape.

Two traps found by querying the live API rather than reasoning about it:
**a 1-episode short (*Go! Saitama*) carried the franchise name only as a
synonym** and took season 3's slot, pushing the real season 3 to 4 - so
matching uses an entry's own titles and never its synonyms. And **a cour
split ("Season 3 Part 2") is still season 3**, which is why the season
regex deliberately doesn't match "Part". 12/12 verified, including a
live query confirming the four real One-Punch Man entries in order.

**Known limit**: a franchise that numbers nothing in its titles falls
back to release order, which is only as good as AniList's dates.

---

## Deliberately not doing

- **Not touching `manga_sites.py`/`anime_sites.py`/etc. beyond the
  listed defects.** Both modules have working engines for their known
  site shapes; this plan bounds the failure modes, it doesn't refactor
  the scraping logic itself.
- **Not building an authenticated Crunchyroll client.** Already
  researched and rejected per `.claude/rules/integrations.md`: dead
  password-login flow, ToS-prohibited scraping of a paid service, and
  redundant with what `anilist.py` already covers. Item #8 looks for a
  *keyless* second source instead - not this.
- **Not adding new streaming services beyond Amazon Prime this month.**
  Prime is the one the owner already flagged as worth investigating;
  anything past it (Hulu, Disney+, etc.) is a new investigation with its
  own coverage question, not assumed to follow the same pattern.
- **Not implementing Amazon Prime - now measured, not merely deferred**
  (item #14). 0 of 3 real tracked titles carry P8055 or P14440, and 0 of
  3 carry a Netflix or Crunchyroll id either. The feature would resolve
  nothing for this library. Revisit only on new evidence.
- **Not purchasing a code-signing certificate** (item #16 is research
  and a recommendation only) - a purchase needs the user's explicit
  approval per the standing purchase-permission rule, and pricing has
  to exist before that approval can be meaningfully asked for.
- **Not re-litigating what "done" means for AniList/Stremio failing
  soft in general** - the fail-soft behavior itself is correct and
  deliberate (`.claude/rules/integrations.md`); items #7/#9/#10 only
  make specific already-silent cases *say why*, they don't change
  anything about when a lookup gives up.
- **Not chasing general performance work beyond item #15's
  measurement** - without a profile, "make it faster" has no target;
  next month's plan can size real fixes once #15 reports what's
  actually slow.
- **Not touching `home.py`, `games.py`, `apps.py`, `link_grid.py`,
  `websites.py` this month** - no diagnosed defect or named gap points
  at them; pulling them in would be scope without a stated cost, which
  `.claude/rules/planning.md` rules out ("an item whose cost can't be
  stated doesn't belong").
- **Not releasing.** Everything above lands on `development` as normal
  patch bumps per `CLAUDE.md` rule 4; nothing here is a release
  decision, and no item should be read as one.

## Could not settle

- **Item #16's actual dollar cost** - genuinely requires live research
  (current certificate vendor pricing) that wasn't done as part of
  writing this plan; the item is scoped as the investigation itself for
  that reason.
- **Whether items #9 and #10 should ship as one combined change or
  two** - they touch the identical code (`tracker.py`'s
  `_on_progress_synced` message block) and are almost certainly cheaper
  done together in one pass than sequenced; left both items in place
  with an explicit cross-reference rather than merging them, since the
  owner named them as two separate points and a future reader should be
  able to tell both are covered.
