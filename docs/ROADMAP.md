# Roadmap

Written from released version **1.4** on `development` (re-cut 2026-08-16,
`development` at `50ef1c5`). Covers roughly the next month of work.
Format and update rules: `.claude/skills/roadmap`. Ordering rules and
the standing facts this plan draws on (already-fixed defects 1-8,
usability gaps, the Amazon Prime/performance/code-signing verdicts):
`.claude/rules/planning.md`.

This is a full replan, not a continuation. The pre-1.4 roadmap's items
1-20 are gone from this file, not carried forward as a block: 1-16 and
18-20 landed (several differently than planned - the correctness pass,
the Crunchyroll saga, the "Stremio only" decision); 17 (Kitsu as a
second progress source) is **superseded**, not merely stale - 1.4's own
decision was "one source, deliberately," made after four alternatives
were tried and each shipped a silently-wrong number. Proposing a fifth
source would contradict that decision rather than complete it. See
*Deliberately not doing* at the bottom.

Every item below was found by reading 1.4's actual code, not by
re-deriving the old list. `docs/VDD-1.4.md` records what 1.4 shipped in
prose; this file is only the open work.

`docs/ROADMAP.md` itself is development-only from now on and is being
kept off `main` by the user directly - not an item here.

**Revised by the owner, 16 August 2026.** Items **9** (code signing) and
**25** (cap the on-disk cover cache) were struck out; both are recorded
under *Deliberately not doing* so they aren't re-proposed. Items **27**
and **28** are new, from the owner's own notes, and #17 and #24 gained
requirements from the same notes. **Numbers are never reused and never
renumbered** - every "item #N" reference in this file, in commits, and
in agent handoffs keeps meaning the same thing, which is worth more than
a gapless list. Work order is the index table's order, not the numbers'.

The owner's standing note, which outranks any single item here: *"we
have a good stable app now - be more careful of adding, removing and
changing in the app backend."* Prefer the contained change over the
clever one, and leave a working path working.

## Index

| # | Item | Owner | Size | Status |
|---|---|---|---|---|
| 1 | Tell a dead/expired Stremio session apart from "nothing to sync" | integrations-engineer | contained | todo |
| 2 | Stop Apps/Websites saving a stale whole list on every change | ui-engineer | contained | todo |
| 3 | Tie the released exe to the source tree it was built from | release-engineer | spans modules | todo |
| 4 | Write 1.4's missing "what's new" notes | ui-engineer | contained | todo |
| 5 | Make a future release unable to ship without its notes | release-engineer | contained | todo |
| 6 | Stop the sidebar Add menu positioning with `mapToGlobal` | ui-engineer | contained | todo |
| 7 | Route entry-search suggestions through `lookup_pool` | integrations-engineer | contained | todo |
| 8 | Retire or rebuild `diagnose_anilist.py` - it calls functions that no longer exist | integrations-engineer | contained | todo |
| 10 | Show a Stremio connection that has gone bad, in Settings itself | ui-engineer | contained | todo |
| 27 | Say what a site's check verdict actually means | ui-engineer | contained | todo - **owner-raised** |
| 28 | Clear site check verdicts when the app restarts | ui-engineer | contained | todo - **owner-raised** |
| 29 | Let the "what's new" dialog scroll | ui-engineer | contained | todo - found landing #4 |
| 30 | Stop a two-line card name being clipped on Apps/Websites | ui-engineer | contained | todo - found landing #2 |
| 11 | Remember a tracker page's search/filter across a revisit, for the session | ui-engineer | contained | todo |
| 12 | Bring search to Games, Apps and Websites | ui-engineer | contained | todo |
| 13 | Check for updates in the background, not only on demand | ui-engineer | contained | todo |
| 14 | Remember window size and position across launches | ui-engineer | contained | todo |
| 15 | Give Settings some structure before it grows further | ui-engineer | shape unknown - investigate first | todo |
| 16 | Flag an App/Website entry whose target has disappeared | ui-engineer | contained | todo |
| 17 | Keyboard shortcut to jump to a page's search box | ui-engineer | contained | todo |
| 18 | Bring Apps to parity with Games' "Import from Launchers" | ui-engineer | spans modules | todo |
| 19 | Check every configured site at once, not one at a time | ui-engineer | contained | todo |
| 20 | Back up all tracked data from Settings | ui-engineer | contained | todo |
| 21 | Restore tracked data from a backup archive | ui-engineer | spans modules | todo |
| 22 | Multi-select bulk status change on tracker cards | ui-engineer | shape unknown - investigate first | todo |
| 23 | Undo the last removal, via a toast | ui-engineer | spans modules | todo |
| 24 | One search across every page, not just the tracker family | ui-engineer | shape unknown - investigate first | todo |
| 26 | Audit what PyInstaller actually bundles into `Atomic.exe` | release-engineer | shape unknown - investigate first | todo |

