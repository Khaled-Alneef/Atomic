# Atomic — Version Description Document

**Version 3.13** · 29 October 2026

---

## 1. Identification

| | |
|---|---|
| System name | Atomic |
| Version | 3.13 |
| Release date | 2026-10-29 |
| Repository | https://github.com/Khaled-Alneef/Atomic |
| Target platform | Windows 10 / 11, 64-bit |
| Delivered as | `Atomic.zip` — one **GitHub release asset** holding the ~11 MB **bridge installer** as its only `Atomic.exe` and the application **as a folder**, packed as `app.zip` |
| At the tag | `Atomic.exe` / `Atomic.zip` — the same bridge, for installs predating 2.0 |
| Asset size | 136,107,124 bytes |
| Asset SHA-256 | `dd72f44a5ac14626375b7891d159bb07445d28f21e16a837c83bfa7e877814be` |
| Bridge SHA-256 | `b143213f3c7aa86555013654788785f02d83be1b81beb8e9a099feedd231317c` (11,179,157 bytes) |
| Preceded by | 3.12 — `docs/VDD-3.12.md` |

---

## 2. System overview

Unchanged from 3.12.

---

## 3. Inventory of materials released

Unchanged from 3.12 §3.

**Changes since the last release** (`git log released/3.12..development`):

| Commit | Summary |
|---|---|
| 1a0da38 | Atomic 3.13 - apps, websites and games from inside the player and the reader; Steam portraits for new games; the player's panels, buttons and corners restyled |

---

## 4. What this version provides

**Apps, websites and games open from inside the video player and the
reader.** A launcher button - first on the right of the player's lower bar
and of the reader's top bar - lists them in Apps, Websites and Games tabs,
most recently used first, with each one's icon. A press opens it exactly
as Home does. Playback is left running.

**New Steam games get their library poster.** AION2 and Phantom Blade Zero
showed a wide banner letterboxed onto a dark tile; they, and any recent
Steam title, now show Steam's 600x900 library art. Games already saved with
a banner are upgraded once, at launch.

**The player's windows match the rest of the app.** Every panel row and the
episode list are drawn and animated like the Apps & Websites list; hovers
are white glass over the picture, not grey boxes; chips and the playback
speed presets look like tabs; the panels and episode list turn solid while
the video is paused; and window corners are smooth curves.

**Round buttons are squares with rounded corners**, except the continue
button on covers, which stays a circle.

---

## 5. Design notes

