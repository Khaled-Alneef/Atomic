"""In-app reader for Manga / Manhwa / Manhua, in Arabic.

A full-window page that sits *over* the app's page stack rather than in
it: it is opened from an entry and closed back to whatever page was
underneath, so it has no sidebar row and no place in main.PAGES (see
`open_reader` at the bottom - one hook, `reader.open_reader(window, entry)`).

Four things decide almost everything in here:

  * **One reading surface.** Continuous vertical scroll, always. A
    page-turning mode existed beside it and is gone: the owner reads
    manhwa and Arabic manga scans, both of which are read as a strip,
    and a second surface only ever meant a second set of bugs. What is
    left of direction is on-screen placement: Arabic and manga are read
    right to left, so the Next Chapter button sits on the **left** edge
    and Ctrl+Left steps forward. The *plain* arrows go the other way -
    Right = next, Left = previous, the owner's ask - see keyPressEvent.
  * **Reading order is not list order.** chapter_source hands chapters
    back newest first, so the next chapter to read is at index **- 1**.
    step_chapter is the one place that inversion lives; it shipped
    without it once and Previous/Next were each other.
  * **Two floating bars, revealed differently.** The top bar comes back
    on a scroll up or on the pointer nearing the top. The chapter
    controls on the floor - next, the jump list, previous - are hover
    only, and never move for a scroll: re-reading a panel is not a
    request to change chapter.
  * **Headers.** Most of these hosts answer an image request with 403
    unless it carries the Referer the source hands back, so every image
    fetch goes through _fetch_page_file with `headers` applied. A page
    that 403s says so and offers the browser, rather than showing a
    broken frame forever.
  * **Original size, centred.** There is deliberately no fit mode: a
    page is decoded at its own pixel size, drawn one image pixel per
    *device* pixel, and centred between the viewport's edges. Fit-width
    and fit-height used to exist and were removed - they re-decoded
    every page on every window drag and still softened scans that were
    already the right size. The +/- controls remain, but they now scale
    *from* that original size rather than choosing between fits, so the
    baseline is a constant and zoom is a multiplier on it.
  * **Memory.** A 200-page webtoon held as full-size QPixmaps is
    gigabytes: one long strip page is ~20MB decoded. So only the slots
    near the viewport hold pixels at all, and everything decoded goes
    through _PixmapCache, which is bounded in *bytes* rather than in
    entries (entries here differ in size by two orders of magnitude, so
    a count would bound nothing).

Decoding happens on lookup_pool's shared workers as a QImage - which is
safe off the UI thread, where QPixmap is not - and only the cheap
QImage->QPixmap conversion happens on the UI thread. Nothing here ever
spawns a thread per page: unbounded per-entry threads have already
saturated this user's whole home network once (see helpers/lookup_pool).
"""

import datetime
import math
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from collections import OrderedDict

from PyQt6.QtCore import QEvent, QObject, QRect, QSize, Qt, QTimer
from PyQt6.QtCore import pyqtSignal as Signal
from PyQt6.QtGui import (QBrush, QColor, QCursor, QFont, QFontMetrics, QImage,
                         QPainter, QPixmap)
from PyQt6.QtWidgets import (
    QApplication, QComboBox, QDialog, QFrame, QHBoxLayout, QLabel,
    QMenu, QPushButton, QScrollArea, QSizePolicy, QVBoxLayout,
    QWidget,
)

from helpers import (app_settings, child_process, downloads, history, images,
                     logs, lookup_pool, net, storage, theme)
from helpers.widgets import (Card, GlassPage, GlyphButton, confirm,
                             finish_toast, frameless_dialog,
                             freeze_covered, show_toast,
                             use_hover_cursor, _Momentum, screen_tick_ms,
                             screen_frame_s)
from windows.tracker import correct_progress, format_chapter_progress


def _mark_history(entry, numbers, read: bool):
    """Tick (or untick) these chapters in History - the store that also
    holds titles with no saved entry, and the only one that can record
    single chapters rather than one read-up-to number. Never raises: a
    history write must not cost a mark."""
    try:
        marks = [history.chapter_key(n) for n in numbers if n is not None]
        if marks:
            history.set_watched(entry, marks, read)
    except Exception:
        logs.exception("could not write the reading history")

# Imported defensively: this page is useful (and testable) with a stub
# source injected over it, and a missing module must read as "the reader
# has no source wired up" in front of the user rather than an ImportError
# that takes the whole app down at startup.
try:
    from helpers import chapter_source
except Exception:                                   # pragma: no cover
    chapter_source = None
else:
    # main preloads this module ~400ms after the window is up
    # (_preload_overlays), so the reader cache's 1.88 MB parse happens
    # on a worker long before any title is clicked - see
    # chapter_source.warm_cache_async for the measurement.
    try:
        chapter_source.warm_cache_async()
    except Exception:                               # pragma: no cover
        pass



# ---- budgets -------------------------------------------------------------
# One image request, and the whole chapter-listing chain. The listing is
# allowed longer because it is a search across a site, not one file.
PAGE_TIMEOUT = 12.0
# Raised from 25s when chapter_source began following a paginated
# chapter list. olympustaff prints 40 chapters per page and hangs the
# other 200 off `?page=2..7`; the full list for The Beginning After the
# End measures 21.7s cold (249 chapters, no gaps) where the first page
# alone was 13s for 41. It is paid once per series per six hours - the
# listing is cached - and the alternative is the list the owner
# reported: only the newest forty chapters, ending at the one just read.
CHAPTER_LIST_TIMEOUT = 45.0
CHAPTER_PAGES_TIMEOUT = 20.0

# Bigger than net.MAX_RESPONSE_BYTES on purpose: that ceiling is sized for
# API responses and covers, and a single webtoon strip image routinely
# runs past it.
#
# It lives in net.py now rather than here, because the *downloader* was
# reading the same page images at the API ceiling and silently dropping
# whatever went over it - see the note beside net.MAX_IMAGE_BYTES for
# the chapter that was measured short. Two copies of a number is how
# that happened; this alias keeps the local name and the single source.
MAX_PAGE_BYTES = net.MAX_IMAGE_BYTES

# Decoded pixmaps held at once. A long strip page is ~20MB, so this is
# roughly a dozen of the worst case and a few hundred of an ordinary
# manga page.
PIXMAP_BUDGET_BYTES = 192 * 1024 * 1024

# Never decode more than this many pixels from one image, whatever the
# zoom asks for. Scaling a page up is legitimate; scaling it up to
# something that costs hundreds of megabytes is not.
MAX_DECODED_PIXELS = 40_000_000

# Zoom, as a multiplier on the *medium's* baseline size. 100% is where
# every chapter opens.
ZOOM_STEP = 0.15
ZOOM_MIN, ZOOM_MAX = 0.25, 4.0

# What 100% means, per medium, as a multiple of the image's own pixels.
#
# It used to be 1.0 for everything - one image pixel per device pixel -
# and that is only right for one of the three. Manga scans are cut for
# print and come off these sites far wider than a page needs to be read
# at, so 100% was a page you had to zoom *out* of every time; manhwa and
# manhua are cut as narrow phone strips and arrived slightly too small.
# The numbers are the owner's own, measured against what he was setting
# by hand every chapter: manga 75% of the old baseline, the two vertical
# strips 115% - then raised again to 145% *of that* (1.15 x 1.45 =
# 1.67), the owner's ask, since 115% still opened smaller than the size
# every strip chapter was being zoomed to by hand.
#
# This is the baseline, not the zoom: the +/- controls and the label
# still read 100% here, so "100%" means "the size this medium opens at"
# rather than a number that has to be re-learned per series.
#
# **The two strips were then cut 40%** - the owner's ask, 22 August 2026:
# "make the manhua/manhwa ch pages size 40% smaller from now". 1.67 x
# 0.60 = 1.00, which for the 760px strips these sites cut is the scan's
# own pixels, one per device pixel. Manga is untouched: he named the two
# vertical media by name, and they are the two this table already holds
# apart from manga, so scoping the cut to them costs nothing.
#
# This number and MEDIUM_TARGET_WIDTH below have to agree, or every
# strip chapter opens at the baseline and then visibly snaps to the
# target the moment the first page decodes (see _on_page_width's 0.01
# guard, which is what stops that happening today). They agree when
# base == target / the medium's usual source width: 762 / 760 = 1.003.
MEDIUM_BASE_SCALE = {"manga": 0.75, "manhwa": 1.00, "manhua": 1.00}

# **What 100% is actually worth, in pixels on screen, per medium.**
#
# The multipliers above are a multiplier on *the source image's own
# width*, which means the same medium opens at a different size on every
# series - the owner's ask, 21 August 2026: "make the size of the ch
# reader view fixed for all manga/manhwa/manhua, but make it auto detect
# the correct size whenever I read".
#
# Measured 21 August 2026, real pages off the owner's own entries:
#
#     One Piece      (3asq)        1644 x 2400   single page
#     Kingdom (WAN)  (3asq)        1325 x ....   single page
#     Kingdom (WAN)  (3asq)        2760 x 1917   DOUBLE-PAGE SPREAD
#     Rise of the Fallen Kingdom's  760 x 13607  vertical strip (manhua)
#
# So under the old multipliers the owner's two manga opened at 1644x0.75
# = 1233px and 1325x0.75 = 994px - a 24% difference between two series
# of the same medium, which is exactly what had to be re-zoomed by hand
# every time. The strip opened at 760x1.67 = 1269px.
#
# The targets below are those same numbers made deliberate: the middle
# of the manga range, and the strip width the owner already reads at.
# 100% now means "this many logical pixels wide", so every series of a
# medium opens identically and the +/- zoom is a multiplier on a
# constant rather than on whatever the scanner happened to choose.
#
# The published widths agree: LINE Webtoon and Naver cut at 800px and
# most manhwa/manhua scans arrive between 720 and 900 (this one is 760),
# so a strip is always being scaled up a little to a readable column -
# which is what the owner was doing by hand.
#
# **Double-page spreads are untouched by this and must stay that way.**
# A spread is ~2x a single page (2760 against 1325 above), so scaling it
# by the same factor makes it ~2x the target and wider than the window -
# and _show's fit-to-viewport then shrinks it to fit, which is the
# behaviour the owner calls perfect. This changes the number that fit
# starts from, never the fit itself.
#
# **The two strips are 40% off those numbers as of 22 August 2026** -
# 1270 x 0.60 = 762, the owner's ask ("make the manhua/manhwa ch pages
# size 40% smaller from now"). Manga's 1100 is deliberately left alone:
# he named manhua and manhwa, they are the two long-strip media, and
# they are the two this table already separates from manga. Cutting the
# shared number would have shrunk his manga scans as well, which he did
# not ask for.
#
# Because a target is absolute, the cut is exactly 40% whatever the scan
# is cut at - the source only decides how much resampling it takes to
# get there. Measured over the 74 long-strip pages sitting in his own
# image cache, 22 August 2026, which is where these sites actually land:
#
#     source  700px  ->  was 1270 (1.81x up), now 762 (1.09x up)
#     source  720px  ->  was 1270 (1.76x up), now 762 (1.06x up)
#     source  760px  ->  was 1270 (1.67x up), now 762 (1.00x - untouched)
#     source  800px  ->  was 1270 (1.59x up), now 762 (0.95x down)
#     source  900px  ->  was 1270 (1.41x up), now 762 (0.85x down)
#
# So this is also the change that stops a strip being blown up at all.
# Every one of those pages used to be upscaled before it was drawn -
# 760 real pixels stretched to 1269 - and an upscale cannot add detail,
# only soften what is there. Crops of the two, 4x, side by side: the old
# render doubles every 1px rule into a grey pair, the new one draws it
# as the single clean line it is in the file.
MEDIUM_TARGET_WIDTH = {"manga": 1100, "manhwa": 762, "manhua": 762}

# **How tall a page has to be, relative to its width, to be read as a
# long strip whatever the entry calls itself.**
#
# The owner asked for manhua and manhwa to be cut 40%, and the widths
# above do exactly that - but measured over his own library on 22 August
# 2026, almost everything he actually reads as a strip is *typed*
# "Manga": Kingdom (WAN), One Piece and The Eternal Supreme are all
# typed Manga, and only three titles in history.json carry Manhwa or
# Manhua at all. So keying the width on the declared type alone would
# have left the change invisible on nearly every title it was asked for,
# and told him to go and retype his library instead.
#
# A page's proportions are not ambiguous about this. Measured across the
# 74 long-strip pages in his cache: 760 x 13607 is 17.9, and the tallest
# is 257 x 26171 at 102. A paged manga scan is 1.4-1.5, and a two-page
# spread is below 1.0 (and is excluded here anyway - only non-spread
# pages report). Three is nowhere near either population, so it
# separates them without a judgement call.
#
# The cost of this, stated plainly: a strip *typed* as manga opens at
# manga's baseline and snaps once to the strip width when the first page
# decodes, because nothing can know the shape before then. A correctly
# typed entry never snaps (its baseline already agrees - see
# MEDIUM_BASE_SCALE), and one snap on the first page beats reading a
# whole series at the wrong size.
STRIP_ASPECT_MIN = 3.0
STRIP_TARGET_WIDTH = 762
# A source this far off the target is not a page cut for reading - a
# thumbnail, a banner, a spread - and forcing it to the target would
# blow it up past any use. Outside this the medium's plain multiplier
# stands.
#
# Left where it was measured when the strips were cut to 762, which
# moves the window of source widths it accepts rather than resizing it:
# strips now normalise a source between 293px and 2177px (was 488-3629),
# manga is unchanged at 423-3143. The published strip range is 720-900
# and the owner's own manhua is 760, so his sources sit mid-window with
# 2.4x of headroom above them - and the case that falls *outside* got
# better, not worse: a rejected source now stands at the 1.00 baseline
# (its own pixels) where it used to stand at 1.67 and render half again
# wider than the target it had just been refused.
TARGET_SCALE_MIN, TARGET_SCALE_MAX = 0.35, 2.6
DEFAULT_BASE_SCALE = 1.0

# How far outside the viewport the strip keeps pixels. This margin is
# also what does the preloading - a slot one screen down is fetched
# before it is scrolled to.
STRIP_LOAD_MARGIN = 1600     # px of strip loaded past each viewport edge
STRIP_KEEP_MARGIN = 3600     # ...and past which pixels are dropped again

# A slot's guessed size before its image has decoded. Width is replaced
# by the first real page's width as soon as one arrives (pages within a
# chapter are all cut to the same width), so the guess only ever governs
# the very first screenful.
PLACEHOLDER_RATIO = 1.45     # height = width * this
DEFAULT_PAGE_WIDTH = 800     # logical px

# The page position is written back debounced, so a scroll doesn't write
# tracker.json on every frame.
SAVE_POSITION_MS = 1200

# Both bars float *over* the reading surface instead of sitting in the
# layout. Hiding a bar that is in the layout gives the strip the bar's
# height back, which yanks the artwork ~48px up the screen mid-read; an
# overlay costs one setGeometry per resize and moves nothing when it
# comes and goes.
BAR_HEIGHT = 48
BOTTOM_HEIGHT = 60      # the chapter controls, pinned to the window's floor
BAR_REVEAL_PX = 72      # pointer this close to the top brings it back
# Within this far of the strip's end counts as 'finished the
# chapter', which is when the next/previous controls appear on
# their own. Generous rather than exact: the last page of a
# webtoon is often a tall credits panel, and the controls being
# there slightly early is better than having to hunt for them.
END_OF_CHAPTER_PX = 260
BOTTOM_REVEAL_PX = 110  # ...and this close to the bottom, for that one
SCROLL_HIDE_DELTA = 12  # px of travel before a scroll counts as intent

# The three floating bottom controls: previous/next pinned to the window's
# two edges with the chapter list centred between them. Sized here rather
# than left to sizeHint so _place_bottom can do its arithmetic before any
# of them has been shown.
BOTTOM_CONTROL_HEIGHT = 34
# Where the door button lands, for the reader and the player alike -
# the owner's ask, replacing "back to whatever page this was opened
# from". main.PAGES' key, not a title.
HOME_PAGE = "home"

BOTTOM_STEP_WIDTH = 170
CHAPTER_BOX_WIDTH = 460     # wide on purpose - chapter names are long
BOTTOM_GAP = 16

# One wheel notch, in px of strip. Qt's default here is singleStep (20)
# times the OS's wheel lines (3) = 60px, measured - which on a webtoon
# chapter 30,000px tall is 500 notches from top to bottom. 300 was the
# first try and the owner still called it slow on a long webtoon; 640
# overshot a panel; 460 was called still slightly too fast; 380 was the
# next step down and the owner asked for slower again, so 320; and the
# owner asked for slower once more, so 260 - a bit over a quarter of a
# ~900px viewport, ~121 notches on the 31,365px Kingdom chapter
# measured earlier. Raised from Qt's 60 in the first place so a long
# webtoon does not need five hundred.
#
# 22 August 2026: still "too fast per wheel notch" at 260 - the fourth
# report of the same direction in a row (640->460->380->320->260, each
# one a "still too fast"/"slower again"). The last two steps down were
# only -60 each and each still came back as too fast, so this one moves
# further rather than repeating another timid -60: 180, a clean one
# fifth of the ~900px viewport (was a bit over a quarter) and ~174
# notches on the 31,365px Kingdom chapter (was ~121). No smooth-scroll
# animation sits on top of this to re-tune alongside it - a notch is one
# immediate bar.setValue, not an animated tween (checked: no
# QPropertyAnimation/QEasingCurve/QScroller anywhere in this file) - so
# a smaller step cannot go "sluggish" against a mismatched animation
# duration the way it could if one existed; it only ever changes how far
# one notch moves.
#
# 24 August 2026: **108, down 40% from 180** - the owner: "make scrolling
# speed 40% slower in the whole app", this time with no reading-mode
# carve-out (the 23 August ask had one; this one names the whole app,
# and every one of the reader's own six tunings above went the same
# direction). The "no smooth-scroll animation sits on top of this" note
# above is no longer true either: the same ask called the app "super
# super stiff", and a notch here was one immediate bar.setValue - the
# only surface in the app that still snapped. It now goes through
# widgets._Momentum like every other scroll area, so the number is a
# resting travel per notch, not a jump.
# **76, down 30% from 108 - the owner, 24 August 2026: "make the
# scrolling slower by 30% in the reader mode only!"** The rest of the
# app keeps its own notch; this constant is already the reader-only one.
# **30 - the owner's own number, 27 August 2026: "no no 30 instead of
# 24", correcting a first reading of "make it faster by 50%" as +50% on
# the travel (which gave 36). He wanted the constant itself set to 30,
# so that is what this is - not a percentage of anything.
#
# Fourth change to this one number in a day, and the asks below pull
# both ways; they are kept so the next person sees the whole swing
# rather than the last step of it.**
#
# **24 was a further 37% off 38, from "make it even smaller (mouse
# scroller steps per tick)".**
#
# **38, half of 76 - the same day: "make each mouse
# scroller tick travels 50% less".** The seventh tuning of this number
# and the fifth in the same direction; the history above is kept because
# two of those asks pulled the other way and the next person needs to
# see that. Halved rather than re-derived from the viewport, because
# NOTCH_FRACTION has been 0.0 since the notch became a flat pixel
# distance - `max(floor, height * 0.0)` is the floor - so this constant
# alone is the travel. The reader keeps its own constant, as it has since
# the 30% reader-only ask above.
WHEEL_STEP_PX = 30

# How fast a reader notch gives its speed back - see the _Momentum built
# in _StripView.__init__ for why this surface does not coast.
#
# **90/34, up from 34, and the ramp and the speed cap with it - the
# owner, 24 August 2026: "remove the mouse drift ENTIRELY from the
# reader view!!!". SUPERSEDED the same day - see the 50/60 note below;
# this paragraph is the reasoning that chose 90, kept because the
# uncapped MAX_SPEED half of it still stands.** Friction 34 was already the app's no-coast value
# and it was not enough here, because "settles" is not "stops": with
# RAMP 40 against FRICTION 34, a notch's covered fraction is
#
#     1 - (R.exp(-F.t) - F.exp(-R.t)) / (R - F)
#
# which is 88% at 100ms and 97% at 150ms - so an eighth of every notch
# was still arriving a tenth of a second after the wheel stopped, and on
# a 108px notch that is ~13px of visible travel with the hand off the
# wheel. At 100/90 the same expression is 98% at 60ms: eight or nine
# frames on a 144Hz panel, which still reads as movement rather than a
# jump (the owner's other standing complaint about this surface is that
# it snapped), and is over before the hand can notice.
#
# The speed cap is the other half and it is the half that actually
# *drifts*: MAX_SPEED parks whatever will not fit in `_pending` and
# feeds it back in as the velocity decays, so a fast flick's undelivered
# distance arrives after the flick. Uncapped here - the travel is the
# same either way (notches x WHEEL_STEP_PX), it is only delivered while
# the hand is still moving.
# **50/60, down from 90/100 - the owner, 24 August 2026: "fix the
# reader view also!!!", on the same complaint as the rest of the app
# ("the scrolling now in the whole app seems lower in fps").**
#
# 90 was set to answer "remove the mouse drift ENTIRELY", and it did -
# too well. Friction does not change how *far* a notch travels, only
# how long it takes, and at 90 the whole notch is delivered in 62ms.
# A steady hand sends a notch every 100-150ms, so the strip moved for
# 62ms and then sat still for the rest - which is not a frame-rate
# problem at all, it is the page being stationary between hops, and it
# reads as exactly the stutter reported. Computed per notch at 240Hz
# over the 76px notch this surface uses:
#
#     ramp/friction   98% delivered at   frames that move >=1px
#     100/90            62 ms             15      <- was
#      80/70            79 ms             18
#      60/50           108 ms             23      <- now
#      50/40           133 ms             27
#
# At 23 moving frames against a ~29-frame gap the hops now nearly touch,
# so a steady scroll is continuous rather than a string of jumps. What
# it costs is stated plainly: after the *last* notch the strip finishes
# its travel in 108ms instead of 62ms. It does not travel further - the
# distance is the same 76px - so this is not the drift that was removed
# (that was `_pending` deferring distance past the hand, and MAX_SPEED
# is still uncapped here so nothing is deferred at all).
#
# Deliberately not the app-wide 34: the rest of the app tolerates a
# ~150ms settle, and a reader is aiming at a panel rather than
# travelling, so it keeps the shorter one.
READER_WHEEL_FRICTION = 50.0
READER_WHEEL_RAMP = 60.0

# The gap drawn between pages. Zero for the vertical strips - a
# webtoon's panels are cut mid-image and any spacing draws a seam
# through the artwork - and a sliver for paged manga, whose pages are
# discrete printed pages: butted hard together they read as one long
# unbroken scan, which is what the owner asked to have visibly broken.
#
# **Keyed on the page's shape, not on the entry's declared type**, as of
# 22 August 2026 - the same signal and the same threshold the width uses
# (STRIP_ASPECT_MIN), deliberately not a second test of its own. Two
# detections that can disagree would eventually cut a manga's seam
# through a webtoon, which is the bug this replaces: the gap was keyed
# on the type, and measured over the owner's own history.json almost
# everything he reads as a long strip is typed "Manga" (Kingdom, One
# Piece, The Eternal Supreme - only three entries carry Manhwa or Manhua
# at all), so every webtoon he opened got manga's 6px drawn straight
# across artwork that had been sliced mid-panel.
#
# Measured after, driving the real ReaderPage with pages of the shapes
# his sources actually send: a strip page typed "Manga" opens at 6 and
# settles at **0**; One Piece 1644x2400 (aspect 1.46) and Kingdom
# 1325x1900 (1.43) stay at **6**; a strip typed Manhwa never leaves 0.
# The two slots then abut exactly - a 120x28px crop of the join at 8x
# shows one unbroken block of artwork with the diagonal running
# through it, against a solid 6px band of page background before.
#
# **Every page votes, and the vote latches** (22 August 2026, second
# pass). The verdict used to be taken from the first page that finished
# decoding, which is not page 1: three pages are requested at once and
# the smallest wins the race - on Swordmaster's Youngest Son ch.207,
# page 2 (800x14925) reported before page 1 (800x18835), measured live.
# A chapter opening on a short notice panel or a credits slice would
# have taken 6px for the whole chapter with nothing able to correct it
# afterwards. `_on_page_shape` now hears from every page and one strip
# anywhere closes the gap for good; it never re-opens inside a chapter.
#
# Verified against the owner's own two long-strip series, live, driving
# the real ReaderPage and reading the join off the screen: The Eternal
# Supreme (meshmanga, 800x9000, aspect 11.25) and Swordmaster's Youngest
# Son (olympustaff, 800x14925-20000, aspect 18.7-25.0) both report
# within 0.25-0.42s of the chapter opening, settle at 0, and their slots
# abut to the pixel (0 / 8550 / 17100 and 0 / 17893 / 32072).
MANGA_PAGE_GAP_PX = 6

# Down/Up arrow, same reasoning as the wheel: 120 was a tenth of a
# screen and unusable as a way to move down a strip.
ARROW_SCROLL_PX = 200

# Segoe Fluent Icons codepoints, the same family the sidebar and the
# player use (theme.FONT_STACK_ICONS) - monochrome, so they take the
# button's own colour instead of an emoji's.
#
# Written as \u escapes rather than as the characters themselves, and
# that is not style: these are private-use codepoints, and any tool that
# re-encodes this file turns each one into two or three mojibake boxes
# in the middle of the bar (CLAUDE.md records exactly that happening
# twice). An escape survives being read in the wrong codepage.
ICON_REFRESH = "\ue72c"               # Refresh
ICON_FULLSCREEN = "\ue740"            # FullScreen
ICON_EXIT_FULLSCREEN = "\ue73f"       # BackToWindow
ICON_LEAVE = "\ue76b"                 # ChevronLeft - the sidebar's own
                                      # fold glyph (main.FOLD_CLOSE_ICON),
                                      # which the player's exit and the
                                      # details page's back now carry too,
                                      # so one shape means "back" across
                                      # the whole app (the owner's ask)
ICON_CHAPTER_LIST = "\ue8fd"          # List - the same glyph the player's
                                      # episode list uses, so "the list of
                                      # things to open" is one icon in both
ICON_BROWSER = "\ue774"               # Globe
ICON_CHEVRON_DOWN = "\ue70d"          # ChevronDown - "this opens"
ICON_DOWNLOAD = "\ue896"              # Download
ICON_READ = "\ue73e"                  # CheckMark - a chapter already read

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Atomic/1.0"


