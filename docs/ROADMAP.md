# Roadmap

Written from released version **1.3** on `development`. Covers roughly
the next month of work. Format and update rules: `.claude/skills/roadmap`.
Ordering rules and the standing facts this plan draws on (8 diagnosed
defects, usability gaps, the Amazon Prime coverage note):
`.claude/rules/planning.md`.

## Index

| # | Item | Owner | Size | Status |
|---|---|---|---|---|
| 1 | Survive an exception in a Qt slot, and log something | ui-engineer | spans modules | todo |
| 2 | Bound manga_sites' regex and reads | integrations-engineer | contained | todo |
| 3 | Bound `resp.read()` on every page-load lookup | integrations-engineer | spans modules | todo |
| 4 | Give a dead host a deadline across the whole resolve chain | integrations-engineer | contained | todo |
| 5 | Route video-site resolution through `lookup_pool` | integrations-engineer | contained | todo |
| 6 | Back up settings/entries before an overwrite, and stop swallowing a corrupt file silently | ui-engineer | spans modules | todo |
| 7 | Tell AniList's rate-limit apart from "no result" | integrations-engineer | contained | todo |
| 8 | Investigate a second source for Crunchyroll progress | integrations-engineer | shape unknown - investigate first | todo |
| 9 | Say so when Netflix/Crunchyroll watch progress can't be read | ui-engineer | spans modules | todo |
| 10 | Surface a missing AniList username where it actually matters | ui-engineer | spans modules | todo |
| 11 | Search and filter on tracker pages | ui-engineer | spans modules | todo |
| 12 | Show whether an added site will resolve to title pages or only ever fall back to search | integrations-engineer | spans modules | todo |
| 13 | Quick +1 for Anime/Series progress | ui-engineer | contained | todo |
| 14 | Investigate Amazon Prime coverage before building it | integrations-engineer | shape unknown - investigate first | todo |
| 15 | Investigate startup and page-rebuild performance | test-engineer | shape unknown - investigate first | todo |
| 16 | Investigate code signing to stop the antivirus false positive | release-engineer | shape unknown - investigate first | todo |

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
- **Not implementing Amazon Prime this month, only investigating it**
  (item #14) - coverage is unmeasured and `planning.md` is explicit that
  measuring comes before building.
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