Ordered correctness-first, worst blast-radius first: a confidently
wrong number (#1) outranks a data-loss *pattern* not yet triggered (#2),
which outranks a release-integrity gap already triggered once (#3),
which outranks two release-communication gaps (#4, #5), a cosmetic
positioning bug (#6), an internal consistency gap (#7), and dead dev
tooling (#8) - all real, in descending severity. Usability (#10, #27,
#28, #11-19), features (#20-24) and optimization (#26) follow, each
internally ordered by how directly it follows from something already in
the app versus how much shape it still needs. The two owner-raised items
sit at the front of the usability block: they are the only ones on this
list reported from actually using the app rather than from reading it.

---

### 1. Tell a dead/expired Stremio session apart from "nothing to sync"

**What** - Since 1.4, Stremio is the *only* watch-progress source
(`tracker._fetch_real_progress`, `.claude/rules/integrations.md`: "this
is settled"). If the stored `auth_key` is ever revoked or expires -
password change, "log out everywhere" on Stremio, anything -
`stremio.fetch_watch_progress` raises, and the catch at
`tracker.py:1253` (`except Exception: result = None`) makes that look
exactly like "not in your library yet." Every entry would quietly stop
syncing, forever, with nothing on screen saying why.
**Why now** - This is the identical shape of defect already fixed for
AniList (`anilist.RateLimited`, roadmap item #7 in the pre-1.4 plan) -
except AniList back then was one of several sources, and Stremio today
is the *only* one. The project's own stated failure mode ("a source
that is silently wrong is worse than no source... a card once showed
episode 7 for a show its owner had watched two episodes of," VDD-1.4
§5) is exactly what an unnoticed auth failure reproduces, just via
silence instead of a wrong number - nothing syncs and nothing says why,
so the owner has no way to tell "not on Stremio yet" from "your sign-in
broke three weeks ago."
**Owner** - integrations-engineer
**Where** - `src/helpers/stremio.py`: `fetch_watch_progress` (~141-178)
wraps `_api_post` in a bare `except Exception: return None` (~155-156).
`src/windows/tracker.py`: `_fetch_real_progress` (~1235-1265) catches
again the same way (~1253-1254) and only ever appends
`REASON_NO_STREMIO_ACCOUNT` (~1256) - there is no reason code for "have
an account, but it stopped answering." Follow the `anilist.RateLimited`
shape (`src/helpers/anilist.py` ~84-121): raise a distinguishable
exception for an auth-shaped failure (401/403 from `_api_post`) instead
of swallowing everything, and add a `REASON_STREMIO_AUTH_*` the
existing `reason` plumbing on the `resolved` signal already carries.
**Done when** - a stubbed 401/403 from Stremio's account API surfaces
through `_on_progress_synced`/`_not_found_message` as its own message
("your Stremio sign-in needs refreshing"), distinct from both "not on
your list yet" and a plain network failure; a stubbed genuine 404/empty
library still reads as "nothing to sync" unchanged.
**What happens when the service says no** - unchanged for every other
failure shape (no account, title not found, network down): still fails
soft to "nothing to sync." Only the auth-specific case gets a new,
distinguishable message - this does not add a fallback source, because
there isn't one and the project has explicitly rejected adding one.
**Risk** - don't let this drift into "add a second progress source" -
that question is closed (see *Deliberately not doing*). The fix is
purely making one specific failure mode of the one existing source
speak instead of going quiet.
**Landed** (1.4.3) - and the item's premise was wrong in a way worth
recording: `api.strem.io` does **not** raise or answer 401 on a dead
key. It answers **HTTP 200 with `{"error": {...}}`** (confirmed against
Stremio's own `stremio-core` `APIResult::Ok|Err` and their JS client,
which tests `resp.status !== 200` and `body.error` separately), so the
old code never raised at all - it fell through to "no items" and
returned `None`. A fix that only converted 401/403, as written above,
would have shipped and never fired once. Both shapes are handled;
`stremio.AuthFailed` + `tracker.REASON_STREMIO_AUTH_FAILED`. Auth is
matched on message keywords because Stremio publishes no error-code
list and neither of their own clients branches on one; an unrecognised
error still fails soft, since a false "your sign-in is broken" is the
same bug pointing the other way. The exact message string is inferred,
not measured - a real key could not be revoked to see it. Scope grew
by one deliberate step: the "done when" covered only the right-click
path, but the failure actually happens on the silent arrival sync and
the page-load backfill, which had no dialog and no toast at all, so
those now raise one toast per app run.

### 2. Stop Apps/Websites saving a stale whole list on every change

**What** - `ui.md`'s standing rule, written after a real incident
("reordering a game erased freshly imported games"), is: save one
entry with `storage.update_entry(file, id, fields)`, never the whole
list back from a page, because another page or a background job can
hold a snapshot that's gone stale by the time this page's copy is
written back. `games.py` was fixed to this shape (`_mutate` reloads
from disk immediately before applying and saving, at
`src/windows/games.py:157-160`; single-field bumps use
`storage.update_entry`, e.g. `games.py:280`, `288`). `link_grid.py` -
the shared base for **both** the Apps and Websites pages - was never
migrated: every one of its four save sites still writes
`storage.save(self.DATA_FILE, self.entries)` off the in-memory list
loaded once in `__init__`.
**Why now** - This is not a hypothetical parallel to the fixed defect,
it's the same defect, unfixed in a sibling file. `main.py`'s own
`refresh_current_page` docstring (~434+) already names the exact race -
"Settings > Clear Data, wiping a category out from under a page that's
already open behind the dialog" - as something this app has to guard
against. `link_grid.py` doesn't: open Websites, open Settings behind it
and clear/import something touching `websites.json`, then remove one
entry on the still-open Websites page - the removal's whole-list save
overwrites whatever changed in the meantime.
**Owner** - ui-engineer
**Where** - `src/windows/link_grid.py`: `storage.save(self.DATA_FILE,
self.entries)` at line 101 (post-migration write), 245 (`_open_entry`'s
`last_used` bump), 252 (`_remove_entry`), and 266 (`_on_form_save`,
add/edit). Reference fix shape: `games.py`'s `_mutate`
(reload-then-apply-then-save) for the ones that touch more than one
field, `storage.update_entry` directly for single-field bumps like
`_open_entry`'s `last_used`.
**Done when** - all four sites either call `storage.update_entry` for a
single field or reload from disk immediately before mutating and
saving the full list, matching `games.py`'s pattern; a scripted
race (mutate the file from outside the page's snapshot, then trigger
each save path) no longer loses the outside change, verified for Apps
and Websites both since they share this class.
**Risk** - this is the same class of bug the project has already been
burned by once; a partial fix (three of four sites migrated) leaves the
page exactly as exploitable as before on the missed one, so treat it as
one item covering all four sites, not four small ones.
**Landed** (1.4.2) - all four sites: `storage.update_entry` for
`last_used` and for an edit's own fields, `games.py`'s reload-then-apply
`_mutate` for add and remove. The `__init__` write now fires only when
the migration actually changed something, instead of rewriting the whole
list on every visit to Apps or Websites. Harness drives all four paths
after changing the file from outside the page's snapshot: 22 checks
pass, and 7 per page fail against the pre-change file - so it measures
the defect rather than passing either way.

### 3. Tie the released exe to the source tree it was built from

**What** - 1.4 was tagged once, then re-cut before anyone had it: the
first `Atomic.exe` was built from a stale PyInstaller cache and was
missing `src/filter_icon.png` (173 archive entries against the 174 a
real build of that tree produces), and still had Home's row labelled
"Series" rather than "Movies & Series" (VDD-1.4 §3). Nothing in the
build or release path would have caught this without someone manually
counting archive entries after the fact.
**Why now** - This shipped once already, on the very release this
roadmap is written after. `packaging/build.py` copies whatever
PyInstaller produces with no verification step at all; the `build`
skill's step 4 already *says* "don't trust the build log... confirm it
with the test skill's frozen-build extraction" for exactly this
reason, but that's a instruction for a human/agent to remember to run
by hand, not something the build itself enforces.
**Owner** - release-engineer
**Where** - `packaging/build.py` (64 lines total - `main()` at
17-60 runs PyInstaller and copies the result with no check beyond
"does `Atomic.exe` exist"). `packaging/Atomic.spec`'s `datas=[...]`
(line 79) is the authoritative list of what must end up bundled. The
`test` skill already has the frozen-archive-extraction snippet this
should reuse rather than reinvent.
**Done when** - `build.py` (or a step it calls) fails loudly - not a
silent success - when the produced exe's bundled archive doesn't
contain every file `Atomic.spec`'s `datas` lists, or when PyInstaller's
own cache appears to have skipped a real rebuild (e.g. compare the
work directory's timestamp against the newest file under `src/`).
Verified by deliberately staging a stale-cache build (skip clean,
remove a bundled asset from disk, rebuild) and confirming the check
catches it rather than producing a silently-broken exe.
**Risk** - the failure mode here is a check that's *technically*
correct but too easy to skip under release pressure (the same way the
existing "read the test skill" instruction was skipped once already) -
wire it into `build.py` itself, or the `release` skill's procedure,
not just into documentation that has to be remembered.

### 4. Write 1.4's missing "what's new" notes

**What** - `helpers/whats_new.py`'s `NOTES` dict (lines 28-55) has
entries for `"1.3"`, `"1.2"`, `"1.1"` - nothing for `"1.4"`. This
dialog exists specifically so "updating from Settings... otherwise
gives no sign of what actually changed" (the module's own docstring).
Right now, anyone updating from 1.3 to 1.4 - the release that changed
the most of anything since 1.0 - gets the dialog's silent-failure path:
`notes_between` finds no `"1.4"` key, returns `[]`, the dialog shows
nothing, and `set_last_seen_version` still records 1.4 as seen -
permanently. There is no second chance; that user will never see 1.4's
notes through this mechanism.
**Why now** - This is live right now, not theoretical: the app is
already at 1.4 and real updates are landing. Every day this stays
unfixed is a user who updates and gets nothing.
**Owner** - ui-engineer
**Where** - `src/helpers/whats_new.py`, `NOTES` dict (28-55). Write a
`"1.4"` entry in the same voice as the existing ones (user-facing
language, no module names - see the docstring's own rule, lines 8-12):
Movies tracked alongside Series, Netflix on by default, one progress
source (Stremio) instead of several, the filter button, quick +/-
restored for hand-set entries, opening a page being the refresh. Draw
the actual list from `docs/VDD-1.4.md` §5, translated into the
user-facing voice the existing entries use - not copied verbatim, which
would reintroduce internal wording the docstring explicitly forbids.
**Done when** - `notes_between("1.3", "1.4")` returns the new section,
and `UpdateSummaryDialog` shows it end to end for a simulated 1.3->1.4
upgrade (stub `take_updated_from`/`get_last_seen_version` to return
`"1.3"`).
**Risk** - low; this is a data-only fix. The process gap that let it
happen in the first place is item #5, sequenced right after this one on
purpose.
**Landed** (1.4.2) - nine notes, written from VDD-1.4 §5 into the
dialog's own voice. `notes_between("1.3", "1.4.1")` returns them (the
live three-part version still matches the "1.4" key), ordering across
1.1-1.4 intact, and the dialog renders end to end for a simulated
1.3 -> 1.4 upgrade. Turned up item #29 on the way.

### 5. Make a future release unable to ship without its notes

**What** - Item #4 fixes 1.4's specific gap; this stops the next
release from having the same one. Nothing today ties `whats_new.NOTES`
to the release process - the `release` skill's procedure has no step
that checks a new version's notes were written before tagging.
**Why now** - Directly caused item #4. A one-off content fix without
this is a bug that will recur at the next release, silently, exactly
the same way.
**Owner** - release-engineer
**Where** - `src/helpers/whats_new.py` (`NOTES`, `current_release_notes`
at 139-148 - a function already built for "this build has no notes"
detection, just not used as a release gate). The natural hook is a
check the `release` skill's procedure runs before tagging: does
`NOTES` contain a key matching the version about to ship.
**Done when** - tagging a release whose version has no `NOTES` entry
either fails the release step or produces an explicit, impossible-to-miss
warning the release-engineer has to act on - not a silent gap
discoverable only by a user updating and seeing nothing.
**Risk** - keep this cheap. A hard failure that blocks tagging is fine
since notes should always exist by release time; the risk is building
something heavier (a whole content-authoring workflow) when a single
presence check at tag time is all item #4's actual failure needed.

### 6. Stop the sidebar Add menu positioning with `mapToGlobal`

**What** - `main.py`'s `_show_add_menu` (418-425) opens its popup menu
with `menu.exec(anchor_btn.mapToGlobal(anchor_btn.rect().bottomLeft()))`
- the exact trap `ui.md` documents: "On two monitors at different scale
factors it returns coordinates divided by the *other* screen's scale
factor - toasts landed 200px off." The tracker's own filter button hit
this same class of decision and was deliberately built with
`setMenu` instead (VDD-1.4 §6: "there is no `mapToGlobal` here to
return coordinates divided by the other screen's scale factor"). Every
other popup menu in the app (`games.py:225`, `link_grid.py:239`,
`tracker.py:1166`) already uses `event.globalPosition().toPoint()` from
the real mouse event, or `setMenu`. The Add menu is the one place still
doing it the old, documented-broken way.
**Why now** - This is a live, unfixed instance of a defect class this
project has already paid to diagnose (toast positions) and already
fixed once in a sibling case (the filter button). It's cheap to fix and
easy to miss precisely because it still runs without visibly erroring -
it just lands wrong, on the hardware that exposes it.
**Owner** - ui-engineer
**Where** - `src/main.py`, `_show_add_menu` (418-425).
**Done when** - the Add menu opens anchored to the button correctly;
verified either on a real mixed-DPI multi-monitor setup (per
`.claude/rules/testing.md` - visual/DPI work needs a real window, not
offscreen) or by switching to `anchor_btn.setMenu(menu)` the same way
the filter button did, which sidesteps the trap by construction rather
than by careful math.
**Risk** - none beyond the usual DPI-testing caveat: this class of bug
is invisible on a single-monitor 100%-scale dev machine, so "looks
fine" here proves nothing - match it against the filter button's fix or
measure on real mixed-DPI hardware.

### 7. Route entry-search suggestions through `lookup_pool`

**What** - `tracker.py`'s Add/Edit title-search suggestions
(`_trigger_search` ~1871, `_search_worker` ~1914-1924) fire a bare
`threading.Thread` per debounce pause, not through
`lookup_pool.submit_latest`. `integrations.md`'s standing rule is
explicit: "Work fired by typing goes to `lookup_pool.submit_latest`,
not `submit`" - and this predates the item that fixed video-site
resolution the same way (`_start_video_site_resolution`, old roadmap
item #5).
**Why now** - Lower stakes than the originally diagnosed instance (a
`QTimer` debounce already prevents one thread per keystroke, so this
isn't the "651 simultaneous connections" shape), but it's a real,
measurable gap against the project's own written rule, and 1.4 made it
worse in one respect: `_search_catalogs` (1896-1912) now asks *two*
Cinemeta catalogs per search when both Series and Movie are offered, so
a thread that outlives its debounce window now costs two requests, not
one. Typing fast enough to outrun one lookup before the next debounce
fires runs two of these concurrently with nothing capping it.
**Owner** - integrations-engineer
**Where** - `src/windows/tracker.py`: `_trigger_search` (~1871-1885,
the `threading.Thread(target=self._search_worker, ...).start()` at
1884-1885). `lookup_pool.submit_latest` (`src/helpers/lookup_pool.py`)
is the existing pattern to route through - same as
`_start_video_site_resolution` a few hundred lines below it in the same
file.
**Done when** - typing rapidly in the Add/Edit title field never runs
more than one `_search_worker` at a time (measured by counting live
threads/in-flight requests during rapid typing, the same method used to
verify the video-site fix); the `seq`-based stale-result discard stays
in place unchanged as the second line of defence.
**Risk** - low; this mirrors an already-landed fix in the same file, not
new design. Verify debounced search still feels responsive after
routing through the shared single-worker key, same check the original
fix made.

### 8. Retire or rebuild `diagnose_anilist.py` - it calls functions that no longer exist

**What** - `packaging/diagnose_anilist.py` is a standalone diagnostic
script for troubleshooting "why isn't my progress syncing," written
for the AniList-username/MAL-Sync era. It calls
`app_settings.get_anilist_username()` and
`anilist.fetch_watch_progress()` - **both removed** in 1.4's "Stremio
only" change (confirmed: `grep` for either name across `src/` returns
nothing outside this file). Running it today raises `AttributeError` on
line 24, before it prints anything useful. Its own docstring and output
text describe a refresh button that no longer exists and a
Crunchyroll-token-priority rule that was removed with the rest of
Crunchyroll progress reading.
**Why now** - This is exactly the tool someone would reach for the next
time Stremio progress looks wrong (see item #1) - and it will crash
immediately instead of helping, at precisely the moment it's needed.
Dead code describing a removed design is actively misleading, not
merely unused.
**Owner** - integrations-engineer
**Where** - `packaging/diagnose_anilist.py` (71 lines, the whole file).
**Done when** - either the file is deleted (nothing in `build`/`release`
skills references it, confirmed by grep), or it's rewritten as
`diagnose_stremio.py` following the same shape - read the stored
`auth_key`, call `stremio.fetch_watch_progress` per tracked title, and
report which of the real failure links broke (no account connected, the
API call itself failing per item #1's new distinguishable error, or the
title genuinely absent from the library) - and runs to completion
without raising against a real or stubbed Stremio response.
**Risk** - none; this is dev tooling, not shipped in the exe
(`Atomic.spec`'s `Analysis` only starts from `src/main.py`), so nothing
about the running app depends on this either way.

---

### 10. Show a Stremio connection that has gone bad, in Settings itself

**What** - Once connected, Settings' Stremio Account section
(`settings_dialog.py` ~930-937, `_refresh_stremio_account`) shows
"Connected as {email}" forever - there's no re-check of whether the
stored `auth_key` still works. Item #1 makes a broken session
*syncable-distinguishable* in the tracker's own messaging; this is the
companion piece that surfaces the same fact where the user would
naturally look to fix it.
**Why now** - Directly follows #1 - once the sync path can tell "auth
is broken" apart from "nothing found," Settings should be able to say
so too, rather than showing a connected-looking status that's quietly
false. Sequencing matters: this needs #1's distinguishable failure to
exist before it has anything real to display.
**Owner** - ui-engineer
**Where** - `src/helpers/settings_dialog.py`: `_refresh_stremio_account`
(~930-937) and the status label `self.stremio_account_status` (~461).
Depends on item #1's new failure signal existing to check against.
**Done when** - after item #1 lands, a stubbed-broken `auth_key` shown
in Settings reads something other than a plain "Connected as X" -
e.g. "Connected as X - last sync failed to authenticate" - distinct
from both "Not connected" and a healthy connection.
**Risk** - avoid firing a network probe just to *render* Settings (that
would add a request cost to opening a dialog); piggyback on the reason
already carried back from the tracker's own last sync attempt rather
than probing independently.

### 27. Say what a site's check verdict actually means

**What** - Owner-raised, verbatim: *"make the checking status more clear
in the settings (what do you mean by: search links only - no title
pages? and (opens title pages))"*. The verdict a site's **Check** button
produces is rendered through `_RESOLVES_LABELS`
(`settings_dialog.py:65-73`) as one of "opens title pages", "opens title
pages (read off its search page)", "search links only - no title pages",
"didn't answer when checked", "couldn't be checked". Those phrases
describe the *resolver's* internal distinction, not anything the owner
can act on, and the person who uses this app every day could not tell
what they meant.
**Why now** - This is the only class of report on this list that comes
from using the app rather than reading it, and the feature it describes
(pre-1.4 item #12) exists precisely so the owner can tell a good site
from a useless one. A verdict nobody can interpret does not do that job,
so the feature is currently not delivering what it was built for.
**Owner** - ui-engineer
**Where** - `src/helpers/settings_dialog.py`: `_RESOLVES_LABELS` (65-73)
and `_site_note` (~757-763), plus the two Check buttons' tooltips
("whether it opens title pages or only search links", lines ~450, ~542).
The underlying distinction to express in the owner's terms: a site that
**opens the exact title you clicked** versus one that can only **drop
you on its search results, where you still have to find the title
yourself**. `anime_sites.probe_site` (~1014-1051) is where each verdict
comes from - the wording must stay true to what was actually measured,
including `generic`, which does open title pages but reads the link off
a search page and is therefore likelier to break.
**Done when** - each verdict reads as a plain sentence about what will
happen when the owner clicks a title on that site, with no resolver
vocabulary ("engine", "generic", "resolves", "title page" used as jargon)
- checked by reading the five strings cold and asking whether the answer
to "so what happens when I click?" is obvious from each one. No change
to `probe_site`'s verdicts themselves.
**Risk** - the temptation is to collapse `engine` and `generic` into one
label because they both open title pages; don't. `generic` is the
fragile path (it scrapes the site's own search results) and losing that
distinction would hide exactly the case that breaks first. Say it in
plain words instead.
**Landed** (1.4.4) - "opens each entry's own page" / "...if its search
still works" / "only opens its search - you pick the entry yourself" /
"the site didn't answer when checked" / "the check didn't finish - try
Check again". `generic` kept its own line, with the measured reason
stated rather than hidden. Both Check tooltips carry the longer
explanation. Measured in a real dialog seeded with one site per verdict:
every realistic row fits at the dialog's default 920px without eliding
(longest 603px). Two things left alone on purpose: at the dialog's
*minimum* width most rows elide, which predates this and needs a layout
change, not a wording one; and `probe_site`'s verdicts themselves are
untouched.

### 28. Clear site check verdicts when the app restarts

**What** - Owner-raised, verbatim: *"make the check status in the
settings clear each time the app closes and re-opens"*. Today a verdict
is written onto the site record (`anime_sites.record_resolution`
line 180-183, `manga_sites.record_resolution` 79-82, stored as
`"resolves"`) and shown forever after, with nothing recording *when* it
was measured - so a check run once, weeks ago, still reads as the site's
current state.
**Why now** - Directly requested, and it is the honest behaviour: the
verdict is a measurement of a remote site at one moment, not a property
of the site. A stale "opens title pages" is a claim the app cannot
stand behind after a restart, which is the same shape as every other
silently-wrong-answer defect this project has fixed.
**Owner** - ui-engineer
**Where** - The stored `resolves` field is **display-only** - grep
confirms its single reader is `settings_dialog._site_note` (~761), so
clearing it changes nothing about how sites are searched or opened. The
contained shape is to drop the field at startup (or ignore any verdict
not recorded this run) rather than to stop persisting it: `src/main.py`
at launch, or a load-time filter in `settings_dialog`.
**Done when** - opening Settings after a fresh launch shows every site
with no verdict until it is checked again; checking one within the same
run still shows its result until the app closes. Verified against a
temp `DATA_DIR` copy, never real user data.
**Risk** - low, and deliberately kept low: do not take this as licence
to change what `probe_site` writes or how sites resolve. Per the owner's
standing note, this is a display-lifetime change, not a backend one.
**Landed** (1.4.4) - a module-level `_CHECKED_THIS_RUN` set of site ids,
consulted by the one reader (`_site_label`). Nothing on disk is cleared
and `record_resolution` still writes exactly as before; a verdict simply
isn't shown unless it was measured this run. Module level rather than on
the dialog, because the dialog is rebuilt on every open and the lifetime
wanted is the process. A site whose `record_resolution` write *fails*
stays hidden too - what's on disk is then an older run's answer, which
is the thing being hidden in the first place. Verified against a temp
copy: stored verdicts survive on disk while showing nothing, appear
after a real probe, and disappear again when the run resets. The stale
Video Websites hint went with it - Crunchyroll and Netflix open title
pages via public databases, and it said they only opened a search.

### 29. Let the "what's new" dialog scroll

**What** - Found while landing #4: `UpdateSummaryDialog` has no scroll
area, so its height is however tall its notes make it. 1.4's nine notes
measure **714px**. Fine on the owner's display; on a laptop screen, or
at a future release with more notes, the bottom simply runs off the
desktop with no way to reach it.
**Why now** - #4 just tripled the length of the longest entry the dialog
has ever shown, so the gap moved from theoretical to one release away
from biting. Cheap now, and it is the kind of thing only noticed on the
machine where it breaks.
**Owner** - ui-engineer
**Where** - `src/helpers/whats_new.py`, `UpdateSummaryDialog`. Wrap the
notes in `widgets.scroll_area` (the app's own helper, already used by
every page) with a maximum dialog height, rather than capping the number
of notes shown.
**Done when** - a dialog built from a deliberately long notes list stays
within the available screen height and scrolls to reach the rest;
1.4's nine notes still show without a scrollbar appearing where none is
needed.
**Risk** - low. Don't solve it by truncating the notes - the whole point
of the dialog is that the user sees what changed.

### 30. Stop a two-line card name being clipped on Apps/Websites

**What** - Found while landing #2, and confirmed pre-existing (it
reproduces on the untouched build path with a stock file): a card whose
name wraps to two lines has its second line clipped, taking the "Added
Just Now" line with it. `QGridLayout` sizes the row before the
word-wrapped label reports its height-for-width.
**Why now** - It is a visible defect on two shipped pages, and it was
measured rather than guessed - a screenshot on the pre-change build
shows it. Not urgent (long names are uncommon), but it costs nothing to
carry.
**Owner** - ui-engineer
**Where** - `src/windows/link_grid.py`'s card construction and the grid
in `_refresh_grid`. The usual causes are a fixed card height, or a
wrapped `QLabel` inside a layout that never gets told its height depends
on its width.
**Done when** - a card with a name long enough to wrap shows both lines
and its "Added"/"Last used" line, at the same card width as today, on
Apps and Websites; single-line cards are unchanged.
**Risk** - low, but it is a layout change on a shipped page: check a
mixed grid (short and long names together) rather than one card in
isolation, or rows end up ragged.

### 11. Remember a tracker page's search/filter across a revisit, for the session

**What** - Documented as a known limitation in VDD-1.4 §10: "Filter
selections do not persist. Pages rebuild from scratch on every visit,
so a filter is cleared by navigating away." Typing a search term,
navigating to Home and back, and finding the search box empty again is
the concrete shape of this.
**Why now** - Named directly in 1.4's own known-limitations list, not
speculative - a search/filter this month made routine navigation lossy
in a way it wasn't before the feature existed.
**Owner** - ui-engineer
**Where** - `src/windows/tracker.py`: `TrackerPage.__init__` builds
`self.search_box`/`self._filter_menu` fresh every time (~569-611,
~469+). `ui.md`'s rule ("pages rebuild from scratch... state lives in
the saved JSON or it doesn't exist") rules out writing this to disk -
the fix is an **in-memory, per-page-name** cache that outlives one
page instance but not the app itself (e.g. a small dict held by
`main.py` or a module-level store in `tracker.py`, read on construction
and written on change), not a new JSON field.
**Done when** - searching/filtering on Anime, navigating to Home, and
returning to Anime shows the same search text and filter ticks still
applied; quitting and relaunching the app starts blank, confirming
nothing was written to disk.
**Risk** - keep this session-only and explicitly not persisted - a
filter silently narrowing the grid after a restart, with no visible
reason why some entries are "missing," would be worse than today's
"always resets."

### 12. Bring search to Games, Apps and Websites

**What** - Item #11 (old roadmap) gave Anime/Reading/Series a search
box; Games, Apps and Websites have none - `grep` for `search` across
`games.py`/`link_grid.py` returns nothing. There's no status/type
concept on these pages (they're flat lists), so this is search only,
not the fuller filter-by-status the tracker pages have.
**Why now** - Same proven, low-risk pattern already shipped once
(debounced text box narrowing a grid before it's laid out); the
remaining three pages are the ones it was never extended to.
**Owner** - ui-engineer
**Where** - `src/windows/games.py` (`_refresh_grid` ~178) and
`src/windows/link_grid.py` (`_refresh_grid` ~187, shared by
`AppsPage`/`WebsitesPage`) - both have a `top_row` sort control (
`link_grid.py` ~128-137) that a search box slots into the same way
`tracker.py`'s did.
**Done when** - a search box on each of Games/Apps/Websites narrows the
grid by case-insensitive substring match on name; clearing it restores
the full list.
**Risk** - Games and Apps/Websites both support drag-to-reorder; per
the precedent already set for the tracker pages (item #11, old
roadmap), disable dragging while a search is active on these pages too
rather than reconciling a partial on-screen order with the saved one.

### 13. Check for updates in the background, not only on demand

**What** - `updater.check_for_update()` is only ever called from
`settings_dialog.py`'s `_on_update_clicked` (280-290), fired by a
button press. There is no startup or periodic check anywhere -
`grep`ping `main.py` for `updater` finds nothing. A user who doesn't
think to open Settings never learns a new version exists.
**Why now** - The app already ships a complete, verified update
mechanism (hash-checked download, atomic swap) - the only missing piece
is *telling* the user one is available, rather than requiring them to
go looking. Low cost given everything else already exists.
**Owner** - ui-engineer
**Where** - `src/main.py` (owns app startup, per item #1's precedent in
the pre-1.4 plan) is where a background check on launch belongs, using
the existing `updater.check_for_update()` unchanged - this item does
not touch the updater's GitHub API contract. Surface via
`widgets.show_toast`/a sidebar badge rather than a dialog (per `ui.md`:
dialogs are for what the user must decide or not miss; "a new version
exists" isn't that).
**Done when** - launching the app with a newer release actually
published shows a passive notice (toast/badge) without any click, and
launching when already current shows nothing; the check runs at most
once per launch (or on a long interval) so it never competes with
`lookup_pool`'s own traffic.
**What happens when the service says no** - unchanged: GitHub
rate-limiting or no network already fails soft inside
`check_for_update` (returns/raises readable errors); a background check
failing should do nothing visible at all, not surface an error for a
check the user didn't ask for.
**Risk** - don't make this naggy - once per launch (or longer) and
dismissible, not a recurring interruption.

### 14. Remember window size and position across launches

**What** - `MainWindow.__init__` (`src/main.py:153`) always calls
`self.resize(1280, 840)` - there is no `saveGeometry`/`restoreGeometry`
anywhere in the file (confirmed by grep). Resizing or moving the window
- to a second monitor, to a preferred size - is lost on every restart.
**Why now** - A daily-use desktop app resetting its own window every
launch is a small but constant friction the owner has had no reason to
mention explicitly because there's nothing to compare it against - it's
simply how the app has always behaved. Cheap, contained, no dependency
on anything else in this plan.
**Owner** - ui-engineer
**Where** - `src/main.py`, `MainWindow.__init__` (~150-153). Save via
`app_settings` (a new key, following the existing pattern in
`helpers/app_settings.py`) on close/move/resize, restore before
`self.show()`.
**Done when** - resizing/moving the window, closing, and relaunching
restores the same size and position; a first-ever launch (no saved
geometry) still falls back to `1280x840` unchanged.
**Risk** - clamp the restored geometry to the current available screen
- a size/position saved on a monitor that's no longer connected must
not open the window off-screen with no way to reach it.

### 15. Give Settings some structure before it grows further

**What** - `settings_dialog.py` is 1,024 lines of one long scrolling
form - Nav order, Stremio Account, Video/Reading Websites, Startup,
Manga Music, Clear Data, Uninstall, Update, all in sequence with no
sections/tabs to jump between. The pre-1.4 roadmap's own item #10
called out "the existing (buried) hint text" in this file as something
not to rely on being found - a symptom of the same underlying shape.
Items #10, #19, #20/#21 in this plan all add more controls to this same
dialog.
**Why now** - Every settings item this plan adds makes the existing
problem worse, not better - the dialog only grows. Better to size the
restructuring question now than let four more sections get buried
behind it.
**Owner** - ui-engineer
**Where** - `src/helpers/settings_dialog.py`, the whole file - shape
genuinely unknown (a `QTabWidget`? a sidebar-nav within the dialog?
collapsible sections?) until someone looks at what groups naturally and
what a fixed-height dialog vs. a taller one costs visually.
**Done when** - not a specific layout, but a decision: either a
restructuring is scoped and sized as a concrete follow-up item, or the
investigation concludes the current single-scroll form is fine at this
size and says why (e.g. "everything is reachable within N scrolls, not
worth the risk of breaking a working dialog").
**Risk** - this is investigate-before-build; a bad restructuring that
breaks a currently-working, if unstructured, dialog is worse than
leaving it alone. Don't let "it's long" alone justify a rewrite without
sizing the actual cost/benefit.

### 16. Flag an App/Website entry whose target has disappeared

**What** - An Apps entry launches a local `.exe`/`.lnk` path; nothing
checks whether that path still exists before offering to launch it (an
uninstalled or moved program just fails silently or with a raw OS
error when clicked). This is the cheap, local half of the same
"does this thing still work" question `probe_site` already answers for
Video/Reading websites (pre-1.4 roadmap item #12) - checking a local
path needs no network at all.
**Why now** - Programs get uninstalled/moved far more often than a
tracked website disappears, and unlike a network probe this is an
instant, free `Path.exists()` check - one of the cheapest items on this
whole list relative to its value.
**Owner** - ui-engineer
**Where** - `src/windows/link_grid.py` (`AppsPage`'s entries carry a
local path via `TARGET_KIND = "app"`, `apps.py:10`) - check at grid-
build time (`_refresh_grid` ~187) or lazily on open, and surface as a
small badge/greyed state on the card, mirroring `probe_site`'s verdict
labels in Settings' site list.
**Done when** - an Apps entry whose target path no longer exists shows
a visible "target not found" state on its card, verified by pointing an
entry at a path and deleting it.
**Risk** - keep this local-only for now (no network dependency, no
flakiness to fail soft around); a Websites-URL liveness equivalent
would need the same deadline/probe discipline as `probe_site` and is
explicitly out of scope here - don't fold it in without separately
sizing that cost.

### 17. Keyboard shortcut to jump to a page's search box

**What** - Once items #11/#12 land, every card-grid page has a search
box, but reaching it always means a mouse click first. A single
shortcut (e.g. `Ctrl+F`) focusing the current page's search box, and
`Esc` clearing/leaving it, is a small, standard, low-risk addition on
top of infrastructure this plan already adds.
**Why now** - Cheap follow-on once #11/#12 exist; not worth its own
investigation, but also not worth doing before the search boxes it
targets exist on every page.
**Owner** - ui-engineer
**Where** - Wherever `search_box`/the Games/Apps/Websites equivalent
already sit after #11/#12 land; a `QShortcut` per page, or one handled
centrally in `main.py` dispatched to whichever page is current.
**Owner's addition** - *"add more shortcuts like Ctrl+Z and Ctrl+Y and
more of your choice."* So this item is no longer only `Ctrl+F`: it owns
the app's keyboard map as a whole. `Ctrl+Z`/`Ctrl+Y` are undo/redo of
the last destructive action, which means this item **depends on #23**
(undo the last removal) for anything to undo - build #23's undo record
first, then bind the keys to it rather than inventing a second undo
mechanism. The rest are the Manager's/UI engineer's choice; keep them
conventional (`Ctrl+N` add, `Ctrl+,` settings, `F5` refresh, `Ctrl+1..7`
pages, `Esc` close) rather than clever, and write them down somewhere
the owner can see them - a shortcut nobody can discover is not a
feature.
**Done when** - `Ctrl+F` focuses the visible page's search box from
anywhere on that page and `Esc` clears it and returns focus to the grid;
`Ctrl+Z` undoes the last removal (and `Ctrl+Y` redoes it) through #23's
record, on every page family that supports removal; the full list is
discoverable from within the app, not only from this file.
**Risk** - low per shortcut, but the set is where it goes wrong:
`Ctrl+Z` must not appear to work while doing nothing (bind it only where
#23 actually has a record), and nothing here may shadow a shortcut Qt
already binds inside a text field - verify by typing in the Add/Edit
dialogs with every new binding live.

### 18. Bring Apps to parity with Games' "Import from Launchers"

**What** - Games has a one-click "Import from Launchers" scan
(`games.py:239` `_import_from_launchers`, backed by `helpers/
launchers.py`'s `scan_all`/`import_scanned_games`) that finds installed
games automatically. Apps (`windows/apps.py`, a 10-line subclass of
`LinkGridPage`) has no equivalent - every app has to be added one at a
time via the file picker (`_open_add_form` -> `QFileDialog`), despite
being conceptually the same kind of entry (a local executable).
**Why now** - The exact infrastructure this needs already exists and
works (`launchers.py`'s scanning, `app_settings.get_launcher_dirs`/
`set_launcher_dir`) - this is extending a proven pattern to a page it
was never pointed at, not building new scanning logic from scratch.
**Owner** - ui-engineer
**Where** - `src/helpers/launchers.py` (currently game-shaped:
`scan_all`/`import_scanned_games` assume a games library layout, e.g.
Steam/Epic install directories) needs a parallel path for general
Windows apps - likely Start Menu `.lnk` shortcuts rather than a game
launcher's library format, which is a different data source, not a
reuse of the existing scan. `src/windows/apps.py` gains the button and
wiring `games.py` already has (`_import_from_launchers`-equivalent,
`_scan_worker`, `_on_scan_done`).
**Done when** - an "Import" action on the Apps page finds Start Menu
shortcuts (or another well-defined common source) not already tracked
and adds them in one pass, mirroring Games' toast-driven flow
("Scanning..." -> result count).
**Risk** - genuinely spans modules: this needs a new scan source in
`launchers.py`, not just UI wiring, since apps and games are discovered
from different places on disk. Size it as its own investigation if the
Start Menu approach doesn't cleanly generalize once started.

### 19. Check every configured site at once, not one at a time

**What** - `probe_site` (pre-1.4 roadmap item #12) added a "Check"
button per Video/Reading website in Settings (`settings_dialog.py`
~448, ~540, `_check_site`/`_probe_site_async` ~765-889) - one at a
time. With several sites configured, re-verifying all of them after,
say, a host outage means clicking Check on each individually.
**Why now** - Small, low-risk extension of an already-shipped,
already-verified feature; modest value on its own, but nearly free
given `_probe_site_async` already exists and just needs to run over a
list instead of one id.
**Owner** - ui-engineer
**Where** - `src/helpers/settings_dialog.py`: `_probe_site_async`
(~765-780) already takes one `site_id`; add a "Check All" button beside
the existing per-site ones on both the Video and Reading site lists
that loops it (through `lookup_pool`, not a bare loop of threads, to
stay within the existing concurrency cap).
**Done when** - one click re-probes every configured site on that list
and updates each verdict as it comes back, without spawning more
concurrent requests than `lookup_pool`'s existing cap allows.
**Risk** - minimal; route through the shared pool rather than firing N
bare threads, or this reintroduces the exact concurrency-cap bug this
project has already fixed once (pre-1.4 item #4/#5).

---

### 20. Back up all tracked data from Settings

**What** - The only safety net today is the per-save `.bak` (item #6,
pre-1.4 plan) - one file, one version back, automatic and invisible.
There's no way for the owner to take a point-in-time copy of everything
(`tracker.json`, `series.json`, `games.json`, `apps.json`,
`websites.json`, settings) before, say, a risky manual edit or moving
machines.
**Why now** - This project has already lost real data once (the BOM'd
`settings.json` incident, item #6). The automatic `.bak` covers "the
last save went wrong"; it doesn't cover "I want a copy before I do
something risky," which is a different, still-unmet need on an app
whose stated philosophy is "data loss outranks every feature."
**Owner** - ui-engineer
**Where** - `src/helpers/settings_dialog.py`'s "Clear Data" section
(~650+) is the natural neighbor - a "Back Up Data" action alongside it
that zips everything under `storage.DATA_DIR` (the JSON files;
`atomic.log` and `image_cache/` are reasonable to exclude, per item
#25, as regenerable/bulky) to a location the user picks via
`QFileDialog`.
**Done when** - clicking Back Up produces a zip containing every
tracked-data JSON file, verified by re-reading it back with `storage`'s
own loaders against a throwaway directory.
**Risk** - low; this is a read-only export, nothing it does can lose
data on its own. Sequenced before item #21 (restore), which is the
higher-risk half.

### 21. Restore tracked data from a backup archive

**What** - The counterpart to item #20: load a previously-exported
backup back into `storage.DATA_DIR`, for a fresh install or recovering
from a bad edit.
**Why now** - A backup nobody can restore from is not actually a safety
net - this completes item #20 rather than standing alone.
**Owner** - ui-engineer
**Where** - `src/helpers/settings_dialog.py`, beside item #20's button.
`src/helpers/storage.py`'s existing quarantine-on-parse-failure and
`.bak`-before-overwrite behavior (item #6, pre-1.4) is exactly the
safety this should route through rather than writing new
overwrite-handling logic - a restore is, mechanically, a batch of
saves.
**Done when** - restoring a backup produced by item #20 into a clean
temp `DATA_DIR` reproduces the original data exactly; restoring a
*corrupted* backup (truncated zip, a JSON file inside it that won't
parse) is rejected with a clear message and **does not partially
overwrite** real data - verified by testing a bad archive against a
temp copy per `.claude/rules/testing.md`, never real user data.
**Risk** - this is the item on this list closest in shape to the
original data-loss incident (item #6, pre-1.4) - a botched restore is
itself a data-loss vector. Confirm-before-overwrite (matching the
"Clear Data" confirmation pattern already in this dialog) and validate
every file in the archive before touching anything on disk, not
file-by-file as it goes.

### 22. Multi-select bulk status change on tracker cards

**What** - Changing several entries' status at once (e.g. marking a
batch "Dropped") needs opening Edit on each one individually - there's
no multi-select on tracker grids today.
**Why now** - Named as a plausible usability win, not a diagnosed gap
the owner has raised - flagged as lower-confidence than the items
above it for exactly that reason. Real value grows with library size;
`.claude/rules/testing.md`'s performance numbers (500 entries, 0.3ms/
card) show the grids already scale, so this isn't blocked on anything
else.
**Owner** - ui-engineer
**Where** - `src/windows/tracker.py`: `Card`/`CardDragReorder`
(`helpers/widgets.py`) have no selection concept at all today - this is
new interaction, not an extension of an existing one. Has to reconcile
with the already-existing drag-to-reorder and the search/filter-active
states (items #11/#7's precedent: dragging is already disabled while
filtering, per VDD-1.4 §5 - a multi-select mode likely needs the same
treatment).
**Done when** - not prescribed; this is genuinely open on interaction
shape (click-to-toggle vs. a dedicated "select" mode, a bulk action bar
vs. a context menu) - the investigation half of this item is choosing
one and sizing it before building it.
**Risk** - the real risk is under-scoping this as "just add checkboxes"
when the harder part is how selection interacts with drag-reorder,
search/filter, and per-status sectioning (`_sections`) that already
exist. Size it honestly once the shape is chosen, don't guess.

### 23. Undo the last removal, via a toast

**What** - Removing an entry/game/app/website asks "Remove 'X'?" (a
`QMessageBox.question`, e.g. `games.py:295`, `link_grid.py:250`,
tracker's equivalent) and then it's gone - no undo beyond manually
re-adding it. The confirmation dialog is the only safety net.
**Why now** - Small, cheap, standard pattern (`widgets.show_toast`
already exists and is used for exactly this "here's what just happened,
briefly" shape elsewhere) - a natural complement to the confirmation
dialog rather than a replacement for it.
**Owner** - ui-engineer
**Where** - `src/windows/games.py:294` (`_remove`), `src/windows/
link_grid.py:249` (`_remove_entry`), and tracker's own entry-removal
path each need to keep the removed record and offer it back via a
toast with an action, using `widgets.show_toast`/`finish_toast`
(`helpers/widgets.py`).
**Done when** - removing an entry shows a toast with an "Undo" action
that, clicked within the toast's dwell time, restores the exact entry
removed (same id, same fields) on all three page families
(tracker/games/link_grid); letting the toast expire leaves the removal
final, as today.
**Risk** - spans the three separate removal code paths (tracker, games,
link_grid don't share a base class) - a fix landed in one and not the
others leaves an inconsistent app where undo works on some pages and
not others; treat as one item covering all three, not three small ones.

### 24. One search across every page, not just the tracker family

**What** - Once items #11/#12 give every page family its own search
box, a natural next question is a single search reachable from
anywhere (Home, or the sidebar) that looks across Anime/Reading/Movies
& Series/Games/Apps/Websites at once, rather than six separate boxes.
**Why now** - Lower-confidence, larger-shape item, explicitly sequenced
after #11/#12: building a global search before every page family has
its own would mean building the underlying "search everything" logic
twice. Real value here scales with how many pages/entries the owner
actually has - genuinely unproven need, flagged honestly as such.
**Owner** - ui-engineer
**Where** - `src/windows/home.py` (already reads every data file, see
its own docstring) is the natural home for an entry point; shape of
the results UI (a dropdown, a dedicated results page, inline
navigation to the matched page) is undecided.
**Owner's addition** - *"the global search idea is great, but make sure
that it has a very very good location and size based on other famous
apps using it."* That moves this item from "is it worth building" to
"build it, and get the placement right": the investigation is now about
**where it lives and how big it is**, measured against how apps the
owner already uses do it (a top-centre command bar, a persistent sidebar
field, a `Ctrl+K` overlay), not about whether to have one.
**Done when** - a global search exists, reachable from every page, at a
position and size argued from named real-world examples rather than
chosen ad hoc - the comparison written down in the item's landing note
so the choice can be reviewed. Per-page searches (#11/#12) stay; this
sits above them, not instead of them.
**Risk** - don't build this before #11/#12 land; it would either
duplicate their filtering logic or leave two inconsistent search
implementations side by side.

---

### 26. Audit what PyInstaller actually bundles into `Atomic.exe`

**What** - `packaging/Atomic.spec` sets `hiddenimports=[]` and
`excludes=[]` (lines 80, 84) - no PyInstaller excludes at all. PyQt6's
default hook is notoriously inclusive; nothing has ever been checked
for whether unused Qt submodules, translation files, or other
transitively-pulled dependencies are being bundled unexamined.
`.claude/rules/testing.md`'s own performance work (pre-1.4 item #15)
found cold start is "mostly PyInstaller's onefile unpack" and explicitly
rejected switching to onedir as not worth losing single-file
distribution - this is a *different* lever on the same cost: same
onefile format, smaller payload, if there's unused weight to trim.
**Why now** - Unmeasured, so explicitly investigate-first, not a
build-it item. Given the exe is already a 47.7MB self-contained
download that gets re-fetched on every update (item #13 above makes
update-checking more visible, which makes download size marginally more
salient), and the file is already under antivirus scrutiny (item #9) -
a smaller, more deliberately-scoped bundle is a plausible win on both
counts, but only if real unused weight is actually found.
**Owner** - release-engineer
**Where** - `packaging/Atomic.spec` (`Analysis(...)`, 65-87) - compare
its output's bundled-package list (PyInstaller can report this) against
what `src/` actually imports; likely candidates given nothing has ever
been excluded: unused Qt modules (QtNetwork, QtSql, QtTest, QtQml if
pulled by the PyQt6 hook despite not being used - `main.py`/`windows/`/
`helpers/` use only QtCore/QtGui/QtWidgets, confirmed by grep for
`from PyQt6` imports), and Pillow's less-common format plugins.
**Done when** - a measured before/after exe size and cold-start time
(same method as pre-1.4 item #15: three runs, `%APPDATA%` redirected to
a copy) with a concrete list of what was excluded and confirmation the
app still runs correctly end to end - not a guess at what's safe to
drop. If nothing meaningful is found, that's a valid, recorded
conclusion too, same as item #15's "nothing to fix" verdict.
**Risk** - excluding something actually needed silently breaks a
feature that isn't covered by a quick smoke test (a rarely-used Qt
submodule, an image format plugin only one site's covers happen to use)
- verify broadly, not just "the app launches," before trusting an
exclude list.

---

## Deliberately not doing

- **Not adding a second watch-progress source (Kitsu or otherwise).**
  This is the pre-1.4 plan's item #17, and it is superseded, not
  merely stale: 1.4's "Stremio only" decision (VDD-1.4 §5) was made
  *after* four alternatives were tried and removed, each because a
  source that's silently wrong is worse than no source, and with more
  than one there's no way to tell which is right. `integrations.md` is
  explicit: "this is settled; do not add a second source without a way
  to tell which one is right." Item #1 above makes the *existing* single
  source fail more honestly; it does not reopen this question.
- **Not touching Amazon Prime, general performance, or code-signing's
  purchase decision beyond tracking it (item #9).** All three are
  measured/priced standing facts per `planning.md` - Prime rejected on
  0/3 real-title coverage, performance found nothing to fix, signing
  priced and awaiting the owner. Re-investigating any of them without
  new evidence would contradict work already done; item #26 is a
  narrower, different question (bundle contents, not general perf) and
  says so explicitly.
- **Not tracking code signing any more (struck-out item #9).** The
  owner removed it from this plan on 16 August 2026. The research stands
  and does not need redoing: Azure Artifact Signing ~$9.99/mo with no
  hardware token, EV certificates ~$249-325/yr and no longer worth it
  since March 2024 (`planning.md`'s standing facts). The Defender
  false-positive it addressed is real but has been lived with since 1.2.
  Raise it again only if the owner asks.
- **Not capping the on-disk cover cache (struck-out item #25).** Also
  removed by the owner. The finding stands - `images.py`'s `CACHE_DIR`
  grows without limit, unlike the in-memory pixmap cache which is
  bounded at 512 - and it costs disk, never correctness, which is why it
  was last on the list before being cut. Worth re-raising only if the
  cache is ever measured large enough to matter.
- **Not adding a light theme.** `ui.md`: six of the app's colors are
  "fixed by the app's colour spec" - this is a deliberate, singular
  design, not an oversight, and nothing in this session's reading of
  the app or its rules suggested otherwise.
- **Not building a Websites-URL liveness check to match item #16's
  local App-path check.** A network probe needs the same
  deadline/fail-soft discipline `probe_site` already has (pre-1.4 item
  #12) - real cost, not free the way `Path.exists()` is. Worth sizing
  on its own if wanted, not folded into #16 for free.
- **Not scoping items #22 (bulk actions) and #24 (global search)
  beyond "investigate first."** Both are genuine, reasonable ideas with
  unproven real-world need on this specific library and real
  interaction-design questions (selection vs. drag-reorder; one search
  box vs. six) that a plan shouldn't guess the answer to. Sizing them
  further now would be inventing a shape rather than discovering one.
- **Not touching `home.py`'s carousel/hero mechanics, `theme.py`'s
  palette, or any of the already-fixed DPI/cursor traps.** No new
  defect or gap was found in any of them this session - `native_cursor.py`,
  `widgets.py`'s toast/Card, `title_match.py`, `release_schedule.py`,
  `wikidata.py`, `updater.py` and `uninstall.py` were all read and are
  in good shape; re-touching them without a finding would be scope
  without a stated cost, which `planning.md` rules out.
- **Not releasing.** Everything above lands on `development` as normal
  patch bumps per `CLAUDE.md` rule 4; nothing here is a release
  decision.

## Could not settle

- **Exact item count.** 26 items after the owner's revision - 24 from
  the replan (26 written, #9 and #25 struck out) plus #27 and #28 raised
  by the owner. The replan was asked for 28-32 and stopped at 26 because
  every item here was found by reading real code (file/line citations
  throughout); reaching the target would have meant items without a
  stated cost, which `planning.md` explicitly rules out ("an item whose
  cost can't be stated doesn't belong... a plan containing everything is
  not a plan"). Flagged rather than silently padded.
- **Item #15's actual shape** (Settings restructuring) - genuinely
  can't be sized further without someone looking at the dialog's actual
  groupings and a taller-dialog-vs-tabs tradeoff, which is why it's
  investigate-first rather than a concrete redesign.
- **Whether #18 (Apps import parity) reduces to a small change or a
  real new scanning module** - depends entirely on whether Start Menu
  `.lnk` scanning turns out as clean as Games' launcher-library scanning
  was; sized as "spans modules" on the assumption it's the latter, but
  could turn out smaller.
