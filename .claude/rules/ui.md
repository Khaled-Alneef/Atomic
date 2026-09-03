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
