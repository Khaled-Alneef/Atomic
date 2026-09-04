# UI rules

Read this before non-trivial UI work (`ui-engineer`).

## Where things live

    src/main.py              window, sidebar, navigation, page transitions, full screen
    src/windows/             one module per page - home, tracker (Anime/Reading/Series),
                             games, link_grid (shared by apps + websites);
                             plus three full-window overlays opened over the
                             page stack, not in it: player, reader, details
                             (the title page a card's body opens - facts +
                             episode/chapter list; the round cover button
                             resumes, and that split is settled)
    src/helpers/theme.py     palette constants + the whole app stylesheet
    src/helpers/widgets.py   GlassPage, Card, toasts, scroll_area, hover-cursor registry
    src/helpers/storage.py   JSON persistence
    src/helpers/nav_config.py sidebar order and section visibility

## Non-optional conventions

- **Colours from `theme`, never a literal.** `BG`, `SURFACE`, `ACCENT`,
  `TEXT`, `BORDER`, `CARD_*` etc. live at the top of theme.py; six are
  fixed by the app's colour spec, the rest derived. A hardcoded
  `#7c6ff2` is a bug even when it matches today.
- **Pages rebuild from scratch on every visit and sort change** - never
  keep state in a widget that must survive; it lives in the saved JSON
  or it doesn't exist.
- **Save with `storage.update_entry(file, id, fields)` for one entry,
  never a whole list back from a page.** Several pages share a file,
  each holding its own copy loaded at build time - writing the whole
  list restores a snapshot that may be minutes stale. Shipped defect:
  reordering a game erased freshly imported games.
- **New clickable thing → `widgets.Card`** (hover highlight, hand
  cursor, click signals already handled). A plain widget needing the
  hand cursor gets `use_hover_cursor(widget)`. Never call `setCursor`
  and leave it set - see cursor trap below.
- **Messages → `show_toast`/`finish_toast`, not QMessageBox.** Dialogs
  only for something the user must decide or not miss. For work that
  takes a moment, a sticky toast (`duration_ms=None`) updated via
  `finish_toast` so the box re-reads instead of blinking. Title case:
  "Updating...", "Updated Successfully", "There is No New Update", "No
  New Games Found".

## Traps already paid for

- **Over the video, native or nothing.** mpv renders into a native child
  window (`player.VideoSurface`), and on Windows a native child paints
  above every non-native sibling whatever `raise_()` was told. The
  loading logo was a plain child of the player page and had therefore
  **never once been visible** - drawn every frame, underneath the video
  surface. Anything that must appear over the video is either
  `_make_native`'d itself or a child of something that is; that is what
  `player.StartupBackdrop` is, and why the logo lives inside it.
- **A Leave is not proof the pointer went away.** It is also what the
  pointer arriving on a raised sibling looks like. The reader's floating
  chapter controls are children of the page over the strip's viewport,
  so reaching for one fired the viewport's Leave and hid the button
  under the cursor - "the lower bar never appears". Ask where the
  pointer actually is (`QApplication.widgetAt(QCursor.pos())`, which
  hit-tests the widget tree and so has no scale factor in it) before
  acting on a Leave. The same fix is in `tracker._CardHoverRelay`.
- **Never position with `mapToGlobal()`.** On two monitors at different
  scale factors it returns coordinates divided by the *other* screen's
  factor - toasts landed 200px off. `window.geometry()` is already
  global; use it.
- **Scale pixmaps by `devicePixelRatio` and tag the result**, or images
  blur on any non-100% display (see the sidebar logo in main.py).
- **QListWidget draws items with the widget's font, not the QSS
  `::item` font-family.** Set the font on the widget too or the rule is
  silently ignored.
- **Cursors:** a widget holds the hand cursor only while the pointer is
  genuinely inside it (Enter/Leave); Qt loses track after a modal
  dialog closes - that's why `native_cursor.py` and the main.py
  watchdog exist. Don't "fix" a cursor problem by setting one
  permanently.
- **Window state changes** (maximized ↔ full screen) go through
  `theme.without_window_animation`.

## House style

Comments explain *why*, often recording what failed and what was
measured - match that density and voice. A comment restating the code
is noise; "this is not mapToGlobal because..." is the point.

## Every visible change is checked on a screenshot, from his side of the glass

**His rule, 2 September 2026**: *"make it a rule to test any UI
modifications on screenshots from the user POV!"*

It was written after three changes in one session that were verified in
a browser and shipped broken in the app - the pictures on Games and Apps,
the sidebar fold, the website logos. Every one of them would have been
caught by one screenshot of the running window.

So, for anything he can see:

1. **Run the real app** and photograph the real window - `QScreen.
   grabWindow(0)` cropped to the window's own geometry. A WebView2 page
   is a native child window and Qt excludes it from `widget.grab()`, so
   a Qt-side grab shows a hole where the page is; only a screen capture
   is what he sees.
2. **Photograph the state he described**, not a neighbouring one: the
   page he named, at the size he runs, mid-animation if the complaint is
   about an animation.
3. **Look at the picture before saying it works.** A route that answers
   200 and a DOM that says `complete === true` are not the claim being
   made; the claim is about pixels.
4. A page rendered in another browser is a *step*, never the evidence.
   The app runs WebView2 inside a Qt window at his DPI - none of which
   Chrome-in-a-pane reproduces.
5. **Say which screenshots were taken.** "Verified on Games, Apps and
   Websites at 1400x900" is a finding; silence about it is a claim.

Never test against real user data while doing it - point `APPDATA` at a
copy first (`.claude/rules/testing.md`), which is enough here because
`storage.DATA_DIR` and `web/backend.DATA_DIR` both read it.

