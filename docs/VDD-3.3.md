# Atomic — Version Description Document

**Version 3.3** · 2 October 2026

---

## 1. Identification

| | |
|---|---|
| System name | Atomic |
| Version | 3.3 |
| Release date | 2026-10-02 |
| Repository | https://github.com/Khaled-Alneef/Atomic |
| Target platform | Windows 10 / 11, 64-bit |
| Delivered as | `Atomic.zip` — one **GitHub release asset** holding the ~11 MB **bridge installer** as its only `Atomic.exe` and the application **as a folder**, packed as `app.zip` |
| At the tag | `Atomic.exe` / `Atomic.zip` — the same bridge, for installs predating 2.0 |
| Asset size | 136,693,237 bytes |
| Asset SHA-256 | `fd42db2502a9703190a5ecb0f0cd0a1fee5f6a05148bff9341cec0d0fa79881c` |
| Preceded by | 3.2 — `docs/VDD-3.2.md` |

---

## 2. System overview

Unchanged from 3.2. One correctness fix - two works that share a name
no longer open, label or draw as each other - and four visible changes:
a subtitle font choice, redesigned skip and next-episode offers, a drawn
episode-list button, and Epic games finding their Steam poster.

---

## 3. Inventory of materials released

Unchanged from 3.2 §3.

**Changes since the last release** (`git log released/3.2..development`):

| Commit | Summary |
|---|---|
| `0aac466` | Atomic 3.3 |

---

## 4. What this version provides

**A title is its IMDb id, not its name.** "What Women Want" is a 2000
film and three series. Opening the series opened the film on one device
and the reverse on another, because a card was matched to a history row
by title, and a film and a series are both on the Watch side. A card now
opens the entry with its own IMDb id, and a name match never crosses a
different id or the film/series line. The card's episode number and its
artwork follow the same rule.

**Subtitle font.** The player's Subtitles panel has a Font row: Default,
then the readable Arabic-capable fonts installed on the machine (12 here
of a curated 24). It reaches styled `.ass` subtitles as well as `.srt`,
is set live, and is remembered for every title.

**Skip Intro / Skip Recap / Next Episode** are drawn in the monochrome
design: a dark outlined offer for a skip, a white one for Next Episode,
with antialiased corners, sitting further in from the edge out of full
screen. **The episode-list button** in the player is a drawn stack of
episodes.

**Epic games** are named by Epic's own manifest when their art is looked
up, so `TheWitcher3` finds Steam's 600x900 poster; a game added since the
old Games page stopped running is asked for art at all.

---

## 5. Design notes

**Three title-keyed lookups, closed separately.** `web_pages._find` (the
card click), `server._marked_progress` and `_saved_twin` (the card's
number) now take the IMDb id first. `artwork._tmdb_id` had a separate
defect: its `find` loop rebound the caller's `kind`, so every title
fallback after an id miss searched the movie list - the series got the
film's backdrop and logo. With that fixed, an exact-name fallback must
link back to the asked id (`_linked_imdb`); a franchise prefix (Bleach
TYBW) is taken as before. `_recheck_kind` drops art cached the old way,
once per id per install.

**The font has two levers, like the size.** `sub-font` reaches text
tracks; an `.ass` is drawn from its own styles, reached by
`sub-ass-style-overrides=FontName=...`, which keeps every position.
`sub-ass-override=force` shrank the dialogue and is not used.

**The offer composes its own alpha.** The window mask that cut its
corners is one bit per logical pixel, a staircase at 125%. It now pushes
an antialiased face through `UpdateLayeredWindow`, as the bars do, and
never takes the whole-window alpha (`SetLayeredWindowAttributes`) the two
modes cannot share.

---

## 6. Configuration and user data

`settings.json` gains `subtitle_font` ("" is Default). `logo_cache` gains
`kind_checked.json`, the ids whose cached art has been rechecked. Nothing
else is rewritten.

## 7. External interfaces

Unchanged from 3.2 §7. TMDB is asked for `external_ids` on the rare path
where a title fallback follows an id miss, and once per title for the
cached-art recheck.

## 8. Installation and removal

Unchanged from 3.2 §8.

---

## 9. Known limitations

Carried forward from 3.2 §9. The card's "saved" tint is still matched by
title, so a saved series tints a same-named film's card. A sign line in
an `.ass` that names only a style takes the chosen subtitle font too; one
that names its font inline keeps it.

---

## 10. Verification performed

**Same-named works.** Harnessed `_find`: with the film in history every
one of four clicks opened the film, with the series every one the series;
after, each click its own work. Old against new over the owner's 53
library and history titles: no difference; card labels the same 53, no
difference; TMDB ids over his 27 titled entries, no difference, TYBW
unchanged. Frozen build, the film planted in a copy's history and its art
under the series' id: the series card opened the series with no film art
(the recheck's log line written), the film card the film.

**Subtitle font**, measured on the build's libmpv with a generated Arabic
`.ass` and `.srt`: both lines changed, the sign kept its place, set live in
1.5 ms, cleared to a zero-pixel diff. The stepper's drawn arrows: ink
centre 27.5/27.7 of a 27.5 middle, against the typed glyph's 32.5. Tested
by the owner on his build.

**Offers and icons.** Frozen builds: Skip Intro over a bright frame and
Next Episode in the credits, at rest and under the pointer; the owner
tested the per-pixel corners and placement. **Witcher 3:** resolved to
appid 292030's 600x900 poster on the frozen build; the other eight games'
answers unchanged.

**The release build, read back out of its own archive**: "3.3" in
`helpers.updater`, `helpers.development_version_patch` and
`helpers.whats_new`. `build.py`: 1,608 entries, 32 bundled files
byte-identical to `src/`. Bridge SELFTEST OK.

**Defender**, service Running: exit 0 on the app `Atomic.exe`, the bridge
`Atomic.exe` and `Atomic.zip`.

**The release-notes gate:** four notes written for 3.3.

**Not measured by me.** Playing either What Women Want title end to end
(the 2020 series has no live sources); the friend's direction of the
collision on the frozen build (harnessed only).

---

## 11. Glossary

Unchanged from 3.2 §11.
