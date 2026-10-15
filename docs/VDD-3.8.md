# Atomic — Version Description Document

**Version 3.8** · 15 October 2026

---

## 1. Identification

| | |
|---|---|
| System name | Atomic |
| Version | 3.8 |
| Release date | 2026-10-15 |
| Repository | https://github.com/Khaled-Alneef/Atomic |
| Target platform | Windows 10 / 11, 64-bit |
| Delivered as | `Atomic.zip` — one **GitHub release asset** holding the ~11 MB **bridge installer** as its only `Atomic.exe` and the application **as a folder**, packed as `app.zip` |
| At the tag | `Atomic.exe` / `Atomic.zip` — the same bridge, for installs predating 2.0 |
| Asset size | 136,030,859 bytes |
| Asset SHA-256 | `8ba495b58d62f7b65991a7f66fcbe1190b89935870cb35c937524aa01654e661` |
| Bridge SHA-256 | `851249b6525a9b5d38efdb30a5d97a5803ad61e74023fd5b35fcb8db33b0b4d8` (11,179,603 bytes) |
| Preceded by | 3.7 — `docs/VDD-3.7.md` |

---

## 2. System overview

Unchanged from 3.7.

---

## 3. Inventory of materials released

Unchanged from 3.7 §3, plus `src/helpers/content_words.py`. The two
shipped seeds (`helpers/anime_genre_seed.py`, `helpers/catalog_seed.py`)
are now stored as encoded blocks written by the test skill's
`seed_blob.py`; they expose the same names as before. Two unused
payloads leave the bundle: Qt's text-to-speech library and its QML
plugin, and Pillow's font module (`Atomic.spec`, `_DEAD_WEIGHT` /
`_DEAD_DATA`).

**Changes since the last release** (`git log released/3.7..development`):

| Commit | Summary |
|---|---|
| `5a30049` | Atomic 3.8 |

---

## 4. What this version provides

**Anime genre filters answer at once.** A genre tick on the Anime page
draws its cards from the catalogue index the moment the index holds any
row, rather than waiting up to 2s on a catalogue walk.

**More anime under each genre.** The anime genre table carries every
genre AniList files (Slice of Life, Supernatural, Psychological, Mecha
and Mahou Shoujo were missing) and five of its tags mapped onto the
filter's names: War, Crime, History, Western and Family.

**Accurate genres on an anime's page.** The details page shows the
genres AniList files for that exact work - matched by exact title and
start year - instead of IMDb's three.

**A title's page no longer hangs on first open.** A film opened for the
first time showed "Loading..." until the refresh button was pressed; it
now shows its information and list as soon as they arrive.

---

## 5. Design notes

**Why War took so long.** His 2,175 anime rows held 2 tagged War: IMDb's
genres rarely say it of an anime, and AniList has no War genre - only a
War tag. With AniList's tags at rank 60 and up (never a spoiler tag):
War 2 -> 77 rows, Crime 146, History 72, Family 152. Measured in process
on a copy of his data: War, Western and Documentary first answers 0.06-
0.08s, against 2.0s before.

**One work's genres, not one title's.** The seed is keyed by title and
unions every work of a name, so the details page asks AniList for its
one work (exact English, romaji or synonym title; start year within one;
closest year wins), keeps the answer a month in
`anime_genre_cache.json` (a miss a day), and goes through
`anilist._post`'s throttle and 403 cooldown. While AniList refuses, the
page keeps the seed's genres, or Cinemeta's.

**The stuck list.** `details._meta_worker` emitted Cinemeta's answer
only when the episode list (`videos`) differed from what the page
showed. A film's meta has no `videos`, so on a first open None compared
equal to None and nothing was emitted - while the worker still saved the
file that a refresh then drew at once. A page showing nothing is now
always answered.

---

## 6. Configuration and user data

New: `anime_genre_cache.json` (per-title AniList genres for details
pages, a month per hit).

## 7. External interfaces

Unchanged from 3.7: AniList (graphql.anilist.co) gains one request per
anime details page per month.

## 8. Installation and removal

Unchanged from 3.7 §8.

---

## 9. Known limitations

Carried forward from 3.7 §9. Cinemeta's anime catalogue includes some
live-action titles, which can appear under the Anime page's genre ticks.
A details page whose title AniList does not match exactly keeps IMDb's
genres.

---

## 10. Verification performed

**Genre ticks, in process on a copy of his data**: first answers for
War, Western, Documentary, Crime, History and Family at 0.06-0.08s.

**The stuck list, reproduced and fixed**: 16 films opened cold (their
disk copies removed) on the frozen build, photographed 3s after each
click - 14 stuck on "Loading..." before the fix, 0 after; the worker
harnessed directly: a cold film emits once, a cached one once, a page
already drawn not at all, a series as before.

**Frozen release build, photographed**: the 3.8 what's-new alert; the
Anime page's War tick at "76 of 119" with cards on screen within a
second; a cold film's details page with facts, genres, cast and the
Play Film row; Death Note's page showing AniList's genres.

**The release build, read back out of its own archive**: "3.8" in
`helpers.updater` and `helpers.development_version_patch`; the new
genre, lookup and logging names present, the superseded ones absent.
Bridge SELFTEST OK.

**Defender**, service Running: exit 0 on the app `Atomic.exe`, the bridge
`Atomic.exe` and `Atomic.zip`.

**The release-notes gate:** three notes written for 3.8.

---

## 11. Glossary

Unchanged from 3.7 §11.
