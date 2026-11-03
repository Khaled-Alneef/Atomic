# Atomic — Version Description Document

**Version 3.16** · 3 November 2026

---

## 1. Identification

| | |
|---|---|
| System name | Atomic |
| Version | 3.16 |
| Release date | 2026-11-03 |
| Repository | https://github.com/Khaled-Alneef/Atomic |
| Target platform | Windows 10 / 11, 64-bit |
| Delivered as | `Atomic.zip` — one **GitHub release asset** holding the ~11 MB **bridge installer** as its only `Atomic.exe` and the application **as a folder**, packed as `app.zip` |
| At the tag | `Atomic.exe` / `Atomic.zip` — the same bridge, for installs predating 2.0 |
| Asset size | 136,121,858 bytes |
| Asset SHA-256 | `a24bfb57f05e8dab15168a7002534a0863a72239592364d11ced8bdc82928a5e` |
| Bridge SHA-256 | `02a55534b586ba84fc50c6111c29caea15854204ca2441bd22ef9cdf66792525` (11,180,056 bytes) |
| Preceded by | 3.15 — `docs/VDD-3.15.md` |

---

## 2. System overview

Unchanged from 3.15.

---

## 3. Inventory of materials released

Unchanged from 3.15 §3.

**Changes since the last release** (`git log released/3.15..development`):

| Commit | Summary |
|---|---|
| fc7655a | The sidebar's right corners are square |
| 63e0f30 | Continue opens at the moment he stopped, never from the start first |
| 8724b5a | Atomic 3.16 - reading banners, logos on disk, chapter lists kept current |

---

## 4. What this version provides

**Continue plays at the moment you stopped.** Every resume opens at the seat
rather than playing from the start first, and stopping saves the exact place,
down to a few seconds in. A title removed from the list keeps its place from
now on.

**Reading banners say what a title is and how far the site has got**: the
medium, the genres where the title has them, and "Last Released Chapter". They
carry no next chapter at all, at the owner's word. Video banners keep their
Expected line. A next chapter that has already come out is hidden on every
other surface and dropped by the Home refresh.

**A banner wears a logo that is on disk**, and asks for one it lacks.

**Chapter lists stay current from Home**, once per title per six hours, and a
refresh no longer cuts a list down to its first page on a host filed under
`www.`.

**The sidebar's right corners are square.**

---

## 5. Design notes

**Why Kingdom said 889.** An estimate for 19 October with 890 read and 893 on
3asq. MangaDex now answers nothing for it, and "an empty answer keeps the old
record" (7 September) kept it for ever. That rule is right for a date that is
late and wrong for a number already out: `release_schedule.spent`.

**Why the logo was missing.** The Eternal Supreme's entry read `hero_logo: null`
beside a backdrop of 31 August, both written by the Qt Home, while the file had
been in logo_cache since 27 September. The backdrop kept the page off
`/api/featured`. `backend.hero_for` falls back to `artwork.cached_logo`; a
banner with none asks `/api/herologo`. The fields the Qt Home wrote are hints,
not verdicts.

**Genres.** A saved reading entry stores none; the verdicts are in
`reading_meta.json`, under the title. `backend._reading_genres` reads them and
`server._refresh_reading_genres` asks MangaDex behind the draw for a title with
no record, once a session. An empty reply never wipes a verdict. Novels are not
asked: MangaDex would answer for a manga of the same name.

**Two bugs the list refresh would have run.** `chapter_source._page_urls`
compared hosts exactly, so a refresh of a `www.` entry kept only the first page
(Survival Story 305 -> 41; 308 now). `_store` dumped a dict another worker was
growing, and lost a refresh on the frozen build: without the lock 2 of 3 rounds
of 240 stores on four threads failed, with it 0 of 3.

**The resume.** `.claude/rules/integrations.md`, "A resume opens at the seat":
Brooklyn Nine-Nine S05E13 at 7:21 over the swarm 16.0s and 29.0s, against 48s
and 65s.

---

## 6. Configuration and user data

`player_state.json` records carry `imdb_id`. Nothing else new is written.

## 7. External interfaces

Unchanged from 3.15. Home now asks each saved reading title's site and MangaDex
behind a draw, at the pace above.

## 8. Installation and removal

Unchanged from 3.15 §8.

---

## 9. Known limitations

Carried forward from 3.15 §9.

- Four of the owner's eight reading titles have no genres, because MangaDex
  answers nothing for them; their banners read the medium alone.
- 3asq lists only its newest rows now: Kingdom 48 (846-893), One Piece 43, where
  Kingdom held 381 on 2 September. No page links were found; older chapters may
  be unreachable from those lists.
- The head-first path in `player_resume_latency_patch` (`_load_from_head`,
  `_arm_head_watchdog`, `_begin_seat_poll`, `_seat_data_ready`, `_seat_blind`)
  is unreachable and left in place, pending the owner's word (CLAUDE.md rule 11).

---

## 10. Verification performed

**The owner tested the builds and approved this for release.**

**On the frozen build, a copy of his data, on the 1080p monitor with his own
Atomic open and untouched:** One Piece, Kingdom, The Eternal Supreme, The World
After The End, Swordmaster's Youngest Son and Black Clover photographed in
rotation; no test window on his 2K panel, the foreground never taken. Survival
Story, The Great Mage and The Beginning After The End photographed on the build
before, which differs only by the next-chapter lines.

**Not exercised:** a novel (none saved) and a Discover banner. The final release
build was read back, not launched; it differs from the photographed build by the
version numbers and the what's-new notes.

**The release build, read back out of its own archive**: "3.16" in
`helpers.updater`, `helpers.development_version_patch` and `helpers.whats_new`,
no "3.15.2" in any; `_reading_genres`, `_latest_line` and "Last Released
Chapter" in `web.backend`, no `MEDIUM_MANGA`; `_refresh_reading_genres` and
`_refresh_chapter_lists` in `web.server`; `_store_lock` and `latest_number` in
`helpers.chapter_source`. Bridge SELFTEST OK.

**Defender**, service Running: exit 0 on the app `Atomic.exe`, the bridge
`Atomic.exe` and `Atomic.zip`, twice each.

**The release-notes gate:** 7 notes written for 3.16.

---

## 11. Glossary

Unchanged from 3.15 §11.
