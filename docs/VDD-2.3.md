# Atomic — Version Description Document

**Version 2.3** · 12 September 2026

---

## 1. Identification

| | |
|---|---|
| System name | Atomic |
| Version | 2.3 |
| Release date | 2026-09-12 |
| Repository | https://github.com/Khaled-Alneef/Atomic |
| Target platform | Windows 10 / 11, 64-bit |
| Delivered as | `Atomic.zip` — a **GitHub release asset**, holding one self-contained `Atomic.exe` |
| Also at the tag | `Atomic.exe` / `Atomic.zip` — the ~11 MB **bridge installer**, for installs predating 2.0 (§3, §8) |

---

## 2. System overview

Unchanged from 2.0 — see `docs/VDD-2.0.md` §2. A single-window Windows
desktop application over one person's anime, reading, films, series,
games, applications and websites, with a native video player (libmpv)
over its own torrent engine (libtorrent) and debrid support, a chapter
reader, a download queue, and a locally served web UI (WebView2).

**2.3 is the watching surface**: subtitles the viewer can supply, pick
and keep, and a player that opens into its own loading screen rather
than showing a half-built one first.

---

## 3. Inventory of materials released

| Item | Description |
|---|---|
| `Atomic.zip` (release asset) | The application. **125,534,748 bytes**. SHA-256 `0c3f8bf02f003066f78b21cc8097e0dd1766abf017053f6477f18c2c56914212` |
| `Atomic.exe` (inside that zip) | **126,312,002 bytes**. SHA-256 `5729ab2d01136dd1cdd251e66c100877524accff2fa41e07ec95a46631587dba` |
| `Atomic.exe` (committed at `v2.3`) | The **bridge installer**, 11,099,441 bytes. SHA-256 `794555387d608f2e97a9c0e2f6fcd7522008766ea7bab4ec90adb18fc04b7078` |
| `Atomic.zip` (committed at `v2.3`) | The same bridge installer, zipped, for a zip-preferring updater |
| `src/` | Full source, **127,046 Python lines** across 126 modules, plus 5,947 lines of served static UI |
| `packaging/` | `build.py`, `Atomic.spec`, `check_release_notes.py`, `fetch_libmpv.py`, and `bridge/` |
| `docs/VDD-2.3.md` | This document |

Two commits stand between `released/2.2` and this release: the
subtitle work of 11 September and this version's player-open fix.

The bridge is unchanged in purpose and rebuilt from the same source: it
resolves the newest release carrying an `Atomic.zip` asset **at run
time**, walking `/releases`, so the copy committed at `v2.3` will find
2.4 and everything after it without being rebuilt.

---

## 4. What this version provides

**The player opens into its loading screen.** Pressing play used to
show, for a fraction of a second, the app's own sidebar and search bar
with a blank page area and the player's seek strip drawn across the
bottom of it — the owner photographed it, and reported the same frame
the day before as "some freezed screen from inside the player". The
first thing on screen now is the title's own still and logo with the
player's bars over it.

**A subtitle file from this device.** "Add Subtitle File..." in the
Subtitles panel takes an `.srt`, an `.ass` or a `.zip` out of a release
folder, files it under My Files, remembers it against that episode, and
re-adds it when the episode is reloaded.

**The languages inside the file itself.** The container's own tracks
are read off the file the engine is writing rather than the stream URL,
so Arabic muxed into a release is offered — and choosing one no longer
freezes the picture for seconds while mpv re-reads the file.

**The pick lights up where it was made.** A muxed track chosen from the
Subtitles panel marks that row at once, and the row it replaced stops
being marked.

**Downloads.** A range of episodes takes one subtitle language and one
preferred source for the whole range — the only shape that can be right
for a range — and videos land in a folder named after the title. A
download that found a release but could not start it now says why
instead of failing silently.

**F11 really reaches the player and the reader.** 2.2 said so; the fix
it shipped was never reached when either was opened over a page (§5).

---

## 5. Design notes

**A page that is a native window shows nothing until Qt paints it.**
`PlayerPage` has native children — mpv's video surface, the startup
backdrop, the two bars — and Qt promotes every ancestor of a native
child, so the page itself is an HWND. Between `show()` and its first
paint that HWND carries no pixels of its own and whatever was
underneath shows through, while the bars, being *layered* native
children, compose themselves immediately. That is the frame he
photographed. The loading frame was put up by `_start`, which runs from
`QTimer.singleShot(0, ...)` — one event-loop turn after `open_player`
had shown the page.

Measured on the build he was running, a copy of his data, the screen
sampled at 60 Hz through a resume from a Home card: the press, **287 ms
with nothing on screen changing at all**, then the page as a flat
ground with its two bars and no backdrop and no logo, and the real
loading frame only at **+502 ms**.

The frame is built in two halves now:

- `PlayerPage._prime_artwork()` reads the backdrop and logo in the
  constructor. `artwork.cached()` is three `stat()` calls (0.05 ms) plus
  the decode (2.5 ms for the w780 copy, 37 ms for the full-resolution
  original) and answers for any title this machine has already drawn.
  `artwork.deliver` cannot serve this: it answers on a worker thread a
  couple of hundred milliseconds later, which is the gap being closed.
- `PlayerPage.reveal_loading_frame()` composes, paints and reveals, in
  `open_player`'s own call, immediately after `show()`.