## How deep to dig

Routine visible fix (colour, layout tweak, known cause) → go straight to
the change. Save tracing/profiling/multi-pass digging for a genuinely
unknown cause or a critical bug (broken functionality, data-loss risk) -
guessing there is worse than taking the time. If unsure which case
applies, say so.

## The search suggestion panel is patched twice, and the second one wins

`helpers/global_search_visual_patch` rebuilds the panel, and
`helpers/global_search_list_polish_patch` then **replaces** several of
its methods outright - `__init__`, `set_query`, `_on_discover_ready`,
`closeEvent`, `mousePressEvent`. It installs after, so a change made in
the visual patch's `on_discover_ready` never runs.

Caught on a screenshot, 3 September 2026: cast rows added to the panel
drew through the polish patch's generic title path instead, wearing
"Watch - Person" as their meta and carrying a payload that would have
sent a click to a details page for a person. The fix is one
implementation both call - `visual.add_face_row` - not a copy in each.
This is `.claude/rules/testing.md`'s "a wrapper that replaced the
patched function outright, so the fix had never run", and the way to
catch it is to photograph the thing rather than read the diff.

## A rect read inside a `content-visibility` card is not free

Measured 3 September 2026, chasing "the cards transition while
fold/unfold the sidebar on Manhwa and Movies still not the same as
series and anime".

`.gc` carries `content-visibility: auto`, so Chromium skips the layout
of every card off screen. Asking for a rectangle **inside** one - the
`<img>`, say - forces that skipped subtree to be laid out. `sweepLazy`
did exactly that, for every pending picture, and it runs on every
scroll and at the end of every fold (`endFold` dispatches a resize).

On a scrolled Movies or Manhwa page that is 850 pending pictures.
Measured against 900 cards with layout dirty:

    reading the card's rect     median 0.6ms   worst 1.0ms
    reading the image's rect    median 0.8ms   worst 890.8ms

**The 890ms is a one-off and the control is what proved it** - two more
passes of the same pair measured 0.7-1.8ms for both, because Chromium
had by then realised the 900 subtrees. It is still a real freeze, and it
lands the first time a freshly-scrolled page is folded or scrolled,
which is precisely where he saw it. Series has five pending pictures and
never pays it at all.

So: read the rectangle of the **contained** element, never of something
inside it. The card is what `content-visibility` is on, its own rect
costs nothing, and the picture is centred in it - which is all a
"is this near the viewport" test needs.

The same reasoning removed the fold's own scan: `hostFold` read a rect
for every card in the grid, twice, to find the ~45 that move. A grid is
row-major so `top` never decreases, and the visible band is found by
bisection - ten reads instead of nine hundred. His log measured what it
had cost: prep 0.8ms at 31 cards against 3.6ms at 917, with the same 45
cards moving on both.

## The Qt reader's targets are image pixels, not logical ones

Measured 4 September 2026, after "the manga still has no size and
quality as before in Qt" - the second report in a row about the same
thing, and the first fix had matched Qt's *numbers* while getting their
*units* wrong.

`windows/reader.py`'s chain: `_on_page_width` picks a target
(MEDIUM_TARGET_WIDTH manga 1100 / manhwa 762 / manhua 762,
STRIP_TARGET_WIDTH 762, or the scan's own width capped to the window),
`_decode_page_job` scales the page to that many **pixels**, and
`_tagged` hands the pixmap to Qt with the screen's device ratio on it.
So a 1100-pixel page occupies 1100/1.25 = **880 logical pixels** on his
panel and is blitted one image pixel to one screen pixel.

Reading those numbers as CSS pixels makes every page 1.25x too wide
*and* upscaled by the compositor on top - bigger and softer, which were
his two words. Dividing by the device ratio is what makes it Qt:

    Kingdom 883   1326px scan -> 1326 image px -> 1061 CSS (1:1)
    Kingdom 885    829px scan -> 1100 image px ->  880 CSS
    Eternal Sup.   800px strip ->  762 image px ->  610 CSS (a downscale)
    any spread                 -> the column, as reader._show fits it

The other half is **who resamples**. Qt resampled once, in the decode,
with SmoothTransformation. A browser handed the scan at its own
resolution and a CSS width resamples on every paint with the compositor's
filter, which on line art is what softens the inking. So the reader asks
the proxy for the page at its drawn width in device pixels
(`?w=<px>&exact=1`, web/server._scaled) and draws the answer 1:1 - Qt's
pipeline, and measurably so: served pixel count equals device pixel count
on every page of the four chapters checked.

Two consequences worth knowing: the served image must not feed back into
the sizing (`_natW/_natH` pin each page's *own* size, or the next pass
reads its own last answer and the page walks), and `_scaled` needs an
`exact` mode - the card path's "already about the right size, leave it"
shortcut would skip precisely the resample this depends on.

## Two CSS traps this app has now paid for

**`background` shorthand on a styled checkbox.** A genre tick drawn with
`appearance: none` took its unchecked colours fine and refused the
checked ones - the element matched `:checked`, `--accent` resolved on it,
and the computed background stayed the ground colour *even when set
inline*. The shorthand resets `background-image` with it, and Chromium
keeps a checkbox's own rendering in that slot under `appearance: none`.
Longhand `background-color` works. Measured 4 September 2026.

**`accent-color` is not "make the checkbox match the theme".** It tints
the tick's fill when the box is on; the box itself stays the platform's
white square, which on this ground is the brightest thing on the page.
The owner asked for the theme colour and that property does not give it -
the control has to be taken out of the paint and drawn.