# ---- fetching ------------------------------------------------------------
def _request_headers(headers) -> dict:
    """The headers one page image is fetched with.

    `headers` comes from chapter_source and is not decoration: most of
    these hosts check Referer and answer 403 without it. A User-Agent is
    added underneath rather than over the top, so a source that wants a
    specific one keeps it."""
    merged = {"User-Agent": USER_AGENT, "Accept": "image/*,*/*;q=0.8"}
    merged.update({str(k): str(v) for k, v in (headers or {}).items()})
    return merged


def _fetch_page_file(url: str, headers: dict):
    """`url` on disk, as (Path, None) - or (None, message) saying what
    went wrong in words meant for the user.

    Cached under images.page_path_for_url - the pages subfolder of the
    app's one image cache, kept apart so the cover-sized shrink passes
    never touch a page (see images.PAGES_DIR for what happened when
    they shared a folder). It is not images.download() only because that helper
    cannot carry headers, and headers are the whole difference between a
    page and a 403 here.

    Written to a temp name and renamed into place: a download cut off
    half way through must not be left behind as a cached file, because
    nothing would ever re-fetch it and the page would be permanently
    broken."""
    # PAGES_DIR, not the shared cover cache - the launch-time shrink
    # pass re-encoded every page stored there down to poster size (56 x
    # 1200 out of an 800 x 17000 strip, measured). See images.PAGES_DIR
    # for the full note. Pages ruined under the old path are simply not
    # found here any more and re-download fresh.
    path = images.page_path_for_url(url)
    try:
        if path.exists() and path.stat().st_size > 0:
            return path, None
    except OSError:
        pass
    request = urllib.request.Request(url, headers=_request_headers(headers))
    try:
        # Computed here rather than at submit time: a job can sit in the
        # pool's queue behind others, and a deadline started back then
        # would already be spent before the first byte is asked for.
        deadline = net.deadline_in(PAGE_TIMEOUT)
        with net.urlopen(request, timeout=PAGE_TIMEOUT) as response:
            data = net.read_bytes(response, deadline, MAX_PAGE_BYTES)
    except urllib.error.HTTPError as error:
        if error.code in (401, 403):
            return None, (f"The site refused this page (HTTP {error.code}). "
                          "That usually means it only serves images to its "
                          "own pages.")
        return None, f"The site answered HTTP {error.code} for this page."
    except Exception as error:
        return None, f"This page couldn't be downloaded ({type(error).__name__})."
    if not data:
        return None, "The site returned an empty page image."
    try:
        temp_path = path.with_name(path.name + ".part")
        temp_path.write_bytes(data)
        os.replace(temp_path, path)
    except OSError as error:
        return None, f"This page couldn't be saved to the cache ({error})."
    return path, None


def _drop_cached_page(url: str):
    """Forget the on-disk copy of one page image.

    Only Refresh calls this. "The images failed" and "the site replaced
    them" are the two reasons that button exists, and re-listing the
    chapter fixes neither on its own: a page that downloaded as a
    truncated or placeholder image is cached, and every later read would
    be served that same file forever."""
    try:
        images.page_path_for_url(url).unlink(missing_ok=True)
        # And the pre-pages-dir location, so Refresh also clears a copy
        # ruined by the old shrink pass.
        images.cache_path_for_url(url).unlink(missing_ok=True)
    except OSError:
        pass


def zoom_key(zoom) -> int:
    """A zoom as the hashable thing everything here caches by: an int
    percentage, not the float, so two arithmetically-equal zooms can't
    produce two cache entries."""
    return int(round(float(zoom) * 100))


def _decode_size(natural: QSize, key: int) -> QSize:
    """The size an image is decoded at: its own, times the zoom.

    At 100% - where every chapter opens - this is the image untouched,
    so nothing is resampled at all in the ordinary case. The
    MAX_DECODED_PIXELS ceiling is the one thing that can override the
    answer, because the alternative to a slightly soft page is the app
    dying on a 400MB decode."""
    if natural.isEmpty():
        return natural
    scale = max(0.01, key / 100.0)
    pixels = natural.width() * natural.height() * scale * scale
    if pixels > MAX_DECODED_PIXELS:
        scale *= (MAX_DECODED_PIXELS / pixels) ** 0.5
    if abs(scale - 1.0) < 1e-6:
        return natural
    return QSize(max(1, round(natural.width() * scale)),
                 max(1, round(natural.height() * scale)))


def _centre_scrollbar(bar, maximum):
    """Put a scroll area's horizontal scroll in the middle of its range.

    Centring the *widget* inside the layout is only half of "the chapter
    sits in the middle": as soon as one page is wider than the window -
    a double-page spread in One Piece is 2630px against a 1488px
    viewport, measured - the content is wider than the viewport, the
    horizontal scroll starts at 0, and every ordinary page is drawn
    off to one side and clipped. Screenshotted before this existed.

    Wired to `rangeChanged` rather than called by hand from six places,
    which is also what makes it leave the reader's own panning alone: a
    drag sideways doesn't change the range, so nothing fights it. Only a
    rebuild, a resize, a zoom, or a wider page arriving does."""
    if maximum > 0:
        bar.setValue(maximum // 2)


class _PageSignals(QObject):
    """The worker -> UI boundary for page images. A widget is never
    touched from a pool thread; this is the only thing that crosses."""

    decoded = Signal(int, int, object, int)  # run, index, QImage, zoom key
    failed = Signal(int, int, str)           # run, index, message


class _ChapterSignals(QObject):
    listed = Signal(int, object, str)     # run, chapters or None, error
    # The first page of a paginated list, handed over before the rest is
    # fetched - see chapter_source._site_chapters, which has published
    # this all along and had nobody listening. A full list is up to six
    # more page fetches at a time (21.7s measured on olympustaff for 249
    # chapters), and the reader showed nothing at all for the whole of
    # it; the first page is the newest forty, which is what someone
    # opening a series is nearly always reaching for.
    partial = Signal(int, object)         # run, chapters so far
    opened = Signal(int, object, str)     # run, pages dict or None, error


class _PixmapCache:
    """Decoded pages, bounded by total bytes.

    Bytes rather than a count (which is what images.py can get away with,
    since every thumbnail there is the same size): one entry here can be
    20MB and another 200KB, so a count would bound nothing at all."""

    def __init__(self, budget=PIXMAP_BUDGET_BYTES):
        self._items = OrderedDict()
        self._bytes = 0
        self._budget = budget

    @staticmethod
    def _cost(pixmap: QPixmap) -> int:
        return max(1, pixmap.width() * pixmap.height() * 4)

    def get(self, key):
        pixmap = self._items.get(key)
        if pixmap is not None:
            self._items.move_to_end(key)
        return pixmap

    def put(self, key, pixmap, keep=()):
        if key in self._items:
            self._bytes -= self._cost(self._items.pop(key))
        self._items[key] = pixmap
        self._bytes += self._cost(pixmap)
        self._evict(keep)

    def _evict(self, keep=()):
        protected = set(keep)
        for key in list(self._items):
            if self._bytes <= self._budget:
                return
            if key in protected:
                continue
            self._bytes -= self._cost(self._items.pop(key))

    def clear(self):
        self._items.clear()
        self._bytes = 0


class _PageStore(QObject):
    """Fetch + decode + cache for the pages of one chapter.

    Keyed by (page index, zoom). It used to be keyed by a whole fit box;
    with one rendering mode the only thing that can still change a
    page's decoded size is the zoom, so that is all the key carries."""

    ready = Signal(int)          # page index, now in the cache
    failed = Signal(int, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._signals = _PageSignals()
        self._signals.decoded.connect(self._on_decoded)
        self._signals.failed.connect(self._on_failed)
        self._cache = _PixmapCache()
        self._urls = []
        self._headers = {}
        self._pending = set()      # (index, zoom key) already queued
        self._errors = {}
        self._keep = set()
        # Carried into every job and checked on the way back: a chapter
        # opened after this one must never have its slots filled by the
        # previous chapter's images, which is the same run-id rule the
        # tracker's lookups follow.
        self._run = 0

    def configure(self, urls, headers):
        self._run += 1
        self._urls = list(urls or [])
        self._headers = dict(headers or {})
        self._pending.clear()
        self._errors.clear()
        self._keep.clear()
        self._cache.clear()

    def count(self):
        return len(self._urls)

    def urls(self):
        return list(self._urls)

    def url(self, index):
        if 0 <= index < len(self._urls):
            return self._urls[index]
        return ""

    def error(self, index):
        return self._errors.get(index)

    def set_keep(self, keys):
        """Cache keys the views are currently *showing*, which eviction
        must not take out from under them - dropping the page on screen
        to make room for the one being preloaded is how a reader ends up
        blinking."""
        self._keep = set(keys)

    def cached(self, index, key):
        return self._cache.get((index, key))

    def request(self, index, key):
        """Queue page `index` at zoom `key` unless it is already cached,
        already queued, or already known to have failed."""
        if not (0 <= index < len(self._urls)):
            return None
        cache_key = (index, key)
        pixmap = self._cache.get(cache_key)
        if pixmap is not None:
            return pixmap
        if cache_key in self._pending or index in self._errors:
            return None
        self._pending.add(cache_key)
        lookup_pool.submit(_decode_page_job, self._signals, self._run, index,
                           self._urls[index], self._headers, key)
        return None

    def _on_decoded(self, run, index, image, key):
        if run != self._run:
            return
        cache_key = (index, key)
        self._pending.discard(cache_key)
        # QImage -> QPixmap has to happen here, on the UI thread; it is
        # also the cheap half (~0.1ms against several ms of decode), which
        # is exactly why the expensive half is done in the worker.
        pixmap = QPixmap.fromImage(image)
        if pixmap.isNull():
            self._errors[index] = "This page couldn't be decoded as an image."
            self.failed.emit(index, self._errors[index])
            return
        self._cache.put(cache_key, pixmap, keep=self._keep)
        self.ready.emit(index)

    def _on_failed(self, run, index, message):
        if run != self._run:
            return
        for key in [k for k in list(self._pending) if k[0] == index]:
            self._pending.discard(key)
        self._errors[index] = message
        self.failed.emit(index, message)


def _decode_page_job(signals, run, index, url, headers, key):
    """One page: download and decode. Runs on a lookup_pool worker.

    Must never raise - an uncaught exception here would be swallowed by
    the pool and the slot would wait forever with nothing to show and
    nothing to say. Every path emits either `decoded` or `failed`.

    QImage rather than QPixmap on purpose: QPixmap may only be touched on
    the GUI thread, QImage may not be."""
    try:
        path, error = _fetch_page_file(url, headers)
        if path is None:
            signals.failed.emit(run, index, error or "This page couldn't be loaded.")
            return
        image = QImage(str(path))
        if image.isNull():
            signals.failed.emit(run, index,
                                "This page isn't a readable image - the site "
                                "may have sent an error page instead.")
            return
        target = _decode_size(image.size(), key)
        # **scaledToWidth, not scaled(..., KeepAspectRatio) - the column
        # was stepping 1px sideways at every page join.** KeepAspectRatio
        # re-derives the width from the *rounded* target height by integer
        # division, so the answer depends on the page's own height: at the
        # same 95% decode, Swordmaster's Youngest Son ch.207 page 1
        # (800x18835) came out **759** wide and page 2 (800x14925) **760**
        # - measured 22 August 2026, screen-read at the join, left edge
        # x=322 above it and x=321 below. Every page of a chapter is cut
        # to one width, so asking for that width and letting the height
        # follow makes every slot in the strip agree, and AlignHCenter
        # stops shifting the artwork under the reader.
        if target.width() != image.width():
            image = image.scaledToWidth(
                target.width(), Qt.TransformationMode.SmoothTransformation)
        signals.decoded.emit(run, index, image, key)
    except Exception as error:                       # pragma: no cover
        logs.exception("reader page decode failed")
        signals.failed.emit(run, index,
                            f"This page couldn't be loaded ({type(error).__name__}).")


def _screen_ratio(widget) -> float:
    """The display's scale factor.

    Off the *screen*, not the widget: a widget's own devicePixelRatioF is
    1.0 until it has been shown, and a pixmap sized from that is upscaled
    by the display afterwards and looks soft - the same trap
    widgets.magnifier_icon records."""
    screen = None
    window = widget.window() if widget is not None else None
    if window is not None:
        screen = window.screen()
    if screen is None:
        screen = QApplication.primaryScreen()
    return screen.devicePixelRatio() if screen is not None else 1.0


def _tagged(pixmap: QPixmap, ratio: float) -> QPixmap:
    """A pixmap decoded in *device* pixels, told what ratio it is in, so
    Qt draws it at the right logical size instead of stretching it. Every
    image in this reader goes through here - without the tag, every page
    is blurry on any non-100% display."""
    if pixmap is not None and not pixmap.isNull():
        pixmap.setDevicePixelRatio(ratio)
    return pixmap


# ---- chapter list --------------------------------------------------------
def is_arabic(chapter) -> bool:
    return str(chapter.get("lang") or "").lower().startswith("ar")


def chapter_number(chapter):
    """A chapter's number as a float, or None when it hasn't got one.
    Fractional on purpose - scanlators split chapters ("24.5"), the same
    reason tracker.parse_chapter_progress is a float."""
    raw = chapter.get("number")
    if raw is None:
        return None
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError):
        return None


def chapter_title(chapter) -> str:
    number = chapter_number(chapter)
    head = f"Chapter {format_chapter_progress(number)}" if number is not None else "Chapter"
    title = str(chapter.get("title") or "").strip()
    return f"{head} - {title}" if title else head


# A leading repetition of the chapter's own number, with whatever
# separator the site used between it and the name.
_LEADING_NUMBER_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*[-:–—.)]*\s*")


def chapter_name(chapter) -> str:
    """The chapter's own name for the list's centre column - without the
    number, which has a column of its own now.

    3asq writes the number into the name as well ("885 - <name>", all
    380 rows of Kingdom, measured), so leaving it there would print the
    same number twice on every row. Only a *leading* repetition of this
    chapter's own number is cut: olympustaff buries it mid-sentence
    instead, and pulling numbers out of the middle of somebody's title
    is guesswork. Falls back to the number when a source gives no name,
    so the column is never simply blank."""
    title = str(chapter.get("title") or "").strip()
    number = chapter_number(chapter)
    if title and number is not None:
        match = _LEADING_NUMBER_RE.match(title)
        if match and _as_number(match.group(1)) == number:
            title = title[match.end():].strip()
    if title:
        return title
    return (f"Chapter {format_chapter_progress(number)}"
            if number is not None else "Chapter")


def _as_number(text):
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def chapter_published(chapter) -> str:
    """A chapter's release date, readable - or "" when there isn't one.

    Empty rather than guessed, and that is the whole point of it.
    Measured across the owner's own entries: the scan sites put no date
    in their markup at all, so chapter_source hands back `published: ""`
    for every row of 3asq (380/380 blank), olympustaff and lavascans,
    and only the meshmanga v2 API carries a real timestamp (200/200
    filled, ISO with a Z). A plausible-looking made-up date would be
    indistinguishable from the real ones on the same list, which is
    worse than a blank column."""
    raw = str(chapter.get("published") or "").strip()
    if not raw:
        return ""
    try:
        moment = datetime.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        # A plain date, or a timestamp with a space where the T should
        # be - neither is worth its own parser, and anything else is a
        # shape nothing here produces.
        for shape in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                moment = datetime.datetime.strptime(raw[:len(shape) + 2], shape)
                break
            except ValueError:
                continue
        else:
            return ""
    return f"{moment.day} {moment.strftime('%b %Y')}"


# The chapter row's two flanks, and they are the *same* fixed width on
# purpose: the name between them is centred on the row only if what sits
# on either side of it is equally wide. Unequal clusters would put its
# centre off the row's by half their difference - the same arithmetic
# _place_title does for the title in the top bar.
ROW_SIDE_WIDTH = 178
ROW_NUMBER_WIDTH = 66


class _ElidedLabel(QLabel):
    """A one-line label that takes whatever width the layout gives it and
    ends in an ellipsis instead of being clipped mid-glyph.

    A plain QLabel cannot do this. With word wrap off its minimum size
    hint is the whole string, so one long chapter name would widen every
    row in a 500-row list past the viewport; with word wrap on it becomes
    two lines and that row is taller than the ones around it, which is
    exactly what a list you scan down a column must not do. `Ignored` as
    the horizontal policy is what lets the layout hand it a width rather
    than ask it for one."""

    def __init__(self, text="", parent=None, **kwargs):
        super().__init__(parent, **kwargs)
        self._full = str(text or "")
        self.setSizePolicy(QSizePolicy.Policy.Ignored,
                           QSizePolicy.Policy.Preferred)
        self.setToolTip(self._full)
        self._elide()

    def _elide(self):
        metrics = QFontMetrics(self.font())
        super().setText(metrics.elidedText(self._full,
                                           Qt.TextElideMode.ElideRight,
                                           max(0, self.width())))

    def setText(self, text):
        self._full = str(text or "")
        self.setToolTip(self._full)
        self._elide()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._elide()


class _ChapterListView(QWidget):
    """The chapter picker before reading starts: the series name, then one
    Card per chapter laid out number / name / release date, Arabic ones
    marked.

    Right-clicking a row marks that chapter read or unread - the only
    place on this page progress can be moved *down*, which is why it goes
    out as a signal for the page to run through tracker.correct_progress
    rather than being written from here."""

    picked = Signal(int)
    markRequested = Signal(int, bool)     # chapter index, finished?
    markAllRequested = Signal(bool)       # every chapter, finished?

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        # The series name. It was nowhere on this page - the only copy
        # was the top bar's, which was being elided to a single ellipsis
        # (see _place_title), so a list of 380 Arabic chapter names
        # carried nothing at all saying which series they belonged to.
        self._title = QLabel("", objectName="PanelTitle")
        self._title.setWordWrap(True)
        self._title.setVisible(False)
        layout.addWidget(self._title)

        self._notice = QLabel("", objectName="Muted")
        self._notice.setWordWrap(True)
        self._notice.setVisible(False)
        layout.addWidget(self._notice)

        # **No QVBoxLayout on the body - rows are placed by hand.** Every
        # row is the same height, and that uniformity is what a box
        # layout cannot exploit: with 508 rows in a QVBoxLayout, each
        # 24-row chunk's activation walked every item's sizeHint
        # recursion and the inter-chunk stall grew linearly - measured
        # 22 August 2026 on the owner's own One Piece list, 20ms at 24
        # rows to 230ms by 480, ~3.3s to fill and each stall an eyeful
        # over the 16.7ms frame budget. Pinning row sizeHints only cut
        # it 3x (67ms stalls); the same fill with rows never shown was
        # flat 8ms, so the whole growth was layout+paint. Fixed geometry
        # makes an insert O(1): below-the-fold chunks expose nothing and
        # paint nothing.
        self._body = QWidget(objectName="Bare")
        self._body.installEventFilter(self)
        self._row_h = 0                 # measured off the first real row
        self._skel_h = 0
        self._skeleton_rows = []

        # What the rows were built from, and one handle per row for
        # restyling in place - see set_chapters for why the rows are
        # kept rather than rebuilt on every visit.
        self._built_from = None
        self._row_state = []
        # Which build is current, so chunks queued by a superseded one
        # stop rather than filling the list with the wrong series, and
        # the marks the chunks have to apply as they land.
        self._build_token = 0
        self._marks = (-1, 0.0)

        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QFrame.Shape.NoFrame)
        area.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        area.setWidget(self._body)
        layout.addWidget(area, stretch=1)

    def set_title(self, text):
        self._title.setText(text or "")
        self._title.setVisible(bool(text))

    def set_notice(self, text):
        self._notice.setText(text or "")
        self._notice.setVisible(bool(text))

    # Manual row geometry - see __init__ for why there is no layout.
    ROW_GAP = 6
    ROW_RIGHT_PAD = 8       # clearance for the scrollbar, as the old
                            # layout's right contents margin was

    def _row_width(self):
        return max(0, self._body.width() - self.ROW_RIGHT_PAD)

    def _size_body(self):
        """Fix the body's height to the whole list the moment the row
        height is known, not as chunks land - so the scrollbar means the
        real list from the first frame, instead of a thumb that shrinks
        for three seconds while background chunks arrive."""
        if self._row_h and self._built_from:
            count = len(self._built_from)
            self._body.setFixedHeight(count * (self._row_h + self.ROW_GAP)
                                      - self.ROW_GAP)
        elif self._skel_h and self._skeleton_rows:
            count = len(self._skeleton_rows)
            self._body.setFixedHeight(count * (self._skel_h + self.ROW_GAP)
                                      - self.ROW_GAP)
        else:
            self._body.setFixedHeight(0)

    def _place_rows(self):
        """Re-width every placed row. Only ever needed when the body
        itself changes width (a window resize, the scrollbar appearing) -
        an insert never moves what is already there."""
        width = self._row_width()
        for index, row in enumerate(self._row_state):
            row["card"].setGeometry(
                0, index * (self._row_h + self.ROW_GAP), width, self._row_h)
        for index, ghost in enumerate(self._skeleton_rows):
            ghost.setGeometry(
                0, index * (self._skel_h + self.ROW_GAP), width, self._skel_h)

    def eventFilter(self, obj, event):
        if obj is self._body and event.type() == QEvent.Type.Resize:
            if event.size().width() != event.oldSize().width():
                self._place_rows()
        return super().eventFilter(obj, event)

    def _clear_rows(self):
        for row in self._row_state:
            try:
                row["card"].hide()
                row["card"].deleteLater()
            except RuntimeError:
                pass        # already gone with a dead panel
        self._row_state = []
        self._clear_skeleton()
        self._body.setFixedHeight(0)

    def _clear_skeleton(self):
        for ghost in self._skeleton_rows:
            try:
                ghost.hide()
                ghost.deleteLater()
            except RuntimeError:
                pass
        self._skeleton_rows = []

    # A screenful of placeholders; the real first chunk is 24 for the
    # same reason (CHUNK below).
    SKELETON_ROWS = 12

    def show_skeleton(self):
        """Placeholder rows while the source is being fetched cold.

        The cold path used to sit on a bare "Loading chapters..." until
        the site's first page parsed - 1-13s measured, against the
        owner's ask that the list *show* inside 100ms (22 August 2026).
        These are outlines, deliberately not fake chapter rows built
        from the entry's own numbers: a row that looks clickable and
        opens nothing would be worse than the wait it papers over.
        set_chapters clears them the moment anything real lands."""
        if self._row_state or self._skeleton_rows:
            return
        fm = QFontMetrics(self.font())
        # Approximates a real row: 10px margins each side of one line of
        # text plus the badge's padding. Close is enough for an outline
        # that only real rows replace.
        self._skel_h = fm.height() + 28
        width = self._row_width()
        for index in range(self.SKELETON_ROWS):
            ghost = QFrame(self._body)
            ghost.setStyleSheet(
                f"background: {theme.SURFACE}; "
                f"border: 1px solid {theme.BORDER}; "
                f"border-radius: {theme.RADIUS}px;")
            ghost.setGeometry(0, index * (self._skel_h + self.ROW_GAP),
                              width, self._skel_h)
            ghost.show()
            self._skeleton_rows.append(ghost)
        self._size_body()

    # How many rows to build before giving the event loop a turn.
    #
    # **24, down from 80, and the number is a screenful.** A row is 44px
    # and this window is ~900px tall, so 80 built four screenfuls of
    # cards nobody had scrolled to before the list could be shown at
    # all. Measured on the owner's own 380-chapter entry, 22 August 2026:
    # set_chapters cost 80ms and the first-show polish of those rows
    # another 72ms inside _show_only - 152ms of a 200ms budget for the
    # whole open, spent on rows 25 through 80.
    CHUNK = 24
    # And how long between chunks. **Not zero, and that is measured.** A
    # zero timer posts an event the loop drains in the round it is
    # already draining, so the whole chain ran before Qt sent a single
    # paint - all 380 of the owner's Kingdom rows were built before the
    # list appeared, and the list took 273-281ms to reach the screen
    # against a 200ms budget. 8ms puts a frame between chunks and fills
    # a 380-chapter list in about an eighth of a second, with the first
    # screenful on screen immediately.
    CHUNK_DELAY_MS = 8

    def set_chapters(self, chapters, current=-1, read_up_to=0.0):
        """Rebuild only when the chapter list itself changed; otherwise
        restyle the rows already built.

        This used to tear down and rebuild every row on every call,
        following the app's "pages rebuild from scratch" rule - but this
        list is not a page visit: it is re-entered on every Back press
        from inside a chapter, and a real list runs to hundreds of rows
        (249 measured for The Beginning After the End). Measured with
        250 fabricated chapters, offscreen: 426ms to rebuild plus 422ms
        of first-show polish for the fresh rows inside _show_only -
        which was the whole of "the back button takes ages". Between
        two visits the only thing that changes is which row is being
        read and which carry the tick, so exactly those rows are
        restyled in place (the same idea as the player's OverlayPanel
        rows), and the scroll position survives for free."""
        chapters = list(chapters)
        if chapters == self._built_from:
            self._update_marks(current, read_up_to)
            return
        self._build_token += 1
        token = self._build_token
        self._clear_rows()
        self._built_from = chapters
        self._marks = (current, read_up_to)
        # **The first screenful now, the rest on the event loop.**
        # Building every row up front is linear in a number this app
        # does not control: measured, One Piece's 1150 chapters cost
        # 1473ms to build and another 317ms to first-show, so opening a
        # chapter on a long series froze for most of two seconds on rows
        # nobody had scrolled to yet. The first chunk covers any
        # viewport, and reaches past the chapter being read so scrolling
        # to it works immediately.
        # Not widened to reach `current` any more: that made opening a
        # series at chapter 300 build three hundred rows before showing
        # anything, and nothing here scrolls to the current row - the
        # marks are re-applied by every chunk as it lands (_build_rows),
        # so the row being read gets its pill when its chunk arrives.
        first = self.CHUNK
        self._build_rows(0, min(first, len(chapters)), token)
        if first < len(chapters):
            QTimer.singleShot(self.CHUNK_DELAY_MS,
                              lambda: self._build_more(first, token))

    def _build_rows(self, start, stop, token):
        """One chunk of rows, unless a newer build has superseded this.

        Returns whether the chunk actually landed, so a chained
        `_build_more` stops instead of queueing the next one.

        **The token guards against a newer build; it does not guard
        against this panel being gone**, and that gap was fatal. The
        chunks are chained on `singleShot`, which keeps no reference to
        the widget, so closing the chapter list between chunks left a
        timer that fired into a deleted layout:

            reader.py:1035 <lambda> -> _build_more -> _build_rows
            RuntimeError: wrapped C/C++ object of type QVBoxLayout
                          has been deleted

        (from the owner's own log, 22 August 2026). PyQt6 turns an
        exception escaping a slot into `qFatal` - an immediate abort,
        not a traceback - so the whole window went. That is the owner's
        "it shows a small window then closes it in a moment": the list
        was building normally and the process died mid-fill."""
        if token != self._build_token:
            return False
        chapters = self._built_from or []
        try:
            width = self._row_width()
            for index in range(start, min(stop, len(chapters))):
                card = self._build_row(index, chapters[index])
                card.setParent(self._body)
                if not self._row_h:
                    # Every row is structurally identical (elided
                    # single-line labels, fixed-width flanks), so the
                    # first one's hint prices them all - and pricing
                    # them all once is what lets an insert be a
                    # setGeometry instead of a layout activation.
                    self._row_h = max(1, card.sizeHint().height())
                    self._size_body()
                card.setGeometry(
                    0, index * (self._row_h + self.ROW_GAP),
                    width, self._row_h)
                card.show()
            self._update_marks(*self._marks)
        except RuntimeError:
            # The panel went away between chunks. Move the token so any
            # timer already queued behind this one gives up too.
            self._build_token += 1
            return False
        return True

    def _build_more(self, start, token):
        """The next chunk, then hand the event loop back. Chained on a
        timer rather than a loop: the point is that the reader stays
        responsive - and scrolling, clicking or leaving mid-fill all get
        their turn between chunks. See CHUNK_DELAY_MS for why that timer
        is not a zero one."""
        if token != self._build_token:
            return
        chapters = self._built_from or []
        stop = min(start + self.CHUNK, len(chapters))
        if not self._build_rows(start, stop, token):
            return          # superseded, or the panel is gone
        if stop < len(chapters):
            QTimer.singleShot(self.CHUNK_DELAY_MS,
                              lambda: self._build_more(stop, token))

    def _update_marks(self, current=-1, read_up_to=0.0):
        """Move the "Reading" pill and the read ticks without touching
        the rows themselves. Only rows whose state actually changed are
        restyled - setStyleSheet re-polishes a widget, and re-polishing
        all ~250 would buy back a chunk of the rebuild just avoided."""
        for index, row in enumerate(self._row_state):
            reading = index == current
            number = row["number"]
            read = bool(read_up_to and number is not None
                        and number <= read_up_to)
            if reading != row["reading"] or read != row["read"]:
                self._apply_row_state(row, reading, read)

    def _apply_row_state(self, row, reading, read):
        # The chapter being read is marked on the row itself rather than
        # by selecting it: this list keeps no selection, and after
        # scrolling 500 rows "which one am I on" is not answerable from
        # anything else on screen.
        row["reading"], row["read"] = reading, read
        accent = (f"color: {theme.ACCENT}; font-weight: 800;"
                  if reading else "")
        row["number_label"].setStyleSheet(accent)
        row["name"].setStyleSheet(accent)
        mark = row["mark"]
        if reading:
            mark.setText("Reading")
            mark.setToolTip("")
            mark.setStyleSheet(
                f"color: {theme.ON_ACCENT}; background: {theme.ACCENT_GRADIENT}; "
                f"border-radius: {theme.RADIUS_SM}px; padding: 2px 10px; "
                f"font-weight: 700;")
        elif read:
            # Every chapter reading has reached carries the tick, not
            # only the one being read - the owner's ask: the list should
            # answer "which of these have I been through" at a glance.
            # A glyph in the SUCCESS green, the same vocabulary the
            # player's episode list uses for the same fact.
            mark.setText(ICON_READ)
            mark.setToolTip("Read")
            mark.setStyleSheet(
                f"color: {theme.SUCCESS}; font-family: {theme.FONT_STACK_ICONS}; "
                f"font-size: 10pt; background: transparent;")
        mark.setVisible(reading or read)

    def _build_row(self, index, chapter):
        """One chapter: number hard left, name centred, date hard right.

        Three cells rather than a run of widgets in one row, because
        "centred" here means centred on the *row* - see ROW_SIDE_WIDTH.
        The `group` field that used to sit in here is gone: every source
        chapter_source builds sets it to "" (all four site shapes and the
        MangaDex path), so it was a widget that could never have text.

        Built state-less on purpose: everything that changes between
        visits (the Reading pill, the tick, the accent) is applied by
        _apply_row_state, so the rows can be kept and restyled instead
        of rebuilt - see set_chapters."""
        card = Card(matte=True)
        row = QHBoxLayout(card)
        row.setContentsMargins(14, 10, 14, 10)
        row.setSpacing(12)
        arabic = is_arabic(chapter)

        left = QWidget(objectName="Bare")
        left.setFixedWidth(ROW_SIDE_WIDTH)
        left_row = QHBoxLayout(left)
        left_row.setContentsMargins(0, 0, 0, 0)
        left_row.setSpacing(8)
        number = chapter_number(chapter)
        number_label = QLabel(format_chapter_progress(number) if number is not None
                              else "-", objectName="CardTitle")
        number_label.setFixedWidth(ROW_NUMBER_WIDTH)
        left_row.addWidget(number_label)

        # The language is the point of this reader, so it is a badge and
        # not a suffix on the name: the owner is scanning for the Arabic
        # ones, and a word at the end of a variable-length line is not
        # something you can scan down a column. It sits in the left cell
        # for the same reason - a column, not a ragged edge.
        badge = QLabel("عربي" if arabic else
                       (str(chapter.get("lang") or "?").upper()))
        badge.setStyleSheet(
            f"color: {theme.ON_ACCENT if arabic else theme.TEXT_MUTED}; "
            f"background: {theme.ACCENT_GRADIENT if arabic else theme.SURFACE_HOVER}; "
            f"border: 1px solid {theme.ACCENT if arabic else theme.BORDER}; "
            f"border-radius: {theme.RADIUS_SM}px; padding: 2px 10px; "
            f"font-weight: 700;")
        left_row.addWidget(badge)
        left_row.addStretch()
        row.addWidget(left)

        # Elided, not wrapped - see _ElidedLabel: a name that wraps makes
        # its row taller than the 500 around it, and one that does not
        # elide widens every row to the longest name in the list.
        # The same setting the episode rows honour - it says "episode
        # and chapter numbers only", and the chapter half of that had
        # never been wired up anywhere but the title page.
        label = "" if app_settings.get_hide_entry_names() else chapter_name(chapter)
        name = _ElidedLabel(label, objectName="CardTitle")
        name.setAlignment(Qt.AlignmentFlag.AlignCenter)
        row.addWidget(name, stretch=1)

        right = QWidget(objectName="Bare")
        right.setFixedWidth(ROW_SIDE_WIDTH)
        right_row = QHBoxLayout(right)
        right_row.setContentsMargins(0, 0, 0, 0)
        right_row.setSpacing(8)
        right_row.addStretch()
        # The Reading pill / read tick, hidden until _apply_row_state
        # gives it a state. A hidden QLabel takes no space in the row,
        # so a bare row lays out exactly as it did when the mark was
        # only created on demand.
        mark = QLabel()
        mark.setVisible(False)
        right_row.addWidget(mark)
        published = chapter_published(chapter)
        date_label = QLabel(published, objectName="CardMeta")
        date_label.setAlignment(Qt.AlignmentFlag.AlignRight
                                | Qt.AlignmentFlag.AlignVCenter)
        # Kept in the layout even with nothing in it, so the flank stays
        # its fixed width and the names down the list stay on one centre
        # line whether or not a source dates its chapters.
        right_row.addWidget(date_label)
        row.addWidget(right)

        card.clicked.connect(lambda i=index: self.picked.emit(i))
        card.rightClicked.connect(
            lambda event, i=index: self._show_mark_menu(event, i))
        self._row_state.append({"card": card, "number": number,
                                "number_label": number_label,
                                "name": name, "mark": mark,
                                "reading": False, "read": False})
        return card

    def _show_mark_menu(self, event, index):
        """Mark a chapter read or unread from the list.

        Both wordings are always offered rather than one being worked out
        from the stored number: "finished" and "unfinished" are the two
        things the owner can want here, and hiding whichever one looks
        redundant means the menu changes shape row to row on a list he is
        scanning by position."""
        menu = QMenu(self)
        menu.addAction("Finished reading",
                       lambda: self.markRequested.emit(index, True))
        menu.addAction("Unfinished reading",
                       lambda: self.markRequested.emit(index, False))
        menu.addSeparator()
        menu.addAction("Mark all as read",
                       lambda: self.markAllRequested.emit(True))
        menu.addAction("Mark all as unread",
                       lambda: self.markAllRequested.emit(False))
        menu.exec(event.globalPosition().toPoint())


