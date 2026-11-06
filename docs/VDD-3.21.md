# Atomic — Version Description Document

**Version 3.21** · 7 November 2026

---

## 1. Identification

| | |
|---|---|
| System name | Atomic |
| Version | 3.21 |
| Release date | 2026-11-07 |
| Repository | https://github.com/Khaled-Alneef/Atomic |
| Target platform | Windows 10 / 11, 64-bit |
| Delivered as | `Atomic.zip` — one **GitHub release asset** holding the ~11 MB **bridge installer** as its only `Atomic.exe`, and the app's folder packed as `app.zip` |
| At the tag | `Atomic.exe` / `Atomic.zip` — the bridge, for installs predating 2.0 |
| Asset size | 136,191,764 bytes |
| Asset SHA-256 | `b5872a736794fb4eef91f56ea32b48d93c425f970656c724eed5a3f63890eac8` |
| Bridge SHA-256 | `a2a03b6c93835d470fc4833e8f665fbabbc25fd6ee1b271af84a905af8b5cdc8` (11,180,284 bytes). The asset's bridge (11,179,914 bytes) is a separate build of the same source; PyInstaller is not byte-identical twice |
| Preceded by | 3.20 — `docs/VDD-3.20.md` |

---

## 2. System overview

Unchanged from 3.20.

---

## 3. Inventory of materials released

Unchanged from 3.20 §3, plus two modules: `helpers/notifications.py` (what the
bell lists) and `helpers/notification_panel.py` (its window).

**Changes since the last release** (`git log released/3.20..development`):

| Commit | Summary |
|---|---|
| ff9d616 | Arabic subtitles for One Piece: its broadcast numbering, and AnimeTosho's new host |
| ec0d979 | Anime Tosho streams again, from its new host, and only the right episode |
| bb861b0 | A notifications bell for new episodes and chapters of what he saved |
| 9f3b546 | Atomic 3.21 |

---

## 4. What this version provides

**A notifications bell** left of the search bar. Its window lists new episodes
of saved shows and new chapters of saved readings, each with its cover and how
many episodes or chapters are waiting ("(2 Unread Chapters)"), and opens the
title when pressed; a new Atomic version is listed too. The count rides the
bell's corner like Downloads'. The bell rings - it swings on its crown with
swooshes - when the pointer reaches it, when it is pressed, and when something
new arrives. A Clear Notifications button empties the list.

**Switches.** Settings > Notifications turns new episodes and chapters, and new
versions, on and off; with both off the bell leaves the bar. Each saved title
has its own bell beside Save on its episode or chapter list.

**One Piece and other long anime get Arabic subtitles again.** OpenSubtitles and
the fansub groups number One Piece 1-1165 where the app's list splits it into 23
seasons; the episode is now asked for both ways.

**SubDL no longer lists other episodes' subtitles**, and **Anime Tosho is back**
for subtitles and anime streams.

---

## 5. Design notes

**The broadcast number** (`helpers/episode_order`) places a Cinemeta episode in
TVmaze's broadcast order by air date, keeping only matches that run in order and
filling a gap only where both sides agree. Counting Cinemeta's seasons is wrong
for 590 of One Piece's 1,179 episodes (its list has holes and doubles); the map
placed all 1,179, matched opensubtitles.org's own titles at 15 sampled points,
and equals the season count on five shows numbered by season.

**Anime Tosho moved**: `feed.animetosho.org` is NXDOMAIN at Google's and
Cloudflare's resolvers, so both the subtitle source and the stream indexer had
answered nothing for every show. They ask `feed.animetosho.net`; attachments are
relative `/download/<id>/subs/file/<n>` links. Past season 1 a stream row must
state its season, and a one- or two-word title must appear whole - the live feed
offered an East Blue pack for S22E59 and "May I Ask for One Final Thing" for
One Piece.

**Notifications read only what is on disk** - the episode lists and chapter
lists Home already keeps current. The first sight of a title is its baseline; an
episode older than two weeks is never news; the waiting count is Home's own
(`_progress_text`). A muted title's baseline keeps moving, so unmuting brings no
backlog.

**The window** is a tool window owned by the main window. A Popup's mouse grab
and a tool window both left Qt's idea of which window the pointer was in wrong
after closing - measured on the owner's machine, the sidebar read not-hovered
for 4.6s until a second press - and a native child was buried by the page's own
native window within milliseconds. It closes on any press outside it (Qt's
events in the app; Windows' button state elsewhere every 15ms, with the
pressed-since bit), and once the press is released the main window takes and
gives back the mouse capture, as the second press did.

---

## 6. Configuration and user data

New: `notifications.json` (the list and each title's baseline). New keys in
`settings.json`: `notifications_enabled`, `update_notifications`. New field on a
saved entry: `notify` (false when its own switch is off; absent means on).

## 7. External interfaces

`feed.animetosho.net` replaces `feed.animetosho.org` (subtitles and streams).
TVmaze's episode list (`/shows/<id>/episodes`) is read for the broadcast number,
cached per title for a day. Nothing else changed from 3.20.

## 8. Installation and removal

Unchanged from 3.20 §8.

---

## 9. Known limitations

Carried forward from 3.20 §9, and:

- **The subtitle and stream changes were not exercised in the live player**:
  playback makes sound, which is the owner's to run (CLAUDE.md rule 14). Rows
  and files were measured through the real search on a copy of his data.
- **Anime Tosho's stream rows for One Piece**: none, since the feed is asked by
  the season's number ("One Piece 59") while its releases are named "- 1144".
- **A spin-off carrying a whole short title** ("Koisuru One Piece") still passes
  the stream indexer's title test.
- **The notifications' hover trace** stays on for 3s after each close of the
  window, logging what Qt sees, in case the hover fault returns on another
  machine.

---

## 10. Verification performed

**The owner asked for this release** after testing the notifications on a
test build against a copy of his data ("works perfectly now").

**Subtitles**, the real search on a copy of his data: One Piece S22E59, S23E05,
S23E06 0 -> 5-7 Arabic rows each from Anime Tosho, first files 157-310 Arabic
cues; Breaking Bad, JJK, AoT, Bleach TYBW unchanged; 29 of 29 release-name cases
for the wrong-episode filter.

**Streams**, `streams.find_streams`: Solo Leveling S02E05 23 Anime Tosho rows of
100, Blue Lock S02E05 14 of 15, Frieren S01E05 3, One Piece S22E59 none.

**Notifications**, offscreen and on the frozen build (second monitor, a copy of
his data): the bell, badge and window photographed; a row opening its title; the
ring filmed (24 distinct frames in 0.92s); covers and counts photographed;
Settings switches and the per-title switch checked offscreen against the copy.
The hover and close behaviour were confirmed by the owner on the test build,
with the trace in its log.

**The release build, read back:** "3.21" in `helpers.updater`,
`helpers.development_version_patch` and `helpers.whats_new`; `_open_notifications`
and `_resync_mouse` in `main`. Both archives test sound; the app folder's exe
inside `app.zip` is byte-identical to the one built.

**Defender**, service Running: no threats on the app `Atomic.exe`, the bridge
`Atomic.exe` and `Atomic.zip`, twice each, full paths.

**The release-notes gate:** 4 notes written for 3.21.

---

## 11. Glossary

Unchanged from 3.20 §11.
