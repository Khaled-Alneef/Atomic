# Atomic — Version Description Document

**Version 3.11** · 19 October 2026

---

## 1. Identification

| | |
|---|---|
| System name | Atomic |
| Version | 3.11 |
| Release date | 2026-10-19 |
| Repository | https://github.com/Khaled-Alneef/Atomic |
| Target platform | Windows 10 / 11, 64-bit |
| Delivered as | `Atomic.zip` — one **GitHub release asset** holding the ~11 MB **bridge installer** as its only `Atomic.exe` and the application **as a folder**, packed as `app.zip` |
| At the tag | `Atomic.exe` / `Atomic.zip` — the same bridge, for installs predating 2.0 |
| Asset size | 136,079,137 bytes |
| Asset SHA-256 | `5e6564a6c10e3be9142d0f69846b08cfe7f39ee1349a6bfbccd77829486afc0a` |
| Bridge SHA-256 | `c56450df5a62912f981dea66f98e62bdd1d05d6e7c0774bb63c0d8079f8cfb16` (11,179,115 bytes) |
| Preceded by | 3.10 — `docs/VDD-3.10.md` |

---

## 2. System overview

Unchanged from 3.10.

---

## 3. Inventory of materials released

Unchanged from 3.10 §3, plus one module: `helpers/top_nav.py` (a page
menu for the window's bar, built and then set aside - see §5; it ships
switched off).

**Changes since the last release** (`git log released/3.10..development`):

| Commit | Summary |
|---|---|
| (this release's commit) | Atomic 3.11 - the sidebar redesign and a rounded-square pass over every control |

---

## 4. What this version provides

**A redesigned sidebar.** The page on screen sits in a white box that
slides from row to row when another page is opened, with its icon and
label inverting to black exactly where the box covers them. The blocks
carry WATCH / READ / LIBRARY headings in the app's mono eyebrow face, and
the column is one matte colour.

**Saved, Schedule and History move like the sidebar.** The open one is
lit by the same sliding white box; on hover the bookmark draws its tick,
History's arrow runs a full turn toward its head, and Schedule's clock
hand goes once round.

**Rounded squares instead of pills.** Every button, chip, filter field,
sort box, the search field and both sliding boxes use a rounded-rectangle
corner. Circles stay circles.

**Every dropdown list is rounded, with a pointing hand.** The web pages'
selects open the app's own list instead of Chromium's; the Qt combo boxes
and menus open in see-through, frameless windows so their radius is the
shape on screen.

**Layout fixes.** On Games / Apps / Websites, + and (on Games) Refresh sit
after Select as rounded squares. On Downloads, Change sits right after
the folder, and "Nothing downloading." is centred. An empty tab on a cast
page says "nothing under this name" in the middle instead of "Looking
around..." forever.

**Keys.** Ctrl+1-9 no longer switches pages (removed from the window,
from the web pages' forwarding, and from Settings > Keybinds). Keybinds
lists Ctrl+N / Ctrl+P for next / previous episode.

**The startup screen is matte** - the glow behind the mark is gone.

---

## 5. Design notes

**Sliding box.** `main._RailPill` is one widget lowered under the rails in
their scrolling column; it reads the target row's live `visualRect` on
every paint, so a fold, scroll, refit or drag moves it with the row.
`_RailDelegate` draws each row twice - resting ink, then black clipped to
the box. `window_chrome._SectionPill` does the same over the bar's three
buttons, painted above them.

**Headings.** The block gaps are `_RailHeading` widgets, never shorter
than `RAIL_HEADING_MIN_PX` (18) at either sidebar width, so a row sits at
the same y folded and unfolded and `_fit_rails` budgets for them.

**Dropdowns.** `app.js openDrop` takes the press on any `<select>`, draws
`.dropmenu`, and on a pick sets the select's value and fires its own
`change`, so no builder changed. `widgets.install_rounded_popups` patches
`QComboBox.showPopup` and `QMenu.__init__` once at startup.

**A top bar was built and set aside.** The page menu in the window's bar
(`helpers/top_nav.py`, `TitleBar.set_nav` / `add_utility`) was the first
direction; the owner kept the sidebar. `main.TOP_NAV = False` ships it
switched off; removing it is waiting on his word (CLAUDE.md rule 11).

---

## 6. Configuration and user data

Unchanged from 3.10. Nothing new is written.

## 7. External interfaces

Unchanged from 3.10.

## 8. Installation and removal

Unchanged from 3.10 §8.

---

## 9. Known limitations

Carried forward from 3.10 §9. The pointing hand on lists is set but
cannot be shown on a screenshot. A genre tab that finishes empty after
`pending` falls to zero still reads "Looking around..." until the page is
reopened.

---

## 10. Verification performed

**On the frozen build against a copy of the owner's data, from a separate
process**: the sidebar box measured sliding monotonically (centre y 67 ->
654 px, settled ~330 ms); the bar's box 143 -> 221.5 px, settled ~280 ms;
History and Schedule hovers burst-captured frame by frame; Ctrl+3 / Ctrl+2
produced no navigation after a Ctrl+F control was seen to land; Sort list
pick re-sorted Movies; screenshots of the Movies filter row, Downloads,
Settings (Keybinds, the resolution combo, Cancel / Save), a details page's
chips, Mark Ruffalo's empty Anime tab, the Games header, and the splash on
a real launch (every sample beside the mark 10,10,10).

**Tested by the owner on the frozen build** before release.

**The release build, read back out of its own archive**: "3.11" in
`helpers.updater`, `helpers.development_version_patch` and the
what's-new notes; `_RailPill`, `_RailHeading`, `install_rounded_popups`,
`set_active_section`, `_SectionPill` present; `QRadialGradient` gone from
`helpers.splash`; the bundled `app.js` / `app.css` byte-identical to the
source. Bridge SELFTEST OK.

**Defender**, service Running: exit 0 on the app `Atomic.exe`, the bridge
`Atomic.exe` and `Atomic.zip`, twice.

**The release-notes gate:** seven notes written for 3.11.

---

## 11. Glossary

Unchanged from 3.10 §11.
