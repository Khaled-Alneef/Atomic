# Atomic — Version Description Document

**Version 1.2** · 15 August 2026

---

## 1. Identification

| | |
|---|---|
| System name | Atomic |
| Version | 1.2 |
| Release date | 2026-08-15 |
| Repository | https://github.com/Khaled-Alneef/Atomic |
| Target platform | Windows 10 / 11, 64-bit |
| Delivered as | `Atomic.exe` — a single self-contained executable |

---

## 2. System overview

Unchanged from VDD-1.1 §2. The seven sections (Home, Anime, Reading,
Series, Games, Apps, Websites) and their purposes are as described
there.

---

## 3. Inventory of materials released

| Item | Description |
|---|---|
| `Atomic.exe` | The application. 47,191,527 bytes. SHA-256 `3145968943349513b93b680f9283972e46b4174c82b646ea55938f494f49a3a5` |
| `src/` | Full Python source, 10,486 lines across 33 modules |
| `packaging/` | `build.py` and `Atomic.spec`, which produce the executable |
| `docs/VDD-1.2.md` | This document |

### Build environment

Unchanged from VDD-1.1 §3.

---

## 4. Inventory of software contents

Two modules are new since 1.1:

| Module | Lines | Responsibility |
|---|---|---|
| `helpers/lookup_pool.py` | 69 | A shared, bounded worker queue for every per-entry background lookup |
| `helpers/whats_new.py` | 183 | Per-version release notes in the user's terms, and the dialog shown once after an update |

Modules materially changed:

| Module | Lines | What changed |
|---|---|---|
| `helpers/anime_sites.py` | 845 | Sites are a base URL only; per-site search engines plus a generic scraper; the `_STREAMING_SITES` table resolving Crunchyroll and Netflix through AniList |
| `helpers/anilist.py` | 242 | `fetch_external_urls` — per-title links on a named streaming service, from `Media.externalLinks` |
| `windows/tracker.py` | 1762 | Background lookups routed through `lookup_pool`; Save waits on an in-flight page-URL lookup; entries with a site but no URL resolve on first open |
| `helpers/widgets.py` | 529 | `release_hover_cursor` guarded against an already-deleted Qt widget |
| `helpers/app_settings.py` | 175 | Update markers: `updated_from`, `last_seen_version`, `has_run_before` |
| `helpers/settings_dialog.py` | 930 | Video/reading site forms take a plain URL; the outgoing build records what version it was before an update replaces it |

---

## 5. What this version provides

### Anime and series open on their own page

A tracked title now opens the page for that title on the configured
site, rather than the site's search-results page. Adding a site takes
only its address — the hand-typed `/search?q=` prefix 1.1 required is
gone, and the search pattern is derived per site instead.

Resolution is tried in order: a per-site engine where one is known,
then a generic scraper that reads candidate links off the site's own
search page and fuzzy-matches them against the title. A site matching
neither, or a genuine no-match, still falls back to the search page —
that fallback is the honest answer, never the intended target.

### Crunchyroll and Netflix, which cannot be scraped at all

Crunchyroll renders its search client-side and its content API answers
401 without an OAuth bearer; Netflix puts search behind a sign-in.
Neither can be resolved by fetching it. Both are resolved from AniList
instead, which publishes a per-title link for each openly and without a
key, so no account or stored credential is involved on either service.

### A summary of what changed, after an update

Updating from Settings previously replaced the executable and relaunched
with no indication of what had changed. The first launch after an update
now shows a plain-language summary once.

### Fixes

- **Atomic no longer closes itself roughly 20 seconds after opening.**
  The cursor watchdog called `unsetCursor()` on a Home carousel label
  whose underlying Qt object had already been deleted; PyQt6 converts an
  exception escaping a slot into an immediate abort, which presented as
  a hang followed by the process disappearing.
- **The app no longer freezes on an unusual page from a video site.** A
  lazy regex hunting a closing `</a>` that never arrived walked to
  end-of-document once per unclosed tag. Python's regex engine does not
  release the GIL, so this stopped every thread including the UI event
  loop.
- **Background lookups no longer saturate the network.** Page load
  previously started one unbounded thread per tracked entry across three
  loops and three pages; all of them now share a four-worker queue.
- **A fast Save no longer stores an empty link permanently.** Saving
  before a background page-URL lookup returned recorded nothing, with
  nothing to retry it.

---

## 6. Design notes

**Why AniList resolves Crunchyroll and Netflix.** Both were investigated
directly first. Crunchyroll's search page is a Next.js shell whose only
`/series/` links are a static promo carousel; its content API needs an
OAuth bearer. Netflix's search requires a session. An authenticated
Crunchyroll client was researched and rejected: the password-login flow
is dead (current tooling requires a hand-copied browser cookie),
automated access is prohibited by its terms, and it would duplicate
progress data `anilist.py` already provides without any login. AniList
carries the link as ordinary public data, which makes it the only
unauthenticated route to either title page.

**Why a second AniList redirect hop is refused.** Following
Crunchyroll's redirect chain to its end lands on a locale-prefixed URL
(`/ar/series/...` from this machine) — the locale is guessed per visitor
by geo-IP, so saving it would pin every entry to wherever the link was
first resolved. Exactly one hop is followed, and only to a `/series/`
path.