class _ChapterCombo(QComboBox):
    """The chapter jump list.

    Two things a plain QComboBox does not do here.

    *It draws its own down-chevron.* Painted rather than styled through
    `QComboBox::down-arrow`, which takes an `image:` url and nothing
    else - there is no icon file in this app to point one at, every
    arrow it draws is a Segoe Fluent glyph (theme.FONT_STACK_ICONS).
    Qt's own arrow is switched off in the same stylesheet so there is
    exactly one, and the box carries right-hand padding so a long
    chapter name does not run under it.

    *It marks the chapter being read.* Qt highlights the row the pointer
    is over and the row that is current, and after scrolling a
    500-chapter list neither of those is still visibly the one open.

    ...and a third that is not about drawing: right-clicking a row of the
    open list marks that chapter read or unread, so the mark is reachable
    from inside a chapter and not only from the list page."""

    markRequested = Signal(int, bool)     # chapter index, finished?
    markAllRequested = Signal(bool)       # every chapter, finished?

    def __init__(self, parent=None):
        super().__init__(parent)
        self._current_chapter = -1
        # Filtered on the popup's *viewport*, not on the box: the popup is
        # a separate top-level view with its own event stream, so a
        # contextMenuEvent on this widget never sees a press inside it.
        self.view().viewport().installEventFilter(self)
        # Background and border repeated from the app's own QComboBox
        # rule rather than inherited from it. Measured: with only
        # `padding-right` set here the box came out **transparent** over
        # the artwork and the chapter name was unreadable against a
        # webtoon's white panels - a widget-level stylesheet re-resolves
        # the whole rule for this widget, it does not merge property by
        # property with the application sheet.
        self.setStyleSheet(
            f"QComboBox {{ background: {theme.SURFACE}; color: {theme.TEXT};"
            f" border: 1px solid {theme.BORDER};"
            f" border-radius: {theme.RADIUS}px;"
            f" padding: 4px 26px 4px 10px; }}"
            f"QComboBox:hover {{ border: 1px solid {theme.ACCENT}; }}"
            f"QComboBox::drop-down {{ border: none; width: 26px; }}"
            f"QComboBox::down-arrow {{ image: none; width: 0px; height: 0px; }}")

    def clear(self):
        # The mark is an item role, so it goes with the items. Without
        # this a re-list (Refresh) leaves _current_chapter pointing at a
        # row that no longer carries the styling, and the next
        # set_current_chapter for the same index returns early and marks
        # nothing at all.
        self._current_chapter = -1
        super().clear()

    def set_current_chapter(self, index):
        if index == self._current_chapter:
            return
        for row in (self._current_chapter, index):
            if not (0 <= row < self.count()):
                continue
            marked = row == index >= 0
            font = QFont(self.font())
            font.setBold(marked)
            self.setItemData(row, font, Qt.ItemDataRole.FontRole)
            self.setItemData(row, QBrush(QColor(theme.ACCENT if marked
                                                else theme.TEXT)),
                             Qt.ItemDataRole.ForegroundRole)
        self._current_chapter = index

    def eventFilter(self, obj, event):
        if (obj is self.view().viewport()
                and event.type() == QEvent.Type.MouseButtonPress
                and event.button() == Qt.MouseButton.RightButton):
            row = self.view().indexAt(event.position().toPoint()).row()
            if row >= 0:
                menu = QMenu(self)
                menu.addAction("Finished reading",
                               lambda r=row: self.markRequested.emit(r, True))
                menu.addAction("Unfinished reading",
                               lambda r=row: self.markRequested.emit(r, False))
                menu.addSeparator()
                menu.addAction("Mark all as read",
                               lambda: self.markAllRequested.emit(True))
                menu.addAction("Mark all as unread",
                               lambda: self.markAllRequested.emit(False))
                menu.exec(event.globalPosition().toPoint())
            # Swallowed either way: a right press the view sees becomes a
            # change of current row, which on this box is a jump to that
            # chapter - marking one must not also open it.
            return True
        return super().eventFilter(obj, event)

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        font = QFont(painter.font())
        font.setFamilies(list(theme.FONT_FAMILY_ICON_FALLBACKS))
        font.setPointSize(8)
        painter.setFont(font)
        painter.setPen(QColor(theme.TEXT_MUTED))
        painter.drawText(self.rect().adjusted(0, 0, -9, 0),
                         int(Qt.AlignmentFlag.AlignRight
                             | Qt.AlignmentFlag.AlignVCenter),
                         ICON_CHEVRON_DOWN)
        painter.end()


# ---- continuous vertical (webtoon) reader -------------------------------
class _StripSlot(QLabel):
    """One page in the vertical strip.

    It keeps its height whether or not it is holding pixels, so dropping
    a far-away page's pixmap doesn't collapse the strip under the reader
    - which would move everything below it and throw the scroll position
    away."""

    def __init__(self, index, parent=None):
        super().__init__(parent)
        self.index = index
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setStyleSheet(f"color: {theme.TEXT_MUTED}; background: transparent;")
        self.setWordWrap(True)
        self.natural = None    # QSize of the decoded image, once known
        self.loaded = False


# Where the strip's body widget is parked, left of the viewport - see
# _StripView.paintEvent for the measurement that exiled it there.
#
# **Horizontal, and within +/-32767, both load-bearing.** Windows child
# widgets clamp their coordinates to a signed 16-bit range, so the
# first park (y = -(1<<24)) silently never landed: the position stayed
# clamped, the "is it parked yet" test never became true, and the Move
# handler re-parked forever - a native stack overflow that killed the
# process before faulthandler could print a frame. And vertical parking
# cannot work anyway: a chapter's body is hundreds of thousands of
# pixels tall, so any in-range y offset still leaves most of it inside
# the viewport. The body is ~2500px wide at most; 30000 to the left is
# out of sight for every window that fits on a desktop.
_BODY_PARK_X = -30000


