# Atomic — Version Description Document

**Version 3.15** · 30 October 2026

---

## 1. Identification

| | |
|---|---|
| System name | Atomic |
| Version | 3.15 |
| Release date | 2026-10-30 |
| Repository | https://github.com/Khaled-Alneef/Atomic |
| Target platform | Windows 10 / 11, 64-bit |
| Delivered as | `Atomic.zip` — one **GitHub release asset** holding the ~11 MB **bridge installer** as its only `Atomic.exe` and the application **as a folder**, packed as `app.zip` |
| At the tag | `Atomic.exe` / `Atomic.zip` — the same bridge, for installs predating 2.0 |
| Asset size | 135,746,500 bytes |
| Asset SHA-256 | `d6f34a737ac5a3e1f6b5d32914b39ca586d60e3087721be4dedaf07607b6683d` |
| Bridge SHA-256 | `7039bd3102e2872dfb9fb90b3ee711464bc406cb8e7f0ca9f1cf671339cce417` (11,179,699 bytes) |
| Preceded by | 3.14 — `docs/VDD-3.14.md` |

---

## 2. System overview

Unchanged from 3.14.

---

## 3. Inventory of materials released

Unchanged from 3.14 §3.

**Changes since the last release** (`git log released/3.14..development`):

| Commit | Summary |
|---|---|
| 8d5b221 | Atomic 3.15 - the sidebar fold no longer steps the web pages; Edge's window never moves during a fold |

---

## 4. What this version provides

**Folding and unfolding the sidebar is smooth on every web page.** The
banner pictures on Home and Discover had moved in visible steps - back and
forth - whenever the sidebar folded or unfolded, as far back as 3.9. They,
and every page's cards and lists, now glide with the sidebar in both
directions.

---

## 5. Design notes

**Why it stepped.** Every fold step moved or resized the WebView2 window on
Qt's clock while Edge drew on its own, and the screen showed Edge's last
frame at the window's new place for a frame before the new layout arrived:
the banner's picture went -d, +d/2, -d ... Measured on a screen band across
the banner, every distinct frame: 100% of frame-to-frame steps reversed
direction on 3.12 and 3.14. With the window held still and the page moving
its own content, 0%.

**The design.** Edge's WinForms form is adopted by the main window rather
than by the Qt view (`webview2_host.WebView2Page.detach`) and always spans
from the *folded* rail's edge to the window's right edge, `lead` px left of
the view. A window region on the form - a window Qt does not own, so Qt
never resets it - clips it back to the page; it is changed without a redraw
and the form carries `WS_CLIPCHILDREN`, so a widening clip never paints the
form's ground over Edge. The form is kept directly above the Qt view in
z-order. The page starts `--qtrail` in (app.css), told by
`web_pages._tell_rail`, and during a fold moves its own left edge and width
on the rail's curve (`app.js foldEdge`). Every page now takes the fold, grid
or not; Home and Discover are no longer special-cased.

**The Qt view stays exactly the page.** A first version put the Qt view
itself under the rail with a mask; Qt does not paint under its own native
children (`webview2_host.suppress`), so the rail's strip went unpainted when
the player or reader closed over it. The Qt view never covers the rail now.

**Three fold-only faults, each found by measurement.** (1) The clip was set
with redraw, and the form painted its ground into the strip each widening
exposed. (2) The fold's width scale was sent with the view's width alone,
not the strip under the rail, so every fold aimed at the wrong width.
(3) The inset was read from the sidebar holder's width while the page's
width came from the container, and the two could disagree for a step:
logged, the form was resized 1891, 1848, 1892, 1848 ... (1848 right) on
every fold step. `main._rail_inset` now reads the container's own left edge.

---

## 6. Configuration and user data

Unchanged. Nothing new is written.

## 7. External interfaces

Unchanged from 3.14.

## 8. Installation and removal

Unchanged from 3.14 §8.

---

## 9. Known limitations

Carried forward from 3.14 §9.

- After a fold and unfold the sidebar's rows sit slightly closer together
  (`_fit_rails` re-fits them). 3.14 does the same, to the pixel; not changed
  here.
- `main.FOLD_SIZED_MS` and `webview2_host.WebView2Page.sync_position` are no
  longer used by web pages and are left in place, pending the owner's word
  (CLAUDE.md rule 11).

---

## 10. Verification performed

**The owner tested the build and approved it for release.**

**On the frozen build, a copy of his data, on a 1920x1080 100% monitor**
(clicks posted to the window, the pointer untouched): Home's banner band,
two folds and one unfold, 0% reversals (fold steps -4, -4, -4, -4 ...); the
form placed 0 times during a fold (logged, source tree). The sidebar's label
rows at rest identical to 3.14's, row for row; after a fold and unfold,
identical to 3.14's after the same.

**Source tree, same rig:** the cause split by disabling one thing at a time
- clip frozen: still stepping; view allowed to grow: still stepping; Qt
leaving the view alone: smooth - then the per-step log above.

**Not exercised by the session on the frozen build:** returning from the
player or the reader with the sidebar open (left to the owner's test).

**The release build, read back out of its own archive**: "3.15" in
`helpers.updater`, `helpers.development_version_patch` and the what's-new
notes, no "3.14" in the first two; `_clip_to_host` and `WS_CLIPCHILDREN` in
`helpers.webview2_host`; `set_lead` and `lead_logical` in `windows.web_pages`.
Bridge SELFTEST OK.

**Defender**, service Running: exit 0 on the app `Atomic.exe`, the bridge
`Atomic.exe` and `Atomic.zip`, twice each.

**The release-notes gate:** 1 note written for 3.15.

---

## 11. Glossary

Unchanged from 3.14 §11.
