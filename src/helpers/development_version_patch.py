"""Final development playback restore and build identity."""


def _install_sharp_watched_episode_stills():
    """Keep DONE episode thumbnails sharp even when spoiler blur is enabled.

    The episode list already knows whether each row is watched before it builds
    the still tile: its badge is ("watched", "DONE").  The ordinary setting is
    still respected for every unwatched episode.  This small import-time patch
    only changes the value seen by DetailsPage._still_tile while one DONE row
    is being constructed, so no image cache or downloader path is duplicated.
    """
    import importlib.abc
    import importlib.machinery
    import sys

    target = "windows.details"

    def patch(module):
        # **Run the whole details patch chain first - this finder is the
        # only one that ever fires.** Four separate MetaPathFinders were
        # installed for windows.details (episode_watch_state_patch,
        # regression_fixes_154, regression_fixes_155, and this one), and
        # Python's import machinery takes the FIRST finder that returns
        # a spec - which is whichever was inserted last, i.e. this one.
        # The other three never ran: measured 29 August 2026 with the
        # app fully imported, DetailsPage._episode_menu was the *core*
        # method, not the cascade (`fixed_episode_menu`), which is the
        # owner's "marking watched does not mark the previous ones" -
        # the feature existed and was silently never installed. Each
        # patcher below guards itself, so calling them here is safe
        # whether or not their own finder someday fires; the order is
        # the original install order, which is the layering the last
        # override (155's chapter menu) was written against. Any future
        # windows.details patch must be added HERE, not as a new finder.
        try:
            from . import episode_watch_state_patch
            episode_watch_state_patch._patch(module)   # + requested_fixes chain
        except Exception:
            pass
        try:
            from . import regression_fixes_154
            regression_fixes_154._patch_details(module)
        except Exception:
            pass
        try:
            from . import regression_fixes_155
            regression_fixes_155._patch_details(module)
        except Exception:
            pass

        cls = getattr(module, "DetailsPage", None)
        if cls is None or getattr(cls, "_atomic_sharp_watched_stills", False):
            return
        old_row_card = cls._row_card

        def row_card(self, title, date_text, badge, on_click, on_menu=None,
                     variant="chapter", still_url=None):
            watched = (variant == "episode"
                       and isinstance(badge, (tuple, list))
                       and bool(badge)
                       and str(badge[0]).lower() == "watched")
            if not watched:
                return old_row_card(
                    self, title, date_text, badge, on_click,
                    on_menu=on_menu, variant=variant, still_url=still_url)

            previous = getattr(self, "_blur_stills", False)
            self._blur_stills = False
            try:
                return old_row_card(
                    self, title, date_text, badge, on_click,
                    on_menu=on_menu, variant=variant, still_url=still_url)
            finally:
                self._blur_stills = previous

        cls._row_card = row_card
        cls._atomic_sharp_watched_stills = True

    module = sys.modules.get(target)
    if module is not None:
        patch(module)
        return

    class _Loader(importlib.abc.Loader):
        def __init__(self, wrapped):
            self._wrapped = wrapped

        def create_module(self, spec):
            creator = getattr(self._wrapped, "create_module", None)
            return creator(spec) if creator is not None else None

        def exec_module(self, module):
            self._wrapped.exec_module(module)
            patch(module)

    class _Finder(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path, target_module=None):
            if fullname != target:
                return None
            spec = importlib.machinery.PathFinder.find_spec(fullname, path)
            if spec is None or spec.loader is None:
                return spec
            spec.loader = _Loader(spec.loader)
            return spec

    sys.meta_path.insert(0, _Finder())


