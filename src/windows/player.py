"""The in-app video player: one full-window page over the whole app.

What is here and what is deliberately elsewhere:

  * `helpers/video_backend.py` owns libmpv - loading it, the option set,
    and tearing an instance down. This module never imports `mpv`.
  * `helpers/streams.py` finds something playable for an entry, and
    `helpers/subtitles.py` finds and decodes subtitle files. Both are
    imported softly (see below): a build without them still opens this
    page and still plays a url it was handed.

Three things about embedding mpv that are not obvious and each cost a
session's worth of debugging elsewhere:

  * **The video widget must be a real native window.** mpv is handed an
    HWND and renders into it directly. `WA_NativeWindow` has to be set
    (with `WA_DontCreateNativeAncestors`, or Qt promotes every ancestor
    up to the window and the page's own painting changes underneath us)
    and `winId()` read *after* that, or mpv gets a handle Qt later
    replaces and the video opens in a detached window of its own.
  * **A native child window is composited by Windows on top of
    everything Qt paints into the top-level.** Ordinary sibling widgets
    raised over the video are simply not visible - the overlay controls
    have to be native windows too, and their stacking then follows
    `raise_()`.
  * **Qt cannot alpha-blend those overlays, but Windows can.** This is
    what the bars being an opaque slab used to be about, and it was only
    half true: Qt's own translucency (WA_TranslucentBackground, an alpha
    in the QSS colour) is composited inside the top-level's surface and
    so blends against Qt's backdrop, not against mpv's separately
    composited swapchain. What *does* work is DWM's own per-window
    blend - `WS_EX_LAYERED` plus `SetLayeredWindowAttributes`, supported
    on child windows since Windows 8. DWM composites the layered child
    over whatever is already in the frame at that point, video included.
    See `_set_window_alpha`. Measured rather than assumed: with a frame
    on screen, sampling one screen pixel inside the controls bar with the
    layer on and again with it off gave two different colours, which is
    only possible if something behind the bar is reaching the pixel.
  * **mpv's callbacks arrive on mpv's own thread.** Every one of them
    does nothing but emit a signal on `_MpvBridge`; touching a widget
    from there is a hard crash, exactly as it is from the tracker's
    lookup threads.
  * **mpv also eats every mouse event over the video**, not just the
    moves the pointer poll already works around: its child window is not
    Qt's, so no press ever reaches a Qt widget. Click-to-pause is
    therefore polled from the same place and in the same way - see
    `_poll_mouse`.
"""

import ctypes
import inspect
import os
import re
import shutil
import tempfile
import threading
import time
import uuid

from PyQt6.QtCore import (QEvent, QObject, QPoint, QPointF, QRect, QRectF, Qt,
                          QTimer)
from PyQt6.QtCore import pyqtSignal as Signal
from PyQt6.QtGui import (QColor, QCursor, QFont, QFontMetrics,
                         QLinearGradient, QPainter, QPen, QPixmap,
                         QPolygonF, QRegion)
from PyQt6.QtWidgets import (
    QApplication, QComboBox, QFrame, QHBoxLayout, QLabel, QLineEdit, QMenu,
    QPushButton, QSizePolicy, QSlider, QVBoxLayout, QWidget,
)

from helpers import (skiptimes, app_settings, artwork, downloads, logs, net, storage,
                     theme, video_backend)
from helpers.widgets import (Card, GlassPage, GlyphButton, LogoProgress,
                             PickCombo,
                             confirm, finish_toast, freeze_covered,
                             scroll_area, show_toast,
                             use_hover_cursor)

# Soft imports. These two modules are written alongside this page and a
# tree without them must still import `windows.player` - main.py imports
# every page at startup, so an ImportError here would be a black app
# rather than a missing feature.
try:
    from helpers import streams as streams_module
except ImportError:                                     # pragma: no cover
    streams_module = None
try:
    from helpers import subtitles as subtitles_module
except ImportError:                                     # pragma: no cover
    subtitles_module = None
try:
    from helpers import ai_translate
except ImportError:                                     # pragma: no cover
    ai_translate = None


# Where a resume point lives. Its own file, not tracker.json: this is
# written every few seconds while something plays, and tracker.json is
# read-modify-written by four lookup workers at the same time.
RESUME_FILE = "player_state.json"

# How long the controls linger after the last pointer movement before
# hiding. 0.5s at the owner's request (3.2s -> 1.5s -> 500ms; each step
# was still "too slow to clear"). This is safe to make this short only
# because of the guard in _hide_controls: the pointer resting anywhere on
# the bar re-arms the timer instead of hiding, so a half-second delay
# never pulls a button out from under the hand reaching for it. The
# pointer poll runs every 180ms, so the bar is back within a tick of the
# mouse moving again.
CONTROLS_HIDE_MS = 500
# The pointer is polled rather than watched for MouseMove events: mpv
# creates its own child window inside ours and eats the moves that
# happen over the video, so the only reliable "the user is still there"
# signal is the global cursor position changing. Same approach as
# main.py's cursor watchdog, and no coordinate mapping is involved.
POINTER_POLL_MS = 180
# The mouse *button* is polled far faster than the pointer, for the same
# reason and by the same route: a click that goes down and up between two
# 180ms ticks would simply not exist. A real click measures 50-100ms, so
# 40ms sees the button held on at least one tick; the "pressed since the
# last call" bit of GetAsyncKeyState catches the ones shorter than that.
MOUSE_POLL_MS = 40
# A press and release this far apart is a drag (or a slipped hand), not a
# click, and must not toggle playback.
CLICK_MOVE_TOLERANCE_PX = 6
POSITION_SAVE_MS = 5000

SEEK_STEP_S = 5
VOLUME_STEP = 5
# 100% sits at the centre of the slider and the track boosts to 200%.
# mpv's `volume` property accepts 0-200 (soft gain above 100), but only
# once `volume-max` is lifted from its default 130 - done in _start.
VOLUME_MAX = 200
VOLUME_DEFAULT = 100
# The speed panel: mpv's range clamped to something watchable, the
# preset stops matching the owner's sketch. The old click-to-cycle ring
# (SPEEDS) is gone with the cycling itself.
SPEED_MIN, SPEED_MAX = 0.25, 4.0
SPEED_PRESETS = (0.25, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 4.0)

# A press-release-press-release faster than this over the video is a
# double-click (fullscreen), not two single clicks (two pause toggles).
# Windows' own default double-click time is ~500ms; 380ms is a touch
# tighter so a deliberate slow "pause, then pause again" is not eaten.
DOUBLE_CLICK_S = 0.38
# **And within this far of the first click**, which is the half Windows
# itself checks and this did not. The owner, 25 August 2026:
# "sometimes clicking on the vid player screen takes more than one click
# to resume/pause (no windows open!)". Clicking to pause and clicking
# again a moment later to resume is an ordinary thing to do, and with
# time as the only test the second one was read as a double-click: the
# pause was *restored* to where it had been before the first click, so
# the picture carried on exactly as if neither click had happened.
# Windows' own rule is GetDoubleClickTime AND SM_CXDOUBLECLK, and a real
# double-click does not travel - the system default is 4px either way,
# and this is generous against a 2560-wide picture.
DOUBLE_CLICK_PX = 24

# Below this there is nothing worth resuming from, and above it the
# episode is effectively finished - offering "resume at 99%" is worse
# than starting over.
RESUME_MIN_S = 30
RESUME_MAX_FRACTION = 0.97

# How close playback has to get to a pending seek's target before the
# seek counts as *landed* and "Resumed From 9:23" is allowed on screen.
# One second, not zero: mpv's exact seek reports the frame it settled
# on, which is at or just past the request, and a strict >= would hang
# the announcement on a rounding difference. It is deliberately not
# generous either - the whole point is that the message follows the
# picture rather than the intention (see _load_into_mpv).
RESUME_LANDED_TOLERANCE_S = 1.0

# How long a seat (a resume point, or the position carried across a
# source switch) may stay un-landed before the player stops promising it
# and says so - the owner's "the Skipping to X:XX ... keeps loading but
# never takes me there", 22 August 2026, where the wait had no end and
# no message. 45s: the stream layer's own waits are 12s each, and the
# owner's worst observed resume-to-picture wait was ~30s (his earlier
# "it takes ~10-30 sec then the vid plays"), so anything past 45 is a
# seat that is not coming and narrating it further is a lie. Not
# measured against a genuinely dead swarm - those cannot be fabricated
# on demand - but every live landing measured (three real titles,
# 22 August 2026) came in far under it.
SEAT_GIVE_UP_S = 45.0

# When an episode counts as watched. Credits and a next-episode teaser
# are routinely the last ~10%, so waiting for the file to actually end
# would mean most finished episodes never got marked.
WATCHED_FRACTION = 0.85

# Step of 2, not 4 - the owner's ask, 23 August 2026: one press of 4
# jumped past the size he wanted. The buttons print the step
# (add_stepper's step_text), so nothing else encodes this number.
SUB_SIZE_MIN, SUB_SIZE_MAX, SUB_SIZE_STEP = 28, 90, 2
SUB_SIZE_DEFAULT = 55          # mpv's own default for sub-font-size
SUB_DELAY_STEP = 0.1

# Hold-to-repeat on the +/- steppers. See _stepper_button: a tenth of a
# second per click is unusable for a release seconds out of sync.
STEPPER_REPEAT_DELAY_MS = 350
STEPPER_REPEAT_MS = 120

# Where mpv puts the subtitle line, as a percentage of frame height
# (100 = hard against the bottom, which is mpv's default). The controls
# are a faint veil now, not a slab, but a subtitle *behind* them is
# still unreadable - two layers of text over each other is worse than
# one over video - so the line still steps up out of the way while they
# show. At CONTROLS_HEIGHT 96, 96px of an 800px window is 12%, so 87
# clears it with a little air.
SUB_POS_CLEAR = 100
SUB_POS_ABOVE_CONTROLS = 87

# The owner's own adjustment, added to whichever of the two above is in
# force (see _apply_sub_position). Negative lifts the line off the
# bottom; the pair compose, so subtitles nudged up by hand still step
# further up when the controls appear rather than jumping back down.
#
# -60 is as far as is useful: it puts the line at 40% of frame height,
# which is above the middle of the picture. +10 exists because a release
# with burned-in credits along the very bottom is the one case for
# pushing *down*, and mpv clamps at 100 anyway.
SUB_POS_OFFSET_MIN, SUB_POS_OFFSET_MAX, SUB_POS_OFFSET_STEP = -60, 10, 2

# ---------------------------------------------------------------------
# Skipping the opening, the ending and a recap
#
# Three sources feed this, and they are different kinds of answer rather
# than one with backups (see helpers/skiptimes.py for the other two):
#
#   * the release's **own chapter markers**, read straight out of mpv -
#     free, instant, offline, and exactly right when a fansub included
#     them;
#   * **AniSkip**, keyed by MyAnimeList id - verified live, Demon Slayer
#     ep5 answers op 44.2-134.2s and ed 1290.6-1380.6s;
#   * **TheIntroDB**, wired but unreachable from here (no DNS).
#
# A chapter whose title contains one of these is that kind of chapter.
# Matched on whole words where the word is short ("op", "ed"), or the
# episode's own "Episode" chapter would match "ed".
# "titles"/"theme" are here for **live action**, which AniSkip cannot
# help with at all: it is keyed by MyAnimeList id, and a series like
# House of the Dragon has none (measured - `mal_id` answers None), so a
# release's own chapter markers are the only source it has. Those
# releases name the title sequence "Main Titles", "Title Sequence" or
# "Opening Titles" rather than "OP", and none of those matched.
# **"teaser" is deliberately not here.** A teaser is the scene *before*
# the title sequence - the cold open - so naming it the opening offers to
# skip the first minutes of the story. It was added with the live-action
# words above, where none of the real examples ("Main Titles", "Title
# Sequence", "Opening Titles") needed it. This is half of the owner's
# "it is not accurate at all, you must keep in mind that not all intros
# start from 0:00"; the span check below is the other half.
#
# **"avant" is gone for the same reason, 23 August 2026.** It is the
# Japanese BD chapter name for the avant-title - the cold open *before*
# the OP - and disc rips are chaptered Avant / OP / Part A / Part B / ED.
# With it here, Avant@0:00 and OP@1:30 both classified as openings, so
# "Skip Intro" showed at 0:00, pressing it seeked to 1:30 - the start of
# the real opening - where the second interval showed "Skip Intro" again.
# That is the owner's report, word for word, and it reproduced exactly
# from the unmodified functions. The general form of the same mistake
# (an "Intro" chapter at 0:00 followed by "Opening") is handled in
# _skips_from_chapters, which keeps only the last of two touching
# openings.
_CHAPTER_OPENING = ("opening", "op", "intro", "titles", "title", "theme")
_CHAPTER_ENDING = ("ending", "ed", "outro", "credits", "closing")
# **"prologue" is gone, 24 August 2026 - the third time this exact
# mistake has been paid for.** "teaser" and then "avant" were removed
# for the same reason and it is the same reason again: a prologue is the
# cold open, which is the *story*, not a summary of an earlier episode.
# BD and fansub releases chapter it Prologue / OP / Part A / Part B / ED,
# so with the word here the first chapter of an episode classified as a
# recap and the button said "Skip Recap" over the opening scene. That is
# the owner's "I tested it in AOT 1st ep 1st season and it was showing
# skip recap while it was not a recap". The general rule this keeps
# proving: only words that mean *previously-seen footage* belong here.
_CHAPTER_RECAP = ("recap", "previously")
# A chapter marker is only believed as an opening if it starts within
# this much of the episode - a "Part 2" chapter at 15 minutes is not an
# opening however it is named. Measured 22 August 2026 over 18 real
# AniSkip openings across eight of the owner's titles: median start
# 84.5s, and 17 of 18 under 192s (the outlier is Demon Slayer's
# double-length premiere, whose OP is filed at 1270s). Left at 600
# rather than tightened to match, because the span check below is the
# lever that was actually missing and changing two thresholds at once
# would leave neither measured.
CHAPTER_OPENING_WINDOW_S = 600.0
# **How long an opening or an ending actually runs.** A chapter interval
# ends where the *next chapter starts*, which for a release that marks
# whole acts is minutes away - and the Skip button seeks to that end, so
# an 8-minute "opening" jumps 8 minutes into the story. That is the rest
# of "not accurate at all", and nothing checked it: `skiptimes._clean`
# holds crowd answers to 3-150s, but chapters never went through it.
#
# Measured the same day, same 18 openings and 20 endings: openings run
# 60.0-91.5s (median 90.0), endings 69.8-114.3s (median 90.0). The floor
# is set at 30 rather than 60 because that sample is anime-only and a
# live-action main title can legitimately be half the length; anything
# under 30s is a logo card, which is not worth a button. The ceiling is
# skiptimes.MAX_SPAN_S, comfortably above the longest measured.
CHAPTER_SPAN_MIN_S = 30.0
CHAPTER_SPAN_MAX_S = 150.0
# **A recap is bounded too now.** It used to be exempt from the span
# check entirely, on the reasoning that recaps genuinely vary
# (measured 17.4-90.0s) and that landing on the opening is harmless.
# What that exemption actually allowed is the failure above: a first
# chapter running to the first act break - LOST S06E01's is 0.00 ->
# 434.06s, measured - classified as a recap and offering to throw the
# viewer seven minutes in. The floor is lower than an opening's because
# a real "previously on" can be under half a minute; the ceiling is
# skiptimes.MAX_SPAN_S like everything else.
CHAPTER_RECAP_SPAN_MIN_S = 8.0
CHAPTER_RECAP_SPAN_MAX_S = 150.0
# Below this, a chapter's claim to be the opening loses to AniSkip's -
# see _on_skips. Deliberately near zero rather than at the measured
# minimum start of 27.4s: this only decides which of two sources to
# believe, and a real opening that genuinely does begin in the first few
# seconds should not be thrown away on a distribution's say-so.
CHAPTER_OPENING_MIN_START_S = 5.0

# The button sits above the controls bar, right-aligned - out of the
# subtitles' way and where a remote's "skip" lives on every streaming
# app. Hidden the instant playback leaves the interval.
# How tall the Subtitles panel's two scrolling columns are - enough for
# ~7 rows; the settings column beside them is three steppers and sets
# the panel's natural height.
SUBS_PANEL_COLUMN_H = 380

SKIP_BUTTON_SIZE = (168, 44)
SKIP_BUTTON_MARGIN = 28
# How long before the end of the file "Next Episode" appears even with no
# ending interval known - most releases run credits over the last minute.
NEXT_TAIL_S = 60.0
# How far into an episode the *next* episode's top release may be
# created - see _maybe_prewarm_next. This used to be a tail window
# (last 180s of the episode), on the reasoning that only the credits
# were safe; the owner's ask, 22 August 2026, is the opposite end:
# "make the next ep loads after ~100 sec from the current ep started
# playing so that when I go to next it goes smoothly". The judgement
# call, made deliberately: warming this early means the next torrent
# runs beside the playing one for most of the episode, and what makes
# that safe is not this number but the same safety condition the tail
# window had - nothing is warmed until the playing file is *complete*
# (torrent_engine gives it priority 7, so on a live swarm it usually is
# well before 100s), at which point playback reads from disk and the
# connection is idle. An incomplete file keeps the warm waiting, however
# far past 100s the episode is.
PREWARM_AFTER_S = 100.0

# 2x the per-request timeout the site modules use, for the same reason
# they bound a chain rather than each step: several sources tried in
# sequence is not "one timeout".
STREAM_BUDGET_S = 24
SUBTITLE_BUDGET_S = 24

# How a subtitle is named in the list: "<Language> <n> <Provider>" -
# "Arabic 1 OpenSubtitles", "Arabic 2 SubDL", "English 1 SubtitleCat"
# (the owner's ask). The release name it used to show was the raw file
# stem - group tags, resolution and hash - which said nothing about what
# picking it would give you, and two entries from one provider were
# routinely indistinguishable. The count restarts per language *and*
# provider, so the numbering answers "the second Arabic one from SubDL".
SUBTITLE_LANGUAGE_NAMES = {
    "ar": "Arabic", "ara": "Arabic", "en": "English", "eng": "English",
    "fr": "French", "es": "Spanish", "de": "German", "ja": "Japanese",
    "jp": "Japanese", "ko": "Korean", "zh": "Chinese", "tr": "Turkish",
    "ru": "Russian", "pt": "Portuguese", "it": "Italian", "nl": "Dutch",
    "id": "Indonesian", "hi": "Hindi", "fa": "Persian", "he": "Hebrew",
}

# The controls hold two rows now - the seek strip with its end times on
# top, the buttons under it (the owner's reference layout) - so the
# height is both rows plus margins: 22 + 48 + 8 + 10 + 8 spacing = 96.
# Top: 38px icon buttons + 4 + 4 = 46, so 50 leaves 4.
CONTROLS_HEIGHT = 96
TOPBAR_HEIGHT = 50
PANEL_WIDTH = 460
PANEL_MAX_HEIGHT = 520

# How far the overlay bars' corners are rounded, and the one place the
# number lives: the QSS fill and the window mask that cuts the native
# window to the same shape have to agree, or a hard square corner shows
# through the rounded fill (see _round_overlay).
BAR_RADIUS = 16

# The episode list, down the left. Widened three times at the owner's
# request (250 -> 320 -> 360 -> 400): the rows carry the episode's own
# name, its date and a badge now, like the details page's list, and 360
# elided most names.
EPISODE_BAR_WIDTH = 400

# How many rows to show for a season the entry carries no length for -
# the ones the ‹ › nav reaches that latest_available does not name. A
# guess, deliberately conservative: a row that resolves to nothing is a
# soft "no source", where omitting a real episode is a dead end.
DEFAULT_SEASON_EPISODES = 12

# How see-through the panels are, 0-255. Pushed glassier at the owner's
# request (was 205 / ~80%). 180 is ~70%: a bright frame reads clearly
# through the panel now, and legibility is held up not by opacity but by
# the fill being pushed down toward near-black (see _bar_style) - a
# dark scrim under white text survives a lower alpha where the old mid
# SURFACE fill would have washed out. SetLayeredWindowAttributes applies
# one uniform alpha to the whole window, text included, so the scrim, not
# the alpha, is what has to carry the contrast.
BAR_ALPHA = 180
# The two full-width bars sit under this fainter veil - a soft
# darkening behind the buttons rather than a slab (the owner's
# reference picture). The top bar wore no veil at all for a while (its
# background was colour-keyed to nothing); that ended when the key
# failed on the owner's display and rendered as black boxes around the
# title and buttons - see _build_top_bar.
# **255 - the bars are opaque now.** They carried a 130/255 DWM veil so
# the picture read through them as glass, and the veil dims *everything*
# in the window uniformly: a native child window has one alpha for all
# its pixels (per-pixel alpha is not available to a child; the colour-key
# route was tried and abandoned - see _hard_edge_font), so the buttons,
# the glyphs and the time labels were all at half strength. That is the
# owner's "make the player UI buttons not transparent, only make the
# frame behind them as is (Like stremio)". The glass look is kept by
# painting the *frame* pre-blended instead (_bar_style): the fill is
# darkened by the same 130/255 the veil used to apply, so over the
# letterbox black the bars sit on the frame reads as it always did,
# while everything drawn on it is at full strength - which is exactly
# what Stremio's bar is.
# **230 - "a bit transparent", the owner's own words on seeing 255.**
# The full history in one line each: 130 made every button and label
# half-strength (uniform DWM alpha dims the whole native window, and
# per-pixel alpha is not available to a child window); 255 made the
# buttons right and the frame a slab he then asked to see through
# again. 230 is the split: buttons at ~90% read as solid, the frame
# genuinely shows the picture behind it. The fill colours went back to
# the theme's own the moment the heavy veil left - the pre-darkening
# existed only to fake 130 on an opaque window.
# 200, down from 230 - the owner on the 230 build: "the ui looks
# perfect ... make JUST the frame a bit more transparent". Buttons at
# ~78% still read solid on the near-black fill; below ~180 they start
# going grey again, which is where this journey began.
CONTROLS_VEIL_ALPHA = 200
# What the panels' scroll viewport paints as its ground - matched to
# the bar gradient's body so rows scroll over a solid, blit-friendly
# fill instead of transparency (see OverlayPanel).
_VEIL_FACTOR = 200 / 255

_GWL_EXSTYLE = -20
_WS_EX_LAYERED = 0x00080000
_LWA_ALPHA = 0x00000002
_VK_LBUTTON = 0x01

# How long an episode *switch* may wait before the loading frame is
# shown at all - see _show_loading_soon. Under this, a season pack
# that is already downloading hands over the next episode's first
# frame with nothing flashing in between.
LOADING_FLASH_GUARD_MS = 350

# And how long a *mid-playback* stall may last before the same frame goes
# up over the picture - the owner's ask, 22 August 2026: "show the
# loading image while buffering". Longer than the guard above on
# purpose, and the two are not the same decision: that one covers a
# window with nothing in it yet, this one covers a picture that is
# already there, so being wrong costs a black frame over content the
# user was watching. mpv's `paused-for-cache` flips true for stalls of
# every size, including ones that resolve in a few hundred milliseconds,
# and those must show nothing at all. **Not measured against a live
# stall** - there is no way to provoke a real one from a harness here -
# so it is set from the cost of being wrong rather than from a number.
BUFFER_FRAME_GUARD_MS = 700

# The startup gauge's drift: how often it recomputes, how much one silent
# tick may add (0.005 per 250ms = 2% a second), and how far past
# *confirmed* progress the drift may run before it parks and pulses.
# Measured before the ticker existed: across a real 14.7s episode start
# the logo's fraction was written exactly twice - 0.04 when the logo art
# landed, 1.0 at the same instant as the first frame - because mpv's
# cache-buffering-state, the gauge's only input, reports an engine URL
# exactly once, already at 100. Every earlier phase has real numbers of
# its own (_startup_snapshot); the drift only covers the gaps between
# them, and the headroom is what stops it promising a finish the swarm
# has not delivered.
STARTUP_TICK_MS = 250
STARTUP_CREEP = 0.005
STARTUP_HEADROOM = 0.08

# How long after a panel opens, closes or rebuilds the click-to-pause
# poll ignores clicks - see _click_toggle. The poll samples the mouse
# up to 40ms late, and a panel that changed shape under a click (the
# resolution drill-down replacing a tall list with a short one) made
# the *same press* that chose a row read as a click on the video
# behind where the row used to be, which dismissed the drill-down the
# press had just opened (the owner's "clicking 4K closes the window").
#
# **This was 0.45s and that is what "I need to click 2 times on the
# screen to close it" was.** A wall-clock window cannot tell the press
# that caused the change from the next, genuine one, so every dismissal
# inside 450ms of the panel opening - or of any *rebuild*, and late
# sources, a track-list update and every Download row all rebuild - was
# swallowed in silence. Measured 22 August 2026 on the real widget with
# real mouse input: a dismiss click 200ms after a rebuild closed the
# panel 0 times out of 4; the same click with no rebuild, 3 of 4. The
# press the change belongs to is now identified rather than timed
# (`_guard_click`), so this is only the backstop for the one case an id
# cannot name - a click so fast that the 40ms poll never saw it go down,
# which can only surface on the very next tick.
PANEL_CLICK_GUARD_S = 0.12

# Segoe Fluent Icons codepoints, same family the sidebar uses (see
# theme.FONT_STACK_ICONS) - monochrome, so they take the button's colour
# instead of an emoji's own.
#
# Every one is written as a \u escape rather than pasted. That is not
# style: these are private-use codepoints, so a pasted one is invisible
# in a diff - which is how a wrong glyph gets reviewed as correct - and
# any tool that re-encodes this file turns it into mojibake. reader.py
# carries the same note, from the same cause.
# Only the episode list's "this one is playing" marker now. The big
# transport disc draws its own play triangle and pause bars (see
# PlayPauseButton) rather than setting a glyph as text: E769, the Fluent
# Pause, spaces its two bars far enough apart that the owner read them as
# two separate marks rather than one control.
ICON_PLAY = "\ue768"
ICON_PREV = "\ue892"
ICON_NEXT = "\ue893"
ICON_VOLUME = "\ue767"
ICON_MUTED = "\ue74f"
ICON_FULLSCREEN = "\ue740"
ICON_EXIT_FULLSCREEN = "\ue73f"
ICON_SUBTITLES = "\uf2b7"   # Translate - this opens the
                            # subtitle search, including the AI
                            # translation rows (the owner's ask).
# Leaving the player, at the far left of the top bar. E72B is Back -
# the plain left arrow, the same glyph the details page's back button
# carries (the owner's ask: a normal back arrow, not E892's
# previous-track arrow, which repeated the transport's previous-episode
# glyph two rows apart and read as the same control twice).
# reader.py's leave button carries the identical glyph, so every
# overlay opens with the same way out.
# The way out of the player. The same single chevron the sidebar folds
# with (main.FOLD_CLOSE_ICON), at the owner's ask: leaving and folding
# both mean "collapse this back to where it came from", and the reader
# and the details page's back button now carry it too, so one shape
# means "back" everywhere.
ICON_EXIT = "\ue76b"
# The episode-list opener, immediately right of the door. E8FD is
# BulletedList, the same glyph the reader's chapter-list button carries.
ICON_EPISODE_LIST = "\ue8fd"
# Marks an episode already watched in the episode list. E73E is
# CheckMark - the tick alone, not the boxed E73A, which would read as a
# checkbox waiting to be clicked rather than a state already reached.
ICON_WATCHED = "\ue73e"
ICON_DOWNLOAD = "\ue896"
# A speaker with waves (Fluent Volume3) for the embedded-tracks button.
# It was E8D6, the sound-bars glyph, which the owner read as a *music*
# mark rather than "the audio and subtitle tracks inside this file" -
# a speaker is the symbol asked for. Distinct from the mute button's
# E767 so the two controls never read as the same one twice.
ICON_EMBEDDED = "\ue9d9"    # Audio wave - the owner asked for a
                            # pulse: this button opens the release's
                            # own audio and subtitle tracks, so an
                            # audio mark says more than a translate
                            # one (which now marks the subtitle
                            # search below).
# The globe, for the settings button - the owner's ask. E774 is the
# same glyph the reader's open-in-browser button carries.
ICON_SETTINGS_GLOBE = "\ue774"

# WiFi bars - the owner asked for this button by its icon (24 August
# 2026, with a screenshot of the numbers it should open). An escape
# rather than the bare character, for the reason every glyph in this
# file is one: a re-encoding tool turns them into mojibake.
ICON_STATS = "\ue701"

# Two buttons came off the control bar at the owner's request and their
# glyphs went with them: the globe (E774), which opened a little menu of
# Embedded Translation and Sources, and the screen (E7F4), which opened
# the resolution drill-down. Embedded Translation is a row in the
# settings panel now, and the resolution drill-down was already on the
# top bar pill and in Settings - each button was a second door onto a
# room that already had one, which is what made the row crowded.


# A stepper's two buttons. Minus and plus, not the ‹ › arrows they were:
# the owner was explicit that the arrows read as "scroll through a list"
# rather than "make this number smaller / larger", which is what these
# actually do. U+2212 (a real minus, wider and centred) over the ASCII
# hyphen, and both are plain text-font glyphs so they sit on the value's
# own baseline where a Fluent glyph would not.
GLYPH_MINUS = "−"
GLYPH_PLUS = "+"
# A right chevron for a drill-down row (the resolution list). Text-font
# U+203A rather than a Fluent chevron for the baseline reason above.
GLYPH_CHEVRON = "›"
# The season-nav pair beside the episode panel's "Season N" heading.
GLYPH_CHEVRON_LEFT = "‹"
GLYPH_CHEVRON_RIGHT = "›"


# ---------------------------------------------------------------------
# Small helpers


def _canonical_quality(quality) -> str:
    """"4k" and "2160p" name one resolution and must not read as two."""
    quality = str(quality or "").lower()
    return "2160p" if quality == "4k" else quality


def _prime_kwargs(function, seat):
    """Tell `streams.prepare`/`prepare_fastest` that playback is going
    to start part-way in, if this build's copy of them can hear it.

    This is the player's half of the owner's "when the alert 'resumed
    from 9:23' appears it takes ~10-30 sec then the vid plays". The seat
    is known here, seconds before mpv asks to seek to it, and it is
    useless unless the engine gets it in time to prioritise the pieces
    at that offset instead of the head of the file. `start_at` and
    `duration` (both seconds, both straight off the resume record) are
    what streams.py takes; a *fraction* is what it computes from them,
    so passing one without the other says nothing and both go together
    or neither does.

    Filtered against the real signature rather than assumed, because
    this landed while the engine side was still being written and a
    build without it must not raise. Never raises for any other reason
    either: a signature that cannot be read is one that does not take
    it. `seat` is PlayerPage._prime_seat's (seconds, total)."""
    start_at, duration = seat or (None, None)
    if not start_at or not duration:
        return {}
    try:
        params = inspect.signature(function).parameters
    except (TypeError, ValueError):
        return {}
    if "start_at" not in params or "duration" not in params:
        return {}
    return {"start_at": float(start_at), "duration": float(duration)}


def _format_time(seconds) -> str:
    if seconds is None:
        return "--:--"
    seconds = max(0, int(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def entry_identities(entry) -> list:
    """Every string this title's resume records could be filed under,
    best first.

    **A title played from Discover has no `id` at all**, and that was the
    owner's "when I played LOST series 1st ep it took me to 7:14!!!". An
    entry only gets a uuid when it is *saved* to the library
    (details.py mints one in `_save`; the Save button is shown exactly
    while `entry["id"]` is empty), so every unsaved title reached
    `_resume_key` with `entry_id=None` and every one of them filed under
    the same `"None|s1e1"`. Measured 22 August 2026 against the owner's
    own player_state.json: seven live records keyed `None|s1e1`,
    `None|s1e2`, `None|s3e2`... shared by every unsaved series he had
    opened, and a fresh LOST S01E01 loaded whatever the last of them
    held - 7:14 in his case, reproduced exactly in the harness.

    So identity falls back: the saved uuid, then the IMDb id (stable,
    global, and what every source here is keyed by), then the title
    itself. Several are returned rather than one because the IMDb id is
    resolved in the background (`details._on_resolved_id`) and can be
    absent on one visit and present on the next - saving under the best
    available and *reading* every candidate means a late resolve costs
    nothing, where a single key would silently lose the seat.

    An entry with none of the three gets an empty list, and nothing is
    saved or loaded for it - which is the honest answer, and is what
    stops "no identity" from becoming a shared bucket again."""
    entry = entry or {}
    identities = []
    saved = str(entry.get("id") or "").strip()
    if saved:
        identities.append(saved)
    imdb = str(entry.get("imdb_id") or "").strip().lower()
    if imdb:
        identities.append(f"imdb:{imdb}")
    # Punctuation and case only, deliberately: this is a fallback for a
    # title with no ids at all, and anything cleverer (stripping
    # subtitles, say) would start merging distinct shows.
    title = re.sub(r"[^a-z0-9]+", "", str(entry.get("title") or "").lower())
    if title:
        identities.append(f"title:{title}")
    return identities


def _resume_key(identity, season, episode) -> str:
    """One id per *episode*, not per entry - a series resumed at E4 must
    not hand its position to E5. `identity` is one of
    entry_identities()'s, never a raw `entry.get("id")`."""
    if episode:
        return f"{identity}|s{int(season or 0)}e{int(episode)}"
    return f"{identity}|film"


def _resume_keys(entry, season, episode) -> list:
    return [_resume_key(identity, season, episode)
            for identity in entry_identities(entry)]


def load_resume(entry, season, episode):
    """The saved position for this episode, or None.

    Every identity is tried, not just the best one - see
    entry_identities for why a title can be filed under two."""
    keys = _resume_keys(entry, season, episode)
    if not keys:
        return None
    records = storage.load(RESUME_FILE, [])
    for key in keys:
        for record in records:
            if record.get("id") == key:
                return record
    return None


def save_resume(entry, season, episode, position, duration, release=None):
    """Remember where this episode got to, **and what it was playing**.

    `release` is the owner's ask of 25 August 2026: "make sure to use
    the same source I used before, when I continue watching". Without it
    the record held a position and nothing about where that position
    came from, so continuing re-ran the whole race and very often landed
    on a different release - a different encode, different audio and
    subtitle tracks, and a seat measured against a runtime that was no
    longer the same file's.

    update_entry first, because it re-reads the file and touches exactly
    one record - writing a whole list back from a page is what erased a
    batch of imported games once (see storage.update_entry). The append
    below is the one case it cannot serve, there being nothing to update
    yet; it is safe here specifically because this file has a single
    writer - one player page, on the UI thread - unlike tracker.json."""
    keys = _resume_keys(entry, season, episode)
    if not keys:
        return          # no id, no imdb id, no title: nothing to key by
    _put_record(keys[0], {
        "entry_id": entry_identities(entry)[0],
        "season": season, "episode": episode,
        "position": float(position or 0.0),
        "duration": float(duration or 0.0),
        # Left alone when this call does not know it, rather than
        # written as None: a save from a path with no stream in hand
        # would otherwise erase the release the last one recorded.
        **({"release": release} if release else {}),
        "updated_at": storage.now_iso()})


def _put_record(key, fields):
    """Write one record into the player's own file, updating in place
    when it is already there.

    update_entry first, because it re-reads the file and touches exactly
    one record - writing a whole list back from a page is what erased a
    batch of imported games once (see storage.update_entry). The append
    below is the one case it cannot serve, there being nothing to update
    yet; it is safe here specifically because this file has a single
    writer - one player page, on the UI thread - unlike tracker.json."""
    if storage.update_entry(RESUME_FILE, key, fields):
        return
    records = storage.load(RESUME_FILE, [])
    records.append({"id": key, **fields})
    storage.save(RESUME_FILE, records)


def _subtitle_pref_keys(entry) -> list:
    """Where this title's remembered subtitle choice is filed.

    Per *entry*, not per episode: picking Arabic once should carry into
    the next episode and into the next session, which is the whole ask.
    Same identities as the resume records (see entry_identities), so a
    title with no id of its own is still remembered."""
    return [f"{identity}|subs" for identity in entry_identities(entry)]


def load_subtitle_choice(entry):
    """The remembered subtitle choice for this title, or None.

    A *recipe*, never a file: the url a subtitle came from is a
    per-session temporary on most of these providers and will not exist
    next time, so what is stored is the language, the source and the AI
    provider that produced it - enough to find the equivalent row in a
    fresh search (see PlayerPage._match_subtitle_choice)."""
    keys = _subtitle_pref_keys(entry)
    if not keys:
        return None
    records = storage.load(RESUME_FILE, [])
    for key in keys:
        for record in records:
            if record.get("id") == key:
                return record
    return None


def save_subtitle_choice(entry, lang, source, provider, label):
    keys = _subtitle_pref_keys(entry)
    if not keys:
        return
    # No "entry_id" on purpose: resume_point scans the same file by that
    # field, and a subtitle record answering it would be read as a
    # half-watched episode with no season and no position.
    _put_record(keys[0], {"lang": lang, "source": source,
                          "provider": provider, "label": label,
                          "updated_at": storage.now_iso()})


def forget_subtitle_choice(entry):
    keys = set(_subtitle_pref_keys(entry))
    if not keys:
        return
    records = storage.load(RESUME_FILE, [])
    kept = [r for r in records if r.get("id") not in keys]
    if len(kept) != len(records):
        storage.save(RESUME_FILE, kept)


def forget_untethered_resume():
    """Drop the records the old title-blind key left behind.

    They are `"None|s1e1"` and friends - one bucket shared by every
    unsaved title, so each one holds the position of whichever show
    wrote to it last and can only ever be wrong (see entry_identities).
    The new keys can never read or write them, so this is tidying rather
    than a fix, but leaving seven records that mean nothing in a file
    this small is how the next reader gets misled."""
    try:
        records = storage.load(RESUME_FILE, [])
        kept = [r for r in records
                if not str(r.get("id") or "").startswith("None|")]
        if len(kept) != len(records):
            storage.save(RESUME_FILE, kept)
    except Exception:
        logs.exception("Could not prune the untethered resume records")


def resume_point(entry):
    """The (season, episode) this entry was last left part-way through,
    or None when there is nothing to go back to.

    What the round continue button on a watching card opens - the same
    idea the reading cards already have, and the same distinction: the
    body of the card plays the *next* episode (see _starting_episode),
    the button goes back to the one that was actually interrupted.

    The two guards are the ones PlayerPage._prime_seat already
    applies, and for the same reasons: under RESUME_MIN_S there is
    nothing worth going back to, and past RESUME_MAX_FRACTION the
    episode is finished and "continue" would mean its credits. An
    episode failing them is simply not offered, which leaves the button
    doing what the card body does.

    Newest first, by the timestamp already stored on each record: a
    series can hold a stale half-watched episode from months ago behind
    the one paused ten minutes back."""
    identities = set(entry_identities(entry))
    if not identities:
        return None
    best = None
    for record in storage.load(RESUME_FILE, []):
        if record.get("entry_id") not in identities:
            continue
        position = float(record.get("position") or 0.0)
        duration = float(record.get("duration") or 0.0)
        if position < RESUME_MIN_S:
            continue
        if duration and position > duration * RESUME_MAX_FRACTION:
            continue
        if best is None or str(record.get("updated_at") or "") > str(best.get("updated_at") or ""):
            best = record
    if best is None:
        return None
    return best.get("season"), best.get("episode")


def clear_resume(entry, season, episode):
    """Drop the resume point once the episode is finished, so the next
    visit starts clean instead of offering the credits.

    Every identity, not only the best one: an episode filed under a
    title before its IMDb id resolved would otherwise survive being
    watched to the end and offer its credits on the next visit."""
    keys = set(_resume_keys(entry, season, episode))
    if not keys:
        return
    records = storage.load(RESUME_FILE, [])
    kept = [r for r in records if r.get("id") not in keys]
    if len(kept) != len(records):
        storage.save(RESUME_FILE, kept)


def _mark_watched(entry, season, episode):
    """Write this episode onto the tracker entry.

    tracker is imported here rather than at module scope on purpose: the
    tracker page is what will open this player, so a top-level import
    each way is a cycle. This direction is the lazy one because it runs
    once per episode, where the tracker's call runs on a click.

    Everything that decides whether the number may move now lives in
    `tracker.record_progress` - forward-only ordering, which file the
    entry belongs to, and the types that carry no episode at all. This
    used to gate on `last_watched_is_editable` as well, and that gate is
    exactly why nothing was ever recorded: it is False for a
    Stremio-backed entry, and with the Video Website choice gone every
    new video entry has no site at all, so the old rule refused
    essentially every anime. Stremio no longer overwrites a higher
    number either, so there is nothing left to protect against."""
    try:
        from windows import tracker
    except ImportError:                                 # pragma: no cover
        return False
    return tracker.record_progress(entry, season=season, episode=episode)


def _name_subtitles(found) -> list:
    """Stamp every result with its display name - see
    SUBTITLE_LANGUAGE_NAMES.

    Done once, here, rather than in the panel: the same string names the
    row, marks which row is selected, and labels the track after it
    loads, and deriving it three times is three chances to disagree.
    Sorted stably by (language, provider) first so the numbering reads
    in the order the rows are drawn."""
    rows = [dict(item) for item in found if isinstance(item, dict)]
    counts = {}
    for item in rows:
        code = str(item.get("lang") or "").strip().lower()
        language = SUBTITLE_LANGUAGE_NAMES.get(
            code, SUBTITLE_LANGUAGE_NAMES.get(code[:2], code.upper() or "Unknown"))
        provider = str(item.get("source") or "Unknown").strip()
        key = (language, provider)
        counts[key] = counts.get(key, 0) + 1
        item["language_name"] = language
        item["display_name"] = f"{language} {counts[key]} {provider}"
    return rows


# GetWindow's "the sibling above this one in z-order". NULL means the
# window is already at the top of its siblings - see _raise_native.
_GW_HWNDPREV = 3


def _raise_native(widget) -> None:
    """`raise_()`, but only when something is actually above it.

    **A raise that changes nothing is not free here, it destroys
    clicks.** Qt's raise_ is an unconditional SetWindowPos, and every
    overlay in this player is a native child window over mpv's own (see
    the module docstring). A SetWindowPos landing a few tens of
    milliseconds either side of a press on that window makes Windows drop
    the press outright - Qt never delivers a mousePressEvent at all, so
    the row under the pointer simply does not react. That is the owner's
    "in the download list I need to click 2 times to choose one".

    Measured 22 August 2026 against the real widget, real mouse input
    walking onto the row the way a hand does, 8 picks per run:

        restacking flowing (the shipped code)        1/8
        the panel's own raise removed                8/8
        pointer parked instead of moving             8/8
        the click poll disabled                      0/8   (not the poll)

    Nearly every raise here is fired by a tick or a property update
    rather than by the order having actually changed - `_wake_controls`
    up to 5.5 times a second while the pointer moves, `_refresh_skip_button`
    on every offer change, `_show_backdrop` on every buffer stall - so
    almost all of that was cost with no effect. Asking first is one
    GetWindow call and turns the storm into nothing, while leaving every
    genuine restack exactly as it was.

    Falls through to a plain raise_ on anything unexpected: being wrong
    about the z-order costs a bar behind the video, and this must not be
    the thing that raises."""
    try:
        if os.name == "nt" and widget.isVisible():
            # internalWinId, never winId: winId *creates* the native
            # window if there is not one, and promoting a widget that was
            # meant to stay a plain child tears down and rebuilds windows
            # under a running video. Measured the hard way - it took the
            # process out at the first call. 0 means "not native", which
            # is not this function's business, so fall through.
            hwnd = int(widget.internalWinId() or 0)
            if hwnd and not ctypes.windll.user32.GetWindow(hwnd, _GW_HWNDPREV):
                return          # already top of its siblings
    except Exception:
        pass
    try:
        widget.raise_()
    except RuntimeError:
        pass                    # torn down under us


def _left_button_down() -> bool:
    """Is the left button down for the message being handled right now?

    `GetKeyState`, deliberately, and not `GetAsyncKeyState`: the async
    call's 0x0001 "pressed since you last asked" bit is *cleared by
    reading it*, and that latch is `_poll_mouse`'s only way of catching a
    click that began and ended between two 40ms ticks. Asking here would
    consume it. GetKeyState answers for the message this thread is
    processing, which inside a row's mousePressEvent is exactly the press
    `_guard_click` is trying to name."""
    if os.name != "nt":
        return False
    try:
        return bool(ctypes.windll.user32.GetKeyState(_VK_LBUTTON) & 0x8000)
    except Exception:
        return False


def _make_native(widget):
    """Turn `widget` into a real native window so Windows composites it
    above the video's own native window.

    WA_DontCreateNativeAncestors matters as much as WA_NativeWindow:
    without it Qt promotes the page and everything above it too, and the
    page's radial-gradient backdrop stops being painted where the
    overlay sits."""
    widget.setAttribute(Qt.WidgetAttribute.WA_DontCreateNativeAncestors, True)
    widget.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, True)
    return widget


def _set_window_alpha(widget, alpha) -> bool:
    """Let DWM blend this native child window over whatever is under it.

    This is the one route to translucency that works here (see the module
    docstring). Qt's own translucency does not: the overlay is a native
    child window, mpv's video is a separately composited swapchain, and
    Qt never has both in one surface to blend. `WS_EX_LAYERED` moves the
    blend to DWM, which composites the children in z-order and so has the
    video already in the frame when it reaches the bar.

    Returns whether it took. Every caller treats False as "stay opaque"
    rather than as an error: a bar that cannot be seen through is a
    cosmetic loss, and a player that refuses to open over it is not."""
    if os.name != "nt":
        return False
    try:
        hwnd = int(widget.winId())
        if not hwnd:
            return False
        user32 = ctypes.windll.user32
        style = user32.GetWindowLongW(hwnd, _GWL_EXSTYLE)
        if not style & _WS_EX_LAYERED:
            user32.SetWindowLongW(hwnd, _GWL_EXSTYLE, style | _WS_EX_LAYERED)
        # The BOOL from this call, not the one above: SetWindowLongW
        # returns the *previous* style, and 0 is a legitimate previous
        # value, so it cannot be tested for success without clearing
        # GetLastError first.
        return bool(user32.SetLayeredWindowAttributes(
            hwnd, 0, int(max(0, min(255, alpha))), _LWA_ALPHA))
    except Exception:
        logs.exception("Could not make the player bar translucent")
        return False


def _hard_edge_font(point_size, bold=False) -> QFont:
    """The top bar's label font. Plain antialiased text now: the
    NoAntialias strategy this carried existed only for the colour-keyed
    era (blended glyph edges stopped matching the key and rode every
    letter as a dark halo), and with the bars alpha-veiled instead
    there is nothing for smoothing to break. The name and the split -
    size in QSS, rendering traits here - stay, because the QSS font
    rules still win the resolve and the eliding metrics still have to
    match what is painted.

    (Historical note, kept because it was photographed twice: with a
    key, antialiased edges blended toward the key colour, stopped
    matching it exactly, and DWM kept them - a near-black halo on every
    letter over a bright frame.)"""
    font = QFont()
    font.setPointSizeF(point_size)
    if bold:
        font.setWeight(QFont.Weight.Bold)
    return font


def _icon_button(glyph, tooltip, size=44, font_pt=16):
    button = QPushButton(glyph)
    button.setObjectName("Flat")
    button.setToolTip(tooltip)
    button.setFixedSize(size, size)
    # Every clickable shows the hand - the owner's ask, 24 August 2026,
    # and this factory is where most of the player's buttons come from.
    use_hover_cursor(button)
    button.setStyleSheet(
        # padding:0 - the app-wide QPushButton rule is `padding: 8px 16px`,
        # which on a fixed 44px button leaves 12px of content width for a
        # glyph whose ink is ~28px across, so it renders sliced. That is
        # what "the exit icon looks scratched" was: not a missing glyph
        # (U+F3B1 measures 233 ink px, bbox 28x26, in a real window) but
        # a clipped one. PlayPauseButton and _season_arrow already carry
        # this; _icon_button is where it was missed.
        f"QPushButton {{ background: transparent; border: none; padding: 0px;"
        f" color: {theme.TEXT}; font-family: {theme.FONT_STACK_ICONS};"
        f" font-size: {font_pt}pt; border-radius: {theme.RADIUS_SM}px; }}"
        f"QPushButton:hover {{ background: {theme.SURFACE_HOVER}; }}"
        f"QPushButton:pressed {{ background: {theme.SURFACE_ACTIVE}; }}"
        f"QPushButton:disabled {{ color: {theme.TEXT_DIM}; }}")
    use_hover_cursor(button)
    return button


def _text_button(text, tooltip=""):
    button = QPushButton(text)
    button.setToolTip(tooltip)
    button.setStyleSheet(
        f"QPushButton {{ background: transparent; border: 1px solid {theme.BORDER};"
        f" color: {theme.TEXT}; padding: 7px 13px; font-size: 11pt;"
        f" border-radius: {theme.RADIUS_SM}px; }}"
        f"QPushButton:hover {{ background: {theme.SURFACE_HOVER};"
        f" border: 1px solid {theme.ACCENT}; }}"
        f"QPushButton:disabled {{ color: {theme.TEXT_DIM}; }}")
    use_hover_cursor(button)
    return button


def _pill_button(text, tooltip=""):
    """A small rounded chip in the top bar - the resolution and audio
    pills. Clickable (each opens the panel it names), so a button rather
    than a label, but styled to read as a status pill, not a control."""
    button = QPushButton(text)
    button.setToolTip(tooltip)
    button.setStyleSheet(
        f"QPushButton {{ background: {theme.SURFACE_HOVER}; color: {theme.TEXT};"
        f" border: 1px solid {theme.BORDER}; padding: 3px 12px; font-size: 10pt;"
        f" font-weight: 700; border-radius: {theme.RADIUS_LG}px; }}"
        f"QPushButton:hover {{ border: 1px solid {theme.ACCENT}; color: {theme.TEXT}; }}")
    use_hover_cursor(button)
    return button


def _round_overlay(widget, radius=BAR_RADIUS, square=()):
    """Cut `widget`'s window to a rounded rectangle.

    Every bar on this page is a *native* child window (see the module
    docstring), and a native window is a hard rectangle no matter what
    its stylesheet draws: a border-radius alone leaves the corner pixels
    outside the rounded fill unpainted rather than transparent, so the
    bar still ends in a square corner over the video. Masking the window
    is what actually removes them.

    `square` names corners to leave alone - "tl", "tr", "bl", "br". The
    two full-width bars keep the pair that sits against the window's own
    edge: rounding a corner there would notch the window frame rather
    than the bar.

    Built out of two rectangles and four ellipses because QRegion has no
    rounded-rect constructor: an ellipse region is what gives the corner
    its curve, where a QPainterPath would have to be flattened to a
    polygon first.
    """
    w, h = widget.width(), widget.height()
    if w <= 0 or h <= 0:
        return
    radius = max(0, min(radius, w // 2, h // 2))
    if radius == 0:
        return
    rect_kind = QRegion.RegionType.Rectangle
    ellipse = QRegion.RegionType.Ellipse
    region = QRegion(0, radius, w, h - 2 * radius, rect_kind)
    region = region.united(QRegion(radius, 0, w - 2 * radius, h, rect_kind))
    span = 2 * radius
    # (disc box, square box) per corner: the disc is a full 2r circle
    # anchored in the corner, of which the mask only ever shows a
    # quarter; the square box is the r x r patch that fills that quarter
    # back in for a corner that is meant to stay sharp.
    corners = {
        "tl": ((0, 0), (0, 0)),
        "tr": ((w - span, 0), (w - radius, 0)),
        "bl": ((0, h - span), (0, h - radius)),
        "br": ((w - span, h - span), (w - radius, h - radius)),
    }
    for name, (disc, box) in corners.items():
        if name in square:
            region = region.united(QRegion(box[0], box[1], radius, radius, rect_kind))
        else:
            region = region.united(QRegion(disc[0], disc[1], span, span, ellipse))
    widget.setMask(region)


def _radius_css(radius=BAR_RADIUS, square=()):
    """The border-radius block matching `_round_overlay`'s mask, so the
    painted fill ends where the window does."""
    names = {"tl": "top-left", "tr": "top-right",
             "bl": "bottom-left", "br": "bottom-right"}
    return "".join(f" border-{names[key]}-radius: {0 if key in square else radius}px;"
                   for key in ("tl", "tr", "bl", "br"))


def _dimmed(color: str, factor: float) -> str:
    """`color` scaled toward black by `factor` - what that colour looked
    like on screen under the old uniform veil, baked into the paint."""
    raw = str(color).lstrip("#")
    r, g, b = (int(raw[i:i + 2], 16) for i in (0, 2, 4))
    return "#{:02x}{:02x}{:02x}".format(
        int(r * factor), int(g * factor), int(b * factor))


def _bar_style():
    """The fill behind a bar (top, controls, side panel).

    A top-lit gradient rather than a flat fill, anchored near-black so
    white text holds its contrast under the bars' light veil
    (CONTROLS_VEIL_ALPHA). The faint lift at the top edge is the
    "glass" - a sheen catching light along the bar's lip."""
    return (f"qlineargradient(x1:0, y1:0, x2:0, y2:1,"
            f" stop:0 {theme.SURFACE}, stop:0.5 {theme.BG}, stop:1 {theme.BG})")


# ---------------------------------------------------------------------
# Thread boundaries


class _MpvBridge(QObject):
    """mpv's thread -> Qt. Nothing else crosses."""
    prop = Signal(str, object)      # property name, new value
    ended = Signal(str)             # end-file reason
    loaded = Signal()               # mpv finished opening a file


class _WorkBridge(QObject):
    """Background lookups -> Qt. Every signal carries the run number it
    was fired for, so an answer from a superseded episode cannot be
    counted toward the current one - the same rule the tracker's lookups
    follow."""
    # list | None, run, still-looking. The last flag separates a partial
    # batch from the finished list: only the finished one may say "no
    # playable source was found", and only a partial may be held back
    # waiting for a better resolution.
    streams_ready = Signal(object, int, bool)
    subs_ready = Signal(object, int)        # list
    # Skip intervals for this episode (AniSkip / TheIntroDB); the
    # file's own chapters need no thread and never come through here.
    skips_ready = Signal(object, int)       # list, run
    sub_file_ready = Signal(str, str, int)  # path, label, run
    logo_ready = Signal(str, int)           # local png path, run
    # Cinemeta's real episode list for this title. No run: it belongs to
    # the entry, which cannot change while the page lives.
    meta_ready = Signal(object)             # videos list
    # What the title *is*, from the same record: {genres, country}. The
    # audio default reads it - see _on_meta_facts.
    meta_facts = Signal(object)
    # A torrent stream only becomes playable once the streaming server
    # has been handed its trackers, which takes a round trip (and may
    # start the server first). That cannot happen on the UI thread.
    stream_prepared = Signal(object, int, object, int)  # stream, index, resume, run
    failed = Signal(str, int)
    # Progress notes from a worker that are not failures - the AI
    # translation announces itself through this, since it takes tens of
    # seconds and silence there reads as a hang.
    note = Signal(str, int)


# ---------------------------------------------------------------------
# Pieces of the page


class VideoSurface(QWidget):
    """The native window mpv renders into. Paints one flat fill so that
    the moment before the first frame is the app's own background rather
    than whatever was on screen underneath."""

    def __init__(self, parent=None):
        super().__init__(parent)
        _make_native(self)
        self.setAutoFillBackground(True)
        palette = self.palette()
        palette.setColor(self.backgroundRole(), QColor(theme.BG))
        self.setPalette(palette)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def native_handle(self) -> int:
        """The HWND to hand mpv. Read after the attributes above, never
        before - see this module's docstring."""
        return int(self.winId())


class StartupBackdrop(QWidget):
    """The frame behind the loading message: this title's own still from
    TMDB, dimmed, with its logo filling over the middle of it.

    **Native, and that is the entire reason this class exists.** mpv
    renders into a native child window (VideoSurface), and on Windows a
    native child paints above every non-native sibling regardless of what
    raise_() was told - so LogoProgress, an ordinary child of the page,
    was being drawn underneath the video surface and had never once been
    visible. Anything that must appear over the video is either native
    itself or a child of something that is. This is native, and it
    carries the logo, so both problems are the same fix.

    Filled with the app's own background when there is no backdrop, so
    it is never a hole: the loading state looks the same as it always
    did for a title TMDB has nothing for, and better for one it does."""

    # The scrim over the still, top to bottom. Heavy enough that a bright
    # daylight backdrop cannot swallow either the logo or the message box
    # that sits on top of it, light enough that the picture is still a
    # picture rather than a texture.
    SCRIM = ((0.0, 14, 12, 9, 190), (0.45, 14, 12, 9, 150), (1.0, 14, 12, 9, 215))

    def __init__(self, parent=None):
        super().__init__(parent)
        _make_native(self)
        self._pixmap = None
        # Mid-playback stall mode (the owner's ask, 23 August 2026:
        # "keep the video with ONLY logo loading"): the widget shrinks
        # to a small badge around the logo and paints bare BG, no
        # backdrop still, no scrim - the frozen video frame stays
        # visible around it. The logo has to stay inside *this* widget
        # either way: it is only ever visible because this window is
        # native and sits over mpv's (see the class docstring), so
        # "logo without the backdrop" is a mode of this surface, not a
        # different widget. Entered/left in _show_buffer_frame and
        # _reset_stall_frame.
        self._stall = False
        # The cover-scaled copy for the current size. Backdrops are
        # full-resolution originals now (see artwork.BACKDROP_SIZE), and
        # smooth-scaling several megapixels on *every* paint - this
        # widget repaints on each buffering tick - is a stutter the
        # loading screen does not need. Rebuilt only when the size or
        # the picture changes.
        self._scaled = None
        self._scaled_size = None
        self.logo = LogoProgress(self)

    def set_backdrop(self, path) -> bool:
        pixmap = QPixmap(path) if path else QPixmap()
        self._pixmap = None if pixmap.isNull() else pixmap
        self._scaled = None
        self._scaled_size = None
        self.update()
        return self._pixmap is not None

    def set_stall(self, stalled: bool):
        if self._stall != bool(stalled):
            self._stall = bool(stalled)
            self.update()

    def stalled(self) -> bool:
        return self._stall

    def paintEvent(self, event):
        painter = QPainter(self)
        rect = self.rect()
        painter.fillRect(rect, QColor(theme.BG))
        if (self._pixmap is not None and not self._stall
                and rect.width() > 0 and rect.height() > 0):
            # **Cut at devicePixelRatio and tagged.** It used to scale to
            # rect.size(), which is *logical*, and Qt then stretched the
            # result up to the device surface - the owner's "the bg image
            # while loading the ep ... is pixeled a bit", seen on his
            # 2560x1440 panel at 125% (DPR 1.25), where a 2048-wide cut
            # was being blown up to 2560. The source is a 3840x2160 TMDB
            # original, so the sharp cut costs nothing but the scale.
            ratio = self.devicePixelRatioF() or 1.0
            if self._scaled is None or self._scaled_size != (rect.size(), ratio):
                # Cover, not fit: a 16:9 still in a 21:9 window would
                # otherwise leave two black columns beside it, which
                # reads as a broken image rather than as a backdrop.
                self._scaled = self._pixmap.scaled(
                    max(1, int(rect.width() * ratio)),
                    max(1, int(rect.height() * ratio)),
                    Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                    Qt.TransformationMode.SmoothTransformation)
                self._scaled.setDevicePixelRatio(ratio)
                self._scaled_size = (rect.size(), ratio)
            scaled = self._scaled
            # Centred on the pixmap's *logical* size - width()/height()
            # are device pixels once it carries a ratio.
            painter.drawPixmap(int((rect.width() - scaled.width() / ratio) / 2),
                               int((rect.height() - scaled.height() / ratio) / 2),
                               scaled)
            gradient = QLinearGradient(0.0, 0.0, 0.0, float(rect.height()))
            for stop, red, green, blue, alpha in self.SCRIM:
                gradient.setColorAt(stop, QColor(red, green, blue, alpha))
            painter.fillRect(rect, gradient)
        painter.end()


class SeekBar(QWidget):
    """Position, buffered-ahead and scrubbing in one strip.

    Buffered is drawn as a separate band rather than left implicit: on a
    stalling HLS source the difference between "the stream is dead" and
    "it is still filling" is the only thing that tells you whether
    switching source is worth doing, and that is precisely when someone
    is staring at this bar."""

    seeked = Signal(float)          # absolute seconds

    BAR_HEIGHT = 5
    KNOB_RADIUS = 7
    # How far in from each side the *track* starts, so that the knob - a
    # KNOB_RADIUS disc with a 2px pen, drawn centred on the played
    # position - is fully inside the widget at both extremes. Without it
    # the left half of the dot is cut off at 0% and the right half at
    # 100%, which is what the owner saw: the handle was not moving to the
    # end, it was being clipped by the widget's own edge. Every position
    # maps into this inset span rather than the full width, so the
    # fraction under the pointer and the fraction drawn still agree.
    EDGE = KNOB_RADIUS + 1

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(self.KNOB_RADIUS * 2 + 6)
        self.setMouseTracking(True)
        use_hover_cursor(self)
        self._duration = 0.0
        self._position = 0.0
        self._buffered = 0.0
        self._dragging = False

    def set_duration(self, value):
        self._duration = float(value or 0.0)
        self.update()

    def _column_of(self, value) -> int:
        """Which pixel column `value` seconds lands on, or -1 with no
        duration yet. What decides whether a repaint would change
        anything at all."""
        if self._duration <= 0:
            return -1
        left, span = self._track_span()
        share = max(0.0, min(1.0, float(value) / self._duration))
        return int(left + span * share)

    def set_position(self, value):
        """**Repaints only when the bar would actually look different.**

        mpv reports `time-pos` many times a second, and this used to
        call update() on every one of them. This bar is a *native child
        window* over the video surface (see the module docstring), so
        each of those was a native repaint competing with mpv's own
        rendering for the same frames - the owner's "frames drop when a
        video is playing". A progress bar that moves one pixel every
        second or so does not need sixty repaints in that second: on a
        24-minute episode across a 1000px bar the thumb advances one
        column roughly every 1.4s, and that is now exactly how often
        this paints. Nothing on screen changes, because a sub-pixel move
        had nothing to draw."""
        if self._dragging:
            return          # the thumb belongs to the pointer mid-drag
        value = float(value or 0.0)
        moved = self._column_of(value) != self._column_of(self._position)
        self._position = value
        if moved:
            self.update()

    def set_buffered(self, value):
        # Same rule, same reason: the buffered shading is redrawn only
        # when its edge reaches a new column.
        value = float(value or 0.0)
        moved = self._column_of(value) != self._column_of(self._buffered)
        self._buffered = value
        if moved:
            self.update()

    def _track_span(self):
        """(left edge, usable width) of the track - see EDGE."""
        return self.EDGE, max(1, self.width() - 2 * self.EDGE)

    def _fraction_at(self, x):
        left, span = self._track_span()
        return min(1.0, max(0.0, (x - left) / span))

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        top = (self.height() - self.BAR_HEIGHT) / 2
        left, span = self._track_span()
        rect = QRectF(left, top, span, self.BAR_HEIGHT)
        radius = self.BAR_HEIGHT / 2
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(theme.SURFACE_HOVER))
        painter.drawRoundedRect(rect, radius, radius)

        if self._duration > 0:
            buffered = min(1.0, max(0.0, self._buffered / self._duration))
            played = min(1.0, max(0.0, self._position / self._duration))
            if buffered > played:
                painter.setBrush(QColor(theme.BORDER))
                painter.drawRoundedRect(
                    QRectF(left, top, span * buffered, self.BAR_HEIGHT),
                    radius, radius)
            painter.setBrush(QColor(theme.ACCENT))
            painter.drawRoundedRect(
                QRectF(left, top, span * played, self.BAR_HEIGHT),
                radius, radius)
            centre = QPoint(int(left + span * played), int(self.height() / 2))
            painter.setBrush(QColor(theme.TEXT))
            painter.setPen(QPen(QColor(theme.ACCENT), 2))
            painter.drawEllipse(centre, self.KNOB_RADIUS, self.KNOB_RADIUS)

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton or self._duration <= 0:
            return
        self._dragging = True
        self._position = self._fraction_at(event.position().x()) * self._duration
        self.update()

    def mouseMoveEvent(self, event):
        if self._dragging and self._duration > 0:
            self._position = self._fraction_at(event.position().x()) * self._duration
            self.update()
        if self._duration > 0:
            self.setToolTip(_format_time(
                self._fraction_at(event.position().x()) * self._duration))

    def mouseReleaseEvent(self, event):
        if not self._dragging:
            return
        self._dragging = False
        self.seeked.emit(self._position)


class PlayPauseButton(QPushButton):
    """Play/pause, sitting directly in the bar like the buttons around
    it.

    The accent disc it used to be is gone at the owner's ask: no circle
    frame, just the mark, drawn in the same colour as the seek bar's
    position knob (theme.TEXT - the white dot marking the current frame)
    so the two controls that mean "here, now" share one colour. The
    transparent-with-hover-fill treatment is the row's own, so it reads
    as one of the bar's buttons rather than a badge floating over them.

    The mark stays *painted* rather than set as a font glyph, for the
    reason it always was: E769, the Fluent Pause, sets its two bars a
    full bar-width apart, which the owner read as two separate marks,
    and a font glyph offers no way to close that gap. Drawn, the gap is
    a number. State is `set_paused`, not `setText`, so QPushButton's own
    painting contributes nothing but the hover fill.
    """

    def __init__(self, tooltip, size=48):
        super().__init__("")
        self.setToolTip(tooltip)
        self.setFixedSize(size, size)
        self._paused = False
        self.setStyleSheet(
            # padding:0 explicitly - the app-wide QPushButton rule sets
            # 8px 16px, which on a small fixed square eats the whole
            # width (measured: the season arrows vanished under it).
            f"QPushButton {{ background: transparent;"
            f" border: none; padding: 0px; border-radius: {theme.RADIUS_SM}px; }}"
            f"QPushButton:hover {{ background: {theme.SURFACE_HOVER}; }}"
            f"QPushButton:pressed {{ background: {theme.SURFACE_ACTIVE}; }}")
        use_hover_cursor(self)

    def set_paused(self, paused):
        paused = bool(paused)
        if paused != self._paused:
            self._paused = paused
            self.update()

    def paintEvent(self, event):
        # super() first, for the QSS hover/pressed fills.
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        # The seek knob's fill (theme.TEXT), the owner's ask - with the
        # disc gone there is no accent ground under it, so the old
        # ON_ACCENT would be near-invisible here.
        painter.setBrush(QColor(theme.TEXT))
        side = float(min(self.width(), self.height()))
        cx, cy = self.width() / 2.0, self.height() / 2.0
        if self._paused:
            # Play. Shifted right by a tenth of its width: a triangle
            # centred on its bounding box always reads left of centre,
            # because its mass is on the flat side.
            height = side * 0.48
            width = height * 0.86
            nudge = width * 0.10
            painter.drawPolygon(QPolygonF([
                QPointF(cx - width / 2 + nudge, cy - height / 2),
                QPointF(cx - width / 2 + nudge, cy + height / 2),
                QPointF(cx + width / 2 + nudge, cy)]))
        else:
            # Pause. The gap is the whole point of drawing this.
            height = side * 0.46
            width = side * 0.14
            gap = width * 0.62
            radius = width * 0.28
            painter.drawRoundedRect(
                QRectF(cx - gap / 2 - width, cy - height / 2, width, height),
                radius, radius)
            painter.drawRoundedRect(
                QRectF(cx + gap / 2, cy - height / 2, width, height),
                radius, radius)


# The ±10s SkipButtons that sat either side of play are gone at the
# owner's ask; the keyboard's ←/→ still seek.


def _clear_layout(layout):
    """Take everything out of `layout`, nested layouts included.

    Recursive because the panel's footer holds stepper *layouts*, not
    just widgets, and a takeAt loop that only looks at item.widget()
    leaves those behind - they would stack up on every refill."""
    while layout.count():
        item = layout.takeAt(0)
        widget = item.widget()
        if widget is not None:
            # hide() before the unparent - see details._clear_rows for
            # the whole story: without it a queued show lands on a
            # parentless widget and Qt gives it a desktop window of its
            # own, which is what flashed white during a fetch.
            widget.hide()
            widget.setParent(None)
            widget.deleteLater()
            continue
        child = item.layout()
        if child is not None:
            _clear_layout(child)
            child.deleteLater()


class OverlayPanel(QFrame):
    """The popup the subtitle/track/quality buttons open.

    Native for the same reason the controls are (see module docstring),
    and therefore opaque: it sits over the video, and a native child
    window cannot be blended against another native child window.

    Refilled rather than replaced when its contents change - see
    `reset`, and the flashing it exists to stop."""

    closed = Signal()

    def __init__(self, parent, title):
        super().__init__(parent)
        _make_native(self)
        # Glassy like the bars: _show_panel gives this window the same
        # DWM alpha (BAR_ALPHA), so the dark scrim gradient shows the
        # frame through it instead of reading as an opaque slab.
        self.setStyleSheet(
            f"QFrame {{ background: {_bar_style()}; border: 1px solid {theme.BORDER};"
            f" border-radius: {BAR_RADIUS}px; }}")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 14, 16, 14)
        outer.setSpacing(10)

        header = QHBoxLayout()
        header.setSpacing(6)
        # Kept on self so reset() can retitle a panel it is refilling
        # instead of building a new one.
        self.title_label = QLabel(title)
        self.title_label.setStyleSheet(
            f"color: {theme.TEXT}; font-size: 13pt; font-weight: 700;"
            f" background: transparent; border: none;")
        header.addWidget(self.title_label)
        header.addStretch(1)
        close_btn = _icon_button("", "Close", size=30, font_pt=11)
        close_btn.clicked.connect(self.closed.emit)
        header.addWidget(close_btn)
        outer.addLayout(header)

        self.body = QWidget()
        self.body.setStyleSheet("background: transparent; border: none;")
        self.body_layout = QVBoxLayout(self.body)
        self.body_layout.setContentsMargins(0, 0, 0, 0)
        self.body_layout.setSpacing(6)
        self.area = scroll_area(self.body)
        self.area.setStyleSheet("background: transparent; border: none;")
        # **A solid viewport, not a transparent one.** With the viewport
        # transparent, a scroll cannot blit: every tick repaints the
        # whole ancestor stack under every row - inside a native child
        # window that is already competing with mpv for present slots,
        # which is the owner's "the windows in the video player like
        # subtitles still has the low fps while scrolling". A solid
        # ground lets QScrollArea move the backing store and repaint
        # only the uncovered band. The colour is the bar gradient's own
        # body, darkened to where the gradient sits mid-panel, so rows
        # scroll over what looks like the same glass.
        self.area.viewport().setStyleSheet(
            f"background: {_dimmed(theme.BG, _VEIL_FACTOR)};")
        outer.addWidget(self.area, stretch=1)

        # Vertical: the footer holds full-width stepper rows now, and two
        # of them side by side left the value column 40px wide.
        self.footer_layout = QVBoxLayout()
        self.footer_layout.setSpacing(6)
        outer.addLayout(self.footer_layout)

    def reset(self, title=None):
        """Empty the body and footer so this panel can be refilled
        without being destroyed.

        **This is what stops the source list "opening several small
        windows".** A redraw used to mean a whole new OverlayPanel, and
        these are *native* child windows over the video: each rebuild
        tore one down and put another up, and a freshly created native
        overlay paints at Qt's default geometry for a frame before
        _show_panel moves it - the same one-frame corner box _show_status
        already documents. At one redraw per lookup that was rare enough
        to miss; progressive stream and subtitle results made it one per
        *batch*, which is several flashes per lookup and exactly what
        the owner reported. Refilling creates no window at all.

        The scroll offset is kept: a list that grows while it is being
        read must not jump back to the top under the pointer."""
        bar = self.area.verticalScrollBar()
        offset = bar.value() if bar is not None else 0
        _clear_layout(self.body_layout)
        _clear_layout(self.footer_layout)
        if title:
            self.title_label.setText(title)
        if bar is not None and offset:
            # **Restored when the range comes back, not on a timer** -
            # the owner's ask, 23 August 2026: "when I chose a subtitle,
            # and it loads, do not make the list reload and take me back
            # to the top so I need to scroll!".
            #
            # The original was a bare `QTimer.singleShot(0, setValue)`,
            # and it did not work: measured in a harness driving the real
            # panel, offset 838 in, **0** out. The event order on that
            # first pass is rangeChanged(0, 0) - the scroll area
            # recomputing against the body this method has just emptied -
            # then the restore fires while maximum is still 0, so the
            # setValue clamps to 0, and only afterwards does the range
            # climb back (0, 1558), (0, 1617), (0, 1676).
            #
            # **The first attempt at this fix failed for a worse reason,
            # and it is worth recording.** It waited on `rangeChanged` -
            # correct - but kept the zero-timer "as a backstop", and that
            # backstop ran first, clamped to 0 exactly as before, and
            # then *disconnected the range handler* on its way out. The
            # fix disabled itself. So: nothing here disconnects or gives
            # up until the value has actually landed, and the backstop
            # is late rather than immediate - it exists only for a refill
            # that ends up shorter than it was, where the old offset is
            # never reachable again and the new bottom is the best answer
            # available.
            state = {"done": False}

            def _restore():
                if state["done"]:
                    return
                try:
                    bar.setValue(offset)
                    landed = bar.value() == offset
                except RuntimeError:
                    state["done"] = True
                    return
                if landed:
                    _finish()

            def _finish():
                state["done"] = True
                try:
                    bar.rangeChanged.disconnect(_on_range)
                except Exception:
                    pass

            def _on_range(_lo, hi):
                if hi >= offset:
                    _restore()

            bar.rangeChanged.connect(_on_range)
            # Long enough that every rangeChanged of a normal refill has
            # been and gone (the measured trace settles inside one event
            # loop pass), short enough that a genuinely shorter list is
            # not left scrolled somewhere arbitrary.
            QTimer.singleShot(250, lambda: (_restore(), _finish()))

    def add_group(self, name, into=None):
        label = QLabel(name)
        label.setStyleSheet(
            f"color: {theme.TEXT_MUTED}; font-size: 10pt; font-weight: 700;"
            f" background: transparent; border: none; padding-top: 4px;")
        # `is None`, never truthiness: PyQt layouts implement __len__ as
        # count(), so an **empty column is falsy** - `(into or default)`
        # sent the first widget of every fresh column to the default
        # layout instead. That one expression is the whole of the
        # owner's "the subtitles window is missed up" screenshot: the
        # three group headers stacked in the body and all three steppers
        # in the footer, while the rows (added through the `is not None`
        # branch below) sat in their columns.
        (self.body_layout if into is None else into).addWidget(label)

    def add_row(self, title, subtitle, on_click, selected=False, chevron=False,
                index=None, into=None, dot=False):
        card = Card(matte=True)

        # Rows are borderless at rest now (the Harbor pass); the border
        # stays 1px in every state so picking one shifts nothing, and
        # the accent ring remains what says "this is the one in use".
        #
        # **The hover rule has to be written here.** A widget's own
        # stylesheet beats the application one for that widget, so the
        # app's `#Card[matte][hoverable]:hover` never reached these rows
        # - measured, a hovered unselected row painted 0 accent pixels,
        # identical to an unhovered one. And since the resting fill was
        # SURFACE_HOVER already, hovering changed nothing at all: the
        # owner's "add a hover when the mouse touch the buttons".
        #
        # Hover deliberately does *not* borrow the accent ring the way
        # the app rule does. Selection is the accent; hover is a lift in
        # the fill plus a neutral edge, so a row under the pointer can
        # never be mistaken for the row in use.
        def paint(is_selected):
            rest = theme.SURFACE_HOVER if is_selected else theme.SURFACE
            hover = theme.SURFACE_ACTIVE if is_selected else theme.SURFACE_HOVER
            ring = theme.ACCENT if is_selected else "transparent"
            hover_ring = theme.ACCENT if is_selected else theme.BORDER
            card.setStyleSheet(
                f"QFrame#Card {{ background: {rest};"
                f" border: 1px solid {ring};"
                f" border-radius: {theme.RADIUS}px; }}"
                f"QFrame#Card:hover {{ background: {hover};"
                f" border: 1px solid {hover_ring};"
                f" border-radius: {theme.RADIUS}px; }}")
            head.setStyleSheet(
                f"color: {theme.ACCENT if is_selected else theme.TEXT};"
                f" font-size: 11pt; font-weight: 600;"
                f" background: transparent; border: none;")

        # Handed back on the card so a caller can move the highlight
        # without rebuilding the panel. Rebuilding is what made picking a
        # track stutter: it deleteLater'd four native child windows over
        # a running video and built them again, one frame later.
        card.set_selected = paint
        # Outer row so a drill-down chevron can sit against the right edge
        # while the title/subtitle stack keeps the left. A plain VBox card
        # had no column to pin the chevron to.
        outer = QHBoxLayout(card)
        outer.setContentsMargins(12, 9, 12, 9)
        outer.setSpacing(8)
        column = QVBoxLayout()
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(2)
        head = QLabel(title)
        head.setWordWrap(True)
        paint(selected)
        column.addWidget(head)
        if subtitle:
            sub = QLabel(subtitle)
            sub.setWordWrap(True)
            sub.setStyleSheet(
                f"color: {theme.TEXT_MUTED}; font-size: 9.5pt;"
                f" background: transparent; border: none;")
            column.addWidget(sub)
        outer.addLayout(column, stretch=1)
        if dot:
            # The small disc Stremio puts on the active language and the
            # active variant - state, where the ring is the browse
            # position. ACCENT rather than Stremio's green: the dot has
            # to be this app's own accent, not another product's.
            mark = QLabel()
            mark.setFixedSize(10, 10)
            mark.setStyleSheet(
                f"background: {theme.ACCENT}; border: none;"
                f" border-radius: 5px;")
            outer.addWidget(mark, alignment=Qt.AlignmentFlag.AlignVCenter)
        if chevron:
            arrow = QLabel(GLYPH_CHEVRON)
            arrow.setStyleSheet(
                f"color: {theme.TEXT_MUTED}; font-size: 15pt; font-weight: 700;"
                f" background: transparent; border: none;")
            outer.addWidget(arrow, alignment=Qt.AlignmentFlag.AlignVCenter)
        card.clicked.connect(on_click)
        if into is not None:
            into.addWidget(card)
        elif index is None:
            self.body_layout.addWidget(card)
        else:
            # For callers keeping a live panel in step with changing data
            # (the tracks panel): a row joins its group in place instead
            # of the whole panel being torn down and rebuilt - see
            # PlayerPage._sync_track_rows for why rebuilding is not free.
            self.body_layout.insertWidget(index, card)
        return card

    def add_message(self, text, into=None):
        label = QLabel(text)
        label.setWordWrap(True)
        label.setStyleSheet(
            f"color: {theme.TEXT_MUTED}; font-size: 11pt;"
            f" background: transparent; border: none;")
        (self.body_layout if into is None else into).addWidget(label)
        return label

    def add_stat(self, name, value_text, into=None):
        """One "Peers 17" pair - the label small and muted above nothing,
        the value beside it in the reading weight. The shape the owner
        sketched for the connection panel: three of these on one row,
        no boxes and no chrome around them."""
        block = QWidget()
        block.setStyleSheet("background: transparent; border: none;")
        row = QHBoxLayout(block)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        title = QLabel(name)
        title.setStyleSheet(
            f"color: {theme.TEXT_MUTED}; font-size: 10.5pt;"
            f" background: transparent; border: none;")
        row.addWidget(title)
        value = QLabel(value_text)
        value.setStyleSheet(
            f"color: {theme.TEXT}; font-size: 11.5pt; font-weight: 700;"
            f" background: transparent; border: none;")
        row.addWidget(value)
        (self.body_layout if into is None else into).addWidget(block)
        return value.setText

    def add_stepper(self, name, value_text, on_left, on_right, step_text="",
                    into=None):
        """A stepper as the owner sketched it: the name small above, then
        a full-width row of − button, the value centred between them,
        + button.

        `step_text` puts the amount on the buttons themselves ("-0.1" /
        "+0.1"), the owner's ask - a bare +/- said which way but not by
        how much, and the delay button in particular moves in tenths
        where the font one moves in twos.

        Two lines rather than the old single row with the name on the
        left: at the panel's width that layout crowded the value column
        to 84px, and the round buttons read better with the whole row to
        themselves.

        Returns a setter for the value text, so the caller nudges the
        number and hands the new one back without rebuilding the panel
        (which would scroll the subtitle list back to the top under the
        finger that is repeatedly pressing one button)."""
        block = QWidget()
        block.setStyleSheet("background: transparent; border: none;")
        column = QVBoxLayout(block)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(4)

        title = QLabel(name)
        title.setStyleSheet(
            f"color: {theme.TEXT_MUTED}; font-size: 10pt;"
            f" background: transparent; border: none;")
        column.addWidget(title)

        row = QHBoxLayout()
        row.setSpacing(8)
        minus = self._stepper_button(
            f"{GLYPH_MINUS}{step_text}" if step_text else GLYPH_MINUS,
            f"Less {name.lower()}", wide=bool(step_text))
        minus.clicked.connect(on_left)
        row.addWidget(minus)

        value = QLabel(value_text)
        value.setAlignment(Qt.AlignmentFlag.AlignCenter)
        value.setStyleSheet(
            f"color: {theme.TEXT}; font-size: 12pt; font-weight: 700;"
            f" background: transparent; border: none;")
        # The value takes the whole middle, so the two buttons sit at the
        # row's ends and never move as the number changes width.
        row.addWidget(value, stretch=1)

        plus = self._stepper_button(
            f"{GLYPH_PLUS}{step_text}" if step_text else GLYPH_PLUS,
            f"More {name.lower()}", wide=bool(step_text))
        plus.clicked.connect(on_right)
        row.addWidget(plus)
        column.addLayout(row)

        # `is None`, not truthiness - an empty QVBoxLayout is falsy (see
        # add_group).
        (self.footer_layout if into is None else into).addWidget(block)
        return value.setText

    @staticmethod
    def _stepper_button(glyph, tooltip, wide=False):
        """A round, clearly-tappable stepper button - circular because
        that is the owner's sketch, and because a disc reads as "tap me"
        where a square reads as a key. `wide` is the pill variant for a
        button carrying the step amount ("-0.1"), which cannot fit a
        36px disc; the smaller font is for the same reason."""
        button = QPushButton(glyph)
        button.setToolTip(tooltip)
        use_hover_cursor(button)
        button.setFixedSize(64 if wide else 36, 36)
        # Hold to repeat. Delay moves in tenths of a second, so a release
        # out of sync by five seconds was fifty separate clicks - which
        # is what "sub-delay has no effect" actually was on the owner's
        # Bleach subtitle. At 120ms a held button covers 0.8s of delay
        # per second held, and 350ms before the first repeat is long
        # enough that a single deliberate tap stays a single step.
        button.setAutoRepeat(True)
        button.setAutoRepeatDelay(STEPPER_REPEAT_DELAY_MS)
        button.setAutoRepeatInterval(STEPPER_REPEAT_MS)
        button.setStyleSheet(
            # padding:0 - see PlayPauseButton; the app-wide 8px 16px padding
            # would otherwise clip a glyph on a button this small.
            f"QPushButton {{ background: {theme.SURFACE_HOVER}; color: {theme.TEXT};"
            f" border: 1px solid {theme.BORDER}; padding: 0px;"
            f" font-size: {'11pt' if wide else '15pt'};"
            f" font-weight: 700; border-radius: 18px; }}"
            f"QPushButton:hover {{ background: {theme.ACCENT_SOFT};"
            f" border: 1px solid {theme.ACCENT}; }}"
            f"QPushButton:pressed {{ background: {theme.SURFACE_ACTIVE}; }}")
        use_hover_cursor(button)
        return button

    def finish(self):
        self.body_layout.addStretch(1)


# ---------------------------------------------------------------------


class _KeyRelay(QObject):
    """Send the keyboard to the player, wherever focus actually is.

    **The player is not one window.** mpv renders into a native child
    (`VideoSurface`), and the bars over it are native children too
    (`_make_native`) - on Windows a native child takes the keyboard when
    it is clicked, and Qt then delivers the key to *that* widget, not to
    the page. So Space did nothing after a click on the control bar, and
    a media key never reached the page at all. Raising or re-focusing
    the page on every click was tried in this file's history and fights
    the click it is answering.

    An application filter is the one place that sees the key whatever
    holds focus. It deliberately does **not** take keys away from
    anything being typed into - the episode filter and the subtitle
    search are QLineEdits inside the player, and "P" belongs to them
    while they have the caret - nor while a modal dialog is up."""

    def __init__(self, page):
        super().__init__(page)
        self._page = page

    @staticmethod
    def _is_typing(widget) -> bool:
        while widget is not None:
            if isinstance(widget, (QLineEdit, QComboBox)):
                return True
            widget = widget.parentWidget()
        return False

    def eventFilter(self, obj, event):
        if event.type() != QEvent.Type.KeyPress:
            return False
        page = self._page
        try:
            if page is None or not page.isVisible() or page._closing:
                return False
        except RuntimeError:
            return False        # the page went away under a queued key
        app = QApplication.instance()
        if app is not None and app.activeModalWidget() is not None:
            return False
        if self._is_typing(QApplication.focusWidget()):
            return False
        page.keyPressEvent(event)
        return event.isAccepted()


class PlayerPage(GlassPage):
    """The player, as a page covering the whole window.

    A page rather than a second top-level window: full screen, the dark
    title bar, the window's own geometry memory and Escape handling all
    already exist on the main window, and a separate window would need
    its own copy of each. It covers the sidebar as well as the content
    area - a 240px navigation column beside a video is not what anyone
    watching wants, and it makes the full-screen toggle a no-op on
    layout rather than a reparent (which would destroy and recreate the
    native window mpv is rendering into)."""

    # Emitted once, from close_player - what lets the page that opened
    # this refresh its cards the moment the player goes away, instead of
    # waiting for the next page visit or a Sync click.
    closed = Signal()

    def __init__(self, parent, entry, season=None, episode=None,
                 streams=None, on_close=None):
        super().__init__(parent=parent)
        self.entry = entry
        self.on_close = on_close
        self._window = parent.window() if parent is not None else None
        self._closing = False
        self._run = 0

        self.season, self.episode = self._starting_episode(season, episode)
        self._given_streams = list(streams) if streams else None

        self.handle = None
        self._streams = []
        self._resumed_release_first = False
        self._stream_index = 0
        # Whether _streams is the list playback is already running
        # against. Once it is, later batches from the same lookup may
        # only *add* to it - see _merge_streams for why replacing it
        # would move _stream_index and _dead_sources under themselves.
        self._streams_started = False
        # Set here as well as in _load_into_mpv: mpv's property callbacks
        # start arriving the moment the instance exists, which is before
        # any file is loaded, and reading these from there would be an
        # AttributeError on mpv's own thread - where it kills the event
        # thread silently and the UI then waits forever.
        self._awaiting_first_frame = False
        self._buffering_percent = 0
        # Sources already proven dead this session, so _try_next_source
        # cannot loop back onto one it has just rejected.
        self._dead_sources = set()
        self._subtitles = []
        self._subtitle_label = "Off"
        # Whether this episode's remembered subtitle has been dealt with
        # - applied, ruled out, or overtaken by a pick the user made.
        # See _auto_apply_subtitle.
        self._sub_auto_done = False
        # Whether this episode's subtitle search has been fired - see
        # _ensure_subtitle_search for when that is allowed to happen.
        self._subs_search_started = False
        # The pick currently in flight, held until mpv confirms it loaded
        # so that only a subtitle that actually worked is remembered.
        self._pending_sub_choice = None
        # The sticky toast a subtitle pick raises, held until that pick
        # finishes - see _pick_subtitle.
        self._sub_toast = None
        self._sub_delay = 0.0
        # Said once per player, not once per nudge - see _apply_sub_delay.
        self._delay_warned = False
        self._sub_size = SUB_SIZE_DEFAULT
        # None, not SUB_POS_CLEAR: the first _set_sub_position must reach
        # mpv even if it asks for what mpv's default already is.
        self._sub_pos = None
        # Where the player wants the line, and the owner's own nudge on
        # top of it - see _apply_sub_position.
        self._sub_pos_base = SUB_POS_CLEAR
        self._sub_pos_offset = 0
        # id(widget) -> (hwnd, alpha) already applied. See _veil.
        self._veiled = {}
        self._tracks = []
        self._duration = 0.0
        self._position = 0.0
        self._paused = False
        self._volume = 100
        self._muted = False
        self._speed = 1.0
        self._marked_watched = False
        # One embedded-Arabic auto-select per episode - see
        # _auto_select_arabic_track.
        self._arabic_track_done = False
        self._panel = None
        # What the download panel is currently set to. Held on the page,
        # not in the panel: the panel is rebuilt from scratch on every
        # pick (the same pattern the subtitle panel uses), so anything
        # kept in it would be thrown away by the first choice made.
        # `None` quality means "best available", which is also what an
        # empty stream list has to fall back to.
        self._dl_scope = "episode"
        self._dl_quality = None
        self._dl_quality_set = False
        self._dl_subtitle = None
        # Which audio the queued release should carry - "jp" (original)
        # or "en" (dub) - a soft preference over release names, see
        # downloads._order_by_audio.
        #
        # **Defaulted from the medium, not fixed at Japanese** (the
        # owner's ask, 22 August 2026): anime in Japanese, films and
        # series in English. It was hardcoded "jp", so queueing an
        # episode of a live-action series quietly asked the download
        # ranker to prefer a Japanese track that does not exist. Same
        # answer the *player* opens on, from the same function, so the
        # download and the playback cannot disagree about a title.
        self._anime_hint = None       # Cinemeta's verdict, once it answers
        self._dl_audio = ("jp" if "jpn" in self._audio_language_preference()
                          else "en")
        self._dl_folder = None
        self._temp_dir = None
        # Where the seat playback still owes is, from the moment a file
        # is loaded until the picture actually gets there. It exists
        # because "Resumed From 9:23" used to be said at *load* time,
        # over a black screen, 10-30s before the picture arrived (the
        # owner's report); the arrival is announced when it has happened
        # now, and the wait is narrated in the meantime. See
        # _load_into_mpv and _on_property.
        self._resume_target = None
        # When that seat stops being promised - see SEAT_GIVE_UP_S.
        self._seat_deadline = None
        # mpv's first time-pos report after an at-seat open - the seek
        # target echoed back, not a decoded frame. See _on_property.
        self._seat_reported = None
        # Whether mpv is currently playing the file this page last asked
        # for. False from the moment a load is issued until the observed
        # `path` property reports the new url. This is the gate that
        # keeps the property stream honest across a load: mpv keeps
        # emitting the *outgoing* file's time-pos for a few frames after
        # loadfile is issued, and those stale events used to be read as
        # the new source's first frame - which on a source switch
        # satisfied the seat's landed check with the *old* position, so
        # the seek into the new file was never re-issued and "Skipping
        # to X:XX" described a journey nothing was making (the owner's
        # report, 22 August 2026). Ordering makes the gate sound: mpv
        # delivers property changes in emission order through one queued
        # signal, so once `path` names the new file, every later
        # time-pos is genuinely its.
        self._file_ready = False
        self._loading_url = None
        # The one sticky toast the player uses to narrate work that
        # happens over a live picture. Nothing else can: the loading
        # backdrop refuses to cover a running picture and the startup
        # gauge has already stopped. See _say_working.
        self._work_toast = None
        # And the same box just after it has said its last word, while it
        # is still readable - so the next thing to say re-reads it rather
        # than stacking a second toast in the same corner.
        self._spent_toast = None
        # Whether this file has had its default audio track chosen yet -
        # once per load, so a track the user picks by hand is not
        # overruled by the next track-list mpv emits. See _apply_audio_default.
        self._audio_default_done = False
        # Whether the wordless loading state is up (backdrop + pulsing
        # logo, no status box) - _status_visible alone can no longer
        # answer "is the loading screen showing" (see _show_loading).
        self._loading_visible = False
        self._episode_bar = None
        self._episode_rows = {}
        # Which season the episode panel is showing. Starts on the one
        # being played, but the ‹ › arrows move it independently so the
        # user can browse another season without the player leaving this
        # one until they actually pick an episode.
        self._panel_season = self.season
        # The resolution panel is a two-level drill-down: None shows the
        # resolution list, a quality string shows the sources at it.
        self._streams_view = None
        # Click-to-pause state, filled in by _poll_mouse. The press
        # position is kept so a drag can be told from a click.
        self._mouse_down = False
        self._press_pos = None
        self._press_on_video = False
        # Double-click-to-fullscreen state. The pause state at the first
        # click is remembered so the second click can put it back exactly
        # (toggling it a second time would race mpv's still-in-flight
        # pause callback and could land on the wrong state).
        self._last_click_time = 0.0
        self._pause_before_click = False
        # Where the last click landed, so the next one can be told from a
        # double-click by distance as well as by time - see
        # DOUBLE_CLICK_PX.
        self._last_click_pos = None

        self._bridge = _MpvBridge()
        self._bridge.prop.connect(self._on_property)
        self._bridge.ended.connect(self._on_ended)
        self._bridge.loaded.connect(self._on_file_loaded)
        self._work = _WorkBridge()
        self._work.streams_ready.connect(self._on_streams)
        self._work.subs_ready.connect(self._on_subtitles)
        self._work.skips_ready.connect(self._on_skips)
        self._work.sub_file_ready.connect(self._on_subtitle_file)
        self._work.logo_ready.connect(self._on_logo)
        self._work.meta_ready.connect(self._on_meta)
        self._work.meta_facts.connect(self._on_meta_facts)
        self._work.stream_prepared.connect(self._on_stream_prepared)
        self._work.failed.connect(self._on_failed)
        # Its own slot, not _on_failed: a note is a *step* of the
        # subtitle job, and it belongs in the sticky toast that job is
        # already holding rather than in a 2s box of its own that fades
        # while the translator is still working (see _pick_subtitle).
        self._work.note.connect(self._on_sub_note)

        # Cinemeta's real season/episode map, once it arrives: what the
        # season list, the prev/next bounds and the wrong-episode guard
        # answer from instead of guessing off latest_available. None
        # until then - the guesses below hold the fort for the first
        # half-second.
        self._meta_aired = None       # {season: highest aired episode}
        self._meta_videos = None      # Cinemeta's raw rows, for the list
        # None until Cinemeta has answered; then whether stremio.looks_anime
        # says this title is anime. Read by _audio_language_preference.
        self._anime_hint = None
        # Whether the next episode's sources have been prefetched into
        # streams' cache for this episode's run.
        self._prefetched = False
        # And the next episode's actual *release*, warmed once near the
        # end of this one - see _maybe_prewarm_next. `_prewarmed` is
        # every hash this page has warmed, so close_player can give them
        # all back.
        self._prewarm_hash = None
        self._prewarm_asked = False
        # The metadata-only half of the next-episode warm, which
        # runs on its own (earlier) condition - see
        # _maybe_prewarm_next.
        self._meta_prewarm_asked = False
        self._prewarm_checked = 0.0
        self._prewarmed = set()
        # Skip intervals for this episode: [{"type","start","end",...}].
        # Filled from mpv's own chapters the moment a file loads, and
        # again from AniSkip when that answers. See _refresh_skip_button.
        self._skips = []
        # A resolution the user pinned by hand (see _switch_stream). It
        # holds for the whole episode now, not just until the pick loads.
        self._requested_quality = None
        self._quality_given_up = False
        # What the skip button is currently offering, so a press knows
        # where to seek and the button is not rebuilt every tick.
        self._skip_offer = None
        # Whether this episode has already been asked about.
        self._skips_asked = False
        # Whether the crowd lookup has reported for this episode - see
        # _current_skip, which holds a 0:00 chapter opening until it has.
        self._skips_answered = False

        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)
        # See _KeyRelay: the keys have to arrive even when a native child
        # of this page holds the keyboard.
        self._key_relay = _KeyRelay(self)
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self._key_relay)

        self.surface = VideoSurface(self)
        self._build_top_bar()
        self._build_controls()
        self._build_episode_bar()
        # The loading frame: the title's still from TMDB with its logo
        # filling over it as the buffer grows. Both live on one native
        # window - see StartupBackdrop for why the logo cannot simply be
        # a child of this page - and both are silently absent for a title
        # TMDB has nothing for, which is why the status box below still
        # carries the whole message on its own.
        self.backdrop = StartupBackdrop(self)
        self.backdrop.hide()
        self.logo = self.backdrop.logo
        self.logo.hide()

        # Native, for the reason .claude/rules/ui.md states plainly: mpv
        # renders into a native child window, and on Windows a native
        # child paints above every non-native sibling whatever raise_()
        # was told. A plain QPushButton here would be drawn every frame
        # and never once be visible - which is exactly what happened to
        # the loading logo before StartupBackdrop existed.
        self.skip_btn = QPushButton("", self)
        _make_native(self.skip_btn)
        self.skip_btn.setFixedSize(*SKIP_BUTTON_SIZE)
        self.skip_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        # Solid - the owner, 24 August 2026: "make them solid, not
        # transparent at all". BG rather than SURFACE: over a bright
        # frame the near-black panel colour read as see-through even
        # when the window itself was opaque.
        self.skip_btn.setStyleSheet(
            f"QPushButton {{ background: {theme.BG}; color: {theme.TEXT};"
            f" border: 1px solid {theme.BORDER};"
            f" border-radius: {BAR_RADIUS}px; padding: 0px;"
            f" font-size: 11pt; font-weight: 700; }}"
            f"QPushButton:hover {{ background: {theme.ACCENT};"
            f" color: {theme.ON_ACCENT}; border: 1px solid {theme.ACCENT}; }}")
        self.skip_btn.clicked.connect(self._take_skip_offer)
        self.skip_btn.hide()
        use_hover_cursor(self.skip_btn)

        self.status = QLabel("", self)
        _make_native(self.status)
        self.status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status.setWordWrap(True)
        # The same near-black veil the control bar sits under, blended
        # by DWM alpha in _show_status. This was colour-keyed to bare
        # words for a while, and the key is what the owner's screenshot
        # caught failing: on his display the keyed background rendered
        # as a solid black box around the text instead of vanishing
        # (LWA_COLORKEY is unreliable under HDR/some scaling paths,
        # and its failure mode is exactly a black slab). Uniform alpha
        # has no such mode - it can only ever be a soft veil.
        self.status.setStyleSheet(
            f"background: {theme.BG}; color: {theme.TEXT};"
            f" padding: 10px 16px; font-size: 13.5pt; font-weight: 600;")
        self.status.hide()

        self._idle_timer = QTimer(self)
        self._idle_timer.setSingleShot(True)
        self._idle_timer.timeout.connect(self._hide_controls)
        # The loading frame's grace timer - see _show_loading_soon.
        self._pending_loading_text = ""
        self._loading_delay = QTimer(self)
        self._loading_delay.setSingleShot(True)
        self._loading_delay.setInterval(LOADING_FLASH_GUARD_MS)
        self._loading_delay.timeout.connect(self._pending_loading_show)
        # The same frame, for a stall in the middle of a running picture
        # - see _begin_buffer_frame.
        self._buffer_frame_up = False
        self._buffer_delay = QTimer(self)
        self._buffer_delay.setSingleShot(True)
        self._buffer_delay.setInterval(BUFFER_FRAME_GUARD_MS)
        self._buffer_delay.timeout.connect(self._show_buffer_frame)
        # The startup gauge (see _update_startup_status). The fraction
        # only ever rises within one wait - the race can swap which
        # torrent is being read, and a gauge stepping backwards reads as
        # breakage - and the ticker is what keeps it moving between real
        # readings, because those were measured to arrive twice in 14.7s.
        self._startup_fraction = 0.0
        self._startup_text = ""
        self._startup_ticker = QTimer(self)
        self._startup_ticker.setInterval(STARTUP_TICK_MS)
        self._startup_ticker.timeout.connect(self._startup_tick)
        # When a panel last opened, closed or rebuilt - what the click
        # poll holds off for (see _click_toggle / PANEL_CLICK_GUARD_S).
        self._panel_guard = 0.0
        # Which press that hold-off is *about*. Every press the poll sees
        # gets the next number; a panel change records the press it
        # belongs to, and only that one press is swallowed. See
        # _guard_click - a plain stopwatch could not tell the press that
        # opened a panel from the next one meant to close it.
        self._press_seq = 0
        self._guarded_press = -1
        self._pointer_timer = QTimer(self)
        self._pointer_timer.timeout.connect(self._poll_pointer)
        self._last_pointer = QCursor.pos()
        self._mouse_timer = QTimer(self)
        self._mouse_timer.timeout.connect(self._poll_mouse)
        self._save_timer = QTimer(self)
        self._save_timer.timeout.connect(self._save_position)
        # The seat watchdog - its own timer rather than a check inside
        # the time-pos handler, because the case it exists for is
        # exactly the one where time-pos events stop arriving (mpv
        # parked buffering at an offset whose data never comes).
        self._seat_timer = QTimer(self)
        self._seat_timer.setInterval(1000)
        self._seat_timer.timeout.connect(self._seat_tick)

        if parent is not None:
            parent.installEventFilter(self)
            self.setGeometry(parent.rect())

        # The real episode map, asked for once per page. It usually
        # lands inside half a second - well before the stream lookup -
        # which is what lets _apply_meta_bounds catch a request for an
        # episode that does not exist before anything wrong plays.
        # For a film as well now: the same record says whether the title
        # is anime, which is what decides the audio language (the owner's
        # "the KNY Infinity Castle movie was playing in Spanish").
        if self.episode or self.entry.get("imdb_id"):
            self._spawn(self._fetch_meta_worker)

        QTimer.singleShot(0, self._start)

    # ---- setup -------------------------------------------------------
    def _starting_episode(self, season, episode):
        """What to play when the caller did not say.

        Next episode rather than the stored one, but only when the
        stored number is confirmed: an unverified number is the tracker's
        *guess* at the latest episode out (see tracker._progress_display),
        and starting one past a guess would skip an episode nobody has
        watched.

        **And clamped to the season's last existing episode.** Someone
        caught up has a stored number equal to the last one released, so
        "next" is an episode that does not exist yet - measured on the
        owner's Bleach entry, progress S04E04 against a season that ends
        at 4, which asked for S04E05. Nothing downstream refuses that:
        the lookup's numbering fallback used to answer with the same
        episode number out of season 1, so pressing play opened a
        completely different season's episode. That substitution is gone
        now, so the same request fails to "no source" instead - which is
        honest but still not playable. Clamping here is what actually
        makes Play work, and `_clamped_from` is how _start says so out
        loud rather than silently playing something else than asked."""
        self._clamped_from = None
        if episode is not None:
            return season, episode
        try:
            from windows import tracker
        except ImportError:                             # pragma: no cover
            return season, episode
        if not tracker.tracks_progress(self.entry.get("type")):
            return None, None
        stored_season, stored_episode = tracker.parse_episode_progress(
            self.entry.get("progress"))
        if self.entry.get("progress_verified") and stored_episode:
            season = stored_season or 1
            wanted = stored_episode + 1
            # Clamped, never rolled into season+1: the next season's
            # episode 1 is a guess at what the user meant, and playing an
            # unasked-for episode is the failure this whole guard exists
            # to prevent.
            last = max(1, self._season_episode_count(season))
            if wanted > last:
                self._clamped_from = wanted
                return season, last
            return season, wanted
        return stored_season or 1, 1

    def _build_top_bar(self):
        self.top_bar = QWidget(self)
        _make_native(self.top_bar)
        # The same faint near-black veil the control bar wears, blended
        # by DWM alpha (_wake_controls). The colour-keyed bare-text
        # treatment that briefly lived here is gone at the owner's ask
        # ("remove the text and the buttons borders - in black now"):
        # his screenshot shows the key *failing* - the keyed background
        # rendering as solid black boxes around the title and buttons.
        # LWA_COLORKEY is unreliable under HDR and some scaling paths,
        # and that is precisely its failure mode; uniform alpha cannot
        # fail that way, so the veil is what stays.
        self.top_bar.setStyleSheet(f"background: {theme.BG};")
        layout = QHBoxLayout(self.top_bar)
        # 4px above and below, not 6, and 38px buttons rather than the
        # 44px default: the bar is TOPBAR_HEIGHT tall and its tallest
        # child is what sets that, so a thinner bar is these two numbers.
        layout.setContentsMargins(12, 4, 12, 4)
        layout.setSpacing(10)

        # The left edge, and it is deliberately identical to the reader's:
        # the way out, then the list of what else there is to open. Both
        # pages are a full-window take-over reached from a card, so the
        # first two controls answering "how do I get out" and "what else
        # is in this title" in the same place, in the same order, with the
        # same two glyphs is the whole of them reading as one design.
        # A large left arrow now, not the SignOut door (the owner's ask -
        # the same arrow the prev/next episode controls carry), painted
        # rather than set as text, which is what ends the clipped,
        # "scratched" rendering for good (see widgets.GlyphButton; the
        # reader's arrow is the same).
        exit_btn = GlyphButton(ICON_EXIT, "Leave the player (Esc)",
                               size=(38, 38), font_pt=18)
        exit_btn.clicked.connect(self.close_player)
        layout.addWidget(exit_btn)
        self.exit_btn = exit_btn

        self.episodes_btn = _icon_button(ICON_EPISODE_LIST, "Episodes",
                                         size=38, font_pt=14)
        self.episodes_btn.clicked.connect(self.toggle_episode_bar)
        self.episodes_btn.setVisible(bool(self.episode))
        layout.addWidget(self.episodes_btn)

        self.title_label = QLabel("")
        # Pure white (the owner's ask), not the palette's warm-tinted
        # TEXT - see theme.TEXT_OVER_MEDIA. The size rides in both
        # places on purpose: QSS font properties win the resolve, so the
        # stylesheet carries size/weight, while NoAntialias - which QSS
        # cannot express and therefore cannot overwrite - rides on the
        # widget font (see _hard_edge_font).
        self.title_label.setFont(_hard_edge_font(13.5, bold=True))
        self.title_label.setStyleSheet(
            f"color: {theme.TEXT_OVER_MEDIA}; font-size: 13.5pt;"
            f" font-weight: 700; background: transparent;")
        layout.addWidget(self.title_label)
        self._refresh_title_label()

        # Resolution pill - shows the current resolution and opens the
        # Resolution & Source drill-down. The source-name/buffering text
        # trails it, smaller and muted.
        self.res_pill = _pill_button("AUTO", "Resolution & source")
        self.res_pill.clicked.connect(self._open_streams_root)
        layout.addWidget(self.res_pill)

        # Audio-language pill - the current audio track's language, opens
        # the tracks panel. Hidden until a file with audio tracks loads.
        self.audio_pill = _pill_button("AUDIO", "Audio & subtitle tracks")
        self.audio_pill.clicked.connect(self._open_tracks_panel)
        self.audio_pill.setVisible(False)
        layout.addWidget(self.audio_pill)

        self.source_label = QLabel("")
        # White like the title (the owner's ask - TEXT_MUTED is a dark
        # muted grey that disappeared into bright frames); smaller size
        # keeps it reading as secondary. Hard-edged for the same reason
        # as the title, size in QSS for the same reason as the title.
        self.source_label.setFont(_hard_edge_font(10.5))
        self.source_label.setStyleSheet(
            f"color: {theme.TEXT_OVER_MEDIA}; font-size: 10.5pt;"
            f" background: transparent;")
        layout.addWidget(self.source_label)
        layout.addStretch(1)

        # Settings is no longer here - it moved to the control bar, where
        # everything else that acts on playback lives. The top bar is now
        # identity only: door, episode list, title, pills.
        #
        # A *second* episode-list button was being built and added here,
        # left behind when settings moved out: it rebound self.episodes_btn
        # to a duplicate, so the bar carried the same control twice and the
        # first copy answered to nothing.
        self.top_bar.installEventFilter(self)

    def _title_text(self):
        title = self.entry.get("title") or "Playing"
        if self.episode:
            try:
                from windows import tracker
                return f"{title}  -  {tracker.format_episode_progress(self.season or 0, self.episode)}"
            except ImportError:                         # pragma: no cover
                pass
        return title

    def _refresh_title_label(self):
        """The title, elided to a share of the bar it can actually have.

        A QLabel's minimum is its whole string, so a long episode name
        used to push the resolution and audio pills - the "watching"
        labels - clean off the right of the bar. Capped at roughly a
        third of the window and ended in an ellipsis instead, with the
        full name in the tooltip; re-run from _layout_overlays so a
        resize re-fits it."""
        full = self._title_text()
        self.title_label.setToolTip(full)
        metrics = QFontMetrics(self.title_label.font())
        cap = max(220, int(self.width() * 0.34))
        self.title_label.setText(
            metrics.elidedText(full, Qt.TextElideMode.ElideRight, cap))

    def _build_controls(self):
        self.controls = QWidget(self)
        _make_native(self.controls)
        # A faint full-width veil, not a slab: no border, no rounded
        # corners, near-black fill under a low alpha (see _wake_controls)
        # - the closest a uniformly-blended layered window gets to the
        # soft scrim in the owner's reference picture.
        self.controls.setStyleSheet(f"background: {theme.BG};")
        # Two rows now, per that picture: the seek strip with its end
        # times on top, the buttons underneath.
        outer = QVBoxLayout(self.controls)
        outer.setContentsMargins(20, 8, 20, 10)
        outer.setSpacing(4)

        seek_row = QHBoxLayout()
        seek_row.setSpacing(10)
        self.pos_label = QLabel("--:--")
        self.dur_label = QLabel("--:--")
        for label in (self.pos_label, self.dur_label):
            label.setStyleSheet(
                f"color: {theme.TEXT}; font-size: 10.5pt; font-weight: 600;"
                f" background: transparent;")
        self.seek_bar = SeekBar()
        self.seek_bar.seeked.connect(self._seek_absolute)
        seek_row.addWidget(self.pos_label)
        seek_row.addWidget(self.seek_bar, stretch=1)
        seek_row.addWidget(self.dur_label)
        outer.addLayout(seek_row)

        row = QHBoxLayout()
        row.setSpacing(9)

        # Transport, left to right: play, previous, next - no ±10s
        # buttons any more (the owner's ask; ←/→ still seek).
        self.play_btn = PlayPauseButton("Play / Pause (Space)")
        self.play_btn.clicked.connect(self.toggle_pause)
        row.addWidget(self.play_btn)

        self.prev_btn = _icon_button(ICON_PREV, "Previous episode", size=40, font_pt=14)
        self.prev_btn.clicked.connect(lambda: self._change_episode(-1))
        row.addWidget(self.prev_btn)

        self.next_btn = _icon_button(ICON_NEXT, "Next episode", size=40, font_pt=14)
        self.next_btn.clicked.connect(lambda: self._change_episode(1))
        row.addWidget(self.next_btn)

        has_episodes = bool(self.episode)
        self.prev_btn.setEnabled(has_episodes and self.episode > 1)
        # Bounded, not just "does this thing have episodes at all" - the
        # last episode of a season must not offer a next one.
        self.next_btn.setEnabled(
            has_episodes and int(self.episode or 0)
            < self._season_episode_count(self.season))

        self.mute_btn = _icon_button(ICON_VOLUME, "Mute (M)", size=40, font_pt=14)
        self.mute_btn.clicked.connect(self.toggle_mute)
        row.addWidget(self.mute_btn)

        self.volume_slider = QSlider(Qt.Orientation.Horizontal)
        # 0-200 with 100 (the default) landing dead centre: right of
        # centre is soft-gain boost, up to 200%. mpv's volume property
        # takes exactly this range once volume-max is lifted (see _start).
        self.volume_slider.setRange(0, VOLUME_MAX)
        self.volume_slider.setValue(VOLUME_DEFAULT)
        self.volume_slider.setFixedWidth(140)
        self.volume_slider.setStyleSheet(
            f"QSlider::groove:horizontal {{ height: 5px; background: {theme.SURFACE_HOVER};"
            f" border-radius: 2px; }}"
            f"QSlider::sub-page:horizontal {{ background: {theme.ACCENT}; border-radius: 2px; }}"
            f"QSlider::handle:horizontal {{ background: {theme.TEXT}; width: 14px;"
            f" margin: -5px 0; border-radius: 7px; }}")
        use_hover_cursor(self.volume_slider)
        self.volume_slider.valueChanged.connect(self._set_volume)
        row.addWidget(self.volume_slider)

        self.volume_label = QLabel(f"{VOLUME_DEFAULT}%")
        # Fixed width so the row does not shuffle as the number gains a
        # digit (7% -> 100% -> 200%), the same reason the stepper value is
        # fixed. Right-aligned against the slider.
        self.volume_label.setFixedWidth(42)
        self.volume_label.setStyleSheet(
            f"color: {theme.TEXT_MUTED}; font-size: 10.5pt; font-weight: 600;"
            f" background: transparent;")
        row.addWidget(self.volume_label)
        # The middle is empty air now that the seek strip has its own
        # row - transport and volume left, everything else right.
        row.addStretch(1)

        # Opens the speed panel (slider + presets) rather than cycling
        # through a fixed ring: cycling meant six clicks past 2x to get
        # back to 1x, and there was no way to land between the stops.
        self.speed_btn = _text_button("1x", "Playback speed")
        self.speed_btn.clicked.connect(self._open_speed_panel)
        row.addWidget(self.speed_btn)

        self.subs_btn = _icon_button(ICON_SUBTITLES, "Arabic subtitles", size=40, font_pt=14)
        self.subs_btn.clicked.connect(self._open_subtitle_panel)
        row.addWidget(self.subs_btn)

        # Immediately right of the subtitles button, at the owner's ask:
        # the tracks inside the file are the other half of "what text is
        # on screen", so the two doors sit together. This lived as a row
        # in the settings panel for one revision and was impossible to
        # find there.
        self.embedded_btn = _icon_button(ICON_EMBEDDED, "Embedded translation",
                                         size=40, font_pt=14)
        self.embedded_btn.clicked.connect(self._open_tracks_panel)
        row.addWidget(self.embedded_btn)

        # The globe and the resolution screen used to sit here - each was
        # a second door onto a room that already had one. Settings moved
        # down here from the top bar: everything else that acts on
        # playback is on this row, and the top bar is now just identity
        # (title, episode, pills).
        # The globe rather than theme.SETTINGS_ICON's gear, at the
        # owner's ask.
        # Connection statistics - peers, speed, how much of the release
        # has arrived. The owner's ask, 24 August 2026, with a
        # screenshot: a WiFi button opening exactly those three numbers.
        self.stats_btn = _icon_button(ICON_STATS, "Connection statistics",
                                      size=40, font_pt=14)
        self.stats_btn.clicked.connect(self._open_stats_panel)
        row.addWidget(self.stats_btn)

        self.settings_btn = _icon_button(ICON_SETTINGS_GLOBE, "Settings",
                                         size=40, font_pt=14)
        self.settings_btn.clicked.connect(self._open_settings_panel)
        row.addWidget(self.settings_btn)

        # Immediately left of fullscreen, as asked.
        self.download_btn = _icon_button(ICON_DOWNLOAD, "Download this episode",
                                         size=40, font_pt=14)
        self.download_btn.clicked.connect(self._open_download_panel)
        row.addWidget(self.download_btn)

        self.fullscreen_btn = _icon_button(ICON_FULLSCREEN, "Full screen (F)",
                                           size=40, font_pt=14)
        self.fullscreen_btn.clicked.connect(self.toggle_fullscreen)
        row.addWidget(self.fullscreen_btn)

        outer.addLayout(row)
        self.controls.installEventFilter(self)

    # ---- episodes ----------------------------------------------------
    def _episode_count(self) -> int:
        """How many episodes the *playing* season has - what the download
        panel needs (queueing the whole season)."""
        return self._season_episode_count(self.season)

    def _latest_available(self):
        """(season, episode) of how far the release currently goes, or
        (0, 0). Kept by the tracker on the entry."""
        try:
            from windows import tracker
        except ImportError:                             # pragma: no cover
            return 0, 0
        return tracker.parse_episode_progress(self.entry.get("latest_available"))

    def _season_episode_count(self, season) -> int:
        """How many episodes to list for `season`.

        **Cinemeta's real episode list answers once it has arrived**
        (self._meta_aired - the highest *aired* episode per season), and
        everything below is only the holding answer for the first
        half-second before it lands. This is the fix for the reported
        "Season 3 Episode 1" on a two-season show: with only guesses to
        go on, the nav could walk into a season that does not exist and
        offer DEFAULT_SEASON_EPISODES phantom rows there, and playing
        one fell through the absolute-numbering fallback into an old
        episode from season 1.

        Before meta: `latest_available` is how far the release currently
        goes, so it is the honest upper bound - but only for the season
        it actually names. The episode being watched always counts,
        since being able to play it proves it exists."""
        season = int(season or 0)
        # getattr, not plain attribute access: _starting_episode calls
        # this to clamp its answer, and that runs *before* __init__ has
        # assigned self.season/self.episode from what it returns. Reading
        # them directly there is an AttributeError inside the player's
        # constructor - the whole page fails to open.
        playing_season = int(getattr(self, "season", 0) or 0)
        playing_episode = int(getattr(self, "episode", 0) or 0)
        current = playing_episode if season == playing_season else 0
        aired = getattr(self, "_meta_aired", None)
        if aired:
            # A season Cinemeta does not list has no episodes to offer -
            # only what is provably being played counts there.
            return max(int(aired.get(season) or 0), current)
        la_season, la_episode = self._latest_available()
        if la_episode and (not la_season or not season or la_season == season):
            return max(la_episode, current, 1)
        return max(current, DEFAULT_SEASON_EPISODES)

    def _max_season(self) -> int:
        """The highest season worth offering in the ‹ › nav - Cinemeta's
        highest aired season once known, else the one being watched or
        the later one latest_available names."""
        aired = getattr(self, "_meta_aired", None)
        if aired:
            return max(max(aired), int(self.season or 1))
        la_season, _ = self._latest_available()
        return max(int(self.season or 1), int(la_season or 1))

    # ---- the real episode map -----------------------------------------
    def _fetch_meta_worker(self):
        """Cinemeta's episode list for this entry, off the UI thread.
        Never raises; a title it cannot answer for simply keeps the
        latest_available guesses."""
        try:
            from windows import tracker
            imdb_id = tracker._entry_imdb_id(self.entry)
        except Exception:
            imdb_id = self.entry.get("imdb_id")
        if not imdb_id:
            return
        try:
            from helpers import stremio
            meta = stremio.fetch_meta(
                imdb_id, "movie" if self.entry.get("type") == "Movie"
                else "series")
        except Exception:
            logs.exception("episode-map lookup failed")
            meta = None
        if meta and not self._closing:
            self._work.meta_facts.emit({"genres": meta.get("genres") or meta.get("genre") or [],
                                        "country": meta.get("country") or ""})
            if meta.get("videos"):
                self._work.meta_ready.emit(list(meta.get("videos") or []))

    def _on_meta_facts(self, facts):
        """Cinemeta's genres/country landed: decide the audio language
        again if that changes the answer. `alang` was set at open from
        the entry's type alone, and the first track pick may already have
        happened - so when the verdict moves the preference, the pick is
        made once more, on the same once-per-file rule."""
        if self._closing:
            return
        try:
            from helpers import stremio
            verdict = stremio.looks_anime(facts)
        except Exception:
            verdict = False
        before = self._audio_language_preference()
        self._anime_hint = bool(verdict)
        after = self._audio_language_preference()
        if after == before or self.handle is None:
            return
        try:
            self.handle["alang"] = ",".join(after)
        except Exception:
            pass
        self._audio_default_done = False
        self._apply_audio_default()

    def _on_meta(self, videos):
        """Fold Cinemeta's list into {season: highest aired episode} and
        correct the current request if it points past what exists."""
        if self._closing:
            return
        import datetime as _dt
        now = _dt.datetime.now(_dt.timezone.utc)
        aired = {}
        for video in videos or []:
            if not isinstance(video, dict):
                continue
            season = int(video.get("season") or 0)
            number = int(video.get("number") or video.get("episode") or 0)
            if season < 1 or number < 1:
                continue        # specials stay out of the nav's bounds
            stamp = str(video.get("firstAired") or video.get("released") or "")
            try:
                when = _dt.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            except ValueError:
                when = None
            if when is not None and when > now:
                continue
            aired[season] = max(aired.get(season, 0), number)
        if not aired:
            return
        self._meta_aired = aired
        self._meta_videos = [v for v in videos if isinstance(v, dict)]
        self._apply_meta_bounds()
        self._fill_episode_bar()
        self._sync_episode_buttons()

    def _apply_meta_bounds(self):
        """If the episode being looked for does not exist, move to the
        last one that does - out loud.

        Only while nothing has started playing: once a frame is on
        screen, whatever is playing was findable, and yanking it away on
        a metadata arrival would be worse than the mismatch. The case
        this exists for (measured on Wistoria: two seasons of 12, asked
        for S03E01) is caught long before that - meta answers in ~0.3s,
        the stream race takes seconds."""
        aired = self._meta_aired
        if not aired or not self.episode:
            return
        if self._position > 0 and not self._awaiting_first_frame:
            return
        season = int(self.season or 1)
        episode = int(self.episode or 1)
        top_season = max(aired)
        target = None
        if season not in aired:
            # A season that does not exist at all: the nearest earlier
            # real one, at its last aired episode.
            clamp_season = max((s for s in aired if s <= season),
                               default=top_season)
            target = (clamp_season, aired[clamp_season])
        elif episode > aired[season]:
            # The season exists but this episode is past its end - never
            # rolled into season+1, which would be playing something
            # nobody asked for.
            target = (season, aired[season])
        if target is None or target == (season, episode):
            return
        self.season, self.episode = target
        self._panel_season = self.season
        self._given_streams = None
        show_toast(self._toast_anchor(),
                   f"S{season:02d}E{episode:02d} Is Not Out - Playing "
                   f"S{self.season:02d}E{self.episode:02d}")
        self._begin_episode()

    def _build_episode_bar(self):
        """The episode list down the left, built once and refilled.

        Hidden until asked for: it covers a quarter of the frame, and a
        list nobody opened is not worth watching a film behind. Only ever
        built for something with episodes at all - a film has one row and
        it is the one already playing.

        Back on the left at the owner's request (it moved to the right for
        one revision and moved straight back), which is also the side the
        button that opens it sits on in the top bar - see
        _layout_overlays, the only place its position lives. Its two
        corners facing the frame are rounded; the pair against the
        window's left edge stays square."""
        if not self.episode:
            return
        bar = QWidget(self)
        _make_native(bar)
        bar.setStyleSheet(
            f"background: {_bar_style()};{_radius_css(square=('tl', 'bl'))}")
        outer = QVBoxLayout(bar)
        outer.setContentsMargins(14, 14, 10, 14)
        outer.setSpacing(8)

        # Header per the owner's reference: ‹ Prev, a Season dropdown in
        # the middle, Next ›. The dropdown replaces the plain title - a
        # ten-season show should not need ten arrow presses.
        header = QHBoxLayout()
        header.setSpacing(6)
        self.season_prev_btn = self._season_arrow(GLYPH_CHEVRON_LEFT, "Previous season")
        self.season_prev_btn.clicked.connect(lambda: self._change_panel_season(-1))
        header.addWidget(self.season_prev_btn)
        header.addStretch(1)
        # PickCombo, not QComboBox - the same two-click defect the
        # details page's season list had. Qt blocks the first release
        # over a popup for a whole doubleClickInterval and only cancels
        # it on a mouse move that lands on the popup, and the popup
        # takes 166-190ms to appear, so a hand already moving never
        # cancels it. See helpers/widgets.PickCombo.
        self.season_combo = PickCombo()
        self.season_combo.setStyleSheet(
            f"QComboBox {{ background: {theme.SURFACE}; color: {theme.TEXT};"
            f" border: 1px solid {theme.BORDER}; border-radius: {theme.RADIUS}px;"
            f" padding: 6px 14px; font-weight: 700; font-size: 12pt; }}"
            f"QComboBox:hover {{ border: 1px solid {theme.ACCENT}; }}")
        use_hover_cursor(self.season_combo)
        self.season_combo.activated.connect(self._pick_panel_season)
        header.addWidget(self.season_combo)
        header.addStretch(1)
        self.season_next_btn = self._season_arrow(GLYPH_CHEVRON_RIGHT, "Next season")
        self.season_next_btn.clicked.connect(lambda: self._change_panel_season(1))
        header.addWidget(self.season_next_btn)
        outer.addLayout(header)

        self._episode_search = QLineEdit()
        self._episode_search.setPlaceholderText("search episodes")
        self._episode_search.setClearButtonEnabled(True)
        # Debounced like every other search box here: a season list is
        # dozens of rebuilt cards, not something to redo per keystroke.
        self._episode_search_timer = QTimer(self)
        self._episode_search_timer.setSingleShot(True)
        self._episode_search_timer.setInterval(200)
        self._episode_search_timer.timeout.connect(self._fill_episode_bar)
        self._episode_search.textChanged.connect(
            lambda _t: self._episode_search_timer.start())
        outer.addWidget(self._episode_search)

        self._episode_list = QWidget()
        self._episode_list.setStyleSheet("background: transparent;")
        self._episode_list_layout = QVBoxLayout(self._episode_list)
        self._episode_list_layout.setContentsMargins(0, 0, 0, 0)
        self._episode_list_layout.setSpacing(6)
        area = scroll_area(self._episode_list)
        area.setStyleSheet("background: transparent; border: none;")
        area.viewport().setStyleSheet("background: transparent;")
        outer.addWidget(area, stretch=1)

        bar.installEventFilter(self)
        bar.hide()
        self._episode_bar = bar
        self._fill_episode_bar()

    def _fill_episode_bar(self):
        """Rebuild the rows. Called on every episode change so the
        highlight follows what is actually playing - the list is small
        enough that rebuilding it costs less than tracking which row was
        selected last, and a page here rebuilds from scratch anyway
        (.claude/rules/ui.md)."""
        if self._episode_bar is None:
            return
        layout = self._episode_list_layout
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                # setParent(None) as well as deleteLater(), and in that
                # order. deleteLater alone leaves the row a visible child
                # of the list with its old geometry until the event loop
                # gets round to it, and taking it out of the layout means
                # nothing moves it - measured: after one refill the list
                # painted a second "Episode 4" card over the rows below,
                # stretched down the panel. Unparenting removes it from
                # the screen on this line rather than eventually.
                # hide() before the unparent - see details._clear_rows:
                # a queued show landing on a parentless widget becomes a
                # framed desktop window, flashing white.
                widget.hide()
                widget.setParent(None)
                widget.deleteLater()
        self._episode_rows = {}

        season = int(self._panel_season or self.season or 1)
        self._sync_season_combo(season)
        # Read once per refill, not once per row: it parses the same
        # string every time and a season can run to thirty rows.
        self._watched_mark = self._watched_through()
        # Bound the arrows so they cannot walk into a season with no data
        # (below 1, or above the furthest one the release names).
        if hasattr(self, "season_prev_btn"):
            self.season_prev_btn.setEnabled(season > 1)
            self.season_next_btn.setEnabled(season < self._max_season())
        wanted = ""
        if hasattr(self, "_episode_search"):
            wanted = self._episode_search.text().strip().lower()
        # Cinemeta's own rows when they have arrived - name, date and an
        # upcoming flag per episode, like the details page's list (the
        # owner's ask) - and the plain numbered rows before then.
        by_number = {}
        for video in (getattr(self, "_meta_videos", None) or []):
            if int(video.get("season") or 0) != season:
                continue
            number = int(video.get("number") or video.get("episode") or 0)
            if number >= 1:
                by_number[number] = video
        count = max(self._season_episode_count(season),
                    max(by_number) if by_number else 0)
        for number in range(1, count + 1):
            row = self._episode_row(number, by_number.get(number))
            if row is None:
                continue
            if wanted:
                text = row.property("searchText") or ""
                if wanted not in text:
                    continue
            self._episode_rows[number] = row
            layout.addWidget(row)
        layout.addStretch(1)

    def _seasons_for_combo(self):
        aired = getattr(self, "_meta_aired", None)
        if aired:
            return sorted(set(aired) | {int(self.season or 1)})
        return list(range(1, self._max_season() + 1))

    def _sync_season_combo(self, season):
        if not hasattr(self, "season_combo"):
            return
        self.season_combo.blockSignals(True)
        self.season_combo.clear()
        for number in self._seasons_for_combo():
            self.season_combo.addItem(f"Season {number}", number)
        index = self.season_combo.findData(season)
        self.season_combo.setCurrentIndex(max(0, index))
        self.season_combo.blockSignals(False)

    def _pick_panel_season(self, _index):
        target = self.season_combo.currentData()
        if target is None:
            return
        self._panel_season = int(target)
        self._fill_episode_bar()

    def _watched_through(self):
        """(season, episode) the entry's progress has reached, or (0, 0)."""
        try:
            from windows import tracker
        except ImportError:                             # pragma: no cover
            return 0, 0
        season, episode = tracker.parse_episode_progress(
            self.entry.get("progress"))
        return int(season or 0), int(episode or 0)

    def _episode_row(self, number, video=None):
        """One row: number and name, the air date under it, and a badge -
        the details page's list shape, which is what the owner asked this
        panel to become. `video` is Cinemeta's record when it has
        arrived; without it the row is the plain numbered fallback."""
        from windows.reader import _ElidedLabel
        season = int(self._panel_season or self.season or 1)
        # "Currently playing" only when the panel is showing the season
        # that is actually playing - browsing another season must not
        # light up its episode 5 as though it were the one on screen.
        current = (number == int(self.episode or 0)
                   and season == int(self.season or 0))
        watched_season, watched_episode = getattr(self, "_watched_mark", (0, 0))
        watched = bool(watched_episode) and (season, number) <= (watched_season,
                                                                 watched_episode)
        name = str((video or {}).get("name") or (video or {}).get("title")
                   or "").strip()
        title = f"{number}. {name}" if name else f"Episode {number}"
        stamp = str((video or {}).get("firstAired")
                    or (video or {}).get("released") or "")
        date_text, upcoming = "", False
        if stamp:
            import datetime as _dt
            try:
                when = _dt.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                date_text = when.strftime("%b %d, %Y").replace(" 0", " ")
                upcoming = when > _dt.datetime.now(_dt.timezone.utc)
            except ValueError:
                pass

        if current:
            fill, edge, text = theme.ACCENT_SOFT, theme.ACCENT, theme.ACCENT
        elif watched:
            fill, edge, text = theme.SURFACE_HOVER, theme.SUCCESS, theme.TEXT
        else:
            # Borderless at rest (the Harbor pass): the states that mean
            # something keep their edges - accent for playing, SUCCESS
            # for watched - and the plain rows stop competing with them.
            # 1px transparent so no row is a pixel taller than another.
            fill, edge, text = theme.SURFACE_HOVER, "transparent", theme.TEXT
        card = Card(matte=True, hoverable=not upcoming)
        card.setStyleSheet(
            f"QFrame#Card {{ background: {fill};"
            f" border: 1px solid {edge};"
            f" border-radius: {theme.RADIUS}px; }}")
        card.setProperty("searchText", title.lower())
        layout = QHBoxLayout(card)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(8)
        column = QVBoxLayout()
        column.setSpacing(2)
        label = _ElidedLabel(title)
        label.setStyleSheet(
            f"color: {text}; font-size: 12pt;"
            f" font-weight: {700 if current else 500};"
            f" background: transparent; border: none;")
        column.addWidget(label)
        if date_text:
            date = QLabel(date_text)
            date.setStyleSheet(
                f"color: {theme.TEXT_MUTED}; font-size: 9.5pt;"
                f" background: transparent; border: none;")
            column.addWidget(date)
        layout.addLayout(column, stretch=1)
        if upcoming:
            badge = QLabel("UPCOMING")
            badge.setStyleSheet(
                f"color: #0d1206; background: {theme.SUCCESS};"
                f" border-radius: {theme.RADIUS_SM}px; padding: 2px 8px;"
                f" font-weight: 700; font-size: 8.5pt;")
            layout.addWidget(badge)
        elif current or watched:
            # A colour alone is not enough of a marker on a list where
            # every row already has a border - the glyph is what makes
            # "which one is playing" and "which ones have I seen"
            # answerable at a glance.
            mark = QLabel(ICON_PLAY if current else ICON_WATCHED)
            mark.setStyleSheet(
                f"color: {theme.ACCENT if current else theme.SUCCESS};"
                f" font-family: {theme.FONT_STACK_ICONS};"
                f" font-size: 11.5pt; background: transparent; border: none;")
            layout.addWidget(mark)
        if not upcoming:
            card.clicked.connect(
                lambda checked=False, n=number: self._pick_episode(n))
            # Right-click marks watched/unwatched - on the episode you
            # are actually looking at, rather than a number on a form.
            card.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            card.customContextMenuRequested.connect(
                lambda pos, n=number, c=card: self._episode_menu(c, pos, n))
        return card

    def _episode_menu(self, card, pos, number):
        """Mark this episode watched, or unwatched.

        "Watched" means progress reaches this episode; "unwatched" means
        it stops just before it - so marking episode 5 unwatched leaves
        the entry on episode 4, which is what someone means when they say
        they have not seen 5. Both go through tracker.correct_progress,
        the one writer allowed to move a number down; the automatic
        write-back stays forward-only."""
        try:
            from windows import tracker
        except ImportError:                             # pragma: no cover
            return
        season = int(self._panel_season or self.season or 1)
        number = int(number)
        watched_season, watched_episode = tracker.parse_episode_progress(
            self.entry.get("progress"))
        already = (watched_season, watched_episode) >= (season, number)

        menu = QMenu(card)
        mark = menu.addAction("Mark as Unwatched" if already else "Mark as Watched")
        menu.addSeparator()
        # The whole season at once - the owner's ask, mirroring the
        # reader's "mark all" pair. "All watched" is this season's last
        # episode; "all unwatched" steps back to the season before it.
        mark_all = menu.addAction("Mark All as Watched")
        clear_all = menu.addAction("Mark All as Unwatched")
        # Positioned from the widget that was clicked, never mapToGlobal
        # on a parent - see .claude/rules/ui.md.
        chosen = menu.exec(card.mapToGlobal(pos))
        if chosen is mark_all:
            tracker.correct_progress(
                self.entry, season=season,
                episode=self._season_episode_count(season))
        elif chosen is clear_all:
            if season > 1:
                tracker.correct_progress(
                    self.entry, season=season - 1,
                    episode=self._season_episode_count(season - 1))
            else:
                # Season 1 unwatched means nothing watched at all -
                # cleared directly, since correct_progress refuses a
                # zero episode.
                self.entry["progress"] = ""
                self.entry["progress_verified"] = False
                try:
                    storage.update_entry(
                        tracker._progress_data_file(self.entry),
                        self.entry.get("id"),
                        {"progress": "", "progress_verified": False,
                         "updated_at": storage.now_iso()})
                except Exception:
                    logs.exception("Could not clear watch progress")
        elif chosen is mark:
            if already:
                # Everything before this episode stays seen; this one
                # does not.
                if number <= 1:
                    target_season, target_episode = max(season - 1, 1), 0
                else:
                    target_season, target_episode = season, number - 1
                if not target_episode:
                    return
            else:
                target_season, target_episode = season, number
            tracker.correct_progress(self.entry, season=target_season,
                                     episode=target_episode)
        else:
            return
        self._fill_episode_bar()
        self._sync_episode_buttons()

    def _season_arrow(self, glyph, tooltip):
        """A small square chevron button for the season selector - the
        text-font glyph, so it sits on the heading's baseline.

        Framed rather than bare at the owner's request: transparent with
        no border, a lone ‹ beside a heading reads as decoration, and the
        only thing saying it was pressable was a hover fill nobody sees
        until they are already hovering it. A filled, bordered square is
        the same shape the stepper's +/- buttons carry, which is the
        panel's existing vocabulary for "small discrete control"."""
        button = QPushButton(glyph)
        button.setToolTip(tooltip)
        # 36, up from 30, with the rest of the list (the owner's ask).
        button.setFixedSize(36, 36)
        button.setStyleSheet(
            # padding:0 - see PlayPauseButton; without it the app-wide
            # 8px 16px padding clips the ‹ › glyph to nothing on a small
            # button, which is exactly how the arrows first went missing.
            f"QPushButton {{ background: {theme.SURFACE_HOVER}; color: {theme.TEXT};"
            f" border: 1px solid {theme.BORDER}; padding: 0px; font-size: 18pt;"
            f" font-weight: 700; border-radius: {theme.RADIUS_SM}px; }}"
            f"QPushButton:hover {{ background: {theme.ACCENT_SOFT};"
            f" border: 1px solid {theme.ACCENT}; }}"
            f"QPushButton:pressed {{ background: {theme.SURFACE_ACTIVE}; }}"
            f"QPushButton:disabled {{ color: {theme.TEXT_DIM};"
            f" background: transparent; border: 1px solid {theme.BORDER}; }}")
        use_hover_cursor(button)
        return button

    def _change_panel_season(self, delta):
        """Move the panel to another season without touching playback.
        Nothing plays until an episode row is actually chosen."""
        target = int(self._panel_season or self.season or 1) + delta
        if target < 1 or target > self._max_season():
            return
        self._panel_season = target
        self._fill_episode_bar()

    def _pick_episode(self, number):
        target_season = int(self._panel_season or self.season or 1)
        if (not self.episode or (int(number) == int(self.episode)
                                 and target_season == int(self.season or 0))):
            self.toggle_episode_bar()
            return
        # The same instant reset Next gets - this path used to leave the
        # outgoing episode playing under the lookup too, and it did not
        # even hand its torrent back. See _leave_playback.
        self._leave_playback("Loading the episode...")
        self.season = target_season
        self.episode = int(number)
        self.prev_btn.setEnabled(self.episode > 1)
        self._given_streams = None      # the handed-in list was for the old episode
        self._fill_episode_bar()
        self._begin_episode()

    def toggle_episode_bar(self):
        if self._episode_bar is None:
            return
        if self._episode_bar.isVisible():
            self._episode_bar.hide()
            self._wake_controls()
            return
        # Open on the season actually playing, wherever the panel was left.
        self._panel_season = self.season
        self._fill_episode_bar()
        self._layout_overlays()         # geometry first - see _show_status
        self._episode_bar.show()
        self._episode_bar.raise_()
        _set_window_alpha(self._episode_bar, BAR_ALPHA)
        self._wake_controls()

    # ---- start-up ----------------------------------------------------
    def _start(self):
        if not video_backend.available():
            self._show_status(
                f"{video_backend.unavailable_reason()}\n\n"
                "Run this from the project folder and reopen the player:\n"
                "python packaging/fetch_libmpv.py")
            self.controls.hide()
            return
        try:
            self.handle = video_backend.create(self.surface.native_handle())
        except video_backend.PlayerError as error:
            logs.exception("Could not create the mpv instance")
            self._show_status(f"The player could not start.\n\n{error}")
            self.controls.hide()
            return

        for name in ("time-pos", "duration", "pause", "demuxer-cache-time",
                     "track-list", "volume", "mute", "speed", "core-idle",
                     "paused-for-cache", "cache-buffering-state", "path"):
            self.handle.observe_property(name, self._mpv_property)
        self.handle.register_event_callback(self._mpv_event)
        # Both levers, and the delay too - an .ass needs sub-scale
        # rather than sub-font-size (see _apply_sub_style), and the
        # delay used not to be applied at all until the first nudge.
        self._apply_sub_style()
        self._apply_sub_delay()
        # And the height, for the same reason the delay is here: a track
        # loaded after the owner nudged it must come up where they put
        # it, not back at the bottom.
        self._apply_sub_position()
        # Lift mpv's soft-volume ceiling from its 130 default so the
        # slider's right half can boost to 200% (see the volume slider).
        try:
            self.handle["volume-max"] = VOLUME_MAX
        except Exception:
            logs.exception("Could not raise volume-max")
        # Auto-select an Arabic subtitle track when the file carries one.
        # Multi-sub releases (ToonsHub, Erai-raws) ship Arabic inside the
        # container, and without a language preference mpv picks the
        # default track - almost always English signs - so the Arabic
        # that was already downloaded sat unselected behind a menu. This
        # is the single cheapest source of Arabic there is: no request,
        # no translation, already on screen.
        try:
            self.handle["slang"] = "ar,ara,arb"
        except Exception:
            logs.exception("Could not set the subtitle language preference")
        # And the audio side of the same idea, the owner's ask: anime
        # opens on its Japanese track, films and series on English.
        # Written here rather than in video_backend.default_options
        # because it depends on *this entry's* type, and the player is
        # per-entry. `alang` is a preference list mpv applies at each
        # file open - a release carrying neither language keeps mpv's
        # own choice, so this can only ever improve the guess. The
        # tracks panel overrides it as it always did; see
        # _apply_audio_default for why that override survives.
        try:
            self.handle["alang"] = ",".join(self._audio_language_preference())
        except Exception:
            logs.exception("Could not set the audio language preference")

        self._pointer_timer.start(POINTER_POLL_MS)
        self._mouse_timer.start(MOUSE_POLL_MS)
        self._save_timer.start(POSITION_SAVE_MS)
        self._wake_controls()
        self.setFocus()
        self._begin_episode()
        # Said out loud, not swallowed: the player was asked for the
        # episode after the last one watched and that one is not out, so
        # it is playing something other than what Play meant. Announced
        # after _begin_episode so it lands over the source lookup rather
        # than being replaced by it.
        if getattr(self, "_clamped_from", None):
            show_toast(self._toast_anchor(),
                       f"Episode {self._clamped_from} Is Not Out Yet - "
                       f"Playing Episode {self.episode}")
            self._clamped_from = None

    def _fetch_logo_worker(self, run):
        """The title's backdrop and logo, off the UI thread.

        The backdrop is fetched first and on its own: it is the one that
        is nearly always there (measured - Wistoria has a backdrop and no
        logo at all), it costs one request against the logo's three to
        five, and it is what turns the source search from an empty box
        into a loading screen. Waiting for the logo to decide whether to
        draw the backdrop would put the cheap one behind the expensive
        one for no reason.

        Never raises: a dead worker here would be invisible, and the
        loading screen simply keeps its text. Both artwork lookups
        already fail soft to None for a missing key, an unknown title, or
        a title TMDB has no artwork for."""
        for kind, fetch in (("backdrop", artwork.backdrop_path),
                            ("logo", artwork.logo_path)):
            try:
                path = fetch(self.entry)
            except Exception:
                logs.exception(f"{kind} lookup failed")
                continue
            if path:
                self._work.logo_ready.emit(f"{kind}:{path}", run)

    def _on_logo(self, payload, run):
        if run != self._run or self._closing:
            return
        kind, _, path = str(payload).partition(":")
        loading = self._status_visible() or self._loading_visible
        if kind == "backdrop":
            if self.backdrop.set_backdrop(path) and loading:
                self._show_backdrop()
            return
        if self.logo.set_logo(path):
            if loading:
                self._show_backdrop()
            self._update_startup_status()

    def _begin_episode(self):
        """Everything that has to happen for one episode: find something
        to play, and look for subtitles for it."""
        # Bring the torrent session and DHT up while the addon lookup is
        # still running. Bootstrapping costs several seconds and is pure
        # dead time if it only starts once a source has been chosen -
        # measured as most of the gap between the first play of a
        # session and every later one.
        try:
            from helpers import torrent_engine
            torrent_engine.prewarm()
        except Exception:
            pass
        self._run += 1
        # The previous episode's subtitle toast goes with it. It is
        # sticky on purpose (a translation is minutes, not moments - see
        # _pick_subtitle), and everything that would have finished it is
        # guarded on the run number that just moved - so a translation
        # abandoned by pressing Next left "Translating to Arabic..." on
        # screen until the whole page closed.
        self._drop_sub_toast()
        # A new episode gets the remembered subtitle applied again -
        # its own search is what _auto_apply_subtitle matches against.
        self._sub_auto_done = False
        self._pending_sub_choice = None
        # And its own search, deferred to the first frame - see
        # _ensure_subtitle_search.
        self._subs_search_started = False
        # Fetched per episode rather than once per page: it is cached on
        # disk by title after the first call, so this costs nothing
        # after the first episode and still works when the page was
        # opened straight onto a later one.
        # Spawned *after* the run number moves, never before. It used to
        # be handed the outgoing run, so _on_logo compared it against the
        # new one and dropped every backdrop and logo ever fetched - the
        # loading frame had silently been text-only since the run guard
        # was added, which is what "the TMDB images still aren't working"
        # was.
        self._spawn(self._fetch_logo_worker, self._run)
        self._marked_watched = False
        self._arabic_track_done = False
        self._prefetched = False
        # The warm-up done for *this* episode has served its purpose (or
        # not); either way the next one owns the question now. Nothing is
        # released here - the release just warmed is very likely the one
        # this episode is about to play, and _load_into_mpv drops it if
        # it turns out not to be.
        self._prewarm_hash = None
        self._prewarm_asked = False
        # The metadata-only half of the next-episode warm, which
        # runs on its own (earlier) condition - see
        # _maybe_prewarm_next.
        self._meta_prewarm_asked = False
        self._prewarm_checked = 0.0
        # A new episode has its own opening, its own chapters and its own
        # AniSkip row - keeping the last one's would offer to skip into
        # the middle of this one.
        self._skips = []
        self._skips_asked = False
        # Whether the crowd lookup has reported for this episode - see
        # _current_skip, which holds a 0:00 chapter opening until it has.
        self._skips_answered = False
        # A resolution the user pinned by hand. Cleared *here*, at an
        # episode boundary, and nowhere else on the success path - see
        # _on_stream_prepared for the report that made that the rule.
        self._requested_quality = None
        # Whether this episode has already had to leave that resolution
        # because nothing at it would start (see _try_next_source).
        self._quality_given_up = False
        self._skip_offer = None
        # The previous episode's switch or seek is over whatever its
        # outcome was; a sticky toast about it would otherwise narrate
        # the old episode over the new one.
        self._clear_seat()
        self._finish_working()
        try:
            self.skip_btn.hide()
        except (AttributeError, RuntimeError):
            pass
        self._duration = 0.0
        self._position = 0.0
        # Owed a fresh first frame from here on - set at the switch, not
        # only when mpv loads, so the delayed loading frame can ask "has
        # it arrived yet" across the whole lookup as well (see
        # _show_loading_soon).
        self._awaiting_first_frame = True
        self.seek_bar.set_duration(0)
        self.seek_bar.set_position(0)
        self.seek_bar.set_buffered(0)
        self._refresh_title_label()
        self._fill_episode_bar()
        # The outgoing episode's sources, and everything that indexes
        # into them. Cleared here rather than in _on_streams because
        # that now arrives several times per episode (partial batches),
        # so it can no longer be the thing that starts a fresh list.
        # _dead_sources especially: an index proven dead in the previous
        # episode would otherwise skip a live source in this one.
        self._streams = []
        self._streams_started = False
        self._stream_index = 0
        self._dead_sources = set()
        # Info hashes a race has already proved dead this episode. The
        # indices in _dead_sources cannot carry this: one race tries
        # many releases and reports one index (see _try_next_source).
        self._dead_hashes = set()
        self._streams_view = None
        self._subtitles = []
        self._set_subtitle_count(None)

        if self._given_streams is not None:
            # A finished list handed in by the details page's picker -
            # there is no lookup behind it, so nothing more is coming.
            self._on_streams(self._given_streams, self._run, False)
        elif streams_module is None:
            self._show_status("No stream sources are available in this build.")
        else:
            self._show_loading_soon("Looking for a source...")
            self._spawn(self._find_streams_worker, self._run)
        # Deliberately no subtitle search here any more - it used to
        # start beside the source lookup and competed with it for the
        # connection while the owner was staring at the loading frame
        # ("make the auto subtitles load after the source loading
        # end"). _ensure_subtitle_search runs it at the first frame
        # instead, and on demand the moment either panel that lists
        # subtitles is opened - so picking by hand never waits on the
        # deferral.

    # ---- background work ---------------------------------------------
    def _spawn(self, target, *args):
        thread = threading.Thread(target=target, args=args, daemon=True)
        thread.start()

    def _find_streams_worker(self, run):
        """Never raises: an exception here would kill the thread silently
        and the page would sit on "Looking for a source..." forever.

        Each source's releases are handed over as they land, not once
        the whole fan-out is done - measured on the owner's entries, the
        first addon answers in 0.3-0.8s while the slowest source finishes
        1.9-2.6s later, and up to the whole budget when one hangs. Every
        batch is the full ranked list so far, so _on_streams either
        starts on it or folds it into the list already playing."""
        def partial(found):
            if not self._closing and run == self._run:
                self._work.streams_ready.emit(list(found or []), run, True)

        try:
            found = streams_module.find_streams(
                self.entry, season=self.season, episode=self.episode,
                deadline=net.deadline_in(STREAM_BUDGET_S),
                on_partial=partial)
            self._work.streams_ready.emit(list(found or []), run, False)
        except Exception:
            logs.exception("Stream lookup failed")
            self._work.streams_ready.emit([], run, False)

    def _ensure_subtitle_search(self):
        """Start this episode's subtitle search, once.

        Called from the first frame (the earliest moment the search can
        no longer slow the path to picture) and from the subtitle and
        download panels (the two places that show its results), so a
        panel opened before the source has loaded still gets a search
        under way immediately."""
        if self._subs_search_started or self._closing:
            return
        self._subs_search_started = True
        self._search_subtitles()

    def _search_subtitles(self):
        if subtitles_module is None:
            return
        self._spawn(self._subtitles_worker, self._run)

    def _subtitles_worker(self, run):
        try:
            from windows import tracker
            imdb_id = tracker._entry_imdb_id(self.entry)
            kind = "series" if self.episode else "movie"
        except Exception:
            imdb_id, kind = self.entry.get("imdb_id"), "movie"
        def partial(found):
            # Each source's answers as they land, so the panel fills in
            # under a second instead of after four (see subtitles.search
            # for the measurement). Every batch is the full ranked list
            # so far, so the panel just redraws.
            if not self._closing and run == self._run:
                self._work.subs_ready.emit(list(found or []), run)

        try:
            found = subtitles_module.search(
                self.entry.get("title") or "", year=self.entry.get("year"),
                season=self.season, episode=self.episode, imdb_id=imdb_id,
                kind=kind, deadline=net.deadline_in(SUBTITLE_BUDGET_S),
                on_partial=partial, release=self._playing_release())
            self._work.subs_ready.emit(list(found or []), run)
        except Exception:
            logs.exception("Subtitle search failed")
            self._work.subs_ready.emit([], run)

    def _release_record(self):
        """What is playing, in the shape the resume file stores - see
        save_resume. Info hash and stream_key both, because the key is
        derived from the row's title and the same release listed by two
        addons can word it differently."""
        try:
            stream = self._streams[self._stream_index] or {}
        except Exception:
            return None
        key = ""
        if streams_module is not None:
            try:
                key = streams_module.stream_key(stream)
            except Exception:
                key = ""
        info_hash = (stream.get("info_hash") or "").lower()
        if not key and not info_hash:
            return None
        return {"key": key, "info_hash": info_hash,
                "file_index": stream.get("file_index"),
                "title": (stream.get("title") or "")[:160]}

    def _hoist_remembered_release(self, playable) -> bool:
        """Put the release this episode was last watched with first.

        Returns whether it was found, because that also decides *how* it
        is started: found means play it alone (the same road a hand pick
        takes), not race it against five others that would otherwise be
        free to win. A release that is genuinely dead still costs only
        SOLO_METADATA_TIMEOUT + SOLO_DATA_WAIT before _try_next_source
        races the rest, so continuing on a swarm that has since died is
        eight seconds slower and then behaves exactly as before."""
        if streams_module is None:
            return False
        try:
            record = load_resume(self.entry, self.season, self.episode) or {}
        except Exception:
            return False
        release = record.get("release") or {}
        info_hash = (release.get("info_hash") or "").lower()
        key = release.get("key") or ""
        if not info_hash and not key:
            return False
        for position, stream in enumerate(playable):
            same_hash = bool(info_hash) and (
                (stream.get("info_hash") or "").lower() == info_hash)
            same_key = False
            if key and not same_hash:
                try:
                    same_key = streams_module.stream_key(stream) == key
                except Exception:
                    same_key = False
            if same_hash or same_key:
                if position:
                    playable.insert(0, playable.pop(position))
                return True
        return False

    def _playing_release(self) -> str:
        """The name of the release currently loaded, or "".

        Handed to `subtitles.search` so a subtitle cut against this same
        encode outranks one cut against a different distributor - see
        subtitles._timeline_rank for the owner's report this answers.
        Read at search time rather than remembered: the search can start
        after a source switch, and the release that matters is the one
        actually playing."""
        try:
            stream = self._streams[self._stream_index]
        except (IndexError, TypeError, AttributeError):
            return ""
        title = stream.get("title") or stream.get("name") or ""
        # The first line only: an addon's title carries seeders, size and
        # the language list under the release name, and none of those say
        # anything about which cut this is.
        return str(title).splitlines()[0].strip() if str(title).strip() else ""

    def _fetch_subtitle_worker(self, result, run, provider=None, label=None):
        """Fetch, then write UTF-8 to a temp file and hand mpv the path.

        mpv is never given the url. Many of these hosts need the same
        Referer the stream did, and an Arabic .srt is very often
        Windows-1256 - helpers/subtitles.fetch has already decoded it,
        and re-encoding as UTF-8 here means libass reads real text
        instead of one long line of mojibake.

        **A translation happens only when the picked row promised one.**
        The rule used to be "any non-Arabic pick goes through the
        translator", with the provider chosen here from
        `default_provider()`. Two things were wrong with that: a row
        filed under "Other Languages" came back in Arabic, and the
        provider was never the user's to choose. Now the row carries the
        provider or carries none, and this only does what it was told.

        **If the Arabic never arrives, nothing is loaded.** The measured
        bug behind this (Bleach TYBW S04E01, 21 August 2026): all four of
        the owner's providers refused - three out of credit, Gemini out
        of free-tier quota - the failure was emitted as a note, and then
        this method carried straight on and loaded the *untranslated
        English*, whereupon _on_subtitle_file overwrote the note with
        "Subtitle Loaded". The owner's words were "it shows Subtitles
        loaded then changes the Subtitles to the EN Opensource". A pick
        that asked for Arabic and produced none must fail and say why;
        the English row is one line further down the panel for anyone
        who wants it, and whatever track was already up is left alone."""
        try:
            text = subtitles_module.fetch(result, net.deadline_in(SUBTITLE_BUDGET_S))
            if not text:
                self._work.failed.emit("That Subtitle Could Not Be Downloaded", run)
                return
            fmt = result.get("format") or "srt"
            # The same name the row carried (see _name_subtitles), so
            # the loaded track and the list agree on what was picked.
            label = (label or result.get("display_name")
                     or result.get("release") or "Subtitle")
            if provider:
                if ai_translate is None:
                    self._work.failed.emit(
                        "AI Translation Is Not Available In This Build", run)
                    return
                name = ai_translate.label(provider)
                if not app_settings.get_api_key(provider):
                    # The key went away between the panel being drawn and
                    # the row being pressed. Say that rather than quietly
                    # handing over the source language.
                    self._work.failed.emit(
                        f"AI Translation Failed - No {name} Key Is Configured",
                        run)
                    return
                self._work.note.emit(f"Translating to Arabic With {name}...",
                                     run)
                cues = subtitles_module.parse(text, fmt)
                if not cues:
                    # Zero cues is the one failure that looks like
                    # success: translate() has nothing to do, returns
                    # None, and the untranslated file used to sail
                    # through. See subtitles._parse_ass for how a real
                    # file parses to nothing.
                    self._work.failed.emit(
                        "That Subtitle Had No Readable Lines To Translate", run)
                    return
                translated = None
                try:
                    # **Say how far along it is.** A full episode is
                    # hundreds of cues in batches, which is minutes; the
                    # owner saw the "Translating..." line, then nothing,
                    # and reasonably read a long silence as a failure.
                    # `progress` has always been on translate() and was
                    # simply never passed. Emitted through the note
                    # signal, so the worker thread never touches a
                    # widget (rules/integrations.md).
                    def told(done, total):
                        self._work.note.emit(
                            f"Translating to Arabic With {name}... "
                            f"{done}/{total}", run)

                    # fallback=False: the row named this provider, so a
                    # different one answering would make the label a lie.
                    translated = ai_translate.translate(
                        cues, provider=provider, fallback=False, progress=told,
                        cancelled=lambda: self._closing or run != self._run)
                except ai_translate.TranslationFailed as failure:
                    # **Say which provider said no, and what it said.**
                    # This used to be a bare "Translation Failed" for
                    # every cause, which is how four pasted keys - three
                    # accounts out of credit, one naming a model Google
                    # had retired - looked like a dead feature instead of
                    # four different billing problems.
                    logs.warning(f"AI translation failed: {failure.reason}")
                    self._work.failed.emit(
                        f"AI Translation Failed - {failure.reason}", run)
                    return
                if not translated:
                    # translate() returns None for a cancelled job - the
                    # episode was left, and _on_subtitle_file would drop
                    # the file anyway. Nothing to load, nothing to say.
                    return
                text, fmt = ai_translate.to_srt(translated), "srt"
            path = self._write_subtitle(text, fmt)
            self._work.sub_file_ready.emit(path, label, run)
        except Exception:
            logs.exception("Subtitle download failed")
            self._work.failed.emit("That Subtitle Could Not Be Downloaded", run)

    def _write_subtitle(self, text, fmt):
        if self._temp_dir is None:
            self._temp_dir = tempfile.mkdtemp(prefix="atomic-subs-")
        suffix = "ass" if str(fmt).lower() in ("ass", "ssa") else "srt"
        path = os.path.join(self._temp_dir, f"{uuid.uuid4().hex}.{suffix}")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path

    # ---- results back on the UI thread -------------------------------
    def _on_streams(self, found, run, looking=False):
        """One batch of sources - a partial one, or the finished list.

        `find_streams` reports every source as it answers, so this runs
        several times per episode. Three cases: playback has already
        started and the batch only adds to the list; nothing has started
        and the batch is worth starting on; or nothing has started and it
        is not, in which case it is held and the next batch is awaited.
        An empty list is always the final answer - the fan-out never
        reports a batch it does not have."""
        if self._closing or run != self._run:
            return
        incoming = list(found or [])
        if self._streams_started:
            self._merge_streams(incoming)
            return
        if not incoming and looking:
            return              # the fan-out never reports an empty batch
        if not incoming:
            self._show_status(
                "No playable source was found for this episode.\n"
                "Try again, or open it on its site.")
            return
        drm = [s for s in incoming if s.get("kind") == "drm"]
        playable = [s for s in incoming if s.get("kind") != "drm"]
        # Before the list is published, so index 0 is the remembered
        # release and every index _play_stream/_dead_sources uses is
        # taken from the final order.
        self._resumed_release_first = self._hoist_remembered_release(playable)
        self._streams = playable + drm
        if not playable:
            # DRM only. The DRM row is put in before the fan-out starts,
            # so a batch holding nothing else means that source answered
            # nothing yet - a later one still might, so only the
            # finished list is allowed to say so out loud.
            if not looking:
                self._show_drm(drm[0])
            return
        # A partial that does not yet carry the preferred resolution is
        # held rather than played: starting on the first batch to arrive
        # would start 720p while the 1080p the user asked for was two
        # tenths of a second behind it, and that plays for the whole
        # episode. See streams.has_preferred_resolution.
        if (looking and streams_module is not None
                and not streams_module.has_preferred_resolution(playable)):
            self._refresh_streams_panel()
            return
        self._streams_started = True
        # A list handed in by the details page is a source the user
        # picked by hand, with their choice at index 0 - start that
        # one alone rather than racing five others against it.
        self._play_stream(0, solo=(self._given_streams is not None
                                   or self._resumed_release_first))

    def _merge_streams(self, incoming):
        """Fold a later batch into the list playback is already using.

        Replacing the list is the obvious move and it is wrong:
        `_stream_index` points into it, `_dead_sources` holds its
        indices, and `_on_stream_prepared` writes the prepared stream
        back *by index*. A re-ranked list moves all three under them, so
        a late source answering would silently switch which release is
        playing. New rows go on the end instead, ahead of the DRM rows
        that have to stay last - every existing playable index is
        untouched by that."""
        if streams_module is None:
            return          # nothing can have produced a second batch
        known = {streams_module.stream_key(s) for s in self._streams}
        # **Also by info hash, not only by stream_key.** `stream_key`
        # prefers a row's `url`, and `_on_stream_prepared` writes the
        # *prepared* stream (which has one) back over the row it started
        # from - so from that moment the release actually playing has a
        # different key than the row a later partial batch carries for
        # the same release, and it was re-added here as a fresh,
        # unselected row. Picking that row is a switch to the file
        # already open, which is exactly the identical-path hang
        # _switch_stream documents. Same release, same hash, one row.
        known_hashes = {(s.get("info_hash") or "").lower()
                        for s in self._streams if s.get("info_hash")}
        added = [s for s in incoming
                 if s.get("kind") != "drm"
                 and streams_module.stream_key(s) not in known
                 and (s.get("info_hash") or "").lower() not in known_hashes]
        if not added:
            return
        cut = len(self._streams)
        while cut and self._streams[cut - 1].get("kind") == "drm":
            cut -= 1
        self._streams[cut:cut] = added
        self._refresh_streams_panel()

    def _refresh_streams_panel(self):
        """Redraw the Resolution & Source panel in place if it is open,
        so a source that answers late appears in a list already being
        read. rebuild=True for the reason _drill_streams gives - without
        it the reopen hits the same-kind toggle and closes the panel."""
        if self._panel is not None and getattr(self._panel, "kind", "") == "streams":
            self._open_streams_panel(rebuild=True)

    def _on_subtitles(self, found, run):
        if self._closing or run != self._run:
            return
        self._subtitles = _name_subtitles(found or [])
        self._set_subtitle_count(len([s for s in self._subtitles
                                      if str(s.get("lang", "")).lower().startswith("ar")]))
        if self._panel is not None and getattr(self._panel, "kind", "") == "subs":
            self._open_subtitle_panel(rebuild=True)   # in place, with the results
        self._auto_apply_subtitle()

    def _auto_apply_subtitle(self):
        """Load the subtitle this title was last watched with.

        The owner's ask, 22 August 2026: "when I select a translation and
        I close the app then re-open it later, make it remember which
        translation I selected and auto load it."

        Runs on every batch of results (subtitles.search reports
        progressively), and gives up the moment it finds a match or the
        user picks anything by hand - `_sub_auto_done` is set by
        _pick_subtitle and _subtitles_off as well, so an automatic pick
        can never land on top of a deliberate one a beat later.

        Fails soft in every direction: no stored choice, nothing in this
        episode's results that matches it, or a translator whose key has
        since been removed - all of them simply leave playback alone,
        which is the behaviour every episode had before this existed."""
        if self._sub_auto_done or self._closing or not self._subtitles:
            return
        try:
            stored = load_subtitle_choice(self.entry)
        except Exception:
            logs.exception("Could not read the remembered subtitle")
            return
        if not stored:
            self._sub_auto_done = True      # nothing to wait for
            return
        match = self._match_subtitle_choice(stored)
        if match is None:
            return          # a later batch may still carry it
        result, provider, label = match
        self._sub_auto_done = True
        self._pick_subtitle(result, provider=provider, label=label)

    def _match_subtitle_choice(self, stored):
        """`(result, provider, label)` for a remembered choice, or None.

        Matched on language and source, never on the row's name or its
        url: `_name_subtitles` numbers rows by their position in *this*
        search ("Arabic 2 SubDL"), so the same subtitle is called
        something different whenever a source answers in a different
        order, and the urls are temporaries."""
        want_lang = str(stored.get("lang") or "").lower()
        want_source = str(stored.get("source") or "").lower()
        provider = stored.get("provider") or None

        if provider:
            # An AI-translated row: the stored provider names the
            # translator and the *source* names what it was translated
            # from. Only offered while that key is still configured -
            # _fetch_subtitle_worker would otherwise fail on every
            # episode with a message the user cannot act on from here.
            try:
                if ai_translate is None or provider not in ai_translate.providers_available():
                    self._sub_auto_done = True
                    return None
            except Exception:
                self._sub_auto_done = True
                return None
            feedstock = [s for s in self._subtitles
                         if not str(s.get("lang", "")).lower().startswith("ar")]
            best = next((s for s in feedstock
                         if str(s.get("source") or "").lower() == want_source),
                        None) or (feedstock[0] if feedstock else None)
            if best is None:
                return None
            translator = ai_translate.label(provider)
            index = feedstock.index(best) + 1
            # The same string the panel builds for this row, so the
            # highlight lands back on it (see _open_subtitle_panel).
            return best, provider, f"Arabic (AI) {index} {translator}"

        same_lang = [s for s in self._subtitles
                     if str(s.get("lang") or "").lower() == want_lang]
        if not same_lang:
            return None
        best = next((s for s in same_lang
                     if str(s.get("source") or "").lower() == want_source),
                    same_lang[0])
        return best, None, (best.get("display_name") or best.get("release")
                            or "Subtitle")

    def _on_subtitle_file(self, path, label, run):
        if self._closing or run != self._run or self.handle is None:
            return
        try:
            # **Asynchronous, like every other track change.** `sub_add`
            # is a synchronous command through mpv's core, and the core
            # can be parked in a demuxer read on a torrent-backed stream
            # - so a subtitle pick could hold the UI thread, and with it
            # every paint, for as long as the swarm took. The visibility
            # write goes the same way. The style and delay setters are
            # property writes of their own and stay as they were: the
            # Map of 23 August 2026 measured a synchronous libmpv get
            # never exceeding 100ms even mid-stall.
            if hasattr(self.handle, "command_async"):
                self.handle.command_async("sub-add", str(path), "select")
                self.handle.command_async("set", "sub-visibility", "yes")
            else:                                   # pragma: no cover
                self.handle.sub_add(path, "select")
                self.handle["sub-visibility"] = True
            # Re-applied after the add, not before: a freshly selected
            # track starts from mpv's defaults, and an .ass needs the
            # scale lever rather than the font-size one.
            self._apply_sub_style()
            self._apply_sub_delay()
        except Exception:
            logs.exception("sub-add failed")
            self._finish_sub_toast("Subtitle Could Not Be Loaded")
            return
        # mpv accepts sub_add and then quietly leaves the old track
        # primary if it could not parse the file. Without this the panel
        # said "Subtitle Loaded", moved its highlight, and the picture
        # kept showing whatever was there before - which from the chair
        # reads as "the delay does nothing on this one".
        if not self._external_sub_selected(path):
            logs.warning(f"mpv did not select the loaded subtitle {path}")
            self._finish_sub_toast("That Subtitle Could Not Be Read")
            return
        self._subtitle_label = label
        self._remember_subtitle_choice()
        self._finish_sub_toast("Subtitle Loaded")
        if self._panel is not None and getattr(self._panel, "kind", "") == "subs":
            self._open_subtitle_panel(rebuild=True)

    def _remember_subtitle_choice(self):
        """File the pick that just loaded, so the next episode and the
        next session open on it (see load_subtitle_choice)."""
        choice, self._pending_sub_choice = self._pending_sub_choice, None
        if not choice:
            return
        result, provider, label = choice
        try:
            save_subtitle_choice(
                self.entry,
                lang=str((result or {}).get("lang") or "").lower(),
                source=str((result or {}).get("source") or ""),
                provider=provider, label=label)
        except Exception:
            # Never fatal: the subtitle is loaded and playing either way.
            logs.exception("Could not remember the subtitle choice")

    def _external_sub_selected(self, path) -> bool:
        """Is the file just handed to sub_add the primary sub track?

        Answered from mpv's own track list rather than from the fact that
        sub_add returned - measured on mpv v0.41, `sub-add <file> select`
        is what selects it, and its silent failure mode is a track list
        that never gained the file. A track list mpv has not published
        yet is treated as success: refusing on a race would be a false
        alarm on every fast machine."""
        try:
            tracks = list(self.handle.track_list or [])
        except Exception:
            return True
        subs = [t for t in tracks if t.get("type") == "sub"]
        if not subs:
            return True
        target = os.path.normcase(os.path.abspath(str(path)))
        for track in subs:
            name = track.get("external-filename")
            if not name:
                continue
            if os.path.normcase(os.path.abspath(str(name))) == target:
                return bool(track.get("selected"))
        return True

    def _on_failed(self, message, run):
        if self._closing or run != self._run:
            return
        # A subtitle pick's sticky toast is the one box the user is
        # already watching, so a failure goes *into* it rather than
        # beside it - otherwise "Loading Subtitle..." sits there for its
        # full two minutes with the reason in a box that has faded.
        if getattr(self, "_sub_toast", None) is not None:
            self._finish_sub_toast(message)
            return
        show_toast(self._toast_anchor(), message)

    def _set_subtitle_count(self, count):
        # The subtitles button is icon-only now, so the count rides in the
        # tooltip rather than in the label it no longer has. The panel
        # itself still shows the full list; this is only the at-a-glance
        # "are there any" hint on hover.
        if count is None:
            self.subs_btn.setToolTip("Arabic subtitles")
        else:
            self.subs_btn.setToolTip(f"Arabic subtitles ({count} found)")

    def _update_source_display(self, stream=None):
        """Refresh the resolution pill and the trailing source name.

        The pill carries the resolution (opening the drill-down); the
        muted label beside it carries which addon/site the stream came
        from. Split so the buffering note can borrow the label without
        clobbering the resolution."""
        if stream is None and 0 <= self._stream_index < len(self._streams):
            stream = self._streams[self._stream_index]
        stream = stream or {}
        quality = (stream.get("quality") or "").strip()
        self.res_pill.setText(quality.upper() if quality else "AUTO")
        # **What is playing, not who listed it** - the owner's ask, 23
        # August 2026: no provider or addon name anywhere in the UI. This
        # printed "Torrentio"/"Anime Tosho", which says nothing about the
        # picture on screen; it now says what the file is (codec, bit
        # depth, dynamic range), read off the release name. The `source`
        # field is untouched on the stream dict - _switch_stream still
        # matches on it to keep a hand-picked provider across episodes.
        from windows.details import _codec_label
        self.source_label.setText(_codec_label(
            stream.get("source"), quality, stream.get("title")))

    def _audio_language_preference(self):
        """Which audio language this entry should open on.

        The owner's ask, 22 August 2026: anime in Japanese, films and
        series in English. It is a *preference*, never a requirement -
        a release carrying neither keeps whatever mpv chose, which is
        the same behaviour every episode had before this existed.

        Both spellings of each, because releases disagree: ISO 639-2
        ("jpn", "eng") is what mkvmerge writes and what AnimeTosho's
        multi-sub groups ship, while 639-1 ("ja", "en") turns up on
        direct-URL sources and web rips."""
        kind = str((self.entry or {}).get("type") or "").strip().lower()
        if kind == "anime":
            return ("jpn", "ja", "jp", "jpa")
        # **A film or series that *is* anime opens in Japanese too.** The
        # type alone said English for Demon Slayer: Infinity Castle (a
        # Movie), so alang asked for a track the release did not have and
        # mpv kept the Spanish one it opened on - the owner's report, 24
        # August 2026. Cinemeta's genres and country decide (see
        # stremio.looks_anime): carried on the entry by the details page
        # when it has them, else learned by this page's own meta fetch.
        # getattr, because __init__ asks this before every field exists
        # (the download panel's default audio is chosen early) - measured
        # 24 August 2026: the anime branch above returned first, so
        # Frieren played while every Movie and Series raised
        # AttributeError here and the player never opened.
        hint = getattr(self, "_anime_hint", None)
        if hint is None:
            try:
                from helpers import stremio
                entry = self.entry or {}
                if entry.get("genres") or entry.get("country"):
                    hint = stremio.looks_anime(entry)
            except Exception:
                hint = None
        if hint:
            return ("jpn", "ja", "jp", "jpa")
        return ("eng", "en")

    # Title words that mark an audio track as a language when its `lang`
    # tag is absent or wrong - dual-audio rips routinely name the track
    # ("AAC English", "ITA 5.1") without tagging it, and mpv's `alang`
    # only ever reads the tag. Keyed by the preference tuple's first
    # element (_audio_language_preference).
    _AUDIO_TITLE_WORDS = {
        "eng": ("english", "eng"),
        "jpn": ("japanese", "jpn", "jap"),
    }
    # And words that mark a track as the one *not* to auto-pick even in
    # the right language - a commentary or described-video mix. Never a
    # hard exclusion: when it is the only match it still beats a track
    # in the wrong language.
    _AUDIO_AVOID_WORDS = ("commentary", "description", "descriptive",
                          "narration")

    def _auto_select_arabic_track(self):
        """Select an embedded Arabic subtitle track the moment the file
        lists one - the owner, 24 August 2026: "when loading the source
        that has ar in embedded translations, make it auto select and
        load it when I play directly" (and the pick now *prefers*
        releases that carry one - streams.arabic_rank).

        This deliberately narrows the standing "no embedded track is
        auto-selected, ever" rule (video_backend's `sid: no`) to
        everything that is not Arabic: that rule existed because English
        and Chinese tracks kept burning themselves over the picture,
        and an Arabic track is the one the whole subtitle apparatus
        exists to find. A remembered choice still outranks it: whatever
        the owner picked last time (_auto_apply_subtitle) or picks now
        (_pick_subtitle / _subtitles_off sets _sub_auto_done) is never
        overridden - this fills the silence before either speaks, and
        only once per episode."""
        if self._closing or self._arabic_track_done:
            return
        try:
            stored = load_subtitle_choice(self.entry)
        except Exception:
            stored = None
        if stored:
            return          # the remembered pick owns this episode
        track = next(
            (t for t in self._tracks
             if t.get("type") == "sub"
             and subtitles_module is not None
             and subtitles_module.is_arabic_code(t.get("lang"))), None)
        if track is None or track.get("selected"):
            if track is not None:
                self._arabic_track_done = True
            return
        self._arabic_track_done = True
        self._sub_auto_done = True      # embedded Arabic is the answer
        self._pick_track("sid", track)

    def _apply_audio_default(self):
        """Move to the preferred audio language, once per loaded file.

        mpv's own `alang` (set in _start) does this at open time and is
        the mechanism that matters; this is the second half, for the
        releases it cannot help with - a track whose `lang` mpv did not
        match but whose own metadata names the language, and the case
        where alang was applied before a later track joined the list.

        **Once per file, and that is not an optimisation.** mpv re-emits
        `track-list` on every `sub_add` and on every track change,
        including the ones the user makes in the tracks panel - so a
        version of this that ran on each emission would silently drag an
        English pick back to Japanese a beat after it was made."""
        if self._audio_default_done or self.handle is None:
            return
        audio = [t for t in self._tracks if t.get("type") == "audio"]
        if not audio:
            return          # nothing published yet; a later emission has it
        current = next((t for t in audio if t.get("selected")), None)
        if current is None:
            return          # mpv has not settled on one yet
        self._audio_default_done = True
        pick = self._preferred_audio_track(audio, current)
        if pick is not None and pick is not current:
            self._pick_track("aid", pick)

    def _preferred_audio_track(self, audio, current):
        """The track playback should move to, or None to keep `current`.

        The owner's report this exists for, 23 August 2026: House of the
        Dragon S01E03 auto-played in Italian. The file that did it is
        readable in his own stream cache (S01E03 "Secondo del suo nome"
        ITA.ENG WEB-DLMux, MeM.GP - 434MB pulled, so it genuinely
        played) and its track table, read straight off those bytes, is:
        audio 1 `ita`, flagged default; audio 2 **no language tag, no
        title at all** - the English track. `alang=eng,en` matches
        nothing there, mpv's default-flag choice (Italian) stood, and
        the old exact `lang == "eng"` walk here also found nothing. Its
        swarm was dead when re-tried (nothing in 60s), so the failure
        and the fix were both proven through the real player on a
        fabricated container of exactly that shape, plus a variant
        whose English track is titled "AAC English" - Italian under
        the shipped logic, English under this one, same files.

        Hence three layers, in trust order: the lang tag as written,
        the tag's base ("en-US" is English), then the track's own
        *title* ("AAC English", "ENG") - and when nothing names the
        language at all, a track carrying *no* language information
        beats a selection explicitly tagged with the wrong one, which
        is precisely the MeM.GP case. A commentary/described mix loses
        to a clean one even when both match."""
        wanted = self._audio_language_preference()
        title_words = self._AUDIO_TITLE_WORDS.get(wanted[0], ())

        def lang_of(track):
            return str(track.get("lang") or "").strip().lower()

        def tag_match(track):
            raw = lang_of(track)
            if not raw:
                return False
            # Exact first, then the base of a BCP-47-ish tag: releases
            # write "en-US", "en_GB", even "eng-Atmos".
            return raw in wanted or re.split(r"[-_]", raw, 1)[0] in wanted

        def title_match(track):
            title = str(track.get("title") or "").lower()
            return any(re.search(r"\b" + word + r"\b", title)
                       for word in title_words)

        def avoid(track):
            title = str(track.get("title") or "").lower()
            return any(word in title for word in self._AUDIO_AVOID_WORDS)

        def matches(track):
            return tag_match(track) or title_match(track)

        # The pick already speaks the language and is not a commentary:
        # nothing to do. (A matching commentary is only left when no
        # clean match exists - checked below.)
        if matches(current) and not avoid(current):
            return None
        candidates = [t for t in audio if matches(t)]
        if candidates:
            # Clean mixes before commentary, a real tag before a title
            # guess, the container's default flag before container order.
            candidates.sort(key=lambda t: (avoid(t), not tag_match(t),
                                           not t.get("default"),
                                           t.get("id") or 0))
            return None if candidates[0] is current else candidates[0]
        # Nothing names the language at all. If mpv's own pick is
        # explicitly tagged some *other* language, a track with no
        # language information ("Track 2") is the better guess for an
        # entry whose type says what the owner watches it in - a known
        # miss loses to an unknown. An untagged current stays: there is
        # nothing that says any other track beats it.
        cur = lang_of(current)
        if cur and cur not in ("und", "mul", "zxx", "unknown"):
            unknown = [t for t in audio
                       if lang_of(t) in ("", "und", "unknown")]
            clean = [t for t in unknown if not avoid(t)]
            unknown = clean or unknown
            if unknown:
                unknown.sort(key=lambda t: (not t.get("default"),
                                            t.get("id") or 0))
                return unknown[0]
        return None     # nothing better than what mpv chose

    def _update_audio_pill(self):
        """The audio-language pill follows the selected audio track."""
        audio = [t for t in self._tracks if t.get("type") == "audio"]
        selected = next((t for t in audio if t.get("selected")), None)
        lang = str((selected or {}).get("lang") or "").strip()
        self.audio_pill.setText(lang.upper() if lang else "AUDIO")
        self.audio_pill.setVisible(bool(audio))

    # ---- playback ----------------------------------------------------
    def _play_stream(self, index, resume_at=None, solo=False):
        if self.handle is None or not self._streams:
            return
        index = max(0, min(index, len(self._streams) - 1))
        stream = self._streams[index]
        if stream.get("kind") == "drm":
            self._show_drm(stream)
            return
        self._stream_index = index
        # Only over a running picture. During startup this used to run
        # unconditionally, and _hide_status drops the loading backdrop -
        # measured live (Demon Slayer, Next pressed, 23 August 2026):
        # the reset put the frame up instantly, the prefetched source
        # list answered ~0.2s later, this hide tore the frame down, and
        # _show_loading_soon's 350ms grace left the window blank at
        # +0.35s. While the first frame is still owed, the frame stays;
        # a stale dead-end box is repainted into the loading state
        # instead of everything going dark.
        if not self._awaiting_first_frame:
            self._hide_status()
        elif self._status_visible() and not self._loading_visible:
            self._show_loading()
        self._update_source_display(stream)

        # A torrent arrives without a url on purpose: the streaming
        # server has to be given the release's trackers before it can
        # serve anything, and doing that for all thirty-odd results at
        # lookup time would announce to every tracker for things nobody
        # is going to watch. So it happens here, for the one chosen
        # stream - off the UI thread, because it can also have to start
        # the server.
        if self._needs_preparing(stream) and streams_module:
            self._show_loading_soon("Connecting to the source...")
            # Worked out here, on the UI thread, and *before* the
            # release is created - see _prime_seat.
            self._spawn(self._prepare_stream_worker, index, resume_at,
                        self._run, solo, self._prime_seat(resume_at))
            return

        self._load_into_mpv(stream, resume_at)

    def _needs_preparing(self, stream) -> bool:
        """Whether this release has to be handed to the engine before it
        can be played.

        **A url on the stream is not proof it still works, and that is
        the owner's "changing the source inside the vid playing does not
        work gets stuck, but if I go back to the ep list and choose the
        source it loads".** The two paths differ in exactly one way: the
        episode list builds a fresh list out of `find_streams`, whose
        rows carry no url, while the player *writes the prepared stream
        back* into `self._streams[index]` (see _on_stream_prepared). So
        the second time a release is picked from the panel it already
        has one - and `_switch_stream` has meanwhile called
        `_release_playing_torrent`, which takes that very torrent out of
        the session. `torrent_engine.stream_url` then serves nothing at
        that address (it answers None for a hash it no longer holds),
        mpv opens a route with no torrent behind it, and the page waits
        for a first frame that cannot arrive.

        So a *local engine* url is only trusted while the engine still
        has the torrent. A direct link - a debrid download, an http
        source - does not depend on the session at all and is left
        alone; re-preparing one would spend a debrid round trip to be
        handed back the same address."""
        url = stream.get("url") or ""
        info_hash = stream.get("info_hash")
        if not url:
            return bool(info_hash)
        if not info_hash or "127.0.0.1" not in url:
            return False
        try:
            from helpers import torrent_engine
            return torrent_engine.stream_url(info_hash) is None
        except Exception:
            return False

    def _prime_seat(self, resume_at):
        """`(seconds, total)` the engine should fetch first, or
        `(None, None)`.

        Where playback is actually going to *begin*, which is not always
        what the caller asked for. A fresh episode is played with
        `resume_at=None` and only discovers its saved seat inside
        `_load_into_mpv`, by which time the torrent has already been
        created and has spent its first seconds pulling the head of the
        file - the head being precisely the part that is not going to be
        shown. That is the owner's "it takes ~10-30 sec then the vid
        plays", and telling the engine afterwards is worth nothing, so
        the record is read here instead.

        These are also the guards that decide whether the file *opens*
        at the seat at all now (_load_into_mpv reads its answer from
        here), which is what keeps the engine and mpv pointed at the
        same offset by construction - the engine must never be sent to
        prioritise an offset playback is then not going to start at."""
        if resume_at:
            return float(resume_at), float(self._duration or 0.0)
        try:
            record = load_resume(self.entry, self.season,
                                 self.episode) or {}
        except Exception:
            return None, None
        position = float(record.get("position") or 0.0)
        duration = float(record.get("duration") or 0.0)
        if position < RESUME_MIN_S:
            return None, None
        if duration and position > duration * RESUME_MAX_FRACTION:
            return None, None
        return position, duration

    def _prepare_stream_worker(self, index, resume_at, run, solo=False,
                               prime=(None, None)):
        """Never raises - a dead worker here leaves the page on
        "Connecting..." with nothing coming.

        Starts the chosen source *and* the untried ones behind it
        together, playing whichever delivers data first. A release's
        advertised seeder count says nothing about whether its swarm
        answers: one measured film claimed 2081 seeders and never
        completed a piece, and discovering that serially cost 45s before
        the next was even started. Raced, the healthy one arrives while
        the dead one is still failing. Measured end to end: 79.5s to
        12.8s on that film, 31.8s to 6.2s on an episode.

        Everything of the chosen resolution is handed over, not the next
        two: streams.prepare_fastest rolls through the list as attempts
        fail rather than waiting on a fixed batch, so a longer list
        costs nothing and is the difference between the race finding a
        live release and reporting that none of the first three were.

        Nothing is re-prepared afterwards. There used to be a serial
        `prepare(chosen)` behind the race for the case where it returned
        None - which is the case where that exact release had *just*
        failed, so it re-ran the whole wait to fail again.

        **`solo` turns the race off, and a deliberate pick sets it.**
        Racing is right when Atomic chose the source itself: a swarm's
        advertised seeders say nothing about whether it answers, and
        finding that out serially cost 45s. It is wrong when the *user*
        picked a release out of the source list, for two reasons
        measured on House of the Dragon S02E05: the six releases start
        together and split one connection, so the 1876-seeder release
        the owner deliberately chose downloaded at a sixth of the rate
        it could - "it takes ages to load" - and whichever of the six
        answered first is what played, quietly overriding the choice
        that `_play_stream_choice` promises not to re-rank. A solo pick
        that genuinely fails still falls through to _try_next_source,
        which races the rest as before."""
        # Every release this race proves dead, so the *next* attempt
        # does not walk them again - see _dead_hashes.
        failed = []
        try:
            chosen = self._streams[index]
            others = [] if solo else self._untried_first([
                s for i, s in enumerate(self._streams)
                if i != index and i not in self._dead_sources
                and s.get("kind") != "drm"
                and _canonical_quality(s.get("quality"))
                == _canonical_quality(chosen.get("quality"))])
            wanted_title = (self.entry or {}).get("title") or ""
            if solo:
                # **A hand-picked release is played, not raced.** It used
                # to go in as candidate 0 of a full race, on the measured
                # grounds that a dead pick otherwise cost 23s. That trade
                # is off: the owner, 23 August 2026 - "I changed the
                # source it reloaded and resumed correctly but with the
                # same video was playing in the 1st source ... the source
                # is not really changing". It was changing; the race was
                # handing back whichever release won, which for a second
                # pick is very often the same one that won the first
                # time. A pick the app can silently override is not a
                # pick.
                #
                # What makes this affordable now is that the reason for
                # racing it has largely gone: the debrid lane resolves a
                # cached release in about a second, and SOLO_METADATA_
                # TIMEOUT/SOLO_DATA_WAIT bound a dead pick at 4s + 4s
                # before _try_next_source races the rest of the list
                # anyway. So a live pick plays, and a dead one costs 8s
                # and then behaves exactly as before.
                stream = streams_module.prepare(
                    chosen, season=self.season, episode=self.episode,
                    title=wanted_title,
                    metadata_timeout=streams_module.SOLO_METADATA_TIMEOUT,
                    data_wait=streams_module.SOLO_DATA_WAIT,
                    **_prime_kwargs(streams_module.prepare, prime))
            elif others and hasattr(streams_module, "prepare_fastest"):
                stream = streams_module.prepare_fastest(
                    [chosen] + others, season=self.season, episode=self.episode,
                    title=wanted_title, failed=failed,
                    **_prime_kwargs(streams_module.prepare_fastest, prime))
            else:
                # The last candidate, alone, on the same bounded budget.
                stream = streams_module.prepare(
                    chosen, season=self.season, episode=self.episode,
                    title=wanted_title,
                    metadata_timeout=streams_module.SOLO_METADATA_TIMEOUT,
                    data_wait=streams_module.SOLO_DATA_WAIT,
                    **_prime_kwargs(streams_module.prepare, prime))
            # Written before the emit, never after: a queued connection
            # delivers this to the UI thread only once the slot runs, so
            # the set is complete by the time _try_next_source reads it.
            self._dead_hashes.update(h for h in failed if h)
            self._work.stream_prepared.emit(stream, index, resume_at, run)
        except Exception:
            logs.exception("Preparing the stream failed")
            self._dead_hashes.update(h for h in failed if h)
            self._work.stream_prepared.emit(None, index, resume_at, run)

    def _on_stream_prepared(self, stream, index, resume_at, run):
        if run != self._run or self.handle is None:
            return
        if not stream or not stream.get("url"):
            reason = (stream or {}).get("reason") or ""
            if reason == "no-engine":
                # Not a dead swarm and not worth walking the list for:
                # this build has no torrent engine at all, so every
                # source after it fails identically.
                self._finish_working("This Build Has No Torrent Engine")
                self._show_status(
                    "This build has no torrent engine, so torrent sources "
                    "can't be played.\nRebuild Atomic with libtorrent "
                    "installed, or pick a direct source from the list.")
                return
            # A dead swarm is common and is not worth showing the user:
            # a release can advertise hundreds of seeders and connect to
            # none (measured - 632 claimed, zero reached), while the next
            # one down starts in under a second. Walking the list is what
            # a person would do by hand, so do it for them rather than
            # stopping on the first dud.
            if self._try_next_source(index, resume_at=resume_at):
                return
            self._finish_working("No Source Would Start")
            self._show_status(
                "None of the sources for this would start.\n"
                "Try again in a moment, or pick one from the list.")
            return
        self._streams[index] = stream
        self._hide_status()
        # **The pinned resolution is *not* cleared here, and that is the
        # owner's "when I choose the 4K from inside the player, it loads
        # and plays then after sometime it auto loads the 1080P again".**
        # It used to be dropped the instant the pick loaded, on the
        # reasoning that the switch was over. It is not: a torrent that
        # stalls twenty minutes in re-enters _try_next_source, which with
        # no constraint walks from index 0 - the top-ranked source, which
        # is the preferred 1080p - so the 4K the user asked for was
        # quietly traded back for the resolution he had just left. A
        # resolution chosen by hand is a choice about the *episode*, so
        # it now holds until the episode changes (_begin_episode clears
        # it) or until nothing at that resolution will start at all, and
        # _try_next_source says so out loud before it crosses over.
        # _seat_now, not resume_at: the old source went on playing for
        # the whole peer hunt, so the position captured when the panel
        # row was clicked is behind where the user actually is.
        self._load_into_mpv(stream, self._seat_now(resume_at))

    # How many dead sources to walk past before giving up and saying so.
    # Bounded rather than open-ended: each attempt costs a create call
    # and a peer wait, and a title where the first several are all dead
    # is a title with a real problem, not one more retry away.
    MAX_SOURCE_ATTEMPTS = 5

    def _release_streams(self):
        """Give back every torrent this page added. Never raises - it
        runs from close_player, where an exception would leave the page
        half torn down."""
        try:
            from helpers import torrent_engine
        except Exception:
            return
        hashes = {s.get("info_hash") for s in (self._streams or [])
                  if s.get("info_hash")}
        hashes.update(getattr(self, "_dead_hashes", ()) or ())
        # Including anything warmed for an episode that will now never be
        # played - see _maybe_prewarm_next.
        hashes.update(getattr(self, "_prewarmed", ()) or ())
        for info_hash in hashes:
            try:
                torrent_engine.release(info_hash)
            except Exception:
                pass

    def _is_dead_hash(self, stream) -> bool:
        """Whether this release has already failed a race this episode -
        see _dead_hashes."""
        info_hash = (stream or {}).get("info_hash")
        return bool(info_hash) and info_hash in getattr(self, "_dead_hashes", ())

    def _untried_first(self, streams):
        """`streams`, with everything an earlier race already failed on
        moved to the back.

        **Moved, not dropped**, and that is deliberate. Dropping them was
        tried: a race tries up to RACE_WIDTH releases at once and rolls
        through more as they fail, and only the *index* it was asked for
        was being remembered, so the next attempt started another race
        over the same twenty dead releases at up to twelve seconds each
        (measured 22 August 2026 on House of the Dragon S02E06 - 52.6s
        across two attempts, the second re-walking the first's
        failures). But a failure here is not proof: six lanes share one
        connection, and a release that missed its data wait under that
        contention can very well answer when it is the only thing
        running. So it goes last rather than away, and the order is
        otherwise the ranking's."""
        fresh = [s for s in streams if not self._is_dead_hash(s)]
        stale = [s for s in streams if self._is_dead_hash(s)]
        return fresh + stale

    def _try_next_source(self, failed_index, resume_at=None) -> bool:
        """Move to the next untried source after one failed to start.

        **`resume_at` is carried, and dropping it was the owner's "when
        I change from 1080P to 4K it starts from 0:00 not where I
        stopped".** The seat was never lost where anyone was looking for
        it: measured 22 August 2026 against the real widget and real
        mpv, `_switch_stream` captured 10.10s and handed it to the
        prepare worker intact. But a 4K pick whose swarm does not answer
        lands here, and this called `_play_stream(index)` with no
        resume - so the release it moved on to opened with
        `resume_at=None` and its first six time-pos readings were
        0.0, 0.1, 0.2, 0.3, 0.4, 0.5. The saved resume record cannot
        cover for it either: it lags by up to
        POSITION_SAVE_MS and does not exist at all in the first seconds
        of an episode.

        **Everything an earlier race already failed on goes last.** A
        race tries up to RACE_WIDTH releases at once and rolls through
        more as they fail, but only the *index* it was asked for was
        being recorded - so the next attempt started another race over
        the same twenty dead releases, at up to twelve seconds each
        (measured 22 August 2026 on House of the Dragon S02E06: 52.6s to
        a playable url across two attempts, the second re-walking the
        first's failures). They are still tried, once nothing untried is
        left - see _untried_first for why a failure under six-way
        contention is not proof that a release is dead.

        Returns False once there is nothing left worth trying, which is
        when the page finally says so out loud."""
        self._dead_sources.add(failed_index)
        if len(self._dead_sources) >= self.MAX_SOURCE_ATTEMPTS:
            return False
        # **A resolution the user chose by hand is a constraint, not a
        # preference.** Without it the walk starts at index 0 - the
        # top-ranked source, normally the preferred 1080p - so a failed
        # 4K pick quietly resumed the release that was already playing
        # and the switch looked like it had done nothing (see
        # _switch_stream).
        wanted = getattr(self, "_requested_quality", None)
        if self._start_next_at(wanted, resume_at):
            return True
        if wanted is None or self._quality_given_up:
            return False
        # **Every release at the pinned resolution is dead, so cross over
        # - once, and say so.** Silently dropping to another resolution
        # is what the pin exists to stop; but the pin now survives a
        # successful load (see _on_stream_prepared), so "nothing at this
        # resolution will start" is no longer only a startup verdict - it
        # can arrive an hour in, and the alternative to crossing over is
        # a picture that stops for good. Saying which resolution went and
        # why is what makes it a report rather than the silent swap the
        # owner caught. Once per episode: _quality_given_up stops this
        # from re-announcing on every later stall.
        self._quality_given_up = True
        self._requested_quality = None
        return self._start_next_at(
            None, resume_at,
            note=f"No {'4K' if wanted == '2160p' else wanted.upper()} Source "
                 f"Would Start - Trying Another Resolution")

    def _start_next_at(self, wanted, resume_at, note=None) -> bool:
        """Start the best untried source, optionally held to one
        resolution. The walk itself; see _try_next_source for the policy
        around it.

        `note` replaces the usual "had no peers" line and is said whether
        or not a toast is already up - it carries news the user has to
        have (his chosen resolution is gone), not a progress note."""
        retry = []
        fallback = False
        order = list(range(len(self._streams)))
        if wanted is not None:
            order = [i for i in order
                     if _canonical_quality(self._streams[i].get("quality"))
                     == wanted]
        for index in order + [None]:
            if index is None:
                # Nothing untried left; take the best of what has
                # already failed once rather than giving up on an
                # episode whose swarms may simply have been busy.
                if not retry:
                    return False
                fallback = True
                index = retry[0]
            if index in self._dead_sources:
                continue
            if self._streams[index].get("kind") == "drm":
                continue
            if self._is_dead_hash(self._streams[index]) and not fallback:
                # Passed over on the first sweep, taken on the second -
                # see _untried_first for why a failed release is worth
                # one more try rather than being written off.
                retry.append(index)
                continue
            self._show_loading("That source had no peers. Trying the next one...")
            # Said in the toast as well, not only through _show_loading:
            # over a running picture the loading frame is suppressed on
            # purpose (_show_backdrop), so on a switch this line was
            # written to a surface nobody could see.
            if note is not None:
                self._say_working(note)
            elif self._work_toast is not None:
                self._say_working("That Source Had No Peers - Trying The Next One")
            self._play_stream(index, resume_at=self._seat_now(resume_at))
            return True
        return False

    def _seat_now(self, resume_at):
        """Where a switch should pick up, given what it asked for and
        where playback has since got to.

        The larger of the two, and that is not pedantry: a torrent switch
        spends seconds finding peers *while the old source keeps
        playing*, so the position captured at the moment the panel row
        was clicked is already behind by the time the new file is handed
        to mpv - resuming to it replays what was just watched. The old
        position is only trusted while a picture is actually up
        (`_awaiting_first_frame` false); mid-startup it is a leftover.

        Returns None rather than 0.0 for "nothing to resume to", because
        that is what `_load_into_mpv` reads as "ask the saved record"."""
        asked = float(resume_at or 0.0)
        live = 0.0 if self._awaiting_first_frame else float(self._position or 0.0)
        return max(asked, live) or None

    def _update_startup_status(self, creep=False):
        """Move the startup gauge to wherever startup actually is.

        Runs from three places: the ticker (with `creep`), mpv's
        cache-buffering-state events, and one-off moments (logo arrival,
        a file handed to mpv). It used to run *only* on the mpv events,
        and for an engine URL there is exactly one of those, already at
        100 - measured across a real 14.7s episode start, the fraction
        was written twice: 0.04 at logo arrival, 1.0 at the first frame.
        The owner read that (correctly) as the progress not working. The
        phases before mpv have real numbers of their own; the snapshot
        below reads them, and the ticker keeps the gauge visibly alive
        between readings."""
        if not self._awaiting_first_frame or self._closing:
            return
        # A dead-end message outranks progress: the ticker must never
        # paint the loading frame back over "no source would start".
        # _show_loading's no-logo fallback is the one _show_status whose
        # box is *itself* the loading state; it re-arms _loading_visible.
        if self._status_visible() and not self._loading_visible:
            return
        target, text = self._startup_snapshot()
        fraction = max(self._startup_fraction, target)
        if creep:
            # Between real readings the gauge still inches forward - a
            # bar parked on a milestone reads as hung even while the
            # phase behind it is genuinely working - but never more than
            # the headroom past what has actually been measured.
            fraction = min(fraction + STARTUP_CREEP,
                           target + STARTUP_HEADROOM)
        fraction = max(self._startup_fraction, min(fraction, 1.0))
        self._startup_fraction = fraction

        frame_up = (self.backdrop.isVisible()
                    or self._loading_delay.isActive())
        # No words with the logo up - the pulse and the fill are the
        # whole message (the owner's ask). The text survives only as
        # the fallback for a title with no logo art.
        if self.logo.has_logo():
            self.logo.set_fraction(fraction)
            # Drop the words the moment the logo can carry the message;
            # otherwise only put the frame up when it is not already -
            # _show_loading re-lays-out and re-raises native windows,
            # and doing that four times a second is its own flicker.
            if self._status_visible() or not frame_up:
                self._show_loading()
        elif text != self._startup_text or not frame_up:
            self._startup_text = text
            self._show_loading(text)

    def _startup_tick(self):
        self._update_startup_status(creep=True)

    def _startup_snapshot(self):
        """(target fraction, status text) for the phase startup is in.

        Real numbers first, in the order the phases happen: sources
        still being looked for; the torrent created but without metadata
        yet; peers found; the head of the file arriving - bytes against
        the engine's HEAD_BYTES, which is the window mpv's open is
        actually waiting on (measured: the longest stretch of a cold
        start, 9.1 of 14.7s, sits between handing mpv the URL and its
        one buffering report). mpv's own cache percent caps the scale;
        for an engine URL it was measured to arrive once, at 100, so it
        is the finisher, never the driver. A phase with nothing to
        measure returns a flat target and lets the drift say "alive"."""
        # A pending seat outranks the byte counters, because it is what
        # the wait is actually *for*. The owner watched a black screen
        # for 10-30s under a toast claiming it had resumed; a torrent
        # has to fetch the pieces at that offset before the picture can
        # come back, and saying so is the difference between "working"
        # and "broken".
        seat = self._resume_target
        if seat:
            return 0.55, f"Skipping to {_format_time(seat)}..."
        percent = getattr(self, "_buffering_percent", 0)
        if percent > 0:
            return (0.92 + 0.08 * min(percent, 100) / 100.0,
                    f"Buffering... {int(percent)}%")
        if not self._streams_started:
            return 0.05, "Looking for a source..."
        try:
            stream = self._streams[self._stream_index] or {}
        except Exception:
            stream = {}
        # The hash of what is being started right now: _play_stream sets
        # _stream_index before it shows "Connecting...", and a race that
        # ends on a different release writes the winner back to this
        # same index before mpv is handed anything.
        info_hash = str(stream.get("info_hash") or "").strip().lower()
        if not info_hash:
            # A direct URL: mpv is opening it and reports nothing until
            # its cache does. Park high enough that the drift stays shy
            # of the percent-driven band above.
            return 0.60, "Connecting to the source..."
        engine = None
        try:
            from helpers import torrent_engine
            if torrent_engine.available():
                engine = torrent_engine
        except Exception:
            engine = None
        if engine is None:
            return 0.20, "Connecting to the source..."
        try:
            progress = engine.file_progress(info_hash) or {}
        except Exception:
            progress = {}
        if progress:
            # Metadata is in (file_progress answers only past that
            # point), so the bytes of the chosen file are the truth.
            done = int(progress.get("done") or 0)
            if done > 0:
                head = float(getattr(engine, "HEAD_BYTES", 0)
                             or 12 * 1024 * 1024)
                return (0.42 + 0.48 * min(1.0, done / head),
                        f"Buffering... {min(99, int(100 * done / head))}%")
            return ((0.40 if progress.get("peers") else 0.36),
                    "Finding peers for this source...\n"
                    "The first few seconds take longest.")
        try:
            # stats() answers before metadata does; keys are stored
            # lowercased and stats() does not lower its argument.
            peers = int((engine.stats(info_hash) or {}).get("peers") or 0)
        except Exception:
            peers = 0
        return ((0.30 if peers else 0.22), "Connecting to the source...")

    def _load_into_mpv(self, stream, resume_at=None):
        # A warmed release that is neither what is about to play nor the
        # warm-up for the episode after it is now dead weight competing
        # for the connection - hand it back before the picture starts.
        # This is the only place that can tell, because it is the only
        # place that knows which release actually won. (The one about to
        # play is protected inside _release_prewarmed.)
        self._release_prewarmed(keep=(self._prewarm_hash,))
        self._awaiting_first_frame = True
        self._buffering_percent = 0
        # A fresh file gets a fresh audio pick - see _apply_audio_default.
        self._audio_default_done = False
        self._clear_seat()
        self._update_startup_status()

        # Where this file should open: what the caller carried across a
        # switch, else the saved resume record (same guards as ever -
        # under RESUME_MIN_S there is nothing worth going back to, past
        # RESUME_MAX_FRACTION the episode is effectively finished; see
        # _prime_seat, the one place those guards live now). There used
        # to be a "Resume from 12:34?" bar with Resume and Start Over on
        # it; it is gone on purpose - the answer was Resume every single
        # time, and ignoring the question for twelve seconds meant
        # starting over, so the default was the option nobody wanted.
        seat = self._prime_seat(resume_at)[0]

        # Headers before the load, not after: many of these hosts answer
        # 403 to a request without the Referer the page carried, and mpv
        # reads these options when it opens the url.
        headers = stream.get("headers") or {}
        url = stream.get("url") or ""
        try:
            fields = [f"{name}: {value}" for name, value in headers.items()
                      if name.lower() != "user-agent"]
            self.handle["http-header-fields"] = fields
            agent = next((v for k, v in headers.items() if k.lower() == "user-agent"), None)
            if agent:
                self.handle["user-agent"] = agent
            # **There used to be a stop here, and it was the hang.**
            # Re-loading a URL mpv already has open produces no `path`
            # *change*, so the gate below could not reopen - and the fix
            # for that was to stop first, driving `path` to None. But
            # `_stop_playback` is `command_async` and `loadfile` is
            # synchronous, and a synchronous command executes in mpv's
            # core ahead of an async one already queued: the stop landed
            # *after* the load and unloaded the file it had just opened,
            # leaving `path` None for good. That is the owner's "I
            # changed the source from inside the player and it got
            # stuck" (25 August 2026) - see `_on_file_loaded`, which now
            # opens the gate on mpv's own file-loaded event and makes
            # the identical-URL reload an ordinary case rather than one
            # needing a trick.
            #
            # `loadfile(..., "replace")` already replaces whatever is
            # playing, so nothing about mpv needed the stop either.
            #
            # The gate closes before the load is issued and reopens on
            # file-loaded, or when the observed `path` names this url -
            # see _file_ready.
            self._file_ready = False
            self._loading_url = url
            if seat:
                # **The file is opened at the seat, not played from the
                # head and seeked after.** The old shape - load, wait
                # for the first time-pos>0, then seek - was a race the
                # owner lost ("the Skipping to X:XX after changing the
                # source ... keeps loading but never takes me there"):
                # a stale time-pos from the outgoing file could trigger
                # the seek while the new file was still opening, mpv
                # refused it, nothing re-issued it, and the new source
                # played from 0:00 under a skip message that could not
                # come true. A per-file start= has no such window, and
                # it is exact, not keyframe-quantised: measured 22
                # August 2026 against a deliberately slow (2.5s/request)
                # http source with keyframes ~10.4s apart, start=8.05
                # opened at time-pos 8.050 (a keyframe-only start would
                # have landed at 0.0). It also asks the demuxer for the
                # head and the seat's neighbourhood only - exactly the
                # window _prime_seat already told the torrent engine to
                # fetch first.
                self.handle.loadfile(url, "replace", start=f"{float(seat):.3f}")
            else:
                self.handle.play(url)
            self.handle["pause"] = False
        except Exception:
            logs.exception("Starting playback failed")
            self._show_status("That source could not be opened. Try another one.")
            return

        if seat:
            # **Announced when it has happened, not when it was
            # decided** (the owner's earlier "resumed from 9:23 ... it
            # takes ~10-30 sec then the vid plays"): _resume_target is
            # only the promise; _on_property says "Resumed From" when
            # the picture is actually at the seat, the startup gauge
            # narrates the wait ("Skipping to 9:23..."), and the
            # watchdog is what keeps the promise from outliving its
            # truth - see _seat_tick.
            self._resume_target = float(seat)
            self._seat_deadline = time.monotonic() + SEAT_GIVE_UP_S
            self._seat_timer.start()
            if self._work_toast is not None:
                # A switch has a toast up already ("Loading Source...");
                # re-word it to what the wait is actually for. A fresh
                # open has the loading frame instead, and the gauge
                # carries the message there.
                self._say_working(f"Skipping To {_format_time(seat)}...")

    def _clear_seat(self):
        """Withdraw the seat promise - landed, superseded or given up."""
        self._resume_target = None
        self._seat_deadline = None
        self._seat_reported = None
        if self._seat_timer.isActive():
            self._seat_timer.stop()

    def _seat_tick(self):
        """Give up on a seat that is not landing, out loud.

        Runs on its own timer because the stuck case is precisely the
        one with no time-pos events to hang a check on: mpv parked
        buffering at an offset whose data never arrives. Playback is
        left wherever it is - stopping it too would turn a failed skip
        into a failed episode."""
        if self._closing or self._resume_target is None:
            self._clear_seat()
            return
        if self._seat_deadline and time.monotonic() > self._seat_deadline:
            seat = self._resume_target
            self._clear_seat()
            if self._awaiting_first_frame:
                # Not one frame arrived from this source: its head
                # answered prepare, but the swarm never delivered the
                # seat's window - measured live (Demon Slayer switch to
                # an Anime Tosho release, 23 August 2026: 45s at 0:34
                # with the position frozen and hwdec never engaged).
                # "Playing From Here" would narrate a black stall, so
                # walk to the next release instead, seat kept - the
                # same move a failed prepare already makes.
                self._say_working("That Source Stalled - Trying Another...")
                if self._try_next_source(self._stream_index, resume_at=seat):
                    return
                self._finish_working()
                self._show_status(
                    "None of the sources for this would start.\n"
                    "Try again in a moment, or pick one from the list.")
            elif self._work_toast is not None:
                self._finish_working(f"Could Not Skip To {_format_time(seat)}"
                                     " - Playing From Here")
            else:
                show_toast(self._toast_anchor(),
                           f"Could Not Skip To {_format_time(seat)}"
                           " - Playing From Here")

    def _show_drm(self, stream):
        """The one case with no spinner and no retry: Widevine content is
        not decodable here and never will be, so the page says exactly
        that and offers the only thing that does work."""
        source = stream.get("source") or "This service"
        self.controls.hide()
        self._close_panel()
        self._show_status(
            f"{source} is DRM-protected (Widevine), so Atomic cannot play it.\n"
            f"Open it on {source} instead.")
        button = _text_button(f"Open on {source}")
        button.setParent(self)
        _make_native(button)
        url = stream.get("url") or self.entry.get("url") or ""

        def open_page():
            import webbrowser
            if url:
                webbrowser.open(url)
        button.clicked.connect(open_page)
        self._drm_button = button
        # Geometry before show, like every other native overlay here -
        # see _show_status for what showing one first looks like.
        self._layout_overlays()
        button.show()
        button.raise_()

    def _seek_absolute(self, seconds, resuming=False):
        if self.handle is None:
            return
        if not resuming:
            # A seek the user asked for cancels the seat the player
            # still owed. Without this, dragging the bar (or taking a
            # skip offer) while the seat was still fetching its pieces
            # left "Skipping To 9:23..." on screen describing a journey
            # nobody was on any more.
            if self._resume_target is not None:
                self._clear_seat()
                self._finish_working()
            else:
                self._clear_seat()
        try:
            # precision="exact", not mpv's default "keyframes": measured
            # on a 10s test mp4, a seek to 6.0s landed at 4.7 because
            # that was the nearest keyframe before it - which reads as
            # the seek bar ignoring where it was dropped.
            self.handle.seek(max(0.0, float(seconds)), reference="absolute",
                             precision="exact")
        except Exception:
            logs.exception("Seek failed")

    def _seek_relative(self, delta):
        self._seek_absolute((self._position or 0.0) + delta)

    def toggle_pause(self):
        if self.handle is None:
            return
        try:
            self.handle["pause"] = not self._paused
        except Exception:
            logs.exception("Pause toggle failed")

    def toggle_mute(self):
        if self.handle is None:
            return
        try:
            self.handle["mute"] = not self._muted
        except Exception:
            logs.exception("Mute toggle failed")

    def _set_volume(self, value):
        if self.handle is None:
            return
        try:
            self.handle["volume"] = int(value)
        except Exception:
            logs.exception("Volume change failed")

    def _set_speed(self, speed):
        speed = max(SPEED_MIN, min(SPEED_MAX, round(float(speed), 2)))
        if self.handle is not None:
            try:
                self.handle["speed"] = speed
            except Exception:
                logs.exception("Speed change failed")
        # Reflected immediately rather than waiting for mpv's property
        # callback, so the panel's number follows the slider under the
        # finger instead of a beat behind it.
        self._speed = speed
        self.speed_btn.setText(f"{speed:g}x")
        self._sync_speed_panel()

    def _sync_speed_panel(self):
        panel = self._panel
        if panel is None or getattr(panel, "kind", "") != "speed":
            return
        try:
            panel.speed_value.setText(f"{self._speed:g}x")
            panel.speed_slider.blockSignals(True)
            panel.speed_slider.setValue(int(round(self._speed * 100)))
            panel.speed_slider.blockSignals(False)
        except RuntimeError:
            pass          # closed between the click and this call

    def _open_speed_panel(self):
        """The speed control: the current value large, a slider under it,
        and the preset stops in one row - the small window the owner
        sketched, in the app's own theme."""
        panel = self._new_panel("Playback Speed", "speed")
        if panel is None:
            return          # the same button closed it

        value = QLabel(f"{self._speed:g}x")
        value.setAlignment(Qt.AlignmentFlag.AlignCenter)
        value.setStyleSheet(
            f"color: {theme.TEXT}; font-size: 15pt; font-weight: 700;"
            f" background: transparent; border: none;")
        panel.body_layout.addWidget(value)

        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setRange(int(SPEED_MIN * 100), int(SPEED_MAX * 100))
        slider.setValue(int(round(self._speed * 100)))
        # Steps of 5% - fine enough to land anywhere useful, coarse
        # enough that the number stays readable while dragging.
        slider.setSingleStep(5)
        slider.setStyleSheet(
            f"QSlider::groove:horizontal {{ height: 5px; background: {theme.SURFACE_HOVER};"
            f" border-radius: 2px; }}"
            f"QSlider::sub-page:horizontal {{ background: {theme.ACCENT}; border-radius: 2px; }}"
            f"QSlider::handle:horizontal {{ background: {theme.TEXT}; width: 14px;"
            f" margin: -5px 0; border-radius: 7px; }}")
        use_hover_cursor(slider)
        slider.valueChanged.connect(
            lambda v: self._set_speed(round(v / 100.0 / 0.05) * 0.05))
        panel.body_layout.addWidget(slider)

        chips = QHBoxLayout()
        chips.setSpacing(4)
        chip_rows = []

        def paint_chip(chip, current):
            chip.setStyleSheet(
                f"QPushButton {{ background: {theme.SURFACE_HOVER if current else 'transparent'};"
                f" color: {theme.ACCENT if current else theme.TEXT};"
                f" border: none; padding: 5px 7px; font-size: 10pt; font-weight: 600;"
                f" border-radius: {theme.RADIUS_SM}px; }}"
                f"QPushButton:hover {{ background: {theme.SURFACE_HOVER}; }}")

        def pick_speed(preset):
            # Repainted at the press, not at the next reopen - the
            # owner's "the playback speed buttons do not get highlighted
            # when I choose them until I close the window and reopen".
            # mpv's confirmation still lands through the property
            # observer; this is the same optimistic move-the-highlight
            # rule every panel row follows (_pick_track).
            self._set_speed(preset)
            for other, other_preset in chip_rows:
                paint_chip(other, abs(other_preset - preset) < 1e-6)

        for preset in SPEED_PRESETS:
            chip = QPushButton(f"{preset:g}x")
            chip.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            paint_chip(chip, abs(preset - self._speed) < 1e-6)
            use_hover_cursor(chip)
            chip.clicked.connect(lambda checked=False, p=preset: pick_speed(p))
            chip_rows.append((chip, preset))
            chips.addWidget(chip)
        # The slider moves the speed too - keep the chips honest under it.
        slider.valueChanged.connect(
            lambda v: [paint_chip(c, abs(pr - round(v / 100.0 / 0.05) * 0.05) < 1e-6)
                       for c, pr in chip_rows])
        panel.body_layout.addLayout(chips)

        panel.speed_value = value
        panel.speed_slider = slider
        panel.finish()
        self._show_panel(panel)

    def _leave_playback(self, message):
        """Everything leaving the current episode owes, in the order the
        owner sees it: the startup frame up *instantly*, the outgoing
        picture and sound genuinely stopped, its torrent handed back,
        and the position saved for next time.

        The frame used to be refused here and the old episode played on:
        pressing Next only wrote "Loading..." while `_show_backdrop`'s
        running-picture guard (correctly) declined to cover a picture
        that was still running - and mpv was never told to stop, so the
        old episode's audio ran under the whole source lookup (the
        owner's report, 22 August 2026: Next must "immediately stop the
        current playing and show bg image with the logo (like I just
        entered the vid player)"). Setting _awaiting_first_frame before
        the frame is what lets the backdrop cover; the stop is what
        makes it honest.

        Order matters twice over: the frame goes up before anything
        that can touch mpv or libtorrent (either can block the UI
        thread behind a torrent read - see torrent_engine._Torrent.
        wait_for), and _save_position runs before the stop only because
        it reads self._position, which the stop's time-pos=None events
        cannot clobber (None is dropped in _on_property)."""
        self._awaiting_first_frame = True
        self._startup_fraction = 0.0
        self._show_loading(message)
        # Hand the old episode's bandwidth over. Switching away does
        # not stop a torrent by itself: it keeps its priority-7 pieces
        # and its reads, so the episode being started competed with the
        # one being left - and those reads are what the UI thread ends
        # up waiting behind.
        self._release_playing_torrent()
        self._save_position()
        self._stop_playback()

    def _stop_playback(self):
        """Unload whatever mpv is playing, now, without waiting on it.

        command_async, not command: a synchronous stop travels through
        mpv's core, and the core can be parked mid-read on a torrent
        piece that is not there - the exact moment this runs is the
        moment the user is giving up on that source, so the UI thread
        must not queue behind it. The gate closes here too: with no file
        wanted, every property event still in flight is stale by
        definition."""
        self._file_ready = False
        self._loading_url = None
        if self.handle is None:
            return
        try:
            if hasattr(self.handle, "command_async"):
                self.handle.command_async("stop")
            else:                                   # pragma: no cover
                self.handle.command("stop")
        except Exception:
            logs.exception("Could not stop playback")

    def _change_episode(self, delta):
        """Step within this season, and refuse to step off the end of it.

        **This had no upper bound, and the consequence was not a dead
        button - it was playing the wrong thing.** Next on the last
        episode of season 4 asked for S04E05, which does not exist; the
        lookup's numbering fallback then tried the same episode number in
        season 1, found S01E05, and played that. A player that silently
        shows a different episode is worse than one that says no."""
        if not self.episode:
            return
        target = self.episode + delta
        if target < 1:
            return
        if delta > 0 and target > self._season_episode_count(self.season):
            self._show_status(
                "The episode you are looking for is not released yet.")
            self._sync_episode_buttons()
            return
        self._leave_playback("Loading the next episode...")
        self.episode = target
        self._panel_season = self.season   # prev/next stay within this season
        self._given_streams = None       # the handed-in list was for the old episode
        self._sync_episode_buttons()
        self._begin_episode()

    def _sync_episode_buttons(self):
        """Enable prev/next only where there is somewhere to go.

        Called on every episode change rather than set once: the season's
        episode count moves as `latest_available` does, so a Next that
        was correctly disabled last week should light up when the next
        episode airs."""
        episode = int(self.episode or 0)
        if not episode:
            return
        self.prev_btn.setEnabled(episode > 1)
        self.next_btn.setEnabled(episode < self._season_episode_count(self.season))

    def toggle_fullscreen(self):
        window = self._window
        if window is None or not hasattr(window, "toggle_fullscreen"):
            return
        window.toggle_fullscreen()
        self.fullscreen_btn.setText(
            ICON_EXIT_FULLSCREEN if window.isFullScreen() else ICON_FULLSCREEN)
        self._wake_controls()

    # ---- mpv callbacks (mpv's thread) --------------------------------
    def _mpv_property(self, name, value):
        try:
            self._bridge.prop.emit(name, value)
        except Exception:
            pass        # the page is going away; nothing to report to

    def _mpv_event(self, event):
        """mpv's own events, named the way mpv actually names them.

        **The id does not stringify as a constant, and the check here
        was written as though it did.** Measured 25 August 2026 against
        the vendored libmpv: `str(event.event_id)` reads
        `<MpvEventID 8 file-loaded>` and `<MpvEventID 7 end-file>` -
        lowercase, hyphenated, wrapped. So `endswith("END_FILE")` had
        never once matched, and the end-of-file branch below has been
        dead for as long as it has existed (its only job is marking an
        episode watched at the very end, which the WATCHED_FRACTION
        check on time-pos had been quietly covering).

        Matched on the readable name now, case-folded, so both this and
        the file-loaded gate that `_on_file_loaded` depends on actually
        fire."""
        try:
            name = str(getattr(event, "event_id", "")).lower()
            if "end-file" in name:
                self._bridge.ended.emit("eof")
            elif "file-loaded" in name:
                self._bridge.loaded.emit()
        except Exception:
            pass

    def _on_file_loaded(self):
        """mpv has opened a file - the second way the load gate opens.

        **The first way is a `path` property *change*, and a change is
        exactly what re-loading the same URL does not produce.** That is
        the shape behind the owner's "I changed the source from inside
        the player and it got stuck", 25 August 2026, and the trace of
        it reads:

            switch to source 2 -> prepare says wrong-episode
            _try_next_source  -> _play_stream(0)
            prepare_fastest   -> a url, and the race is won by the
                                 release that had just been switched
                                 *away* from, so it is byte-identical to
                                 the one already loaded
            _on_stream_prepared -> _load_into_mpv
            ... path stays None for the rest of the session

        `_load_into_mpv` handled the identical-URL case by issuing a
        stop first, so that `path` would fall to None and rise again -
        but that stop is `command_async` while `loadfile` is
        synchronous, and a synchronous command executes in the core
        ahead of an async one already queued. The stop therefore lands
        *after* the load and unloads the file that was just opened.

        Opening the gate on mpv's own `file-loaded` event removes the
        need for that stop entirely: the event fires whatever the path
        did, so a reload of the same URL is no longer a special case.
        The path check stays as well - the two agree in the ordinary
        case, and the path is the stricter of the two when a superseded
        load answers late."""
        if self._closing or self.handle is None:
            return
        if not self._loading_url:
            return
        self._file_ready = True

    # ---- property updates (Qt thread) --------------------------------
    def _on_property(self, name, value):
        if self._closing:
            return
        if name == "time-pos" and value is not None:
            if not self._file_ready:
                # A position from the file being *left*, not the one
                # being loaded - see _file_ready in __init__ for the
                # measured race this gate closes. Dropped whole: acting
                # on it once mis-declared the new source's first frame
                # and settled a seat with the old file's position.
                return
            if self._resume_target is not None and self._seat_reported is None:
                # **The first time-pos of a file opened at a seat is the
                # seek target itself, decoded or not.** Measured live,
                # 23 August 2026, Demon Slayer S01E01 resumed at 22:54
                # over a real swarm: mpv reported 1374.29 the instant
                # the file opened, then sat on exactly that number for
                # 16+ seconds while the pieces at that offset were still
                # downloading - no frame existed, hwdec had not even
                # engaged. Treating that report as playback declared the
                # first frame, said "Resumed From 22:54" over a stalled
                # screen, and disarmed the give-up watchdog. So the
                # report is recorded (the bar may show it) and nothing
                # else below runs until a position *different from it*
                # arrives - and only decoded frames move time-pos, so a
                # change is proof of picture.
                self._seat_reported = self._position = float(value)
                self.seek_bar.set_position(self._position)
                self._update_time_label()
                return
            if (self._resume_target is not None
                    and float(value) == self._seat_reported):
                return          # still the same undecoded report
            if self._awaiting_first_frame and float(value) > 0:
                # The first frame is the only proof that a source has
                # actually started; until it lands, "nothing on screen"
                # and "broken" look identical.
                self._awaiting_first_frame = False
                self._hide_status()
                # A switch that was announced is now finished - unless
                # it still owes a seat, in which case the toast keeps
                # saying so until the picture is there.
                if self._work_toast is not None and self._resume_target is None:
                    self._finish_working("Source Loaded")
                # The picture is up: only now is the connection's slack
                # spent on anything else. The subtitle search used to
                # start beside the source lookup and competed with it
                # for the path to first frame (the owner's "make the
                # auto subtitles load after the source loading end").
                self._ensure_subtitle_search()
                # And idle time on the episode after this one, so Next
                # answers from the cache instead of re-running the whole
                # source fan-out.
                self._prefetch_next_episode()
            self._position = float(value)
            self.seek_bar.set_position(self._position)
            self._update_time_label()
            self._check_watched()
            self._refresh_skip_button()
            self._maybe_prewarm_next()
            if self._resume_target is not None and self._position >= \
                    self._resume_target - RESUME_LANDED_TOLERANCE_S:
                # The seat has landed - the file was opened at it (see
                # _load_into_mpv's start=), so this fires on the first
                # honest position of the new file, not on a promise.
                seat = self._resume_target
                self._clear_seat()
                if self._work_toast is not None:
                    self._finish_working(f"Resumed From {_format_time(seat)}")
                else:
                    # A fresh open resumes with no switch toast up; the
                    # arrival still deserves its word.
                    show_toast(self._toast_anchor(),
                               f"Resumed From {_format_time(seat)}")
        elif name == "path":
            # The observed path is the load boundary: it names the new
            # file the moment mpv begins it, after the last event the
            # old file will ever emit. Matching against the url this
            # page asked for (rather than "any non-empty path") keeps a
            # superseded load from opening the gate for the one that
            # replaced it.
            self._file_ready = bool(value) and str(value) == \
                str(self._loading_url or "")
        elif name == "duration" and value:
            self._duration = float(value)
            self.seek_bar.set_duration(self._duration)
            self._update_time_label()
            # The file is loaded by the time a duration exists, so its
            # chapters are readable now - and AniSkip wants the length,
            # which is what lets it reject an interval belonging to a
            # different cut of the episode. Once per run, not once per
            # duration update.
            if not self._skips_asked:
                self._skips_asked = True
                self._skips = self._skips_from_chapters()
                self._refresh_skip_button()
                self._load_skip_times()
        elif name == "demuxer-cache-time" and value is not None:
            self.seek_bar.set_buffered(float(value))
        elif name == "pause":
            self._paused = bool(value)
            self.play_btn.set_paused(self._paused)
            if self._paused:
                self._wake_controls()
        elif name == "volume" and value is not None:
            self._volume = int(value)
            self.volume_slider.blockSignals(True)
            self.volume_slider.setValue(self._volume)
            self.volume_slider.blockSignals(False)
            self.volume_label.setText(f"{self._volume}%")
        elif name == "mute":
            self._muted = bool(value)
            self.mute_btn.setText(ICON_MUTED if self._muted else ICON_VOLUME)
        elif name == "speed" and value:
            self._speed = float(value)
            self.speed_btn.setText(f"{self._speed:g}x")
            self._sync_speed_panel()
        elif name == "track-list":
            self._tracks = list(value or [])
            # Before the pill is drawn, so it reads the track that is
            # actually going to be playing rather than the one mpv
            # opened on and is about to be moved off.
            self._apply_audio_default()
            self._auto_select_arabic_track()
            self._update_audio_pill()
            # An open tracks panel follows mpv's own answer: the pick
            # already moved the highlight optimistically (_pick_track),
            # and this is the confirmation - or the correction, when mpv
            # refused the track - arriving a beat later. Rows are synced
            # and repainted in place, never rebuilt for a joined or lost
            # track: mpv emits track-list on every sub_add too, and
            # tearing four native windows down over the video each time
            # is what the owner saw as the panel flickering. The rebuild
            # remains only for a change the panel structurally cannot
            # absorb (see _sync_track_rows).
            if self._panel is not None and getattr(self._panel, "kind", "") == "tracks":
                if not (self._sync_track_rows() and self._highlight_tracks()):
                    self._open_tracks_panel(rebuild=True)
        elif name == "paused-for-cache":
            # Only ever a note, never an error: a stalled buffer usually
            # recovers, and a dialog for it would fire constantly on a
            # slow source.
            if value and not self._status_visible():
                self.source_label.setText("Buffering...")
                # The label lives in the controls bar, which is hidden
                # most of the time - so the frame carries it as well.
                self._begin_buffer_frame()
            elif not value:
                self._end_buffer_frame()
                if self._streams:
                    self._update_source_display()
        elif name == "cache-buffering-state":
            self._buffering_percent = int(value or 0)
            self._update_startup_status()
            if self._buffer_frame_up:
                # _update_startup_status returns early once the first
                # frame has landed, so the mid-playback frame fills its
                # logo from the same number here.
                self.logo.set_fraction(
                    min(1.0, max(0.0, self._buffering_percent / 100.0)))

    def _on_ended(self, _reason):
        if self._closing:
            return
        self._check_watched(force=True)

    def _prefetch_next_episode(self):
        """Warm streams' result cache for the episode after this one.

        Sources only - nothing is prepared and no torrent is created, so
        no tracker is announced to and no bandwidth is taken from the
        episode actually playing. It just means pressing Next skips the
        several-second fan-out and goes straight to connecting.

        The *release* behind the top source is warmed separately and much
        later - see _maybe_prewarm_next, which has a safety condition
        this one does not need."""
        if self._prefetched or not self.episode or streams_module is None:
            return
        self._prefetched = True
        target = int(self.episode) + 1
        if target > self._season_episode_count(self.season):
            return
        season = self.season

        def worker():
            try:
                streams_module.find_streams(
                    self.entry, season=season, episode=target,
                    deadline=net.deadline_in(STREAM_BUDGET_S))
            except Exception:
                pass        # a failed prefetch is just a cold Next
        self._spawn(worker)

    # ---- warming the next episode's actual release --------------------
    def _maybe_prewarm_next(self):
        """Create the next episode's top torrent early, but only while it
        can cost the picture on screen nothing.

        Press-source-to-picture is ~12s and the owner wants five. Most of
        that is the torrent: metadata over DHT, then the first pieces of
        the file. Both can be paid before Next is pressed - and both are
        dangerous to pay early, because a second swarm downloading at
        full rate is competing with the episode being watched for one
        connection. That is not speculation: a source switch used to
        leave the outgoing torrent running, and the release being
        switched *to* downloaded at a sixth of its rate (see
        _switch_stream).

        So the condition is not "probably fine", it is exact: **the file
        being watched must already be complete.** torrent_engine gives
        the whole chosen file priority 7, so by the tail of an episode it
        usually is; `file_progress(...)["finished"]` says whether it is,
        and until it says so nothing is warmed. A direct-URL source is
        never warmed at all - mpv is still pulling that one over the same
        connection and the engine cannot tell us how much is left.

        Bounded to one release, started no earlier than PREWARM_AFTER_S
        into playback (the owner's "~100 sec from the current ep started
        playing" - it was the last 180s of the episode before that, see
        the constant for the judgement call), released the moment it
        stops being the thing the next episode wants (see _load_into_mpv)
        or the page closes (see _release_streams)."""
        if self._prewarm_asked or self._closing:
            return
        if not self.episode or not self._duration or streams_module is None:
            return
        if self._position < PREWARM_AFTER_S:
            return
        if not self._has_next_episode():
            return
        # Throttled: this is called from every time-pos update, which is
        # several a second, and the check below asks libtorrent for a
        # status.
        now = time.monotonic()
        if now - self._prewarm_checked < 1.0:
            return
        self._prewarm_checked = now
        # **The metadata half is paid at PREWARM_AFTER_S regardless**, and
        # that is the owner's ask ("make the next ep loads after ~100 sec
        # ... so that when I go to next it goes smoothly", 22 August
        # 2026). The completeness gate below is right about *data* and
        # wrong as a gate on everything: measured 23 August 2026, it held
        # the whole prewarm back for an entire live session, because a
        # streamed file is rarely complete before the episode ends - so
        # the owner's Next stayed cold, which is the case this exists for.
        #
        # Metadata is a few hundred KB, asks for no piece data at all
        # (torrent_engine.prefetch_metadata adds in upload mode and gives
        # the swarm straight back), and it is the fixed cost of a cold
        # Next - the part that does not get better with a good swarm. So
        # it is safe to pay while something is playing, and it is most of
        # what "goes smoothly" means.
        if not self._meta_prewarm_asked:
            self._meta_prewarm_asked = True
            self._spawn(self._prewarm_metadata_worker, self.season,
                        int(self.episode) + 1, self._run)
        if not self._playing_file_complete():
            return
        self._prewarm_asked = True
        self._spawn(self._prewarm_worker, self.season,
                    int(self.episode) + 1, self._run)

    def _playing_file_complete(self) -> bool:
        """Whether the episode on screen still needs the connection.

        False for anything that is not a finished torrent file, which
        includes every direct URL - the conservative answer, and the one
        that makes the prewarm safe to have at all."""
        try:
            stream = self._streams[self._stream_index] or {}
        except Exception:
            return False
        info_hash = str(stream.get("info_hash") or "").strip().lower()
        if not info_hash:
            return False
        try:
            from helpers import torrent_engine
            return bool((torrent_engine.file_progress(info_hash)
                         or {}).get("finished"))
        except Exception:
            return False

    def _prewarm_metadata_worker(self, season, episode, run):
        """The next episode's .torrent metadata into the disk cache, so
        pressing Next skips the metadata round trip.

        No piece data is requested (see torrent_engine.prefetch_metadata),
        so unlike `_prewarm_worker` this does not wait for the playing
        file to be complete. Never raises - it is speculation, and a
        failure must be indistinguishable from not having tried."""
        try:
            found = streams_module.find_streams(
                self.entry, season=season, episode=episode,
                deadline=net.deadline_in(STREAM_BUDGET_S))
        except Exception:
            return
        if self._closing or run != self._run:
            return
        candidate = next((s for s in (found or [])
                          if s.get("info_hash") and s.get("kind") != "drm"),
                         None)
        if candidate is None:
            return
        try:
            from helpers import torrent_engine
            if not torrent_engine.available():
                return
            torrent_engine.prefetch_metadata(
                str(candidate.get("info_hash")),
                trackers=candidate.get("trackers") or ())
        except Exception:
            return

    def _prewarm_worker(self, season, episode, run):
        """Never raises - this is pure speculation and a failure here
        must be indistinguishable from not having tried."""
        try:
            found = streams_module.find_streams(
                self.entry, season=season, episode=episode,
                deadline=net.deadline_in(STREAM_BUDGET_S))
        except Exception:
            return
        if self._closing or run != self._run:
            return
        candidate = next((s for s in (found or [])
                          if s.get("info_hash") and s.get("kind") != "drm"),
                         None)
        if candidate is None:
            return
        info_hash = str(candidate.get("info_hash")).strip().lower()
        # Re-asked here, not only before the lookup: find_streams can
        # take seconds, and in that time the episode may have moved on or
        # the picture may have gone back to needing the connection.
        if not self._playing_file_complete():
            return
        try:
            from helpers import torrent_engine
            if not torrent_engine.available():
                return
            # Same trackers rule as streams._prepare_with_own_engine: an
            # indexer that gave none would otherwise be left to find its
            # swarm on DHT alone, which is minutes.
            trackers = list(candidate.get("sources") or [])
            if not trackers:
                trackers = [f"tracker:{url}"
                            for url in streams_module.DEFAULT_TRACKERS]
            added = torrent_engine.add(
                info_hash, trackers=trackers, season=season, episode=episode,
                file_index=candidate.get("file_index"),
                metadata_timeout=streams_module.METADATA_TIMEOUT)
        except Exception:
            return
        if not added:
            return
        self._prewarmed.add(info_hash)
        if self._closing or run != self._run:
            # Left while this was running: give it straight back rather
            # than leaving a swarm running behind a closed page.
            self._release_prewarmed(keep=())
            return
        self._prewarm_hash = info_hash

    def _release_prewarmed(self, keep=()):
        """Hand back every warmed release except the ones named - and
        never the one on screen, whatever the caller asked for.

        That last guard is not belt-and-braces. `torrent_engine.add`
        returns the *existing* torrent when the hash is already running,
        so a release warmed for the next episode can turn out to be the
        one being watched - press Next while the warm-up is in flight and
        the two are the same object. Releasing it would stop playback
        dead, so the playing hash is protected here rather than at each
        of the three call sites."""
        keep = {h for h in keep if h}
        try:
            playing = str(self._streams[self._stream_index]
                          .get("info_hash") or "").lower()
        except Exception:
            playing = ""
        if playing:
            keep.add(playing)
        try:
            from helpers import torrent_engine
        except Exception:
            return
        for info_hash in list(self._prewarmed - keep):
            self._prewarmed.discard(info_hash)
            try:
                torrent_engine.release(info_hash)
            except Exception:
                pass

    def _update_time_label(self):
        self.pos_label.setText(_format_time(self._position))
        self.dur_label.setText(_format_time(self._duration))

    def _check_watched(self, force=False):
        if self._marked_watched or not self._duration:
            return
        if not force and self._position < self._duration * WATCHED_FRACTION:
            return
        self._marked_watched = True
        if _mark_watched(self.entry, self.season, self.episode):
            show_toast(self._toast_anchor(), "Marked As Watched")
        clear_resume(self.entry, self.season, self.episode)

    def _save_position(self):
        if self._closing or not self._duration or self._position <= RESUME_MIN_S:
            return
        if self._position > self._duration * RESUME_MAX_FRACTION:
            return
        try:
            save_resume(self.entry, self.season, self.episode,
                        self._position, self._duration,
                        release=self._release_record())
        except Exception:
            logs.exception("Could not save the resume position")

    # ---- panels ------------------------------------------------------
    def _guard_click(self):
        """Swallow the click this panel change belongs to - and only it.

        Called on every open, close and rebuild, and by the skip button.
        The thing being guarded against is one *physical press* whose
        geometry moves under it: the press picks a row, the row rebuilds
        the panel, and the release a hundred milliseconds later lands
        "over the video" where the row used to be. So name that press
        instead of timing it (see PANEL_CLICK_GUARD_S for what timing it
        cost).

        Two orders are possible and both are covered:

        * the 40ms poll already saw the button go down, so `_press_seq`
          *is* this press - guard that number;
        * Qt delivered the row's mousePressEvent first, which is the
          usual order for a Card (it emits on press), so the poll has yet
          to count it - guard the next number.

        And a change with **no press behind it at all** - a late batch of
        sources redrawing the list, mpv's track-list arriving, Escape -
        guards nothing. That case is exactly the owner's second click:
        under the old stopwatch a background rebuild armed a 450ms window
        against a click the user had not made yet."""
        self._panel_guard = time.monotonic()
        if self._mouse_down:
            self._guarded_press = self._press_seq
        elif _left_button_down():
            self._guarded_press = self._press_seq + 1
        else:
            self._guarded_press = -1

    def _close_panel(self):
        # Guarded on every close as well as every show: the click that
        # picked a row can close (or rebuild) the panel mid-press, and
        # the poll's late sample must not read that press as a click on
        # whatever the panel uncovered - see _guard_click.
        self._guard_click()
        if self._panel is not None:
            self._panel.deleteLater()
            self._panel = None
        self.setFocus()

    def _new_panel(self, title, kind, rebuild=False):
        """A fresh overlay panel, or None when this button just closed
        its own.

        Pressing Sources, Subtitles or any other lower-bar button while
        that button's panel is already up used to tear it down and build
        an identical one - which looks like nothing happening, and left
        no way to dismiss a panel except clicking the video (the owner's
        ask). The same button is now a toggle; a *different* button
        still swaps straight to its own panel.

        Callers must handle None by returning - see every _open_*_panel
        below.

        **Every reopen that is not a bar-button press must pass
        rebuild=True.** The toggle keys on the panel *kind*, so a panel
        redrawing itself - a drill-down, a picked row, a search result
        landing - looks identical to the button being pressed again and
        shuts instead. Shipped in 1.10.12 on both the streams drill-down
        (`_drill_streams`) and every Download row (`_dl_set`). The full
        list of internal reopens: _open_subtitle_panel, _open_tracks_panel,
        _open_streams_panel, _open_download_panel."""
        if (not rebuild and self._panel is not None
                and getattr(self._panel, "kind", "") == kind):
            self._close_panel()
            return None
        if (rebuild and self._panel is not None
                and getattr(self._panel, "kind", "") == kind):
            # Same panel, new contents: refill the native window that is
            # already up instead of destroying it and putting an
            # identical one in its place. See OverlayPanel.reset - with
            # progressive results this path now runs several times per
            # lookup, and each swap was a visible flash.
            try:
                self._panel.reset(title)
                return self._panel
            except RuntimeError:
                pass        # deleted under us; fall through to a new one
        self._close_panel()
        panel = OverlayPanel(self, title)
        panel.kind = kind
        panel.closed.connect(self._close_panel)
        panel.installEventFilter(self)
        self._panel = panel
        return panel

    # Column-1 language names, written the way Stremio writes them - the
    # owner's ask, 24 August 2026, twice, both times with a screenshot of
    # Stremio's panel: three columns, native-script names, a dot on what
    # is active. Codes not named here fall back to the bare code,
    # uppercased.
    _SUB_LANG_NAMES = {
        "ar": "العربية",
        "en": "English", "de": "Deutsch", "id": "Bahasa Indonesia",
        "ja": "日本語", "fr": "Français",
        "es": "Español", "pt": "Português", "it": "Italiano",
        "ru": "Русский",
        "tr": "Türkçe", "zh": "中文",
        "ko": "한국어",
    }

    # The pseudo-language holding the AI translations - a row of its own
    # in column 1, exactly where the owner's Stremio mock puts "Make
    # Arabic". Keeping it out of العربية matters: those are real Arabic
    # files, these are machine output, and the split is what lets a
    # person reach for either knowingly.
    _MAKE_ARABIC = "make-ar"

    @staticmethod
    def _sub_lang_key(value) -> str:
        """A language field collapsed to a two-letter key: 'ara', 'ar-sa'
        and 'arabic' are all one column-1 row."""
        code = str(value or "").strip().lower()
        if not code:
            return ""
        if subtitles_module is not None and subtitles_module.is_arabic_code(code):
            return "ar"
        return code[:2]

    def _active_sub_key(self, embedded) -> str:
        """Which column-1 row owns the subtitle actually showing: a
        language key, _MAKE_ARABIC for a loaded AI translation, or ""
        for Off. This is what the dot marks - state, where the
        selection ring marks only where the user is browsing."""
        track = next((t for t in embedded if t.get("selected")), None)
        if track is not None:
            return self._sub_lang_key(track.get("lang"))
        label = self._subtitle_label or "Off"
        if label == "Off":
            return ""
        if label.startswith("Arabic (AI)"):
            return self._MAKE_ARABIC
        match = next((s for s in self._subtitles
                      if (s.get("display_name") or s.get("release")) == label),
                     None)
        return self._sub_lang_key(match.get("lang")) if match else "ar"

    def _open_subtitle_panel(self, rebuild=False):
        """The Subtitles panel, laid out the way Stremio lays its out -
        the owner's ask, 24 August 2026, sent with a screenshot:
        **Languages | Variants | Settings**, title-case headers, a dot
        on the active language and the active variant, and the AI
        translations filed under their own "Make Arabic" language row
        rather than mixed into العربية.

        Which language column 2 shows is `self._subs_panel_lang`, held
        on the page rather than the panel because every pick rebuilds
        the panel (see _new_panel) and the browse position has to
        survive that."""
        # Opening the panel is a request for results, however early it
        # comes - the automatic search waits for the first frame, a
        # person asking must not (see _ensure_subtitle_search).
        self._ensure_subtitle_search()
        panel = self._new_panel("Subtitles", "subs", rebuild)
        if panel is None:
            return          # the same button closed it
        panel.panel_width = 880

        columns = QWidget()
        columns.setStyleSheet("background: transparent; border: none;")
        row = QHBoxLayout(columns)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(18)
        lang_col, variant_col, settings_col = QVBoxLayout(), QVBoxLayout(), QVBoxLayout()
        for column in (lang_col, variant_col, settings_col):
            # A sliver of right margin on the scrolling two, so a row's
            # selection ring is not shaved off by its own scrollbar.
            column.setContentsMargins(0, 0, 6, 0)
            column.setSpacing(6)

        def scrolling(column):
            # Languages and Variants scroll independently - the owner's
            # ask - so a long variants list never drags the settings
            # column off screen. Settings stays plain: three steppers
            # always fit.
            holder = QWidget()
            holder.setStyleSheet("background: transparent; border: none;")
            holder.setLayout(column)
            area = scroll_area(holder)
            area.setStyleSheet("background: transparent; border: none;")
            area.viewport().setStyleSheet("background: transparent;")
            area.setFixedHeight(SUBS_PANEL_COLUMN_H)
            return area

        row.addWidget(scrolling(lang_col), stretch=3)
        row.addWidget(scrolling(variant_col), stretch=4)
        row.addLayout(settings_col, stretch=3)
        panel.body_layout.addWidget(columns)

        embedded = [t for t in self._tracks if t.get("type") == "sub"]
        external = list(self._subtitles)
        arabic = [s for s in external
                  if subtitles_module is not None
                  and subtitles_module.is_arabic_code(s.get("lang"))]
        other = [s for s in external if s not in arabic]
        translators = []
        try:
            if ai_translate is not None:
                translators = list(ai_translate.providers_available())
        except Exception:
            translators = []

        active = self._active_sub_key(embedded)
        browsing = getattr(self, "_subs_panel_lang", "") or (active or "ar")

        def browse(lang):
            self._subs_panel_lang = lang
            self._open_subtitle_panel(rebuild=True)

        # ---- column 1: languages ------------------------------------
        panel.add_group("Subtitles Languages", into=lang_col)
        # The dot says "subtitles are off"; the ring stays the browse
        # position, which OFF is not one of - both ringed at once was
        # the owner's "the off button keeps highlighted even if I
        # choose another".
        panel.add_row("OFF", "", self._subtitles_off,
                      selected=False, dot=active == "", into=lang_col)
        languages = ["ar"]
        for track in embedded:
            key = self._sub_lang_key(track.get("lang"))
            if key and key not in languages:
                languages.append(key)
        for item in other:
            key = self._sub_lang_key(item.get("lang"))
            if key and key not in languages:
                languages.append(key)
        if other and (translators or ai_translate is not None):
            languages.append(self._MAKE_ARABIC)
        for key in languages:
            name = ("Make Arabic" if key == self._MAKE_ARABIC
                    else self._SUB_LANG_NAMES.get(key, key.upper()))
            panel.add_row(name, "",
                          lambda checked=False, k=key: browse(k),
                          selected=key == browsing, dot=key == active,
                          into=lang_col)
        lang_col.addStretch(1)

        # ---- column 2: variants of the browsed language -------------
        panel.add_group("Subtitles Variants", into=variant_col)
        shown = 0
        if browsing != self._MAKE_ARABIC:
            for track in embedded:
                if self._sub_lang_key(track.get("lang")) != browsing:
                    continue
                panel.add_row(self._track_label(track), "embedded",
                              lambda checked=False, t=track:
                                  self._pick_track("sid", t),
                              selected=bool(track.get("selected")),
                              dot=bool(track.get("selected")),
                              into=variant_col)
                shown += 1

        def add_external(items):
            nonlocal shown
            for item in items:
                label = (item.get("display_name")
                         or item.get("release") or "Subtitle")
                parts = [str(p) for p in
                         (item.get("source"), item.get("format"),
                          item.get("release")) if p]
                # Say so when a line was produced by machine translation
                # rather than written by a person - which one you picked
                # should not be a guess.
                if item.get("translated"):
                    parts.insert(0, "auto-translated")
                on = label == self._subtitle_label
                panel.add_row(label, " · ".join(parts),
                              lambda checked=False, r=item:
                                  self._pick_subtitle(r),
                              selected=on, dot=on, into=variant_col)
                shown += 1

        if browsing == "ar":
            add_external(arabic)
        elif browsing == self._MAKE_ARABIC:
            # **Every provider the owner has a key for, not just the
            # first** - four pasted keys once offered exactly one
            # translator; a group per provider makes which model does
            # the work a choice at the moment of picking.
            if translators and other:
                for provider in translators:
                    translator = ai_translate.label(provider)
                    for index, item in enumerate(other, start=1):
                        source_name = (item.get("display_name")
                                       or item.get("release") or "")
                        label = f"Arabic (AI) {index} {translator}"
                        on = label == self._subtitle_label
                        # r/p/l bound per row - the loop variables are
                        # rebound on the next iteration.
                        panel.add_row(
                            label, f"translated from {source_name}",
                            lambda checked=False, r=item, p=provider, l=label:
                                self._pick_subtitle(r, provider=p, label=l),
                            selected=on, dot=on, into=variant_col)
                        shown += 1
            elif other:
                panel.add_message(
                    "Add an OpenAI, DeepSeek, Gemini or Anthropic key in "
                    "Settings > API Keys to translate the other languages' "
                    "subtitles into Arabic.", into=variant_col)
                shown += 1
        else:
            add_external([item for item in other
                          if self._sub_lang_key(item.get("lang")) == browsing])

        if not shown:
            if not external and not embedded:
                panel.add_message(
                    "Searching..." if subtitles_module is not None
                    else "Subtitle search is not available in this build.",
                    into=variant_col)
            else:
                panel.add_message("Nothing found for this language yet.",
                                  into=variant_col)
        variant_col.addStretch(1)

        # ---- column 3: settings -------------------------------------
        panel.add_group("Subtitles Settings", into=settings_col)
        set_delay = panel.add_stepper(
            "Delay", self._delay_text(),
            lambda: self._nudge_delay(-SUB_DELAY_STEP),
            lambda: self._nudge_delay(SUB_DELAY_STEP),
            step_text=f"{SUB_DELAY_STEP:g}s", into=settings_col)
        set_size = panel.add_stepper(
            "Size", self._sub_size_text(),
            lambda: self._nudge_size(-SUB_SIZE_STEP),
            lambda: self._nudge_size(SUB_SIZE_STEP),
            step_text=f"{round(SUB_SIZE_STEP * 100 / SUB_SIZE_DEFAULT)}%",
            into=settings_col)
        # Left raises the line, right lowers it - the arrows point the
        # way the text moves rather than the way mpv's number goes.
        set_pos = panel.add_stepper(
            "Vertical Position", self._sub_pos_text(),
            lambda: self._nudge_sub_pos(SUB_POS_OFFSET_STEP),
            lambda: self._nudge_sub_pos(-SUB_POS_OFFSET_STEP),
            step_text=str(SUB_POS_OFFSET_STEP), into=settings_col)
        settings_col.addStretch(1)
        # Held on the panel, not captured in the nudge calls: the panel
        # is rebuilt whenever a subtitle is picked, and a setter
        # belonging to a deleted QLabel would take the process with it.
        panel.set_delay_text = set_delay
        panel.set_size_text = set_size
        panel.set_pos_text = set_pos
        panel.finish()
        self._show_panel(panel)

    def _sub_size_text(self):
        """The size as a percentage of the default, not the raw
        sub-font-size - the owner, 24 August 2026: "make the font size a
        percentage (%100) instead of numbers like 50". 55 is mpv's own
        default, so 100% is "what mpv would do untouched" - the same
        anchor `sub-scale` already divides by for .ass tracks
        (_apply_sub_style)."""
        return f"{round(self._sub_size * 100 / SUB_SIZE_DEFAULT)}%"

    def _delay_text(self):
        return f"{self._sub_delay:+.1f}s"

    def _subtitles_off(self):
        if self.handle is not None:
            try:
                # Same async route as _pick_track - the panel's Off row
                # must not hitch behind a parked demuxer either.
                if hasattr(self.handle, "command_async"):
                    self.handle.command_async("set", "sub-visibility", "no")
                    self.handle.command_async("set", "sid", "no")
                else:                               # pragma: no cover
                    self.handle["sub-visibility"] = False
                    self.handle["sid"] = "no"
            except Exception:
                logs.exception("Turning subtitles off failed")
        self._subtitle_label = "Off"
        # Off is a choice too, and it has to stick: without this the
        # remembered pick would be re-applied on the very next episode
        # and the switch would look like it had done nothing.
        self._sub_auto_done = True
        self._pending_sub_choice = None
        try:
            forget_subtitle_choice(self.entry)
        except Exception:
            logs.exception("Could not forget the subtitle choice")
        # Rebuilt, not toggled: this runs from the panel's own "Off" row,
        # and the point is to move the highlight onto it - closing the
        # panel under the press would look like the click missed.
        self._open_subtitle_panel(rebuild=True)

    def _pick_subtitle(self, result, provider=None, label=None):
        """Load one subtitle, saying so until it has actually loaded.

        `provider` names the AI translator the picked row promised, and
        is None for every row that is already the language it says -
        which is what makes "Other Languages" mean it. `label` is the
        row's own text, carried through so the loaded track is named
        the same thing the row was and the highlight lands back on it
        (see _name_subtitles on deriving one string in one place).

        **Sticky, not a 2s toast.** Picking a non-Arabic row runs the
        whole file through the AI translator, which for a 400-cue
        episode is minutes, not moments - and the old transient toast
        said "Translating to Arabic With OpenAI..." and then vanished
        long before anything happened. The owner reported exactly that:
        an alert, and then no subtitle and no explanation. One toast now
        lives for the whole job and is handed each step's wording, so
        the end of it is always visible - loaded, or why not."""
        if subtitles_module is None:
            return
        # Any pick, by hand or automatic, closes the door on this
        # episode's auto-apply - see _auto_apply_subtitle.
        self._sub_auto_done = True
        # Remembered only once it has actually loaded (_on_subtitle_file):
        # a row that turns out to be unfetchable, unparseable or refused
        # by a translator must not become the thing every future episode
        # of this title tries first.
        self._pending_sub_choice = (result, provider, label)
        self._drop_sub_toast()
        self._sub_toast = show_toast(self._toast_anchor(),
                                     "Loading Subtitle...", duration_ms=None)
        self._spawn(self._fetch_subtitle_worker, result, self._run,
                    provider, label)

    def _drop_sub_toast(self):
        """Close a subtitle toast still up from an earlier pick, so two
        picks in a row do not stack two sticky boxes in the corner."""
        toast, self._sub_toast = getattr(self, "_sub_toast", None), None
        if toast is None:
            return
        try:
            toast.close()
        except RuntimeError:
            pass        # already gone on the C++ side

    def _finish_sub_toast(self, text):
        """Put `text` into the live subtitle toast and let it fade."""
        toast, self._sub_toast = getattr(self, "_sub_toast", None), None
        finish_toast(toast, self._toast_anchor(), text)

    def _on_sub_note(self, message, run):
        """A step of the subtitle job, into the toast already up.

        Never fades on its own: the job is still running, and the
        translator's own progress lands here too."""
        if self._closing or run != self._run:
            return
        toast = getattr(self, "_sub_toast", None)
        if toast is None:
            self._sub_toast = show_toast(self._toast_anchor(), message,
                                         duration_ms=None)
            return
        try:
            toast.set_text(message, None)
        except RuntimeError:
            self._sub_toast = show_toast(self._toast_anchor(), message,
                                         duration_ms=None)

    def _nudge_delay(self, delta):
        self._sub_delay = round(self._sub_delay + delta, 2)
        self._apply_sub_delay()
        self._update_stepper("set_delay_text", self._delay_text())

    def _nudge_size(self, delta):
        self._sub_size = max(SUB_SIZE_MIN, min(SUB_SIZE_MAX, self._sub_size + delta))
        self._apply_sub_style()
        self._update_stepper("set_size_text", self._sub_size_text())

    def _apply_sub_delay(self):
        """Push the delay onto every subtitle mpv is showing.

        The secondary track as well as the primary: a release with a
        muxed signs track and a loaded Arabic one shows both, and moving
        only the primary leaves half the screen out of step.

        **Read back, don't assume.** "The delay does nothing on this
        subtitle" was investigated against real mpv (v0.41): the write is
        accepted and applied for one external .srt, for two, and with a
        secondary track selected - the step being a tenth of a second
        with no hold-to-repeat was the whole story. But a silent write is
        the one thing that would have made that investigation
        unnecessary, so the value is read back now and a disagreement is
        said out loud once rather than logged into nothing."""
        if self.handle is None:
            return
        for prop in ("sub-delay", "secondary-sub-delay"):
            try:
                self.handle[prop] = self._sub_delay
            except Exception:
                # secondary-sub-delay does not exist on every mpv; the
                # primary is the one that must not fail quietly.
                if prop == "sub-delay":
                    logs.exception("Subtitle delay change failed")
        try:
            actual = float(self.handle["sub-delay"])
        except Exception:
            return
        if abs(actual - self._sub_delay) < 0.001 or self._delay_warned:
            return
        self._delay_warned = True
        logs.warning(f"mpv kept sub-delay at {actual:.2f} after being asked"
                     f" for {self._sub_delay:.2f}")
        show_toast(self._toast_anchor(), "This Player Refused the Subtitle Delay")

    def _apply_sub_style(self):
        """Size the subtitles, whichever renderer is drawing them.

        **`sub-font-size` does not touch an .ass** - that is the owner's
        bug report, and it is mpv working as designed: ASS/SSA is drawn
        by libass from the script's own styles, and the `sub-*` font
        options apply to plain text only. `sub-scale` is the one that
        reaches it, because mpv's `sub-ass-override` defaults to
        `scale` (read back from this build's mpv, not assumed), which
        means "the script's styles, multiplied by sub-scale".

        So both are set, from one number: the font size for SRT and the
        equivalent multiplier for ASS. Measured on this build's mpv with
        a generated .ass and .srt - sub-delay already moved both, which
        is why only the size needed a second lever."""
        if self.handle is None:
            return
        try:
            self.handle["sub-font-size"] = self._sub_size
        except Exception:
            logs.exception("Subtitle size change failed")
        try:
            # Relative to mpv's own default, so "55" stays 1.0 and the
            # stepper's numbers keep meaning what they always meant.
            self.handle["sub-scale"] = round(
                self._sub_size / float(SUB_SIZE_DEFAULT), 3)
        except Exception:
            logs.exception("Subtitle scale change failed")

    def _update_stepper(self, name, text):
        """Write a new value into a stepper row if that row is still on
        screen. RuntimeError is the case that matters: the panel can be
        closed between a click and this call, and its QLabel is then a
        deleted C++ object behind a live Python name."""
        setter = getattr(self._panel, name, None) if self._panel is not None else None
        if setter is None:
            return
        try:
            setter(text)
        except RuntimeError:
            pass

    def _open_tracks_panel(self, rebuild=False):
        panel = self._new_panel("Audio and Subtitle Tracks", "tracks",
                                rebuild)
        if panel is None:
            return          # the same button closed it
        audio = [t for t in self._tracks if t.get("type") == "audio"]
        subs = [t for t in self._tracks if t.get("type") == "sub"]
        # Row cards by (property, track id), so a later pick moves the
        # highlight through card.set_selected instead of rebuilding the
        # whole panel - see _sync_track_rows/_highlight_tracks.
        panel.track_rows = {}
        if not audio and not subs:
            panel.add_message("This source has no selectable tracks.")
        if audio:
            panel.add_group("AUDIO")
            for track in audio:
                panel.track_rows[("aid", track.get("id"))] = panel.add_row(
                    self._track_label(track), track.get("codec") or "",
                    lambda checked=False, t=track: self._pick_track("aid", t),
                    selected=bool(track.get("selected")))
        if subs:
            panel.add_group("SUBTITLES IN THIS FILE")
            # `selected=` was missing here, so Off was the one row that
            # could never light up - the owner's report. "No sub track
            # chosen" is exactly what Off means, so it is selected when
            # nothing else in this group is.
            panel.track_rows[("sid", None)] = panel.add_row(
                "Off", "", lambda: self._pick_track("sid", None),
                selected=not any(t.get("selected") for t in subs))
            for track in subs:
                panel.track_rows[("sid", track.get("id"))] = panel.add_row(
                    self._track_label(track), track.get("codec") or "",
                    lambda checked=False, t=track: self._pick_track("sid", t),
                    selected=bool(track.get("selected")))
        panel.finish()
        self._show_panel(panel)

    def _sync_track_rows(self) -> bool:
        """Add or remove rows on the open tracks panel so they match
        `self._tracks`, without tearing the panel down.

        mpv re-emits track-list whenever a track joins or leaves - every
        external subtitle load does (sub_add fires mid-playback when a
        download or AI translation lands), and the old answer was
        `_open_tracks_panel(rebuild=True)`: a deleteLater of a native
        child window over the running video, measured as the panel
        visibly stuttering while the user was in it picking a track. A
        joined track is instead inserted at the end of its own group
        (mpv numbers new tracks upward, so that is also id order) and
        the panel re-laid to fit - a geometry change on the live native
        window, not a teardown.

        Returns False when only a rebuild can represent the change: no
        tracks panel is open, a whole group would have to appear (a
        panel built without any subtitles has no SUBTITLES header or Off
        row to insert under), or the cards are already deleted C++
        objects behind live Python names (the panel closed under us)."""
        panel = self._panel
        rows = getattr(panel, "track_rows", None) if panel is not None else None
        if rows is None or getattr(panel, "kind", "") != "tracks":
            return False
        current = {("aid" if t.get("type") == "audio" else "sid", t.get("id")): t
                   for t in self._tracks if t.get("type") in ("audio", "sub")}
        added = [key for key in current if key not in rows]
        gone = [key for key in rows
                if key != ("sid", None) and key not in current]
        if not added and not gone:
            return True
        try:
            layout = panel.body_layout
            for key in gone:
                card = rows.pop(key)
                card.hide()      # deleteLater is a frame away; hide now
                card.deleteLater()
            for key in sorted(added,
                              key=lambda k: (0 if k[0] == "aid" else 1, k[1])):
                prop, track = key[0], current[key]
                # After the last surviving row of the same group. The Off
                # row anchors an empty subtitle group - it is keyed
                # ("sid", None), so a first external sub lands under it.
                anchor = max((layout.indexOf(card)
                              for k, card in rows.items() if k[0] == prop),
                             default=-1)
                if anchor < 0:
                    return False    # the whole group is new: rebuild
                rows[key] = panel.add_row(
                    self._track_label(track), track.get("codec") or "",
                    lambda checked=False, p=prop, t=track:
                        self._pick_track(p, t),
                    index=anchor + 1)
        except RuntimeError:
            return False
        # Grow (or shrink) the panel to its new content - resizing the
        # live native window is smooth where recreating it is not.
        self._layout_overlays()
        return True

    def _highlight_tracks(self) -> bool:
        """Repaint the open tracks panel's rows from `self._tracks`.

        A pure repaint: `_sync_track_rows` has already made the row set
        match the track list, so every pass covers every row - which is
        what keeps exactly one row lit per group, with Off lit only
        while no sub track is selected. Returns False when there is no
        tracks panel, or its cards are deleted C++ objects (the panel
        closed between the event and this call).

        This exists because rebuilding stutters. `_pick_track` used to
        call `_open_tracks_panel(rebuild=True)`, which deleteLater'd the
        panel - a native child window over a running video - and built a
        fresh one, and mpv's own track-list confirmation a beat later did
        it a second time. Two teardowns per click of a native window
        above the video surface is exactly the flicker the owner saw."""
        panel = self._panel
        rows = getattr(panel, "track_rows", None) if panel is not None else None
        if not rows or getattr(panel, "kind", "") != "tracks":
            return False
        # **Only audio and sub tracks, and that filter is the whole of a
        # shipped bug.** This read `"aid" if type == "audio" else "sid"`
        # over every selected track, so mpv's *video* track - always
        # selected - became ("sid", <video id>). mpv numbers ids per
        # type, so the video track is id 1 and the first subtitle is
        # also id 1: the forged key lit that subtitle whenever a video
        # was playing, while no_sub below correctly lit Off as well.
        # Two rows lit at once in the subtitle group, which is exactly
        # what the owner reported ("it selected both and it is
        # confusing") - and it needed no clicking to appear.
        selected = set()
        for track in self._tracks:
            kind = track.get("type")
            if kind in ("audio", "sub") and track.get("selected"):
                selected.add(("aid" if kind == "audio" else "sid",
                              track.get("id")))
        no_sub = not any(t.get("type") == "sub" and t.get("selected")
                         for t in self._tracks)
        try:
            for key, card in rows.items():
                card.set_selected(
                    no_sub if key == ("sid", None) else key in selected)
        except RuntimeError:
            # The panel was closed between the click and this call; its
            # cards are deleted C++ objects behind live Python names.
            return False
        return True

    @staticmethod
    def _track_label(track):
        parts = [str(track.get("title") or "").strip(),
                 str(track.get("lang") or "").strip()]
        text = " · ".join(p for p in parts if p)
        return text or f"Track {track.get('id')}"

    def _pick_track(self, prop, track):
        if self.handle is None:
            return
        try:
            # Asynchronous on purpose: mpv applies an embedded track
            # switch on its core thread, and the synchronous property
            # write held this (UI) thread through the decoder
            # reconfigure - felt as the panel hitching right as a row
            # was clicked. The return value was never the feedback
            # anyway; mpv's track-list observation is what confirms or
            # corrects the pick (see _on_property).
            if hasattr(self.handle, "command_async"):
                self.handle.command_async(
                    "set", prop,
                    "no" if track is None else str(int(track.get("id"))))
                if prop == "sid" and track is not None:
                    self.handle.command_async("set", "sub-visibility", "yes")
            else:
                self.handle[prop] = ("no" if track is None
                                     else int(track.get("id")))
                if prop == "sid" and track is not None:
                    self.handle["sub-visibility"] = True
        except Exception:
            logs.exception("Track change failed")
        # The local list's `selected` flags are updated here, before the
        # rebuild, rather than waiting for mpv: the property write above
        # is asynchronous, and the track-list observation confirming it
        # arrives a beat later - so the rebuilt panel used to highlight
        # the *old* row until the panel was closed and reopened (the
        # owner's report). mpv's own answer still lands in _on_property,
        # which refreshes the panel again if it disagrees.
        kind = "audio" if prop == "aid" else "sub"
        picked = None if track is None else track.get("id")
        for entry in self._tracks:
            if entry.get("type") == kind:
                entry["selected"] = (picked is not None
                                     and entry.get("id") == picked)
        if kind == "audio":
            self._update_audio_pill()
        # In place, always: rows are added or removed to match the track
        # list and then repainted. A rebuild is only for a change the
        # panel structurally cannot absorb (see _sync_track_rows).
        if not (self._sync_track_rows() and self._highlight_tracks()):
            self._open_tracks_panel(rebuild=True)

    # How often the connection panel re-reads the swarm. The numbers
    # move constantly and nobody reads them faster than this; a second
    # also keeps the libtorrent status call off the frame budget.
    STATS_POLL_MS = 1000

    @staticmethod
    def _rate_text(bytes_per_second) -> str:
        """A download rate the way the owner's screenshot writes it -
        "6.55 MB/s". Decimal MB, matching every other size this app
        prints (release sizes come from the indexers in decimal too)."""
        rate = float(bytes_per_second or 0.0)
        if rate >= 1_000_000:
            return f"{rate / 1_000_000:.2f} MB/s"
        if rate >= 1_000:
            return f"{rate / 1_000:.0f} KB/s"
        return f"{int(rate)} B/s"

    def _playing_info_hash(self):
        """The info hash of the release actually on screen, or "" when
        this episode is not a torrent at all (a direct URL, a debrid
        link). The panel says so rather than showing three zeroes."""
        stream = None
        if 0 <= self._stream_index < len(self._streams):
            stream = self._streams[self._stream_index]
        return str((stream or {}).get("info_hash") or "").lower()

    def _mpv_number(self, name, default=None):
        """One mpv property as a float, or `default`.

        python-mpv raises for a property the current file has no answer
        for - `video-bitrate` before the first frame, `estimated-vf-fps`
        on an audio-only stream - and every one of those is a normal
        state for a panel that opens whenever the user presses the
        button, so each read is guarded rather than the whole refresh."""
        try:
            value = getattr(self.handle, name.replace("-", "_"))
        except Exception:
            return default
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def _mpv_text(self, name, default=""):
        """One mpv property as a string, or `default` - same guard as
        `_mpv_number`, and the same reason."""
        try:
            value = getattr(self.handle, name.replace("-", "_"))
        except Exception:
            return default
        text = "" if value is None else str(value).strip()
        return text or default

    def _playback_stats(self) -> dict:
        """What mpv can say about the picture on screen right now.

        Every field is optional: a stream that has not produced a frame
        yet, an audio-only file and a still-loading seek all answer
        nothing for most of these, and "-" is the honest reading rather
        than a zero that looks like a measurement."""
        width = self._mpv_number("width")
        height = self._mpv_number("height")
        fps = (self._mpv_number("estimated-vf-fps")
               or self._mpv_number("container-fps"))
        # **Two frame rates, not one, and conflating them cost a bug
        # report.** The owner, 25 August 2026: "the vid player frames are
        # still <60 make them at least 144!!!!" - reading this panel's
        # single "FPS" row, which was the *video's* rate. Measured the
        # same day on his own machine, Attack on Titan S01E01 (AV1
        # 1920x1080, d3d11va):
        #
        #     estimated-vf-fps        23.98   the file's own frame rate
        #     estimated-display-fps  239.99   the panel, driven by mpv
        #     vsync-ratio             10.0    240 / 23.976, exactly
        #     dropped 0, late 0, a/v sync -0.00009
        #
        # 23.976 is what the release contains; no player can turn that
        # into 144 without inventing frames, and the display side was
        # already running at the panel's full rate with nothing dropped.
        # So the row says which number it is, and the display rate sits
        # beside it where the claim can be checked.
        #
        # **And the row is named the right way round now** - the owner,
        # 25 August 2026, reading it at 10.0: "why is it 10 FPR???????".
        # It said "Frames per refresh", which would mean ten frames
        # squeezed into one refresh; mpv's `vsync-ratio` is the
        # reciprocal of that - "for how many vsyncs a frame is displayed
        # on average" - and 10 is exactly 240 / 23.976, one video frame
        # held for ten refreshes of his 240Hz panel. That is the correct
        # and healthy number for 24fps content on a 240Hz display; only
        # the label was wrong.
        display = self._mpv_number("estimated-display-fps")
        vsync = self._mpv_number("vsync-ratio")
        return {
            "resolution": (f"{int(width)}x{int(height)}"
                           if width and height else "-"),
            "fps": f"{fps:.2f}" if fps else "-",
            "display": f"{display:.0f} Hz" if display else "-",
            # Rounded, because mpv hands this over as a raw double and
            # the panel printed "10.132530120481928" across the owner's
            # screenshot. The digits after the second are display-clock
            # estimation noise, not information: 240/23.976 is 10.01,
            # and what says whether the pacing is healthy is the dropped
            # and late counts underneath, not this ratio's tail.
            "vsync": (f"{vsync:.2f}" if vsync else
                      self._mpv_text("vsync-ratio", "-")),
            # video-format ("h264"), not video-codec - the latter is
            # mpv's prose description and reads "H.264 / AVC / MPEG-4
            # AVC / MPEG-4 part 10" in a panel column a few dozen pixels
            # wide (measured on the real player, Attack on Titan S01E05).
            "video": self._mpv_text("video-format",
                                    self._mpv_text("video-codec", "-")),
            "audio": self._mpv_text("audio-codec-name", "-"),
            # hwdec-current is "no" when mpv fell back to software, and
            # that is worth reading rather than hiding: a 4K release
            # decoding on the CPU is the one case where the picture
            # stutters for a reason the swarm numbers cannot explain.
            "hwdec": self._mpv_text("hwdec-current", "-"),
            "bitrate": self._mpv_number("video-bitrate"),
            "dropped": self._mpv_number("frame-drop-count"),
            "delayed": self._mpv_number("vo-delayed-frame-count"),
            "buffer": self._mpv_number("demuxer-cache-duration"),
        }

    def _open_stats_panel(self, rebuild=False):
        """What is arriving, and what is being drawn from it.

        **Two groups now, and the second is the owner's ask, 24 August
        2026: "make the statistics also show information while vid is
        playing".** The panel used to hold the swarm and nothing else -
        peers, speed, completed - so an episode playing from a direct
        URL or a debrid link opened it to a single sentence saying there
        were no peers to report, and a torrent playing badly showed a
        healthy swarm with no way to see that the picture was the
        problem. Everything mpv knows about the frame on screen lives in
        mpv own properties; this reads them beside the swarm numbers.

        Read from `torrent_engine.stats` (libtorrent status for the
        handle this episode streams from) and from the mpv handle, and
        repolled on a timer while the panel is up. The timer is owned by
        the panel and dies with it, so nothing polls a swarm - or a
        player - nobody is looking at."""
        panel = self._new_panel("Statistics", "stats", rebuild)
        if panel is None:
            return          # the same button closed it
        info_hash = self._playing_info_hash()

        setters = {}
        # **While the loading screen is up, this panel used to say
        # nothing at all** - the owner, 25 August 2026: "the statistics
        # are not showing anything while logo loading". Everything below
        # is read from mpv and from a torrent handle, and during startup
        # there is neither: no file open, and often no release chosen
        # yet. So the first thing the panel carries is the same phase
        # the loading gauge is showing, which is the only honest answer
        # to "what is it doing" at that moment - and the one worth
        # having, because it names *which* step is slow.
        panel.add_group("Startup")
        setters["startup"] = panel.add_stat("Doing", "-")
        panel.add_group("Source")
        if info_hash:
            row = QHBoxLayout()
            row.setSpacing(26)
            columns = QWidget()
            columns.setStyleSheet("background: transparent; border: none;")
            columns.setLayout(row)
            setters["peers"] = panel.add_stat("Peers", "-", into=row)
            setters["speed"] = panel.add_stat("Speed", "-", into=row)
            setters["done"] = panel.add_stat("Completed", "-", into=row)
            row.addStretch(1)
            panel.body_layout.addWidget(columns)
        else:
            panel.add_message(
                "This episode is not streaming from a torrent, so there "
                "are no peers to report.")

        panel.add_group("Playback")
        # Two stats to a line rather than six across one: the panel is a
        # fixed-width column, and the same crowding add_stepper records
        # for the subtitle panel applies to any row that tries to hold
        # more than a couple of value pairs.
        for pairs in ((("resolution", "Resolution"), ("fps", "Video FPS")),
                      (("display", "Display"), ("vsync", "Refreshes per frame")),
                      (("video", "Video"), ("audio", "Audio")),
                      (("hwdec", "Decode"), ("bitrate", "Bitrate")),
                      (("dropped", "Dropped"), ("buffer", "Buffer"))):
            line = QHBoxLayout()
            line.setSpacing(26)
            holder = QWidget()
            holder.setStyleSheet("background: transparent; border: none;")
            holder.setLayout(line)
            for key, label in pairs:
                setters[key] = panel.add_stat(label, "-", into=line)
            line.addStretch(1)
            panel.body_layout.addWidget(holder)

        def refresh():
            if self._awaiting_first_frame:
                try:
                    _fraction, text = self._startup_snapshot()
                except Exception:
                    text = ""
                # One line: the snapshot's second line is the reassuring
                # half ("The first few seconds take longest.") and this
                # is a stats row, not a loading screen.
                setters["startup"](text.splitlines()[0] if text else "Starting...")
            else:
                setters["startup"]("Playing")
            if info_hash:
                try:
                    from helpers import torrent_engine
                    found = torrent_engine.stats(info_hash) or {}
                except Exception:
                    found = {}
                if not found:
                    # The handle was released (the episode moved on, or
                    # the engine reaped it) - say nothing rather than
                    # freezing the last numbers as though they were
                    # current.
                    setters["peers"]("-")
                    setters["speed"]("-")
                    setters["done"]("-")
                else:
                    setters["peers"](str(int(found.get("peers") or 0)))
                    setters["speed"](self._rate_text(found.get("download_rate")))
                    setters["done"](
                        f"{float(found.get('progress') or 0.0) * 100:.2f} %")
            live = self._playback_stats()
            setters["resolution"](live["resolution"])
            setters["fps"](live["fps"])
            setters["display"](live["display"])
            setters["vsync"](live["vsync"])
            setters["video"](live["video"])
            setters["audio"](live["audio"])
            setters["hwdec"](live["hwdec"])
            bitrate = live["bitrate"]
            setters["bitrate"](f"{bitrate / 1_000_000:.2f} Mbps"
                               if bitrate else "-")
            dropped, delayed = live["dropped"], live["delayed"]
            # Dropped and delayed together: a frame mpv threw away and a
            # frame it showed late are different faults with the same
            # symptom, and reading only the first says "nothing is
            # wrong" for the second.
            setters["dropped"](
                "-" if dropped is None
                else f"{int(dropped)} ({int(delayed or 0)} late)")
            buffer_s = live["buffer"]
            setters["buffer"]("-" if buffer_s is None else f"{buffer_s:.1f} s")

        refresh()
        timer = QTimer(panel)
        timer.timeout.connect(refresh)
        timer.start(self.STATS_POLL_MS)
        panel.stats_timer = timer
        panel.finish()
        self._show_panel(panel)

    def _open_settings_panel(self):
        """The gear: the controls that do not earn a button of their own
        on the bar, gathered in one place. Each row opens the full panel
        it names.

        Download is deliberately *not* here any more. It has its own
        button on the bar now, and one action reachable twice from two
        controls a hand's width apart is the same crowding that took the
        globe and the resolution screen off the row."""
        panel = self._new_panel("Settings", "settings")
        if panel is None:
            return          # the same button closed it
        panel.add_group("PLAYBACK")
        current = {}
        if 0 <= self._stream_index < len(self._streams):
            current = self._streams[self._stream_index]
        quality = (current.get("quality") or "").strip()
        panel.add_row("Resolution & source",
                      f"Currently {quality.upper()}" if quality
                      else "Choose a source", self._open_streams_root,
                      chevron=True)
        # Embedded translation is not here any more - it has its own
        # button on the bar, beside subtitles, at the owner's ask. The
        # audio pill in the top bar still opens the same panel.
        panel.finish()
        self._show_panel(panel)

    def _open_streams_root(self):
        """Open the resolution panel at its top level (the pill / gear).

        No `rebuild`: this is the *button* path, so pressing the pill
        while the resolution panel is already up toggles it shut, which
        is what every other bar button does."""
        self._streams_view = None
        self._open_streams_panel()

    def _open_streams_panel(self, rebuild=False):
        """Resolution & Source, as a drill-down.

        A real lookup comes back with thirty-odd streams and listing them
        flat makes picking 1080p a hunt through near-identical rows. What
        someone wants first is a resolution, so the top level is just the
        resolutions present - each a row with a chevron - and choosing one
        opens the individual sources at it (`streams.matching_quality`).
        `self._streams_view` holds which level is on screen: None for the
        resolution list, a quality string for the sources under it."""
        panel = self._new_panel("Resolution & Source", "streams", rebuild)
        if panel is None:
            return          # the same button closed it
        if not self._streams:
            panel.add_message("No sources were found.")
            panel.finish()
            self._show_panel(panel)
            return

        available = streams_module.qualities(self._streams) if streams_module else []
        # Streams whose resolution could not be parsed still have to be
        # reachable; they live under an "Other" bucket at the top level.
        has_unlabelled = any(not (s.get("quality") or "") for s in self._streams)

        if self._streams_view is None:
            self._fill_streams_root(panel, available, has_unlabelled)
        else:
            self._fill_streams_sources(panel, self._streams_view)
        panel.finish()
        self._show_panel(panel)

    def _fill_streams_root(self, panel, available, has_unlabelled):
        current = {}
        if 0 <= self._stream_index < len(self._streams):
            current = self._streams[self._stream_index]
        current_quality = _canonical_quality(current.get("quality"))

        panel.add_message("Choose a resolution")
        for quality in available:
            matches = streams_module.matching_quality(self._streams, quality)
            if not matches:
                continue
            # The most any release at this resolution has, not the first
            # row's - _rank caps seeders at 200 outside the preferred
            # resolution, so "up to N" read off matches[0] was regularly
            # not the largest number in the group it claimed to describe.
            seeders = max((int(m.get("seeders") or 0) for m in matches),
                          default=0)
            label = "4K (2160p)" if quality == "2160p" else quality
            panel.add_row(
                label,
                f"{len(matches)} source{'s' if len(matches) != 1 else ''}"
                + (f" · up to {seeders} seeders" if seeders else ""),
                lambda checked=False, q=quality: self._drill_streams(q),
                selected=quality == current_quality,
                chevron=True)
        if has_unlabelled:
            others = [s for s in self._streams if not (s.get("quality") or "")]
            panel.add_row(
                "Other",
                f"{len(others)} source{'s' if len(others) != 1 else ''}"
                " of unlabelled resolution",
                lambda: self._drill_streams(""),
                selected=not current_quality and self._stream_index >= 0
                and not (current.get("quality") or ""),
                chevron=True)

    def _fill_streams_sources(self, panel, quality):
        # A back row first, so the drill-down has a way out that is not
        # closing the whole panel.
        label = ("4K (2160p)" if quality == "2160p"
                 else (quality or "Other").upper() if quality else "Other")
        panel.add_row(f"{GLYPH_CHEVRON_LEFT}  All resolutions", "",
                      lambda: self._drill_streams(None))
        panel.add_message(f"Sources · {label}")
        if quality:
            sources = (streams_module.matching_quality(self._streams, quality)
                       if streams_module else [])
        else:
            sources = [s for s in self._streams if not (s.get("quality") or "")]
        # Most seeders first. The list is in _rank order, which caps
        # seeders at 200 outside the preferred resolution and now also
        # carries late-arriving sources appended on the end - so without
        # this the counts printed down the panel run in no order. Same
        # rule as the details page's source list (streams.list_sort_key)
        # so the two cannot disagree about what "best" means.
        if streams_module is not None:
            sources = sorted(sources, key=streams_module.list_sort_key)
        for stream in sources:
            index = self._streams.index(stream)
            seeders = int(stream.get("seeders") or 0)
            meta = stream.get("reason") or ""
            if not meta:
                release = (stream.get("title") or "").strip()
                size = (streams_module.format_size(stream.get("size_bytes"))
                        if streams_module else "")
                parts = [f"{seeders} seeders" if seeders else "",
                         size,       # the file's size, the owner's ask
                         release[:64] if release else (stream.get("kind") or "")]
                meta = " · ".join(p for p in parts if p)
            title = " · ".join(str(p) for p in
                               (stream.get("quality"), stream.get("source")) if p)
            panel.add_row(title or stream.get("title") or "Source", meta,
                          lambda checked=False, i=index: self._switch_stream(i),
                          selected=index == self._stream_index)

    def _drill_streams(self, view):
        """Move between the resolution list (None) and one resolution's
        sources (a quality), rebuilding the panel in place.

        **rebuild=True is the whole point of this call.** Without it the
        reopen lands on `_new_panel`'s same-kind toggle and the panel
        that was drilling *closes* instead - the 1.10.12 regression the
        owner reported as "clicking 4K shuts the panel". Every internal
        reopen carries it; only a bar button leaves it False."""
        self._streams_view = view
        self._open_streams_panel(rebuild=True)

    # ---- downloading -------------------------------------------------
    def _download_folder(self):
        """Where this player's downloads go - the last folder used
        anywhere in the app until the user changes it here."""
        if self._dl_folder is None:
            from windows import downloads_page
            self._dl_folder = downloads_page.saved_folder()
        return self._dl_folder

    def _dl_available_qualities(self):
        if streams_module is None:
            return []
        return streams_module.qualities(self._streams)

    def _dl_default_quality(self):
        """The preferred resolution when the swarm actually has it.

        Falling back to "best available" rather than to the preference
        itself: a preference for 1080p on a release that only exists in
        720p would otherwise queue a job whose quality filter matches
        nothing, and the honest offer is the resolutions that are there.
        Computed once per player, so a pick made here is not silently
        reset when the stream list is refreshed mid-panel."""
        if self._dl_quality_set:
            return self._dl_quality
        self._dl_quality_set = True
        preferred = app_settings.get_preferred_resolution()
        available = self._dl_available_qualities()
        self._dl_quality = preferred if preferred in available else None
        return self._dl_quality

    def _open_download_panel(self, rebuild=False):
        """Scope, resolution and subtitle in one panel, then Download.

        An OverlayPanel rather than a QDialog, like every other choice in
        the player: these controls are native child windows over the
        video (see the module docstring), and a modal dialog in the
        middle of a film is a heavier interruption than the panel the
        same buttons beside it already open."""
        # The subtitle-to-save rows below come from the same search the
        # subtitle panel shows; opening this panel is the same request
        # (see _ensure_subtitle_search).
        self._ensure_subtitle_search()
        panel = self._new_panel("Download", "download", rebuild)
        if panel is None:
            return          # the same button closed it
        self._dl_default_quality()

        if self.episode:
            count = self._episode_count()
            panel.add_group("WHAT TO SAVE")
            panel.add_row(
                "This episode",
                f"Season {int(self.season or 1)}, episode {int(self.episode)}",
                lambda: self._dl_set("scope", "episode"),
                selected=self._dl_scope == "episode")
            panel.add_row(
                "The whole season",
                f"Season {int(self.season or 1)} - "
                f"{count} episode{'s' if count != 1 else ''}",
                lambda: self._dl_set("scope", "season"),
                selected=self._dl_scope == "season")
        else:
            self._dl_scope = "episode"

        panel.add_group("RESOLUTION")
        available = self._dl_available_qualities()
        panel.add_row("Best available",
                      "Whatever the fastest source is offering",
                      lambda: self._dl_set("quality", None),
                      selected=self._dl_quality is None)
        for quality in available:
            label = "4K (2160p)" if quality == "2160p" else quality
            matches = (streams_module.matching_quality(self._streams, quality)
                       if streams_module else [])
            panel.add_row(
                label,
                f"{len(matches)} source{'s' if len(matches) != 1 else ''}",
                lambda checked=False, q=quality: self._dl_set("quality", q),
                selected=self._dl_quality == quality)
        if not available:
            panel.add_message("Sources are still being looked up - "
                              "best available is what will be saved.")

        # Which of the two is "the original" depends on the title, and
        # calling English a dub for a live-action series read as a bug
        # even once the default was right (see _dl_audio's note).
        japanese_first = "jpn" in self._audio_language_preference()
        panel.add_group("AUDIO")
        panel.add_row("Japanese" + (" (original)" if japanese_first else " dub"),
                      "The ordinary fansub releases" if japanese_first
                      else "Prefers dual-audio releases when one exists",
                      lambda: self._dl_set("audio", "jp"),
                      selected=self._dl_audio == "jp")
        panel.add_row("English" + (" dub" if japanese_first else " (original)"),
                      "Prefers dual-audio releases when one exists"
                      if japanese_first else "The ordinary releases",
                      lambda: self._dl_set("audio", "en"),
                      selected=self._dl_audio == "en")

        panel.add_group("SUBTITLE TO SAVE ALONGSIDE")
        panel.add_row("None", "Video only",
                      lambda: self._dl_set("subtitle", None),
                      selected=self._dl_subtitle is None)
        arabic = [s for s in self._subtitles
                  if str(s.get("lang", "")).lower().startswith("ar")]
        for item in arabic:
            label = item.get("release") or item.get("name") or "Subtitle"
            parts = [str(p) for p in (item.get("source"), item.get("format")) if p]
            if item.get("translated"):
                parts.insert(0, "auto-translated")
            panel.add_row(label, " · ".join(parts),
                          lambda checked=False, r=item: self._dl_set("subtitle", r),
                          # By value, not identity: a fresh subtitle
                          # search replaces the whole list, and an
                          # identity check would silently unpick the row
                          # the user had already chosen.
                          selected=self._dl_subtitle == item)
        if not arabic:
            panel.add_message("No Arabic subtitles have been found for this "
                              "yet - open the Subtitles panel to search.")
        panel.finish()

        folder_btn = _text_button(os.path.basename(self._download_folder())
                                  or self._download_folder(),
                                  f"Saving to {self._download_folder()}")
        folder_btn.clicked.connect(self._dl_pick_folder)
        panel.footer_layout.addWidget(self._dl_footer_row("Folder", folder_btn))

        start = _text_button("Download", "Add this to the download queue")
        start.setStyleSheet(
            start.styleSheet()
            + f"QPushButton {{ background: {theme.ACCENT_GRADIENT};"
              f" color: {theme.ON_ACCENT};"
              f" border: 1px solid {theme.ACCENT}; font-weight: 700; }}"
              f"QPushButton:hover {{ background: {theme.ACCENT_GRADIENT_HOVER}; }}")
        start.clicked.connect(self._dl_start)
        panel.footer_layout.addWidget(start)
        self._show_panel(panel)

    @staticmethod
    def _dl_footer_row(name, button):
        row = QWidget()
        row.setStyleSheet("background: transparent; border: none;")
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        title = QLabel(name)
        title.setStyleSheet(
            f"color: {theme.TEXT_MUTED}; font-size: 11pt;"
            f" background: transparent; border: none;")
        layout.addWidget(title)
        layout.addStretch(1)
        # Elided by fixing the width rather than the text: the button
        # carries the full path in its tooltip, and a folder name is what
        # is worth reading at a glance.
        button.setFixedWidth(170)
        layout.addWidget(button)
        return row

    def _dl_set(self, field, value):
        setattr(self, f"_dl_{field}", value)
        if field == "quality":
            self._dl_quality_set = True
        # rebuild=True, same reason as _drill_streams: an internal reopen
        # of the panel's own kind would otherwise hit the toggle and shut
        # it, so every scope/quality/audio/subtitle pick closed the
        # Download panel instead of moving its highlight.
        self._open_download_panel(rebuild=True)

    def _dl_pick_folder(self):
        from windows import downloads_page
        picked = downloads_page.choose_folder(self, self._download_folder())
        if not picked:
            return
        self._dl_folder = picked
        self._open_download_panel(rebuild=True)

    def _dl_start(self):
        folder = self._download_folder()
        try:
            if self._dl_scope == "season" and self.episode:
                numbers = list(range(1, self._episode_count() + 1))
                if not confirm(
                        self, "Download Season",
                        f"Queue all {len(numbers)} episodes of season "
                        f"{int(self.season or 1)} for download?"):
                    return
                downloads.queue_season(
                    self.entry, season=self.season, episodes=numbers,
                    quality=self._dl_quality, subtitle=self._dl_subtitle,
                    audio=self._dl_audio, folder=folder)
                message = f"Queued {len(numbers)} Episodes"
            else:
                downloads.queue_episode(
                    self.entry, season=self.season, episode=self.episode,
                    quality=self._dl_quality, subtitle=self._dl_subtitle,
                    audio=self._dl_audio, folder=folder)
                message = "Queued for Download"
        except Exception:
            # Queueing writes a file and starts a thread; neither is
            # allowed to take the player down mid-playback.
            logs.exception("Could not queue a download")
            show_toast(self._toast_anchor(), "Could Not Queue That Download")
            return
        self._close_panel()
        show_toast(self._toast_anchor(), message)

    def _switch_stream(self, index):
        """Swap source without losing the seat - a stalling source is
        exactly when this gets used, and restarting from zero would make
        it useless.

        **The resolution asked for is remembered, and that is the fix
        for "I change 1080p to 4K and it keeps playing the same
        thing".** When the picked release failed, `_try_next_source`
        walked the list from index 0 - which is the top-ranked source,
        normally the preferred 1080p - so a failed 4K pick landed
        silently back on the release that was already playing. Nothing
        said so, because from the code's point of view a source had been
        found. See _try_next_source, which now stays inside this
        resolution.

        The torrent being left behind is released too. It is not paused
        by switching away from it: it keeps its priority-7 pieces and
        goes on pulling at full rate, so the release being switched *to*
        was competing with the one being abandoned for the same
        connection - which is most of why a switch felt like it never
        happened.

        **And it says so out loud, because otherwise it says nothing at
        all.** Measured 22 August 2026 on the real widget, a switch made
        while a picture is running: the status box stays hidden, the
        loading backdrop refuses to cover a running picture (correctly -
        see _show_backdrop), and `_show_loading_soon`'s 350ms guard is
        the *most* that could ever have appeared. Probed at +0, +150,
        +400, +1000 and +2000ms after the pick, nothing whatsoever was
        on screen while the old source went on playing. A toast is the
        right instrument here for the same reason the backdrop is not:
        it is a window of its own, so it sits over the video without
        blacking it out."""
        self._close_panel()
        try:
            chosen = self._streams[index]
        except (IndexError, TypeError):
            return
        self._requested_quality = _canonical_quality(chosen.get("quality"))
        # A fresh pick by hand is a fresh answer to "which resolution",
        # so an earlier episode-long cross-over does not bind it.
        self._quality_given_up = False
        # Read before anything below can disturb it - the stop emits
        # time-pos events, and this seat is where the switch has to land.
        seat = self._position
        quality = (chosen.get("quality") or "").strip().upper()

        # **The old file is stopped, not left running.** This is the
        # owner's "it keeps loading forever, and sometimes only the audio
        # plays from the old source", 23 August 2026, and it is the same
        # defect `_leave_playback` was given on 22 August for Next - which
        # this path never received. Three things were wrong at once and
        # the stop fixes all three:
        #
        #  1. mpv was never told to unload, so for the whole prepare
        #     (up to RACE_TIMEOUT) it went on decoding the outgoing file -
        #     which is the audio the owner hears - while the release
        #     underneath it had already been handed back one line later.
        #  2. `_release_playing_torrent` removed that torrent from the
        #     session while mpv still had a read open on its URL, so the
        #     picture froze and only the already-demuxed audio survived.
        #     Releasing now happens *after* the stop, in that order.
        #  3. The forever-loading half: `_file_ready` reopens only on an
        #     mpv `path` property **change** (_on_property), and
        #     torrent_engine.stream_url is a pure function of the info
        #     hash - so switching to a release that resolves to the URL
        #     already loaded handed mpv a byte-identical path, `path`
        #     never changed, the gate stayed shut, every time-pos was
        #     dropped, `_awaiting_first_frame` never cleared and the
        #     "Skipping to X:XX..." frame painted forever over a file
        #     that was in fact playing. A stop drives `path` to None
        #     first, so the reload is always a change.
        self._awaiting_first_frame = True
        self._startup_fraction = 0.0
        self._show_loading(
            f"Loading Source{f' ({quality})' if quality else ''}...")
        self._save_position()
        self._stop_playback()
        self._release_playing_torrent(keep=chosen.get("info_hash"))
        self._say_working(f"Loading Source{f' ({quality})' if quality else ''}...")
        # Picked by hand from the sources panel - same rule as a
        # pick off the details page (see _prepare_stream_worker).
        self._play_stream(index, resume_at=seat, solo=True)

    # ---- what the player says while it is busy over a live picture ----
    def _say_working(self, text):
        """Raise or re-word the one sticky toast the player uses to
        narrate work that happens *while a picture is already up*.

        A toast rather than the loading frame, and that is the whole
        reason this exists: `_show_backdrop` refuses to cover a running
        picture (rightly), and `_hide_status` has already stopped the
        startup gauge by the time these waits begin - so a source switch
        and a resume seek were both narrated to surfaces that could not
        be shown. A toast is a window of its own and sits over the video
        without blacking it out.

        Sticky (`duration_ms=None`) rather than a 2s box: the wait it
        describes is a torrent finding peers or fetching the pieces at a
        seek target, which is seconds to tens of seconds, and a message
        that fades before the thing it describes has happened is worse
        than none.

        **One box, re-read.** A toast that has already reported its
        result is reused while it is still on screen, rather than a
        second one being raised beside it: every toast in this app is
        anchored to the same bottom-right corner, so two at once overlap
        - measured in the smoke pass, where "Skipping To 6:40..." went up
        under a "Resumed From 2:00" that still had 1.2s of its dwell
        left. Hence _finish_working keeps the reference."""
        for candidate in (self._work_toast, self._spent_toast):
            if candidate is None:
                continue
            try:
                candidate.set_text(text, None)
            except RuntimeError:
                continue        # closed and deleted on the C++ side
            self._work_toast, self._spent_toast = candidate, None
            return
        self._work_toast = show_toast(self._toast_anchor(), text, None)
        self._spent_toast = None

    def _finish_working(self, text=None):
        """Close out that toast, with a last word or without one.

        Called from every end the work can have: the first frame of the
        new source, the seek landing, the page giving up on the
        resolution, and a fresh episode - a sticky toast left behind
        would otherwise sit over the next thing the user did.

        A box given a last word is kept in `_spent_toast` for the couple
        of seconds it is still readable, so more work starting in that
        window re-reads it instead of stacking a second one in the same
        corner (see _say_working)."""
        toast, self._work_toast = self._work_toast, None
        self._spent_toast = None
        if toast is None:
            return
        try:
            if text:
                finish_toast(toast, self._toast_anchor(), text)
                self._spent_toast = toast
            else:
                toast.close()
        except RuntimeError:
            pass

    def _release_playing_torrent(self, keep=None):
        """Give back the torrent that is playing right now.

        Called when the user switches away from it deliberately. Never
        raises and never touches a pinned torrent - a download owns its
        own (see torrent_engine.pin)."""
        try:
            current = self._streams[self._stream_index]
        except (IndexError, TypeError):
            return
        info_hash = current.get("info_hash")
        if not info_hash or info_hash == keep:
            return
        try:
            from helpers import torrent_engine
            torrent_engine.release(info_hash)
        except Exception:
            pass

    def _show_panel(self, panel):
        self._guard_click()
        # Geometry first - see _show_status.
        self._layout_overlays()
        panel.show()
        # _raise_native for the same reason as the rest: this runs on
        # every *rebuild* too, and stream results arrive progressively -
        # so a list being read is restacked several times per lookup,
        # under the pointer that is about to press a row.
        _raise_native(panel)
        _set_window_alpha(panel, BAR_ALPHA)
        self._wake_controls()

    # ---- status ------------------------------------------------------
    def _show_status(self, text):
        """Put the centred message box up.

        `_layout_overlays` runs *before* `show()`, and that order is the
        whole point. These overlays are native child windows, which
        Windows composites itself; a freshly created one has Qt's default
        widget geometry, so showing it first painted a small box in the
        top-left corner of the frame for one frame before this method
        moved it to the middle. That one-frame box in the corner is the
        "window flashing open and closed" - it is not a window of its
        own, and every overlay here now gets its geometry before it is
        allowed on screen."""
        # A message on screen outranks a loading frame still waiting on
        # its delay - firing after this would paint the frame over the
        # words (see _show_loading_soon).
        self._loading_delay.stop()
        # And it outranks the gauge: a centred message is a dead end
        # ("no source would start"), so the ticker stops and the
        # loading flag drops - which is what _update_startup_status
        # checks before it may paint the frame back up. _show_loading's
        # no-logo fallback, the one caller for which this box is the
        # loading state itself, re-arms both right after this returns.
        self._startup_ticker.stop()
        self._loading_visible = False
        self.status.setText(text)
        # _layout_overlays sizes the box whether or not it is showing -
        # it used to skip a hidden one, which is exactly the case that
        # needed positioning.
        self._layout_overlays()
        self._show_backdrop()
        self.status.show()
        self.status.raise_()
        # The bars' own veil - see the status stylesheet for why this
        # stopped being colour-keyed.
        _set_window_alpha(self.status, BAR_ALPHA)
        _round_overlay(self.status)

    def _show_loading_soon(self, text=""):
        """The loading frame, only once the wait proves real.

        Switching to another episode while the season's source is
        already active gets a first frame in well under the guard, and
        the full-window loading frame blinking up for those few frames
        is the "opens a small window and closes it fast" the owner
        reported from the episode list. So a switch arms a short timer
        instead of showing anything, and _pending_loading_show puts the
        frame up only if the new episode's first frame still has not
        arrived - a fast switch shows nothing at all, and a slow one
        loses 350ms of spinner nobody needed. The first episode of a
        page skips the delay: nothing is on screen yet, and a blank
        page saying nothing reads as hung."""
        if not self._streams_started:
            # A fresh episode's wait starts the gauge over. Not on the
            # "Connecting..." a retry or a source switch raises:
            # progress already made - sources found, a swarm answering -
            # is still made, and a gauge that snaps back reads as
            # breakage rather than as a phase change.
            self._startup_fraction = 0.0
        if self._run <= 1:
            self._show_loading(text)
            return
        self._pending_loading_text = text
        self._loading_delay.start()

    def _pending_loading_show(self):
        if self._closing or not self._awaiting_first_frame:
            return
        if self._status_visible():
            return    # a real message (an error) outranks the frame
        self._show_loading(self._pending_loading_text)

    def _show_loading(self, text=""):
        """The loading state, wordless (the owner's ask): the title's
        backdrop with its logo pulsing and filling over the middle, and
        no message box at all. _show_status is now only for dead ends -
        things that stopped and need saying - never for progress.

        The one exception is a title with no logo (TMDB has none, or no
        key is set): there the words were the only thing on screen
        saying "working, not hung", so `text` is shown for exactly that
        case and dropped the moment a logo exists."""
        # While a switch's grace timer runs, every caller lands here
        # eventually - _update_startup_status included, which mpv's
        # buffering callbacks fire straight away on a load. Deferring
        # them all in this one place is what keeps a fast episode
        # switch from flashing the frame through a side door; the timer
        # slot shows whatever text arrived last.
        if self._loading_delay.isActive():
            self._pending_loading_text = text
            return
        self._loading_visible = True
        if self.logo.has_logo() or not text:
            self.status.hide()
            self._layout_overlays()
            self._show_backdrop()
        else:
            self._show_status(text)
            # _show_status marks a dead end - clears the flag, stops the
            # gauge - but this caller is the no-logo loading fallback,
            # where the box *is* the loading state. Undo both.
            self._loading_visible = True
        # What keeps the gauge moving while the phases underneath are
        # silent - see _update_startup_status. Runs only while a wait
        # is on screen; _hide_status and a dead-end _show_status stop it.
        self._startup_ticker.start()

    def _show_backdrop(self, force=False):
        """Put the loading frame up behind whatever the status box says.

        Raised over the video surface and then left there while the
        message is up - the bars raise themselves over it in turn
        (_wake_controls), and the order between native children is real
        window z-order, so each raise_ has to happen after the thing it
        must sit above.

        **Never over a running picture.** The status box is also how the
        player says things mid-episode ("that episode is not out yet"),
        and this covers the whole window - putting it up then would
        black out the video behind a one-line notice. A frame on screen
        (`_position` past zero with nothing being waited for) is the
        test, not which message it is.

        `force` is the one exception, and it has exactly one caller:
        _show_buffer_frame, where the picture is *not* running - mpv has
        stopped it dead on an empty cache - and what goes up there is
        the stall *badge* (logo over the frozen frame, no backdrop
        image - see _show_buffer_frame), not this full frame. Nothing
        else may pass it."""
        if self._closing:
            return
        if not force and self._position > 0 and not self._awaiting_first_frame:
            return
        if not force:
            # A full-frame caller (a cold open, Next's instant reset via
            # _leave_playback). If the stall badge is still up from a
            # stall the moment before, put the widget back into its
            # full-window shape first - after the guard above, so a
            # status box shown over a *running* picture can never flip
            # the badge into a full frame over the video.
            self._reset_stall_frame()
        self.backdrop.show()
        _raise_native(self.backdrop)
        self.logo.setVisible(self.logo.has_logo())
        for overlay in (self._episode_bar, self._panel, self.controls,
                        self.top_bar):
            if overlay is not None and overlay.isVisible():
                # _raise_native, not raise_: a buffer stall calls this on
                # every `paused-for-cache` flip, and restacking the panel
                # beside a press on it loses that press (see _raise_native).
                _raise_native(overlay)

    def _hide_status(self):
        self._loading_delay.stop()
        self._startup_ticker.stop()
        self._startup_text = ""
        self._loading_visible = False
        self.status.hide()
        # The loading frame goes with it: it covers the whole window, so
        # leaving it up would hide the first frame it was waiting for.
        self.backdrop.hide()
        # And the buffering frame is that same widget, so its flag has to
        # go too or _end_buffer_frame would think it is still up - and
        # its badge shape with it, or the next cold open would show a
        # tiny rounded backdrop.
        self._buffer_delay.stop()
        self._buffer_frame_up = False
        self._reset_stall_frame()

    # ---- the loading frame during a mid-playback stall ----------------
    def _begin_buffer_frame(self):
        """Arm the stall badge for a stall that is still going in
        BUFFER_FRAME_GUARD_MS.

        Two owner asks, one day apart. 22 August: show the loading
        image while buffering - before that, a stall mid-episode only
        wrote "Buffering..." into the source label in the controls bar,
        which is hidden whenever the controls are, so most stalls said
        nothing whatsoever. 23 August, after seeing it: "make it just
        shows the logo not the bg image (keep the video with ONLY logo
        loading)" - the full backdrop belongs to a cold open, not to a
        picture that was just there. So what goes up now is the badge
        (see _show_buffer_frame), not the frame.

        Only over a *running* picture. During startup the ordinary
        loading frame is already up doing this job, and arming a second
        path to show it would fight _show_loading_soon's own guard."""
        if self._closing or self._awaiting_first_frame or self._position <= 0:
            return
        # A stall is a stall, whoever caused it. The suppression that
        # sat here for a track switch's own reconfigure existed only to
        # hide the badge's box; the badge is clipped to the logo now
        # (_layout_overlays), and the owner's next report was the exact
        # case it hid - "when I change the embedded subtitles it freezes
        # for ~5-10 sec (no loading logo)". So the logo shows for every
        # wait past BUFFER_FRAME_GUARD_MS, a subtitle switch included.
        if self._buffer_frame_up or self._buffer_delay.isActive():
            return
        self._buffer_delay.start()

    def _show_buffer_frame(self):
        if self._closing or self._awaiting_first_frame:
            return
        if self._status_visible():
            return          # a dead-end message outranks it, as ever
        if not self.logo.has_logo():
            # No logo, nothing to badge. The full frame is not shown
            # instead - the owner's ask, 23 August 2026, is exactly that
            # a stall must not cover the picture with the backdrop image
            # - so this rare case (no TMDB art) falls back to what
            # stalls did before 22 August: the "Buffering..." note in
            # the controls' source label.
            return
        self._buffer_frame_up = True
        # **The full startup frame, exactly - the owner, 24 August 2026:
        # "remove it entirely, then readd it as the 1st loading logo
        # exactly!!".** This is the third design for a mid-play stall
        # and it supersedes the other two, both his own earlier asks:
        # the 23 August "keep the video with ONLY logo loading" badge,
        # and today's silhouette-clipped variant of it. The badge could
        # never be made identical to the startup logo, because it is a
        # native child window blended whole by DWM - the startup logo
        # sits on an opaque backdrop and needs no blending at all. So a
        # stall now raises the very same surface startup uses: the
        # backdrop still, the scrim, the logo filling with the buffer.
        # set_stall stays False; StartupBackdrop's badge mode and the
        # silhouette clip in _layout_overlays are dead branches kept for
        # the history recorded on them.
        self.backdrop.set_stall(False)
        # Geometry before show, for the reason _show_status gives: these
        # are native child windows, and one shown before it is placed
        # paints in the window's top-left corner for a frame.
        self._layout_overlays()
        self._show_backdrop(force=True)
        _set_window_alpha(self.backdrop, 255)
        self.logo.set_fraction(
            min(1.0, max(0.0, getattr(self, "_buffering_percent", 0) / 100.0)))

    def _end_buffer_frame(self):
        """The cache filled (or the episode moved on); give the picture
        back."""
        self._buffer_delay.stop()
        if not self._buffer_frame_up:
            return
        self._buffer_frame_up = False
        if self._closing:
            return
        # Not _hide_status: that also stops the startup gauge and clears
        # its text, and during a mid-playback stall neither is running.
        self.backdrop.hide()
        self._reset_stall_frame()

    def _reset_stall_frame(self):
        """Put the backdrop back into its full-window shape after the
        stall badge (see _show_buffer_frame): full opacity, no rounded
        window region, full geometry. Safe to call at any time; a
        backdrop that is not in stall mode is left alone."""
        if not self.backdrop.stalled():
            return
        self.backdrop.set_stall(False)
        _set_window_alpha(self.backdrop, 255)
        self._layout_overlays()

    def _status_visible(self):
        return self.status.isVisible()

    def _toast_anchor(self):
        return self._window or self

    # ---- controls visibility -----------------------------------------
    def _veil(self, widget, alpha):
        """Give `widget` its DWM alpha, but only when that is actually
        needed.

        The reapplication exists because Qt destroys and recreates a
        native child window on a reparent or a screen change, and the
        recreated one has none of the extended style, so the bar silently
        goes back to opaque. That means the trigger is the *handle*
        changing, not the passing of a tick - so the handle is what is
        remembered. Measured at 0.41ms a pair of calls, which was being
        paid 5.5 times a second for as long as the pointer kept moving."""
        try:
            hwnd = int(widget.winId())
        except RuntimeError:
            return
        if self._veiled.get(id(widget)) == (hwnd, alpha):
            return
        if _set_window_alpha(widget, alpha):
            self._veiled[id(widget)] = (hwnd, alpha)

    def _wake_controls(self):
        was_hidden = not self.controls.isVisible()
        if was_hidden:
            # Same order as _show_status, for the same reason: a native
            # child window that is shown before it is placed paints once
            # where it used to be. Only on the way back from hidden - this
            # runs on every pointer tick, and setting geometry on four
            # native windows 5x a second is its own flicker.
            self._layout_overlays()
        self.controls.show()
        self.top_bar.show()
        if was_hidden:
            # Same reasoning as the veil above: a raise_ is a
            # SetWindowPos on a native window sitting over the video, and
            # nothing can have got above these two while they were
            # already up - everything that raises itself over them
            # (_show_backdrop, the episode bar, a panel) re-raises them
            # itself, or is raised again below.
            self.controls.raise_()
            self.top_bar.raise_()
        # Still unconditional, and still correct - but through
        # _raise_native, which does nothing when the window is already on
        # top. This ran up to 5.5 times a second for as long as the
        # pointer kept moving, and a SetWindowPos on the panel next to a
        # press on that same panel is what loses the press ("I need to
        # click 2 times to choose one"). See _raise_native for the
        # numbers; the order these produce is unchanged.
        if self._episode_bar is not None and self._episode_bar.isVisible():
            _raise_native(self._episode_bar)
            self._veil(self._episode_bar, BAR_ALPHA)
        if self._panel is not None:
            _raise_native(self._panel)
        # Both bars wear the same low-alpha near-black veil now (see
        # _build_top_bar for why the top bar's colour-keying is gone).
        self._veil(self.controls, CONTROLS_VEIL_ALPHA)
        self._veil(self.top_bar, CONTROLS_VEIL_ALPHA)
        # Deliberate, and reversed on every path that hides them again:
        # this is not the sticky-hand-cursor trap in .claude/rules/ui.md,
        # which is about a widget keeping a cursor it no longer earns.
        # A blank cursor over a playing video is the point.
        self.surface.unsetCursor()
        self._set_sub_position(SUB_POS_ABOVE_CONTROLS)
        self._idle_timer.start(CONTROLS_HIDE_MS)

    def _set_sub_position(self, value):
        """Where the *player* wants the line - bottom, or lifted clear of
        the controls. The owner's own offset is added on top."""
        self._sub_pos_base = value
        self._apply_sub_position()

    def _apply_sub_position(self):
        """Push base + the owner's offset onto mpv.

        **One property covers every kind of subtitle this app shows.**
        `sub-pos` moves plain text (.srt), libass scripts (.ass, and the
        embedded tracks in a release's own container) and bitmap tracks
        alike - unlike the size lever, which needed `sub-scale` beside
        `sub-font-size` because the `sub-font-*` options never reach an
        .ass. Verified on this build's mpv against a generated .srt and
        .ass and against a real embedded track: the property reads back
        as written in all three cases.

        Written only when it changes: this is called from every wake of
        the controls, and a property write costs mpv a subtitle
        re-render. Remembered rather than read back, because the read is
        the same round trip as the write."""
        if self.handle is None:
            return
        value = max(0, min(100, self._sub_pos_base + self._sub_pos_offset))
        if value == self._sub_pos:
            return
        try:
            self.handle["sub-pos"] = value
            self._sub_pos = value
        except Exception:
            logs.exception("Subtitle position change failed")

    def _nudge_sub_pos(self, delta):
        """The panel's Position stepper. `delta` negative moves the line
        up the frame, which is the direction anyone actually wants -
        away from burned-in signs and the controls."""
        self._sub_pos_offset = max(SUB_POS_OFFSET_MIN,
                                   min(SUB_POS_OFFSET_MAX,
                                       self._sub_pos_offset + delta))
        self._apply_sub_position()
        self._update_stepper("set_pos_text", self._sub_pos_text())

    def _sub_pos_text(self):
        """Said as height off the bottom rather than as mpv's number:
        mpv counts *down* from the top, so its 100 is the bottom and a
        bigger number means lower - which reads backwards on a control
        whose whole job is "move it up"."""
        return f"{-self._sub_pos_offset:+d}" if self._sub_pos_offset else "0"

    def _hide_controls(self):
        if self._closing or self._paused:
            return
        if self._panel is not None:
            return
        # The episode list is opened deliberately and dismissed the same
        # way, so nothing auto-hides while it is up - having the list
        # vanish mid-scroll is what would make it useless.
        if self._episode_bar is not None and self._episode_bar.isVisible():
            return
        if self._status_visible():
            return
        if self._pointer_in(self.controls) or self._pointer_in(self.top_bar):
            # The pointer resting on the controls is not idleness - it is
            # someone about to press something. Re-armed rather than
            # dropped, so it hides once they move off.
            self._idle_timer.start(CONTROLS_HIDE_MS)
            return
        self.controls.hide()
        self.top_bar.hide()
        self.surface.setCursor(Qt.CursorShape.BlankCursor)
        self._set_sub_position(SUB_POS_CLEAR)

    def _pointer_in(self, widget):
        """Is the pointer really over `widget`?

        Not `underMouse()`, which was the first attempt and is wrong
        here: measured with the pointer sitting on the controls bar, it
        answered False, because it depends on an Enter having been
        delivered and this window is not always the active one. The bar
        then hid itself out from under the pointer.

        The rect is built from `window().geometry()`, which is already in
        global coordinates, plus `mapTo(window, ...)`, which is plain
        widget-local arithmetic inside one window. Deliberately not
        mapToGlobal: on two monitors at different scale factors it
        divides by the wrong screen's factor (see .claude/rules/ui.md)."""
        try:
            if not widget.isVisible():
                return False
            window = self.window()
            if window is None:
                return False
            origin = window.geometry().topLeft() + widget.mapTo(window, QPoint(0, 0))
            return QRect(origin, widget.size()).contains(QCursor.pos())
        except RuntimeError:
            return False

    def _poll_pointer(self):
        """Wake the controls when the pointer moves *on this player*.

        The window test is the fix for "it starts glitching when the
        cursor is on other apps or desktop". This used to wake on any
        movement anywhere on screen, so working in another application
        held the bars up over the video indefinitely - the idle timer was
        re-armed 5.5 times a second (measured, POINTER_POLL_MS 180) and
        never got to fire - and each of those wakes paid two raise_ calls
        and two SetLayeredWindowAttributes on native windows sitting over
        the video surface.

        For the record, because it was the first suspect and it is
        wrong: that per-tick work is not itself expensive. Measured
        against a real 1080p60 gpu-vo render - 2x raise_ 1.02ms median,
        2x SetLayeredWindowAttributes 0.41ms, sub-pos write 0.14ms, and
        over 3 alternated 10s runs mpv dropped 0 frames and a 16ms
        heartbeat timer ran 0 late frames either way. What the churn
        actually costs is the bars never going away."""
        position = QCursor.pos()
        if position == self._last_pointer:
            return
        self._last_pointer = position
        if not self._widget_rect(self).contains(position):
            return
        self._wake_controls()

    # ---- click to play/pause -----------------------------------------
    def _over_video(self, position) -> bool:
        """Is this screen point on the video and on nothing else?

        Same global-rect arithmetic as `_pointer_in` and for the same
        reason (never mapToGlobal - see .claude/rules/ui.md). Everything
        the player draws over the video is subtracted, so a press on the
        controls, the episode list, a panel or the status box is not a
        press on the video.

        **The skip button belongs in that list, and its absence is the
        owner's "after skip intro btn do not pause the player".**
        Measured 22 August 2026 on the real widget: a press at the Skip
        Intro button's centre answered True here, so `_poll_mouse`
        stored `_press_on_video` and the release wrote `pause=True` at
        mpv - while the button's own `clicked` seeked to 380.0. One
        press, two actions, and the second one was never asked for."""
        try:
            window = self.window()
            if window is None:
                return False
            origin = window.geometry().topLeft() + self.mapTo(window, QPoint(0, 0))
            if not QRect(origin, self.size()).contains(position):
                return False
        except RuntimeError:
            return False
        overlays = [self.controls, self.top_bar, self.status, self.skip_btn,
                    self._panel, self._episode_bar,
                    getattr(self, "_drm_button", None)]
        return not any(w is not None and self._widget_rect(w).contains(position)
                       for w in overlays)

    def _widget_rect(self, widget) -> QRect:
        try:
            if not widget.isVisible():
                return QRect()
            window = self.window()
            if window is None:
                return QRect()
            origin = window.geometry().topLeft() + widget.mapTo(window, QPoint(0, 0))
            return QRect(origin, widget.size())
        except RuntimeError:
            return QRect()

    def _poll_mouse(self):
        """Click anywhere on the video to play/pause.

        Polled, not handled as a Qt event, for exactly the reason the
        pointer is polled: mpv renders into a native child window of its
        own, so a press over the video is delivered to mpv's window and
        no Qt widget ever sees it. There is no filter to install - the
        window does not belong to Qt.

        GetAsyncKeyState is read twice per state: 0x8000 is "held right
        now", which a 40ms tick sees for any normal click, and 0x0001 is
        "was pressed since this thread last asked", which catches a click
        that began and ended entirely between two ticks. The second is
        only ever a fallback; it cannot fire without a real press having
        happened."""
        if self._closing or os.name != "nt":
            return
        try:
            state = ctypes.windll.user32.GetAsyncKeyState(_VK_LBUTTON)
        except Exception:
            return
        down = bool(state & 0x8000)
        pressed_since = bool(state & 0x0001)
        position = QCursor.pos()

        if down and not self._mouse_down:
            self._mouse_down = True
            # Every press gets a number, and _guard_click names one of
            # them - so "this click opened the panel" is a fact rather
            # than a stopwatch reading.
            self._press_seq += 1
            self._press_pos = position
            # Decided at press time, not at release: the window being
            # active is what separates "clicked the video" from "clicked
            # some other app that happens to be over this one", and a
            # click that only raises this window must not also pause it.
            self._press_on_video = (self._is_active() and self._over_video(position))
            return
        if down:
            return
        if self._mouse_down:
            self._mouse_down = False
            if self._press_on_video and self._is_click(position):
                self._click_toggle(position)
            self._press_on_video = False
            self._press_pos = None
        elif pressed_since:
            # A whole click between two ticks. It is still a press and
            # still gets a number, before the gates below - a panel that
            # guarded "the next press" (Qt saw it first) has to be able to
            # match it here.
            self._press_seq += 1
            if self._is_active() and self._over_video(position):
                self._click_toggle(position)

    def _is_active(self) -> bool:
        """Is this app the one in front?

        **Qt's answer alone was losing clicks.** `isActiveWindow()` is a
        cached flag updated when Qt processes the activation message, and
        the mouse here is read from a 40ms poll of the OS - so a press
        that arrives in the same breath as the activation is judged
        against a flag that has not caught up, `_press_on_video` stays
        False, and the click does nothing. The user presses again and it
        works, which is the owner's "takes more than one click".

        So the OS is asked as well, and either answer counts. What this
        does *not* change is the rule the flag was there for: when
        another application really is in front, both answers are False
        and a click that only raises this window still does not pause
        it."""
        try:
            window = self.window()
            if window is None:
                return False
            if window.isActiveWindow():
                return True
        except RuntimeError:
            return False
        if os.name != "nt":
            return False
        try:
            import ctypes
            front = ctypes.windll.user32.GetForegroundWindow()
            return bool(front) and int(front) == int(window.winId())
        except Exception:
            return False

    def _is_click(self, position) -> bool:
        if self._press_pos is None:
            return True
        moved = ((position.x() - self._press_pos.x()) ** 2
                 + (position.y() - self._press_pos.y()) ** 2)
        return moved <= CLICK_MOVE_TOLERANCE_PX ** 2

    def _click_toggle(self, position):
        """A click landed on the video. Single click toggles play/pause;
        a double-click toggles full screen instead.

        Nothing plays yet while the status box is up (looking for a
        source, buffering), and toggling pause on a file that has not
        loaded only makes the play button lie about what is happening.

        The double-click is disambiguated without delaying the single
        click (which must stay instant): the first click toggles pause
        immediately, and if a second lands inside DOUBLE_CLICK_S the pause
        is put back to exactly where it was and full screen toggles
        instead - so a double-click never leaves playback paused or
        played, and the second click is otherwise swallowed. Pause is
        *restored to a remembered value* rather than toggled again,
        because mpv's pause callback may not have arrived yet and a blind
        second toggle would race it onto the wrong state.

        A click while a sub-panel is open dismisses it and does nothing
        else, at the owner's request: clicking off an open menu is how a
        menu is closed everywhere, and pausing the film as well meant
        every dismissal had to be undone with a second click. The panel
        and the episode list are already excluded from `_over_video`, so
        this only ever fires for a click on the picture itself."""
        if not self._over_video(position):
            return
        # The press a panel opening, closing or rebuilding belongs to is
        # that interaction's own press, whatever the geometry says now:
        # the resolution drill-down replaces a tall panel with a short one
        # under the pointer mid-click, so the release lands "over the
        # video" where the row just was and dismissed the very view the
        # click had opened (the reported "clicking 4K closes the window").
        # Matched by identity, so a *later* click - the one meant to
        # dismiss - is never mistaken for it however soon it comes.
        if self._press_seq == self._guarded_press:
            return
        if time.monotonic() - self._panel_guard < PANEL_CLICK_GUARD_S:
            return
        if self._panel is not None:
            self._close_panel()
            self._wake_controls()
            return
        if self._episode_bar is not None and self._episode_bar.isVisible():
            self.toggle_episode_bar()
            return
        if self.handle is None or self._status_visible():
            return
        now = time.monotonic()
        near = True
        if self._last_click_pos is not None:
            moved = ((position.x() - self._last_click_pos.x()) ** 2
                     + (position.y() - self._last_click_pos.y()) ** 2)
            near = moved <= DOUBLE_CLICK_PX ** 2
        if now - self._last_click_time <= DOUBLE_CLICK_S and near:
            self._last_click_time = 0.0
            self._last_click_pos = None
            try:
                self.handle["pause"] = self._pause_before_click
            except Exception:
                logs.exception("Restoring pause on double-click failed")
            self.toggle_fullscreen()
            return
        self._last_click_time = now
        self._last_click_pos = position
        self._pause_before_click = self._paused
        self._wake_controls()
        self.toggle_pause()

    # ---- skipping the opening / ending -------------------------------
    def _skips_from_chapters(self):
        """Skip intervals taken from the file's own chapter markers.

        The best source there is when it exists: no network, no id to
        match, no crowd data to be wrong - the person who encoded the
        release said where the opening was. mpv exposes them as
        `chapter-list`, each with a title and a start; the end of one is
        the start of the next."""
        if self.handle is None:
            return []
        try:
            chapters = list(self.handle.chapter_list or [])
        except Exception:
            return []
        rows = []
        # Written down whatever happens next: "the skip intro is not
        # showing" for a live-action title (House of the Dragon, the
        # owner, 24 August 2026) is unanswerable without knowing what
        # the release actually carried. AniSkip cannot know live action
        # (it is keyed by MAL id), TheIntroDB is dead from here, so the
        # file's own markers are the *only* source - and most WEB rips
        # ship either none or bare "Chapter N" names, which correctly
        # classify as nothing. One line per episode makes the next
        # report diagnosable from the log instead of from a guess.
        try:
            logs.info("chapter markers: " + (", ".join(
                repr(str(c.get("title") or "")) for c in chapters) or "none"))
        except Exception:
            pass
        for index, chapter in enumerate(chapters):
            title = str(chapter.get("title") or "").strip().lower()
            if not title:
                continue
            words = set(re.findall(r"[a-z]+", title))
            if words & set(_CHAPTER_OPENING):
                kind = skiptimes.OPENING
            elif words & set(_CHAPTER_ENDING):
                kind = skiptimes.ENDING
            elif words & set(_CHAPTER_RECAP):
                # Nothing precedes the first episode of a series, so a
                # chapter calling itself a recap there is naming the cold
                # open. See skiptimes.first_episode.
                if skiptimes.first_episode(self.season, self.episode):
                    continue
                kind = skiptimes.RECAP
            else:
                continue
            start = float(chapter.get("time") or 0.0)
            if index + 1 < len(chapters):
                end = float(chapters[index + 1].get("time") or 0.0)
            else:
                end = self._duration or (start + 90.0)
            # A marker named like an opening but sitting fifteen minutes
            # in is a scene break, not an opening.
            if kind == skiptimes.OPENING and start > CHAPTER_OPENING_WINDOW_S:
                continue
            # And one named like an ending but sitting at the head of the
            # file is not the credits - the same position rule the crowd
            # data goes through (skiptimes.ENDING_MIN_POSITION), applied
            # here because the player offers "Next Episode" over an
            # ending and being wrong about that ends the episode.
            if (kind == skiptimes.ENDING and self._duration
                    and start < self._duration * skiptimes.ENDING_MIN_POSITION):
                continue
            if end <= start:
                continue
            # **And one whose interval is the wrong length is not an
            # opening either, whatever it is called.** The end here is
            # the next chapter's start, so a release that marks acts
            # rather than sections gives an "Intro" chapter that runs
            # until the first act break - and the button seeks to that
            # end. Measured on the owner's own download of LOST S06E01:
            # twelve chapters, all named "Chapter N" (so nothing matched
            # and the classifier stayed silent, correctly), but the first
            # interval is 0.00 -> 434.06s. Had that chapter been called
            # "Intro" - and plenty of live-action releases do call it
            # that - "Skip Intro" would have thrown the viewer 7:14 into
            # the episode. A recap is exempt: recaps genuinely vary
            # (measured 17.4-90.0s) and skipping one lands on the
            # opening rather than inside the story.
            span = end - start
            floor, ceiling = ((CHAPTER_RECAP_SPAN_MIN_S, CHAPTER_RECAP_SPAN_MAX_S)
                              if kind == skiptimes.RECAP
                              else (CHAPTER_SPAN_MIN_S, CHAPTER_SPAN_MAX_S))
            if not floor <= span <= ceiling:
                continue
            rows.append({"type": kind, "start": start, "end": end,
                         "source": "chapters"})
        # **Two openings that touch are a cold open and the real OP.** A
        # chaptered release can name its first scene "Intro" (or, until
        # today, "Avant") and its title sequence "Opening" right after it;
        # both pass the window and span checks above, and the pair is
        # precisely the owner's "Skip Intro at 0:00 takes me to the start
        # of the intro, where there is a Skip Intro again". The later one
        # is the title sequence, so it is the one kept. This runs here
        # rather than relying on _on_skips' AniSkip-backed rule, because
        # that rule only fires when AniSkip answers - and AniSkip is
        # reached through an AniList id, which 403s for hours at a time.
        openings = [r for r in rows if r["type"] == skiptimes.OPENING]
        if len(openings) >= 2:
            openings.sort(key=lambda r: r["start"])
            dropped = set()
            for earlier, later in zip(openings, openings[1:]):
                if abs(float(later["start"]) - float(earlier["end"])) <= 1.0:
                    dropped.add(id(earlier))
            rows = [r for r in rows if id(r) not in dropped]
        return rows

    def _load_skip_times(self):
        """Ask AniSkip for this episode, off the UI thread.

        Chapters are read separately and immediately (they cost nothing);
        this is only the network half, and it never raises - no skip data
        means no button, which is the same as every episode before this
        feature existed."""
        if skiptimes is None or not self.episode:
            return
        title = (self.entry or {}).get("title") or ""
        season, episode, run = self.season, self.episode, self._run

        def worker():
            try:
                found = skiptimes.fetch(title, season=season, episode=episode,
                                        episode_length=self._duration or 0.0)
            except Exception:
                found = []
            if found and not self._closing and run == self._run:
                self._work.skips_ready.emit(list(found), run)

        self._spawn(worker)

    @staticmethod
    def _overlaps(left, right) -> bool:
        """Whether two skip intervals cover any of the same seconds.

        Used to decide whether a chapter marker and a crowd entry are
        describing the same title sequence - see _on_skips. Any overlap
        at all counts: the two sources cut at different frames, and
        "within a second of each other" is a tuning knob nothing here
        has a measurement for."""
        try:
            return (float(left.get("start") or 0.0) < float(right.get("end") or 0.0)
                    and float(right.get("start") or 0.0) < float(left.get("end") or 0.0))
        except (TypeError, ValueError):
            return False

    def _on_skips(self, found, run):
        if self._closing or run != self._run:
            return
        # The crowd has now spoken, whatever it said - see _current_skip
        # for what was being held back until it did.
        self._skips_answered = True
        # Chapters win where both have an interval of the same kind: the
        # release's own markers are about *this* cut of the episode,
        # while a crowd entry may be about another release entirely.
        #
        # **Except an opening the chapters put at 0:00.** Measured 22
        # August 2026 over 18 real AniSkip openings across eight of the
        # owner's titles, not one starts before 27.4s (median 84.5s);
        # every interval at or near zero in that data is a *recap*. So a
        # chapter marker claiming the opening begins at 0:00 is, on the
        # evidence, a cold open or a title card that happens to carry an
        # opening-ish word - and where AniSkip has a real answer, that
        # answer is better. This is the rest of the owner's "not all
        # intros start from 0:00": with the chapter marker winning, the
        # button appeared over the cold open and AniSkip's correct
        # interval was thrown away unseen. Nothing changes for live
        # action, where AniSkip is silent and the chapter stands.
        #
        # **And more generally: where both sources name the opening,
        # they have to be talking about the same stretch of the file.**
        # The owner, 24 August 2026, with a screenshot: "the skip intro
        # is not accurate at all ... it is showing while there is not
        # intro" - The Angel Next Door Spoils Me Rotten S01E07, the
        # button up at **3:10** of a 23:41 episode. Measured live that
        # day, AniSkip (mal 50739) answers exactly one opening for that
        # episode, **0.00 -> 90.00**, and one ending at 1325. So
        # whatever was offering to skip at 190s was a chapter marker
        # naming a stretch the crowd data says is not the opening at
        # all.
        #
        # Two independent sources disagreeing about *where* the opening
        # is is different from one source being silent. A chapter is
        # authoritative about this cut of the file; it is not
        # authoritative about which scene is the title sequence, and a
        # release that marks acts will happily call one of them
        # something opening-shaped. So when AniSkip has an opening for
        # this episode, a chapter opening is kept only if the two
        # overlap - and dropped, not merged, when they do not.
        #
        # Live action is untouched: AniSkip is keyed by MyAnimeList id
        # and answers nothing at all there, so `crowd` is empty and
        # every chapter marker stands exactly as before.
        crowd_kinds = {row.get("type") for row in found}
        crowd_openings = [row for row in found
                          if row.get("type") == skiptimes.OPENING]
        kept = []
        for row in self._skips:
            if (row.get("source") == "chapters"
                    and row.get("type") == skiptimes.OPENING
                    and skiptimes.OPENING in crowd_kinds):
                if float(row.get("start") or 0.0) < CHAPTER_OPENING_MIN_START_S:
                    continue
                if not any(self._overlaps(row, other) for other in crowd_openings):
                    continue
            kept.append(row)
        have = {row.get("type") for row in kept
                if row.get("source") == "chapters"}
        kept.extend(row for row in found if row.get("type") not in have)
        # **Sorted, and one interval per kind per stretch.** Before this,
        # the merged list was in whatever order the two sources arrived,
        # and `_current_skip` returns the *first* row that contains the
        # position - so which offer appeared depended on arrival order
        # rather than on where playback was. The overlap resolver is the
        # same one AniSkip's own contradictory submissions go through;
        # see skiptimes._resolve_overlaps for the measured pairs.
        self._skips = skiptimes._resolve_overlaps(
            sorted(kept, key=lambda row: float(row.get("start") or 0.0)))
        self._refresh_skip_button()

    def _current_skip(self):
        """What to offer at this instant, or None.

        An opening or a recap offers to jump past it. An ending - or the
        last minute of the file when no ending is known - offers the next
        episode instead, which is what anyone wants once the credits are
        rolling."""
        position = self._position
        for row in self._skips:
            # **A chapter marker claiming the opening starts at 0:00 is
            # not offered until the crowd answer is in.** The chapters
            # are read the instant the file loads and AniSkip answers a
            # second or two later, so the merge in `_on_skips` - which
            # already knows a near-zero chapter opening loses to a real
            # crowd interval - was being applied *after* the button had
            # been on screen. Measured over 18 real AniSkip openings,
            # none begins before 27.4s; a marker at zero is a cold open
            # or a title card carrying an opening-ish word, and the two
            # seconds it takes to find that out are exactly the seconds
            # the owner is looking at the screen.
            if (not self._skips_answered
                    and row.get("source") == "chapters"
                    and row.get("type") == skiptimes.OPENING
                    and float(row.get("start") or 0.0)
                    < CHAPTER_OPENING_MIN_START_S):
                continue
            if row["start"] <= position < row["end"] - 0.5:
                if row["type"] == skiptimes.ENDING:
                    if self._has_next_episode():
                        return ("next", "Next Episode  →", row["end"])
                    continue
                # **A recap is not an intro, and calling it one is the
                # owner's "in JJK s1ep3 the skip intro was appearing
                # from 0:00 while it is supposed to appear at 3:12".**
                # Measured live against AniSkip, Jujutsu Kaisen S01E03
                # (mal 40748) answers three intervals:
                #
                #     recap    0.50 ->   41.62
                #     op     191.73 ->  282.08     <- 3:11.7, the owner's 3:12
                #     ed    1274.66 -> 1364.96
                #
                # Both the recap and the op came back here as "Skip
                # Intro", so the button appeared over the cold open and
                # the *real* opening offer at 3:12 looked like the same
                # button never going away. The data was right the whole
                # time; the label was not. Naming the recap for what it
                # is keeps both offers and makes each one honest.
                if row["type"] == skiptimes.RECAP:
                    return ("seek", "Skip Recap", row["end"])
                return ("seek", "Skip Intro", row["end"])
        if (self._duration and self._has_next_episode()
                and position >= self._duration - NEXT_TAIL_S):
            return ("next", "Next Episode  →", self._duration)
        return None

    def _has_next_episode(self) -> bool:
        if not self.episode:
            return False
        try:
            return int(self.episode) + 1 <= self._season_episode_count(self.season)
        except Exception:
            return False

    def _refresh_skip_button(self):
        """Show, hide or retitle the button for where playback now is.

        Called from every time-pos update, so it does as little as
        possible when nothing has changed - the offer is compared before
        anything is written to a widget."""
        if self._closing:
            return
        offer = self._current_skip()
        if offer == self._skip_offer:
            return
        self._skip_offer = offer
        try:
            # Re-asserted at every offer change, not only in
            # _wake_controls: the button shows itself with the bars
            # hidden, and a native window that was never given its
            # alpha keeps whatever DWM last had for it.
            if offer is not None:
                _set_window_alpha(self.skip_btn, 255)
            if offer is None:
                self.skip_btn.hide()
                return
            # **Only the text and the place change while it is up.** The
            # raise, the alpha and the corner cut used to be redone on
            # every offer change, and `offer` carries `self._duration` -
            # which for a stream is still settling, so that was every
            # time-pos tick. Two things came out of it, both measured 22
            # August 2026: the button raised over an open panel and the
            # next pointer tick raised the panel back, a restacking
            # ping-pong right where the rows are (see _raise_native for
            # what a restack does to a press), and re-applying
            # SetLayeredWindowAttributes at that rate is what takes the
            # process down with an 0xC0000409 in a harness. The veil is
            # taken through self._veil now, which re-applies only when
            # the window handle itself has changed.
            was_hidden = not self.skip_btn.isVisible()
            self.skip_btn.setText(offer[1])
            self._place_skip_button()
            self.skip_btn.show()
            if was_hidden:
                _raise_native(self.skip_btn)
                self._veil(self.skip_btn, 255)
                # Size is fixed (SKIP_BUTTON_SIZE) so the cut only has to
                # follow the window, not every move.
                _round_overlay(self.skip_btn)
        except RuntimeError:
            pass        # torn down between the tick and here

    def _place_skip_button(self):
        rect = self.rect()
        width, height = SKIP_BUTTON_SIZE
        # Above the controls bar whether or not it is showing, so the
        # button never moves while someone is aiming at it.
        self.skip_btn.setGeometry(
            rect.width() - width - SKIP_BUTTON_MARGIN,
            rect.height() - CONTROLS_HEIGHT - height - 12,
            width, height)

    def _take_skip_offer(self):
        offer, self._skip_offer = self._skip_offer, None
        if offer is None:
            return
        kind, _label, target = offer
        # The same guard a panel takes, and for the identical reason:
        # this press is the *button's*, and nothing else may read it as
        # a press on the picture. Subtracting the button in _over_video
        # covers the press-time decision, but not `_poll_mouse`'s
        # `pressed_since` fallback - that branch re-asks _over_video at
        # release time, and by then the hide below has taken the button
        # out of the way. Measured: _over_video at the button's own
        # centre answered True again the instant it was hidden.
        self._guard_click()
        try:
            self.skip_btn.hide()
        except RuntimeError:
            pass
        if kind == "next":
            self._change_episode(1)
        else:
            self._seek_absolute(float(target))

    # ---- layout ------------------------------------------------------
    def _layout_overlays(self):
        rect = self.rect()
        self.surface.setGeometry(rect)
        self.top_bar.setGeometry(0, 0, rect.width(), TOPBAR_HEIGHT)
        self.controls.setGeometry(0, rect.height() - CONTROLS_HEIGHT,
                                  rect.width(), CONTROLS_HEIGHT)
        self._place_skip_button()
        # No masks on the two bars any more: the top bar is colour-keyed
        # (only its content exists) and the controls are a full-width
        # veil - neither has corners to round.
        # The title's elide cap follows the window's width.
        self._refresh_title_label()
        if self._episode_bar is not None:
            # Left edge (the owner's ask). The corners against the
            # window's own left edge stay square - rounding them would
            # notch the window frame rather than the bar.
            self._episode_bar.setGeometry(
                0, TOPBAR_HEIGHT, EPISODE_BAR_WIDTH,
                max(120, rect.height() - TOPBAR_HEIGHT - CONTROLS_HEIGHT))
            _round_overlay(self._episode_bar, square=("tl", "bl"))
        # The logo sits at the exact centre of the window (the owner's
        # ask), with the frameless status line just under it; when there
        # is no logo the words take the centre themselves. Placed whether
        # or not either is showing: _show_status calls this while the
        # label is still hidden precisely so it appears in place.
        width = min(680, max(320, rect.width() - 120))
        height = self.status.sizeHint().height() + 12
        # Much smaller than the old 46%-of-window treatment (the owner's
        # ask): a quarter of the width, capped at 300 - a loading mark
        # that pulses in the middle, not a poster.
        logo_width = max(160, min(int(rect.width() * 0.24), 300))
        logo_height = int(logo_width * 0.34)
        logo_y = int((rect.height() - logo_height) / 2)
        if self.backdrop.stalled():
            # Mid-playback stall: the backdrop window shrinks to a badge
            # around the logo so the frozen frame stays visible around
            # it (the owner's ask, 23 August 2026 - "keep the video with
            # ONLY logo loading"). Same centre as the full-frame logo,
            # so the mark does not jump between the two modes. This
            # branch is here rather than in _show_buffer_frame because
            # every later layout pass (a resize, _wake_controls bringing
            # the bars back) runs through this method and would
            # otherwise snap the badge back to full-window.
            pad_x, pad_y = 28, 18
            self.backdrop.setGeometry(
                int((rect.width() - logo_width) / 2) - pad_x,
                logo_y - pad_y,
                logo_width + 2 * pad_x, logo_height + 2 * pad_y)
            self.logo.setGeometry(pad_x, pad_y, logo_width, logo_height)
            # **Clipped to the logo, not to a rounded rectangle** - the
            # owner's ask, twice: "the loading icon still has a bg frame,
            # remove the frame just keep the loading icon!".
            #
            # There is no border and no stylesheet to delete. The frame is
            # this window: in stall mode it is a small native child filled
            # with theme.BG, masked to a 16px rounded rect and blended
            # whole by DWM (see _set_window_alpha), so what reads as a
            # bordered panel is the mask edge of that fill against the
            # video. It cannot simply paint nothing - `WS_EX_LAYERED` with
            # LWA_ALPHA blends the whole window, there is no per-pixel
            # alpha for a child window, and the colour-key route this file
            # used to take was abandoned because antialiased edges stopped
            # matching the key and rode every glyph as a halo (see
            # _hard_edge_font).
            #
            # So the window is clipped to the logo's own silhouette
            # instead. Only logo pixels remain, and the fill has nowhere
            # left to show. The mask is 1-bit, so the logo's edge is hard
            # rather than feathered - the honest cost of getting rid of
            # the box without a per-pixel surface.
            region = None
            try:
                region = self.logo.logo_region()
            except Exception:
                region = None
            if region is not None and not region.isEmpty():
                region.translate(pad_x, pad_y)
                self.backdrop.setMask(region)
            else:
                # No logo art for this title: the badge is all there is
                # to show, so it keeps its rounded frame.
                _round_overlay(self.backdrop)
        else:
            self.backdrop.setGeometry(rect)
            # Full-window again: drop the badge's rounded window region
            # or it keeps clipping the full-size frame to a small
            # rounded hole in the corner.
            self.backdrop.clearMask()
            self.logo.setGeometry(int((rect.width() - logo_width) / 2),
                                  logo_y, logo_width, logo_height)
        status_y = (logo_y + logo_height + 14 if self.logo.has_logo()
                    else int((rect.height() - height) / 2))
        self.status.setGeometry(int((rect.width() - width) / 2), status_y,
                                width, height)
        # Re-cut after every size change, or the veil keeps the corners
        # it was rounded with at the old geometry.
        _round_overlay(self.status)
        button = getattr(self, "_drm_button", None)
        if button is not None:
            size = button.sizeHint()
            self.status.adjustSize()
            button.setGeometry(int((rect.width() - size.width()) / 2),
                               int(rect.height() / 2) + 90,
                               size.width(), size.height())
        if self._panel is not None:
            width = min(getattr(self._panel, "panel_width", PANEL_WIDTH),
                        max(280, rect.width() - 60))
            # As tall as its content and no taller: the speed panel is a
            # value, a slider and a row of chips, and at the old fixed
            # height it was four-fifths empty (the owner's complaint).
            # Long lists still cap at PANEL_MAX_HEIGHT and scroll.
            wanted = (self._panel.body.sizeHint().height()
                      + self._panel.footer_layout.sizeHint().height() + 96)
            # **Grow-only while the panel stays open**, which is what
            # stops "the resolution and sources window goes too small
            # when I press 4K or 1080p". Drilling into one resolution
            # replaces a list of every release with a handful, and a
            # height taken from that content alone collapsed the box to
            # near the 150px floor - under the pointer, mid-read. Since
            # panels are refilled rather than rebuilt now
            # (OverlayPanel.reset), the same measurement could also land
            # while the body was momentarily empty, which is the floor
            # exactly. So a panel never shrinks during one opening; the
            # floor resets when a panel of a different kind opens, and
            # the cap and the scrolling are unchanged.
            floor = getattr(self._panel, "grown_height", 0)
            height = max(150, floor,
                         min(PANEL_MAX_HEIGHT, wanted,
                             rect.height() - CONTROLS_HEIGHT - 80))
            height = min(height, rect.height() - CONTROLS_HEIGHT - 80)
            self._panel.grown_height = height
            self._panel.setGeometry(rect.width() - width - 24,
                                    rect.height() - CONTROLS_HEIGHT - height - 10,
                                    width, height)
            _round_overlay(self._panel)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._layout_overlays()

    def eventFilter(self, obj, event):
        if obj is self.parent() and event.type() == QEvent.Type.Resize:
            self.setGeometry(self.parent().rect())
            return False
        if event.type() in (QEvent.Type.Enter, QEvent.Type.MouseMove):
            self._wake_controls()
        return super().eventFilter(obj, event)

    # ---- keyboard ----------------------------------------------------
    # The keyboard's own transport keys. **Bound here rather than left to
    # Space alone** - the owner's ask, 24 August 2026: "make the pause and
    # resume and next and prev buttons in the keyboard work while in a
    # video player". Windows delivers a media key as WM_APPCOMMAND and Qt
    # turns it into one of these key codes, so it arrives like any other
    # key - but only at whatever has keyboard focus, which is what
    # _KeyRelay exists to fix.
    _MEDIA_PLAY_PAUSE = (Qt.Key.Key_MediaTogglePlayPause, Qt.Key.Key_MediaPlay,
                         Qt.Key.Key_MediaPause)

    # **Only a key this player acts on brings the chrome back.** The
    # owner's ask, 25 August 2026: "when I press any button on the
    # keyboard the ui in vid player appears - do not make it appear when
    # I press any btn e.g. raising the windows sound". Windows delivers
    # its own volume keys as Key_VolumeUp/Down/Mute to whatever has
    # focus, and the launcher and browser keys the same way, so "any
    # key" was never the right trigger. Escape is deliberately not in
    # here: it closes a panel, leaves full screen or leaves the player,
    # and none of those is a request to see the bars.
    _WAKING_KEYS = frozenset({
        Qt.Key.Key_Space, Qt.Key.Key_MediaTogglePlayPause, Qt.Key.Key_MediaPlay,
        Qt.Key.Key_MediaPause, Qt.Key.Key_MediaStop, Qt.Key.Key_MediaNext,
        Qt.Key.Key_MediaPrevious, Qt.Key.Key_N, Qt.Key.Key_P,
        Qt.Key.Key_Left, Qt.Key.Key_Right, Qt.Key.Key_Up, Qt.Key.Key_Down,
        Qt.Key.Key_F, Qt.Key.Key_F11, Qt.Key.Key_M,
    })

    def keyPressEvent(self, event):
        key = event.key()
        if key in self._WAKING_KEYS:
            self._wake_controls()
        if key == Qt.Key.Key_Space or key in self._MEDIA_PLAY_PAUSE:
            self.toggle_pause()
        elif key == Qt.Key.Key_MediaStop:
            # Stop pauses rather than closing the player: it sits next to
            # Next on most keyboards, and losing the episode to a
            # mis-hit is not a mistake worth allowing twice.
            if not self._paused:
                self.toggle_pause()
        elif key in (Qt.Key.Key_MediaNext, Qt.Key.Key_N):
            self._change_episode(1)
        elif key in (Qt.Key.Key_MediaPrevious, Qt.Key.Key_P):
            self._change_episode(-1)
        elif key == Qt.Key.Key_Left:
            self._seek_relative(-SEEK_STEP_S)
        elif key == Qt.Key.Key_Right:
            self._seek_relative(SEEK_STEP_S)
        elif key == Qt.Key.Key_Up:
            self.volume_slider.setValue(min(VOLUME_MAX, self._volume + VOLUME_STEP))
        elif key == Qt.Key.Key_Down:
            self.volume_slider.setValue(max(0, self._volume - VOLUME_STEP))
        elif key in (Qt.Key.Key_F, Qt.Key.Key_F11):
            self.toggle_fullscreen()
        elif key == Qt.Key.Key_M:
            self.toggle_mute()
        elif key == Qt.Key.Key_Escape:
            # One thing at a time, innermost first: a panel, then full
            # screen, then the player. Closing the whole page because a
            # subtitle list was open is the kind of Escape that makes
            # people stop using it.
            if self._panel is not None:
                self._close_panel()
            elif self._episode_bar is not None and self._episode_bar.isVisible():
                self.toggle_episode_bar()
            elif self._window is not None and self._window.isFullScreen():
                self.toggle_fullscreen()
            else:
                self.close_player()
        else:
            super().keyPressEvent(event)

    # ---- teardown ----------------------------------------------------
    def close_player(self):
        if self._closing:
            return
        self._closing = True
        relay = getattr(self, "_key_relay", None)
        app = QApplication.instance()
        if relay is not None and app is not None:
            app.removeEventFilter(relay)
        self._save_position()
        self._idle_timer.stop()
        self._pointer_timer.stop()
        self._mouse_timer.stop()
        self._save_timer.stop()
        self._loading_delay.stop()
        self._close_panel()
        # A sticky subtitle toast outlives this page otherwise - it is
        # parented to the window, and nothing would ever finish it. The
        # same is true of the "Loading Source..." / "Skipping To..." one.
        self._drop_sub_toast()
        self._finish_working()
        # Cursor first: the surface is about to go away, and a widget
        # that dies holding the blank cursor leaves Windows painting it
        # over whatever comes next.
        self.surface.unsetCursor()
        handle, self.handle = self.handle, None
        video_backend.shutdown(handle)
        # **The swarm goes with the player.** Nothing released the
        # torrent that had just been watched, so it kept downloading and
        # announcing for the rest of the run - a session of six episodes
        # ended with six swarms competing for the connection with
        # whatever was playing next. A torrent a download is using is
        # pinned and survives this (torrent_engine.pin).
        self._release_streams()
        if self._temp_dir:
            shutil.rmtree(self._temp_dir, ignore_errors=True)
            self._temp_dir = None
        parent = self.parent()
        if parent is not None:
            parent.removeEventFilter(self)
        if self.on_close is not None:
            self.on_close()
        self.closed.emit()
        self.hide()
        # **Leaving the player navigates nowhere.** It is an overlay over
        # the central widget, not a page in the stack, so whatever it was
        # opened over is still built, still scrolled where it was, and
        # revealing it is both correct and free - the rule `reader.leave`
        # already follows, and what this page's own `open_player`
        # docstring promises ("coming back out of the player should land
        # exactly where it was entered from").
        #
        # It used to navigate to Home here, which is the owner's report of
        # 23 August 2026: "when I play an ep then press the back btn and
        # in the ep list page press the back btn it takes me to home page,
        # and I want it to take me to the last page i was in before
        # entering the ep list page". The details page is *also* an
        # overlay, so that navigation ran underneath a details page still
        # on screen - silently swapping the tracker page beneath it for
        # Home (and deleteLater-ing it), so the next Back on the episode
        # list uncovered Home. Landing on Home was an earlier ask, since
        # reversed for the reader for exactly the same reason.
        window = self.window()

        def land():
            try:
                # Focus what is actually on top. The details page the
                # player was opened from is still covering the pages, and
                # focusing the page underneath it would take the keyboard
                # away from the episode list the user is now looking at -
                # including the Esc that closes it.
                overlay = None
                top = getattr(window, "_top_overlay", None)
                if callable(top):
                    overlay = top()
                target = overlay if overlay is not None else getattr(
                    window, "_current_page", None)
                if target is not None:
                    target.setFocus()
            except RuntimeError:
                pass        # the window went away first - app shutdown
            except Exception:
                logs.exception("player could not hand focus back")

        QTimer.singleShot(0, land)
        self.deleteLater()

    def closeEvent(self, event):
        self.close_player()
        super().closeEvent(event)


# ---------------------------------------------------------------------


def open_player(window, entry, season=None, episode=None, streams=None):
    """Put the player over `window` and return it.

    This is the whole wiring surface. A caller (a tracker card's Play, a
    Home item) needs this one call and nothing else - the page parents
    itself, sizes itself, follows the window's resizes, and puts things
    back when it closes.

    Parented to the central widget rather than to main.py's page
    container so it covers the sidebar too (see PlayerPage's docstring),
    and the previous page is left alone underneath rather than being
    torn down: coming back out of the player should land exactly where
    it was entered from, and rebuilding a tracker page costs a full
    reload of its covers."""
    host = window.centralWidget() if hasattr(window, "centralWidget") else window
    # Once per open, and cheap: the file is a couple of dozen records and
    # it is only rewritten when there is actually something to drop.
    forget_untethered_resume()
    # History is written on *open*, not only when the watched threshold
    # is crossed: the owner played things and found Watch History empty,
    # because the only writer was _check_watched at 90% of the runtime.
    # Opening it is the thing History is meant to remember.
    try:
        from helpers import history
        from windows.tracker import format_episode_progress
        shown = (format_episode_progress(int(season or 0), int(episode))
                 if episode else None)
        history.touch(entry, progress=shown)
        # The tick, only for an episode actually named - a film has no
        # episode to tick and is remembered by the touch above.
        if episode:
            history.set_watched(entry, history.episode_key(season, episode),
                                True)
    except Exception:
        logs.exception("could not record the watch history")

    existing = getattr(window, "_player_page", None)
    if existing is not None:
        try:
            existing.close_player()
        except RuntimeError:
            pass

    def on_close():
        window._player_page = None

    try:
        page = PlayerPage(host, entry, season=season, episode=episode,
                          streams=streams, on_close=on_close)
    except Exception:
        # A constructor that raises has already called QWidget.__init__
        # with `host` as parent, so a half-built page is left alive under
        # the central widget with none of its fields - and every later
        # resize walks into it (31 "no attribute 'surface'" errors in
        # the owner's log, 24 August 2026, one per failed open). Take
        # them down before the error goes back to the caller.
        for stray in host.findChildren(PlayerPage):
            if not hasattr(stray, "surface"):
                try:
                    stray.hide()
                    stray.setParent(None)
                    stray.deleteLater()
                except RuntimeError:
                    pass
        raise
    window._player_page = page
    page.setGeometry(host.rect())
    page.show()
    page.raise_()
    freeze_covered(page)   # see widgets._CoveredFreeze
    page.setFocus()
    return page