**Composing it while the page is still hidden costs 450 ms.** That was
the first cut, and the owner measured it from the other side — "now
there is a delay when I click to start the ep it takes ~1.5 sec". The
first `_show_loading` of a page measures **~450 ms hidden against 2–4 ms
shown**, on every open rather than once per session. It is not the
artwork (the same 391 ms with the picture set *after* the call) and not
Python (the statements inside `_layout_overlays` sum to 1 ms under
cProfile): it is Qt realising the page's native children against a
parent that is not on screen. So the composing happens after `show()`,
and what keeps the photographed frame from coming back is that
`top_bar` and `controls` are held down until it is painted — the
exposed milliseconds can then show the page he pressed on and nothing
else.

**One `open_player`, because there were two.** `player.open_player` was
not what the app called: `helpers/player_watch_threshold_patch`
replaced it at import with a *copy* carrying one change — no watched
tick on open — and the copy had drifted from the original. It had
neither `web_pages.overlay_opened(page)`, so the Home document under
the player was never put down, nor `webview2_host.keyboard_to_qt(page)`,
so 2.2's F11 fix had never once run when the player opened over a web
page. The difference is `player.MARK_WATCHED_ON_OPEN` now, one flag on
the one line that differs, and the duplicate is gone.

**Subtitles** (11 September, nine asks): the native file picker is
refused deliberately — it runs its own Win32 modal loop and deadlocks
against mpv's native child window — so the picker is Qt's. A muxed
track mpv has not been demuxing costs a refresh seek that re-reads from
eleven seconds back and reads the Cues at the file's tail: 0.00 s on a
local file, **19.2 s over HTTP** and not recovered. Telling mpv the
language before the file opens is what removes it (14.27 s → 0.00 s),
with a control confirming that a language the file lacks still selects
nothing. A reload drops every externally added track, so the file is
re-added at the reload's first frame through the same path that loaded
it.

---

## 6. Configuration and user data

Unchanged. The remembered subtitle pick is written where playback state
already lives; no new file, no migration.

---

## 7. External interfaces

Unchanged from 2.2. No host is added; `artwork.cached()` reads the
existing TMDB artwork cache off disk and makes no request of its own.

---

## 8. Installation and removal

Unchanged — `docs/VDD-2.0.md` §9. An install running 2.0 or later
updates in place from the release asset; an install running 1.10 or
older is handed the bridge committed at the tag, which fetches the
asset.

---

## 9. Known limitations

Carried forward from 2.0 §10 and 2.2 §9. Three that belong to this
version:

- **The press is not acknowledged instantly.** ~340 ms pass between the
  press and the loading screen, most of it before the page exists at
  all; the page he pressed on is what is on screen for them. The
  measured alternative — composing before `show()` — costs 450 ms more
  and was rejected by measurement, not by taste.
- **At worst one 16–18 ms frame** with the page area blank and no player
  chrome in it still occurs on some opens; it is one frame at 60 Hz,
  and it carries nothing of the player.
- Code signing (roadmap #8) is still open; the release is unsigned, and
  shipping the zip rather than the bare exe is what makes the download
  come through.

---

## 10. Verification performed

Method: `.claude/rules/testing.md` — reproduce on the build he tested,
measure, fix, read the archive back, drive the frozen exe from outside,
photograph it.

**Reproduced first**, on the frozen build he photographed, against a
copy of his real `%APPDATA%\Atomic` (taken with `copy_real_data.py`,
outside the desktop app's package), the screen sampled at 60 Hz through
a resume from a Home card:

| | build he photographed | first cut | shipped |
|---|---|---|---|
| press → anything on screen | 287 ms (bare player) | 792 ms | **340 ms** |
| half-built player on screen | **215 ms** | one 23 ms frame | none — at worst one 16–18 ms frame carrying no player chrome |
| press → complete loading screen | 502 ms | 792 ms | **340–353 ms**, bars at +20 ms |

Three opens on the shipped build, two titles, one from the details
page. Playback reached its first frame on every one; no `could not
open`, `falling back` or `stopped mid` in the log.

**Photographed** (2578×1398, his own maximised geometry): the exposed
frame — the app with an empty page area and no player bar in it; the
next frame — backdrop, logo, title and seek strip; and the finished
player 0.55 s after the press.

**F11 from inside the player**, frozen build: maximised
(-9,-9,2569,1389) → full screen (0,0,2560,1440) → back.

**Read back out of the frozen archive**: `helpers.artwork` carries
`cached`; `windows.player` carries `_prime_artwork`,
`reveal_loading_frame`, `MARK_WATCHED_ON_OPEN`, `overlay_opened` and
`keyboard_to_qt`, and no longer `_prime_loading_frame`;
`helpers.player_watch_threshold_patch` carries `MARK_WATCHED_ON_OPEN`
and no longer `threshold_open_player`; `APP_VERSION` reads `2.3`.

**Not exercised**: his laptop (the second machine these reports usually
come from), and a pick made in the details page's source picker — the
details → episode path was, and is near-seamless there, the loading
frame being the same artwork the details page already shows.

**Defender**: `MpCmdRun -Scan -ScanType 3` over the release exe, twice,
clean both times (`WinDefend` running, `-DisableRemediation`).

---

## 11. Glossary

See `docs/VDD-2.0.md` §12.