**Why the update summary has three detection cases.** The marker saying
"an update just happened" can only be written by the build being
replaced, and every build released before 1.2 predates that code. So an
explicit marker is preferred, a recorded last-seen version comes next
(covering an executable replaced by hand), and failing both, a profile
that already holds settings from an earlier run is treated as an
upgrade. Without the third case the first update into this feature —
1.1 to 1.2, the one most people will take — would have shown nothing.

**Why response reads are bounded by a deadline, not just a timeout.**
`urlopen(timeout=)` bounds each socket operation, not the transfer, so a
host trickling one byte every two seconds never returns at all. Four
such hosts would permanently drain the shared worker queue.

---

## 7. Configuration and user data

As VDD-1.1 §7, with additions to `settings.json`:

| Key | Meaning |
|---|---|
| `updated_from` | Version being replaced, written by the outgoing build and consumed once by the new one |
| `last_seen_version` | Version that last completed startup |

Video and reading sites now store `base_url`. Records written by 1.1
carrying the older `search_url` are migrated on load.

---

## 8. External interfaces

As VDD-1.1 §8, plus AniList's `Media.externalLinks`, used to resolve a
title's page on Crunchyroll and Netflix. No credentials are sent to any
service, and no API keys are held anywhere in the application.

---

## 9. Installation and removal

Unchanged from VDD-1.1 §9.

---

## 10. Known limitations

- **Netflix resolution is unverified against the live AniList API.** The
  logic is exercised against a stubbed AniList, and Netflix's URL
  normalization is covered by unit checks, but AniList began answering
  `403` to every request from the development network partway through
  this work, so the live shape of its Netflix link rows was not
  confirmed. The failure mode if the rows differ is the pre-existing
  one: no link found, and the search page opens instead.
- **AniList rate-limits into a `403`, not a `429`,** and every lookup
  fails soft, so a rate-limited network is indistinguishable from "this
  title has no link" without checking the HTTP status directly.
- **A site that renders its search results in the browser cannot be
  resolved** unless a third party publishes its links, as AniList does
  for Crunchyroll and Netflix. So can a site whose results carry no
  title text near the link.
- **`manga_sites.py` still carries the unbounded-regex and unbounded-read
  patterns** that were fixed in `anime_sites.py`. Its search paths are
  reached only from the Add/Edit form, not from page load, so the
  20-second freeze is not reachable there — but the defect is present.
- **Unbounded `resp.read()` remains** in `anilist.py`, `stremio.py`,
  `tvmaze.py`, `mangadex.py` and `images.py`, all of which do run on the
  page-load path.
- **A dead host costs roughly 24 seconds per resolution attempt** — three
  engines plus the generic scraper, each with its own socket timeout and
  no deadline across the chain. Bounded and correct, but slow.
- Crunchyroll and Netflix watch progress is not synchronised; AniList
  progress covers those entries where a public username is configured.

---

## 11. Verification performed

In addition to the checks recorded in VDD-1.0 §11 and VDD-1.1 §11:

- The frozen executable's `updater` module was decompiled out of the
  packaged archive and its `APP_VERSION` confirmed to read `"1.2"`,
  ruling out a PyInstaller cache reusing a stale build — the rebuild
  completed in under a second, so this check was not optional.
  `helpers.whats_new` was confirmed present in the same archive, and the
  Netflix literal confirmed present in `helpers.anime_sites`.
- The update summary was driven end-to-end against the **frozen
  executable** with `APPDATA` redirected to a scratch profile: the
  `Atomic Updated` window was confirmed present and visible by window
  enumeration, with the main window simultaneously reporting
  `enabled=False`, which is the signature of a modal dialog above it.
  The marker was confirmed consumed, so the summary shows once.
- 24 checks over the summary logic: version-range selection including a
  skipped version and a downgrade, marker lifecycle, and each of the
  three detection cases plus a genuine first install, which must stay
  silent.
- 22 checks over streaming resolution: host matching including a
  lookalike domain, Netflix URL normalization across country and
  language-country segments, `http`, www-less, tracking query and
  trailing-slash forms, and refusal of a non-Netflix URL. Crunchyroll's
  existing behaviour, including its preference for the subbed row over a
  `-dubs` row, was confirmed unchanged.
- The 20-second crash was reproduced against the pre-fix executable
  (exit `0xc0000409`, faulting module `Qt6Core.dll`) and the fixed build
  confirmed to survive past that point, both with `APPDATA` redirected.
- The regex freeze was measured before and after: a 0.7 MB malformed
  page took 32.51 s to parse before and 0.02 s after; the worst UI stall
  fell from 32.4 s to 0.2 s, and a click left undelivered for 29.5 s was
  delivered immediately.
- Background-lookup concurrency was measured with 360 fabricated entries
  across all three tracker pages: peak simultaneous in-flight lookups
  fell from 651 to 4, with the same 840 total lookups completing, so
  none were dropped.
- No test at any point read or wrote the real user data at
  `%APPDATA%\Atomic`; every run used a copied or fabricated profile, and
  the real profile's modification time was confirmed unchanged
  afterwards.

---

## 12. Glossary

Unchanged from VDD-1.1 §12.