class _StripView(QScrollArea):
    """Continuous vertical scroll - manhwa and manhua, which are drawn as
    one strip and have no pages to turn. Also the default for everything
    else: it is what the owner reads in."""

    positionChanged = Signal()
    ranOff = Signal(int)
    zoomRequested = Signal(int)     # +1 / -1 zoom steps (Ctrl+wheel)
    # The source width of this chapter's ordinary pages, in logical px at
    # 100% - emitted once per chapter, as soon as the first page that is
    # not a spread has been decoded. See MEDIUM_TARGET_WIDTH: it is what
    # lets the reader open every series of a medium at one size.
    pageWidthKnown = Signal(int, float)
    # ...and the shape of *every* page that decodes, not just the first.
    # Separate from the signal above because the two answers have
    # different lifetimes: the width may only be acted on once (it
    # re-decodes the whole chapter), while the gap has to keep listening
    # - see MANGA_PAGE_GAP_PX for the page that made that necessary.
    pageShapeKnown = Signal(float)          # height / width

    def __init__(self, store: _PageStore, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setWidgetResizable(True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        # Always on: a strip is taller than any window by definition, so
        # the bar is coming either way, and reserving its width up front
        # keeps the centred column from shifting sideways the moment the
        # first page arrives and the bar appears.
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        # The reading scrollbar, restyled to actually be visible: the
        # app-wide handle is SURFACE_HOVER, which over near-black artwork
        # margins is a bar the owner reported as unreadable. Wider, with
        # a lit handle and an accent hover, scoped to this view only so
        # the rest of the app's bars stay as they are.
        self.verticalScrollBar().setStyleSheet(
            f"QScrollBar:vertical {{ background: {theme.BG_ALT};"
            f" width: 14px; margin: 0px; border-left: 1px solid {theme.BORDER}; }}"
            f"QScrollBar::handle:vertical {{ background: {theme.TEXT_DIM};"
            f" min-height: 36px; border-radius: 6px; margin: 2px 2px; }}"
            f"QScrollBar::handle:vertical:hover {{ background: {theme.ACCENT}; }}"
            f"QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical"
            f" {{ height: 0; }}"
            f"QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical"
            f" {{ background: none; }}")
        self._store = store
        self._slots = []
        # One queued _sync_visible per event-loop turn - see _on_scrolled.
        self._sync_queued = False
        self._zoom = zoom_key(1.0)
        # See eventFilter: re-entrancy guard for the scrollbar wheel
        # forwarding, not state anything else reads.
        self._forwarding_wheel = False
        # The width the last decoded page came out at, in logical px.
        # Pages inside one chapter are cut to the same width, so the
        # first one that lands makes every later placeholder right.
        self._typical_width = DEFAULT_PAGE_WIDTH
        # Whether this chapter has already said how wide its pages are.
        # Once per chapter, not once per page - and deliberately not
        # reset by set_zoom, or re-scaling would ask again and again.
        self._reported_width = False

        self._body = QWidget(objectName="Bare")
        self._column = QVBoxLayout(self._body)
        self._column.setContentsMargins(0, 0, 0, 0)
        # No gap at all: a webtoon's panels are cut mid-image, and any
        # spacing between slots draws a visible seam through the artwork.
        self._column.setSpacing(0)
        self._column.addStretch()
        self.setWidget(self._body)
        self._park_body()
        # The viewport never shows anything but this class's own
        # painting (see paintEvent) - saying it is opaque lets Qt skip
        # every ancestor on every frame.
        self.viewport().setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent,
                                     True)
        # QScrollArea moves the body back whenever the body resizes (a
        # slot settling into its decoded height does that constantly
        # while a chapter loads) - watch for it and re-park.
        self._body.installEventFilter(self)
        # The canvas repaints from the bars' values; every write - the
        # wheel model, a jump, a drag of the bar itself - lands as one
        # update on the next frame.
        self.verticalScrollBar().valueChanged.connect(
            lambda _v: self.viewport().update())
        self.horizontalScrollBar().valueChanged.connect(
            lambda _v: self.viewport().update())

        store.ready.connect(self._on_page_ready)
        store.failed.connect(self._on_page_failed)
        self.verticalScrollBar().valueChanged.connect(self._on_scrolled)
        h_bar = self.horizontalScrollBar()
        h_bar.rangeChanged.connect(
            lambda _minimum, maximum: _centre_scrollbar(h_bar, maximum))
        # **The wheel died in the last 11px of the window and this is
        # why.** The vertical bar is AlwaysOn, so it owns the rightmost
        # SCROLLBAR_WIDTH px of the reading surface - and a QScrollBar
        # handles the wheel *itself* and accepts it, so wheelEvent below
        # never ran there. Measured on a 1400px window over a 31,365px
        # chapter: x <= 1388 moved 460px a notch, x >= 1389 moved 60
        # (Qt's wheelScrollLines x singleStep), which on a strip that
        # long is 0.19% of it and reads as nothing happening at all.
        # Filtered rather than fixed by widening the margin: the bar has
        # to stay where it is and stay draggable.
        for bar in (self.verticalScrollBar(), self.horizontalScrollBar()):
            bar.installEventFilter(self)
        # The wheel's motion - velocity and friction, shared with every
        # scroll area in the app (see widgets._Momentum and WHEEL_STEP_PX).
        # Grabbing the slider must win instantly over a glide still
        # writing values under it, same as widgets._SmoothWheel.
        # **No coast here, and that is deliberate** - the owner, 24
        # August 2026: "while in reader mode remove the scrolling
        # movement after the mouse scroll stops (there is a small
        # after-scroll movement) ONLY IN READER MODE". The pages keep
        # their momentum; a reader is aiming at a panel, so the strip
        # tracks the wheel and stops with it.
        #
        # Friction 34/s rather than the app's 7: a notch's unfinished
        # fraction is exp(-34t), so it is a pixel from done in ~110ms -
        # smooth to the eye, over before the hand notices - against
        # ~700ms of glide at 7. Acceleration is off for the same reason:
        # a fast flick through a chapter should cover more ground by
        # sending more notches, not by adding speed that outlives them.
        self._wheel_motion = _Momentum(
            self.verticalScrollBar(), lambda: screen_tick_ms(self), self,
            friction=READER_WHEEL_FRICTION, accel_max=1.0,
            ramp=READER_WHEEL_RAMP, max_speed=math.inf,
            frame_s=lambda: screen_frame_s(self))
        self.verticalScrollBar().sliderPressed.connect(self._wheel_motion.cancel)

    # ---- state -------------------------------------------------------
    def set_zoom(self, key):
        """Change the zoom without losing the reader's place.

        Every slot's pixels belong to the *old* zoom, so they are dropped
        rather than left on screen at the wrong size - the store still
        holds them, so zooming back is a cache hit. The scroll position
        is re-anchored on the page that was under the top of the
        viewport: heights are about to change by the zoom's ratio, and
        keeping the raw scrollbar value would land somewhere unrelated,
        several pages off at 4x."""
        if int(key) == self._zoom:
            return
        anchor = self.index()
        previous_zoom, self._zoom = self._zoom, int(key)
        # Carried across rather than reset to the default: the guess is
        # only wrong by the ratio between the two zooms, and the real
        # widths are what it was measured from.
        self._typical_width = max(1, round(
            self._typical_width * (self._zoom / max(1, previous_zoom))))
        for slot in self._slots:
            slot.setPixmap(QPixmap())
            slot.loaded = False
            slot.natural = None
        for slot in self._slots:
            self._resize_slot(slot)
        if 0 <= anchor < len(self._slots):
            self.verticalScrollBar().setValue(self._slots[anchor].y())
        self._sync_visible()

    def rebuild(self):
        while self._column.count() > 1:
            item = self._column.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._slots = []
        self._reported_width = False
        self._typical_width = max(1, round(DEFAULT_PAGE_WIDTH * self._zoom / 100.0))
        for index in range(self._store.count()):
            slot = _StripSlot(index, self._body)
            slot.setText("")
            # AlignHCenter is the whole of "the chapter sits in the
            # middle". Without it a fixed-size widget in a QVBoxLayout is
            # placed at its cell's top-*left*, so every page hugged the
            # left edge of the window however wide the window got.
            self._column.insertWidget(index, slot, 0,
                                      Qt.AlignmentFlag.AlignHCenter)
            self._slots.append(slot)
            self._resize_slot(slot)
        # A glide from the previous chapter must not keep writing into
        # the new one.
        self._wheel_motion.cancel()
        self.verticalScrollBar().setValue(0)
        self._sync_visible()

    def index(self):
        """Which page the reader is actually on - the slot under the top
        of the viewport, since in a strip "the page you are on" is the
        one you are looking at, not one you turned to."""
        top = self.verticalScrollBar().value()
        for slot in self._slots:
            if slot.y() + slot.height() > top:
                return slot.index
        return max(0, len(self._slots) - 1)

    def set_index(self, index):
        if 0 <= index < len(self._slots):
            self._wheel_motion.cancel()
            self.verticalScrollBar().setValue(self._slots[index].y())
            self._sync_visible()
            self.positionChanged.emit()

    def step(self, forward):
        """A screenful, not a page: this is a strip. Running off either
        end is what moves to the next/previous chapter."""
        bar = self.verticalScrollBar()
        amount = int(self.viewport().height() * 0.9)
        if forward and bar.value() >= bar.maximum() - 2:
            self.ranOff.emit(1)
            return
        if not forward and bar.value() <= bar.minimum() + 2:
            self.ranOff.emit(-1)
            return
        bar.setValue(bar.value() + (amount if forward else -amount))

    def scroll_step(self, dy):
        bar = self.verticalScrollBar()
        bar.setValue(bar.value() + dy)

    def wheelEvent(self, event):
        """A wheel notch moves WHEEL_STEP_PX, not Qt's 60.

        Done here rather than by raising the scrollbar's singleStep
        because QScrollArea recomputes that itself whenever the widget or
        the viewport is resized - and this strip resizes on every page
        that decodes, so a step set once would not survive the first
        image. A trackpad's pixel-delta scroll is left alone: it is
        already 1:1 with the finger and multiplying it would make the
        strip fly.

        Ctrl+wheel is zoom, not scroll - the shortcut every browser and
        image viewer has taught. Emitted as a request rather than acted
        on: the zoom (and the medium baseline folded into it) belongs to
        the page, not the view."""
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            steps = event.angleDelta().y() / 120.0
            if steps:
                self.zoomRequested.emit(1 if steps > 0 else -1)
            event.accept()
            return
        if not event.pixelDelta().isNull():
            super().wheelEvent(event)
            return
        steps = event.angleDelta().y() / 120.0
        if not steps:
            super().wheelEvent(event)
            return
        # wheel up = value down; the integrator clamps at the strip's
        # ends and reads the range live, so pages decoding mid-glide
        # (the strip resizes on every one) just extend where it can go.
        self._wheel_motion.kick(abs(steps) * WHEEL_STEP_PX, -1 if steps > 0 else 1)
        event.accept()

    def set_page_gap(self, gap):
        """The space drawn between page slots - see MANGA_PAGE_GAP_PX for
        which pages get one and why a strip's must not.

        A no-op when nothing changes, because every page of a paged manga
        now votes for the same 6 (see _on_page_shape) and
        QLayout.setSpacing invalidates the column whether or not the
        number moved - forty pages would be forty relayouts of a chapter
        that is not re-flowing at all."""
        gap = max(0, int(gap))
        if gap != self._column.spacing():
            self._column.setSpacing(gap)

    def eventFilter(self, obj, event):
        """Wheel over either scrollbar scrolls the strip, not the bar.

        See the installEventFilter note in __init__: without this the
        rightmost 11px of the window scrolled at Qt's default 60px a
        notch while the rest of it moved WHEEL_STEP_PX, which the owner
        reported as the wheel not working over there. Returning True
        consumes it so the bar's own handler cannot also run and add its
        60 on top."""
        # getattr, not a bare attribute: this filter is installed on the
        # scrollbars a few lines before _body exists, and an exception
        # inside a Qt virtual is a process abort, not a traceback.
        body = getattr(self, "_body", None)
        if body is not None and obj is body:
            if (event.type() in (QEvent.Type.Move, QEvent.Type.Resize)
                    and body.x() != _BODY_PARK_X):
                self._park_body()
                self.viewport().update()
            return False

        if (event.type() == QEvent.Type.Wheel
                and not self._forwarding_wheel
                and obj in (self.verticalScrollBar(), self.horizontalScrollBar())):
            # Guarded because wheelEvent's own fall-through calls
            # QScrollArea's handler, which hands the event straight back
            # to the scrollbar - and without the flag that arrives here
            # again and recurses until the stack ends. A trackpad's
            # pixel-delta scroll takes that path on every event.
            self._forwarding_wheel = True
            try:
                self.wheelEvent(event)
            finally:
                self._forwarding_wheel = False
            return True
        return super().eventFilter(obj, event)

    # ---- sizing ------------------------------------------------------
    def _placeholder_size(self):
        # Deliberately *not* clamped to the viewport: the guess exists to
        # make the strip's total height roughly right before anything has
        # decoded, and a width clamped to a narrow window would
        # under-guess every height and make the scrollbar lurch as the
        # real pages arrive.
        width = max(1, self._typical_width)
        return width, int(width * PLACEHOLDER_RATIO)

    def _resize_slot(self, slot):
        """The slot's size: the decoded image's own, in logical pixels,
        once it is known - and a guess before that.

        Changing the height of a slot *above* the viewport moves
        everything below it, so the scrollbar is compensated by the same
        delta. Without that, a page settling into its real height while
        another is being read scrolls the reader somewhere else
        mid-panel - and on a webtoon, where the guess is out by a factor
        of three, it does so violently."""
        ratio = _screen_ratio(self)
        if slot.natural is not None and not slot.natural.isEmpty():
            width = max(1, round(slot.natural.width() / ratio))
            height = max(1, round(slot.natural.height() / ratio))
        else:
            width, height = self._placeholder_size()
        previous = slot.height()
        if width == slot.width() and height == previous:
            return
        bar = self.verticalScrollBar()
        above = slot.y() + slot.height() <= bar.value()
        slot.setFixedSize(width, height)
        if above:
            delta = height - previous
            bar.setValue(bar.value() + delta)
            # **And the glide is told, or it undoes this.** The wheel
            # model carries its own float position and writes
            # int(round(...)) every refresh; a correction it does not
            # know about is one it writes straight back over, costing a
            # repaint each time. See widgets._Momentum.shift for the
            # measurement - it is the whole of the reader's "scrolling
            # upward is really low fps".
            for motion in (getattr(self, "_wheel_motion", None),
                           getattr(self, "_drag_motion", None)):
                if motion is not None:
                    motion.shift(delta)

    # ---- loading -----------------------------------------------------
    def _sync_visible(self):
        """Give pixels to the slots near the viewport, take them back
        from the ones far outside it. This is also what preloads: a slot
        one screen below has already been asked for by the time it is
        scrolled to."""
        if not self._slots:
            return
        bar = self.verticalScrollBar()
        top = bar.value()
        bottom = top + self.viewport().height()
        keep = []
        ratio = _screen_ratio(self)
        for slot in self._slots:
            slot_top, slot_bottom = slot.y(), slot.y() + slot.height()
            near = (slot_bottom > top - STRIP_LOAD_MARGIN
                    and slot_top < bottom + STRIP_LOAD_MARGIN)
            far = (slot_bottom < top - STRIP_KEEP_MARGIN
                   or slot_top > bottom + STRIP_KEEP_MARGIN)
            if near:
                keep.append((slot.index, self._zoom))
                pixmap = self._store.request(slot.index, self._zoom)
                if pixmap is not None and not slot.loaded:
                    self._show(slot, pixmap, ratio)
                elif pixmap is None and not slot.loaded:
                    slot.setText(self._store.error(slot.index) or "")
            elif far and slot.loaded:
                # Height is kept (see _StripSlot) - only the pixels go.
                slot.setPixmap(QPixmap())
                slot.loaded = False
        self._store.set_keep(keep)

    def _show(self, slot, pixmap, ratio):
        # A landscape page is a double-page spread (manga pages are cut
        # portrait; only a spread comes out wider than tall), and at the
        # chapter's zoom it lands about twice the viewport wide - the
        # owner was zooming out for page 3 of every such chapter and
        # back in for page 4. Fitted to the window's width instead,
        # display-only: the cache keeps the decode untouched, so nothing
        # is re-fetched when the window or the zoom changes, and
        # _typical_width is not fed a spread's width (it sizes the
        # placeholders for the ordinary pages).
        shown = QPixmap(pixmap)
        is_spread = pixmap.width() > pixmap.height()
        if is_spread:
            fit = self.viewport().width() * ratio
            if 0 < fit < shown.width():
                shown = shown.scaledToWidth(
                    int(fit), Qt.TransformationMode.SmoothTransformation)
        slot.natural = shown.size()
        slot.setText("")
        slot.setPixmap(_tagged(shown, ratio))
        slot.loaded = True
        if not is_spread:
            self._typical_width = max(1, round(pixmap.width() / ratio))
            # Every page, every time - the gap is a *chapter* verdict and
            # one page is a poor witness for it. Cheap: a float across a
            # queued connection, a handful of times per chapter.
            self.pageShapeKnown.emit(
                pixmap.height() / max(1.0, float(pixmap.width())))
            if not self._reported_width:
                # Back out the zoom this was decoded at, so what leaves
                # here is the scan's own width and not this session's.
                self._reported_width = True
                # The page's *shape* goes with its width - see
                # STRIP_ASPECT_MIN. Taken off the decoded pixmap, so it
                # is the scan's own proportions and not this session's
                # zoom (which cancels in the ratio anyway).
                self.pageWidthKnown.emit(
                    max(1, round(self._typical_width * 100.0
                                 / max(1, self._zoom))),
                    pixmap.height() / max(1.0, float(pixmap.width())))
        self._resize_slot(slot)

    def _on_page_ready(self, index):
        if 0 <= index < len(self._slots):
            pixmap = self._store.cached(index, self._zoom)
            if pixmap is not None:
                self._show(self._slots[index], pixmap, _screen_ratio(self))
        self._sync_visible()

    def _on_page_failed(self, index, message):
        if 0 <= index < len(self._slots):
            self._slots[index].setText(message)

    def paintEvent(self, event):
        """The strip paints its pages itself - the slots are data.

        **Why - measured 24 August 2026 on the owner's new PC (Windows
        11 26200, 2560x1440 @ 240Hz, DPR 1.25), per-refresh scroll steps
        sampled at the panel's own vblank (IDXGIOutput::WaitForVBlank):**

            slots moved by QScrollArea (any path)   58-72 steps/s, 70-76%
                                                    of refreshes moving
                                                    nothing at all
            the same motion with the body hidden    234/s, 2.3% dead
            poster_grid (paints its own cells)      236/s, 1.8% dead

        The wheel model was never the problem - its tick costs 0.26ms
        and _sync_visible 0.03ms - and neither blit-vs-repaint nor
        timer resolution nor foreground state moved the number: moving
        child widgets through Qt's scroll machinery costs ~14ms a frame
        on this machine, whatever else is true. Content one widget
        paints at an offset does not pay it. So the slots still hold
        the pixmaps, the geometry and the load state - every consumer
        of slot.y()/height() is untouched - but they live on a body
        parked far outside the viewport where they are never painted or
        moved (see scrollContentsBy), and this paints their pixmaps at
        the scroll offset, exactly as helpers/poster_grid draws its
        cells. QAbstractScrollArea routes the viewport's paint events
        here; the painter must open on the viewport, not on self."""
        viewport = self.viewport()
        painter = QPainter(viewport)
        painter.fillRect(viewport.rect(), QColor(theme.BG))
        top = self.verticalScrollBar().value()
        left = self.horizontalScrollBar().value()
        bottom = top + viewport.height()
        for slot in self._slots:
            slot_top = slot.y()
            slot_bottom = slot_top + slot.height()
            if slot_bottom <= top or slot_top >= bottom:
                continue
            x = slot.x() - left
            y = slot_top - top
            pixmap = slot.pixmap()
            if slot.loaded and pixmap is not None and not pixmap.isNull():
                painter.drawPixmap(x, y, pixmap)
                continue
            # A slot still waiting (or failed): the placeholder slab and
            # whatever message it carries, the same surface the QLabel
            # used to show.
            painter.fillRect(x, y, slot.width(), slot.height(),
                             QColor(theme.SURFACE))
            text = slot.text()
            if text:
                painter.setPen(QColor(theme.TEXT_MUTED))
                painter.drawText(
                    QRect(x + 16, y, max(1, slot.width() - 32), slot.height()),
                    int(Qt.AlignmentFlag.AlignCenter)
                    | int(Qt.TextFlag.TextWordWrap), text)
        painter.end()

    def _park_body(self):
        """Exile the body left of the viewport, where its slots are never
        painted or moved. Slot geometry stays valid - it is relative to
        the body - and paintEvent is what puts the artwork on screen."""
        widget = self.widget()
        if widget is not None and widget.x() != _BODY_PARK_X:
            widget.move(_BODY_PARK_X, 0)

    def scrollContentsBy(self, dx, dy):
        """**Deliberately does not call QScrollArea's implementation.**
        The base class repositions the body widget here, and moving a
        child through that machinery measures **~14ms a frame** on the
        owner's machine whatever the path under it (blit, full repaint,
        timer resolution, foreground state - all measured, none moved
        it; see _StripCanvas). The body stays parked; the canvas
        repaints at the new offset through the valueChanged hook."""
        self._park_body()
        self.viewport().update()

    def _on_scrolled(self, _value):
        # **Coalesced to one pass per event-loop turn, off the hot
        # path.** The momentum timer writes the scrollbar up to once per
        # refresh (144/s here), and this slot ran the full slot walk -
        # geometry for every slot, a store request per near one, a
        # set_keep - synchronously inside every single write, *before*
        # the viewport blit the setValue exists to cause. None of that
        # work changes within a frame, so at 144Hz it was pure overhead
        # between the wheel and the picture: the owner's "reader mode
        # still has the low fps issue". A queued singleShot runs once
        # after the burst of writes in the current turn, and the blit
        # itself stays synchronous - the strip still *moves* on every
        # write, it just budgets its bookkeeping per frame.
        if not self._sync_queued:
            self._sync_queued = True
            QTimer.singleShot(0, self._deferred_sync)
        self.positionChanged.emit()

    def _deferred_sync(self):
        # The flag drops only *after* the pass: _sync_visible can move
        # the scrollbar itself (slot-height compensation in
        # _resize_slot), and with the flag already cleared that write
        # re-queued another pass from inside this one - a 0-interval
        # timer loop pegging the UI thread. Writes that land during the
        # pass are absorbed by it; a genuinely later scroll queues the
        # next one normally.
        try:
            self._sync_visible()
        finally:
            self._sync_queued = False

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # Only the not-yet-decoded slots can change size on a resize now
        # that pages are never re-fitted to the window; _resize_slot is
        # a no-op for the rest.
        for slot in self._slots:
            self._resize_slot(slot)
        self._park_body()
        self._sync_visible()


# ---- the page ------------------------------------------------------------
class ReaderPage(GlassPage):
    """The reader itself: a chapter list, a reading surface, and the one
    place progress is written back to the entry.

    It covers the *whole* window, sidebar included - it is parented to
    the main window's central widget rather than to main.py's page
    container (see `open_reader`). That is the entire mechanism behind
    "no sidebar while reading", and it is the same one player.py already
    uses; main.py needs no hide/restore code and knows nothing about
    it."""

    closed = Signal()

    def __init__(self, entry, data_file="tracker.json", parent=None,
                 origin_page=None, resume=True, chapter_number=None):
        super().__init__(parent=parent)
        self.entry = dict(entry or {})
        self.data_file = data_file
        # Whether opening this entry jumps straight back to where reading
        # stopped, or lands on the chapter list. The card now has two
        # targets - the round continue button on the cover resumes, the
        # rest of the card browses - and this is which one was pressed.
        self.resume_on_open = bool(resume)
        # A specific chapter to open the moment the list arrives - the
        # details page's chapter rows pass this. It outranks resume: a
        # row named a chapter, and opening anything else is a wrong
        # answer, not a convenience.
        self._open_number = chapter_number
        # Which page of the app the reader was opened from, so the door
        # button puts him back there rather than on whatever happens to
        # be underneath. None when the caller couldn't say - see
        # `open_reader`, which is the only thing that fills this in.
        self.origin_page = origin_page
        self.chapters = []
        self.chapter_index = -1
        self.direction = "rtl"
        self._page_headers = {}
        self._run = 0
        # Every chapter opens at 100%; +/- scales from there. Not
        # remembered on the entry - the fixed baseline is the point, and
        # a zoom left at 250% from a fortnight ago is not.
        self._zoom = 1.0
        # ...and what 100% is worth for this medium (see
        # MEDIUM_BASE_SCALE). Read off the entry's own type, so a manga
        # and a manhwa opened one after the other each land where they
        # are meant to without either being touched.
        self._base_scale = MEDIUM_BASE_SCALE.get(
            str(self.entry.get("type") or "").strip().lower(),
            DEFAULT_BASE_SCALE)
        # Whether a page of *this* chapter has been seen to be a strip.
        # Reset per chapter in _show_chapter; see _on_page_shape.
        self._chapter_is_strip = False
        self._closed = False
        self._last_scroll = 0
        # The chapter controls are hover-only, so this is the one thing
        # that decides whether they are on screen - never the scroll.
        self._bottom_shown = False
        # Why the chapter controls are showing: True when the strip
        # reached the end rather than the pointer arriving.
        self._bottom_at_end = False
        self._pending_start_page = 0
        self._pending_resume = None
        self._refresh_toast = None
        self._bottom_widgets = None     # see resizeEvent's guard
        # How many times _place_title has bailed out on a bar whose
        # layout had not run yet. Bounded there, not state anything reads.
        self._title_retries = 0
        # Filled in the first time the download dialog opens, from the
        # folder last used anywhere in the app.
        self._download_folder = None

        self._signals = _ChapterSignals()
        self._signals.listed.connect(self._on_chapters_listed)
        self._signals.partial.connect(self._on_chapters_partial)
        self._signals.opened.connect(self._on_chapter_opened)

        self._store = _PageStore(self)

        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.timeout.connect(self._write_position)

        self._build()
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._load_chapters()

    # ---- construction -------------------------------------------------
    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self._stack_host = QWidget(objectName="Bare")
        self._stack = QVBoxLayout(self._stack_host)
        self._stack.setContentsMargins(0, 0, 0, 0)
        root.addWidget(self._stack_host, stretch=1)

        self._list_view = _ChapterListView()
        self._list_view.picked.connect(self.open_chapter)
        self._list_view.markRequested.connect(self._mark_chapter)
        self._list_view.markAllRequested.connect(self._mark_all_chapters)
        self._list_view.set_title(self.entry.get("title") or "")
        self._strip_view = _StripView(self._store)
        self._strip_view.positionChanged.connect(self._on_position_changed)
        self._strip_view.ranOff.connect(self._on_ran_off)
        self._strip_view.zoomRequested.connect(
            lambda step: self._change_zoom(ZOOM_STEP * step))
        self._strip_view.pageWidthKnown.connect(self._on_page_width)
        self._strip_view.pageShapeKnown.connect(self._on_page_shape)
        # The opening guess only: the shape overrules it in
        # _on_page_shape the moment a page decodes (see
        # MANGA_PAGE_GAP_PX). Nothing can know the shape before then, so
        # the type is what there is to guess with - and it is right for
        # every correctly typed entry, which is the whole reason to keep
        # it rather than open every chapter at one fixed gap and re-flow
        # half of them.
        if str(self.entry.get("type") or "").strip().lower() == "manga":
            self._strip_view.set_page_gap(MANGA_PAGE_GAP_PX)

        self._message = QWidget(objectName="Bare")
        message_column = QVBoxLayout(self._message)
        message_column.addStretch()
        # The series name over every message this page shows, and the
        # reason it is here rather than left to the top bar: "Loading
        # chapter..." on its own says nothing about *which* series is
        # loading, and the bar's own copy was being elided away to an
        # ellipsis (see _place_title, fixed in the same change).
        self._message_title = QLabel(self.entry.get("title") or "",
                                     objectName="PanelTitle")
        self._message_title.setWordWrap(True)
        self._message_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        # **Added to the layout before it is made visible, and that
        # order is the whole of a reported bug.** setVisible(True) on a
        # widget that has no parent yet does not "mark it visible for
        # later" - Qt shows it *now*, and a parentless widget is a
        # top-level window, so this put a framed 640x480 window titled
        # "python" on screen for the instant between here and the
        # addWidget below. Measured with a SetWinEventHook: one
        # Qt6111QWindowIcon with its own _q_titlebar, created and shown
        # every time a chapter was opened - the owner's "a pop-up small
        # window appears then closes in a moment". addWidget reparents
        # it, after which visibility means what it reads as.
        message_column.addWidget(self._message_title)
        self._message_title.setVisible(bool(self.entry.get("title")))
        self._message_label = QLabel("", objectName="PanelSubtitle")
        self._message_label.setWordWrap(True)
        self._message_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        message_column.addWidget(self._message_label)
        self._message_button = QPushButton("Open in Browser", objectName="Accent")
        use_hover_cursor(self._message_button)
        self._message_button.clicked.connect(self._open_in_browser)
        button_row = QHBoxLayout()
        button_row.addStretch()
        button_row.addWidget(self._message_button)
        button_row.addStretch()
        message_column.addLayout(button_row)
        message_column.addStretch()

        for widget in (self._list_view, self._strip_view, self._message):
            self._stack.addWidget(widget)
            widget.setVisible(False)

        # Built last and parented straight to the page, deliberately not
        # added to `root`: they float over the reading surface so that
        # hiding either one moves nothing underneath (see BAR_HEIGHT).
        self._bar = self._build_bar()
        self._bar.setParent(self)
        self._bar.setFixedHeight(BAR_HEIGHT)
        self._build_bottom()

        # Watched for three things: the pointer reaching the top of the
        # window (which brings the top bar back), the pointer reaching
        # the bottom (which is the *only* thing that brings the chapter
        # controls back), and the scroll direction.
        view = self._strip_view
        view.viewport().setMouseTracking(True)
        view.viewport().installEventFilter(self)
        view.verticalScrollBar().valueChanged.connect(self._on_scroll_value)

        # Before the first chapter, not after it: the strip is born at
        # 1.0 and rebuild() reads that value, so a manga would decode its
        # first screenful at the old baseline and re-decode it the
        # moment anything touched the zoom.
        self._apply_zoom()

        # Deliberately nothing shown yet: _load_chapters runs next in
        # __init__ and picks the surface - the cached list, or the
        # skeleton. Showing "Loading chapters..." here first meant every
        # warm open flashed a message that was wrong within one tick
        # (measured 22 August 2026: the message landed at 69ms and the
        # cached list replaced it at 139ms).

    def _build_bar(self):
        bar = QWidget(objectName="ReaderBar")
        # Scoped to the object name rather than set bare: a plain
        # `background:` on a parent propagates into every child, and the
        # buttons in here have their own QSS to keep.
        # Rounded along the edge that faces the artwork; the two corners
        # sitting on the window's own top edge stay square, so the bar
        # reads as a panel laid over the page rather than a notched
        # window frame. This one is an ordinary child widget, not a
        # native window like the player's bars, so the corners it does
        # not paint simply show the page behind - no mask needed.
        bar.setStyleSheet(
            f"#ReaderBar {{ background: {theme.PANEL_FILL}; "
            f"border-bottom: 1px solid {theme.BORDER}; "
            f"border-bottom-left-radius: {theme.RADIUS_LG}px; "
            f"border-bottom-right-radius: {theme.RADIUS_LG}px; }}")
        row = QHBoxLayout(bar)
        row.setContentsMargins(12, 6, 12, 6)
        row.setSpacing(8)

        # Far left: the way out, then which chapter this is. The player's
        # top bar opens the same way (arrow, then the episode list), so
        # leaving is in the same corner whichever one you are in. A large
        # left arrow now, not the SignOut door (the owner's ask - the
        # same glyph the player's prev/next carry), painted rather than
        # set as text so the clipping that read as a scratched icon
        # stays gone (see widgets.GlyphButton).
        self._leave_btn = GlyphButton(
            ICON_LEAVE, "Leave the reader (Esc)", size=(34, 30), font_pt=15)
        self._leave_btn.clicked.connect(self.leave)
        row.addWidget(self._leave_btn)

        # Beside the door, and the pair is deliberate: one leaves the
        # reader, the other goes up one level to the chapter list. The
        # jump list on the floor changes chapter without showing the
        # list, which is a different thing and not a way back to it.
        self._list_btn = self._glyph_button(
            ICON_CHAPTER_LIST, "Back to the chapter list")
        self._list_btn.clicked.connect(self.show_chapter_list)
        row.addWidget(self._list_btn)

        # The chapter's *own* number, not its position in the list.
        # "Chapter 4 of 41" was here and is gone: on a 507-chapter One
        # Piece listing the index says nothing, and the number is what
        # the rest of the app tracks by.
        self._chapter_label = QLabel("", objectName="SectionTitle")
        row.addWidget(self._chapter_label)
        row.addStretch()

        # The title is centred on the *window*, so it is not in `row` at
        # all: a label between two stretches sits in the middle of what
        # the two clusters leave over, and those clusters are different
        # widths, so it would land off centre by half their difference.
        # A free child positioned across the whole bar is the only way
        # the centre is the real centre - see _place_title, which also
        # elides it before it can reach either cluster.
        self._title_label = QLabel(self.entry.get("title") or "Reader",
                                   bar, objectName="SectionTitle")
        self._title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._title_label.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._title_label.lower()

        # Right cluster, and the order is read from the right edge
        # inwards: exit, refresh, then the size buttons. Full screen sits
        # outside those three because it is the one control here that
        # changes the window rather than the chapter.
        self._full_btn = self._glyph_button(ICON_FULLSCREEN, "Full screen (F)")
        self._full_btn.clicked.connect(self._toggle_fullscreen)
        row.addWidget(self._full_btn)

        # Sizing, scaling the page's own size up or down. There is no fit
        # menu beside these any more: 100% is always the original, and
        # these are the only thing that moves off it (0 resets).
        self._zoom_out_btn = self._button("−", "Smaller (−)")
        self._zoom_out_btn.clicked.connect(lambda: self._change_zoom(-ZOOM_STEP))
        self._zoom_label = QLabel("100%", objectName="Muted")
        self._zoom_label.setToolTip("Page size - 100% is the image's own size (0 resets)")
        self._zoom_in_btn = self._button("+", "Bigger (+)")
        self._zoom_in_btn.clicked.connect(lambda: self._change_zoom(ZOOM_STEP))
        # Squared to the glyph buttons' footprint: left at the app's
        # default padding these came out 89px wide against the 34px
        # icons beside them (measured), which made the right-hand
        # cluster read as two unrelated groups.
        #
        # ...and the padding itself has to go with the width, or the
        # button keeps the app-wide 8px 16px inside a 34px box and clips
        # its own "-"/"+" away to nothing - which is exactly what these
        # two were doing (measured: an empty frame, no glyph). Same trap
        # as _glyph_button below; the size is unchanged, only the inset.
        for button in (self._zoom_out_btn, self._zoom_in_btn):
            button.setFixedSize(34, 30)
            # Padding only. Everything else - fill, border, font size -
            # still comes from the app-wide rule, so these two look
            # exactly as they were meant to, minus the clipping.
            button.setStyleSheet("QPushButton { padding: 0px; }")
        row.addWidget(self._zoom_out_btn)
        row.addWidget(self._zoom_label)
        row.addWidget(self._zoom_in_btn)

        self._download_btn = self._glyph_button(
            ICON_DOWNLOAD, "Save chapters as .cbz files")
        self._download_btn.clicked.connect(self._open_download_dialog)
        row.addWidget(self._download_btn)

        self._refresh_btn = self._glyph_button(
            ICON_REFRESH, "Re-fetch this chapter from the site (R)")
        self._refresh_btn.clicked.connect(self.refresh_chapter)
        row.addWidget(self._refresh_btn)

        # Far right: the site this chapter came from. It sits at the
        # opposite edge from the door on purpose - the two controls that
        # take you *out* of the reader bracket the bar rather than
        # sitting next to each other where one gets pressed for the
        # other.
        self._browser_btn = self._glyph_button(
            ICON_BROWSER, "Open this chapter on its site")
        self._browser_btn.clicked.connect(self._open_in_browser)
        row.addWidget(self._browser_btn)
        return bar

    def _build_bottom(self):
        """The chapter controls: previous and next pinned hard to the
        window's two edges with the jump list centred between them,
        floating over the artwork.

        Three separate children rather than one full-width bar, and that
        is the point of them: a bar would cover the bottom 60px of every
        page and swallow the wheel and any click there even where it was
        showing nothing but background. Positioned by hand in
        _place_bottom - there is no layout here to join (see the top bar).

        Which edge is which used to follow the reading direction - next
        on the left, because the content is right-to-left. The owner
        asked for the two swapped, so it is now the keyboard's sense on
        screen too: **previous on the left, next on the right**, matching
        the plain Left/Right arrows in keyPressEvent. The chevrons flip
        with the buttons - each still points the way its press moves -
        and both carry the word as well, so the position never has to be
        interpreted. Ctrl+arrow keeps the older reading-direction sense
        and is therefore the one thing left that does not match the
        edges."""
        self._prev_btn = self._button("‹  Previous Chapter",
                                      "Previous chapter (Left, or Ctrl+Right)")
        self._prev_btn.clicked.connect(lambda: self.step_chapter(-1))
        self._next_btn = self._button("Next Chapter  ›",
                                      "Next chapter (Right, or Ctrl+Left)")
        self._next_btn.clicked.connect(lambda: self.step_chapter(1))

        self._chapter_box = _ChapterCombo(self)
        self._chapter_box.setToolTip("Jump to a chapter")
        # The popup is a separate view with its own width, and it does
        # *not* inherit the box's: chapter names are long ("Chapter 214 -
        # ...") and were being elided to nothing useful. Widened here,
        # and the font is set on the view as well because a QListView
        # paints its items with its own font() rather than the QSS
        # ::item font-family (.claude/rules/ui.md).
        popup = self._chapter_box.view()
        popup.setMinimumWidth(560)
        popup.setFont(self._chapter_box.font())
        popup.setTextElideMode(Qt.TextElideMode.ElideRight)
        use_hover_cursor(self._chapter_box)
        self._chapter_box.activated.connect(self.open_chapter)
        # Right-clicking a row of the open jump list marks it read or
        # unread - the same menu the list page's rows carry, so the mark
        # is reachable from inside a chapter too.
        self._chapter_box.markRequested.connect(self._mark_chapter)
        self._chapter_box.markAllRequested.connect(self._mark_all_chapters)

        # Listed left to right, the order they now sit in on the floor.
        self._bottom_widgets = (self._prev_btn, self._chapter_box,
                                self._next_btn)
        for widget in self._bottom_widgets:
            widget.setParent(self)
            widget.setVisible(False)
            # The pointer can leave a control without ever re-entering
            # the strip's viewport - off the side of the window, or up
            # into the top bar - and the viewport's own Leave is the
            # only other thing watching. Deferred a turn so the Enter
            # onto a neighbouring control lands first.
            widget.installEventFilter(self)
        # Solid rather than transparent: these sit on top of artwork that
        # is white as often as it is black, and a control that borrows
        # the page's colours is unreadable on half of them.
        edge = (f"QPushButton {{ background: {theme.PANEL_FILL};"
                f" color: {theme.TEXT}; border: 1px solid {theme.BORDER};"
                f" border-radius: {theme.RADIUS}px; font-weight: 600; }}"
                f"QPushButton:hover {{ background: {theme.SURFACE_HOVER};"
                f" border: 1px solid {theme.ACCENT}; }}"
                f"QPushButton:disabled {{ color: {theme.TEXT_DIM};"
                f" border: 1px solid {theme.BORDER}; }}")
        self._next_btn.setStyleSheet(edge)
        self._prev_btn.setStyleSheet(edge)

    def _button(self, text, tooltip):
        button = QPushButton(text)
        button.setToolTip(tooltip)
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        use_hover_cursor(button)
        return button

    def _glyph_button(self, glyph, tooltip):
        """A bar button carrying a Segoe Fluent glyph instead of a word.

        The font family has to be named on the button itself: the glyph
        is a private-use codepoint, and in the app's default family it
        is a blank box rather than an icon (theme.FONT_STACK_ICONS is
        the same stack the sidebar and the player use).

        `padding: 0px` is what makes the glyph appear at all, and it is
        not cosmetic. A widget stylesheet is merged with the application
        one property by property, so a rule that names no padding
        inherits the app-wide QPushButton's `8px 16px` - which on a 34px
        button leaves 34 - 32 = 2px of content width, and Qt renders the
        glyph as a 2px sliver. Measured off a grab of this bar: refresh
        painted 4 non-background pixels, the globe 22, where the +/-
        buttons beside them (also clipped, also at the app's padding)
        painted only their own frame. player.py already carries the same
        note on PlayPauseButton and _season_arrow, from the same cause."""
        button = self._button(glyph, tooltip)
        button.setFixedSize(34, 30)
        button.setStyleSheet(
            f"QPushButton {{ background: transparent; border: none;"
            f" color: {theme.TEXT}; font-family: {theme.FONT_STACK_ICONS};"
            f" padding: 0px;"
            f" font-size: 12pt; border-radius: {theme.RADIUS_SM}px; }}"
            f"QPushButton:hover {{ background: {theme.SURFACE_HOVER}; }}"
            f"QPushButton:pressed {{ background: {theme.SURFACE_ACTIVE}; }}"
            f"QPushButton:disabled {{ color: {theme.TEXT_DIM}; }}")
        return button

    def _in_chapter(self):
        return self.chapter_index >= 0 and self._strip_view.isVisible()

    def _show_only(self, widget):
        for child in (self._list_view, self._strip_view, self._message):
            child.setVisible(child is widget)
        reading = widget is self._strip_view
        # The reading surface runs edge to edge under the floating bars;
        # the list and the message keep the top bar's height clear so
        # their first row isn't hidden behind it.
        self._stack.setContentsMargins(
            *((0, 0, 0, 0) if reading else (16, BAR_HEIGHT + 10, 16, 12)))
        self._reveal_bar()
        # The chapter controls belong to a chapter. On the list and on an
        # error there is nothing for them to step through, and the list
        # has its own rows to click.
        if reading:
            self._place_bottom()
        else:
            self._hide_bottom()

    def _set_message(self, text, browser=True):
        self._message_label.setText(text)
        self._message_button.setVisible(browser)
        self._show_only(self._message)

    # ---- top bar visibility --------------------------------------------
    def _reveal_bar(self):
        self._bar.show()
        self._bar.raise_()

    def _hide_bar(self):
        # Only ever inside a chapter: the list and the error messages
        # have no scroll gesture behind them and would lose their only
        # way back out.
        if not self._in_chapter():
            return
        if self._chapter_box.view().isVisible():
            return          # the jump list is open over it - don't yank it
        self._bar.hide()

    def _on_scroll_value(self, value):
        """Hide on the way down, come back on the way up.

        Thresholded rather than sign-only, and `_last_scroll` is only
        moved when the threshold is crossed, so a slow drag accumulates
        into one decision instead of flickering the bar on the jitter of
        a trackpad."""
        # The end of the chapter is the one place the chapter controls
        # are wanted without being asked for: finishing a chapter *is*
        # the request to go to the next one. Checked before the movement
        # threshold below, because the last scroll into the end can be a
        # small one and would otherwise be swallowed.
        self._update_end_of_chapter()

        delta = value - self._last_scroll
        if abs(delta) < SCROLL_HIDE_DELTA:
            return
        self._last_scroll = value
        if delta > 0:
            self._hide_bar()
        else:
            self._reveal_bar()
        # The chapter controls are otherwise hover-only: scrolling up
        # mid-chapter to re-read a panel is not a request to jump
        # chapters, and having them appear over the artwork every time
        # the strip went backwards is what made them worth pinning to
        # the floor in the first place.

    def _update_end_of_chapter(self):
        """Show the chapter controls once the strip reaches its end, and
        take them away again on scrolling back up.

        `_bottom_at_end` records that *this* is why they are showing, so
        scrolling away hides them again without also yanking them out
        from under a pointer that is genuinely hovering there."""
        view = self._strip_view
        bar = view.verticalScrollBar() if view is not None else None
        if bar is None or bar.maximum() <= 0:
            return
        at_end = bar.value() >= bar.maximum() - END_OF_CHAPTER_PX
        if at_end and not self._bottom_shown:
            self._bottom_at_end = True
            self._reveal_bottom()
        elif not at_end and self._bottom_at_end:
            self._bottom_at_end = False
            self._hide_bottom()

    # ---- chapter controls (hover only) ---------------------------------
    def _place_bottom(self):
        """Put the three floating controls on the window's floor.

        Widths are fixed rather than taken from sizeHint so this can run
        before any of them has been shown - _show_only calls it on the
        way into a chapter, which is before the first paint."""
        y = self.height() - BOTTOM_HEIGHT + (
            BOTTOM_HEIGHT - BOTTOM_CONTROL_HEIGHT) // 2
        width = self.width()
        # x=0 and flush against the right edge, not inset: "pinned hard
        # to the far left and far right edges" is the ask, and an inset
        # of even 8px reads as floating rather than pinned.
        self._prev_btn.setGeometry(0, y, BOTTOM_STEP_WIDTH,
                                   BOTTOM_CONTROL_HEIGHT)
        self._next_btn.setGeometry(width - BOTTOM_STEP_WIDTH, y,
                                   BOTTOM_STEP_WIDTH, BOTTOM_CONTROL_HEIGHT)
        # Centred on the window, which is the same centre the artwork is
        # drawn on - and capped, so it stays a control rather than
        # growing into a bar as the window widens.
        gap = max(0, width - 2 * (BOTTOM_STEP_WIDTH + BOTTOM_GAP))
        box_width = max(200, min(CHAPTER_BOX_WIDTH, gap))
        self._chapter_box.setGeometry((width - box_width) // 2, y,
                                      box_width, BOTTOM_CONTROL_HEIGHT)
        # Positioning never reveals them - they are hover-only, so a
        # resize or a chapter opening leaves them exactly as hidden or
        # as shown as they already were.
        if self._bottom_shown:
            self._reveal_bottom()

    def _reveal_bottom(self):
        # Guarded rather than run on every mouse move through the strip:
        # show()+raise_() on three native-composited children, sixty
        # times a second, is the flicker the owner saw as the bar
        # "glitching" as much as the hide below was.
        if self._bottom_shown:
            return
        self._bottom_shown = True
        for widget in self._bottom_widgets:
            widget.show()
            widget.raise_()

    def _hide_bottom(self):
        if not self._bottom_shown:
            return
        if self._chapter_box.view().isVisible():
            return          # the jump list is open - don't yank it shut
        self._bottom_shown = False
        for widget in self._bottom_widgets:
            widget.hide()

    def _pointer_wants_bottom(self) -> bool:
        """Whether the pointer is on one of the three floating chapter
        controls, or still in the strip of window they live in.

        **This is the whole reason the bar looked broken.** The controls
        are children of this page raised over the strip, so the pointer
        arriving on one *leaves* the strip's viewport - and the Leave
        handler hid them. Reaching for the Next Chapter button therefore
        made it vanish under the cursor every time, which is what "the
        lower bar does not appear" was.

        Asked through QApplication.widgetAt, which hit-tests the widget
        tree rather than comparing coordinates, so no scale factor comes
        into it - mapToGlobal is wrong across two monitors at different
        scales and this has to be right on both (.claude/rules/ui.md)."""
        widgets = self._bottom_widgets or ()
        cursor = QCursor.pos()
        under = QApplication.widgetAt(cursor)
        while under is not None:
            if under in widgets:
                return True
            under = under.parentWidget()
        # ...and the band itself, so sliding off the end of one control
        # sideways - which the viewport never sees, because the pointer
        # was never in it - does not shut the row that is still being
        # used. Measured against the window's own geometry, which is
        # already global, never through mapFromGlobal - that maps through
        # the wrong screen's scale factor on this machine's mixed-DPI
        # monitors (.claude/rules/ui.md).
        frame = self.window().geometry()
        return (frame.contains(cursor)
                and cursor.y() >= frame.bottom() - BOTTOM_REVEAL_PX)

    def _hide_bottom_unless_aimed_at(self):
        # Wrapped because this is also reached from a zero-timer, and
        # the reader can be closed and deleted between the Leave and the
        # timer firing. An exception escaping a Qt slot is qFatal in
        # PyQt6 - an immediate abort, not a traceback (the same trap
        # widgets.release_hover_cursor carries).
        try:
            if self._closed or self._pointer_wants_bottom():
                return
            self._hide_bottom()
        except RuntimeError:
            pass

    # ---- chapter loading ----------------------------------------------
    def _load_chapters(self, refresh=False):
        if chapter_source is None:
            self._set_message(
                "The chapter source isn't available in this build, so there "
                "is nothing to read from here. The entry's own page still "
                "opens in your browser.")
            return
        self._run += 1
        if not refresh:
            # **A cache hit never crosses a thread.** It is a dictionary
            # lookup, and going through a worker for it cost 50-84ms of
            # signal latency - the UI thread was busy showing this very
            # reader, so the answer waited for it - and split what should
            # be one frame into two. Off a zero timer rather than called
            # straight from here, because this runs from __init__ and
            # _on_chapters_listed can open a chapter.
            cached = None
            try:
                cached = chapter_source.cached_chapters(self.entry)
            except Exception:
                cached = None
            if cached:
                run = self._run
                # The list surface goes up now, in this same frame -
                # empty for at most the one event-loop turn the deliver
                # below waits, rather than behind a "Loading
                # chapters..." message that a cache hit makes a lie.
                self._show_only(self._list_view)

                def deliver():
                    # The reader can be closed inside the one turn this
                    # waits, and touching a deleted QWidget raises
                    # RuntimeError inside a slot - which takes the
                    # process down (helpers/logs.py).
                    try:
                        self._on_chapters_listed(run, list(cached), "")
                    except RuntimeError:
                        pass

                QTimer.singleShot(0, deliver)
                return
            # Nothing cached: the skeleton, on screen inside this frame.
            # The fetch behind it takes 1-13s to its first partial page
            # and up to CHAPTER_LIST_TIMEOUT in full - a bare "Loading
            # chapters..." sitting through that is exactly the owner's
            # "the ch list takes a while to show" (22 August 2026).
            self._list_view.set_notice(
                "Fetching the chapter list from the source - the first "
                "look at a series can take a few seconds.")
            self._list_view.show_skeleton()
            self._show_only(self._list_view)
        # **submit_watched, not submit.** This is the one lookup the user
        # is sitting in front of - they pressed a title and are looking
        # at an empty reader - and the shared queue is drained by three
        # tracker pages' worth of page-load backfill (see lookup_pool).
        lookup_pool.submit_watched(_list_chapters_job, self._signals,
                                   self._run, self.entry, refresh)

    def refresh_chapter(self):
        """Re-fetch what is on screen from the site.

        Both halves, because "the images are broken" and "the site put up
        a fixed version" are the two reasons to press this and neither is
        fixed by the other: the chapter list is re-listed past
        chapter_source's cache, *and* the cached image files for the
        pages currently open are dropped so they are downloaded again
        rather than re-read from disk."""
        if chapter_source is None:
            return
        for url in self._store.urls():
            _drop_cached_page(url)
        if 0 <= self.chapter_index < len(self.chapters):
            self._pending_resume = {
                "chapter_id": self.chapters[self.chapter_index].get("id"),
                "index": self.chapter_index,
                "page": int(self._current_view().index()) if self._store.count() else 0,
            }
        else:
            self._pending_resume = None
        self._refresh_toast = show_toast(self, "Refreshing...", duration_ms=None)
        self._load_chapters(refresh=True)

    def _on_chapters_partial(self, run, chapters):
        """The source's first page, on screen while the rest is fetched.

        Deliberately does *less* than _on_chapters_listed: it fills the
        list and nothing else. No resume, no jump to a numbered chapter,
        no "Refreshed" verdict - all three are answers about the whole
        list, and giving them from a fortieth of it would open the wrong
        chapter or claim a refresh that is still running.

        Never touches a reader that has moved on: a chapter opened from
        this partial list must not be yanked back to the list when the
        next page lands."""
        if run != self._run or self._closed or not chapters:
            return
        if self._in_chapter() or self.chapters:
            return
        self._fill_chapter_list(list(chapters))
        self._show_only(self._list_view)
        self._sync_controls()

    def _fill_chapter_list(self, chapters):
        """The chapter list widgets, from `chapters`. Shared by the
        partial answer and the full one so both land identically."""
        self.chapters = chapters
        self._chapter_box.clear()
        for chapter in self.chapters:
            label = chapter_title(chapter)
            if is_arabic(chapter):
                label = f"[عربي] {label}"
            self._chapter_box.addItem(label)
        arabic = [c for c in self.chapters if is_arabic(c)]
        self._list_view.set_notice(
            "" if arabic else
            "No Arabic chapters were found for this title - the chapters "
            "below are what the source has in other languages.")
        self._list_view.set_chapters(self.chapters, self._reading_index(),
                                     self._last_read_number())

    def _on_chapters_listed(self, run, chapters, error):
        if run != self._run or self._closed:
            return
        if chapters is None:
            self._finish_refresh("Refresh Failed")
            self._set_message(
                f"The chapter list couldn't be loaded. {error}".strip())
            return
        if not chapters:
            self._finish_refresh("No Chapters Found")
            self._set_message(
                "No chapters were found for this title on any configured "
                "source - not in Arabic and not in any other language.")
            return
        self._fill_chapter_list(list(chapters))
        # Not while a chapter is open: the partial list above may have
        # been read from already, and the rest of the list arriving is
        # no reason to take the reader out of it.
        if not self._in_chapter():
            self._show_only(self._list_view)
        self._sync_controls()
        self._finish_refresh("Refreshed")
        if self._pending_resume is not None:
            self._reopen_pending()
            return
        # A chapter asked for by number (a details-page row) wins over
        # everything: that click named exactly one chapter.
        if self._open_number is not None:
            wanted, self._open_number = self._open_number, None
            index = self._row_for_number(wanted)
            if index >= 0:
                self.open_chapter(index)
                return
        # Only when the reader was opened *to* resume. Clicking the body
        # of a card asks for the list; jumping straight into a chapter
        # would be the one thing that click is not for.
        if self.resume_on_open:
            self._offer_resume()

    def _reading_index(self):
        """Which row of the chapter list is "the one being read".

        The open chapter while there is one - but the list is built
        *before* the first chapter opens (and a resume opens one only
        after), so on the way in there is no chapter_index yet and the
        list would land with nothing marked at all. The saved position
        answers it there, and the entry's last-read chapter number
        answers it for a title opened for the first time on this
        machine. -1 only when the entry has never been read."""
        if 0 <= self.chapter_index < len(self.chapters):
            return self.chapter_index
        saved = self.entry.get("reader_position")
        chapter_id = saved.get("chapter_id") if isinstance(saved, dict) else None
        if chapter_id is not None:
            for index, chapter in enumerate(self.chapters):
                if chapter.get("id") == chapter_id:
                    return index
        try:
            last = float(self.entry.get("last_watched_chapter") or 0)
        except (TypeError, ValueError):
            last = 0.0
        if last:
            for index, chapter in enumerate(self.chapters):
                if chapter_number(chapter) == last:
                    return index
        return -1

    def _finish_refresh(self, text):
        if self._refresh_toast is None:
            return
        finish_toast(self._refresh_toast, self, text)
        self._refresh_toast = None

    def _drop_refresh_toast(self):
        """Take the sticky "Refreshing..." off screen without a verdict.

        Leaving the chapter (or the reader) is when the owner stopped
        caring about the answer, and the toast otherwise sat there until
        the 45s listing finished or the sticky ceiling closed it - the
        reported "the refreshing alert was still there for a long time".
        The refresh itself keeps running; only its announcement goes."""
        toast, self._refresh_toast = self._refresh_toast, None
        if toast is None:
            return
        try:
            toast.close()
        except RuntimeError:
            pass        # already closed itself

    def _reopen_pending(self):
        """Go back to the chapter Refresh was pressed on.

        By id first: a re-list can have picked up newer chapters at the
        top, so the index it had is not the index it has. The index is
        only a fallback for a source that hands back no ids."""
        resume, self._pending_resume = self._pending_resume, None
        chapter_id = resume.get("chapter_id")
        page = int(resume.get("page") or 0)
        for index, chapter in enumerate(self.chapters):
            if chapter_id is not None and chapter.get("id") == chapter_id:
                self.open_chapter(index, start_page=page)
                return
        index = int(resume.get("index") or 0)
        if 0 <= index < len(self.chapters):
            self.open_chapter(index, start_page=page)

    def _offer_resume(self):
        """Reopen where reading stopped.

        Two ways of knowing where that is, and it used to accept only
        the first. **A saved page position exists only for a chapter
        read inside this reader**; a series whose progress came from the
        tracker - typed in, or marked read from the list - has a
        last-read chapter number and no `reader_position` at all, and
        for those the continue button landed on the chapter list, which
        is exactly what it is not for. Measured on the owner's own file:
        of thirteen entries, six carry a reader position and the rest
        carry only a number.

        So the position answers it when there is one, and the entry's
        own last-read chapter answers it when there is not
        (`_reading_index`, which matches the number exactly). Still
        silently ignored when neither resolves to a chapter that exists -
        a source can renumber or drop chapters between sessions, and
        opening the wrong one is worse than opening the list."""
        saved = self.entry.get("reader_position")
        saved = saved if isinstance(saved, dict) else {}
        chapter_id = saved.get("chapter_id")
        # The freshest of the two records wins. The position is written
        # by scrolling; the chapter number by *opening* a chapter and by
        # the list's mark-read menu - so a reader who moved on to chapter
        # 1190 can still be holding a page position inside 1189. Opening
        # the stale page then reads as "continue did nothing" (measured
        # on the owner's own One Piece entry: position at 1189, number at
        # 1190). When the number is ahead, the number is the truth.
        try:
            last_watched = float(self.entry.get("last_watched_chapter") or 0)
        except (TypeError, ValueError):
            last_watched = 0.0
        saved_index = -1
        if chapter_id is not None:
            for index, chapter in enumerate(self.chapters):
                if chapter.get("id") == chapter_id:
                    saved_index = index
                    break
        try:
            saved_number = float(saved.get("chapter_number") or 0)
        except (TypeError, ValueError):
            saved_number = 0.0
        if not saved_number and saved_index >= 0:
            # An older record without the number field - the chapter it
            # points at knows its own.
            saved_number = float(chapter_number(self.chapters[saved_index]) or 0)
        if saved_index >= 0 and saved_number >= last_watched:
            self.open_chapter(saved_index, start_page=int(saved.get("page") or 0))
            return
        # No stored position, a stale one, or one this source no longer
        # lists. The last-watched *number* answers instead. Looked up
        # directly rather than through _reading_index, which prefers the
        # saved position's chapter id - the very record that just lost
        # the freshness comparison above.
        if last_watched:
            index = self._row_for_number(last_watched)
            if index >= 0:
                self.open_chapter(index)
                return
        index = self._reading_index()
        if index >= 0:
            self.open_chapter(index)

    def open_chapter(self, index, start_page=0):
        if not (0 <= index < len(self.chapters)):
            return
        self.chapter_index = index
        self._pending_start_page = max(0, start_page)
        self._run += 1
        self._set_message("Loading chapter...", browser=True)
        self._sync_controls()
        # Opening is what records progress - see _mark_chapter_read. It
        # happens here rather than on reaching the last page because the
        # rest of the app now tracks reading by the last chapter opened,
        # and a chapter half-read is still the one being read.
        self._mark_chapter_read()
        lookup_pool.submit(_chapter_pages_job, self._signals, self._run,
                           self.chapters[index])

    def _on_chapter_opened(self, run, payload, error):
        if run != self._run or self._closed:
            return
        if payload is None:
            self._set_message(f"This chapter couldn't be opened. {error}".strip())
            return
        pages = list(payload.get("pages") or [])
        if not pages:
            self._set_message(
                "The source returned no pages for this chapter. It may have "
                "been removed, or the site changed its reader.")
            return
        self.direction = (payload.get("direction") or "rtl").lower()
        self._page_headers = dict(payload.get("headers") or {})
        self._store.configure(pages, self._page_headers)
        # Every chapter that actually opens is read history - stepping
        # from 12 to 13 inside the reader is as much "I read that" as
        # opening 13 from the list was.
        if 0 <= self.chapter_index < len(self.chapters):
            number = chapter_number(self.chapters[self.chapter_index])
            if number is not None:
                _mark_history(self.entry, [number], True)
                try:
                    history.touch(self.entry, progress=f"Ch {number:g}")
                except Exception:
                    pass
        self._show_chapter(start_page=self._pending_start_page)

    def _show_chapter(self, start_page=0):
        self._last_scroll = 0
        # The gap verdict belongs to one chapter and is re-earned by the
        # next one - see _on_page_shape. The gap itself is left where it
        # is until a page says otherwise, so a run of strip chapters
        # never re-flows.
        self._chapter_is_strip = False
        self._show_only(self._strip_view)
        self._strip_view.rebuild()
        self._strip_view.set_index(start_page)
        # Re-anchored *after* the jump to the resume page, not before it:
        # that jump is a scroll of hundreds of px and would otherwise
        # read as "scrolling down", hiding the bar the instant a chapter
        # opened.
        self._last_scroll = self._strip_view.verticalScrollBar().value()
        self._reveal_bar()
        self._sync_controls()

    # ---- navigation ----------------------------------------------------
    def step_chapter(self, step):
        """Move a chapter in *reading* order: +1 is the next chapter to
        read, -1 the one before it.

        **The sign is inverted against the index on purpose.**
        chapter_source.list_chapters returns chapters newest first
        (`_sort_key`, `reverse=True`), so the next chapter to read is at
        index **- 1**, not index + 1. This shipped backwards - Previous
        opened the chapter after the one being read and Next opened the
        one before it, and running off the bottom of a strip went
        backwards too, because every caller passed a raw index delta.
        The inversion happens here, once, so the two buttons, Ctrl+arrow
        and ranOff can all talk in reading order.

        Language is the second half of it: chapters arrive Arabic-first
        rather than in one flat order, so the adjacent index can be the
        English fallback of the chapter just read. Same-language first,
        and only then whatever is adjacent."""
        if not self.chapters:
            return
        if self.chapter_index < 0:
            self.open_chapter(0)
            return
        delta = -1 if step > 0 else 1
        current_lang = str(self.chapters[self.chapter_index].get("lang") or "").lower()
        candidates = range(self.chapter_index + delta,
                           len(self.chapters) if delta > 0 else -1,
                           delta)
        for index in candidates:
            if str(self.chapters[index].get("lang") or "").lower() == current_lang:
                self.open_chapter(index)
                return
        nearby = self.chapter_index + delta
        if 0 <= nearby < len(self.chapters):
            self.open_chapter(nearby)
            return
        show_toast(self, "No More Chapters" if step > 0 else "This Is the First Chapter")

    def _on_ran_off(self, direction):
        self.step_chapter(1 if direction > 0 else -1)

    def _current_view(self):
        # One reading surface. Kept as a method because the persistence
        # and refresh paths ask it for the page index, and a second
        # surface is exactly what this reader is not getting again.
        return self._strip_view

    def _on_position_changed(self):
        self._sync_controls()
        self._save_timer.start(SAVE_POSITION_MS)

    # ---- view controls -------------------------------------------------
    def _sync_controls(self):
        total = len(self.chapters)
        reading = 0 <= self.chapter_index < total
        if reading:
            number = chapter_number(self.chapters[self.chapter_index])
            self._chapter_label.setText(
                f"Chapter {format_chapter_progress(number)}"
                if number is not None else "Chapter")
            self._chapter_box.blockSignals(True)
            self._chapter_box.setCurrentIndex(self.chapter_index)
            self._chapter_box.blockSignals(False)
        else:
            self._chapter_label.setText(f"{total} chapters" if total else "")
        # Marked on the box itself as well as selected - see
        # _ChapterCombo.set_current_chapter for why selection alone is
        # not visible after scrolling a 500-row popup.
        self._chapter_box.set_current_chapter(self.chapter_index if reading else -1)
        # Newest first, so the *next* chapter is the lower index: at
        # index 0 there is nothing further to read, at the last index
        # nothing earlier.
        self._next_btn.setEnabled(reading and self.chapter_index > 0)
        self._prev_btn.setEnabled(reading and self.chapter_index < total - 1)
        # Off while the list is what is already on screen - a button that
        # goes where you are reads as broken, not as a no-op.
        self._list_btn.setEnabled(bool(self.chapters)
                                  and not self._list_view.isVisible())
        self._refresh_btn.setEnabled(chapter_source is not None
                                     and self._refresh_toast is None)
        self._zoom_label.setText(f"{round(self._zoom * 100)}%")
        self._place_title()

    def _place_title(self):
        """The entry's name, centred on the window and elided before it
        can reach either cluster of buttons.

        The reserve is twice the wider cluster because the label spans
        the whole bar: taking only the wider side off one end would move
        the text's centre off the window's.

        The series name went missing from this bar for **two** reasons,
        and each on its own was enough to reduce every title to the 40px
        floor - which is what the owner saw as "Th..." beside "Chapter
        247". Both are fixed here.

        *The two edges were measured off the wrong buttons.* The door is
        the leftmost control and the globe the rightmost, so
        `_browser_btn.right()` was ~width-12 and `width -
        _leave_btn.left()` ~width-12 as well; the reserve came out at
        about 2 x width. The left cluster actually ends at the chapter
        label and the right one starts at the full-screen button, so
        those are the two clearances.

        *And they were measured before the bar's layout had run.* This is
        reached from _sync_controls, which can fire while every child of
        the bar is still at the default (0, 0) - `_full_btn.left()` is
        then 0, the right clearance is the whole bar width, and the
        reserve is twice it again. So the layout is activated first
        (synchronous, and a no-op once it is current), and if the
        clusters still read as unpositioned after that the label is left
        exactly as it is and re-placed on the next turn of the event
        loop, rather than having an ellipsis written over it against a
        measurement that means nothing."""
        label = getattr(self, "_title_label", None)
        if label is None:
            return
        width = self._bar.width()
        if width <= 0:
            return
        layout = self._bar.layout()
        if layout is not None:
            layout.activate()
        label.setGeometry(0, 0, width, BAR_HEIGHT)
        left = self._chapter_label.geometry().right()
        right_start = self._full_btn.geometry().left()
        if left <= 0 or right_start <= 0:
            # Bounded, because a retry that can never succeed would spin
            # a zero-timer for the life of the page.
            if self._title_retries < 5:
                self._title_retries += 1
                QTimer.singleShot(0, self._place_title)
            return
        self._title_retries = 0
        reserve = 2 * max(left, width - right_start, 0) + 24
        text = self.entry.get("title") or "Reader"
        metrics = QFontMetrics(label.font())
        label.setText(metrics.elidedText(text, Qt.TextElideMode.ElideRight,
                                         max(40, width - reserve)))
        label.setToolTip(text)

    def _change_zoom(self, delta):
        zoom = max(ZOOM_MIN, min(ZOOM_MAX, self._zoom + delta))
        if abs(zoom - self._zoom) < 1e-6:
            return
        self._zoom = zoom
        self._apply_zoom()

    def _on_page_width(self, source_width, aspect=0.0):
        """Set 100% to the medium's target width, now that this chapter
        has said how wide its pages actually are.

        This is the "auto detect the correct size whenever I read" half
        of the owner's ask: the multiplier is *derived* per series rather
        than fixed per medium, so a 1644px One Piece scan and a 1325px
        Kingdom scan both open at MEDIUM_TARGET_WIDTH instead of 24%
        apart. Once per chapter, and unchanged across the rest of a
        session unless the next series is cut differently.

        Falls back to the plain multiplier when the number is not
        credible - see TARGET_SCALE_MIN/MAX. A thumbnail standing in for
        a page must not be blown up to fill the column.

        The gap is decided off the same threshold but not here - see
        _on_page_shape, which gets to hear from every page rather than
        only from this one."""
        medium = str(self.entry.get("type") or "").strip().lower()
        target = MEDIUM_TARGET_WIDTH.get(medium)
        # A page shaped like a strip is one, whatever the entry says.
        # See STRIP_ASPECT_MIN for why the declared type cannot be
        # trusted here.
        if aspect >= STRIP_ASPECT_MIN:
            target = STRIP_TARGET_WIDTH
        elif target and source_width > target:
            # **A paged scan opens at its own resolution, up to the
            # window - the owner, 24 August 2026: "why does the ch
            # appear in less resolution than the original in the
            # websites like one piece!!".** The fixed 1100 target was
            # his earlier ask (one size across every manga series), and
            # it quietly *downscaled* every scan cut wider than 1100 -
            # One Piece's 1644px pages drew at two-thirds of the pixels
            # the site serves. Native-up-to-the-window is what the
            # site he compares against does; the target remains the
            # floor for scans narrower than it (nothing is blown up),
            # and the strips keep their own width - their sources are
            # 700-900px and 762 already is native for them.
            viewport = max(target, self._strip_view.viewport().width() - 24
                           if self._strip_view is not None else target)
            target = min(source_width, viewport)
        if not target or source_width <= 0:
            return
        scale = target / float(source_width)
        if not (TARGET_SCALE_MIN <= scale <= TARGET_SCALE_MAX):
            return
        if abs(scale - self._base_scale) < 0.01:
            return
        self._base_scale = scale
        self._apply_zoom()

    def _on_page_shape(self, aspect):
        """Whether this chapter is a strip, and so whether it gets a gap.

        **A latch, and it only ever latches one way - see
        MANGA_PAGE_GAP_PX.** The gap used to be decided by the first page
        that happened to decode, which is not the first page of the
        chapter: three pages are requested at once and the *smallest*
        finishes first (measured on Swordmaster's Youngest Son ch.207 -
        page 2, 800x14925, reported before page 1's 800x18835). A chapter
        that opens on a short notice panel or a credits slice would
        therefore have had manga's 6px cut across every join in it, with
        nothing left to correct the verdict afterwards.

        So: one strip-shaped page anywhere in the chapter closes the gap
        for the whole of it, permanently. The asymmetry is deliberate.
        Being wrong towards 0 costs two printed pages sitting flush;
        being wrong towards 6 draws a line through artwork that was cut
        mid-panel, which is the bug being reported."""
        if self._chapter_is_strip or aspect <= 0:
            return
        if aspect >= STRIP_ASPECT_MIN:
            self._chapter_is_strip = True
            self._strip_view.set_page_gap(0)
        else:
            self._strip_view.set_page_gap(MANGA_PAGE_GAP_PX)

    def _apply_zoom(self):
        # The strip is told the *decode* scale - the label and the
        # controls stay in the user's 100%, the medium's baseline is
        # folded in here, once.
        self._strip_view.set_zoom(zoom_key(self._zoom * self._base_scale))
        self._sync_controls()

    def _toggle_fullscreen(self):
        window = self.window()
        toggle = getattr(window, "toggle_fullscreen", None)
        if callable(toggle):
            toggle()
        # Asked *after* the toggle, and off the window rather than
        # remembered here: F and the title bar can both change this, so
        # a flag of our own goes stale the first time either is used.
        self._sync_fullscreen_glyph()

    def _sync_fullscreen_glyph(self):
        full = bool(self.window().isFullScreen())
        self._full_btn.setText(ICON_EXIT_FULLSCREEN if full else ICON_FULLSCREEN)
        self._full_btn.setToolTip("Leave full screen (F)" if full
                                  else "Full screen (F)")

    def _open_in_browser(self):
        url = ""
        if 0 <= self.chapter_index < len(self.chapters):
            url = self.chapters[self.chapter_index].get("url") or ""
        url = url or self.entry.get("url") or ""
        if not url:
            show_toast(self, "This Entry Has No Page to Open")
            return
        webbrowser.open(url)

    # ---- downloading ---------------------------------------------------
    def _download_candidates(self):
        """The chapters a range may be taken from: everything in one
        language, newest first.

        One language, not the whole list, and this is not tidiness. The
        list arrives grouped - Arabic first, then whatever other
        languages the source has - so an index span across the join holds
        the same chapter twice. Both copies save as
        `<title> - <number>.cbz` (helpers.downloads._run_chapter), so the
        second silently overwrites the first and a "42 chapters" download
        leaves 30 files. The language of the chapter being read decides
        it; on the chapter list, the first chapter's does."""
        if not self.chapters:
            return []
        anchor = (self.chapter_index
                  if 0 <= self.chapter_index < len(self.chapters) else 0)
        wanted = str(self.chapters[anchor].get("lang") or "").lower()
        return [index for index, chapter in enumerate(self.chapters)
                if str(chapter.get("lang") or "").lower() == wanted]

    def _open_download_dialog(self):
        """This chapter, or a range of them, saved as .cbz.

        A dialog rather than the player's overlay panel: this page is an
        ordinary widget in the main window, and every other "fill this in
        and confirm" in the app (adding a game, editing an entry) is a
        QDialog. The reader's own bars are hover-revealed and float over
        the artwork, which is the wrong place to put a form."""
        if not self.chapters:
            show_toast(self, "No Chapters to Download")
            return
        from windows import downloads_page
        if self._download_folder is None:
            self._download_folder = downloads_page.saved_folder()

        candidates = self._download_candidates()
        reading = self.chapter_index if self.chapter_index in candidates else -1
        current = candidates.index(reading) if reading >= 0 else 0

        dialog = QDialog(self)
        dialog.setMinimumWidth(560)
        column = QVBoxLayout(dialog)
        column.setContentsMargins(20, 18, 20, 18)
        column.setSpacing(12)
        # Called with the layout already in place so the heading lands at
        # its top; the rows below then append after it.
        frameless_dialog(dialog, title="Download Chapters")

        scope = QComboBox()
        use_hover_cursor(scope)
        scope.addItems(["This Chapter", "A Range of Chapters"])
        use_hover_cursor(scope)
        scope_row = QHBoxLayout()
        scope_row.addWidget(QLabel("Download:"))
        scope_row.addWidget(scope, stretch=1)
        column.addLayout(scope_row)

        labels = [chapter_title(self.chapters[index]) for index in candidates]
        first_box, last_box = QComboBox(), QComboBox()
        use_hover_cursor(first_box)
        use_hover_cursor(last_box)
        for box in (first_box, last_box):
            box.addItems(labels)
            box.setCurrentIndex(current)
            use_hover_cursor(box)
            # A QComboBox popup paints its items with the view's own
            # font, not the QSS ::item family (.claude/rules/ui.md), and
            # chapter names are long enough to need the width.
            box.view().setMinimumWidth(460)
            box.view().setFont(box.font())
        range_row = QHBoxLayout()
        range_row.addWidget(QLabel("From:"))
        range_row.addWidget(first_box, stretch=1)
        range_row.addWidget(QLabel("To:"))
        range_row.addWidget(last_box, stretch=1)
        column.addLayout(range_row)

        count_label = QLabel("", objectName="Muted")
        count_label.setWordWrap(True)
        column.addWidget(count_label)

        folder_row = QHBoxLayout()
        folder_row.addWidget(QLabel("Saving to:", objectName="Muted"))
        folder_label = QLabel(self._download_folder)
        folder_label.setToolTip(self._download_folder)
        folder_row.addWidget(folder_label, stretch=1)
        change_btn = QPushButton("Change...")
        use_hover_cursor(change_btn)
        folder_row.addWidget(change_btn)
        column.addLayout(folder_row)

        buttons = QHBoxLayout()
        buttons.addStretch()
        cancel_btn = QPushButton("Cancel")
        use_hover_cursor(cancel_btn)
        cancel_btn.clicked.connect(dialog.reject)
        buttons.addWidget(cancel_btn)
        start_btn = QPushButton("Download", objectName="Accent")
        use_hover_cursor(start_btn)
        buttons.addWidget(start_btn)
        column.addLayout(buttons)

        def picked_indexes():
            if scope.currentIndex() == 0:
                return [candidates[first_box.currentIndex()]]
            low = min(first_box.currentIndex(), last_box.currentIndex())
            high = max(first_box.currentIndex(), last_box.currentIndex())
            # Reversed: the list runs newest first, and a range should
            # arrive on disk oldest first so the queue reads in order.
            return list(reversed(candidates[low:high + 1]))

        def sync(*_args):
            ranged = scope.currentIndex() == 1
            last_box.setEnabled(ranged)
            count = len(picked_indexes())
            count_label.setText(
                f"{count} chapter{'s' if count != 1 else ''} will be saved as "
                f".cbz file{'s' if count != 1 else ''}.")
            start_btn.setText("Download" if count == 1 else f"Download {count}")

        scope.currentIndexChanged.connect(sync)
        first_box.currentIndexChanged.connect(sync)
        last_box.currentIndexChanged.connect(sync)

        def change_folder():
            picked = downloads_page.choose_folder(dialog, self._download_folder)
            if not picked:
                return
            self._download_folder = picked
            folder_label.setText(picked)
            folder_label.setToolTip(picked)

        change_btn.clicked.connect(change_folder)
        start_btn.clicked.connect(
            lambda: self._start_chapter_download(dialog, picked_indexes()))
        sync()
        dialog.exec()

    def _start_chapter_download(self, dialog, indexes):
        chapters = [self.chapters[index] for index in indexes
                    if 0 <= index < len(self.chapters)]
        if not chapters:
            show_toast(self, "Nothing to Download")
            return
        # Asked once, with the count, and only for a range: this list can
        # run to several hundred chapters, and queueing all of One Piece
        # must not be a single click on the wrong row.
        if len(chapters) > 1 and not confirm(
                dialog, "Download Chapters",
                f"Queue {len(chapters)} chapters for download?"):
            return
        try:
            downloads.queue_chapters(self.entry, chapters,
                                     folder=self._download_folder)
        except Exception:
            logs.exception("Could not queue a chapter download")
            show_toast(self, "Could Not Queue Those Chapters")
            return
        dialog.accept()
        show_toast(self, "Queued for Download" if len(chapters) == 1
                   else f"Queued {len(chapters)} Chapters")

    # ---- back to the list, and marking a chapter ------------------------
    def show_chapter_list(self):
        """Up one level: the chapter list, without leaving the reader.

        The jump list on the floor is not this. That one changes chapter
        without ever showing the list, which is a different thing - it
        answers "take me to 214", not "let me look at what there is". The
        button sits beside the door because the two are the pair of ways
        *out* of a chapter, one of them a level and one of them all the
        way.

        The position is written here rather than left to the debounce
        timer: leaving a chapter is exactly when a reader stops
        scrolling, and losing the last page to a 1.2s timer that had not
        fired yet is only ever noticed a session later. chapter_index is
        deliberately kept, so the list still marks the chapter that was
        open and Next/Previous still answer for it."""
        if not self.chapters:
            return
        self._save_timer.stop()
        self._write_position()
        self._drop_refresh_toast()
        self._list_view.set_chapters(self.chapters, self._reading_index(),
                                     self._last_read_number())
        self._show_only(self._list_view)
        self._sync_controls()

    # ---- back / forward -------------------------------------------------
    # The two extra buttons on the side of the mouse, which every browser
    # and file manager on this machine already treats as "up one level"
    # and "back into where I was". Wired to the same two steps the reader
    # already has - the chapter list is one level above a chapter, and
    # the reader itself is one above the list - rather than to
    # previous/next chapter: a fourth-button click that silently changed
    # chapter would be a very expensive way to be wrong, and Ctrl+arrow
    # and the floor buttons already do that.
    def go_back(self):
        if self._in_chapter():
            self.show_chapter_list()
            return
        self.leave()

    def go_forward(self):
        """Back into the chapter the list was opened from.

        Only when its pages are still loaded - chapter_index survives
        going up to the list (see show_chapter_list), so this costs
        nothing and needs no refetch. With no chapter behind it there is
        nothing forward of here, and it does nothing rather than opening
        something arbitrary."""
        if self._in_chapter() or not self._store.count():
            return
        if not (0 <= self.chapter_index < len(self.chapters)):
            return
        self._show_only(self._strip_view)
        self._sync_controls()

    def _handle_side_button(self, button) -> bool:
        """True when `button` was one of the mouse's side buttons and has
        been acted on. Shared by the page's own press handler and the
        filter over the strip's viewport - a press inside the viewport
        never reaches the page."""
        if button == Qt.MouseButton.BackButton:
            self.go_back()
            return True
        if button == Qt.MouseButton.ForwardButton:
            self.go_forward()
            return True
        return False

    def mousePressEvent(self, event):
        # Reached by anything that ignored the press on its way up -
        # the chapter list's rows and its scroll area all do, since
        # Card only ever handles the left and right buttons.
        if self._handle_side_button(event.button()):
            event.accept()
            return
        super().mousePressEvent(event)

    def wheelEvent(self, event):
        # The backstop for the window's absolute right edge: a wheel that
        # lands on the page itself (past the scrollbar's own pixels, or
        # on a floating control that ignored it) still scrolls the strip
        # rather than dying. The strip's own handler also owns Ctrl+wheel
        # zoom, so forwarding gets both behaviours for free.
        if self._in_chapter():
            self._strip_view.wheelEvent(event)
            event.accept()
            return
        super().wheelEvent(event)

    def _row_for_number(self, number):
        for index, chapter in enumerate(self.chapters):
            if chapter_number(chapter) == number:
                return index
        return -1

    def _mark_chapter(self, index, finished):
        """Mark chapter `index` read, or not read.

        `correct_progress` and not `record_progress`: this is the one
        writer allowed to move the stored number *down*, which is exactly
        what "unfinished" is, and record_progress would refuse it in
        silence (tracker._write_progress is forward-only by design).

        "Finished" is the chapter's own number. "Unfinished" is the
        highest number *this source actually lists* below it, not
        `number - 1`: scanlators split chapters, so the one before 25 can
        be 24.5 - and 0 when there is nothing earlier, which reads as
        nothing read."""
        if not (0 <= index < len(self.chapters)):
            return
        number = chapter_number(self.chapters[index])
        if number is None:
            show_toast(self, "This Chapter Has No Number to Record")
            return
        if finished:
            target = number
        else:
            earlier = [n for n in (chapter_number(c) for c in self.chapters)
                       if n is not None and n < number]
            target = max(earlier) if earlier else 0.0
        # The History tick, explicitly and in both directions: the write
        # that rides along inside correct_progress only ever *adds* one,
        # for the chapter it is given - so unmarking 25 (which stores
        # 24.5) would leave 25's own tick standing and the details page
        # would go on calling it read.
        _mark_history(self.entry, [number], finished)
        # An unsaved title has no entry to write a number onto, and
        # correct_progress says so by returning False - but the mark was
        # still recorded, in History. Only a *saved* title failing here
        # is a real failure worth a toast.
        if not correct_progress(self.entry, chapter=target) and self.entry.get("id"):
            show_toast(self, "Could Not Save That Chapter")
            return
        # Rebuilt rather than left alone: the list marks where reading has
        # reached, and the row it was marking is not the answer any more.
        # While a chapter is open that mark belongs to the open one; on
        # the list it belongs to whatever was just marked.
        marked = (self.chapter_index
                  if 0 <= self.chapter_index < len(self.chapters)
                  else self._row_for_number(target))
        self._list_view.set_chapters(self.chapters, marked,
                                     self._last_read_number())
        self._sync_controls()
        show_toast(self, f"Read Up to Chapter {format_chapter_progress(target)}"
                   if target else "Marked as Not Started")

    def _last_read_number(self) -> float:
        try:
            return float(self.entry.get("last_watched_chapter") or 0.0)
        except (TypeError, ValueError):
            return 0.0

    def _mark_all_chapters(self, finished):
        """Every chapter read, or none of them - the menu's "mark all".

        "All read" is the highest numbered chapter this source lists,
        which is exactly what "I have read all of this" means to the
        tracker's read-up-to model; "all unread" is zero. Both through
        correct_progress, the one writer allowed to move the number in
        either direction."""
        numbers = [n for n in (chapter_number(c) for c in self.chapters)
                   if n is not None]
        if not numbers:
            show_toast(self, "These Chapters Carry No Numbers to Record")
            return
        target = max(numbers) if finished else 0.0
        # Every chapter's tick, not just the highest - see _mark_chapter.
        _mark_history(self.entry, numbers, finished)
        # See _mark_chapter: an unsaved title records into History only,
        # and that is not a failure.
        if not correct_progress(self.entry, chapter=target) and self.entry.get("id"):
            show_toast(self, "Could Not Save That")
            return
        marked = (self.chapter_index
                  if 0 <= self.chapter_index < len(self.chapters) else -1)
        self._list_view.set_chapters(self.chapters, marked,
                                     self._last_read_number())
        self._sync_controls()
        show_toast(self, f"Marked All Read Up to Chapter "
                         f"{format_chapter_progress(target)}"
                   if finished else "Marked All as Unread")

    # ---- persistence ---------------------------------------------------
    def _write_position(self):
        if not self.chapters or self.chapter_index < 0 or not self._store.count():
            return
        chapter = self.chapters[self.chapter_index]
        self._update_entry({"reader_position": {
            "chapter_id": chapter.get("id"),
            "chapter_number": chapter.get("number"),
            "page": int(self._current_view().index()),
            "saved_at": storage.now_iso(),
        }})

    def _mark_chapter_read(self):
        """Raise the entry's own last-read chapter to the one just
        opened.

        The tracker's existing `last_watched_chapter` (a float - see
        tracker.parse_chapter_progress), never a second field of this
        page's own: the card, Home and the schedule all read that one.
        Only ever raised - reopening chapter 3 of a series you are 40
        chapters into must not throw that away.

        Silent: this page has no manual progress control any more, and a
        toast on every chapter opened is noise, not news."""
        if not (0 <= self.chapter_index < len(self.chapters)):
            return
        number = chapter_number(self.chapters[self.chapter_index])
        if number is None:
            return
        current = self.entry.get("last_watched_chapter") or 0.0
        if number <= current:
            return
        self.entry["last_watched_chapter"] = number
        self._update_entry({"last_watched_chapter": number,
                            "updated_at": storage.now_iso()})

    def _update_entry(self, fields) -> bool:
        """One entry, never a whole list back from this page - several
        pages hold their own copy of tracker.json and a whole-list save
        restores a snapshot minutes stale (.claude/rules/ui.md, and the
        reordering defect it comes from)."""
        entry_id = self.entry.get("id")
        if not entry_id:
            return False
        self.entry.update(fields)
        try:
            return storage.update_entry(self.data_file, entry_id, fields)
        except Exception:
            logs.exception("reader could not save progress")
            return False

    # ---- input ----------------------------------------------------------
    def keyPressEvent(self, event):
        key = event.key()
        control = event.modifiers() & Qt.KeyboardModifier.ControlModifier
        if key == Qt.Key.Key_Escape:
            self.leave()
            return
        if key == Qt.Key.Key_F and not control:
            self._toggle_fullscreen()
            return
        if key == Qt.Key.Key_R and not control:
            self.refresh_chapter()
            return
        if key in (Qt.Key.Key_Plus, Qt.Key.Key_Equal):
            self._change_zoom(ZOOM_STEP)
            return
        if key == Qt.Key.Key_Minus:
            self._change_zoom(-ZOOM_STEP)
            return
        if key == Qt.Key.Key_0:
            self._zoom = 1.0
            self._apply_zoom()
            return
        if self.chapter_index < 0 or not self._store.count():
            super().keyPressEvent(event)
            return
        view = self._strip_view
        if key in (Qt.Key.Key_Left, Qt.Key.Key_Right):
            if control:
                # Ctrl keeps the sense it has always had, which follows
                # the reading direction: right-to-left content, so
                # Ctrl+Left is the *next* chapter - the side the Next
                # button sits on.
                self.step_chapter(1 if key == Qt.Key.Key_Left else -1)
            else:
                # Plain arrows step *chapters* now, not pages (the
                # owner's ask), and by keyboard habit: Right is next,
                # Left is previous. That is deliberately the opposite
                # of the on-screen sides - Next sits bottom-*left*
                # because the content reads right-to-left - so don't
                # "fix" this back to match the buttons. Page stepping
                # still lives on Space/PageDown and Backspace/PageUp.
                self.step_chapter(1 if key == Qt.Key.Key_Right else -1)
            return
        if key in (Qt.Key.Key_Space, Qt.Key.Key_PageDown):
            view.step(True)
            return
        if key in (Qt.Key.Key_Backspace, Qt.Key.Key_PageUp):
            view.step(False)
            return
        if key in (Qt.Key.Key_Down, Qt.Key.Key_Up):
            down = key == Qt.Key.Key_Down
            view.scroll_step(ARROW_SCROLL_PX if down else -ARROW_SCROLL_PX)
            return
        if key == Qt.Key.Key_Home:
            view.set_index(0)
            return
        if key == Qt.Key.Key_End:
            view.set_index(self._store.count() - 1)
            return
        super().keyPressEvent(event)

    # ---- lifetime --------------------------------------------------------
    def follow(self, host):
        """Track `host`'s size, so the reader stays exactly over what it
        covers - which is the whole central widget, sidebar included.

        An event filter rather than a layout: the pages under it are
        positioned by hand and slide over each other (main._show_page),
        so there is no layout here to join - and this keeps the hook in
        main.py to nothing at all."""
        self._host = host
        host.installEventFilter(self)
        self.setGeometry(host.rect())

    def eventFilter(self, obj, event):
        if obj is getattr(self, "_host", None) and event.type() == QEvent.Type.Resize:
            self.setGeometry(obj.rect())
            return super().eventFilter(obj, event)
        if obj is self._strip_view.viewport():
            if event.type() == QEvent.Type.MouseButtonPress:
                # Swallowed here rather than left to propagate: a press
                # inside a QAbstractScrollArea's viewport does not reach
                # this page, so mousePressEvent above never sees the one
                # gesture the reading surface most needs it for.
                if self._handle_side_button(event.button()):
                    return True
            elif event.type() == QEvent.Type.MouseMove:
                # The viewport's own top is the page's top while a
                # chapter is open (no layout margin there), so this
                # needs no coordinate mapping - and mapToGlobal is
                # wrong on mixed-DPI displays anyway.
                y = event.position().y()
                if y <= BAR_REVEAL_PX:
                    self._reveal_bar()
                # Hover is the *only* way the chapter controls come back
                # (the owner's ask: scrolling must never summon them),
                # so this is also the only place that shows them.
                if y >= self._strip_view.viewport().height() - BOTTOM_REVEAL_PX:
                    self._reveal_bottom()
                else:
                    self._hide_bottom()
            elif event.type() == QEvent.Type.Leave:
                # Pointer off the reading surface - which is also what
                # the pointer arriving *on* the controls looks like from
                # here, so where it actually went has to be asked. See
                # _pointer_wants_bottom: hiding unconditionally is what
                # made the bar impossible to click.
                self._hide_bottom_unless_aimed_at()
        elif (obj in (self._bottom_widgets or ())
                and event.type() == QEvent.Type.Leave):
            QTimer.singleShot(0, self._hide_bottom_unless_aimed_at)
        return super().eventFilter(obj, event)

    def showEvent(self, event):
        super().showEvent(event)
        # The bar's buttons only take real positions once the page is up,
        # and _place_title measures the gap between two of them - placing
        # it again here is what stops the title being elided against a
        # bar whose layout had not run when _sync_controls first asked.
        self._place_title()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # Guarded because adding this page's own layout in _build can
        # resize it before the floating children exist - they are
        # deliberately built last (they are not in any layout).
        if getattr(self, "_bottom_widgets", None) is None:
            return
        self._bar.setGeometry(0, 0, self.width(), BAR_HEIGHT)
        self._place_title()
        self._place_bottom()
        # A resize is also how full screen arrives, and it can be
        # triggered from the title bar rather than from this page.
        self._sync_fullscreen_glyph()

    def leave(self):
        """Close the reader and uncover whatever it was opened over.

        **Back where you were, not Home** - the owner's ask, 21 August
        2026: "back btn from any reading ch list takes me home, make it
        take me to the last visited exactly like the ep list in
        watching". This used to land on Home deliberately, which was an
        earlier ask; the ep list is the model now, and the ep list does
        nothing at all on the way out.

        That is the whole implementation: the details page never
        navigated either, it just hid itself and let the page underneath
        show through. The reader covers the *central widget* rather than
        replacing a page, so the page it was opened over is still there,
        still built, and still where it was scrolled to - revealing it
        is both the right destination and free.

        The sidebar needs nothing put back either: it was never
        modified, only covered, so it reappears the moment this widget
        is hidden. Dropping the navigation also drops the whole-page
        rebuild that used to follow every Back press (LEAVE_NAV_MS)."""
        if self._closed:
            return
        self._closed = True
        # Leaving the reader is leaving the manga - this lands on Home,
        # not back on the chapter list - so the music goes with it. The
        # grace period is what keeps it playing while the *details*
        # chapter list swaps one reader for the next: that reader opens
        # for the same title inside the window and cancels this.
        stop_music(_MUSIC_STOP_GRACE_MS)
        self._save_timer.stop()
        self._write_position()
        self._drop_refresh_toast()
        self._run += 1          # nothing still in flight may touch this
        host = getattr(self, "_host", None)
        if host is not None:
            host.removeEventFilter(self)
        self.closed.emit()
        self.hide()
        window = self.window()

        def land():
            # Deliberately holds no reference to the reader - it is
            # deleteLater'd below and may well be gone by the time this
            # runs. A dead window raises RuntimeError, which is the
            # normal case on app shutdown, not an error worth a log.
            try:
                page = getattr(window, "_current_page", None)
                if page is not None:
                    page.setFocus()
            except RuntimeError:
                pass
            except Exception:
                logs.exception("reader could not hand focus back")

        QTimer.singleShot(0, land)
        self.deleteLater()


# ---- background jobs -----------------------------------------------------
def _list_chapters_job(signals, run, entry, refresh=False):
    """The chapter list for one entry. Runs on a lookup_pool worker and
    must never raise - a dead worker takes every queued lookup with it.

    Reports the source's *first page* as soon as it parses (see
    _ChapterSignals.partial), which is the difference between a reader
    that shows the newest forty chapters in a second and one that shows
    nothing for the six-plus seconds a full paginated list takes."""
    def partial(chapters):
        try:
            signals.partial.emit(run, list(chapters or []))
        except RuntimeError:
            pass        # the reader closed under the fetch
    try:
        chapters = chapter_source.list_chapters(
            entry, deadline=net.deadline_in(CHAPTER_LIST_TIMEOUT),
            refresh=bool(refresh), on_partial=partial)
        signals.listed.emit(run, list(chapters or []), "")
    except Exception as error:
        logs.exception("reader chapter listing failed")
        signals.listed.emit(run, None, _reason(error))


def _chapter_pages_job(signals, run, chapter):
    try:
        payload = chapter_source.chapter_pages(
            chapter, deadline=net.deadline_in(CHAPTER_PAGES_TIMEOUT))
        if not payload:
            signals.opened.emit(run, None, "The source returned nothing for it.")
            return
        signals.opened.emit(run, dict(payload), "")
    except Exception as error:
        logs.exception("reader chapter open failed")
        signals.opened.emit(run, None, _reason(error))


def _reason(error) -> str:
    """A failure in words, ending in a full stop - these are pasted
    straight into a sentence in front of the user."""
    text = str(error).strip()
    if not text:
        return f"The source failed with {type(error).__name__}."
    return text if text.endswith(".") else text + "."


def _origin_page_name(window):
    """Which of main.PAGES the reader is being opened over.

    Read off the window's own navigation history rather than passed in,
    because the callers are entry cards on two different pages and
    neither knows its own name - and importing main here to look up
    `_current_page`'s class would be a circular import at load time.
    None whenever the window is not the real one (a test harness, a
    stub), which the door button treats as "nowhere to go back to"."""
    try:
        history = getattr(window, "_history", None)
        index = getattr(window, "_history_index", None)
        if history and isinstance(index, int) and 0 <= index < len(history):
            return history[index]
    except Exception:
        pass
    return None


# The music belongs to the *title being read*, not to the clock.
#
# It used to be throttled: opened once, then refused for ten minutes, so
# a reader opened again half an hour later got music and one opened five
# minutes later got silence. The owner's ask is simpler and has nothing
# to do with elapsed time - "if it opens once, then if I leave the ch it
# closes, then when I enter a new ch it reopens; while moving from ch to
# ch without leaving, do not close it; close when leaving the whole
# manga page".
#
# So: one music window per entry. Opening a reader for the entry that is
# already playing does nothing at all; leaving the reader closes the
# window; opening one again starts it again.
#
# `hwnd` is the window the launch actually created, found by the same
# sweep that minimizes it (_hush_browser_window). It is only ever a
# window this app opened *itself*, in its own new browser window - see
# _launch_music - because closing a window that happened to be carrying
# the owner's other tabs would be a far worse bug than music that keeps
# playing.
_music = {"key": None, "hwnd": None, "pid": None, "settled": False,
          "gesture_sent": False, "pressed_at": None, "reviving": False}

# Every browser pid this session ever spawned for music, so a stuck
# close can prove a process is ours before ending it - see
# _force_close_music. Only ever grows; pids are 4 bytes and a session
# spawns a handful.
_music_pids = set()

# Leaving a reader does not close the music instantly: opening a chapter
# from the details list tears the old reader down and builds a new one,
# and a stop that fired in between would kill the music every time a
# chapter was turned. A reader opening for the same entry inside this
# window cancels the stop.
_MUSIC_STOP_GRACE_MS = 600
_music_stop_timer = None

# How long after the launch to keep watching for the music window, and
# how often to look. A cold browser can take over a second to put its
# first window up, and some put a splash up before the real one - so the
# sweep keeps going for the whole window rather than stopping on the
# first hit (see _hush_browser_window).
#
# 22 August 2026: raised from 4.0 to comfortably outlast
# _MUSIC_AUTOPLAY_GRACE_MS. sweep() reuses this same loop to watch for
# the browser stealing focus back *after* being tucked away, and at 4.0
# that watchdog was already dead before the old _MUSIC_TUCK_MS=6000
# delay it was meant to cover had even fired - a pre-existing mismatch,
# not something introduced here, but not worth leaving to get worse.
# 9.0 = ~1s to first-detect (measured 0.5-0.6s) + the 5.0s grace + about
# the original's own ~3.5s of post-action watchdog coverage (it used to
# run from the ~0s bury to its 4.0s deadline).
_MUSIC_HUSH_S = 9.0
_MUSIC_HUSH_STEP_MS = 120

# How long to leave a freshly-appeared music window completely alone -
# still foreground, still on top, actually visible - before touching it
# at all. This replaces _MUSIC_TUCK_MS, which timed the wrong step.
#
# 22 August 2026: the owner's report ("music URL open... paused at
# 0:00/2:56 with the big play button up") pointed straight back to a
# contradiction already sitting in this file's own comments - they say
# a fully covered Chromium window is document.hidden exactly like a
# minimized one, and that Chromium refuses to autoplay unmuted media in
# a hidden page, and then sweep() (below) pushed the window behind and
# pulled Atomic's foreground back in the *same tick* it first saw the
# window - covering it within one _MUSIC_HUSH_STEP_MS (120ms) of it
# existing, the exact state those comments say kills autoplay. The old
# _MUSIC_TUCK_MS=6000 delay was real but pointed at the wrong step: it
# postponed the SW_MINIMIZE call, several seconds after the window had
# already been fully occluded by the immediate bury+refront above it.
#
# Verified live before writing this fix (not just reasoned about): drove
# the real _open_music_quietly/_hush_browser_window against a real
# YouTube video, with a throwaway stand-in window for Atomic (never the
# real one). With the browser window continuously covered from the
# moment it was first seen (0.53s after launch) onward, a forced peek at
# the underlying page still showed a black, not-yet-painted frame at
# +3.0s: by +5.0s it was several seconds into the clip with no "blocked"
# play-button overlay, so on *this* real, heavily-used Brave profile the
# video started playing regardless of being covered the whole time -
# most likely because youtube.com already has a high Media Engagement
# Index (or Brave's own per-site autoplay permission) on this profile,
# both of which Chromium documents as exemptions separate from page
# visibility. Neither is something this code can read or a fresh domain
# or a cleared profile can be assumed to have, so the grace stays in
# regardless of that result: it costs nothing where the site is already
# exempt, and it is the only thing that can help where it is not. Not
# re-measured against a low-engagement domain - that would mean reading
# the owner's actual configured URL, which a test must not do (CLAUDE.md
# rule 5) - so say plainly that this is unconfirmed for that case.
#
# 5000, not the 3000 first tried: +3.0s still showed the black,
# not-yet-painted frame in two separate runs, so anything near there
# risked burying it at the exact moment it needed to be left alone the
# most; +5.0s was the first point actually confirmed playing.
_MUSIC_AUTOPLAY_GRACE_MS = 5000

# The default browser's full path, resolved once and then remembered -
# including the failure, as "".
_BROWSER_PATH = None

# The switch that makes each of these open a window of its own rather
# than a tab in whatever is already up. That window is the one this app
# is then allowed to close again, which is the whole reason for knowing
# the browser by name at all.
_NEW_WINDOW_SWITCH = {
    "chrome.exe": "--new-window", "msedge.exe": "--new-window",
    "brave.exe": "--new-window", "opera.exe": "--new-window",
    "vivaldi.exe": "--new-window", "chromium.exe": "--new-window",
    "firefox.exe": "-new-window", "librewolf.exe": "-new-window",
    "waterfox.exe": "-new-window", "zen.exe": "-new-window",
}


def _default_browser_path() -> str:
    """Full path of whatever opens an http:// link on this machine, or
    "" if it can't be read.

    Read from the user's own file association rather than matched
    against a list of known browsers: the music URL is opened through
    exactly this program, and being sure of that is what makes it safe
    to minimize its window (see _hush_browser_window) and, now, to close
    it again (see stop_music)."""
    global _BROWSER_PATH
    cached = _BROWSER_PATH
    if cached is not None:
        return cached
    # **Resolved into a local and published once, at the end.** Writing
    # the "" sentinel into the global *before* doing the registry read
    # is why the reading music never played on the first open of a
    # session: _open_music_quietly starts the launch thread and then
    # calls _hush_browser_window on the UI thread, so the worker gets
    # here first, publishes "", and spends 0.287ms in the registry -
    # during which _default_browser_exe() reads that "" and
    # _hush_browser_window returns without installing its sweep at all.
    # Measured 22 August 2026: **200 runs out of 200** in that order.
    # With no sweep the music window is never pushed behind Atomic, its
    # handle is never recorded (so stop_music can never close it), and
    # the only thing left touching it is the two blind _refront calls,
    # which bury it under a maximized Atomic - and a fully covered
    # Chromium window is document.hidden exactly as a minimized one is,
    # which is the state this whole dance already exists to avoid (see
    # _hush_browser_window). Second open onwards the value is cached and
    # everything worked, which is precisely what the owner reported.
    path = ""
    if os.name != "nt":
        _BROWSER_PATH = path
        return path
    try:
        import winreg
        with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\Shell\Associations"
                r"\UrlAssociations\http\UserChoice") as key:
            prog_id = winreg.QueryValueEx(key, "ProgId")[0]
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT,
                            rf"{prog_id}\shell\open\command") as key:
            command = (winreg.QueryValueEx(key, "")[0] or "").strip()
        # '"C:\\...\\brave.exe" -- "%1"', or an unquoted path followed by
        # its arguments. Taking the quoted head rather than splitting on
        # spaces, because Program Files has one in it.
        if command.startswith('"'):
            exe = command[1:].split('"', 1)[0]
        else:
            exe = command.split(" ", 1)[0]
        path = exe if os.path.exists(exe) else ""
    except Exception:
        path = ""              # no association to read: leave it alone
    # Two threads racing here now both do the same read and store the
    # same answer, which costs 0.287ms once and cannot be observed;
    # publishing early cost the music instead.
    _BROWSER_PATH = path
    return path


