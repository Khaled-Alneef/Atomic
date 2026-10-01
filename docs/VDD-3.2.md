# Atomic — Version Description Document

**Version 3.2** · 2 October 2026

---

## 1. Identification

| | |
|---|---|
| System name | Atomic |
| Version | 3.2 |
| Release date | 2026-10-02 |
| Repository | https://github.com/Khaled-Alneef/Atomic |
| Target platform | Windows 10 / 11, 64-bit |
| Delivered as | `Atomic.zip` — one **GitHub release asset** holding the ~11 MB **bridge installer** as its only `Atomic.exe` and the application **as a folder**, packed as `app.zip` |
| At the tag | `Atomic.exe` / `Atomic.zip` — the same bridge, for installs predating 2.0 |
| Asset size | 136,678,397 bytes |
| Asset SHA-256 | `e602bd7db0728a1f4887826d887de95efd8beea9308b51aa139e7538eebc88af` |
| Preceded by | 3.1 — `docs/VDD-3.1.md` |

---

## 2. System overview

Unchanged from 3.1. Two fixes: the app no longer opens with the search
bar taking the keyboard, and a season that has only been announced is no
longer listed, offered or played.

---

## 3. Inventory of materials released

Unchanged from 3.1 §3.

**Changes since the last release** (`git log released/3.1..development`):

| Commit | Summary |
|---|---|
| `3ee7326` | The app no longer opens typing into the search bar |
| `51e498b` | An announced season is not an aired one |
| `314cca8` | Atomic 3.2 |

---

## 4. What this version provides

**The search bar waits to be asked.** The app opened with a caret in the
search bar and the first keys typed went into it. The field now takes the
keyboard only from a press on it or Ctrl+F; the page has it at launch.

**An announced season is not a season yet.** Cinemeta lists an announced
season as one episode with no date (Witch Hat Atelier S2E1, Family Guy
S25E1). The details page offered it, and the player stepped from Witch
Hat S1E13 into it and played a wrong file. Now an undated episode past
the last aired one counts as unaired, a season whose first episode has no
date is not listed at all, the player and the card labels stop at the
last aired episode, and a title page opens on its newest aired season.

---

## 5. Design notes

**Qt picks a focus when nobody has.** When a window activates with no
focus widget, Qt hands the keyboard to the first tab-focusable widget,
which was the search field (measured: focus 0.64 s after launch, held).
3.1.1 made it click-focus and gave the page the keyboard before the window
shows; that measured clean on every launch path here, and the owner still
saw it on a full-screen start. 3.2 makes the field `NoFocus` - no automatic
route can reach it - with `main.eventFilter` giving it focus on a press
(and taking the Win32 keyboard back from the web view, as Ctrl+F does).
The fold arrow, the next automatic pick, is `NoFocus` too.

**Undated is not always unaired.** Over the 912 Cinemeta records on the
owner's disk, 700 episodes have no date: 252 sit before a dated episode
that aired (Doraemon, Bullseye - real episodes missing a date), and old
finished shows trail long undated runs that did air (Maya, 165 after
1975). `stremio.unaired_episodes` counts an undated episode unaired only
past the last aired one, and only when the show aired inside
`UNDATED_RECENT_DAYS` (730) or the tail is a new season of at most
`UNDATED_PLACEHOLDER_MAX` (3). `stremio.unannounced_seasons` names the
seasons whose first episode is undated and unaired; the details page drops
them. Every reader of "aired" - the details rows and season pick, the
player's map and episode panel, the server's card labels - asks the one
rule.

---

## 6. Configuration and user data

Unchanged from 3.1 §6. Nothing is rewritten: a watched mark already
written on a placeholder episode stays in `history.json`; every reader
now clamps past it (a Witch Hat card with a `2:1` tick reads S01E13).

## 7. External interfaces

Unchanged from 3.1 §7.

## 8. Installation and removal

Unchanged from 3.1 §8.

---

## 9. Known limitations

Carried forward from 3.1 §9. The undated rule is a heuristic over
Cinemeta's data: a long-running show whose newest real episodes Cinemeta
left undated *and* that aired in the last two years would show them as
UPCOMING. None of the 912 records on the owner's disk does.

---

## 10. Verification performed

**Search focus.** In-process focus trace on a copy of the owner's data,
windowed and full-screen starts, plus minimise/restore and F11: the page
takes the keyboard, the field never does without a press. Frozen 3.2
build, `--startup` with full screen on: typing at launch left the field
empty; a real click then took "bleach" and drew the suggestions; no
"search field took the keyboard" line was written.

**Seasons.** Across the 912 records: 51 titles end at their last aired
episode, 47 lose an announced season, Doraemon's 257 and Maya's 167
undated episodes stay aired. Frozen build, copy of his data: Witch Hat's
card and History read S01E13 and its season list holds Season 1 alone;
Family Guy's ends at Season 24.

**The release build, read back out of its own archive**: "3.2" in
`helpers.updater`, `helpers.development_version_patch` and
`helpers.whats_new`; `window_chrome` carries `NoFocus` and no
`ClickFocus`; `MainWindow.eventFilter` carries the focus log line.
`build.py`: 1,608 entries, 32 bundled files byte-identical to `src/`.
Bridge SELFTEST OK.

**Defender**, service Running: exit 0 on the app `Atomic.exe`, the bridge
`Atomic.exe` and `Atomic.zip`, and again on both exes.

**The release-notes gate:** two notes written for 3.2.

**Not measured by me.** The owner's own sign-in launch - the path he
reported - could not be reproduced here; the log line is what will name
it if it recurs. Playing past Witch Hat S1E13 end to end was not run.

---

## 11. Glossary

Unchanged from 3.1 §11.
