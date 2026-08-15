# Atomic — Version Description Document

**Version 1.3** · 15 August 2026

---

## 1. Identification

| | |
|---|---|
| System name | Atomic |
| Version | 1.3 |
| Release date | 2026-08-15 |
| Repository | https://github.com/Khaled-Alneef/Atomic |
| Target platform | Windows 10 / 11, 64-bit |
| Delivered as | `Atomic.exe` — a single self-contained executable |

---

## 2. System overview

Unchanged from VDD-1.2 §2.

---

## 3. Inventory of materials released

| Item | Description |
|---|---|
| `Atomic.exe` | The application. 47,196,781 bytes. SHA-256 `416b9f9d52e6f4deeb6ee001d5eda0bc2c97b8a2e7222a619f9ac59a8c8809ee` |
| `src/` | Full Python source, 10,694 lines across 34 modules |
| `packaging/` | `build.py` and `Atomic.spec`, which produce the executable |
| `docs/VDD-1.3.md` | This document |

Build environment unchanged from VDD-1.1 §3.

---

## 4. Inventory of software contents

One module is new since 1.2:

| Module | Lines | Responsibility |
|---|---|---|
| `helpers/wikidata.py` | 143 | Netflix title ids from Wikidata's public P1874 property |

| Module changed | Lines | What changed |
|---|---|---|
| `helpers/anime_sites.py` | 904 | Netflix resolved through Wikidata first, AniList second; a regional-availability probe before a resolved Netflix URL is accepted |

---

## 5. What this version provides

**Netflix titles open on the show's own page.** 1.2 shipped Netflix
resolution that relied on AniList's `externalLinks`, and it did not
work in practice — Netflix entries still opened the search page.

The reason matters more than the fix. AniList had begun answering `403`
to every request from the development network, and still was more than
an hour later. Because every lookup in this application fails soft, that
block is indistinguishable from "this title has no link": the lookup
returns nothing and the app quietly falls back to a search page. The
1.2 implementation was therefore never verified against a live AniList,
which VDD-1.2 §10 recorded as a known limitation. This release replaces
that dependency for Netflix rather than waiting for it to recover.

Wikidata publishes the Netflix id as property **P1874**, without a key
or an account, and answered for every title tried while AniList was
still blocked. A lookup costs two requests: a title search, then a
single batched entity fetch for the candidates — not one request per
candidate, which the obvious implementation would have cost eight of,
per tracked entry.

AniList's Netflix rows remain as a fallback, so the two sources cover
each other.

---

## 6. Design notes

**Why the candidate set is filtered by "has a Netflix id" before
scoring, not after.** A Wikidata title search for "Frieren" returns a
manga series, a fictional character, a painting by Erik Pauelsen, a
Norwegian television film, and the anime — every one of them labelled
exactly "Frieren", so every one scores identically on label
similarity. Nothing in the label distinguishes them. What does is that
only the anime carries P1874 at all. Ranking by label match alone would
have picked arbitrarily among five, and could pin a tracked show to a
painting. Where scores still tie, the shorter label wins, which
prefers a base series over a sequel or a film spun off it — the same
rule `anilist.py` already used.

**Why a resolved URL is probed before it is saved.** Netflix ids are
global; its catalogue is regional. Frieren's id (`81726714`) is the
correct one and still returns 404 from this region. Saving it would
pin the entry to a permanently dead page — worse than the search page
it would otherwise fall back to. Only an explicit 404 discards the
link; a probe that fails for any other reason keeps it, because the id
came from a source asserting the title is on Netflix and a transient
network error is not evidence against that.

**Why the probe never reads the response body.** Netflix truncates
these responses: a plain `read()` raises `IncompleteRead` on pages that
are perfectly healthy, which would have made every good link look
broken. The status line answers the whole question.

---

## 7. Configuration and user data

Unchanged from VDD-1.2 §7.

---

## 8. External interfaces

As VDD-1.2 §8, plus Wikidata's `wbsearchentities` and `wbgetentities`
endpoints, used to look up a title's Netflix id. Unauthenticated, no
key, and identified by a descriptive User-Agent as Wikidata asks. No
credentials are sent to any service.

---

## 9. Installation and removal

Unchanged from VDD-1.2 §9.

---

## 10. Known limitations

- **A Netflix title unavailable in the user's region falls back to the
  search page.** The id is correct and the show simply is not licensed
  there; the search page is the honest answer.
- **AniList may still be rate-limited into a `403`**, which affects
  Crunchyroll resolution — that path has no second source. Netflix no
  longer depends on it.
- **Wikidata coverage is not universal.** A title with no P1874
  recorded resolves through AniList if it has a row there, and
  otherwise falls back to the search page.
- The limitations carried from VDD-1.2 §10 that were not addressed here
  remain: `manga_sites.py` still carries the unbounded-regex and
  unbounded-read patterns (not reachable from page load), unbounded
  `resp.read()` remains in `anilist.py`, `stremio.py`, `tvmaze.py`,
  `mangadex.py` and `images.py`, and a dead host still costs roughly 24
  seconds per resolution attempt.

### Antivirus note

The 1.2 executable was flagged by Microsoft Defender as
`Trojan:Win32/Wacatac.B!ml`, blocking its download. It was established
by bisection that this was a false positive attached to that specific
binary and not to any code: 1.0, 1.1, a freshly rebuilt 1.1, and the
1.1.3, 1.1.4 and 1.1.5 development builds all scanned clean, while 1.2
— which differs from the clean 1.1.5 build by exactly one line, the
version string — was flagged. A version string cannot make a binary
malicious. This 1.3 executable was scanned before publication and is
clean. The durable remedy is a code-signing certificate; unsigned
self-extracting executables with no download history are what these
heuristics are tuned to distrust, so this can recur.

---

## 11. Verification performed

In addition to VDD-1.0 §11, VDD-1.1 §11 and VDD-1.2 §11:

- Netflix resolution was verified **live** against the real Wikidata
  API. `HunterxHunter` resolves to
  `https://www.netflix.com/title/70300472` — the id from the original
  report — and the spaced spelling `Hunter x Hunter` resolves to the
  same page. Devilman Crybaby, Cyberpunk Edgerunners, One Piece and
  Blue Eye Samurai all resolve.
- Each resolved page was then fetched and confirmed to return **HTTP
  200 with the matching title in the page** (`Watch Hunter X Hunter
  (2011) | Netflix`, `Watch Devilman Crybaby | Netflix Official Site`,
  `Watch Cyberpunk: Edgerunners | Netflix Official Site`) — the id was
  not merely assumed correct because a lookup returned one.
- Frieren was confirmed to resolve to a correct id that 404s from this
  region, and confirmed to fall back rather than save it. A nonsense
  title returns nothing.
- 28 offline checks with both sources stubbed cover routing, URL
  normalization (country and language-country segments, `http`,
  www-less, tracking query, trailing slash, lookalike host), the
  regional-404 fallback, and the AniList fallback. Crunchyroll's
  behaviour, including its preference for the subbed row, was confirmed
  unchanged.
- The frozen executable was opened and `helpers.wikidata` confirmed
  present with the `P1874` constant baked in, and `APP_VERSION`
  confirmed to read `"1.3"` — not inferred from the build log.
- The release executable was scanned with Microsoft Defender before
  publication and reported no threats.
- No test read or wrote the real user data at `%APPDATA%\Atomic`.

---

## 12. Glossary

Unchanged from VDD-1.2 §12.