def _default_browser_exe() -> str:
    """Lowercased file name of the above ("brave.exe", "chrome.exe"), or
    "" - which is what the window sweep matches against."""
    return os.path.basename(_default_browser_path()).lower()


# Why there is a keystroke in the music launch, and not an autoplay
# flag. Chromium autoplays unmuted media only for sites the *profile*
# has earned a high Media Engagement Index on, and nothing this app
# does can grant that: measured 22-23 August 2026 with a probe page
# reporting its own state, the owner's profile sat
# `AudioContext state=suspended` from first paint through the whole
# foreground grace and forever after, and his real signed-in YouTube
# watch page sat on 0:00 with the big play button for 14+ seconds of
# being foreground and unoccluded. `--autoplay-policy=
# no-user-gesture-required` fixes exactly that, but only when the
# launch *starts* the browser process, which forced a private
# --user-data-dir profile - which is signed out, and the owner's music
# URL is a private playlist on his Premium account, which a signed-out
# profile cannot open at all. So the profile is the owner's own, and
# playback is started by _grant_play_gesture instead: one real
# OS-level `k` into the window, which Chromium counts as genuine user
# activation where every scripted gesture is ignored.
_MUSIC_GESTURE_DELAY_MS = 2500      # page-load headroom after first sight
# How often to ask whether the browser has started making noise, while
# deciding when to tuck its window away.
#
# **Measured 23 August 2026, because this poll runs on the UI thread:**
# the first _browser_is_audible() call costs 48.7ms (COM init plus device
# enumeration) and every one after it 3.5-5.2ms. 250ms with a ~4ms probe
# is under 2% of the thread and never two probes inside one frame, which
# is why it is safe here; the 48.7ms one is deliberately paid off-thread
# by the launch worker before this poll ever starts (see
# _open_music_quietly), because 48.7ms on the UI thread is three dropped
# frames at 60Hz and the whole point of this poll is that the app stays
# smooth while it runs.
_MUSIC_AUDIBLE_POLL_MS = 250
_MUSIC_GESTURE_POLL_MS = 400