**The launcher.** The player builds its panel from `apps.json`,
`websites.json` and `games.json` (`last_used` / `last_played` first); the
reader asks a new `/api/launchers` route built from Home's own rows
(`server._linked`, hoisted out of `_home`, and `_launch_games`, which
prefers a game's square exe icon over its poster). A press goes through
`link_grid.open_link_entry` or `game_launch.run`, so the stamp and Home's
order follow. Escape never reaches the reader's page
(`webview2_host._accelerator` takes it), so the page tells the shell the
menu is up and `web_reader._escape_pressed` closes the menu before the
reader.

**Steam's hashed art.** Measured 29 October 2026: AION2 (3393110) and PBZ
(4115450) 404 on every guessable portrait path - Steam keeps a new title's
files under a content hash - so `_art_url` fell to appdetails' landscape
header and `_as_poster` letterboxed it. `IStoreBrowseService/GetItems`
names the hashed `library_capsule_2x` keyless (0.8-1.8s; 600x900 for both,
and for Guild Wars 2 the same file its guessed path serves). It is a rung
before the header now (`_store_capsule`); a header reached because that
request *failed* is shown but not cached. `upgrade_letterboxed_covers` runs
in the launch-time image tidy and re-resolves only a `-poster.png` tile
whose cached source is a Steam `header.jpg`, so a Wikipedia logo
(VALORANT) is left alone.

**Rows, hovers and tabs.** `_TileCard` (every `add_row` and every episode
row) and `_LaunchTile` share one clock (`_tile_motion`: 350ms on
cubic-bezier(0.16, 1, 0.3, 1)) and one painter (`_paint_tile_hover`). A row
keeps its own widgets, so the 6px slide is carried by the layout's margins
- left grows as right gives the same back, so wrapped lines never reflow.
The bars are composed per pixel over the video, so the old `SURFACE_HOVER`
fill had landed as an opaque `#1f1f1f` square; hovers are
`_GLASS_HOVER` (white at 38/255) now. `player_top_bar_live_patch.
_clean_controls` appended the old hover after every top-bar button's own
style and so beat any change made in `player.py`; it carries the glass
hover too.

**Smooth corners.** A window region is all-or-nothing per pixel, and
Windows 11's rounded corners are for top-level windows only. `_round_overlay`
now cuts the rounded corners out square and `_SmoothCorners` lays a
per-pixel-alpha, click-through piece over each. Qt clips a widget's own
render to its mask (measured: the panel's corner rendered to an alpha sum of
0 of 112,455), so a piece paints the one-pixel column beside the square,
stretched, cut to the outline by an antialiased mask image, and follows the
owner's DWM alpha. Pieces are redrawn only when geometry or alpha changes.

**`_raise_native` never worked under PyQt6.** It read `internalWinId()`,
which PyQt6 lacks; the AttributeError landed in its own `try`, so the
"raise only when something is above it" test never ran and every call was
a SetWindowPos. `_native_hwnd` reads the handle only once the native
window exists. In a test window: 10 pointer ticks, 10 raises before,
0 after. The corner pieces are not counted as covering their owner.

**Opacity while paused.** `_list_alpha` is 255 while paused and `BAR_ALPHA`
(180) while playing, applied to an open panel and the episode list the
moment pause flips. The two bars are unchanged (made solid for one build,
reverted at the owner's ask).

---

## 6. Configuration and user data

`games.json`: a game whose cover was a letterboxed Steam header has `cover`
rewritten once to the portrait file in `image_cache`, through
`storage.update_entry`. `game_art/` gains the resolved portrait URL under the
existing key. Nothing else new is written.

## 7. External interfaces

One addition: `api.steampowered.com/IStoreBrowseService/GetItems/v1`
(keyless) for a game's library art, and the files it names on
`shared.akamai.steamstatic.com`. Asked only when every guessable portrait
path 404s.

## 8. Installation and removal

Unchanged from 3.12 §8.

---

## 9. Known limitations

Carried forward from 3.12 §9.

- A launch from the launcher leaves playback running; whatever opens comes
  up in front of the player.
- A game in the launcher shows no "not found" note; whether it starts is
  `game_launch`'s to say at the press, as a toast.
- The smooth corners' tint against a translucent panel was checked only in
  a test window, where the panel body captured as solid while the pieces
  captured at its alpha; in the player both are 180/255.

---

## 10. Verification performed

**The frozen build was handed to the owner to test** across the session;
he approved each round and asked for the release. He reported the loading
bar's heavy glyphs fixed on a build of this tree; the session did not
isolate which change fixed them.

**On the frozen build, a copy of his data**: the launch pass logged `game
covers upgraded to Steam's portrait: 2`; AION2 and PBZ became 600x900 files,
looked at and matching his Steam library pictures. A resume from Home
played (`first frame` at 15.3s, no `could not open` / `falling back`).

**Offscreen and in a test window, source tree**: the launcher lists games
newest-played first in both the player and `/api/launchers`; panel alpha
180 playing, 255 paused; a row's margins 12/12 at rest, 18/6 hovered, name
`#cfcfcf` -> `#ffffff`; an Arabic name starts beside the icon; corner pieces
antialiased on all four corners; `_raise_native` 10 -> 0 raises.

**Not photographed on the frozen build by the session**: the launcher in the
player and the reader, the restyled panels, the smooth corners, the square
buttons.

**The release build, read back out of its own archive**: "3.13" in
`helpers.updater`, `helpers.development_version_patch` and the what's-new
notes; `_LaunchTile`, `_TileCard`, `_SmoothCorners` and `_native_hwnd` in
`windows.player`; `_store_capsule` in `helpers.game_art`. Bridge SELFTEST OK.

**Defender**, service Running: exit 0 on the app `Atomic.exe`, the bridge
`Atomic.exe` and `Atomic.zip`, twice each.

**The release-notes gate:** 8 notes written for 3.13.

---

## 11. Glossary

Unchanged from 3.12 §11.
