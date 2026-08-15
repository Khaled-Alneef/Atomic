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

**Diagnosed defects, fix deferred.** These are the cheapest real work
available; causes are known and measured.

| # | Defect | Where |
|---|---|---|
| 1 | Catastrophic-backtracking regex + unbounded reads, identical to the ones fixed in `anime_sites.py` | `manga_sites.py` `_AJAX_SEARCH_CARD_RE` (~252), `.*?` (~102), `resp.read()` (~184/194) |
| 2 | Unbounded `resp.read()` on the page-load path; a slow-drip host never returns and 4 of them drain `lookup_pool` | `anilist.py` ~110, `stremio.py` ~35/89/117, `tvmaze.py` ~47, `mangadex.py` ~94, `images.py` ~44 |
| 3 | A dead host costs ~24s per resolve - 3 engines + generic scraper, each its own 6s timeout, no deadline across the chain | `anime_sites.resolve_page_url` |
| 4 | Video-site resolution fires a bare `threading.Thread` per debounced keystroke, outside `lookup_pool` | `tracker.py` `_start_video_site_resolution` |
| 5 | Any exception escaping a Qt slot aborts the process (`qFatal`, exit `0xc0000409`, no traceback), and the app has **no logging at all** | app-wide; the ~20s startup crash was one instance |
| 6 | No backup/export, and unparseable JSON silently loses data - a BOM'd `settings.json` failed to parse, `storage.load` returned `{}`, and the app overwrote the file, destroying the stored AniList username | `storage.py` |
| 7 | AniList rate-limits into a **403 on every POST** (not 429) for over an hour; every lookup fails soft, so it is indistinguishable from "no result". Crunchyroll resolution depends on it with **no second source** | `anilist.py`, `anime_sites.py` |
| 8 | Releases flagged `Trojan:Win32/Wacatac.B!ml` - proven a false positive by bisection (1.0→1.1.5 clean; 1.2 flagged, differing from clean 1.1.5 by one line). Unsigned, no download reputation | packaging; durable fix is code signing |

`docs/VDD-*.md` §10 of the newest release carries the same list in prose
and is the place to check for anything added since.

**Usability gaps seen with the owner.**
- Watch progress on Netflix and Crunchyroll cannot be read at all - no
  public history API, login/ToS wall. AniList is the only "watched
  anywhere" source, and nothing in the UI says that an empty AniList
  username is why nothing syncs.
- No search or filter on tracker pages.
- Adding a site gives no sign whether it will resolve to title pages or
  only ever fall back to search.
- Bumping progress needs the Edit dialog; no quick +1.

**Amazon Prime, if it comes up.** Wikidata property **P8055** ("Amazon
Prime Video ID", US) exists and works - *The Boys* → `B07QQQ52B3`; a
newer **P14440** ("Prime Video ID") also exists. But coverage for anime
looks thin: Hunter x Hunter and Vinland Saga both carry a Netflix id
(P1874) and **no** Prime id. Measure coverage across real tracked titles
before committing to the feature; the Netflix pattern in
`helpers/wikidata.py` is what to copy if it holds up.

## Depending on someone else's service

Any item resting on an outside service needs a line on what happens when
it says no. This project has already shipped a feature that silently did
nothing because AniList began answering 403 and every lookup fails soft.
"Falls back to X" is an answer; "should be fine" is not.

## Sizing

Say whether an item is contained, spans several modules, or has an
unknown shape and needs an investigation task first. No hour estimates -
they survive nothing here.
