# Atomic — Version Description Document

**Version 3.9** · 17 October 2026

---

## 1. Identification

| | |
|---|---|
| System name | Atomic |
| Version | 3.9 |
| Release date | 2026-10-17 |
| Repository | https://github.com/Khaled-Alneef/Atomic |
| Target platform | Windows 10 / 11, 64-bit |
| Delivered as | `Atomic.zip` — one **GitHub release asset** holding the ~11 MB **bridge installer** as its only `Atomic.exe` and the application **as a folder**, packed as `app.zip` |
| At the tag | `Atomic.exe` / `Atomic.zip` — the same bridge, for installs predating 2.0 |
| Asset size | 136,031,494 bytes |
| Asset SHA-256 | `85e293a1de1f7b2f740ed03250c07806e06283d9a4e5de5806b449c808fe9c6b` |
| Bridge SHA-256 | `f6fb9224d37b5eff9c4ca8904d5b6fafeb739a24fe89a3ba9c17f86869ca8d9e` (11,179,981 bytes) |
| Preceded by | 3.8 — `docs/VDD-3.8.md` |

---

## 2. System overview

Unchanged from 3.8.

---

## 3. Inventory of materials released

Unchanged from 3.8 §3.

**Changes since the last release** (`git log released/3.8..development`):

| Commit | Summary |
|---|---|
| `f8ca003` | Atomic 3.9 |

---

## 4. What this version provides

**Episode ratings stay current.** The per-episode ratings on a title's
page and in the player's episode list are refreshed in the background
once the saved copy is more than a week old, instead of staying as they
were first fetched.

---

## 5. Design notes

**Why they were old.** Both lists asked TMDB only when the disk cache
answered nothing for the season. A season with any cached answer was
never asked again, so the cache's one-week TTL never fired. Measured on
a copy of his rating cache: 162 series, the seasons he watches 47-53
days old (Attack on Titan, Reacher, Demon Slayer), and Brooklyn
Nine-Nine, watched the night before, 7.3-10.7 days.

**The fix.** `ratings.needs_refresh` reads the cache only and answers
whether `episode_ratings` would ask the network (series record or any
season it would read past its TTL). A stale hit is still drawn at once;
a worker fetches behind it, once per season per page, and the list is
redrawn only when a number changed.

---

## 6. Configuration and user data

Unchanged from 3.8. `rating_cache/` is rewritten as seasons are
refreshed.

## 7. External interfaces

TMDB: up to three requests per stale season opened, at most once a week
per season - the rate the cache was always meant to run at.

## 8. Installation and removal

Unchanged from 3.8 §8.

---

## 9. Known limitations

Carried forward from 3.8 §9. Seasons Cinemeta rates itself (mostly
western live action) print Cinemeta's IMDb numbers, which come with the
title's episode list and were not examined in this release.

---

## 10. Verification performed

**Harness, TMDB stubbed, on a copy of his rating cache**: Attack on
Titan's 53-day-old season reports stale, is drawn at once from the cache
and submits one worker; a second draw on the same page submits none;
after the fetch the season reports fresh and a new page submits nothing;
an unchanged answer does not redraw, a changed one does. The player's
panel follows the same path (one worker on a stale hit).

**Frozen build, a copy of his data**: Black Clover's 11-day-old cache
re-fetched within seconds of opening its page; season 2 episode 1 9.4 ->
8.1 and episode 2 unrated -> 7.7, both photographed on the page. The
player's panel was not exercised on the frozen build.

**The release build, read back out of its own archive**: "3.9" in
`helpers.updater` and `helpers.development_version_patch`;
`ratings.needs_refresh` present and called from both
`_season_ratings`. Bridge SELFTEST OK.

**Defender**, service Running: exit 0 on the app `Atomic.exe`, the bridge
`Atomic.exe` and `Atomic.zip`.

**The release-notes gate:** one note written for 3.9.

---

## 11. Glossary

Unchanged from 3.8 §11.
