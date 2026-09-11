# Atomic — Version Description Document

**Version 2.2** · 11 September 2026

---

## 1. Identification

| | |
|---|---|
| System name | Atomic |
| Version | 2.2 |
| Release date | 2026-09-11 |
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

**2.2 is two changes, and they are the same change twice**: input the
user makes at the edge of the app — the wheel in the window's last
pixel column, and the keys the window owns while a web page holds the
keyboard — was arriving somewhere other than the app.

---

## 3. Inventory of materials released

| Item | Description |
|---|---|
| `Atomic.zip` (release asset) | The application. **125,153,104 bytes**. SHA-256 `60994884edc5883e58e0e5d9c8e3014e84f82a3018a048b985a64b8733291c37` |
| `Atomic.exe` (inside that zip) | **125,927,176 bytes**. SHA-256 `c480ddaa98ba92583ecb7123a3db42746442d94c78b7079923fb27db9c9a3ee4` |
| `Atomic.exe` (committed at `v2.2`) | The **bridge installer**, 11,100,466 bytes. SHA-256 `f7a342a05689038f4a221bb060a3394568040d7729d51510968bf373a62daec7` |
| `Atomic.zip` (committed at `v2.2`) | The same bridge installer, zipped, for a zip-preferring updater |
| `src/` | Full source, **125,836 Python lines** across 126 modules, plus 5,947 lines of served static UI |
| `packaging/` | `build.py`, `Atomic.spec`, `check_release_notes.py`, `fetch_libmpv.py`, and `bridge/` |
| `docs/VDD-2.2.md` | This document |

Two commits stand between `released/2.1` and this release: one on the
release skill's own notes, and this version's work.

The bridge is unchanged in purpose and rebuilt from the same source: it
resolves the newest release carrying an `Atomic.zip` asset **at run
time**, walking `/releases`, so the copy committed at `v2.2` will find
2.3 and everything after it without being rebuilt.

---

## 4. What this version provides

**The wheel scrolls with the pointer at the very right edge.** Throw the
mouse at the right of the screen and the pointer lands in the window's
last pixel column, just past the scroll bar; a wheel notch there did
nothing at all on any page.

**Ctrl+F goes to the search bar from a page, and the browser's find bar
is gone.** Pressing it on Home, Movies, Series, Anime, Manga, Games,
Apps, Websites or Downloads had been drawing Edge's own find-on-page bar
over the app. It now focuses the window's one search field, selects
whatever is in it, and moves the keyboard there so the next letter typed
lands in the field rather than in the page.

**And it does nothing where there is no bar to go to** — the episode or
chapter list in full screen, the reader, the player. The field is
`isVisible()` in those states but covered, so focusing it would have put
his typing somewhere he could not see.

**F11 reaches the player and the reader.** Full screen works from inside
them as it does everywhere else.

---

## 5. Design notes

**A native child is sized in device pixels, and the arithmetic was not
the same as the box.** `webview2_host._fit` sized the web view from
`width() × devicePixelRatio` and truncated. Measured on the owner's own
window (2560×1440 physical, DPR 1.25): the page widget occupied x
293..2559 while `int(1813 × 1.25) = 2266` sized the view 293..2558 —
leaving the window's **last pixel column to Qt**, which forwards no
wheel to the view. Three notches posted there wrote no scroll at all,
while the same burst two pixels left of it scrolled. The size now comes
from the host window's own client rect, which *is* the box to fill,
with a rounded-up fallback; the bottom row was one short the same way.

**A web view holds the real keyboard focus, and Qt cannot move it.**
That is the whole of the second change:

- Edge acted on the app's keys first. `AreBrowserAcceleratorKeysEnabled`
  is off now, so Ctrl+F, Ctrl+P, Ctrl+R/F5, Ctrl+S/O/U and F11 do
  nothing in the view — none of them is a feature of these pages. The
  editing keys (Ctrl+C/V/X/A/Z/Y and every movement key) are explicitly
  outside that setting, so the pages' own filter field still copies and
  pastes.
- The forwarding hook was not enough. `AcceleratorKeyPressed` is the
  intended route and did not deliver Ctrl+F on the owner's machine; its
  modifier test also read `GetKeyState`, which answers from the *calling
  thread's* input queue while the key was delivered to the view. It now
  reads the hardware state as well — and, more to the point, the page's
  own `keydown` listener, which has carried F11 and Escape to the window
  since it was written, now carries the window's Ctrl block too.
