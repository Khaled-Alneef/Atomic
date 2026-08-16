# Planning rules

Read before writing or reordering `docs/ROADMAP.md` (`architect`).

## Plan only when asked

Planning is a requested activity, never a reflex. Don't open the roadmap
because work looks unplanned, don't re-prioritise on your own initiative,
and don't turn a bug report into a planning exercise - a diagnosed defect
goes straight to its owner. The Manager dispatches `architect` when the
user asks for a plan, a roadmap, or "what should we do next"; otherwise
the roadmap is read, not rewritten.

## Ordering

Correctness before capability, without exception: anything that loses
data, crashes, hangs, or silently reports a wrong answer outranks any
feature. Within correctness, prefer defects already diagnosed - their
cause is known, so they cost a fix rather than an investigation.

Say what you are deliberately *not* doing, and why. A plan containing
everything is not a plan.

## Standing facts - already established, do not re-derive

**Diagnosed defects.** Numbers 1-7 were **fixed in the 1.3.1 correctness
pass** (roadmap items 1-8, each carrying a `**Landed**` note with what
was measured). Do not re-propose them; do read the roadmap block before
assuming how one was solved, because three were solved differently from
the plan.

| # | Defect | State |
|---|---|---|
| 1 | Catastrophic-backtracking regex + unbounded reads in `manga_sites.py` | fixed - `_ajax_search_cards` splits on `</a>` instead of scanning (224ms → 0ms on a malformed 1MB page) |
| 2 | Unbounded `resp.read()` on the page-load path | fixed - one shared `helpers/net.py`, seven call sites, all bounded at 6.0s against a one-byte-per-second server |
| 3 | A dead host costs ~24s per resolve | fixed - one deadline across the whole chain, 12s measured (2x timeout, deliberately not 8s) |
| 4 | Bare `threading.Thread` per debounced keystroke | fixed - `lookup_pool.submit_latest`, one worker, superseded jobs dropped |
| 5 | Exception in a Qt slot aborts the process; no logging at all | fixed - `helpers/logs.py` + `install_excepthook`; control run without it still dies `0xC0000409` |
| 6 | Unparseable JSON silently loses data (the BOM'd `settings.json`) | fixed - utf-8-sig, quarantine, atomic write, `.bak` |
| 7 | AniList's 403 indistinguishable from "no result"; Crunchyroll had no second source | fixed both halves - `anilist.RateLimited`, and Crunchyroll now resolves via Wikidata **P11330** |
| 8 | Releases flagged `Trojan:Win32/Wacatac.B!ml` - proven a false positive by bisection (1.0→1.1.5 clean; 1.2 flagged, differing from clean 1.1.5 by one line). Unsigned, no download reputation | **open** - packaging; durable fix is code signing |

`docs/VDD-*.md` §10 of the newest release carries the same list in prose
and is the place to check for anything added since.

**Watch progress has one source and cannot easily get another.** It is
personal watch history, so no public knowledge base can supply it -
Wikidata solves *links*, never *progress*. Measured during the item #8
research: **Kitsu**'s JSON:API answers without a key (the one real
candidate, now roadmap item #17); **MyAnimeList v2 403s without a client
id**. Don't re-derive this.

**Usability gaps seen with the owner.**
- Watch progress on Netflix and Crunchyroll cannot be read at all - no
  public history API, login/ToS wall. AniList is the only "watched
  anywhere" source, and nothing in the UI says that an empty AniList
  username is why nothing syncs.
- No search or filter on tracker pages.
- Adding a site gives no sign whether it will resolve to title pages or
  only ever fall back to search.
- Bumping progress needs the Edit dialog; no quick +1.

**Amazon Prime: measured and rejected, do not re-propose.** Coverage was
measured across the owner's real tracked titles (roadmap #14): **0 of 3**
carry P8055 or P14440 - and 0 of 3 carry a Netflix or Crunchyroll id
either, though all three exist on Wikidata with exact label matches.
Wikidata's streaming ids sit on headline and older titles; a library of
current seasonal anime falls outside them entirely. Building Prime would
resolve nothing here, and unlike Netflix there is no second public
source. Revisit only on new evidence - a materially different library,
or visibly improved coverage. "Prime would be nice" is not evidence.

**Performance: measured, nothing to fix** (roadmap #15). Frozen exe cold
start 1.29-1.57s, mostly PyInstaller unpack and Qt init; tracker redraw
0.3-0.4ms per card, linear to 500 entries; covers are not a cost
(pixmaps are cached). Don't open a performance item without a new
measurement contradicting this.

**Code signing: priced, awaiting the owner's decision** (roadmap #16).
Azure Artifact Signing is ~$9.99/mo with no hardware token and is open
to individuals; EV certs are ~$249-325/yr and, since March 2024, no
longer grant the instant SmartScreen pass that was their whole point.
The purchase itself is the owner's to make.

## Depending on someone else's service

Any item resting on an outside service needs a line on what happens when
it says no. This project has already shipped a feature that silently did
nothing because AniList began answering 403 and every lookup fails soft.
"Falls back to X" is an answer; "should be fine" is not.

## Sizing

Say whether an item is contained, spans several modules, or has an
unknown shape and needs an investigation task first. No hour estimates -
they survive nothing here.
