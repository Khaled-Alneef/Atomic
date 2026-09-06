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

## The reader draws a page exactly as its site does (5 September 2026)

His ask: *"3asq manga page size and sharpness: make it like the source
site exactly on size and quality, also the other sites like TeamX and
others"*. Measured on each of his hosts, reader page plus stylesheets
plus one chapter's images, and the live 3asq page in a 1638px viewport
(1327 of 1638 for a 1327px scan): **every site draws a page at its own
width in CSS px, capped to the column** - Madara's `.reading-content img
{max-width:100%}`, olympustaff's natural `.manga-chapter-img`, Lava's
`.reader-area{max-width:800px} img{width:100%}` on 800px pages. app.js
`targetFor` is now exactly `min(naturalWidth, column)` and `sizePage`
draws each page at its own width; the medium floors (manga 1100, strips
762) that made the app differ are gone - an 829px 3asq chapter drew at
1100, a 1.33x enlargement the site never makes, and that enlargement
was the "sharpness". His 4 September ask to enlarge narrow scans is
superseded by this one and the code says so. `askForExact` still asks
the proxy for the device pixels once (LANCZOS + unsharp, measured 13.53
against the browser stretch's 10.18), so the app is the site's size and
at least its sharpness. The Qt fallback reader follows the same rule in
device pixels (`_on_page_width`). Remember his primary panel is
2560x1440 at 125% (2048x1152 logical - `QScreen.geometry()` is
logical), which is why his reader log says `column=1976` at `dpr=1.25`.

## A programmatic track pick must not open the tracks panel (5 September 2026)

`_pick_track` fell back to `_open_tracks_panel(rebuild=True)` whenever
`_sync_track_rows` answered False - which it also does for "no tracks
panel is open" - and `_new_panel(rebuild=True)` creates one. Every
automatic pick (`_apply_audio_default`, `_apply_remembered_track`,
`_auto_select_arabic_track`) therefore opened the Audio and Subtitle
Tracks panel, and a muxed pick from the Subtitles panel replaced that
panel with it. Reproduced offscreen with the real unbound methods on a
stub page (`tracks_panel_harness.py`); guarded on an open tracks panel,
all five cases leave the panels as they were and the open-panel control
still repaints in place.

## Two page sizes in one chapter were the cover shrinker, not the reader (5 September 2026)

His picture of Kingdom 883: one page a third narrower than the next.
Measured: 3asq serves every single page of 883 at 1326x1920 and 884 at
1306x1920 (and the same bytes to the app's request, a browser's, and
one with no Referer); his image cache held 269 chapter pages at
816-829x1200, 883's pages 3-10 among them, written in one burst two
minutes before pages 2 and 11 arrived at full size. The writer is
`helpers/images.shrink_existing`, the launch-time pass that re-encodes
any cache file over 300KB to the *cover* bounds (PORTRAIT_MAX_H 1200,
LANDSCAPE_MAX_W 2560 - the 2760x1917 spread was on disk at 2560x1778,
to the pixel). It had already destroyed the Qt reader's pages once (the
note above PAGES_DIR) and was moved off them; the web proxy's `web_`
files stayed in its path. The old chapter-wide width stretched shrunk
pages back up, softly, which hid it; drawing each page at its own file's
size showed it.

Three fixes: the pass skips `web_` files; the image route re-fetches a
cached page once when the reader asks for more pixels than the file
has (backend.fetch_image(refresh=True)) and replaces the shrunk file
with the host's original - seven such lines on the frozen build as 883
was read; and because a page already decoded from a shrunk file keeps
its pinned width until the chapter is reopened, a one-off launch pass
(backend.heal_shrunk_pages) deletes every `web_` file that carries the
shrinker's exact signature - portrait at 1200 tall or landscape at 2560
wide, over 200KB - and marks the cache so it never runs twice. Harnessed:
a planted 816px page came back 1306px on the first exact request and
was not re-asked; the pass left a 1326px `web_` page alone and shrank a
cover to 829px; the heal removed the two signature files and kept the
real page, a small cover and a Qt cover.

## Wheel, touchpad and the reader, measured from the screen (5 September 2026)

His asks: touchpad two-finger scrolling stutters on Watch and Read while
cards load (laptop), and "make the scrolling in the reader smooth exactly
like the scrolling in movies page". Measured with `.claude/skills/test/
scrollmeasure.py` (sampler.py at 240Hz over a band of the window, the
band's vertical shift per distinct frame) on the frozen build:

    Movies, 5 notches   130 frames, steps 1-27px (median 4), 0 dead
    reader, 5 notches   5 frames, 125px each - one jump per notch

The page's own line says the rest: `web page: what=glide, frames=33,
ms=133, gapMax=10` per notch on Movies, nothing in the reader, because
app.js's wheel glide returned early for the reader on a note that its
scrolling was "already smooth" - with Chromium's wheel animation off
(webview2_host._BROWSER_ARGS) the browser's own is one 100px jump. The
exclusion is gone; the reader logs the same 33-frame glide now (73
frames for 3 notches, sampled).

**The touchpad tell is cadence as well as size.** A precision touchpad
streams wheel events at 60-120Hz, and a flick's deltas pass NOTCH_MIN_PX
(50), so the size test alone handed every larger delta of a flick to the
130ms glide, re-aimed a few milliseconds apart on the main thread while
pictures decoded. An event under 40ms after a finger event is a finger
now (FINGER_GAP_MS); a stream of 40 events at 12ms measured 40 native
frames of 31px and no glide line. `sweepLazy` reports itself when it
costs over 8ms (`lazy sweep slow`), so his laptop can say what this
machine cannot reproduce.

Three rig traps paid for on the way, all now in rig.py: SendInput's
wheel goes to the keyboard-focus window and dies in the Qt host once
focus has left a page - post WM_MOUSEWHEEL to the window under the
pointer instead (`wheelto`); the reader's back chevron is under a bar
that hides, so a click there without a hover lands on the page and the
reader stays open (which is how a "throttled after the reader" theory
was born and then killed - measure which document received the event:
the page logs `route=`); and `Stop-Process -Name Atomic` leaves a
source-run `python.exe` alive with its window, so the rig drives the
old instance - stop by command line, and read `src/data/atomic.log`,
which is where a source run logs.

## A web view dies with its page, and before it (5 September 2026)

His report, with a log from another device: *"the pages becomes empty
for no reason no buttons no grid nothing at ALL"*. Nothing disposed a
`WebView2Page`'s WinForms form or its control when the Qt page was
deleted - and a page is rebuilt on every visit - so every visit left
Edge's document running behind the one on screen. His log proved it:
`wrapped C/C++ object of type WebView2Page has been deleted` raised
inside `_got_message`, a dead page's document still posting. Measured
here on his build, sixteen sidebar switches in 21s: Edge renderer
processes **7 -> 14**, their memory **476MB -> 1105MB**, six of those
errors, and **9 of 18 arrivals drew**. The orphans keep their pull
loops (`moreOnScroll`) and share the renderer with the live page, which
is what an empty page waiting on its fetch looks like on a machine with
less to share.

`webview2_host._open` holds every undisposed pair; `_WebPage.event` and
`WebReader.event` call `view.dispose()` on `DeferredDelete`, before the
native window goes, and the `destroyed` signal is the fallback. After:
renderers **6 -> 6**, 445MB, every arrival drew, `WebView2: disposed,
live=1` in the log. A navigation that does not arrive now logs its
`WebErrorStatus` and is retried `NAV_RETRIES` times, not forever and not
silently, and `web route slow:` in the log names any `/api/` answer over
a second.

Two rig traps paid for on the way: an exe copy run from a **new path**
raises the Windows firewall prompt, which takes the keyboard (the cold
"before" run's Ctrl+6 never arrived - run the old build from the repo
root path, which already has a rule, and never touch that dialog); and
`rig.find()` only accepts a process named `Atomic.exe`, so a copy called
anything else finds no window.

## A manga spread is drawn at the column's width (5 September 2026)

His ask: *"make the double pages in the Manga fit in width and make sure
it works in all resolutions monitors! (ONLY IN MANGA, and keep the
single pages as is)"*. Under the site's rule (`targetFor`, min of the
scan and the column) a spread wider than the column already filled it
and a narrower one did not: measured through the app's own pages route,
Kingdom (WAN) ch.886 opens on a 2760x1917 spread and ch.885 ends on a
1205x880 one beside 1327px singles, and the second drew at 60% of the
1991px column on his panel. app.js `sizePage` now gives a spread the
live column (`fillSpreads`, manga only, the same medium test `paged`
uses), zoom still multiplies, singles keep the site's rule, and the
reader logs `reader spread` with scan, column and drawn once per real
change. Measured on the frozen build, ch.885's spread: column 1991 ->
drawn 1991; a 1920x1080 window -> 1464 of 1464; 1366x768 -> 1022 of
1022; the singles' own line unchanged at 1327.

Rig trap paid for: the window comes back at whatever size it was closed
at, and its frame ignores SW_MAXIMIZE - set the maximised rect with
SetWindowPos *after* the app has applied its saved geometry (about four
seconds in), and check `rig.find()`'s rect before clicking.

## A spread ignores the zoom; manga singles are 85% (6 September 2026)

His morning report on the column rule above: *"the manga double page
still does not fill the width!!!!"*, beside *"decrease the Manga ONLY
SINGLE PAGES width by 15% keeping the good quality"*. The reader's zoom
is remembered between sessions (`atomic.reader.zoom`), and a spread was
drawn at column x zoom along with everything else - a reader zoomed out
by hand (which is what a smaller single page looks like when done by
hand) shrank the double page too. `sizePage` now draws a spread at the
live column whatever the zoom; the zoom multiplies single pages only.
A paged manga scan is drawn at `MANGA_SINGLE_SCALE` (0.85) of its own
width; spreads and strip-shaped pages are untouched, and the proxy's
one resample (askForExact) is the quality. Measured on the frozen
build, Kingdom (WAN) ch.885: `reader sized ... scan=1327x1920,
target=1128, drawn=1128`, and at 70% zoom `reader spread ...
scan=1205x880, column=1991, drawn=1991, zoom=70` - the single beside it
at 790, the spread edge to edge.

## The player steps forward only on a known season end (6 September 2026)

His report: *"why is Reacher showing me S01E09 while the E08 is the last
ep of the season"* - after the same press had stepped to S02E01 here.
`_fetch_meta_worker` asked the network for Cinemeta's episode list on
every open and never the disk file the details page had just written
(`meta-series-<id>.json`, stremio.fetch_meta_cached), so on a device
where Cinemeta was slow or refused `_meta_aired` stayed None and the
season's length was `DEFAULT_SEASON_EPISODES`'s guess of 12. The disk
answers first now, and `_change_episode` steps forward only past a
`_known_season_end` (the aired list, or `latest_available` naming this
season); while Cinemeta has not answered the press says "Checking the
season's episode list..." and asks again, and a title Cinemeta has no
list for keeps the guesses as before (`_meta_answered`). Harnessed on
the real unbound methods (`h_nextseason2.py`), and S01E08 -> S02E01
photographed again on the frozen build.

The manga reader has no side gutter now (`.reader.paged` padding 0,
`availableWidth` reads the computed padding): his zoomed-in single pages
had stopped at the same width as the spreads, 30px short of the window
each side. Measured: column 1991 -> 2051 on his panel (the scrollbar's
12px is what remains).

A title saved after it was watched starts at History's position
(`details._seed_progress_from_history`): the player and reader write
`history.touch(progress=...)` for unsaved titles too, and a save was
throwing it away - "it says S01E01 until something is watched".

## Every "next" is season-aware, from the map on disk (6 September 2026)

His two follow-ups on Reacher: *"Reacher still shows S01E09"* (the
resume), then *"the resume button takes me to the correct ep now, but
the cards in the main page and the history still show S01E09"*. Three
more places computed episode + 1 inside the season: `_starting_episode`
(ran in the constructor before the map was even requested, so a resume
after E08 asked for E09 and `_apply_meta_bounds` then clamped it back,
never forward - that clamp now rolls into season+1 when the map has
one), `_prefetch_next_episode` / `_maybe_prewarm_next` (one
`_next_target` now), and server's card labels (`_last_mark`, `_one_on`
-> `_season_step`, which reads `meta-series-<id>.json` through an
mtime-keyed cache). The player seeds `_meta_aired` from
`stremio.cached_meta` before anything is decided. Harnessed
(`h_resume.py`, `h_label.py`) and photographed: Reacher stored S01E08,
the Home card reads S02E01, the ring opens S02E01.

## Home's banner art was written by a page that no longer runs (6 September 2026)

His screenshot from the other device: Home opening straight on
"Watching", no banner. `backend.hero_for` drew only an entry carrying
`hero_backdrop`, and that field is written by `windows/home`'s
`_hero_backdrop_worker` - the Qt Home - while the Home on screen has
been the web page since 31 August. A fresh install therefore never gets
the field; this machine had it from the Qt days, which is why it never
reproduced here until the art was stripped from a copy (16 fields).
`hero_for` now falls back to the entry's cover the way a Discover row's
banner is built (`poster: True`, the cover as ground and cover, the IMDb
id carried so app.js `heroFor` asks `/api/featured` for TMDB's wide art
and logo). Photographed on the frozen build against the stripped copy at
2560 and 1920 wide: the banner is there, with the wide art in.

Second report from the same device after the cover fallback: still no
banner. A library brought over from another machine can carry
`cover_path` into that machine's cache and nothing else, so
`cover_url(entry)` is empty there while every card still draws (a card
asks `/api/cover` for its picture). `hero_for` now returns a banner for
any titled entry (`poster: True`, ground and cover empty), and app.js
`heroFor` asks `/api/cover` for the banner's cover the way a card does,
keeps it hidden until it has decoded, and lets the blurred ground take
it until `/api/featured` brings TMDB's wide art. Reproduced and
photographed on a copy with every art field stripped and no image cache
(the closest this machine can get to his other device): the banner is
there at 2560 and 1920 wide, with the wide art, logo and cover in.

## A resolution group folds in place (6 September 2026)

His recording: *"the folding and unfolding of the resolution sources in
the ep list page is not smooth at all"*. A heading click called
`_fill_rows`, which threw every row in the panel away and built every
row again - Reacher's 1080P group is forty-one cards - and then the list
jumped to its new length. `details._GroupBody` holds a group's rows
now, built once on the first open, and the fold animates its
maximumHeight on `widgets.SmoothTween`, the sidebar's own tween at the
panel's refresh; the heading is restyled in place. Measured on the
frozen build, from the log line each fold writes:

    first version   opened built=37ms frames=0 over 260ms   (a jump)
                    closed frames=13 over 225ms             (Qt's 60Hz tick)
    now             opened built=17ms frames=56 over 224ms
                    closed frames=43 over 220ms, opened frames=44 over 220ms

The zero was the forty-one new cards being polished by the event loop
after `build()` returned, under the animation's clock; the first
animation now starts one event-loop turn later. A late batch of sources
still rebuilds the panel (`_on_sources`) and `_open_source_groups` is
what survives it. See CLAUDE.md rule 13 for the refresh rule landed the
same day (helpers/changes).

## The reader's bar moves the window; a landed slide lets go of its pictures (6 September 2026)

His ask: *"make the window draggable from the upper bar in the reader
mode while in not fullscreen"*. The reader covers the window's own bar
and its bar is inside Edge, where a press never reaches Qt. WebView2's
non-client region support (`IsNonClientRegionSupportEnabled`, set in
`webview2_host._ready`; SDK 1.0.3856 and runtime 152 both carry it)
treats an element styled `app-region: drag` as the host window's
caption, so Windows runs its own move loop - the same thing
`window_chrome.begin_window_drag` gets from `startSystemMove`. app.css
marks `.rbar` and excludes its controls; app.js marks the document `fs`
when it is exactly the screen's size, and `body.fs .rbar` is not a drag
region. Measured on the frozen build in a 1600x900 window: a press on
the bar's empty part dragged by (+300,+160) moved the window by exactly
(300,160); a press on the chapter dropdown moved it 0; in full screen
the same drag moved it 0.

The other finding from last night's switch measurement, Atomic's own
process 256MB -> 676MB over sixteen sidebar switches: a page grab at his
window and device ratio is 3222x1747 pixels, 22.5MB, two per switch,
and `PageSlide` kept both reachable through the closure cycle its
`on_done` makes with the `slide` variable in main._show_page, so the
pixel data waited on a generational collection. `_finish` now drops the
callback and both pictures the moment the slide lands.

## The glide moves the page by a distance, never to a position (6 September 2026)

His report on 1.10.270: *"the page started to move opposite side when I
scroll for the first ticks"*, then *"the issue of scrolling happens in
all pages!!!!"*. app.js's wheel glide wrote an absolute `scrollTop`
each frame from where the notch found the view, so anything that moved
the page inside its 130ms was undone by the next frame - and the
undoing is what the eye sees, a page going the wrong way for a tick.
Measured on the frozen build with a continuous wheel-down at four
notches a second over a Discover page still filling: **376px and 164px
jumps up**, exactly at the two batches, because the first fill threw
every strip away and rebuilt them (one frame of a page with no strips,
scroll clamped to it). On a slow device the same thing happens on every
catalogue page: the first live batch adds the filter ticks above the
grid during the user's first ticks, Chromium's scroll anchoring moves
the page to keep the grid in place, and the glide moved it back.

Two changes. `pullDiscover` swaps only the blocks whose titles changed,
in place (a strip's height does not depend on its cards), appends new
sections, and compensates `scrollTop` by the banner's height when the
banner arrives. And the glide applies the *increment* of its eased curve
to wherever the page is - a move from elsewhere is kept, the notch still
travels its distance, a notch mid-glide re-aims from the current view
plus what is still owed, one frame chain at a time (`glideOn`). The
glide line now carries `moved` and `movedMax`: how far something else
moved the page under it, so his log names the trigger on his machine.

## The frame's timestamp is older than the handler that scheduled it (6 September 2026)

His laptop log on 1.10.272, wheeling down on Movies, the position each
notch found: **1506, 1503, 1500, 1494, 1490, 1478**, then 2206 when the
burst ended - *"it is taking me up while I scroll down, so there is not
a big move in pixels but it feels disgusting"*. The relative glide's
clock starts in the wheel handler with `performance.now()`; the
animation-frame callback that follows carries the frame's *start*
timestamp, which lies before that (MDN's own note: compared against a
`performance.now()` taken just ahead of it, the frame time is in the
past). So the first frame after every notch computed a negative time,
the ease-out went negative, and the frame applied a step backwards -
which the old absolute glide overwrote a frame later and the relative
one kept, while a burst of notches restarted the curve before it could
recover. At 60Hz the lag reaches a whole frame; at 165Hz here the same
bursts measured monotonic, which is why this never reproduced on this
machine and the instrument (`wheel=` on the glide line) is what found
it. `t` is clamped at zero. What other apps on the engine do: VS Code
animates its own wheel scrolling in script as this does; plain Electron
apps take Chromium's compositor curve, which was measured here on 1
September as a 660ms dribble and turned off (webview2_host).

## A web view is revealed once, dark, with its document (6 September 2026)

His report: *"while I am scrolling in the 3asq readings there is a
black stutter"* - with the trigger, once asked: leave the chapter, enter
it again, scroll fast. Sampled at the screen's rate (`sampler.py`) on
Kingdom (WAN) 886, the frames of a re-entry, no wheel at all: the
details page, the reader widget's ground for 57ms, **a pure white frame
for 25ms**, the WinForms form's ground for 67ms, the details page
showing through again for 85ms, a 100ms fade to dark, a dark hold of
840ms, then the pages. Two greys told the layers apart - 14 is the
form's back colour, 19 the document's ground - and a trace on the Qt
side placed the white frame 200ms *before* the document had navigated:
`WebView2Page` creates its form parked at -32000 and `_navigated`
reveals it, but the reader's `follow()` resizes the widget first and
`_fit` sized the child at 0,0, dragging the empty control on screen
early. Two changes in webview2_host: a child with no document is sized
where it is parked (`SWP_NOMOVE`, a `_parked` flag so the reveal's fit
still moves it even at an unchanged size - the first version of that
left the reader off screen for good), and the control's
`DefaultBackgroundColor` is the app's ground, the colour WebView2 shows
before a first paint and for a frame on any resize. After: the details
page, one dark hold of about 400ms, the pages, and they arrive at 0.64s
after the click against 0.9s.

Under a fast wheel two more things showed as page-sized ground frames
mid-scroll: the reader's pictures were asked for only 800px ahead, less
than one screen (`LAZY_MARGIN_READER_PX`, four screens now), and the
exact-size copy replaced a visible page before it was decoded
(`askForExact` decodes the probe first). Ground frames per re-entry at
25 notches a second, source tree: 5, 2, 1 before; 1, 2, 0 with the
look-ahead; 0, 1, 0 with both. The bench is `drive_flash.py` beside
`rig.py`; it targets only the window its own launch created, so an
instance he has open is left alone.

What measured as *not* it, so it is not re-dug: the exact-size swap
alone (switched off: same three entry frames), the spread's box getting
its width a frame before its shape (real, a 1547px jolt of the strip on
entry, fixed in `sizePage` - but not the flicker), and any large scroll
step (the steps around every ground frame were 40-70px).