# How long the window must stay untouched *after the keystroke* before
# the tuck may minimize it. The press starts the player, but the player
# takes a beat to actually roll (0.3-1.5s observed before the first
# audio), and a page hidden before its playback is really rolling can
# defer or park it - which is the owner's original screenshot: parked
# at 0:00 with the big play button, in the days when the window was
# buried within 120ms. So a press buys playback a guaranteed stretch on
# screen, and the tuck is re-armed from the press rather than only from
# first sight. (An earlier version of this comment blamed the tuck for
# a run that ended "paused at 0:01" - the audio meter showed that was
# the keystroke itself pausing a page that had autoplayed, see
# _browser_is_audible, and the tuck was innocent: playback that had
# audibly started survived the minimize in every metered run.)
_MUSIC_POST_PRESS_VISIBLE_S = 5.0


def _send_key(vk):
    """One real key press+release through SendInput. Goes to whatever
    window holds the keyboard focus, which is why the caller must prove
    that window is the music window first."""
    import ctypes
    from ctypes import wintypes

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                    ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                    ("dwExtraInfo", ctypes.c_size_t)]

    class _INPUTUNION(ctypes.Union):
        # Padded to MOUSEINPUT's 32 bytes so sizeof(INPUT) is what
        # user32 expects (40 on x64); a short struct makes SendInput
        # reject the whole batch with ERROR_INVALID_PARAMETER.
        _fields_ = [("ki", KEYBDINPUT), ("padding", ctypes.c_ubyte * 32)]

    class INPUT(ctypes.Structure):
        _anonymous_ = ("u",)
        _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]

    down = INPUT(type=1)                        # INPUT_KEYBOARD
    down.ki = KEYBDINPUT(vk, 0, 0, 0, 0)
    up = INPUT(type=1)
    up.ki = KEYBDINPUT(vk, 0, 2, 0, 0)          # KEYEVENTF_KEYUP
    batch = (INPUT * 2)(down, up)
    ctypes.windll.user32.SendInput(2, batch, ctypes.sizeof(INPUT))


