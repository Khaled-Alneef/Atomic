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
import os
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
    QComboBox, QFrame, QHBoxLayout, QLabel, QLineEdit, QMenu,
    QPushButton, QSizePolicy, QSlider, QVBoxLayout, QWidget,
)

from helpers import (app_settings, artwork, downloads, logs, net, storage,
                     theme, video_backend)
from helpers.widgets import (Card, GlassPage, GlyphButton, LogoProgress,
                             confirm, scroll_area, show_toast,
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

# Below this there is nothing worth resuming from, and above it the
# episode is effectively finished - offering "resume at 99%" is worse
# than starting over.
RESUME_MIN_S = 30
RESUME_MAX_FRACTION = 0.97

# When an episode counts as watched. Credits and a next-episode teaser
# are routinely the last ~10%, so waiting for the file to actually end
# would mean most finished episodes never got marked.
WATCHED_FRACTION = 0.85

SUB_SIZE_MIN, SUB_SIZE_MAX, SUB_SIZE_STEP = 28, 90, 4
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
CONTROLS_VEIL_ALPHA = 130

_GWL_EXSTYLE = -20
_WS_EX_LAYERED = 0x00080000
_LWA_ALPHA = 0x00000002
_VK_LBUTTON = 0x01

# How long an episode *switch* may wait before the loading frame is
# shown at all - see _show_loading_soon. Under this, a season pack
# that is already downloading hands over the next episode's first
# frame with nothing flashing in between.
LOADING_FLASH_GUARD_MS = 350

# How long after a panel opens, closes or rebuilds the click-to-pause
# poll ignores clicks - see _click_toggle. The poll samples the mouse
# up to 40ms late, and a panel that changed shape under a click (the
# resolution drill-down replacing a tall list with a short one) made
# the *same press* that chose a row read as a click on the video
# behind where the row used to be, which dismissed the drill-down the
# press had just opened (the owner's "clicking 4K closes the window").
PANEL_CLICK_GUARD_S = 0.45

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


def _format_time(seconds) -> str:
    if seconds is None:
        return "--:--"
    seconds = max(0, int(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _resume_key(entry_id, season, episode) -> str:
    """One id per *episode*, not per entry - a series resumed at E4 must
    not hand its position to E5."""
    if episode:
        return f"{entry_id}|s{int(season or 0)}e{int(episode)}"
    return f"{entry_id}|film"


def load_resume(entry_id, season, episode):
    """The saved position for this episode, or None."""
    key = _resume_key(entry_id, season, episode)
    for record in storage.load(RESUME_FILE, []):
        if record.get("id") == key:
            return record
    return None


def save_resume(entry_id, season, episode, position, duration):
    """Remember where this episode got to.

    update_entry first, because it re-reads the file and touches exactly
    one record - writing a whole list back from a page is what erased a
    batch of imported games once (see storage.update_entry). The append
    below is the one case it cannot serve, there being nothing to update
    yet; it is safe here specifically because this file has a single
    writer - one player page, on the UI thread - unlike tracker.json."""
    key = _resume_key(entry_id, season, episode)
    fields = {"entry_id": entry_id, "season": season, "episode": episode,
              "position": float(position or 0.0),
              "duration": float(duration or 0.0),
              "updated_at": storage.now_iso()}
    if storage.update_entry(RESUME_FILE, key, fields):
        return
    records = storage.load(RESUME_FILE, [])
    records.append({"id": key, **fields})
    storage.save(RESUME_FILE, records)


def resume_point(entry):
    """The (season, episode) this entry was last left part-way through,
    or None when there is nothing to go back to.

    What the round continue button on a watching card opens - the same
    idea the reading cards already have, and the same distinction: the
    body of the card plays the *next* episode (see _starting_episode),
    the button goes back to the one that was actually interrupted.

    The two guards are the ones _resume_where_it_stopped already
    applies, and for the same reasons: under RESUME_MIN_S there is
    nothing worth going back to, and past RESUME_MAX_FRACTION the
    episode is finished and "continue" would mean its credits. An
    episode failing them is simply not offered, which leaves the button
    doing what the card body does.

    Newest first, by the timestamp already stored on each record: a
    series can hold a stale half-watched episode from months ago behind
    the one paused ten minutes back."""
    entry_id = (entry or {}).get("id")
    if not entry_id:
        return None
    best = None
    for record in storage.load(RESUME_FILE, []):
        if record.get("entry_id") != entry_id:
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


def clear_resume(entry_id, season, episode):
    """Drop the resume point once the episode is finished, so the next
    visit starts clean instead of offering the credits."""
    key = _resume_key(entry_id, season, episode)
    records = storage.load(RESUME_FILE, [])
    kept = [r for r in records if r.get("id") != key]
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


def _bar_style():
    """The fill behind a translucent bar (top, controls, side panel).

    A top-lit gradient rather than a flat fill, and anchored near-black
    rather than at SURFACE: under the ~70% alpha the bars now carry, a
    mid-tone fill washed white text out over a bright frame, while a dark
    scrim holds the contrast (see BAR_ALPHA). The faint lift at the top
    edge is the "glass" - a sheen catching light along the bar's lip."""
    return (f"qlineargradient(x1:0, y1:0, x2:0, y2:1,"
            f" stop:0 {theme.SURFACE}, stop:0.5 {theme.BG}, stop:1 {theme.BG})")


# ---------------------------------------------------------------------
# Thread boundaries


class _MpvBridge(QObject):
    """mpv's thread -> Qt. Nothing else crosses."""
    prop = Signal(str, object)      # property name, new value
    ended = Signal(str)             # end-file reason


class _WorkBridge(QObject):
    """Background lookups -> Qt. Every signal carries the run number it
    was fired for, so an answer from a superseded episode cannot be
    counted toward the current one - the same rule the tracker's lookups
    follow."""
    streams_ready = Signal(object, int)     # list | None
    subs_ready = Signal(object, int)        # list
    sub_file_ready = Signal(str, str, int)  # path, label, run
    logo_ready = Signal(str, int)           # local png path, run
    # Cinemeta's real episode list for this title. No run: it belongs to
    # the entry, which cannot change while the page lives.
    meta_ready = Signal(object)             # videos list
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

    def paintEvent(self, event):
        painter = QPainter(self)
        rect = self.rect()
        painter.fillRect(rect, QColor(theme.BG))
        if self._pixmap is not None and rect.width() > 0 and rect.height() > 0:
            if self._scaled is None or self._scaled_size != rect.size():
                # Cover, not fit: a 16:9 still in a 21:9 window would
                # otherwise leave two black columns beside it, which
                # reads as a broken image rather than as a backdrop.
                self._scaled = self._pixmap.scaled(
                    rect.size(),
                    Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                    Qt.TransformationMode.SmoothTransformation)
                self._scaled_size = rect.size()
            scaled = self._scaled
            painter.drawPixmap(int((rect.width() - scaled.width()) / 2),
                               int((rect.height() - scaled.height()) / 2),
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

    def set_position(self, value):
        if self._dragging:
            return          # the thumb belongs to the pointer mid-drag
        self._position = float(value or 0.0)
        self.update()

    def set_buffered(self, value):
        self._buffered = float(value or 0.0)
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


class OverlayPanel(QFrame):
    """The popup the subtitle/track/quality buttons open.

    Native for the same reason the controls are (see module docstring),
    and therefore opaque: it sits over the video, and a native child
    window cannot be blended against another native child window."""

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
        label = QLabel(title)
        label.setStyleSheet(
            f"color: {theme.TEXT}; font-size: 13pt; font-weight: 700;"
            f" background: transparent; border: none;")
        header.addWidget(label)
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
        area = scroll_area(self.body)
        area.setStyleSheet("background: transparent; border: none;")
        area.viewport().setStyleSheet("background: transparent;")
        outer.addWidget(area, stretch=1)

        # Vertical: the footer holds full-width stepper rows now, and two
        # of them side by side left the value column 40px wide.
        self.footer_layout = QVBoxLayout()
        self.footer_layout.setSpacing(6)
        outer.addLayout(self.footer_layout)

    def add_group(self, name):
        label = QLabel(name)
        label.setStyleSheet(
            f"color: {theme.TEXT_MUTED}; font-size: 10pt; font-weight: 700;"
            f" background: transparent; border: none; padding-top: 4px;")
        self.body_layout.addWidget(label)

    def add_row(self, title, subtitle, on_click, selected=False, chevron=False):
        card = Card(matte=True)

        # Rows are borderless at rest now (the Harbor pass); the border
        # stays 1px in both states so picking one shifts nothing, and
        # the accent ring remains what says "this is the one in use".
        def paint(is_selected):
            card.setStyleSheet(
                f"QFrame#Card {{ background: {theme.SURFACE_HOVER};"
                f" border: 1px solid"
                f" {theme.ACCENT if is_selected else 'transparent'};"
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
        if chevron:
            arrow = QLabel(GLYPH_CHEVRON)
            arrow.setStyleSheet(
                f"color: {theme.TEXT_MUTED}; font-size: 15pt; font-weight: 700;"
                f" background: transparent; border: none;")
            outer.addWidget(arrow, alignment=Qt.AlignmentFlag.AlignVCenter)
        card.clicked.connect(on_click)
        self.body_layout.addWidget(card)
        return card

    def add_message(self, text):
        label = QLabel(text)
        label.setWordWrap(True)
        label.setStyleSheet(
            f"color: {theme.TEXT_MUTED}; font-size: 11pt;"
            f" background: transparent; border: none;")
        self.body_layout.addWidget(label)
        return label

    def add_stepper(self, name, value_text, on_left, on_right, step_text=""):
        """A stepper as the owner sketched it: the name small above, then
        a full-width row of − button, the value centred between them,
        + button.

        `step_text` puts the amount on the buttons themselves ("-0.1" /
        "+0.1"), the owner's ask - a bare +/- said which way but not by
        how much, and the delay button in particular moves in tenths
        where the font one moves in fours.

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

        self.footer_layout.addWidget(block)
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
        self._stream_index = 0
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
        self._sub_delay = 0.0
        # Said once per player, not once per nudge - see _apply_sub_delay.
        self._delay_warned = False
        self._sub_size = SUB_SIZE_DEFAULT
        # None, not SUB_POS_CLEAR: the first _set_sub_position must reach
        # mpv even if it asks for what mpv's default already is.
        self._sub_pos = None
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
        self._dl_audio = "jp"
        self._dl_folder = None
        self._temp_dir = None
        self._pending_resume = None
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

        self._bridge = _MpvBridge()
        self._bridge.prop.connect(self._on_property)
        self._bridge.ended.connect(self._on_ended)
        self._work = _WorkBridge()
        self._work.streams_ready.connect(self._on_streams)
        self._work.subs_ready.connect(self._on_subtitles)
        self._work.sub_file_ready.connect(self._on_subtitle_file)
        self._work.logo_ready.connect(self._on_logo)
        self._work.meta_ready.connect(self._on_meta)
        self._work.stream_prepared.connect(self._on_stream_prepared)
        self._work.failed.connect(self._on_failed)
        self._work.note.connect(self._on_failed)

        # Cinemeta's real season/episode map, once it arrives: what the
        # season list, the prev/next bounds and the wrong-episode guard
        # answer from instead of guessing off latest_available. None
        # until then - the guesses below hold the fort for the first
        # half-second.
        self._meta_aired = None       # {season: highest aired episode}
        self._meta_videos = None      # Cinemeta's raw rows, for the list
        # Whether the next episode's sources have been prefetched into
        # streams' cache for this episode's run.
        self._prefetched = False

        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)

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
        # When a panel last opened, closed or rebuilt - what the click
        # poll holds off for (see _click_toggle / PANEL_CLICK_GUARD_S).
        self._panel_guard = 0.0
        self._pointer_timer = QTimer(self)
        self._pointer_timer.timeout.connect(self._poll_pointer)
        self._last_pointer = QCursor.pos()
        self._mouse_timer = QTimer(self)
        self._mouse_timer.timeout.connect(self._poll_mouse)
        self._save_timer = QTimer(self)
        self._save_timer.timeout.connect(self._save_position)

        if parent is not None:
            parent.installEventFilter(self)
            self.setGeometry(parent.rect())

        # The real episode map, asked for once per page. It usually
        # lands inside half a second - well before the stream lookup -
        # which is what lets _apply_meta_bounds catch a request for an
        # episode that does not exist before anything wrong plays.
        if self.episode:
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
            self._work.meta_ready.emit(list(meta.get("videos") or []))

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
        self.season_combo = QComboBox()
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
        self._save_position()
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
                     "paused-for-cache", "cache-buffering-state"):
            self.handle.observe_property(name, self._mpv_property)
        self.handle.register_event_callback(self._mpv_event)
        # Both levers, and the delay too - an .ass needs sub-scale
        # rather than sub-font-size (see _apply_sub_style), and the
        # delay used not to be applied at all until the first nudge.
        self._apply_sub_style()
        self._apply_sub_delay()
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
        self._prefetched = False
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
        self._subtitles = []
        self._set_subtitle_count(None)

        if self._given_streams is not None:
            self._on_streams(self._given_streams, self._run)
        elif streams_module is None:
            self._show_status("No stream sources are available in this build.")
        else:
            self._show_loading_soon("Looking for a source...")
            self._spawn(self._find_streams_worker, self._run)
        self._search_subtitles()

    # ---- background work ---------------------------------------------
    def _spawn(self, target, *args):
        thread = threading.Thread(target=target, args=args, daemon=True)
        thread.start()

    def _find_streams_worker(self, run):
        """Never raises: an exception here would kill the thread silently
        and the page would sit on "Looking for a source..." forever."""
        try:
            found = streams_module.find_streams(
                self.entry, season=self.season, episode=self.episode,
                deadline=net.deadline_in(STREAM_BUDGET_S))
            self._work.streams_ready.emit(list(found or []), run)
        except Exception:
            logs.exception("Stream lookup failed")
            self._work.streams_ready.emit([], run)

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
                on_partial=partial)
            self._work.subs_ready.emit(list(found or []), run)
        except Exception:
            logs.exception("Subtitle search failed")
            self._work.subs_ready.emit([], run)

    def _fetch_subtitle_worker(self, result, run):
        """Fetch, then write UTF-8 to a temp file and hand mpv the path.

        mpv is never given the url. Many of these hosts need the same
        Referer the stream did, and an Arabic .srt is very often
        Windows-1256 - helpers/subtitles.fetch has already decoded it,
        and re-encoding as UTF-8 here means libass reads real text
        instead of one long line of mojibake.

        A non-Arabic pick goes through the AI translator when a provider
        key is configured (Settings → API Keys). This is the whole reason
        English results are in the list at all: for seasonal anime an
        English track is routinely the only thing published, and
        translating it is the one route that does not depend on somebody
        having released Arabic for that exact episode."""
        try:
            text = subtitles_module.fetch(result, net.deadline_in(SUBTITLE_BUDGET_S))
            if not text:
                self._work.failed.emit("That Subtitle Could Not Be Downloaded", run)
                return
            fmt = result.get("format") or "srt"
            # The same name the row carried (see _name_subtitles), so
            # the loaded track and the list agree on what was picked.
            label = (result.get("display_name") or result.get("release")
                     or "Subtitle")
            lang = str(result.get("lang") or "").lower()
            if (not lang.startswith("ar") and ai_translate is not None
                    and ai_translate.available()):
                provider = ai_translate.default_provider()
                self._work.note.emit(
                    f"Translating to Arabic With {ai_translate.label(provider)}...",
                    run)
                cues = subtitles_module.parse(text, fmt)
                translated = None
                try:
                    translated = ai_translate.translate(
                        cues, provider=provider,
                        cancelled=lambda: self._closing or run != self._run)
                except ai_translate.TranslationFailed as failure:
                    # **Say which provider said no, and what it said.**
                    # This used to be a bare "Translation Failed" for
                    # every cause, which is how four pasted keys - three
                    # accounts out of credit, one naming a model Google
                    # had retired - looked like a dead feature instead of
                    # four different billing problems.
                    logs.warning(f"AI translation failed: {failure.reason}")
                    self._work.note.emit(
                        f"AI Translation Failed - {failure.reason}", run)
                if translated:
                    text, fmt = ai_translate.to_srt(translated), "srt"
                    label = f"{label} (AI Arabic)"
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
    def _on_streams(self, found, run):
        if self._closing or run != self._run:
            return
        self._streams = list(found or [])
        if not self._streams:
            self._show_status(
                "No playable source was found for this episode.\n"
                "Try again, or open it on its site.")
            return
        drm = [s for s in self._streams if s.get("kind") == "drm"]
        playable = [s for s in self._streams if s.get("kind") != "drm"]
        if not playable:
            self._show_drm(drm[0])
            return
        self._streams = playable + drm
        self._play_stream(0)

    def _on_subtitles(self, found, run):
        if self._closing or run != self._run:
            return
        self._subtitles = _name_subtitles(found or [])
        self._set_subtitle_count(len([s for s in self._subtitles
                                      if str(s.get("lang", "")).lower().startswith("ar")]))
        if self._panel is not None and getattr(self._panel, "kind", "") == "subs":
            self._open_subtitle_panel(rebuild=True)   # in place, with the results

    def _on_subtitle_file(self, path, label, run):
        if self._closing or run != self._run or self.handle is None:
            return
        try:
            self.handle.sub_add(path, "select")
            # Re-applied after the add, not before: a freshly selected
            # track starts from mpv's defaults, and an .ass needs the
            # scale lever rather than the font-size one.
            self._apply_sub_style()
            self._apply_sub_delay()
            self.handle["sub-visibility"] = True
        except Exception:
            logs.exception("sub-add failed")
            show_toast(self._toast_anchor(), "Subtitle Could Not Be Loaded")
            return
        # mpv accepts sub_add and then quietly leaves the old track
        # primary if it could not parse the file. Without this the panel
        # said "Subtitle Loaded", moved its highlight, and the picture
        # kept showing whatever was there before - which from the chair
        # reads as "the delay does nothing on this one".
        if not self._external_sub_selected(path):
            logs.warning(f"mpv did not select the loaded subtitle {path}")
            show_toast(self._toast_anchor(), "That Subtitle Could Not Be Read")
            return
        self._subtitle_label = label
        show_toast(self._toast_anchor(), "Subtitle Loaded")
        if self._panel is not None and getattr(self._panel, "kind", "") == "subs":
            self._open_subtitle_panel(rebuild=True)

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
        self.source_label.setText(stream.get("source") or "")

    def _update_audio_pill(self):
        """The audio-language pill follows the selected audio track."""
        audio = [t for t in self._tracks if t.get("type") == "audio"]
        selected = next((t for t in audio if t.get("selected")), None)
        lang = str((selected or {}).get("lang") or "").strip()
        self.audio_pill.setText(lang.upper() if lang else "AUDIO")
        self.audio_pill.setVisible(bool(audio))

    # ---- playback ----------------------------------------------------
    def _play_stream(self, index, resume_at=None):
        if self.handle is None or not self._streams:
            return
        index = max(0, min(index, len(self._streams) - 1))
        stream = self._streams[index]
        if stream.get("kind") == "drm":
            self._show_drm(stream)
            return
        self._stream_index = index
        self._hide_status()
        self._update_source_display(stream)

        # A torrent arrives without a url on purpose: the streaming
        # server has to be given the release's trackers before it can
        # serve anything, and doing that for all thirty-odd results at
        # lookup time would announce to every tracker for things nobody
        # is going to watch. So it happens here, for the one chosen
        # stream - off the UI thread, because it can also have to start
        # the server.
        if not stream.get("url") and stream.get("info_hash") and streams_module:
            self._show_loading_soon("Connecting to the source...")
            self._spawn(self._prepare_stream_worker, index, resume_at, self._run)
            return

        self._load_into_mpv(stream, resume_at)

    def _prepare_stream_worker(self, index, resume_at, run):
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
        failed, so it re-ran the whole wait to fail again."""
        try:
            chosen = self._streams[index]
            others = [s for i, s in enumerate(self._streams)
                      if i != index and i not in self._dead_sources
                      and s.get("kind") != "drm"
                      and _canonical_quality(s.get("quality"))
                      == _canonical_quality(chosen.get("quality"))]
            if hasattr(streams_module, "prepare_fastest"):
                stream = streams_module.prepare_fastest(
                    [chosen] + others, season=self.season, episode=self.episode)
            else:
                stream = streams_module.prepare(
                    chosen, season=self.season, episode=self.episode)
            self._work.stream_prepared.emit(stream, index, resume_at, run)
        except Exception:
            logs.exception("Preparing the stream failed")
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
            if self._try_next_source(index):
                return
            self._show_status(
                "None of the sources for this would start.\n"
                "Try again in a moment, or pick one from the list.")
            return
        self._streams[index] = stream
        self._hide_status()
        self._load_into_mpv(stream, resume_at)

    # How many dead sources to walk past before giving up and saying so.
    # Bounded rather than open-ended: each attempt costs a create call
    # and a peer wait, and a title where the first several are all dead
    # is a title with a real problem, not one more retry away.
    MAX_SOURCE_ATTEMPTS = 5

    def _try_next_source(self, failed_index) -> bool:
        """Move to the next untried source after one failed to start.

        Returns False once there is nothing left worth trying, which is
        when the page finally says so out loud."""
        self._dead_sources.add(failed_index)
        if len(self._dead_sources) >= self.MAX_SOURCE_ATTEMPTS:
            return False
        for index in range(len(self._streams)):
            if index in self._dead_sources:
                continue
            if self._streams[index].get("kind") == "drm":
                continue
            self._show_loading("That source had no peers. Trying the next one...")
            self._play_stream(index)
            return True
        return False

    def _update_startup_status(self):
        """What to say between choosing a source and the first frame.

        A torrent that has just been created has no peers yet, so this
        window is genuinely long - long enough that with nothing on
        screen it reads as a hang. Naming the stage, and showing the
        buffer filling, is the difference between "it's working" and
        "it's broken"."""
        if not self._awaiting_first_frame or self._closing:
            return
        percent = getattr(self, "_buffering_percent", 0)
        # The logo is the progress. Percent is what mpv reports for its
        # cache; before that starts moving there is still the peer
        # search, so the bar creeps rather than sitting at zero - a
        # completely empty logo reads as nothing happening.
        if self.logo.has_logo():
            self.logo.set_fraction(max(percent / 100.0, 0.04))
        # No words with the logo up - the pulse and the fill are the
        # whole message now (the owner's ask). The text survives only as
        # _show_loading's fallback for a title with no logo art.
        if percent > 0:
            self._show_loading(f"Buffering... {percent}%")
        else:
            self._show_loading("Finding peers for this source...\n"
                               "The first few seconds take longest.")

    def _load_into_mpv(self, stream, resume_at=None):
        self._awaiting_first_frame = True
        self._buffering_percent = 0
        self._update_startup_status()

        # Headers before the load, not after: many of these hosts answer
        # 403 to a request without the Referer the page carried, and mpv
        # reads these options when it opens the url.
        headers = stream.get("headers") or {}
        try:
            fields = [f"{name}: {value}" for name, value in headers.items()
                      if name.lower() != "user-agent"]
            self.handle["http-header-fields"] = fields
            agent = next((v for k, v in headers.items() if k.lower() == "user-agent"), None)
            if agent:
                self.handle["user-agent"] = agent
            self.handle.play(stream.get("url") or "")
            self.handle["pause"] = False
        except Exception:
            logs.exception("Starting playback failed")
            self._show_status("That source could not be opened. Try another one.")
            return

        if resume_at:
            self._pending_resume = float(resume_at)
        else:
            self._resume_where_it_stopped()

    def _resume_where_it_stopped(self):
        """Seek to the stored position, without asking.

        There used to be a "Resume from 12:34?" bar with Resume and Start
        Over on it. It is gone on purpose: the answer was Resume every
        single time, and a question whose answer is always the same is a
        step, not a choice - worse, ignoring it for twelve seconds meant
        starting over, so the default was the option nobody wanted.

        The two guards that made the bar sensible are kept exactly as
        they were, because they are what stops "always resume" from being
        wrong: under RESUME_MIN_S there is nothing worth resuming to, and
        past RESUME_MAX_FRACTION the episode is effectively finished and
        resuming would open on the credits."""
        record = load_resume(self.entry.get("id"), self.season, self.episode)
        if not record:
            return
        position = float(record.get("position") or 0.0)
        duration = float(record.get("duration") or 0.0)
        if position < RESUME_MIN_S:
            return
        if duration and position > duration * RESUME_MAX_FRACTION:
            return
        # Not seeked here: nothing is decoded yet and mpv answers a seek
        # on an unloaded file with an error. _on_property applies it on
        # the first frame, the same path a source switch already uses.
        self._pending_resume = position
        show_toast(self._toast_anchor(),
                   f"Resumed From {_format_time(position)}")

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

    def _seek_absolute(self, seconds):
        if self.handle is None:
            return
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
        for preset in SPEED_PRESETS:
            chip = QPushButton(f"{preset:g}x")
            chip.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            current = abs(preset - self._speed) < 1e-6
            chip.setStyleSheet(
                f"QPushButton {{ background: {theme.SURFACE_HOVER if current else 'transparent'};"
                f" color: {theme.ACCENT if current else theme.TEXT};"
                f" border: none; padding: 5px 7px; font-size: 10pt; font-weight: 600;"
                f" border-radius: {theme.RADIUS_SM}px; }}"
                f"QPushButton:hover {{ background: {theme.SURFACE_HOVER}; }}")
            use_hover_cursor(chip)
            chip.clicked.connect(lambda checked=False, p=preset: self._set_speed(p))
            chips.addWidget(chip)
        panel.body_layout.addLayout(chips)

        panel.speed_value = value
        panel.speed_slider = slider
        panel.finish()
        self._show_panel(panel)

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
        self._save_position()
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
        try:
            if str(getattr(event, "event_id", "")).endswith("END_FILE"):
                self._bridge.ended.emit("eof")
        except Exception:
            pass

    # ---- property updates (Qt thread) --------------------------------
    def _on_property(self, name, value):
        if self._closing:
            return
        if name == "time-pos" and value is not None:
            if self._awaiting_first_frame and float(value) > 0:
                # The first frame is the only proof that a source has
                # actually started; until it lands, "nothing on screen"
                # and "broken" look identical.
                self._awaiting_first_frame = False
                self._hide_status()
                # Playback is up - now spend idle time on the episode
                # after this one, so Next answers from the cache instead
                # of re-running the whole source fan-out.
                self._prefetch_next_episode()
            self._position = float(value)
            self.seek_bar.set_position(self._position)
            self._update_time_label()
            self._check_watched()
            if self._pending_resume is not None and self._position > 0:
                target, self._pending_resume = self._pending_resume, None
                self._seek_absolute(target)
        elif name == "duration" and value:
            self._duration = float(value)
            self.seek_bar.set_duration(self._duration)
            self._update_time_label()
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
            self._update_audio_pill()
            # An open tracks panel follows mpv's own answer: the pick
            # already moved the highlight optimistically (_pick_track),
            # and this is the confirmation - or the correction, when mpv
            # refused the track - arriving a beat later. Repainted rather
            # than rebuilt, unless the track list itself changed: mpv
            # emits track-list on every sub_add too, and tearing four
            # native windows down over the video each time is what the
            # owner saw as the panel flickering.
            if self._panel is not None and getattr(self._panel, "kind", "") == "tracks":
                if not self._highlight_tracks():
                    self._open_tracks_panel(rebuild=True)
        elif name == "paused-for-cache":
            # Only ever a note, never an error: a stalled buffer usually
            # recovers, and a dialog for it would fire constantly on a
            # slow source.
            if value and not self._status_visible():
                self.source_label.setText("Buffering...")
            elif not value and self._streams:
                self._update_source_display()
        elif name == "cache-buffering-state":
            self._buffering_percent = int(value or 0)
            self._update_startup_status()

    def _on_ended(self, _reason):
        if self._closing:
            return
        self._check_watched(force=True)

    def _prefetch_next_episode(self):
        """Warm streams' result cache for the episode after this one.

        Sources only - nothing is prepared and no torrent is created, so
        no tracker is announced to and no bandwidth is taken from the
        episode actually playing. It just means pressing Next skips the
        several-second fan-out and goes straight to connecting."""
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
        clear_resume(self.entry.get("id"), self.season, self.episode)

    def _save_position(self):
        if self._closing or not self._duration or self._position <= RESUME_MIN_S:
            return
        if self._position > self._duration * RESUME_MAX_FRACTION:
            return
        try:
            save_resume(self.entry.get("id"), self.season, self.episode,
                        self._position, self._duration)
        except Exception:
            logs.exception("Could not save the resume position")

    # ---- panels ------------------------------------------------------
    def _close_panel(self):
        # Stamped on every close as well as every show: the click that
        # picked a row can close (or rebuild) the panel mid-press, and
        # the poll's late sample must not read that press as a click on
        # whatever the panel uncovered - see PANEL_CLICK_GUARD_S.
        self._panel_guard = time.monotonic()
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
        self._close_panel()
        panel = OverlayPanel(self, title)
        panel.kind = kind
        panel.closed.connect(self._close_panel)
        panel.installEventFilter(self)
        self._panel = panel
        return panel

    def _open_subtitle_panel(self, rebuild=False):
        panel = self._new_panel("Arabic Subtitles", "subs", rebuild)
        if panel is None:
            return          # the same button closed it
        panel.add_row("Off", "No external subtitle", self._subtitles_off,
                      selected=self._subtitle_label == "Off")

        arabic = [s for s in self._subtitles
                  if str(s.get("lang", "")).lower().startswith("ar")]
        other = [s for s in self._subtitles if s not in arabic]
        if not self._subtitles:
            panel.add_message(
                "Searching..." if subtitles_module is not None
                else "Subtitle search is not available in this build.")
        for group, items in (("Arabic", arabic), ("Other Languages", other)):
            if not items:
                continue
            by_source = {}
            for item in items:
                by_source.setdefault(item.get("source") or "Unknown", []).append(item)
            for source, entries in by_source.items():
                panel.add_group(f"{group.upper()}  ·  {source.upper()}")
                for item in entries:
                    # "Arabic 2 SubDL" (see _name_subtitles), with the
                    # release name moved down to the detail line - it is
                    # still worth seeing, it just is not a name.
                    label = (item.get("display_name")
                             or item.get("release") or "Subtitle")
                    parts = [str(p) for p in
                             (item.get("format"), item.get("release")) if p]
                    # Say so when a line was produced by machine
                    # translation rather than written by a person. It is
                    # often serviceable and sometimes nonsense, and
                    # which one you picked should not be a guess -
                    # especially for anime, where these are frequently
                    # the only Arabic on offer.
                    if item.get("translated"):
                        parts.insert(0, "auto-translated")
                    panel.add_row(label, " · ".join(parts),
                                  lambda checked=False, r=item: self._pick_subtitle(r),
                                  selected=label == self._subtitle_label)

        # **AI translation, said out loud.** Picking a non-Arabic
        # subtitle has always run it through the configured translator,
        # but nothing in this panel ever said so - the owner pasted keys
        # for every provider, saw no "AI" anywhere, and reasonably
        # concluded the feature was dead. These rows are the same picks
        # as the "Other Languages" group above, listed again under a
        # name that says what they will become.
        translator = ""
        try:
            if ai_translate is not None and ai_translate.available():
                translator = ai_translate.label(ai_translate.default_provider())
        except Exception:
            translator = ""
        if translator and other:
            panel.add_group(f"ARABIC  ·  TRANSLATED BY {translator.upper()}")
            for index, item in enumerate(other, start=1):
                source_name = item.get("display_name") or item.get("release") or ""
                label = f"Arabic (AI) {index} {translator}"
                panel.add_row(
                    label, f"translated from {source_name}",
                    lambda checked=False, r=item: self._pick_subtitle(r),
                    selected=label == self._subtitle_label)
        elif other and ai_translate is not None:
            # A key would turn these into Arabic; say where to add one
            # rather than leaving the English rows looking pointless.
            panel.add_group("ARABIC  ·  AI TRANSLATION")
            panel.add_message(
                "Add an OpenAI, DeepSeek, Gemini or Anthropic key in "
                "Settings > API Keys to translate the subtitles above "
                "into Arabic.")
        panel.finish()

        # Delay first, size second: resyncing a mismatched Arabic release
        # is by far the most common thing anyone needs from this panel.
        set_delay = panel.add_stepper(
            "Delay", self._delay_text(),
            lambda: self._nudge_delay(-SUB_DELAY_STEP),
            lambda: self._nudge_delay(SUB_DELAY_STEP),
            step_text=f"{SUB_DELAY_STEP:g}")
        set_size = panel.add_stepper(
            "Font size", str(self._sub_size),
            lambda: self._nudge_size(-SUB_SIZE_STEP),
            lambda: self._nudge_size(SUB_SIZE_STEP),
            step_text=str(SUB_SIZE_STEP))
        # Held on the panel, not captured in the nudge calls: the panel is
        # rebuilt whenever a subtitle is picked, and a setter belonging to
        # a deleted QLabel would take the process with it.
        panel.set_delay_text = set_delay
        panel.set_size_text = set_size
        self._show_panel(panel)

    def _delay_text(self):
        return f"{self._sub_delay:+.1f}s"

    def _subtitles_off(self):
        if self.handle is not None:
            try:
                self.handle["sub-visibility"] = False
                self.handle["sid"] = "no"
            except Exception:
                logs.exception("Turning subtitles off failed")
        self._subtitle_label = "Off"
        # Rebuilt, not toggled: this runs from the panel's own "Off" row,
        # and the point is to move the highlight onto it - closing the
        # panel under the press would look like the click missed.
        self._open_subtitle_panel(rebuild=True)

    def _pick_subtitle(self, result):
        if subtitles_module is None:
            return
        show_toast(self._toast_anchor(), "Loading Subtitle...")
        self._spawn(self._fetch_subtitle_worker, result, self._run)

    def _nudge_delay(self, delta):
        self._sub_delay = round(self._sub_delay + delta, 2)
        self._apply_sub_delay()
        self._update_stepper("set_delay_text", self._delay_text())

    def _nudge_size(self, delta):
        self._sub_size = max(SUB_SIZE_MIN, min(SUB_SIZE_MAX, self._sub_size + delta))
        self._apply_sub_style()
        self._update_stepper("set_size_text", str(self._sub_size))

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
        # whole panel - see _highlight_tracks.
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

    def _highlight_tracks(self) -> bool:
        """Repaint the open tracks panel's rows from `self._tracks`.

        Returns False when there is no panel to repaint, or when its rows
        no longer describe the current track list (mpv gained or lost a
        track) - the caller then rebuilds.

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
        wanted = {("aid" if t.get("type") == "audio" else "sid", t.get("id"))
                  for t in self._tracks if t.get("type") in ("audio", "sub")}
        if wanted - (set(rows) - {("sid", None)}):
            return False          # a track appeared that has no row
        selected = {("aid" if t.get("type") == "audio" else "sid", t.get("id"))
                    for t in self._tracks if t.get("selected")}
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
            self.handle[prop] = "no" if track is None else int(track.get("id"))
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
        # In place when the rows still fit the track list; a rebuild is
        # the fallback, not the normal path (see _highlight_tracks).
        if not self._highlight_tracks():
            self._open_tracks_panel(rebuild=True)

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
            best = matches[0]
            seeders = int(best.get("seeders") or 0)
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

        panel.add_group("AUDIO")
        panel.add_row("Japanese (original)",
                      "The ordinary fansub releases",
                      lambda: self._dl_set("audio", "jp"),
                      selected=self._dl_audio == "jp")
        panel.add_row("English dub",
                      "Prefers dual-audio releases when one exists",
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
        it useless."""
        self._close_panel()
        self._play_stream(index, resume_at=self._position)

    def _show_panel(self, panel):
        self._panel_guard = time.monotonic()
        # Geometry first - see _show_status.
        self._layout_overlays()
        panel.show()
        panel.raise_()
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

    def _show_backdrop(self):
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
        test, not which message it is."""
        if self._closing:
            return
        if self._position > 0 and not self._awaiting_first_frame:
            return
        self.backdrop.show()
        self.backdrop.raise_()
        self.logo.setVisible(self.logo.has_logo())
        for overlay in (self._episode_bar, self._panel, self.controls,
                        self.top_bar):
            if overlay is not None and overlay.isVisible():
                overlay.raise_()

    def _hide_status(self):
        self._loading_delay.stop()
        self._loading_visible = False
        self.status.hide()
        # The loading frame goes with it: it covers the whole window, so
        # leaving it up would hide the first frame it was waiting for.
        self.backdrop.hide()

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
        if self._episode_bar is not None and self._episode_bar.isVisible():
            self._episode_bar.raise_()
            self._veil(self._episode_bar, BAR_ALPHA)
        if self._panel is not None:
            self._panel.raise_()
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
        # Written only when it changes: this is called from every wake,
        # and a property write costs mpv a subtitle re-render. Remembered
        # rather than read back, because the read is the same round trip
        # as the write.
        if self.handle is None or value == self._sub_pos:
            return
        try:
            self.handle["sub-pos"] = value
            self._sub_pos = value
        except Exception:
            logs.exception("Subtitle position change failed")

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
        press on the video."""
        try:
            window = self.window()
            if window is None:
                return False
            origin = window.geometry().topLeft() + self.mapTo(window, QPoint(0, 0))
            if not QRect(origin, self.size()).contains(position):
                return False
        except RuntimeError:
            return False
        overlays = [self.controls, self.top_bar, self.status,
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
        elif pressed_since and self._is_active() and self._over_video(position):
            self._click_toggle(position)

    def _is_active(self) -> bool:
        try:
            window = self.window()
            return window is not None and window.isActiveWindow()
        except RuntimeError:
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
        # A press within the guard of a panel opening, closing or
        # rebuilding is that interaction's own press, whatever the
        # geometry says now: the resolution drill-down replaces a tall
        # panel with a short one under the pointer mid-click, so the
        # release lands "over the video" where the row just was and
        # dismissed the very view the click had opened (the reported
        # "clicking 4K closes the window").
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
        if now - self._last_click_time <= DOUBLE_CLICK_S:
            self._last_click_time = 0.0
            try:
                self.handle["pause"] = self._pause_before_click
            except Exception:
                logs.exception("Restoring pause on double-click failed")
            self.toggle_fullscreen()
            return
        self._last_click_time = now
        self._pause_before_click = self._paused
        self._wake_controls()
        self.toggle_pause()

    # ---- layout ------------------------------------------------------
    def _layout_overlays(self):
        rect = self.rect()
        self.surface.setGeometry(rect)
        self.top_bar.setGeometry(0, 0, rect.width(), TOPBAR_HEIGHT)
        self.controls.setGeometry(0, rect.height() - CONTROLS_HEIGHT,
                                  rect.width(), CONTROLS_HEIGHT)
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
        self.backdrop.setGeometry(rect)
        width = min(680, max(320, rect.width() - 120))
        height = self.status.sizeHint().height() + 12
        # Much smaller than the old 46%-of-window treatment (the owner's
        # ask): a quarter of the width, capped at 300 - a loading mark
        # that pulses in the middle, not a poster.
        logo_width = max(160, min(int(rect.width() * 0.24), 300))
        logo_height = int(logo_width * 0.34)
        logo_y = int((rect.height() - logo_height) / 2)
        self.logo.setGeometry(int((rect.width() - logo_width) / 2), logo_y,
                              logo_width, logo_height)
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
            width = min(PANEL_WIDTH, max(280, rect.width() - 60))
            # As tall as its content and no taller: the speed panel is a
            # value, a slider and a row of chips, and at the old fixed
            # height it was four-fifths empty (the owner's complaint).
            # Long lists still cap at PANEL_MAX_HEIGHT and scroll.
            wanted = (self._panel.body.sizeHint().height()
                      + self._panel.footer_layout.sizeHint().height() + 96)
            height = max(150, min(PANEL_MAX_HEIGHT, wanted,
                                  rect.height() - CONTROLS_HEIGHT - 80))
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
    def keyPressEvent(self, event):
        key = event.key()
        self._wake_controls()
        if key == Qt.Key.Key_Space:
            self.toggle_pause()
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
        self._save_position()
        self._idle_timer.stop()
        self._pointer_timer.stop()
        self._mouse_timer.stop()
        self._save_timer.stop()
        self._loading_delay.stop()
        self._close_panel()
        # Cursor first: the surface is about to go away, and a widget
        # that dies holding the blank cursor leaves Windows painting it
        # over whatever comes next.
        self.surface.unsetCursor()
        handle, self.handle = self.handle, None
        video_backend.shutdown(handle)
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

    page = PlayerPage(host, entry, season=season, episode=episode,
                      streams=streams, on_close=on_close)
    window._player_page = page
    page.setGeometry(host.rect())
    page.show()
    page.raise_()
    page.setFocus()
    return page