def install():
    # Restore the user-confirmed 1.10.139 playback/startup mechanics only after
    # every newer runtime patch has installed.
    from . import regression_fixes_152
    regression_fixes_152.install()

    # Older top-bar experiments still install earlier for compatibility with
    # existing code paths; 1.10.155 below removes them from the final player
    # class and restores the exact 1.10.139 bar implementation.
    from . import regression_fixes_153
    regression_fixes_153.install()

    # Preserve the chapter-row right-click forwarding fix.
    from . import regression_fixes_154
    regression_fixes_154.install()

    # Final 1.10.155 corrections: exact 1.10.139 upper bar, plus contiguous
    # chapter unread semantics (clicked chapter and every newer chapter).
    from . import regression_fixes_155
    regression_fixes_155.install()

    # Watched episodes are no longer spoiler-blurred. Unwatched episode stills
    # continue to follow Settings > Watching > blur episode stills.
    _install_sharp_watched_episode_stills()

    # **Re-arm the top-bar clip after 155's restore strips it.** The
    # owner has asked three times for the playing-state upper bar to
    # look like the loading one ("no frame in the upper bar bg"), and
    # the fix has existed since player_top_bar_transparency_patch - but
    # regression_fixes_155 restores the exact 1.10.139 _build_top_bar/
    # _layout_overlays/_wake_controls, which replaces the wrapped
    # methods and silently discards the clip (the same shadowing family
    # as the windows.details finders, found 30 August 2026 by asking
    # the live class rather than reading the installs). Re-applied at
    # the END of the player chain, with the patch's once-guard reset,
    # so it wraps the restored core rather than being thrown away with
    # the wrappers 155 removes.
    from . import player_top_bar_live_patch
    player_top_bar_live_patch.install()

    # Late in the player chain, deliberately: it reinstates the deferred
    # resume that 152's restore removed, and must sit on top of that
    # restore rather than under it. See the module for the runs where a
    # resume produced no picture at all.
    from . import player_resume_latency_patch
    player_resume_latency_patch.install()

    # Installed after the deferred resume so its wrapper runs first on a
    # load and hands the atomic:// stream copy down the chain. This is
    # the fix for "does not play until 100%": the vendored libmpv cannot
    # seek HTTP at all (measured - see the module), so engine URLs go
    # through a client stream that can.
    from . import player_seekable_stream_patch
    player_seekable_stream_patch.install()

    from . import updater
    # 1.10.218: the grid draws and drags its own scrollbar.
    #
    # "The scrollbar dragging is super unsmooth" - and this is the one
    # part where matching Chromium exactly is the wrong goal. A browser
    # does not smooth a thumb drag at all: the content is snapped to
    # wherever the pointer implies, every frame. He said as much of the
    # plain Edge page in the same breath as praising its wheel, so
    # Stremio drags exactly this badly too.
    #
    # On this page - 9344px tall behind an ~870px track - one pointer
    # pixel is about 10px of content, so a native drag is a ~90px jump
    # per pointer sample with three still refreshes between each. Hence
    # the native bar is hidden, the page draws its own, and the view
    # *eases* toward the thumb with the bound measured on the painted
    # grid earlier today: past two pointer steps of lag it goes straight
    # there. Unbounded easing trailed 281px and paid it off in single
    # 4114px frames; bounded, the worst was 212px. One step was tried
    # there too and thrashed at 11982px, so two is the floor.
    #
    # Measured on a driven drag: 0 -> 3987px over **222 moving frames**,
    # median step 9.8px, worst 243px. The wheel is untouched and remains
    # Chromium's own smooth scroll.
    # 1.10.217: smooth scrolling was animating at 60fps on a 240Hz panel.
    #
    # 1.10.216 gave the grid an animated notch and he saw after-image
    # traces at once. The frame stamps from that build say why: the eased
    # notch arrived over 10 frames spaced 15.3, 17.2, 15.8, 17.0, 16.8ms
    # - **60fps** - so every position was held for four refreshes and the
    # eye saw four copies of it. There was nothing to smear before
    # because there was no animation at all; adding one exposed the cap.
    #
    # --disable-frame-rate-limit lifts the compositor's own ceiling.
    # Measured after: the same notch arrives over **21 steps at 151.5fps**
    # (6.60ms apart) rather than 10 at 60fps, so a position is held about
    # 1.6 refreshes instead of four.
    #
    # **--disable-gpu-vsync is deliberately not set.** An earlier build
    # set both together and he felt the tearing immediately. The cap and
    # the sync are different things and only the cap was in the way.
    #
    # Still short of the panel: 151fps against 240. That last gap is the
    # embedded view compositing through Qt rather than presenting
    # directly, and it is not addressed here.
    # 1.10.216: the grid never animated a notch. Two weeks, one flag.
    #
    # Measured properly at last, with an out-of-process sampler watching
    # the screen while he scrolled each app by hand:
    #
    #     Stremio  change  26.9  unevenness 22%  lurches 11  worst 532.7
    #     Atomic   change 590.6  unevenness  9%  lurches  0  worst 926.9
    #
    # Atomic measured *more* even than the thing it was being compared
    # against, and lurch-free, and still felt worse. That only fits one
    # way, and asking the page settled it: **one wheel notch produced
    # exactly two scroll events, 60px apart, with nothing in between.**
    # One discrete teleport per notch. Perfectly even and perfectly
    # steppy at the same time, which is why every evenness fix of the
    # last fortnight measured better and changed nothing he could see.
    #
    # QtWebEngine does not enable Chromium's smooth scrolling. The
    # renderer was Chromium's; the scroll behaviour never was.
    # `--enable-smooth-scrolling`, and the same notch now arrives over
    # **10 frames in 133ms**, decelerating 12, 12, 10.5, 8.2, 6.8, 4.5,
    # 3.0, 2.2, 0.8px - a browser's ease-out.
    #
    # Two corrections to what I told him along the way, both from this
    # same measurement: the per-frame `change` figure is luminance
    # difference and depends on what is on screen, so the 22x gap was
    # mostly the two apps showing different pictures - I read it as
    # travel and said so. And travel per notch was never too large: it is
    # 60 CSS px against a browser's 120, half, not double.
    # 1.10.215: the grid page tells Chromium what it may skip.
    #
    # Three changes aimed at the slow wheel and the drag trace, all of
    # them the ordinary way a browser is asked to carry a long list of
    # pictures:
    #
    #   - `content-visibility:auto` with an intrinsic size on every card,
    #     so layout, paint and image decode are skipped for everything
    #     off screen. Without it the compositor carries 180 cards for a
    #     viewport showing two dozen.
    #   - `decoding="async"` on covers, so one arriving mid-scroll cannot
    #     decode inline and cost the frame it lands on.
    #   - the visible-card report every 160ms rather than 60ms: each one
    #     is a title change, a Qt signal and a round of Python, and at a
    #     slow wheel the old interval fired continuously.
    #
    # **Measured, and the measurement was no good.** An in-page sampler
    # reported 59.9fps on a 240Hz panel with 93% of frames dead and a
    # 60.00px step - identical with the change switched off and on, at
    # runtime, same page and same input. That is requestAnimationFrame
    # being throttled to 60Hz inside an embedded view, not a reading of
    # anything. So these three are reasoned rather than proven, and the
    # honest next instrument is an out-of-process screen sampler, which
    # is what this repo's testing rules already say for exactly this
    # question.
    # 1.10.214: Movies' pictures under the Anime heading - the browser
    # cache, and a URL that did not change.
    #
    # His repro was exact: Movies then Anime showed the Movies covers,
    # while reaching Anime through a different page was correct. Movies,
    # Series and Anime are sections of one page and share one grid
    # widget, so the covers had identical URLs across a section change -
    # same grid id, same index - and Chromium answered from its own cache
    # with the pictures it already had. Going via Manga rebuilt the
    # widget, which gave it a new id and therefore new URLs, which is why
    # that road looked fine.
    #
    # Every cover URL now carries a generation, bumped on each fill:
    # `atomicimg://c/<grid>/<gen>/<index>`, and the handler refuses a
    # request whose generation is not the current one. Measured after -
    # Movies served `c/1/1/0`, Anime `c/3/5/0`, both decoding at 216px,
    # so the two sections cannot share a cache entry.
    #
    # Not verified: the leg that goes on to Manga and back. The harness
    # died at exit 127 there, which is the signature this repo's own
    # notes give to a harness rather than the app, but it is unproven
    # either way and is said so rather than left out.
    # 1.10.213: the page emptied on return, and it was three isinstance
    # checks.
    #
    # The category page asks `isinstance(grid, PosterGrid)` in three
    # places, and WebPosterGrid is a QWebEngineView - so every one was
    # false. `_refill_grid` therefore refused the refill and returned
    # False, which is precisely "they load at first but when I change the
    # page and go back it goes totally empty", and the append-to-live-
    # grid path never ran either. The checks are duck-typed now (_is_grid:
    # set_items/append_items/count/record), and `record()` plus a real
    # `keep_position` are added, because _refill_grid calls both and
    # would have failed on the next line regardless.
    #
    # **And the cover handler was per-widget on a shared profile.** A
    # profile keeps one handler per scheme, so each new grid replaced the
    # last one's and destroying either took the live handler with it -
    # after which the page URL answered nothing. One module-owned handler
    # now, with grids registering by id and held weakly.
    #
    # Driven the way he described it - Anime, away, Anime, away, Anime -
    # and measured in pixels rather than in the DOM:
    #
    #     1st visit  44399 colours, 61.4% non-dominant, 180 cards
    #     revisit    47557 colours, 65.4% non-dominant, 180 cards
    #     3rd visit  44399 colours, 61.4% non-dominant, 180 cards
    #
    # same first title every time. A run that gave the third visit only
    # 13s showed 61 cards and a different title; at 26s it is identical -
    # that was the catalogue still loading, not a fault, and it is
    # recorded here so the next person does not chase it.
    # 1.10.212: the blank page was the native-window attributes, and
    # every check that missed it asked the wrong question.
    #
    # 1.10.209 made the web view a native child (WA_NativeWindow +
    # WA_DontCreateNativeAncestors), to have Chromium present straight to
    # its own HWND and keep Qt's UI thread out of the path. That works
    # for a *top-level* window and is exactly when his Anime page went
    # blank: as a child, the native surface is not composited into the
    # layout, so the page renders where nothing is shown. Both attributes
    # are gone.
    #
    # **The DOM was correct the whole time**, which is why three builds
    # went out on checks that passed - "180 cards on the page", "48
    # decoded". A native surface holds a perfect document and composites
    # nowhere, so none of that was evidence about what he could see.
    #
    # The check is now pixels. Grab the grid and count colours: a blank
    # page is one colour and ~0% of pixels differ from it; the grid
    # measures **44,399 distinct colours and 61.4% of pixels not the
    # dominant one**, on Anime and on Manga. That is the question that
    # should have been asked on the first build.
    #
    # Also worth stating because it was never said: the manga, manhwa and
    # manhua category grids share TrackerPage, so they have been on
    # Chromium since 1.10.206 too. Home, Discover and the reader are
    # different pages rather than grids and are still painted.
    # 1.10.211: the scroll code the Chromium curve replaced is gone -
    # the part of it nothing still uses.
    #
    # His rule, given the same day and now CLAUDE.md rule 10: a fix that
    # changes the root retires what it replaced, once he approves. Two
    # weeks left four models stacked on one another and every new
    # measurement had to reason past all of them.
    #
    # Scanned all 119 source files, deleted, re-scanned, and repeated -
    # removing one dead function usually kills the constants it was the
    # last caller of. Genuinely unreferenced, and now gone from both
    # engines: `_live_rate`, `_speed_factor`, `MAX_QUEUE_PX`, `SNAP_PX`.
    #
    # **Everything else stays, and that is the honest half.** Only the
    # Anime grid renders in Chromium; Home, Discover, the reader,
    # Settings and the manga grids all still run the painted model, so
    # its curve, its cadence tracking and its per-frame cap are live code
    # and not leftovers. Re-measured after the deletions on Home: jitter
    # 0%, dead 0%, worst 12px, coast 21/0/0/0ms. (One run showed a 377ms
    # coast and a second did not - covers loading, not the deletions.)
    # 1.10.210: the Anime page was blank, and 1.10.209 did that.
    #
    # Pointing setHtml's base URL at `atomicimg://grid/` put the page on
    # the same origin as its covers, which was the right idea and the
    # wrong mechanism: the handler registered for that scheme was then
    # asked for the page itself, tried to read "grid" as a cover index,
    # failed the request - and the page never loaded at all.
    #
    # The scheme now serves both. `atomicimg://grid/` returns the page,
    # `atomicimg://c/<index>` returns a cover, one origin for both, and
    # setHtml is not used. That also settles the black covers of
    # 1.10.208: an opaque origin could not fetch a registered secure
    # scheme, and there is no opaque origin left.
    # 1.10.209: the black covers had a real cause, and my last check for
    # them was worthless.
    #
    # **I verified "48 decoded" from source and he runs the frozen exe** -
    # the one thing this repo's testing rules name outright. Asked
    # properly, the frozen build logs `cover scheme registered=True`, so
    # registration was never the fault.
    #
    # The fault is an origin. The page was loaded from `atomic:grid`,
    # which is not a registered scheme and therefore an **opaque
    # origin**, while the covers came from the registered `atomicimg://`.
    # An opaque origin may not fetch a registered secure scheme, so every
    # cover request was refused and every card was a black rectangle. The
    # page now loads from `atomicimg://grid/` - one origin for the page
    # and its pictures - and the handler counts what it serves into the
    # log, so the next time this is in doubt the build can be asked.
    #
    # A data-URL fallback sits behind it, used only when the scheme is
    # unavailable: slower per cover, but it cannot 404, and a black grid
    # is worse than a heavier one.
    #
    # **The view is a native window now.** QWebEngineView composites
    # through Qt's scene graph on the UI thread, which is what the
    # measured stalls were; with its own HWND Chromium presents straight
    # to it and that thread is out of the path - the same reason the
    # player's mpv surface is native. With the cover encode already moved
    # to a worker, Qt's event loop while scrolling now measures p99
    # 2.18ms and worst 3.16ms, against 18.51/50.42 two builds ago and
    # 1.45/2.38 when idle.
    #
    # **The snap bits are re-applied on every window state change.** Qt
    # recreates the native window on some transitions and manual style
    # bits go with it, which would explain WS_CAPTION measuring present
    # and drag-to-top still doing nothing later. Not confirmed on the
    # frozen build: the probe read a null window handle, which is the
    # probe failing rather than a finding about the window.
    # 1.10.208: the covers, and the lag - both of them this module's own
    # doing rather than anything about Chromium.
    #
    # **No images at all.** The covers were being handed to the page as
    # file:// URLs, and the page's base URL is `atomic:grid` - an opaque
    # origin, from which Chromium refuses a file:// subresource outright.
    # Every card had a src and not one picture loaded. They are served
    # over a registered scheme now (atomicimg://c/<index>, declared
    # before QApplication exists), which Chromium fetches itself the way
    # it fetches any image. Measured after: 48 images with a src, **48
    # actually decoded** - the earlier check counted only the attribute,
    # which is exactly how a build shipped with no pictures.
    #
    # **The wheel and the thumb both lagged, and it was the UI thread.**
    # QWebEngineView renders in Chromium's own process but *presents*
    # through Qt's scene graph on this thread, so anything that blocks it
    # holds back frames Chromium has already drawn. Measured with a 1ms
    # timer:
    #
    #     idle             median 1.07ms   p99  2.09ms   worst  2.88ms
    #     while scrolling  median 1.07ms   p99 18.51ms   worst 50.42ms
    #
    # and the block was this module: every cover scrolled into view was
    # PNG-encoded synchronously on the UI thread. QPixmap cannot leave
    # that thread but QImage can, and converting is cheap because the
    # buffer is shared - so the encode is a worker's job now and the page
    # hears about a cover when its bytes exist. Measured after:
    #
    #     while scrolling  median 1.08ms   p99  2.18ms   worst  3.69ms
    #                      0.0% of ticks over 8ms
    #
    # which is the idle figure. That is the whole of the reported lag: a
    # browser embedded in a busy Qt process is only as smooth as that
    # process lets it present, and this one was not letting it.
    # 1.10.207: three faults from his first run of the Chromium grid,
    # two of them mine and one an assertion nobody had measured.
    #
    # **The wheel "jumps hard" - I had switched vsync off.** The
    # bootstrap set --disable-gpu-vsync and --disable-frame-rate-limit,
    # added when a throttled measurement showed 60fps. Unlocking
    # presentation from the display refresh is what tearing is, and
    # Stremio sets neither: it runs Chromium's defaults. Both are gone.
    #
    # **The drag "jumps in steps" - every cover was 28KB of JavaScript,
    # parsed while scrolling.** Covers arrive as QPixmaps, and the first
    # version encoded each to a base64 data URL and handed the string to
    # runJavaScript on the main thread: one cover is 21KB raw and 28KB
    # as a JS string, so a screenful of 24 was **0.6MB of JavaScript**
    # during the scroll that revealed them. Each is now written once to a
    # session temp file and the page gets a file:// URL - about eighty
    # bytes - so Chromium fetches and decodes it on its own threads.
    #
    # **Drag-to-top did not maximise, and the reason was written down
    # wrongly.** window_chrome said WS_CAPTION "is deliberately absent -
    # putting it back is putting the title bar back", and that snapping
    # needs only WS_THICKFRAME and WS_MAXIMIZEBOX. Measured on the
    # running window, both of those were already set and the drag still
    # did nothing: those two buy *edge* snapping, while drag-to-top and
    # the Snap Layouts overlay are gated on the caption bit. It is now
    # set, and the assertion is disproven rather than argued with - with
    # WS_CAPTION on, the window rect and client rect are both 1867x1147,
    # so nothing is drawn for it. Chromium's own windows do the same.
    #
    # The rest of the pages are not converted yet, deliberately: the
    # three fixes above want one confirmation on the Anime grid first,
    # because converting every page before that would put the same fault
    # on all of them.
    # 1.10.206: the category grid is a web page now.
    #
    # **The owner's call, after his own repositories ended the
    # argument.** stremio-web has no scrolling code to copy: its only
    # wheel handler is in the video player, every scrollTo is a jump, and
    # the layout is overflow-y:auto in thirty-three places. All of its
    # smoothness is Chromium's compositor, so the only way to have that
    # is to be Chromium. helpers/web_grid renders the grid as HTML in a
    # QWebEngineView, and nothing in it scrolls anything - no wheel
    # handler, no animation, no script touching scrollTop.
    #
    # It is a drop-in: the same clicked/needs_cover/scrolled signals and
    # the same set_items/append_items/set_cover the painted grid has, so
    # the category page does not know which it is holding.
    # **ATOMIC_WEB_GRID=0 goes back to the painted one**, which is how
    # the two get compared, and a failure to start QtWebEngine falls back
    # on its own rather than leaving a blank page.
    #
    # Proven before it was wired, in this order, because each one could
    # have ended it:
    #   - QtWebEngine runs in-process here: Chromium 140.0.7339.225.
    #   - PyInstaller bundles it into the **single-file** exe and the
    #     result launches with its helper process - 200MB against the
    #     123MB the painted build weighs, which is the price.
    #   - On the real page: 180 records, 180 cards in the DOM, 9075px of
    #     scrollable height, 114 covers delivered.
    #
    # Two seams were not obvious and are worth keeping written down:
    #   - covers arrive as QPixmaps (_GridCover.setPixmap is how every
    #     cover in this app is delivered), and a browser cannot take one,
    #     so each is encoded once as PNG and pushed in as a data URL;
    #   - the page cannot talk back through a URL. Chromium refuses a
    #     navigation to an unregistered scheme before it becomes a
    #     request, so the first version reported needs_cover **x0** on a
    #     grid that had rendered all 180 cards, and no cover ever
    #     appeared. document.title is the channel instead, and the same
    #     run then reported 114.
    #
    # What is not done yet, plainly: hover art, the right-click menu,
    # the Saved badge beyond a coloured meta line, and every other page.
    # This is the Anime/category grid only, so the two can be compared on
    # the same machine before anything else moves.
    # 1.10.205: the curve is evaluated at the refresh it belongs to.
    #
    # **A correction to 1.10.204 first.** That note said the frame clock
    # was a 4ms timer running at 249Hz on a 240Hz panel. That was the
    # median, and the median was misleading. Measured properly:
    #
    #     average      240.02Hz, mean gap 4.166ms   <- the panel exactly
    #     clock        dwm, failed=False
    #     individual   4.0ms x359, 4.5ms x150, 4.2ms x61, 4.1ms x45
    #
    # The clock is right and locked to the display. What is not right is
    # when the tick is *acted on*: it crosses from the ticker thread into
    # the UI thread through Qt's event queue, and that scatters it half a
    # millisecond either way. The animation was then evaluated at the
    # moment the slot happened to run rather than at the refresh it
    # belonged to, and near a frame boundary that reads as a double
    # advance or none - the trace on cards and labels.
    #
    # The ticker now records the instant its vblank wait returned and
    # both engines evaluate the curve at *that* (widgets.vblank_now), so
    # a tick delivered late still lands on its own refresh. Chromium gets
    # this for free: it runs the animation on the thread that presents.
    #
    # Measured on the chunked harness, Anime grid, step jitter:
    #
    #     band    before   after
    #     slow      30%      23%
    #     mid       21%      14%
    #     fast      18%      10%
    #     rapid      0%       0%
    #
    # Dead frames are unchanged at 7-10%, and that is the honest limit of
    # this path: a Qt raster widget is repainted from a queued signal,
    # where Chromium composites on the GPU and presents in lockstep.
    #
    # **And the thing worth knowing about stremio-web**: there is no
    # scrolling code in it to copy. Its only wheel handler is in the
    # video player; every scrollTo/scrollTop is a jump (restore a
    # position, bring an episode into view, a settings anchor), and the
    # layout is overflow-y:auto in 33 places. All of its smoothness is
    # the browser's compositor. Matching it exactly means rendering in a
    # browser - not writing a better curve.
    # 1.10.204: the trace, and what actually causes it.
    #
    # He tested 1.10.203 against the Chromium build of his own page:
    # *"the fucking cards were stuttering and showing image trace also
    # the labels, unlike the Https you provided me"*. Same machine, same
    # covers, same curve - so the model is not the difference and the
    # renderer is.
    #
    # **The per-frame cap is back.** 1.10.202 removed it to be exactly
    # Chromium, and a fast wheel went from 10px between frames to
    # **34.9px**; a frame that arrives late then doubles that. It is
    # applied to the delivered step, not by shortening the animation, so
    # below the cap this is still Chromium's curve to the pixel - slow
    # and mid scrolling never reach 10px in a frame and are untouched.
    # Only a fast flick is limited, and it arrives a frame or two later
    # rather than losing travel. Worst step back to 10-12px.
    #
    # **Two things were measured and are not the cause**, so nobody digs
    # there again:
    #   - paint cost. One grid frame is **0.57ms median, 2.45ms worst,
    #     0.0% over the 4.17ms budget** on 745 and 779 painted frames.
    #     Cells including their labels are already composited once and
    #     blitted (see _paint_cell), so a compositor would buy nothing.
    #   - the scroll model. It is Chromium's, constant for constant.
    #
    # **What is the cause: the frame clock is not the panel.** Measured
    # 31 August 2026, the ticker every scrolling surface runs on:
    #
    #     ticker      720 ticks, median gap 4.014ms -> 249.12Hz
    #     the panel   240.00Hz  (27GS950, and the window is on it)
    #     spread      p10 3.501ms, p90 4.971ms
    #
    # 4.014ms is a 4ms millisecond timer, not a vblank: 240Hz needs
    # 4.167ms and no integer number of milliseconds is 4.167. So frames
    # are produced ~9 a second faster than the panel can show them and
    # unevenly besides, which means the positions the eye actually
    # receives are unevenly spaced however even the model is. That is a
    # trace on moving cards and labels, and it is exactly what Chromium
    # does not do - it presents on the display's own vsync.
    #
    # The fix is to drive frames from the real vblank. DwmFlush measured
    # **240.1 calls a second** on this panel when the ticker was written,
    # which is the clock wanted; the note in _VBlankTicker prefers the
    # timer on a throughput measurement (fps and dead frames), and
    # throughput is not what a trace is. That swap wants its own A/B on
    # *evenness of presented steps*, which is the next piece of work and
    # is not in this build.
    # 1.10.203: the thumb drag, which the Chromium build finally isolated.
    #
    # **The web build of his own Anime page answered the question the
    # scroll work had been circling for two weeks.** Same page, same
    # covers, same palette, rendered by Chromium with no scroll code in
    # it at all - his verdict: *"the wheel scrolling is smooth as
    # fuck!!!!! but the scrollbar dragging is not at all"*.
    #
    # That splits the problem in two, and both halves are useful. The
    # wheel is settled: Chromium's curve is what 1.10.202 runs, and on
    # his machine that curve is smooth, so the model and the renderer are
    # both fine. And the drag is not a Qt failing at all - Chromium's own
    # thumb drag is unsmoothed, the content snapped to wherever the
    # pointer implies every frame, which on a long page is a large jump
    # per pointer pixel.
    #
    # Atomic *does* interpolate a drag, and measured worse than the thing
    # it was meant to improve on. On Movies - 406 cards, 20462px of
    # scroll, so ~24px of content per pointer pixel - dragged by a
    # hand-shaped pointer track rather than the uniform steps every
    # earlier harness used:
    #
    #     single-frame jumps    4114, 2597, 2492px
    #     behind the pointer    median 281px, worst 4979px
    #
    # The spreading is right in principle: a mouse reports at ~125Hz
    # against a 240Hz screen, so handing a whole pointer step to one
    # frame leaves the next showing nothing. But it only ever *spread* -
    # when steps arrived faster than it delivered them the shortfall
    # accumulated, the view trailed hundreds of pixels behind the thumb,
    # and then paid the debt off in one frame. Lag, then lurch, which is
    # exactly what "not smooth" feels like.
    #
    # The lag is now bounded (MAX_DRAG_LAG_FRAMES), in multiples of the
    # pointer's own recent step so it scales with the hand rather than
    # with the page length:
    #
    #     single-frame jumps    4114px  ->  202-248px
    #     dead frames           5%      ->  1-2%
    #     behind the pointer    281px   ->  ~250px
    #
    # 1.0 was tried and is far worse - 11982px jumps, 20-34% of frames
    # dead - because snapping every frame fights the spread restarting on
    # the next pointer sample. 2.0 is the measured floor; scratch
    # drag_human.py is the harness, and it drives a hand-shaped track
    # because a uniform one is the single case that cannot show this.
    #
    # Still true and worth stating: ~250px of trail on a 20462px page is
    # ten pointer pixels. What is left is the sensitivity of a scrollbar
    # over a long list, which Chromium has too and which no smoothing can
    # remove - only a different control would.
    # 1.10.202: the wheel is Chromium's now, exactly, and the seek no
    # longer serves the decoder a hole.
    #
    # **"Make the scrolling exactly like Stremio scrolling EXACTLY."**
    # Stremio's shell is WebView2, so what he is comparing against is
    # cc::ScrollOffsetAnimationCurve. Its constants are now the ones this
    # app uses, taken from the Chromium source rather than tuned by feel:
    #
    #     duration_units = clamp(14 - |delta| / 60, 6, 12)
    #     seconds        = duration_units / 60
    #     easing         = cubic-bezier(0.42, 0, 0.58, 1)
    #     one notch      = 120px  (3 lines x 40px on Windows)
    #
    # so one notch takes exactly 200.0ms, 480px or more takes exactly
    # 100.0ms, and everything between is on the ramp. Verified against
    # the formula: 120 -> 200.0ms, 240 -> 166.7ms, 300 -> 150.0ms,
    # 480 -> 100.0ms. The position is *evaluated* on the curve at the
    # elapsed fraction rather than integrated from a velocity, which is
    # what Chromium does and what makes the travel identical whatever a
    # frame's dt happened to be.
    #
    # Everything tuned here over the previous two days is gone with it -
    # the speed factor, the per-frame cap, the queued-travel bound, the
    # fitted delivery time, the stop rate. Chromium has none of them.
    # That is deliberate and it has a visible consequence: without a cap,
    # a fast wheel crosses **34.9px between two frames** on a grid
    # (measured), where the capped model held 10px. That is Chromium's
    # behaviour and therefore Stremio's; if it reads as an after-image
    # the cap can come back, but it would no longer be "exactly".
    #
    # Measured on the chunked harness he specified - four chunks, each
    # winding 0 -> max -> 0, resting between:
    #
    #     band     step/frame   coast after the chunk
    #     slow      1.17px      3-18ms
    #     mid       2.30px
    #     fast      4.36px
    #     rapid    11.15px
    #
    # The step-to-step variation now reads as 20-25% "jitter" on that
    # harness, and that is the curve doing its job: an ease-in-out is
    # *supposed* to change speed through the notch. The metric was built
    # for a constant-speed model and no longer measures a defect.
    #
    # **The seek glitch, and it is the same fault as the recording.**
    # "When I clicked on min4 in the progress bar, it glitched and
    # started doing the same behavior as in the prev video". A torrent's
    # file is sparse, so reading a region the swarm has not reached
    # returns **zeros of exactly the right length** - every length check
    # passes and zeros decode to macroblock garbage rather than failing.
    # read_present guards the piece map first, but its disk fallback runs
    # precisely when read_piece could not answer for a piece have() says
    # is there, and the usual reason for that is the one written two
    # lines above it: a piece completed but not yet flushed. An all-zero
    # answer is now refused, so the caller waits for the real bytes.
    # frame 3 of issue.mp4 is what serving them looks like.
    # 1.10.201: his list of 31 August, with the recording.
    #
    # **Scrolling follows the wheel now.** "Do not make the scrolling
    # speed fixed... the issue is when I scroll slow it moves fast".
    # Every notch travelled the same 100px, and an isolated notch - which
    # is the whole of a slow, deliberate scroll - had no measured cadence
    # so it used the short default delivery: 100px in 0.12s, 833px/s for
    # one flick of a finger. The distance now scales with the measured
    # cadence (_speed_factor): 62px base, up to 2.6x for a hard spin.
    # Measured with a harness that scrolls the way he asked for - four
    # chunks, each winding 0 -> max -> 0, resting between, rather than
    # one long sweep:
    #
    #     band     step/frame   jitter   dead   worst
    #     slow      0.76px        0%      13%   1.6px
    #     mid       2.25px        0%       5%   7.2px
    #     fast      4.01px        0%       5%    10px
    #     rapid    10.00px        0%       6%    10px
    #
    # A 13x span of speed off the wheel alone, and the view stops when
    # the hand does: coast 10-46ms after each chunk (Home 0-46ms).
    #
    # **The wrong episode.** "When I continued The Angel Next Door Spoils
    # Me Rotten EP09 S01 it played an ep from season 2". The file picker
    # is not at fault - tested against real pack shapes, it puts S01E09
    # in season 1 even when the addon points into season 2. The hole is a
    # release *name*: asking indexers.episode_match for S01E09,
    #
    #     'The Angel ... S2 - 09'      rejected
    #     'The Angel ... Season 2 09'  rejected
    #     'The Angel ... S01E09'       exact
    #     'The Angel ... - 09'         exact     <- the hole
    #
    # and fansub groups number a second season exactly that way. The bare
    # form cannot be rejected outright - for a single-season show it is
    # simply episode 9 - so it is demoted instead: a release stating the
    # season asked for now outranks one that says nothing (states_season,
    # threaded through _rank). Measured on a fabricated list, S01E09 now
    # picks the 40-seeder S01E09 release over a 1500-seeder S2 one.
    #
    # **The picture in issue.mp4.** Frame 3 is macroblock garbage right
    # after a seek - the decoder being handed bytes that are not there.
    # _EngineStream.read answered b"" on any read error, and b"" is EOF
    # to mpv rather than "wait", so a connection dropped while the swarm
    # re-seeked truncated the file under the decoder. It now reopens from
    # the same offset once before giving up, exactly as the debrid reader
    # already did, with the cancel check kept in between so a real stop
    # still stops at once.
    #
    # **Not fixed, and named**: the "buffers, finds peers, buffers again"
    # cycle. It is the same area and the same recording, but I have not
    # measured its timeline yet, and the read fix above may or may not
    # touch it. It is the next thing.
    # 1.10.200: Continue played 720p while Settings asked for 1080p -
    # the owner, 31 August 2026, reporting it as an old one.
    #
    # The player remembers which release an episode was watched with and
    # puts it first, so a replay lands on the file already downloaded.
    # It matched on info hash alone, though, so whatever was watched once
    # led for ever whatever its resolution - and _on_streams' guard could
    # not catch it, because that asks whether the *batch* carries the
    # preferred resolution, not whether the row about to play is it. A
    # batch holding 1080p passed the guard and then started the
    # remembered 720p sitting at index 0.
    #
    # The memory is now honoured only while it agrees with the setting
    # (_remembered_is_wanted). A remembered release at the preferred
    # resolution still leads, which is what keeps a replay on the
    # downloaded file and with it 1.10.199's instant path; one at any
    # other resolution stands aside and the ordinary ranking picks, which
    # puts the preferred resolution first by definition. Two cases keep
    # the memory deliberately: "best", which names no resolution to
    # prefer it over, and a list carrying nothing at the preferred
    # resolution, where the remembered release is the one known to work.
    #
    # It also heals itself: the next play records the 1080p release, so
    # Continue is back on the fast path from then on.
    #
    # Five cases measured with fabricated rows and a stubbed resume
    # record - all pass, and with the check disabled as a control exactly
    # one fails, the reported one, which is how the harness is known to
    # be testing the right thing. The Continue button hands the player no
    # stream list (tracker.py), so it takes this path; the only caller
    # that does is the source picker, where the hand pick rightly wins.
    # 1.10.199: time from pressing an episode to seeing picture. The
    # owner, 31 August 2026: "~12 sec from pressing on the ep to the vid
    # playing, make sure to make the max time ~4 sec for the videos first
    # played, and instant for the videos started watching already".
    #
    # Measured on the real player (not on the modules back to back - that
    # pipeline nobody runs reported 14.2s and was misleading), stamping
    # every stage. **Sourcing was never the problem**: playback starts on
    # the first partial batch at 0.86-0.96s. The whole wait is prepare,
    # and on a replay it was being paid twice over:
    #
    #     Attack on Titan S01E05      before      after
    #     cold, never played          7.3-26.3s   6.05s
    #     replay of the same episode  9.06s       1.20s
    #
    # **Nothing was ever re-downloaded on a replay - libtorrent just did
    # not know the files were good.** Added without resume data it
    # re-checks every piece before have_piece() answers True, so
    # await_start saw no data and waited: 8.4s of a 9.06s replay, on a
    # release whose pieces were written four seconds earlier. Verified
    # state is now saved (_cached_resume / _save_resume, on the window
    # ticker and at the first playable moment) and handed back on the
    # next add. await_start then returns in **0-1ms**.
    #
    # **And a complete file needs no session at all.** If the episode's
    # file is on disk at exactly the length the torrent states, it is
    # played as an ordinary local file - no swarm, no metadata wait, no
    # piece check (torrent_engine.finished_file_path, taken in
    # streams._prepare_with_own_engine before the engine is touched).
    # Deliberately strict: a file even a byte short is a partial download
    # and goes the ordinary way, because playing a truncated file is
    # worse than waiting. That is the "instant" path - prepare returns in
    # **6-10ms** and the 1.20s left is 0.54s of cached source listing and
    # 0.65s of mpv opening the file.
    #
    # **Cold play is 6.05s, not the 4s asked for, and the reason is
    # measurable**: 4.7s of it is one torrent's cold start - metadata,
    # peers, first pieces. `packaging/rd_token.txt` **does not exist in
    # this tree**, so the debrid lane that resolves a cached release in
    # about a second is dark and every play goes through the swarm. That
    # file is a paid credential, gitignored and never committed; without
    # it 4s cold is not reachable on the torrent path alone.
    #
    # One more thing the numbers said plainly: the pick can be bad. The
    # top-ranked row for S01E02 was a four-season collection with 836
    # stated seeders that **failed after 24.00s** on its own, while
    # another row from the same list prepared in 13.6s. _default_pick_key
    # weighs resolution, then Arabic, then seeders, and knows nothing
    # about whether a swarm will actually answer. Left alone here rather
    # than guessed at - it wants its own measured pass.
    # 1.10.198: the slow band, found and fixed - and it was a real bug,
    # not the physical floor the last build called it.
    #
    # **A slow wheel could not be measured at all.** The delivery time is
    # fitted to the gap between notches, and that gap was read off
    # `_kicks` - a deque pruned to ACCEL_WINDOW_S, 0.25s, because it
    # exists to count notches for acceleration. At any wheel slower than
    # 250ms a notch the previous entry was always gone before the next
    # arrived, so the measured gap came back **0** and the delivery fell
    # back to the fixed glide: a 100px notch delivered in 120ms with
    # 260ms between them, the view standing still 54% of the time.
    # Measured by instrumenting step() directly at a held 260ms cadence:
    # **321 live calls in 2.5s, where a continuous glide is ~600**. The
    # cadence is now tracked separately and unpruned (_track_cadence),
    # weighted toward the newest interval and biased to lengthening,
    # since slowing down is the case that strands a notch. After:
    # **555-559 calls, 3% zero-move** - continuous.
    #
    # **Two faults in my own harness, both of which flattered or framed
    # the app wrongly**, and both worth recording because they produced
    # confident numbers:
    #   - the frame probe was left installed while the fast-burst stop
    #     test ran, so that burst's notches were folded into the sweep's
    #     bands - a held 260ms run reported a >2.4k px/s band that cannot
    #     exist at that cadence;
    #   - the bands are speed-filtered, and with slow scrolling now
    #     continuous at ~339px/s its frames land in the *mid* band. The
    #     "<250px/s" bucket holds only the start and stop transients:
    #     **1515 of 1554 frames are mid, at 1% dead and 0% jitter**,
    #     while the slow bucket holds 13-25 frames. Reporting a
    #     percentage of a 13-frame set as "the slow band is 42% broken"
    #     is what the last build did, and it was wrong.
    #
    # **The reader, measured on his One Piece chapter** (his ask): the
    # real entry on 3asq.online, 12254px of strip, ready 1.0s after the
    # page opened. Under the same sweep it holds **0% dead frames in
    # every band**, stop 232ms, worst step 12px.
    #
    # It also turned up a bug the grids could not show: the synchronous
    # tick that makes the first notch move at once runs per *notch*, and
    # at a rapid wheel several land between two paints, each integrating
    # another whole frame of travel. The screen sees the sum - `worst
    # 21.0px` against a 10px ceiling every other surface was holding. It
    # is now bounded to once per frame; the queued travel is not lost,
    # the ordinary tick delivers it. Worst fell to 12px and the lurch
    # count roughly halved.
    #
    # The queue bound is 320px rather than 420: the cap is per frame, so
    # a surface that paints slower spends more wall-clock on the same
    # coast - the reader measured 313ms at 420px against the grids' 167.
    # 1.10.197: scrolling measured the way he asked for it - "going from
    # minimum speed to the max gradually then vice versa in all pages".
    # Every harness before this drove a *fixed* notch cadence, which is
    # exactly what hides these faults: a constant rate never crosses the
    # boundaries where a glide restarts, a cap engages or a threshold
    # flips. The new one (scratch: ramp.py) sweeps 260ms -> 28ms -> 260ms
    # and reports per speed band. What it found, on the Anime grid:
    #
    #     band        before                     after
    #     slow        dead 23%                   dead 41%, jitter 0%
    #     mid         dead 30%                   dead  4%, jitter 0%
    #     fast        x2 53, dead 16%            x2  8,   dead 5%
    #     rapid       worst 18.3px, dead 26%     worst 10px, dead 8%
    #     flick       jitter 96%, worst 19px     jitter 0%, worst 10px
    #
    # Four separate causes, each fixed at its own point:
    #
    # 1. **The after-image guard was per second, not per frame.** Scaling
    #    it by dt let a frame that arrived two refreshes late travel 20px
    #    in one go, which the ramp found as `worst 20.0px` in every band.
    #    It is now a flat per-frame ceiling; falling a little behind is
    #    the documented trade (see MAX_DT), and worst is 10px everywhere.
    # 2. **A dead-constant speed starts late and stops dead.** A notch
    #    was a fortieth of its travel in the first frame, then full speed
    #    until it halted in one - his "when I start it takes time to
    #    start moving, and when I stop it takes a while to stop". The
    #    view now runs at a held constant speed **while the wheel is
    #    turning** and eases exponentially into rest **only once it
    #    stops**. Two profiles, because one cannot do both jobs: an
    #    exponential everywhere makes every notch a decelerating pulse,
    #    which is the "ticks jump" of the morning coming back.
    # 3. **The delivery time now comes from his own cadence** - about
    #    1.15x the measured gap between notches, so the next notch lands
    #    while the last is still running and slow scrolling never lapses.
    #    A fixed glide could not suit both ends: at 260ms apart it
    #    finished early and the view stood still over half the time.
    # 4. **The coast is bounded by the queue, not by the curve.** With
    #    the step capped per frame, a bank of notches takes as many
    #    frames as it is long, so a heavy page coasted further: Movies
    #    (265 cards) kept moving **519ms** after a fast burst where Anime
    #    (60) took 182ms. Queued travel is capped at 420px, and a fast
    #    burst now stops in 133-216ms. The honest cost: a very long flick
    #    travels less far than the notches asked for.
    #
    # Scrolling is 35% faster on top of all of it (100px a notch).
    #
    # **Not fixed, and said plainly**: below ~250px/s, 21-41% of frames
    # still repeat the previous position (jitter 0% - the steps that do
    # happen are even, and paints are at the refresh rate, so these are
    # not surplus frames). Every band above it is 2-9%. I could not find
    # the mechanism inside this pass; it is the slowest deliberate
    # scrolling, where the content moves about a pixel a frame.
    # 1.10.196: his four from the same hour, and one crash of mine caught
    # on the way.
    #
    # **The after-image at speed was an uncapped velocity.** "All pages
    # shows after image while scrolling fast" - and the fixed-speed model
    # had no ceiling at all (MAX_SPEED was inf on both engines), so speed
    # scaled with how many notches were in flight: eight of them measured
    # **14.9px between two shown frames** on a grid and a 9-21px spread
    # on the pages. Content crossing that much is seen twice at once.
    # Both engines now cap at 10px a frame and let the glide run longer
    # instead, which is what Chromium does with a burst - measured after,
    # a uniform 10.00px with no spread. Capped per *frame*, not per
    # second, because what smears is the gap between two things the eye
    # is shown: per-second would let the 165Hz panel travel 50% further
    # between frames than the 240Hz one.
    #
    # **The slow-mid stutter was mine, from 1.10.195.** Snapping every
    # write to a whole screen pixel means a page moving 1-2px a frame has
    # to bank three before it may show any, so it moved in lumps. It now
    # snaps only above 2x the quantum, where the rounding is at most a
    # third of the step: Home went from 188 position changes in 1.8s back
    # to **375**, past the 319 it managed before any of this. Fast
    # scrolling still snaps, which is where the blur it was added for
    # actually shows. Settings is the same `scroll_area` path and is
    # fixed with them; so is the reader.
    #
    # **The glide is 0.12s again, and the 0.15 was treating a symptom.**
    # 0.12 had measured 96% jitter at the mid pace, which is why it was
    # lengthened; that was the whole-logical-pixel truncation, and with
    # that fixed the same 0.12 measures 0% jitter at every pace. A notch
    # completes in 120ms rather than 150 - his "remove the delay in
    # responding when I scroll". Scrolling is also 10% faster again
    # (74px a notch, 67 x 1.10).
    #
    # **A correction to what I reported for 1.10.195**: I gave 52.1ms as
    # the pages' wheel-to-first-pixel delay. Two better instruments put
    # the first write at **0.3ms** with no event round over 4.0ms; the
    # 52ms was the harness, not the app. The felt delay is the flat ramp
    # - only a tenth of a notch has arrived four frames in - which is
    # what the shorter glide addresses.
    #
    # Watching is off the sidebar again and on Home instead, where the
    # row already existed and had simply never had anything to draw:
    # series.json holds 0 entries on his machine while History holds 41.
    # It now falls back to the watchable half of History, preferring a
    # saved entry wherever one exists.
    #
    # The crash: the adaptive snapping above did arithmetic on
    # `_last_value` while it was still None, inside a Qt slot - which on
    # Windows ends the process with no traceback. It would have gone off
    # on the first scroll of any page. Found by a harness dying at exit
    # 127, which is the only reason this line is a note and not a report.
    # 1.10.195: the blur, and the trace, and the shake - one cause, and
    # not the one everything had been aimed at. The owner, 31 August
    # 2026: "while wheel scrolling and dragging the scroller, there is a
    # trace-like in the items, it gets blurry and like-shaking a little",
    # adding that it shows at mid to high speed. By then the motion was
    # even to 0% step jitter, so the fault was in what the paint did with
    # an even position: everything snapped to a whole *logical* pixel,
    # and on the 2K panel one of those is 1.333 real ones. Two frames
    # grabbed a pixel apart and diffed prove it - the content was not
    # moving, it was being redrawn:
    #
    #     scrolled by     before        after
    #     1 device px     53.7% of pixels differ    0.0%
    #     2 device px     53.7%                     0.0%
    #     4 device px      0.0%                     0.0%
    #
    # The old path was crisp only at 4 device pixels, which is exactly 3
    # logical ones - the single step size truncation got right. Every
    # other step re-rasterised every glyph on a new subpixel phase, which
    # is the blur, and it worsened with speed because the phase then
    # moves further per frame (91% of frames fractional at the mid pace,
    # 96% at the fast one). Truncating an even position also emitted
    # 3,3,2,3,6px where the motion was uniform - the shake.
    #
    # The painted grids now snap the offset to the device grid and hand
    # the remainder to the painter, so they keep full-rate motion *and*
    # crisp text: 0% jitter as before, dead frames 12-31% -> 2-3%. The
    # pages Qt lays out itself cannot take a fractional offset, so those
    # snap to the nearest whole screen pixel instead (3 logical px here,
    # 1 on the 1080p panel, where none of this ever applied and which is
    # why that screen always looked the better of the two).
    #
    # **This is also the answer to a complaint that has been open for
    # weeks** - "scrolling in home page is not smooth compared to
    # discover". Same test: Home 29.4% of pixels redrawn on a one-pixel
    # scroll, Discover 0.3%. Not the background frame, not the page
    # build - Home is rows of small text and Discover is artwork, so the
    # same fault was violent on one and invisible on the other.
    #
    # Scrolling is 15% faster, his ask on the same day: the notch is 67px
    # rather than 58 on all three surfaces. Raised as distance rather
    # than by shortening the glide because steady-state speed is
    # distance/notch-period exactly, so this is +15% with the overlap
    # that keeps consecutive notches from restarting the clock left
    # alone. Measured 670px over ten notches against 583 before.
    #
    # Watching is back on the rail, also his ask, at the head of the
    # watch block. It routes to Saved, not to the bare page - measured,
    # that lands on the page's Discover tab, which is browsing and is
    # already pinned two rows above.
    # 1.10.194: the wheel travels at one flat speed now, which is what he
    # asked for - "make it straight fixed speed like I am dragging the
    # scrollbar", with Stremio named as the reference. Stremio's shell is
    # WebView2, so the thing he is comparing against is Chromium's wheel:
    # a notch animates a fixed distance over a fixed time and later
    # notches extend that animation rather than stacking impulses on it.
    # Both engines (poster_grid.FrameMotion, widgets._Momentum) now do
    # exactly that - a target and one speed, recomputed on each notch,
    # integrated linearly - in place of an impulse decaying under
    # friction, whose whole character was fast-then-slow.
    #
    # Sampled inside the paint event on the Anime grid, one reading per
    # frame the screen is given, at three wheel paces:
    #
    #     pace     impulse (1.10.193)      fixed speed (this build)
    #     60ms     jitter 26%, 24% dead    jitter 0%, 5% dead
    #     100ms    jitter 28%, 30% dead    jitter 0%, 1% dead
    #     160ms    jitter 27%, 21% dead    jitter 0%, 6% dead
    #
    # Step-to-step jitter is zero at every pace, which is the definition
    # of the thing he asked for. The glide is 0.15s and not the 0.12s
    # first tried, because 0.12 fails at exactly the mid pace he
    # reported - see FrameMotion.GLIDE_S for that measurement. Drag and
    # post-player were re-measured underneath it and are unchanged (thumb
    # constant at 145px through a drag, 143-146 positions/s after
    # playback against 138 before).
    # 1.10.193: the after-image at mid wheel speed. Measured the way the
    # symptom describes itself - the position sampled inside the paint
    # event, one sample per frame the screen is given - on the Anime
    # grid at three paces. Against Qt's timer, 24-45% of presented
    # frames repeated the previous position and the next then moved
    # double; the shared vblank ticker (a thread) roughly halves the
    # step jitter (27->10%, 26->12%, 16->12%), cuts the double steps by
    # a third to a half and raises the distinct positions per second.
    # It is the default clock for every scrolling surface now, not only
    # after the player has been opened.
    #
    # This also corrects a claim of mine from 30 August: raising the
    # present cap to 240 was called neutral on a *rate* measurement,
    # and on an evenness measurement it is not - at 240 with Qt's timer
    # 28% of frames are duplicates against 6% at 120. The cap stays at
    # 240 because the ticker beats both, but the number that mattered
    # was never the frame rate.
    # 1.10.192 - sizing, on the reading of "the same" his last message
    # finally pinned down: with one factor on both panels the 1080p read
    # as LARGER, because the same pixel sizes cover more of a smaller
    # screen. Each monitor is now scaled to the same *logical* width
    # (helpers/screen_scale: 2560/1920 = 1.3333 and 1920/1920 = 1.0), so
    # both report a 1920x1080 desktop to the app and the layout is
    # identical on each - same cards per row, same proportions, zero
    # clipped labels on either. Both his panels are 27", so equal
    # logical size is equal physical size here too.
    #
    # The drag freeze and the post-player scroll clock from the last two
    # builds were re-measured under it and are unchanged.
    # 1.10.191 - the two he was still seeing.
    #
    # **Both monitors render at exactly 1.25 now.** The previous two
    # attempts each satisfied a reading of "the same size" he did not
    # mean; this one pins one factor for every screen, and it is the
    # 2K's own 125% - the screen he asked the 1080p to match. It needs
    # all three of Floor, QT_SCALE_FACTOR and QT_FONT_DPI, because
    # QT_SCALE_FACTOR multiplies a live per-monitor factor rather than
    # replacing it (1.25 gave 1.5625 on the 2K, measured) and Qt reads
    # point sizes off the panel's own DPI unless told otherwise. Both
    # panels: devicePixelRatio 1.250, identical cards, zero clipped
    # labels, art cut at 1.25.
    #
    # **The category scrollbar holds still while cards load.** The
    # re-anchor of 1.10.190 fixed the content but not the thumb: rows
    # landing mid-drag still resized it under the finger (145 -> 103px)
    # and slid it up the track. A drag now runs entirely against the
    # range it started with - thumb height constant, content step 25.5px
    # against a settled 27px, where the control lurched 134.6px in one
    # step and the thumb changed size mid-drag.
    # 1.10.190 - the three he was still seeing, each measured before and
    # after.
    #
    # **Leaving the player no longer costs the app its frame rate.** The
    # cause is mpv, but not where anyone was looking: with an mpv core
    # alive a QTimer at 4ms fires 93 times a second against 248 in a
    # clean process, while a plain Python thread at the same interval is
    # untouched (226 vs 227). Painting was never the victim - a tight
    # update() loop runs 405/s before and 378/s after. So the scroll
    # clock moves onto the shared vblank ticker, which is a thread, the
    # moment a core exists. Home glide after playing: 36 -> 103
    # positions a second, 11px steps back to 4px.
    #
    # **Both monitors are the same size again.** QT_FONT_DPI turns Qt's
    # per-monitor scaling OFF - measured - which pinned both panels to
    # 1.0 and made everything physically smaller on the denser 2K. It is
    # gone and the rounding policy passes each screen's own factor
    # through (1.25 and 1.00), with zero clipped labels on either.
    #
    # **The category scrollbar no longer jumps while cards load.** The
    # drag maps pointer travel through the content height, and rows
    # landing mid-drag changed it underneath: the step at the moment of
    # growth measured 143.5px against a typical 33px. The drag re-anchors
    # on growth now - 26.5px against a settled 39px, no discontinuity.
    # 1.10.189: the 2K/1080p difference is closed at its real cause -
    # Qt converts point sizes through each screen's own DPI (120 vs 96),
    # so every pt in theme.py drew 25% larger on the 2K panel while the
    # pixel boxes around it stayed put, which is the clipped text in his
    # two screenshots. QT_FONT_DPI=96 makes a point the same number of
    # pixels on both. Home's covers were blurry because images quantised
    # the device ratio up to a 0.25 grid - 1.1 became 1.25, so every
    # tile was cut 14% oversize and scaled back down; the step is 0.05
    # now and tiles land exactly on their slots. The painted grids no
    # longer raise a Quick overlay on a wheel notch, so the wheel and
    # the scrollbar drag take the same path (his instruction). A 1ms
    # timer period is held for the process. Filter buttons 46px, the
    # episode list 560px, the waste-bin glyph sized in pixels so it fits
    # its button, sidebar and card text a step smaller.
    #
    # **Not fixed, and now understood**: opening the player costs the
    # process two thirds of its paint rate for the rest of the session
    # (135 -> 45 paints/s, measured). It is libmpv's core init, not any
    # option, window, thread or timer - see video_backend for the eleven
    # hypotheses that were eliminated and the only fix that can work.
    # 1.10.188: the mid-playback freeze is fixed at its real cause -
    # the container tail sat at playback's own priority long after the
    # index bytes had landed, so on a swarm delivering 1.5x realtime
    # the head and the tail split the line and playback froze **19.7s**
    # at 5.9s in (reproduced twice, same timestamps; zero stalls after,
    # holding exact realtime). mpv's cache knobs were tried on the same
    # rig first and measured as changing nothing - they are not in this
    # build. Sizes: the app-wide factor is 1.1 on every monitor and
    # card sizes no longer track screen width, so a card is the same on
    # the 2K and the 1080p panel (160x216 logical, 176 device px);
    # Home's own are one step up (176x238). The volume flyout gets
    # rounder corners, a see-through frame only, and a bar against the
    # real 0-200 range so 100% sits in the middle.
    # 1.10.187: debrid/direct HTTPS plays through the same seekable
    # client stream as the engine (atomicd://) - the vendored libmpv
    # cannot seek any http, so a Real-Debrid MKV read its whole body
    # before opening (the owner's log: 108s to chapter markers; his
    # "stuck on 99% until it 100% loads"); verified 1.75s to first
    # frame on the case that took ~36s. A BD-menu file can no longer
    # override the addon's fileIdx (the 501-file ITA collection that
    # played a menu loop for S1E2). An in-player source switch extends
    # its 4s data window while peers are connected - the .torrent fast
    # path had eaten the handshake time the metadata wait used to
    # provide. The top bar's per-pixel compose gains an alpha-1 base
    # (whole controls clickable, drag gaps back) and event-driven
    # recompose; arrow keys seek/volume without waking the bars, volume
    # gets its own flyout. Wizard step dots are the hero's own pills.
    # High-DPI scale factors are floored - one size on every monitor.
    # 1.10.186: the playing-state top bar is composed with per-pixel
    # alpha (UpdateLayeredWindow) - bare glyphs and title on the video,
    # no per-control boxes (verified by screenshot; 248/250 sampled bar
    # pixels show video through). Loading is untouched, and the core
    # veil is skipped for a bar whose window is in per-pixel mode (the
    # SLWA/ULW clash faulthandler caught). Poster tiles can never draw
    # past their slot (the reading categories' enlarged-cover flash).
    # Measured and reported, not changed: find_streams first rows 0.93s
    # with the real addon list; mpv frame-drop/delay counters zero over
    # 18s in both direct and engine playback.
    # 1.10.185: the owner's 30 August afternoon list. "Part 2" no longer
    # reads as episode 2 (his AoT S1E2 was served the franchise concert
    # film - his own log, pick_file line), and a file naming itself a
    # movie/concert is refused for an episode ask however the addon's
    # fileIdx points; the playing-state top bar is clipped to its real
    # controls (player_top_bar_live_patch) so no strip crosses the
    # video; a live category grid keeps its on-screen order when the
    # sites' sweep lands and only appends (the Manga section's
    # reshuffle - Manhwa's single stable source never showed it).
    # Post-exit scroll stutter did not reproduce (event-loop p99 1.5ms
    # after close on direct AND torrent paths); during-playback stalls
    # (p99 10-13ms) are measured and still open - compositor/GIL side,
    # not the property bridge (exonerated by A/B).
    # 1.10.184: the vendored libmpv cannot seek HTTP at all (its own log:
    # "Seek failed", one request on the wire), so a moov-at-end MP4 read
    # the whole file before playing - the owner's "does not play until
    # 100%". Engine URLs now play through an atomic:// client stream
    # with a real seek (player_seekable_stream_patch): first frame 2.7s
    # after loadfile on the case that never played, MKV fresh play
    # unchanged, resume seeks land. Suggestion artwork resolves through
    # cover_fetch's catalogue chain, so the coverless reading rows (8 of
    # 8 measured) get art. The Read page draws last session's category
    # rows at once (scrollable at 0.14s against 2-7s of bare page). The
    # search panel centres under the field. Playback-time scroll spikes
    # were reproduced (15.8-18.6ms worst against 4ms idle, no Python in
    # the loop) - compositor contention, no app-side lever measured yet.
    #
    # 1.10.183: the owner's 30 August list, on top of 1.10.182's. The
    # Quick scroll compositor is retired outright (it cost 28-101ms of
    # stalled motion on the first glide after every page build); a
    # resume no longer spends its opening bandwidth on 12MB of head, and
    # the container index is fetched from the end of the file inwards -
    # the piece await_start actually gates on - so the seat resolves
    # instead of timing out; playback starts from the head and takes the
    # seat once its band is on disk, rather than opening at a seat whose
    # data is 20s away and showing nothing at all; the search
    # suggestions sit under the search field in full screen (they were
    # 114px left of it); the anime section confirms against AniList
    # beside its own request instead of after it; the schedule walks
    # every catalog until one *matches* rather than stopping at the
    # first that answers; and the player's upper bar is opaque, so it
    # looks the same over a picture as over the loading backdrop.
    updater.APP_VERSION = "1.10.220"
    try:
        updater._HEADERS["User-Agent"] = f"Atomic/{updater.APP_VERSION}"
    except Exception:
        pass