# How long after the media key to keep listening before concluding it
# did nothing. The key is delivered to the session immediately; YouTube
# takes 0.3-1.5s to actually roll (the same runway the visible press
# needed), so 3s is comfortable without stalling the fallback.
_MUSIC_MEDIA_KEY_WAIT_S = 3.0
VK_MEDIA_PLAY_PAUSE = 0xB3
# How long the page is given to start itself before any key is pressed.
# Stage 1's own measurement is "~2s in" (23 August 2026); 4s leaves room
# for a cold profile without stalling the escalation noticeably.
_MUSIC_AUTOPLAY_WAIT_S = 4.0


def _start_music_minimized(hwnd, window):
    """Start playback in the already-minimized music window - the
    owner's open -> minimize -> play, 24 August 2026 - escalating only
    as far as silence forces:

      1. **Autoplay.** A handoff launch into his running profile
         autoplays on its own ~2s in (measured 23 August 2026, visible;
         whether it also does so minimized is what the sensor decides
         here rather than anyone guessing).
      2. **The media key**, which reaches a media session foreground or
         not - guarded on the whole machine being silent, because
         VK_MEDIA_PLAY_PAUSE goes to whichever session Windows deems
         current and would pause the owner's other music instead.
      3. **The visible press** - the pre-24-August behaviour whole:
         restore, foreground, `k`, re-tuck once audible. Last resort
         only, because it is the flash the new order exists to remove;
         kept, because a music feature that can silently produce no
         music is worse than one that flashes.

    Every stage logs what it did, so "the music did not play" reports
    are diagnosable from atomic.log."""
    if _music.get("hwnd") != hwnd or _music.get("gesture_sent"):
        return
    try:
        # **Autoplay gets its runway before any key is pressed.** This
        # used to read the sensor once, immediately, and send the media
        # key the moment it found silence - but stage 1 above is
        # measured at *~2s in*, so at the instant this runs the page has
        # not loaded and silence is the only possible answer. The key
        # therefore went out on every single open, and then raced the
        # autoplay it was meant to be the fallback for: whichever landed
        # second toggled the other off. That is the owner's report, 26
        # August 2026 - "the music URL does not play, it always pause
        # the music automatically" - and it is a toggle being used where
        # "play" was meant.
        #
        # So: poll for the page starting by itself first, and only reach
        # for the key once the runway has actually run out.
        started = time.monotonic()

        def after_runway():
            if _music.get("hwnd") != hwnd or _music.get("gesture_sent"):
                return
            if _browser_is_audible():
                _music["gesture_sent"] = True
                logs.info("music: autoplayed while minimized")
                return
            if time.monotonic() - started < _MUSIC_AUTOPLAY_WAIT_S:
                QTimer.singleShot(_MUSIC_AUDIBLE_POLL_MS, after_runway)
                return
            _press_media_key(hwnd, window)

        after_runway()
    except Exception:
        logs.exception("could not start the minimized music")


def _press_media_key(hwnd, window):
    """Stage 2: the media key, once autoplay has had its chance."""
    try:
        if not _anything_is_audible():
            _send_key(VK_MEDIA_PLAY_PAUSE)
            logs.info("music: media key sent (machine was silent)")
        else:
            logs.info("music: machine already audible; media key withheld")
        deadline = time.monotonic() + _MUSIC_MEDIA_KEY_WAIT_S

        def listen():
            if _music.get("hwnd") != hwnd or _music.get("gesture_sent"):
                return
            if _browser_is_audible():
                _music["gesture_sent"] = True
                logs.info("music: playing after the media key")
                return
            if time.monotonic() < deadline:
                QTimer.singleShot(_MUSIC_AUDIBLE_POLL_MS, listen)
                return
            _music_visible_press(hwnd, window)

        QTimer.singleShot(_MUSIC_AUDIBLE_POLL_MS, listen)
    except Exception:
        logs.exception("could not press the media key")


def _music_visible_press(hwnd, window):
    """The last resort: put the window up, press `k` the moment it is
    foreground, and tuck it again once sound arrives (or the runway
    runs out). This is the whole pre-24-August flow, demoted."""
    if _music.get("hwnd") != hwnd or _music.get("gesture_sent"):
        return
    logs.info("music: still silent; falling back to the visible press")
    try:
        import ctypes
        user32 = ctypes.windll.user32
        if not user32.IsWindow(hwnd):
            return
        _music["reviving"] = True       # the sweep must not slam it shut
        user32.ShowWindow(hwnd, 9)      # SW_RESTORE
        user32.SetForegroundWindow(hwnd)
        gesture_deadline = time.monotonic() + _MUSIC_AUTOPLAY_GRACE_MS / 1000.0
        QTimer.singleShot(
            400, lambda: _grant_play_gesture(hwnd, gesture_deadline))
        started = time.monotonic()

        def retuck():
            if _music.get("hwnd") != hwnd:
                _music["reviving"] = False
                return
            pressed = _music.get("pressed_at")
            runway_done = (pressed is not None and time.monotonic() - pressed
                           >= _MUSIC_POST_PRESS_VISIBLE_S)
            out_of_time = (time.monotonic() - started
                           >= _MUSIC_AUTOPLAY_GRACE_MS / 1000.0
                           + _MUSIC_POST_PRESS_VISIBLE_S)
            if _browser_is_audible() or runway_done or out_of_time:
                _music["reviving"] = False
                _music["settled"] = False
                _tuck_music_window(hwnd, window)
                return
            QTimer.singleShot(_MUSIC_AUDIBLE_POLL_MS, retuck)

        QTimer.singleShot(_MUSIC_AUDIBLE_POLL_MS, retuck)
    except Exception:
        _music["reviving"] = False
        logs.exception("the visible music press failed")


def _grant_play_gesture(hwnd, deadline):
    """Press `k` in the music window, once, so the page starts playing.

    A real input event is the one thing Chromium accepts as user
    activation - the owner's page needs a play command it can attribute
    to a person, and `k` is YouTube's own play/pause key (chosen over
    space, which a focused button would swallow as a click).

    Guarded hard, because a stray keystroke into the wrong window would
    be a worse bug than silent music: it is sent only while the exact
    window the sweep identified is the *foreground* window - a
    foreground window of any other process, the owner's own browser
    included, means no press - and at most once per launch
    (_music["gesture_sent"]). Until the grace deadline it re-polls
    rather than gives up, because the browser may still be putting the
    window up; after it, never.

    `k` toggles, so pressing a page that is already playing would
    *pause* it - and that is not hypothetical: on the owner's profile a
    handoff-launched window autoplays ~2s in while a cold-started one
    parks at 0:00 (both measured, 23 August 2026). The audible check
    below is what tells them apart: silence means the press is play,
    sound means there is nothing to do."""
    try:
        if _music.get("hwnd") != hwnd or _music.get("gesture_sent"):
            return              # a newer launch took over, or done
        if _browser_is_audible():
            # The page beat us to it. A handoff-launched window on the
            # owner's profile autoplays on its own ~2s in, and `k` on a
            # playing player is *pause* - measured doing exactly that,
            # three runs, before this gate existed (see
            # _browser_is_audible). Audible means finished here.
            _music["gesture_sent"] = True
            return
        import ctypes
        user32 = ctypes.windll.user32
        if not user32.IsWindow(hwnd):
            return
        if user32.GetForegroundWindow() != hwnd:
            if time.monotonic() < deadline:
                QTimer.singleShot(
                    _MUSIC_GESTURE_POLL_MS,
                    lambda: _grant_play_gesture(hwnd, deadline))
            return
        _music["gesture_sent"] = True
        # The tuck reads this: playback must get its runway on screen
        # after the press - see _MUSIC_POST_PRESS_VISIBLE_S.
        _music["pressed_at"] = time.monotonic()
        _send_key(0x4B)                         # 'K'
    except Exception:
        pass        # a window that died mid-poll is not worth a log


def _autoplay_url(url: str) -> str:
    """The configured music URL, in the form it should be opened.

    One line, because the implementation moved to
    `app_settings.music_launch_url` - the reader is not the only thing
    that opens this URL. `tracker._open_manga_entry` opens it too and was
    passing it through raw, so the same link autoplayed from one door and
    not the other; a transform used by two callers belongs beside the
    setting rather than inside one page."""
    return app_settings.music_launch_url(url)


def _process_exe(pid) -> str:
    """Lowercased file name of process `pid`, or ""."""
    import ctypes
    from ctypes import wintypes
    if not pid:
        return ""
    # PROCESS_QUERY_LIMITED_INFORMATION. Not PROCESS_QUERY_INFORMATION:
    # the limited form is the one a normal process is allowed to open on
    # another process of the same user, and the browser is one.
    handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(512)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not ctypes.windll.kernel32.QueryFullProcessImageNameW(
                handle, 0, buffer, ctypes.byref(size)):
            return ""
        return os.path.basename(buffer.value).lower()
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


def _window_exe(hwnd) -> str:
    """Lowercased file name of the process owning `hwnd`, or ""."""
    import ctypes
    from ctypes import wintypes
    pid = wintypes.DWORD()
    ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return _process_exe(pid.value)


def _browser_is_audible() -> bool:
    """True when the default output device is currently carrying audio
    from the default browser's processes - see _audible."""
    exe = _default_browser_exe()
    return _audible(exe) if exe else False


def _anything_is_audible() -> bool:
    """True when *any* process is putting sound out. The guard in front
    of the media key (see _start_music_minimized): VK_MEDIA_PLAY_PAUSE
    goes to whichever media session Windows considers current, and
    pressing it while the owner's Spotify - or anything else - is
    playing would pause that instead of starting this. Silence
    everywhere is the one state where the key can only do its job."""
    return _audible(None)


