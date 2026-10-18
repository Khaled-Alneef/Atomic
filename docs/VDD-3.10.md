# Atomic — Version Description Document

**Version 3.10** · 18 October 2026

---

## 1. Identification

| | |
|---|---|
| System name | Atomic |
| Version | 3.10 |
| Release date | 2026-10-18 |
| Repository | https://github.com/Khaled-Alneef/Atomic |
| Target platform | Windows 10 / 11, 64-bit |
| Delivered as | `Atomic.zip` — one **GitHub release asset** holding the ~11 MB **bridge installer** as its only `Atomic.exe` and the application **as a folder**, packed as `app.zip` |
| At the tag | `Atomic.exe` / `Atomic.zip` — the same bridge, for installs predating 2.0 |
| Asset size | 136,045,472 bytes |
| Asset SHA-256 | `cbff8a07be0b989e5a09ad80e2063febc42b23ed0c6e6caf115f11979d17ed33` |
| Bridge SHA-256 | `8d0b6c5353af015fb007cff2f1c981d6592fbbef9a072a33427df318ec8f6644` (11,179,941 bytes) |
| Preceded by | 3.9 — `docs/VDD-3.9.md` |

---

## 2. System overview

Unchanged from 3.9.

---

## 3. Inventory of materials released

Unchanged from 3.9 §3.

**Changes since the last release** (`git log released/3.9..development`):

| Commit | Summary |
|---|---|
| `e07f1d5` | A number search finds that number; leaving a chapter early stops its music |
| `716c688` | Atomic 3.10 |

---

## 4. What this version provides

**Continue reading goes back to the spot.** A chapter left part-way is
reopened at the page and place where reading stopped; a chapter read to
its end goes on to the next one.

**Continue watching plays the episode last opened.** Pressing Next and
leaving within seconds no longer sends Continue back to the previous
episode, and a finished episode moves on to the next.

**Highlights in novels.** Select text, right-click, Highlight; select it
again and right-click, Unhighlight. Highlights are kept per chapter.

**A number search is exact.** Typing 11 in an episode or chapter list
(the details page and the player's list) shows 11 only, not 110 or 211.

**The reading music closes on a quick exit.** Leaving a chapter within
seconds of opening it - mouse button 4 being the fast way out - left the
music to start playing behind the app.

---

## 5. Design notes

**Reading place.** Chapters are marked read on open, so marks cannot
tell a half-read chapter from a finished one. The web reader writes
`reading_place.json` (per title: chapter key, the strip child at the top
of the view, the fraction down it, and `done` once the end row came on
screen over loaded pages) 400 ms after a scroll stops, with no
`changes.bump`. `web_pages._place_index` decides Continue for Home's ring
and the details page; the reader re-applies the spot against the child's
real box for up to 8 s while pages above load, stopping at the first
wheel, key or press. A typed progress correction drops the place.

**Episode resume.** `RESUME_MIN_S` (30 s) meant a new episode had no
record until 30 s in. `player.mark_opened` stamps `opened_at` when the
file loads, and `resume_point` lets the newest record decide: a
just-opened episode wins at once, a finished one (its zeroed record now
kept, not dropped) answers None and progress picks the next episode.

**Music race.** The music window is found by a sweep seconds after the
launch; a close before that had no handle. Each stop now bumps a launch
number, and a sweep whose launch is no longer current closes the window
when it appears - only one that did not exist before the launch, and only
while no other title has taken the music.

**Highlights.** Character ranges per paragraph per chapter in
`novel_highlights.json`, merged and cut on the server, drawn as `<mark>`
runs built from the paragraph's text.

---

## 6. Configuration and user data

Two new files in the data directory: `reading_place.json` (at most 200
titles) and `novel_highlights.json`. `player_state.json` records gain an
`opened_at` field, and a finished episode's record is kept zeroed
instead of removed.

## 7. External interfaces

Unchanged from 3.9.

## 8. Installation and removal

Unchanged from 3.9 §8.

---

## 9. Known limitations

Carried forward from 3.9 §9. The reading place applies to the web reader
only; the Qt fallback reader keeps its own `reader_position`. Continue on
an episode left under 30 s in opens it from the start. A scroll in the
last 400 ms before leaving a chapter may not be saved.

---

## 10. Verification performed

**Harness, fabricated data in a temp directory**: E05 half-watched, Next
to E06 at 10 s -> Continue E06; E06 finished -> None (the next episode by
progress), not E05; a correction clears both. Reading place over a
newest-first list: half-way -> same chapter; finished -> the next;
finished on the newest -> the newest with the site check; a correction ->
none. Highlight route: overlaps merge, a removal cuts a hole, clearing
all empties the file, malformed spans are dropped. Number query: "11",
"ep 11", "ch. 11", "#11", "11.5" parsed; "2160p", "s01", "11a" not.

**Tested by the owner on the frozen build** before release.

**The release build, read back out of its own archive**: "3.10" in
`helpers.updater`, `helpers.development_version_patch` and the
what's-new notes; `mark_opened`, `save_reading_place`,
`set_novel_highlight`, `_place_index` and the `place_set` / `highlight`
routes present; `startPlace` and `startHighlights` in the bundled
`app.js`. Bridge SELFTEST OK.

**Defender**, service Running: exit 0 on the app `Atomic.exe`, the bridge
`Atomic.exe` and `Atomic.zip`.

**The release-notes gate:** five notes written for 3.10.

---

## 11. Glossary

Unchanged from 3.9 §11.