- A Qt `setFocus()` moves the caret and nothing else. Measured: Ctrl+F
  drew the field's accent ring and caret while the letter typed next
  went to the page. `webview2_host.keyboard_to_qt` takes the Win32
  keyboard back to the window; the player asks for it as it opens, which
  is why F11 reaches the player's own handler.

**Where the bar actually is, rather than whether Qt calls it visible.**
Windowed, the title bar is a strip above the body and the details page
opens inside the body, so Ctrl+F belongs to it. Full screen, the bar is
re-parented into the page container and any overlay is drawn over it;
the reader and the player take the immersive host and cover it in either
state. `MainWindow._search_bar_reachable` is that rule, and nothing
else changed about the shortcut.

**Each hop now writes one line.** `web page key: 'Ctrl+F' from home ->
the window`, `Ctrl+F at the window: bar reachable=…`, `reader key: 'F11'
arrived from the page`, `player key: F11 -> full screen=…`. They are
rare keys, so the log stays quiet, and a key that stops short says
where.

---

## 6. Configuration and user data

Unchanged. No new file, no new field, no migration.

---

## 7. External interfaces

Unchanged from 2.0 (§8 there). No host is added. One WebView2 setting is
turned off (`AreBrowserAcceleratorKeysEnabled`), inside its own `try` so
a runtime older than 1.0.1189 starts as before and says so in the log.

---

## 8. Installation and removal

Unchanged — `docs/VDD-2.0.md` §9. An install running 2.0 or 2.1 updates
in place from the release asset; an install running 1.10 or older is
handed the bridge committed at the tag, which fetches the asset.

---

## 9. Known limitations

Carried forward from 2.0 §10. Three worth repeating here:

- **WebView2's `AcceleratorKeyPressed` hook is still in place and is
  still not what delivers these keys** on this machine. It is kept
  because it is the documented route and costs nothing; the page's own
  listener is what the app relies on.
- A key pressed while the pointer is over a page but the keyboard is
  somewhere else entirely (another application's window) is not the
  app's to receive, and nothing here changes that.
- Code signing (roadmap #8) is still open; the release is unsigned, and
  shipping the zip rather than the bare exe is what makes the download
  come through.

---

## 10. Verification performed

Method: `.claude/rules/testing.md` — measure first, fix, read the
archive back, drive the frozen exe from outside, photograph it.

**The wheel, on the frozen build** (a copy of his data, window set to
his own 2560×1440 rect, bursts of three notches posted at a series of
distances from the right edge):

| | before | after |
|---|---|---|
| the view's right edge | x 2558 | x 2559, flush with the window |
| bursts that scrolled | 7 of 8 | **8 of 8** |
| the last pixel column | nothing in the log | one glide per burst, 300 px each |

Photographed at the last column: Home scrolled from the Watching row
down to Quick Apps and Websites, ~40% of the window's pixels changing
per burst.

**The keyboard**, on the real window against a copy of his data, driven
without touching the pointer (the owner was using the machine):

- With the focus on the Home page and the field unfocused, the key the
  page now sends lands as `focusWidget=QLineEdit`, field focused, in
  both windowed and full screen, and the Win32 focus moves to the app's
  window.
- `_search_bar_reachable` answered **True** on a web page windowed and
  full screen, **False** with the player open in either state, and True
  again after the player closed.
- F11 posted to the player's own window: full screen, then back, both
  times, with `focusWidget=PlayerPage`.
- The page still draws with the new listener in it (`route drawn,
  route=home, rows=33`), and the `app.js` inside the exe is
  byte-identical to the source.

**Read back out of the frozen archive**: `helpers.webview2_host` carries
`AreBrowserAcceleratorKeysEnabled`, `GetAsyncKeyState`, `keyboard_to_qt`
and `SetFocus`; `main` carries `_search_bar_reachable` and
`keyboard_to_qt`; `windows.player` carries `keyboard_to_qt`;
`APP_VERSION` reads `2.2`.

**Not exercised here**: the physical keystroke with the window in the
foreground — the owner was at the machine throughout and the rig was
kept off his keyboard and pointer. Every hop before and after the press
was measured separately, and **the owner confirmed the finished build on
his own hands** before this release was cut.

**Defender**: `MpCmdRun -Scan -ScanType 3` over the release exe, twice,
clean both times (`WinDefend` running, `-DisableRemediation`).

---

## 11. Glossary

See `docs/VDD-2.0.md` §12.
