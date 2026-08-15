# UI rules

Read this before non-trivial UI work (`ui-engineer`).

## Where things live

    src/main.py              window, sidebar, navigation, page transitions, full screen
    src/windows/             one module per page - home, tracker (Anime/Reading/Series),
                             games, link_grid (shared by apps + websites)
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

## How deep to dig

Routine visible fix (colour, layout tweak, known cause) → go straight to
the change. Save tracing/profiling/multi-pass digging for a genuinely
unknown cause or a critical bug (broken functionality, data-loss risk) -
guessing there is worse than taking the time. If unsure which case
applies, say so.