def _audible(exe) -> bool:
    """True when the default output device is currently carrying audio
    from `exe`'s processes - or from any process at all when `exe` is
    None.

    This is the sensor that keeps _grant_play_gesture's `k` from doing
    the opposite of its job. Measured 23 August 2026, owner's real
    profile: a music window opened by *handoff* into his running Brave
    autoplayed on its own ~2s after appearing (session peak 0.12-0.29 on
    the meter), and the keystroke landed as *pause* - the audio died
    within a second of the press, which is the whole of three runs that
    ended "paused at 0:01". A cold-started browser never autoplayed
    (0:00 with the play overlay for 14+ foreground seconds) and needs
    the press. Silence is the one signal that separates the two.

    Raw COM against WASAPI (MMDeviceEnumerator -> IAudioSessionManager2
    -> per-session IAudioMeterInformation), no new dependency. Costs
    ~1-3ms per call; called a handful of times per music launch. Any
    failure anywhere returns False - the press then happens exactly as
    it would have without this sensor. Never raises."""
    if os.name != "nt":
        return False
    import ctypes
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [("d1", ctypes.c_uint32), ("d2", ctypes.c_uint16),
                    ("d3", ctypes.c_uint16), ("d4", ctypes.c_ubyte * 8)]

    try:
        ole32 = ctypes.windll.ole32

        def guid(text):
            out = GUID()
            ole32.CLSIDFromString(text, ctypes.byref(out))
            return out

        # S_FALSE (already initialised, Qt does this) is fine.
        ole32.CoInitialize(None)

        def com(obj, index, argtypes, *args):
            vtbl = ctypes.cast(
                obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))
            ).contents
            proto = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p,
                                       *argtypes)
            return proto(vtbl[index])(obj, *args)

        def release(obj):
            try:
                if obj:
                    com(obj, 2, [])
            except Exception:
                pass

        CLSID_MMDeviceEnumerator = guid(
            "{BCDE0395-E52F-467C-8E3D-C4579291692E}")
        IID_IMMDeviceEnumerator = guid(
            "{A95664D2-9614-4F35-A746-DE8DB63617E6}")
        IID_IAudioSessionManager2 = guid(
            "{77AA99A0-1BD6-484F-8BC7-2C654C9A9B6F}")
        IID_IAudioSessionControl2 = guid(
            "{BFB7FF88-7239-4FC9-8FA2-07C950BE9C6D}")
        IID_IAudioMeterInformation = guid(
            "{C02216F6-8C67-4B5B-9D00-D008E73E0064}")

        enum = ctypes.c_void_p()
        if ole32.CoCreateInstance(
                ctypes.byref(CLSID_MMDeviceEnumerator), None, 23,
                ctypes.byref(IID_IMMDeviceEnumerator),
                ctypes.byref(enum)) or not enum:
            return False
        device = ctypes.c_void_p()
        manager = ctypes.c_void_p()
        session_enum = ctypes.c_void_p()
        audible = False
        try:
            # IMMDeviceEnumerator::GetDefaultAudioEndpoint(eRender,
            # eMultimedia)
            if com(enum, 4, [ctypes.c_uint, ctypes.c_uint,
                             ctypes.POINTER(ctypes.c_void_p)],
                   0, 1, ctypes.byref(device)) or not device:
                return False
            # IMMDevice::Activate(IAudioSessionManager2, CLSCTX_ALL)
            if com(device, 3, [ctypes.POINTER(GUID), ctypes.c_uint,
                               ctypes.c_void_p,
                               ctypes.POINTER(ctypes.c_void_p)],
                   ctypes.byref(IID_IAudioSessionManager2), 23, None,
                   ctypes.byref(manager)) or not manager:
                return False
            if com(manager, 5, [ctypes.POINTER(ctypes.c_void_p)],
                   ctypes.byref(session_enum)) or not session_enum:
                return False
            count = ctypes.c_int(0)
            com(session_enum, 3, [ctypes.POINTER(ctypes.c_int)],
                ctypes.byref(count))
            for index in range(count.value):
                session = ctypes.c_void_p()
                if com(session_enum, 4,
                       [ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)],
                       index, ctypes.byref(session)) or not session:
                    continue
                control2 = ctypes.c_void_p()
                meter = ctypes.c_void_p()
                try:
                    if com(session, 0, [ctypes.POINTER(GUID),
                                        ctypes.POINTER(ctypes.c_void_p)],
                           ctypes.byref(IID_IAudioSessionControl2),
                           ctypes.byref(control2)) or not control2:
                        continue
                    pid = ctypes.c_uint32(0)
                    com(control2, 14, [ctypes.POINTER(ctypes.c_uint32)],
                        ctypes.byref(pid))
                    if exe is not None and _process_exe(pid.value) != exe:
                        continue
                    if com(session, 0, [ctypes.POINTER(GUID),
                                        ctypes.POINTER(ctypes.c_void_p)],
                           ctypes.byref(IID_IAudioMeterInformation),
                           ctypes.byref(meter)) or not meter:
                        continue
                    peak = ctypes.c_float(0.0)
                    com(meter, 3, [ctypes.POINTER(ctypes.c_float)],
                        ctypes.byref(peak))
                    # Playing audio measured 0.12-0.29; silence 0.0000.
                    if peak.value > 0.01:
                        audible = True
                        break
                finally:
                    release(meter)
                    release(control2)
                    release(session)
        finally:
            release(session_enum)
            release(manager)
            release(device)
            release(enum)
        return audible
    except Exception:
        return False


def _find_window_of_pid(pid, exclude=0) -> int:
    """First visible, unowned top-level window belonging to `pid`, or 0.
    `exclude` skips one handle - _force_close_music asks "does this
    process own any window *besides* the one being closed".

    When the music launch cold-starts the browser, the Popen'd pid *is*
    the browser process and the window can be found by process id
    instead of waiting for it to take the foreground. That wait was
    measured failing 22 August 2026: on one first launch Brave put its
    window up without ever becoming the foreground window, so the sweep
    recorded no handle, nothing was ever tucked behind Atomic, and
    stop_music had nothing to close. A handoff to an already-running
    browser exits the launcher pid at once, and there the foreground
    sweep is the answer instead - it also remains first try on a cold
    start, and is what spots the window stealing focus back later."""
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def enum(hwnd, _lparam):
        if exclude and int(hwnd) == int(exclude):
            return True
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if (owner.value == pid and user32.IsWindowVisible(hwnd)
                and not user32.GetWindow(hwnd, 4)):     # GW_OWNER: skip
            found.append(int(hwnd))                     # popups/tooltips
            return False        # stop the enumeration - found it
        return True

    try:
        user32.EnumWindows(enum, 0)
    except Exception:
        pass        # a stopped enumeration reports failure; found[] is set
    return found[0] if found else 0


def _fallback_refront(window):
    """The launch-time safety net: refront Atomic only when the sweep
    never identified the music window at all (no browser association to
    read, or the window never appeared). Once a window is known, the
    tuck path is the sole owner of the refront - see the call site."""
    if _music.get("hwnd") is None:
        _refront(window)


def _refront(window):
    """Pull Atomic back over whatever just took the foreground."""
    try:
        if window.isMinimized():
            window.showNormal()
        window.raise_()
        window.activateWindow()
        if os.name == "nt":
            import ctypes
            # activateWindow is a request Windows is free to ignore for
            # a process that no longer holds the foreground; asking
            # user32 directly is the stronger form, and its worst case
            # is a taskbar flash rather than a buried app.
            ctypes.windll.user32.SetForegroundWindow(int(window.winId()))
    except Exception:
        pass        # a closed window mid-timer is not worth a log


def _hush_browser_window(window, was_foreground):
    """Watch for the music window, leave it alone while it starts, then
    put it down.

    **The show hint does nothing.** ShellExecuteW is asked for
    SW_SHOWMINNOACTIVE, but a browser opens a normal, focused window
    regardless - measured, and it does the same when asked for SW_HIDE
    outright, on both a cold start and a handoff to an already-running
    instance. So what the owner once saw as "a small window opens then
    closes fast" was never a window closing: it was the browser
    appearing and then being *covered* by Atomic a moment later. The
    flash was the gap between the two - which is exactly the gap this
    function now makes deliberately wide instead of closing fast (see
    _MUSIC_AUTOPLAY_GRACE_MS).

    Deliberately narrow about which window: only one that belongs to
    this machine's own default http handler *and* was not already the
    foreground window before the launch. A browser the owner was reading
    when the reader opened is left where it was, and no other
    application can be caught by this at all. With no association to
    read, nothing is touched and the behaviour is what it always was."""
    exe = _default_browser_exe()
    if not exe or os.name != "nt":
        return
    import ctypes
    user32 = ctypes.windll.user32
    deadline = time.monotonic() + _MUSIC_HUSH_S

    def first_sighting(hwnd):
        # **Minimize first, then play - the order is the owner's, 24
        # August 2026:** "open URL - minimize - play the music". The old
        # order (leave it visible, press `k`, tuck once audible) existed
        # because a page hidden at load can park at 0:00 and the `k`
        # gesture needs the window foreground - both still true, both
        # still handled: playback is now started *minimized* in stages
        # (autoplay, then the media key), and the visible press survives
        # only as the last resort when nothing else made a sound - see
        # _start_music_minimized. Remembered so stop_music can still
        # close exactly this handle.
        _music["hwnd"] = int(hwnd)
        _tuck_music_window(int(hwnd), window)
        QTimer.singleShot(
            _MUSIC_GESTURE_DELAY_MS,
            lambda h=int(hwnd): _start_music_minimized(h, window))

    def sweep():
        try:
            hwnd = user32.GetForegroundWindow()
            if hwnd and hwnd != was_foreground and _window_exe(hwnd) == exe:
                if _music.get("hwnd") is None:
                    first_sighting(hwnd)
                elif _music.get("settled") and not _music.get("reviving"):
                    # Already had its grace period and been tucked away
                    # once - this is the same window stealing focus back
                    # afterwards (a browser can do that on its own a beat
                    # later), so reclaim immediately. `reviving` is the
                    # one exception: the last-resort visible press has
                    # restored it on purpose (_start_music_minimized)
                    # and this sweep must not slam it shut mid-press.
                    user32.ShowWindow(hwnd, 6)         # SW_MINIMIZE
                    _refront(window)
            elif _music.get("hwnd") is None and _music.get("pid"):
                # Not foreground - which a fresh profile's first launch
                # never was, measured 22 August 2026 - but the process
                # is our own child, so ask for its window directly.
                hwnd = _find_window_of_pid(_music["pid"])
                if hwnd and hwnd != was_foreground:
                    first_sighting(hwnd)
            if time.monotonic() < deadline:
                QTimer.singleShot(_MUSIC_HUSH_STEP_MS, sweep)
        except Exception:
            pass    # a window that died mid-sweep is not worth a log

    QTimer.singleShot(_MUSIC_HUSH_STEP_MS, sweep)


def _tuck_music_window(hwnd, window):
    """Minimize the music window and give Atomic its foreground back -
    immediately at first sighting under the 24 August open->minimize->
    play order, and again after the last-resort visible press.

    By now the page has either already started playing - in which case
    minimizing does not stop it, this file's own prior measurements say
    a browser keeps an already-playing page going once minimized - or it
    was never going to autoplay hidden or not, in which case tucking it
    away does not make that worse. Either way Atomic gets refronted
    unconditionally, even if the window died in the meantime: a reader
    that lost the foreground for its grace period must always get it
    back, not only when the tuck itself succeeds.

    Guarded on the handle still being ours: the owner may have closed
    the window, or `stop_music` may have. Never raises."""
    if os.name != "nt" or _music.get("hwnd") != hwnd:
        return
    # The keystroke may have landed late (the gesture polls for the
    # window to be foreground); a tuck right on its heels pauses the
    # playback it just started - measured, see
    # _MUSIC_POST_PRESS_VISIBLE_S. Wait out the remainder first.
    pressed = _music.get("pressed_at")
    if pressed is not None:
        remaining = _MUSIC_POST_PRESS_VISIBLE_S - (time.monotonic() - pressed)
        # **Sound ends the wait early.** That wait exists to be sure the
        # press actually took - and audio coming out of the browser is
        # that certainty, arrived sooner. Without this the owner waits the
        # full post-press window on top of everything else, which is his
        # "took long time to minimize" (23 August 2026).
        try:
            if _browser_is_audible():
                remaining = 0
        except Exception:
            pass                # no sensor: keep the conservative wait
        if remaining > 0:
            QTimer.singleShot(int(remaining * 1000) + 50,
                              lambda: _tuck_music_window(hwnd, window))
            return
    try:
        import ctypes
        user32 = ctypes.windll.user32
        if user32.IsWindow(hwnd):
            user32.ShowWindow(hwnd, 6)      # SW_MINIMIZE
            _music["settled"] = True
        _refront(window)
    except Exception:
        pass        # a window that died in the meantime is not a problem


def _open_music_quietly(window, entry):
    """Open the configured reading-music URL (Settings) behind the app.

    The setting promises music *alongside* reading, but a plain
    webbrowser.open fronts the browser, which buried the reader the
    moment it opened (the owner's report) - the music is to be heard,
    not looked at.

    ShellExecuteW is still asked for SW_SHOWMINNOACTIVE (7), but that
    hint is now known to buy nothing: measured, a browser opens a
    normal focused window whether it is asked for 7 or for SW_HIDE, on
    a cold start and on a handoff alike. _hush_browser_window is what
    actually watches for and tucks away the window the launch brought
    up - deliberately not right away, see _MUSIC_AUTOPLAY_GRACE_MS - and
    the two re-fronts below are the same safety net they always were:
    a last resort for the rare case _hush_browser_window never manages
    to identify the window at all (no browser association to read), so
    they must not fire any earlier than its own grace period does, or
    they would bury the window themselves before it has had its chance.

    Never raises; no music URL means nothing happens.

    **One window per title, not one per chapter, and no clock in it.**
    Opening a reader for the title already playing does nothing at all,
    so turning pages and stepping through chapters never restarts the
    music or picks another focus fight. Leaving the reader closes it
    (see stop_music), and opening one again starts it again - which is
    the behaviour the ten-minute throttle that used to live here got
    wrong in both directions.

    Opened in a browser window of *this app's* making wherever the
    browser is one we know the switch for (_NEW_WINDOW_SWITCH), because
    a window we opened is the only kind we may close later. Anything
    else falls back to ShellExecute and simply never gets closed - the
    owner's other tabs are not ours to take down."""
    key = _music_key(entry)
    _cancel_music_stop()
    if _music["key"] == key and _music["hwnd"]:
        return                  # already playing for this title
    if _music["key"] is not None and _music["key"] != key:
        stop_music()            # a different title: swap, don't stack
    try:
        url = app_settings.get_manga_music_url()
    except Exception:
        url = ""
    if not url:
        return
    # Opened in a form that starts itself - see _autoplay_url.
    url = _autoplay_url(url)
    # Resolved here, on this thread, before anything else can want it:
    # both the launch below and _hush_browser_window ask for it, and the
    # order they used to ask in is what silenced the first open of every
    # session (see _default_browser_path). 0.287ms once per run.
    _default_browser_path()
    # Read *before* the launch: a browser window that already held the
    # foreground is one the owner put there, and is the one window
    # _hush_browser_window must not touch.
    was_foreground = 0
    if os.name == "nt":
        import ctypes
        was_foreground = ctypes.windll.user32.GetForegroundWindow()

    def launch():
        """Start the browser. **On a worker, not the UI thread.**

        Spawning a browser costs a process creation - measured as part
        of the owner's "the watch/read buttons take ~1.5 sec": this ran
        inline, before the reader page was even built, so the reading
        surface waited on a program that has nothing to do with it. It
        answers to nobody here; the only thing that has to come back to
        the UI thread is the window sweep, which is a QTimer and stays
        below.

        Never raises - it is a thread, and an uncaught exception in one
        is silent."""
        try:
            # Pay the audio sensor's one-off COM initialisation here,
            # where nobody is waiting on it. Measured 23 August 2026: the
            # first _browser_is_audible() call is 48.7ms and every one
            # after it 3.5-5.2ms, and the tuck poll that follows runs on
            # the UI thread - 48.7ms there is three dropped frames, and
            # this makes it none. The answer is discarded on purpose;
            # only the initialisation is wanted.
            try:
                _browser_is_audible()
            except Exception:
                pass
            if os.name == "nt":
                path = _default_browser_path()
                switch = _NEW_WINDOW_SWITCH.get(os.path.basename(path).lower())
                # A child process, not ShellExecute, wherever the browser
                # takes a new-window switch: ShellExecute hands the link
                # to whatever instance is already running, which puts the
                # music in a *tab* of a window full of the owner's own
                # pages - nothing this app could then close. Both routes
                # get a clean environment: the child inherits
                # PyInstaller's bootloader variables otherwise, which is
                # fatal to any child that is itself a PyInstaller build
                # (see helpers/child_process).
                with child_process.clean_environ():
                    if switch:
                        # **The owner's own profile, deliberately.** A
                        # private --user-data-dir profile with
                        # --autoplay-policy=no-user-gesture-required was
                        # built and measured playing unaided (23 August
                        # 2026) - and then removed, because the owner's
                        # music URL is a *private* playlist on his
                        # signed-in Premium account: a fresh profile is
                        # signed out, cannot open a private playlist at
                        # all, and loses Premium. What starts playback
                        # instead is a real keystroke into the window -
                        # see _grant_play_gesture. Do not bring the
                        # private profile back without solving sign-in.
                        args = [path, switch, url]
                        if switch == "--new-window":
                            # Chromium family only (Firefox has no such
                            # switch). Applies when this launch *starts*
                            # the browser; a handoff to a running
                            # instance ignores it, and must: it would
                            # change the owner's own browser session.
                            # Measured 23 August 2026: without it, a
                            # cold-started brave.exe (8 processes,
                            # ~400MB) stayed resident minutes after
                            # stop_music closed its window; with it, the
                            # process exited 1s after the window closed.
                            args.insert(2, "--disable-background-mode")
                        proc = subprocess.Popen(
                            args,
                            creationflags=getattr(subprocess,
                                                  "CREATE_NO_WINDOW", 0))
                        # On a cold start this pid is the browser
                        # process itself, so the sweep can find its
                        # window even if it never takes the foreground
                        # (see _find_window_of_pid); on a handoff it
                        # exits at once and the foreground sweep is
                        # what finds the window.
                        _music["pid"] = proc.pid
                        _music_pids.add(proc.pid)
                    else:
                        ctypes.windll.shell32.ShellExecuteW(
                            None, "open", url, None, None, 7)
            else:
                webbrowser.open(url)
        except Exception:
            logs.exception("could not open the reading-music URL")

    threading.Thread(target=launch, name="music-launch", daemon=True).start()

    _music["key"] = key
    _music["hwnd"] = None       # filled in by the sweep below
    _music["pid"] = None        # filled in by the launch thread
    _music["settled"] = False
    _music["gesture_sent"] = False
    _music["pressed_at"] = None
    _hush_browser_window(window, was_foreground)
    # Fallback only - see the docstring above, and they stand down the
    # moment the sweep has identified the window: from then on the tuck
    # owns the refront, and it may still be *waiting* (the keystroke
    # gets a visible runway, _MUSIC_POST_PRESS_VISIBLE_S) - a blind
    # refront here would cover the window mid-runway and pause the
    # playback the press just started.
    QTimer.singleShot(_MUSIC_AUTOPLAY_GRACE_MS + 200,
                      lambda: _fallback_refront(window))
    QTimer.singleShot(_MUSIC_AUTOPLAY_GRACE_MS + 1200,
                      lambda: _fallback_refront(window))


def _music_key(entry):
    """What "the same title" means for the music. The entry's id where it
    has one, its title otherwise - a card being edited mid-session must
    not read as a different manga and restart the music."""
    entry = entry or {}
    return str(entry.get("id") or (entry.get("title") or "").strip().lower())


def _cancel_music_stop():
    global _music_stop_timer
    if _music_stop_timer is not None:
        try:
            _music_stop_timer.stop()
        except RuntimeError:
            pass                # the timer's owner is already gone
        _music_stop_timer = None


def close_music_now():
    """Close the music window with no grace and no timers - for the one
    caller that has no event loop left to wait in: the whole app is
    closing (main.MainWindow.closeEvent). The owner, 24 August 2026:
    "when I close the whole app while I am reading in the reader mode,
    close the music URL as in the back btn!" - the back button's
    stop_music() ran fine, but quitting the app skipped it and left the
    music playing in the browser. Synchronous on purpose: the 900ms
    forced-close follow-up (see stop_music) rides a QTimer that will
    never fire during shutdown, so this asks once politely and then
    forces it in the same breath."""
    hwnd = _music.get("hwnd")
    _cancel_music_stop()
    _music["hwnd"] = None
    _music["key"] = None
    _music["pid"] = None
    if not hwnd or os.name != "nt":
        return
    try:
        import ctypes
        user32 = ctypes.windll.user32
        if user32.IsWindow(hwnd):
            user32.PostMessageW(hwnd, 0x0010, 0, 0)     # WM_CLOSE
        _force_close_music(hwnd)
    except Exception:
        logs.exception("could not close the music window at app exit")


def stop_music(delay_ms: int = 0):
    """Close the music window this app opened, if it opened one.

    `delay_ms` gives a reader that is only being *replaced* - the details
    chapter list tears one down and builds the next - a moment to say so
    by opening again for the same title, which cancels this.

    Never raises, and never closes a window this app did not open: only
    the handle the sweep recorded for its own launch is touched, and a
    handle that has already gone is simply forgotten."""
    global _music_stop_timer
    if delay_ms > 0:
        _cancel_music_stop()
        _music_stop_timer = QTimer()
        _music_stop_timer.setSingleShot(True)
        _music_stop_timer.timeout.connect(lambda: stop_music(0))
        _music_stop_timer.start(delay_ms)
        return
    _cancel_music_stop()
    hwnd, _music["hwnd"], _music["key"] = _music["hwnd"], None, None
    _music["pid"] = None
    _music["settled"] = False
    _music["gesture_sent"] = False
    _music["pressed_at"] = None
    if not hwnd or os.name != "nt":
        return
    try:
        import ctypes
        user32 = ctypes.windll.user32
        if user32.IsWindow(hwnd):
            # PostMessage, not a process kill: this asks the window to
            # close the way its own X button does, so the browser saves
            # its session and the owner's other windows are untouched.
            user32.PostMessageW(hwnd, 0x0010, 0, 0)     # WM_CLOSE
            # ...but a close request is not a close. Measured 23 August
            # 2026: a music window holding two tabs answered WM_CLOSE
            # with Brave's "Close all tabs?" modal, sat *disabled*
            # behind it, and swallowed every further WM_CLOSE and
            # SC_CLOSE whole - music playing invisibly forever. (Two
            # tabs is what a handoff launch into the still-running
            # music-profile process produced, twice in two runs; and
            # seeding the profile preference that disables the modal
            # did not survive Brave rewriting the file.) So the close
            # is followed up - see _force_close_music.
            QTimer.singleShot(900, lambda: _force_close_music(hwnd))
    except Exception:
        logs.exception("could not close the reading-music window")


def _force_close_music(hwnd, attempt=0):
    """Finish closing a music window that ignored stop_music's WM_CLOSE.

    Two stages, 1.5s apart. First: if the window is disabled behind an
    enabled popup of its own - Brave's "Close all tabs?" - post Enter to
    accept the dialog's default, which is "Close all", the very thing
    the WM_CLOSE asked for. Cheap, but measured unreliable: it closed
    one such dialog and was swallowed by the next (23 August 2026,
    posted key events only reach a Chromium dialog's button when the
    dialog happens to hold focus). Second: if the window is *still*
    alive, terminate its process - but only when its pid is one this
    app itself Popen'd for music (_music_pids), meaning this launch
    cold-started the browser; a pid not in the set (a handoff into the
    owner's already-running browser, a sweep mis-latch onto one of his
    own windows) is left alone, which is exactly the old behaviour.
    Together with the other-windows check below, a terminate can only
    ever hit a browser process this app started whose sole remaining
    window is the stuck music window - at worst that costs a "restore
    pages?" bubble on the owner's next browser start, against music
    that would otherwise play invisibly forever. Never raises."""
    try:
        import ctypes
        user32 = ctypes.windll.user32
        if not user32.IsWindow(hwnd):
            return          # closed - done
        if not user32.IsWindowEnabled(hwnd):
            popup = user32.GetWindow(hwnd, 6)       # GW_ENABLEDPOPUP
            if popup:
                user32.PostMessageW(popup, 0x0100, 0x0D, 0)     # VK_RETURN down
                user32.PostMessageW(popup, 0x0101, 0x0D, 0)     # VK_RETURN up
        if attempt == 0:
            QTimer.singleShot(1500, lambda: _force_close_music(hwnd, 1))
            return
        from ctypes import wintypes
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value or pid.value not in _music_pids:
            return
        # Never end a process that still owns another window: swapping
        # titles closes A and opens B, and a handoff can put B's window
        # in the very process A's stuck window belongs to. B survives;
        # A's window stays stuck, which is the lesser wrong.
        if _find_window_of_pid(pid.value, exclude=hwnd):
            return
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x0001, False, pid.value)  # PROCESS_TERMINATE
        if handle:
            try:
                kernel32.TerminateProcess(handle, 0)
            finally:
                kernel32.CloseHandle(handle)
    except Exception:
        pass        # a window that died in the meantime is not a problem


def open_reader(window, entry, data_file="tracker.json", resume=True,
                chapter_number=None):
    """Open the reader over `window`, covering the sidebar as well.

    `resume=False` lands on the chapter list instead of jumping back to
    where reading stopped. That is the difference between the two targets
    a reading card now has: the round continue button on its cover
    resumes, the rest of the card browses.

    The one entry point, and the one thing main.py needs to call. It is
    deliberately not a member of main.PAGES: this is not a section of the
    app with a sidebar row, it is a surface opened on top of one and
    closed back out of.

    The host is the window's **central widget**, not main.py's page
    container - that one line is the whole of "the sidebar disappears
    while reading". player.py already does exactly this for the same
    reason; nothing in main.py hides or restores anything, so there is
    no state to get stuck in the hidden half."""
    # The whole window, the app's own bar included - see
    # main.immersive_host. This surface is the content, not a page
    # showing it, and it carries its own bar and its own way out.
    host = (window.immersive_host() if hasattr(window, "immersive_host")
            else (window.centralWidget() if hasattr(window, "centralWidget")
                  else window))
    host = host if host is not None else getattr(window, "container", window)
    # Opening is what Read History records - the owner opened chapters
    # and found the section empty, because the only writer was the
    # mark-as-read path. The chapter tick rides along when one was asked
    # for by number; resuming records the title and lets the reader's
    # own marking tick the chapter it lands on.
    try:
        shown = (f"Ch {float(chapter_number):g}"
                 if chapter_number is not None else None)
        history.touch(entry, progress=shown)
        if chapter_number is not None:
            history.set_watched(entry, history.chapter_key(chapter_number), True)
    except Exception:
        logs.exception("could not record the reading history")
    # The music site opens with the reader, exactly as the browser route
    # opens it beside the reading tab (tracker._open_manga_entry) - but
    # quietly, behind the app, since the reading surface is *this* page.
    _open_music_quietly(window, entry)
    page = ReaderPage(entry, data_file, host,
                      origin_page=_origin_page_name(window), resume=resume,
                      chapter_number=chapter_number)
    page.follow(host)
    page.show()
    page.raise_()
    # The sidebar and the page container are covered, not hidden, so
    # without this they keep repainting behind an opaque overlay -
    # measured at 67,090 paint events in 3.6s of scrolling, and the
    # reason scrolling up ran slower than scrolling down. See
    # widgets._CoveredFreeze for the numbers.
    freeze_covered(page)
    page.setFocus()
    return page
