"""The details page: what clicking the body of a tracker card opens.

A full-window surface over the app - the same overlay mechanism the
reader and player use (parented to the main window's central widget, so
the sidebar is covered, not hidden) - showing one entry the way a
streaming service's title page does: the artwork full-bleed behind a
scrim, the title's logo, the facts (runtime, years, rating, genres,
cast, summary) down the left, and the thing you actually came for on the
right - every episode or chapter, with its date and whether it has been
watched, one click from playing.

Where the data comes from, and why it costs almost nothing:

  * **Video** (Anime/Series/Movie): one keyless Cinemeta request
    (stremio.fetch_meta) carries every fact on the page *and* the whole
    episode list - name, date and thumbnail per episode. Measured on
    Bleach TYBW: 50 episodes, every field present.
  * **Reading** (Manga/Manhwa/Manhua): the chapter list comes from
    chapter_source, which is cached on disk for six hours - a details
    page for a series that was just read opens instantly.
  * The backdrop and logo are TMDB via helpers/artwork, cached on disk
    by IMDb id, and both fail soft to nothing: no backdrop means the
    page keeps its own dark ground, no logo means the title is text.

The split between the two ways into an entry is settled here once:
**the card's body opens this page; only the round continue button on
the cover resumes.** Before this page existed the two did nearly the
same thing for video and the owner rightly called that broken.
"""

import copy
import datetime
import re
import threading
import time
import uuid
from pathlib import Path

from PyQt6.QtCore import (QEvent, QObject, QPoint, QRect, QRectF, QSize, Qt,
                          QTimer)
from PyQt6.QtCore import pyqtSignal as Signal
from PyQt6.QtGui import (QBrush, QColor, QCursor, QIcon, QLinearGradient,
                         QPainter, QPainterPath, QPixmap, QTransform)
from PyQt6.QtWidgets import (
    QComboBox, QDialog, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit,
    QMenu, QPushButton, QVBoxLayout, QWidget,
)

from helpers import (anime_identity, app_settings, artwork, hero_art, history,
                     images,
                     logs, lookup_pool, net, storage, theme)
from helpers.poster_grid import PosterGrid
from helpers.widgets import (CHECK_GLYPH, PLUS_GLYPH, TRASH_GLYPH,
                             TRASH_ICON_SIZE, Card, GlassPage,
                             GlyphButton, PickCombo, confirm,
                             frameless_dialog, freeze_covered, glyph_icon,
                             scroll_area, search_field, show_toast,
                             show_undo_toast, use_hover_cursor)

try:
    from helpers import stremio
except Exception:                                   # pragma: no cover
    stremio = None
try:
    from helpers import chapter_source
except Exception:                                   # pragma: no cover
    chapter_source = None

MANGA_TYPES = ("Manga", "Manhwa", "Manhua")

# The right-hand list panel. Wide enough for an episode name, a date and
# a badge on one row; the rest of the window belongs to the artwork.
# Widened with the rows: the owner asked for larger text throughout this
# list, and 12pt names in a 440px panel elided half of everything.
# 560, up from 490 - the owner's ask, 30 August 2026: "make the ep
# list wider". The rows inside are a still, two lines of text and a
# DONE badge, and at 490 the badge crowded the date line.
PANEL_WIDTH = 560
ROW_HEIGHT = 72

# **The list rows are not all one shape any more** (the owner's ask, 22
# August 2026: "the buttons in ep/ch and sources list feel stiff and
# old... make the resolution buttons distinguished from the sources
# buttons"). Before this, every row in both lists was the same 72px
# slab in the same fill, so a resolution *heading* and one of the
# releases under it were the same object on screen and the grouping was
# invisible - see _group_card for what separates them now.
#
# Height is the first thing that says what a row is, so each kind gets
# its own and they are deliberately far apart:
EPISODE_ROW_HEIGHT = 88     # ...carries a 16:9 still, so the tallest
SOURCE_ROW_HEIGHT = 68      # one release: indented under its heading
GROUP_ROW_HEIGHT = 50       # a resolution heading: shorter than any row
BACK_ROW_HEIGHT = 46        # navigation, not content - the shortest

# Cinemeta carries a still per episode and it costs nothing extra to
# use: measured 22 August 2026 on Re:Zero, **155 of 155 videos carry
# `thumbnail`** - 780x439 JPEGs from episodes.metahub.space, 29-65KB,
# 0.19s each after the first connection. So the row art needs no new
# fetcher and no second service; it is already in the meta the list is
# drawn from. 16:9 at the row height above.
STILL_SIZE = (112, 63)
# The blurred copy is a *file*, written once beside the original in the
# image cache, rather than a blur applied per row. QPixmap cannot be
# built off the UI thread, so blurring in the slot would put PIL back on
# the thread images.prewarm exists to keep it off - 337ms of it was
# measured in the Discover rows earlier today. Writing a second file
# instead means the blurred tile goes through exactly the same
# download -> _fitted -> _PIXMAP path as the sharp one.
STILL_BLUR_RADIUS = 6.0     # at STILL_SIZE, not at the 780px original
# Stills already on disk, url -> local path, so a rebuild (every search
# keystroke rebuilds the list) redraws from the pixmap cache instead of
# queueing forty pool jobs again. Bounded: a long session browsing many
# titles would otherwise grow it without limit.
_STILL_READY = {}
_STILL_READY_MAX = 800

# How many list rows exist before the list is shown, and how many more
# are added each time the view nears the end of them. See
# DetailsPage._queue_rows for the measurement these come from - a
# 508-chapter list built in one go froze the UI for 1.26s.
#
# 40 against a viewport that holds ~13 rows: enough that the first
# screen and the one after it are already there, few enough that
# building them is ~50ms.
ROW_FIRST_BATCH = 40
ROW_BATCH = 40
# Start building the next batch this far from the end, so rows are ready
# before the scroll reaches them rather than after.
ROW_EXTEND_MARGIN_PX = ROW_HEIGHT * 6

# The scrim over the backdrop, top to bottom - heavier than the player's
# loading frame because real text sits on this one.
#
# **Lifted, 23 August 2026 - the owner's "in the ch list page add the
# cover as bg image but make it blurred!", sent with a screenshot of a
# Berserk chapter list that is flat dark with no picture in it at all.**
#
# The page was already doing the work: `_reading_art_worker` falls back
# to the entry's own cover when no AniList banner exists, and paintEvent
# already turns a portrait cover into a blurred ground rather than
# stretching it (see the note there). What was wrong is that at alpha
# 200/170/232 over a ground blurred at 24x, there was nothing left to
# see - the scrim was doing to a real backdrop what it does to an empty
# one, so "no art" and "art" looked identical. That is why the owner read
# it as a missing feature rather than an invisible one.
#
# The premise that reading was wrong, and the correction is worth
# recording. The page was not scrimming an invisible picture - measured
# on that entry in a real window, `_backdrop` was **None** nine seconds
# after open: there was no picture at all, because the art chain is a
# network chain and it delivered nothing. The fix is
# `_seed_backdrop_from_cover`, which puts the entry's own cover up at
# once from disk; the scrim was never the problem.
#
# So these stops stay close to the originals (200/170/232), lifted only
# a little, because now there is reliably something behind them: the
# cover has to read as a *ground*, not as a picture the page is sitting
# on top of. Tried first at 168/132/200 with a 14x blur and photographed:
# the result was a face filling the window, which is worse than the flat
# black it replaced.
SCRIM = ((0.0, 14, 12, 9, 196), (0.45, 14, 12, 9, 172), (1.0, 14, 12, 9, 224))

CHAPTER_LIST_TIMEOUT = 45.0

# How long the panel may go on claiming to be loading before it says
# something else. **Measured 22 August 2026**: the chapter list for the
# owner's Swordmaster's Youngest Son normally answers in 1.5s (cached)
# to 5.1s (a live refresh of all seven paginated pages), but one run in
# this harness sat with *nothing* at 220s - the worker had not returned
# at all, so neither the 45s deadline above nor the page's one silent
# retry had produced a word. The panel said "Loading..." the entire
# time, which is the failure CLAUDE.md rule 7 names: a surface someone
# opened is finished, or is showing what it has, or says why not.
#
# This does not cancel anything - a lookup still running is still the
# best answer available and may land a second later. It only stops the
# panel from lying about it, and re-arms the refresh button so there is
# a way out. Set past the worker's own 45s budget so a slow-but-working
# lookup is never talked over.
LIST_QUIET_MS = 52_000

# U+200E LEFT-TO-RIGHT MARK, built with chr() so no invisible character
# sits in this file waiting for a re-encoding tool to mangle it (the
# same reason the Fluent glyphs are escapes). Prefixed onto every list
# row title: an Arabic name otherwise flips the whole paragraph RTL and
# the clipped end is the *start* - the chapter number.
_LTR_MARK = chr(0x200E)

# The " . " a row's meta line is joined with - a real middle dot, built
# with chr() for the same reason as above.
SEPARATOR = " " + chr(0x00B7) + " "

# Fluent glyphs, as escapes on purpose (see reader.py - a re-encoding
# tool turns the bare characters into mojibake, and it has happened).
ICON_BACK = "\ue76b"                  # ChevronLeft - the sidebar's own
                                      # fold glyph, which the player's
                                      # exit and the reader's leave now
                                      # carry too, so one shape means
                                      # "back" everywhere (owner's ask)
ICON_FULLSCREEN = "\ue740"
ICON_EXIT_FULLSCREEN = "\ue73f"
# The list panel's re-ask button - the same Refresh glyph the reader's
# top bar carries, so "look again" is one shape across the app.
ICON_REFRESH = "\ue72c"
ICON_PLAY_GLYPH = "\ue768"
# The fold carets on the resolution headings. Fluent glyphs rather than
# the bare triangle characters they replace: those are drawn by whatever
# fallback face happens to carry them, so they sat a hair off the
# baseline at a different weight from every other icon in the app.
ICON_CHEVRON_DOWN = "\ue70d"
ICON_CHEVRON_RIGHT = "\ue76c"


class _Signals(QObject):
    meta = Signal(int, object)          # run, Cinemeta meta dict or None
    art = Signal(int, str, str)         # run, "logo"/"backdrop", local path
    chapters = Signal(int, object)      # run, chapter list or None
    # run, stream list, (season, ep), still-looking. The last flag is
    # what separates "here is more" from "that is all there is": a
    # partial batch holding nothing playable must leave the note saying
    # "Finding sources...", while the final one saying the same thing
    # means no source was found.
    sources = Signal(int, object, object, bool)
    # A reading title's MangaDex genre tags - run, [names]. Video pages
    # get genres free with the Cinemeta meta; this is the reading pair.
    reading_genres = Signal(int, object)
    # One episode's still, decoded and ready to draw - row key, the
    # local path (the blurred copy when that setting is on). Keyed by
    # row rather than by episode because the list is rebuilt on every
    # search keystroke and the widget a late image belongs to may no
    # longer exist. See DetailsPage._on_episode_still.
    episode_still = Signal(int, str)
    # Resolving a Discover reading title on a picked site - run, the
    # fields to store ({url, site_id}) or None, the site's name.
    site_resolved = Signal(int, object, str)
    # An entry that arrived with no id, matched against the catalog
    # by title - run, the imdb id ("" = nothing matched well
    # enough). See DetailsPage._on_resolved_id.
    resolved_id = Signal(int, str)
    # A season's episode ratings from TMDB, when Cinemeta had none -
    # run, (imdb_id, season) key, {episode_number: score}. See
    # DetailsPage._on_episode_ratings.
    episode_ratings = Signal(int, object, object)
    # A just-saved entry's downloaded cover: entry id, data file, the
    # cover URL it came from ("" when the entry already knew it), local
    # path. Crossed back so the storage write happens on the UI thread
    # with every other writer, not from the pool.
    saved_cover = Signal(str, str, str, str)


# Cinemeta meta is kept on disk per title so the episode list can be drawn
# from it at once and refreshed behind. Measured 23 August 2026: a warm
# fetch is 82-91ms for a 130-150 episode series and **2.67s** for One
# Piece's 1.39MB record on a cold connection, and nothing cached it - so
# every open of a video details page paid that before a single row
# existed. A day is generous for a schedule; a new episode's row is a
# refresh away, and the refresh is emitted only when the list changed.
META_CACHE_TTL_S = 24 * 3600.0


def _meta_cache_name(imdb_id, content_type) -> str:
    safe = re.sub(r"[^a-z0-9]", "", str(imdb_id or "").lower())
    return f"meta-{content_type}-{safe}.json"


def _cached_meta(imdb_id, content_type, any_age=False):
    """The disk-cached Cinemeta meta for a title, or None.

    Read on the UI thread by _start_lookups before any worker exists,
    and that is the point: measured 2 September 2026 on the owner's own
    Reacher and Attack on Titan, the *cached* meta went worker -> queued
    signal -> slot in 116-515ms (the first page of a session pays the
    high end), while storage.load of the same files is 0.2-7ms warm and
    15.8ms for the largest meta on disk (One Piece, 1.98MB). A list
    that is already on disk is drawn in the same frame the page opens
    (CLAUDE.md rule 7), and the worker only ever has the live answer to
    add.

    `any_age` ignores META_CACHE_TTL_S: the copy on disk however old it
    is, for a page that has to show something now and refresh behind.
    Measured 3 September 2026 on the owner's own data: Reacher's and
    Attack on Titan's metas were both **42-43 hours** old, so the 24h
    read above answered None for every title he had opened yesterday
    and the page waited on Cinemeta - **1.54-2.60s** to the first row
    on the session's first page (a cold connection), 0.15s on the next.
    Drawn stale and refreshed behind: **0.036-0.074s** to the first
    row, same list, and _meta_worker redraws only if Cinemeta's differs
    (it did not, either title - no second draw)."""
    name = _meta_cache_name(imdb_id, content_type)
    try:
        stored = storage.load(name, None)
        if (isinstance(stored, dict) and isinstance(stored.get("meta"), dict)
                and (any_age or time.time() - float(stored.get("ts") or 0)
                     < META_CACHE_TTL_S)):
            return stored["meta"]
    except Exception:
        pass
    return None


def _meta_worker(signals, run, imdb_id, content_type, drawn=False):
    """Never raises - lookup_pool workers die silently (see that module).

    Emits the cached meta first when there is one, then the live answer
    only if its episode list differs - so a cached title draws its rows
    immediately and a second emit never rebuilds identical rows.
    `drawn` says the page already drew the disk copy itself - of any
    age, see _cached_meta - so only the live answer is emitted, and
    still only when it differs from what is showing. A failed lookup
    then emits nothing: the stale list stays up rather than being
    replaced by "couldn't be loaded", and the refresh button is the way
    to ask again."""
    name = _meta_cache_name(imdb_id, content_type)
    cached = _cached_meta(imdb_id, content_type, any_age=bool(drawn))
    if cached is not None and not drawn:
        signals.meta.emit(run, cached)
    try:
        meta = stremio.fetch_meta(imdb_id, content_type) if stremio else None
    except Exception:
        logs.exception("details meta lookup failed")
        meta = None
    if meta is None:
        if cached is None:
            signals.meta.emit(run, None)
        return
    try:
        storage.save(name, {"ts": time.time(), "meta": meta})
    except Exception:
        pass
    if cached is not None and (cached.get("videos") == meta.get("videos")):
        return          # nothing the rows would show has changed
    signals.meta.emit(run, meta)


def _episode_ratings_worker(signals, run, imdb_id, season, videos):
    """TMDB's per-episode ratings for one season, off the UI thread.

    Never raises - lookup_pool workers die silently. Cinemeta carries a
    real per-episode rating for some series and the string "0" for every
    episode of others (Attack on Titan, House of the Dragon), and TMDB
    is the second source for exactly those. Cached on disk per series by
    ratings.py, so a season already fetched costs no request."""
    mapping = {}
    try:
        from helpers import ratings
        mapping = ratings.episode_ratings(imdb_id, season, videos) or {}
    except Exception:
        logs.exception("details TMDB ratings lookup failed")
        mapping = {}
    signals.episode_ratings.emit(run, (imdb_id, season), mapping)


class _BrowserLinkBridge(QObject):
    """Worker -> Qt for the download dialog's "Download in Browser": the
    direct link found (or None) and the request it answers. Parented to
    the page, so a link resolving after the page closed lands on nothing
    rather than on a deleted widget."""
    resolved = Signal(object, object)   # {"url", "name", "size"} | None, request


def _browser_link_worker(bridge, request):
    """Never raises - lookup_pool workers die silently (see that module),
    and a dead one here would leave the sticky toast up forever."""
    result = None
    try:
        from helpers import downloads
        result = downloads.browser_url_for(
            request["entry"], season=request["season"],
            episode=request["episode"], audio=request["audio"])
    except Exception:
        logs.exception("details direct link lookup failed")
    try:
        bridge.resolved.emit(result, request)
    except RuntimeError:
        pass                # the page, and the bridge with it, is gone


def _resolve_id_worker(signals, run, title, entry_type):
    """Find the catalog id for a title that arrived without one.

    Never raises - lookup_pool workers die silently (see that module).

    Matched, not merely searched. An episode list belonging to some
    *other* show is worse than no episode list, because nothing on the
    page would say it was the wrong show - so a candidate has to clear
    the same 0.8 similarity bar the rest of this app uses, and anything
    below it comes back empty and gets the honest message."""
    found = ""
    try:
        from helpers import discover, title_match
        wanted = (title or "").strip()
        kinds = (("movie",) if entry_type == "Movie" else ("anime", "series"))
        for kind in kinds:
            rows = discover.discover_video(kind, query=wanted, limit=6) or []
            best, score = None, 0.0
            for row in rows:
                value = title_match.similarity(wanted, row.get("title") or "")
                if value > score:
                    best, score = row, value
            if best is not None and score >= 0.8 and best.get("imdb_id"):
                found = best["imdb_id"]
                break
    except Exception:
        logs.exception("details id lookup failed")
        found = ""
    signals.resolved_id.emit(run, found)


def _art_worker(signals, run, entry):
    """Backdrop first - it is the whole page's ground.

    The small w780 copy goes up the moment it lands (a few hundred KB)
    and the full-resolution original replaces it when it arrives - the
    owner reported the ground "taking a while", and most of that while
    was a multi-MB original on a first visit. Runs on its own thread,
    not lookup_pool: that pool is four wide and shared with the
    tracker's page-load backfill, so this page's own artwork could sit
    in a queue behind fifty schedule lookups.

    **The logo runs beside the backdrops rather than behind them** -
    artwork.deliver, which carries the measurement and is what every
    page uses now. Reproduced for this page on the frozen build against
    a copy of his data with logo_cache deleted: it opened on the blurred
    cover seed with its typed title, and the logo and the sharp ground
    appeared together seconds later."""
    artwork.deliver(
        entry,
        on_backdrop=lambda path: signals.art.emit(run, "backdrop", path),
        on_logo=lambda path: path and signals.art.emit(run, "logo", path))


def _blurred_still(path):
    """The blurred copy of a downloaded still, written once into the
    image cache and returned as a path - or the sharp original if it
    cannot be made.

    Blurred at STILL_SIZE, not at the 780px original: the tile is what
    ends up on screen, so a small blur here is the same smear a huge
    radius on the original would give after downscaling, for a fraction
    of the work (measured 1.5ms per tile against 34ms on the original).
    Written .part-then-replace for the reason images.download is - an
    interrupted write otherwise leaves a truncated file that every later
    run hands straight back."""
    from PIL import Image, ImageFilter, ImageOps
    source = Path(path)
    target = (images.CACHE_DIR
              / f"{source.stem}-blur{STILL_SIZE[0]}x{STILL_SIZE[1]}.png")
    if target.is_file():
        return target
    try:
        with Image.open(source) as opened:
            tile = ImageOps.fit(opened.convert("RGBA"), STILL_SIZE,
                                Image.LANCZOS)
        tile = tile.filter(ImageFilter.GaussianBlur(STILL_BLUR_RADIUS))
        temporary = target.with_suffix(".png.part")
        tile.save(temporary, "PNG")
        temporary.replace(target)
        return target
    except Exception:
        try:
            target.with_suffix(".png.part").unlink(missing_ok=True)
        except OSError:
            pass
        return source


def _still_worker(signals, key, url, blur, alive=None):
    """One episode's still: download it, blur it if that is on, and
    **decode it here rather than in the slot**.

    The decode is the split images.prewarm exists for, and skipping it
    is not free: 40 poster decodes done in the slot instead were
    measured at 337ms of UI-thread time in the Discover rows on 22
    August 2026, which starved a page transition to a single frame.
    Warming _fitted here leaves the slot with only the ~0.1ms QPixmap
    conversion.

    `alive` answers whether the page that asked is still open: a still
    for a page the user has already left is a download nobody will see,
    and on the cover queue it would sit ahead of the page they are
    looking at now. Skipped before the first byte, not after.

    Never raises - a lookup_pool worker dies silently otherwise, taking
    every still still queued behind it with it."""
    try:
        if alive is not None and not alive():
            return
        path = images.download(url)
        if not path:
            # A refusing host (net.host_refusing - two failures, ten
            # minutes) is skipped by download without a request, so
            # the row would keep its play tile for the life of the
            # page. An empty path tells the page to ask again later
            # (_on_episode_still); anything else - a 404, a file that
            # is not an image - is final and the row keeps its tile.
            if net.host_refusing(url):
                signals.episode_still.emit(key, "")
            return
        if blur:
            path = _blurred_still(path)
        # A file that will not decode is not a still - say nothing and
        # let the row keep the placeholder rather than draw a hole.
        if images.warm(str(path), STILL_SIZE) is None:
            return
    except Exception:
        return
    if len(_STILL_READY) >= _STILL_READY_MAX:
        for old in list(_STILL_READY)[:_STILL_READY_MAX // 4]:
            _STILL_READY.pop(old, None)
    _STILL_READY[(url, bool(blur))] = str(path)
    signals.episode_still.emit(key, str(path))


def _sources_worker(signals, run, entry, season, episode):
    """Everything playable for one episode - what the source picker
    lists. Its own thread (the user is watching this one), never raises.

    Each source's releases go up as they land rather than the whole
    fan-out at the end - the owner's "the sourcing is still slow".
    Measured on their own entries: the first addon's rows are in hand
    after 0.3-0.8s and this panel used to show nothing until the slowest
    source finished, 1.9-2.6s later and up to the whole budget when one
    hung. Every batch is a superset of the last, so the panel just
    refills - the same shape _chapters_worker above already uses."""
    def partial(found):
        signals.sources.emit(run, list(found or []), (season, episode), True)

    try:
        from helpers import streams as streams_module
        found = streams_module.find_streams(
            entry, season=season, episode=episode,
            deadline=net.deadline_in(24), on_partial=partial)
    except Exception:
        logs.exception("details source lookup failed")
        found = []
    signals.sources.emit(run, list(found or []), (season, episode), False)


def _reading_logo_worker(signals, run, entry):
    """The anime's TMDB title treatment for a reading entry, by title -
    see artwork.logo_path_by_title for the strict match that keeps
    "Animal Kingdom" off "Kingdom". Emits only when one exists."""
    try:
        path = artwork.logo_path_by_title(entry.get("title") or "")
    except Exception:
        path = None
    if path:
        signals.art.emit(run, "logo", str(path))


def _reading_genres_worker(signals, run, title):
    """MangaDex's genre tags for this title, for the genre buttons.
    Never raises - the pool's worker thread dies silently otherwise."""
    try:
        from helpers import discover
        names = discover.reading_genres(title)
    except Exception:
        names = []
    signals.reading_genres.emit(run, list(names or []))


def _site_resolve_worker(signals, run, site, title):
    """Find `title` on one picked reading site and hand back the fields
    that bind the entry to it. Best title-matched result wins; a site
    that answers nothing usable reports None so the page can say so and
    re-offer the list. Never raises."""
    fields, site_name = None, str(site.get("name") or "the site")
    try:
        from helpers import manga_sites, title_match
        results = manga_sites.search_site(site, title,
                                          deadline=net.deadline_in(12))
        best, best_score = None, 0.0
        for row in results or []:
            score = title_match.similarity(title, row.get("title") or "")
            if score > best_score:
                best, best_score = row, score
        # 0.5, not the schedule lookups' 0.85: the user picked this site
        # by hand and is about to see the chapter list it produces, so a
        # looser match is checkable in a way a silent background lookup
        # never is - and these sites localize titles enough that 0.85
        # would refuse real hits.
        if best is not None and best_score >= 0.5 and best.get("url"):
            fields = {"url": best["url"], "site_id": site.get("id")}
            if best.get("cover_url"):
                fields["cover_url"] = best["cover_url"]
    except Exception:
        fields = None
    signals.site_resolved.emit(run, fields, site_name)


def _chapters_worker(signals, run, entry, refresh=False, drawn=None,
                     alive=None):
    """`refresh` skips chapter_source's six-hour disk cache - what the
    panel's refresh button passes, and the only way a title that
    published an hour ago shows its new chapter today.

    The first page is drawn as soon as it parses rather than after the
    whole paginated list is in: a full list is up to twenty-four more
    fetches (21.7s measured on olympustaff), and the newest chapters -
    the ones on page one - are what someone opening a series is nearly
    always after. Every emission is a superset of the last, so the panel
    just refills.

    `drawn` is the list the page already put on screen from the stale
    disk cache (see DetailsPage._start_lookups). While one is up, this
    emits only a *different* complete answer: a first-page partial is
    shorter than what is showing and would shrink the list under the
    pointer, an identical answer would rebuild identical rows, and a
    failed or empty lookup must not wipe a list that is there - the
    refresh button stays the way to ask again.

    `alive` (DetailsPage._still_wanted) is asked once, before the first
    request: a listing for a page that has been left is 2-22s of site
    pages nobody will see, on one of the two watched workers."""
    if alive is not None and not alive():
        return
    def partial(found):
        if drawn is None:
            signals.chapters.emit(run, list(found or []))

    # **The one line that says what this did.** The owner, 4 September
    # 2026, with a screenshot of a chapter panel reading "Loading...":
    # "why is it showing no chapters???" - and nothing in his log to
    # answer it, because this path had never written anything. Host,
    # milliseconds and count, so the next report arrives with its cause
    # attached; never the URL, which can carry a token.
    started = time.perf_counter()
    host = ""
    try:
        from urllib.parse import urlsplit
        host = urlsplit(str(entry.get("url") or "")).netloc
    except Exception:
        host = ""
    try:
        chapters = chapter_source.list_chapters(
            entry, deadline=net.deadline_in(CHAPTER_LIST_TIMEOUT),
            refresh=refresh, on_partial=partial)
        logs.info(f"chapter list: host={host or '-'} "
                  f"ms={int((time.perf_counter() - started) * 1000)} "
                  f"found={len(chapters or [])} refresh={bool(refresh)}")
    except Exception:
        logs.info(f"chapter list: host={host or '-'} "
                  f"ms={int((time.perf_counter() - started) * 1000)} failed")
        logs.exception("details chapter listing failed")
        if drawn is not None:
            return
        # None, not []: an exception here is "the lookup failed", and
        # collapsing it into an empty list made a dead connection read
        # as "this title has no chapters" - with no retry, since the
        # page believed it had its answer. _on_chapters retries a None
        # once and only then says so.
        signals.chapters.emit(run, None)
        return
    chapters = list(chapters or [])
    if drawn is not None and (not chapters or chapters == drawn):
        return
    signals.chapters.emit(run, chapters)


# What a release *is*, read off its own name: the codec, the bit depth,
# and the dynamic range. Ordered most-distinguishing first, and each entry
# is (what to print, the patterns that mean it). Word-boundary matched so
# "H264" in a group's name cannot masquerade as a codec claim - and read
# from the release title only, which is the one string every provider
# returns in the same shape.
_CODEC_TAGS = (
    ("HDR10+", (r"hdr10\+", r"hdr10plus")),
    ("Dolby Vision", (r"dolby[\s._-]?vision", r"\bdv\b", r"\bdovi\b")),
    ("HDR", (r"\bhdr\b",)),
)
_VIDEO_CODECS = (
    ("AV1", (r"\bav1\b",)),
    ("HEVC", (r"\bhevc\b", r"\bx265\b", r"\bh\.?265\b")),
    ("H.264", (r"\bx264\b", r"\bh\.?264\b", r"\bavc\b")),
)
# "Hi10"/"Hi10P" is how fansub groups write 10-bit and it is common on
# exactly the releases this app reaches (measured on the owner's Demon
# Slayer list: [SCY] and [LostYears] both use it and neither says "10bit").
_DEPTH_TAGS = (("10-bit", (r"10[\s._-]?bits?\b", r"\bhi10p?\b")),)


def _codec_label(source, quality, meta_text) -> str:
    """The line a source row leads with: what the file is, not who
    listed it.

    Built from the release name because that is where this information
    actually lives - no provider returns a codec field. Falls back to the
    resolution, and only then to a neutral word, so the row is never
    blank: a release whose name says nothing is still a real choice and
    has to be labelled something."""
    text = f"{meta_text or ''} {source or ''}"
    bits = []
    for label, patterns in _VIDEO_CODECS:
        if any(re.search(p, text, re.I) for p in patterns):
            bits.append(label)
            break
    for group in (_CODEC_TAGS, _DEPTH_TAGS):
        for label, patterns in group:
            if any(re.search(p, text, re.I) for p in patterns):
                bits.append(label)
                break
    if bits:
        return "  ·  ".join(bits)
    return _quality_label(quality) if quality else "Release"


def _quality_label(value) -> str:
    """A stream's resolution as one word, for a source row.

    "4k" and "2160p" are the same thing said two ways, and in a list
    that no longer groups by resolution they would read as two different
    ones sitting apart in the same column."""
    quality = str(value or "").strip().lower()
    return "2160p" if quality == "4k" else quality


def _year_text(value) -> str:
    """A release year as it should read, or "".

    Cinemeta writes an unfinished run as "2022-" with a trailing dash
    (an en dash, U+2013) and a finished one as "2020-2023". The dash
    means "and still going", but on a card with nothing after it, it
    reads as a truncation - the owner asked for it gone. A real range
    keeps its dash; only a dangling one is dropped."""
    text = str(value or "").strip()
    while text and text[-1] in "-\u2012\u2013\u2014\u2015 ":
        text = text[:-1].strip()
    return text


def _episode_rating(value, source="IMDb") -> str:
    """An episode's rating as "IMDb 8.5" / "TMDB 8.1", or "" when there
    is none.

    `source` is named because the number is not always IMDb's: Cinemeta
    ships IMDb ratings (and the string "0" for unrated), but where it has
    none TMDB is the fallback and its scale differs - so a TMDB number
    labelled "IMDb" would be a quiet lie (see DetailsPage._season_ratings).
    A zero or an unparseable value shows nothing: a rating is only worth
    printing when it is a real one."""
    try:
        score = float(str(value).strip())
    except (TypeError, ValueError):
        return ""
    if score <= 0:
        return ""
    return f"{source} {score:g}"


def _pretty_date(value) -> str:
    """`Jul 25, 2026` out of an ISO stamp, or the raw text if it will not
    parse - a wrong-looking date is more honest than a hidden one."""
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        stamp = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
        return stamp.strftime("%b %d, %Y").replace(" 0", " ")
    except ValueError:
        return text[:10]


def _aired(value):
    try:
        return datetime.datetime.fromisoformat(
            str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None


def _chip(text, accent=False) -> QLabel:
    label = QLabel(text)
    label.setStyleSheet(
        f"color: {theme.ON_ACCENT if accent else theme.TEXT};"
        f" background: {theme.ACCENT_GRADIENT if accent else theme.SURFACE_HOVER};"
        f" border: 1px solid {theme.ACCENT if accent else theme.BORDER};"
        f" border-radius: {theme.RADIUS}px; padding: 5px 14px;"
        f" font-weight: 600; font-size: 10.5pt;")
    return label


def _chip_button(text) -> QPushButton:
    """A chip that can be pressed - the genre buttons (the owner's ask:
    the genres open everything else filed under them). Deliberately the
    _chip look at rest, so the facts row stays one design; hover borrows
    the accent border every other clickable thing here answers with."""
    button = QPushButton(text)
    use_hover_cursor(button)
    button.setStyleSheet(
        f"QPushButton {{ color: {theme.TEXT}; background: {theme.SURFACE_HOVER};"
        f" border: 1px solid {theme.BORDER};"
        f" border-radius: {theme.RADIUS}px; padding: 5px 14px;"
        f" font-weight: 600; font-size: 10.5pt; }}"
        f"QPushButton:hover {{ border: 1px solid {theme.ACCENT};"
        f" color: {theme.ACCENT}; }}")
    button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    use_hover_cursor(button)
    return button


def _badge(text, kind) -> QLabel:
    """DONE in the accent, UPCOMING in the success green - the two
    states the owner's reference picture colours differently. One word
    for finished across both media (the owner's ask), where this used to
    say WATCHED on episodes and READ on chapters."""
    # ON_ACCENT on both fills: it computes 10.5:1 on SUCCESS (25 August
    # 2026) - this held a hand-typed near-black green before, which the
    # navy re-theme would have stranded.
    colours = {"watched": (theme.ON_ACCENT, theme.ACCENT_GRADIENT, theme.ACCENT),
               "upcoming": (theme.ON_ACCENT, theme.SUCCESS, theme.SUCCESS)}
    fg, bg, border = colours[kind]
    label = QLabel(text)
    label.setStyleSheet(
        f"color: {fg}; background: {bg}; border: 1px solid {border};"
        f" border-radius: {theme.RADIUS_SM}px; padding: 2px 10px;"
        f" font-weight: 700; font-size: 8.5pt;")
    return label


def _pill(text, accent=False) -> QLabel:
    """A small count/figure chip - what a resolution heading carries on
    its right instead of a second line of prose, and what a release row
    carries for its seeders.

    This is the other half of telling the two apart (see _group_card):
    a heading is one line with figures pushed to its right edge, a row
    is two lines of text reading left to right. `accent` is for the one
    number that decides the choice - the seeders on a *heading*.

    No border, and translucent fills rather than flat ones: the first
    cut gave every pill a solid fill and a 1px ring, which rendered as a
    row of small buttons and put a gold chip on every release row as
    well as on every heading - so the headings stopped standing out at
    all, which was the whole point of them. Translucent also means one
    pill works on both grounds a heading has (its dark resting fill and
    the accent-tinted open one)."""
    label = QLabel(text)
    label.setStyleSheet(
        f"color: {theme.ACCENT if accent else theme.TEXT_MUTED};"
        f" background: {theme.rgba(theme.ACCENT, 38) if accent else theme.rgba(theme.TEXT_MUTED, 26)};"
        f" border: none;"
        f" border-radius: {theme.RADIUS_SM}px; padding: 3px 9px;"
        f" font-weight: 700; font-size: 8.5pt;")
    return label


def _glyph_tile(glyph, size, radius, point_size=12.0, accent=True) -> QLabel:
    """A rounded tile carrying one Fluent glyph - the caret on a
    resolution heading, and the placeholder an episode row shows until
    its still lands (or forever, when there is none).

    The placeholder is the same shape and size as the still on purpose:
    a row that never gets an image keeps its rhythm with the rows that
    did, instead of leaving a hole where the art should be."""
    label = QLabel(glyph)
    label.setFixedSize(*size)
    label.setAlignment(Qt.AlignmentFlag.AlignCenter)
    label.setStyleSheet(
        f"color: {theme.ACCENT if accent else theme.TEXT_MUTED};"
        f" background: {theme.ACCENT_SOFT if accent else theme.SURFACE_HOVER};"
        f" border: none; border-radius: {radius}px;"
        f" font-family: {theme.FONT_STACK_ICONS};"
        f" font-size: {point_size}pt;")
    return label


class _GroupBody(QWidget):
    """One resolution's releases under their heading, folded by height.

    Built on the first open, never rebuilt on a fold: forty-one cards
    made once is a cost paid once. The fold animates maximumHeight over
    GROUP_FOLD_MS on the sidebar's own curve (widgets.ease_out_cubic is
    what OutCubic is) and says in the log what it cost - the build in
    milliseconds and the frames the animation actually got, which on a
    165Hz panel should read near 165 x GROUP_FOLD_MS / 1000."""

    def __init__(self, quality, streams, page):
        super().__init__()
        self.quality = quality
        self._streams = list(streams)
        self._page = page
        self._built = False
        self._tween = None
        self._frames = 0
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(self._page._rows.spacing()
                                if hasattr(self._page, "_rows") else 6)

    def build(self):
        if self._built:
            return 0.0
        started = time.perf_counter()
        try:
            from helpers import streams as streams_helper
        except Exception:                               # pragma: no cover
            streams_helper = None
        for stream in self._streams:
            seeders = int(stream.get("seeders") or 0)
            size = ""
            if streams_helper:
                size = streams_helper.format_size(stream.get("size_bytes"))
            detail = SEPARATOR.join(
                p for p in (size, (stream.get("title") or "").strip()[:70])
                if p)
            self._layout.addWidget(self._page._source_card(
                stream.get("source") or "Source",
                _quality_label(stream.get("quality")), seeders, detail,
                lambda s=stream: self._page._play_stream_choice(s)))
        self._built = True
        return (time.perf_counter() - started) * 1000.0

    def fold(self, opening):
        """Open or close, animated at the screen's rate.

        **Measured on the frozen build, 6 September 2026, Reacher's 1080P
        group of forty-one releases**, with the first version of this
        (a QPropertyAnimation started straight after build()):

            opened  built=37ms  frames=0   over 260ms
            closed  built=0ms   frames=13  over 225ms
            opened  built=0ms   frames=14  over 226ms

        Zero frames on the first open: the forty-one new cards are
        polished and laid out by the event loop *after* build() returns,
        on the UI thread, and the animation's clock ran out underneath
        that work - the group simply appeared, which is the jump he
        recorded. So the first animation now starts one event-loop turn
        later, once the polish has happened. And 13-14 frames over 220ms
        is Qt's own 60Hz animation tick; the sidebar's fold runs on
        widgets.SmoothTween at the panel's refresh instead, so this does
        too, and the two motions read as one."""
        from helpers.widgets import SmoothTween
        built_ms = self.build() if opening else 0.0
        if self._tween is not None:
            self._tween.stop()
        start = self.maximumHeight() if self.maximumHeight() < QWIDGETSIZE_MAX else self.height()
        end = self._layout.sizeHint().height() if opening else 0
        self._frames = 0
        began = [0.0]

        def apply(value):
            self._frames += 1
            self.setMaximumHeight(max(0, int(round(value))))

        def done():
            if opening:
                self.setMaximumHeight(QWIDGETSIZE_MAX)
            try:
                logs.info(f"source group {self.quality or 'other'}: "
                          f"{'opened' if opening else 'closed'} rows="
                          f"{len(self._streams)} built={built_ms:.0f}ms "
                          f"frames={self._frames} over "
                          f"{(time.perf_counter() - began[0]) * 1000:.0f}ms")
            except Exception:
                pass

        tween = SmoothTween(self, apply, GROUP_FOLD_MS, on_done=done)
        self._tween = tween

        def go():
            if self._tween is not tween:
                return          # re-aimed before it started
            began[0] = time.perf_counter()
            tween.start(start, end)

        if built_ms:
            # A fresh body: let the event loop polish the new cards
            # before a single frame is owed. Measured above.
            self.setMaximumHeight(0)
            QTimer.singleShot(0, go)
        else:
            go()


# One resolution group's unfold, in milliseconds - the sidebar's own
# 220ms, so the two motions in the app read as one.
GROUP_FOLD_MS = 220
QWIDGETSIZE_MAX = 16777215


class _PanelNote(QLabel):
    """The status note centred over the list panel's viewport.

    It is a viewport child placed by hand (a layout row would be
    destroyed by every refill - see _build_panel), and the by-hand
    placement used to hold two bugs the owner saw as one clipped,
    off-centre message: the geometry was computed in the page's
    resizeEvent from the viewport's size *at that moment*, which is
    stale (the splitter's layout settles after the page's own resize),
    and nothing re-ran the layout when the text changed, so a long
    error kept a short predecessor's box. Now the note watches the
    viewport's own Resize - never stale - and re-lays itself on every
    setText."""

    def __init__(self, text, viewport):
        super().__init__(text, viewport)
        self._viewport = viewport
        self.setWordWrap(True)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        # The size is set programmatically rather than in the
        # stylesheet: the wrapped height below comes from fontMetrics(),
        # which does not see a QSS font-size.
        font = self.font()
        font.setPointSizeF(11.5)
        self.setFont(font)
        self.setStyleSheet(f"color: {theme.TEXT_MUTED};"
                           f" background: transparent; border: none;")
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        viewport.installEventFilter(self)
        self.relayout()

    def eventFilter(self, obj, event):
        if obj is self._viewport and event.type() in (QEvent.Type.Resize,
                                                      QEvent.Type.Show):
            self.relayout()
        return False

    def setText(self, text):
        super().setText(text)
        self.relayout()

    def relayout(self):
        try:
            width = max(80, self._viewport.width() - 40)
            # Ints, not the enums: TextFlag and AlignmentFlag are
            # different enum types in PyQt6 and cannot be |'d directly.
            flags = (int(Qt.TextFlag.TextWordWrap.value)
                     | int(Qt.AlignmentFlag.AlignHCenter.value))
            wrapped = self.fontMetrics().boundingRect(
                QRect(0, 0, width, 0), flags, self.text())
            height = max(wrapped.height(), self.fontMetrics().height())
            self.setGeometry(20,
                             max(0, (self._viewport.height() - height) // 2),
                             width, height)
            self.raise_()
        except RuntimeError:
            pass        # the panel is being torn down


# PickCombo moved to helpers/widgets.py - the player's download panel
# has the same two-click symptom on its own drop-downs, and a window
# module importing another window module is how import cycles start.
# Imported above; every `PickCombo(...)` here still resolves.


class DetailsPage(GlassPage):
    """One entry, full screen: facts on the left, the episode or chapter
    list on the right."""

    closed = Signal()

    def __init__(self, entry, parent=None):
        super().__init__(parent=parent)
        self.entry = dict(entry or {})
        self._run = 0
        self._closed = False
        # Whether this open has already warmed its episode's sources -
        # see _prefetch_sources. One fan-out per open, never per relayout.
        self._prefetch_started = False
        self._backdrop = None
        self._backdrop_scaled = None      # see paintEvent's size cache
        self._backdrop_size = None
        # Whether _backdrop is a hero_art ground - already blurred, so it
        # is expanded to cover the page rather than letterboxed across the
        # top like a sharp 1900x400 AniList banner would be. Upscaling a
        # blur costs it nothing; letterboxing one leaves two thirds of the
        # page flat black, which is what the owner was looking at.
        self._backdrop_is_ground = False
        self._meta = None
        self._videos = []
        # TMDB ratings for the season on screen, when Cinemeta had none.
        # Keyed (imdb_id, season) so flipping seasons re-asks, and the
        # seasons already fetched are remembered so a flip back is free.
        self._tmdb_ratings = {}
        self._tmdb_ratings_key = None
        self._tmdb_ratings_asked = set()
        self._chapters = []
        self._season = 0
        self._is_reading = self.entry.get("type") in MANGA_TYPES
        # One-shot latches for the silent retry each list lookup gets
        # (see _on_meta/_on_chapters); re-armed by every fresh start.
        self._meta_retried = False
        self._chapters_retried = False

        # The source-picker step: filled in when an episode row is
        # clicked, cleared by its back row or by picking a source.
        # {"season", "episode", "streams" (None until the first batch
        # lands), "looking" (more sources still out)}.
        self._source_pick = None
        # Which resolution groups are open in the source list. Empty on
        # purpose: every group starts folded (see _fill_source_rows).
        self._open_source_groups = set()
        # A Discover reading title with no site yet: the list panel
        # offers the configured reading sites first (the owner's ask),
        # and this stays True until one is picked and answers.
        self._site_choice_pending = False
        # Per-episode/chapter ticks from helpers/history - what lets an
        # *unsaved* title be marked watched at all, and what lets a
        # saved one be ticked out of order (its progress is one number,
        # which cannot say "5 and 7 but not 6"). Read once here and kept
        # in step by _mark_history.
        self._history_marks = history.watched_keys(self.entry)

        # Episode-still tiles waiting on their image, by row key. Not by
        # episode number: a search keystroke rebuilds every row, so the
        # widget a download was started for may already be gone.
        self._still_tiles = {}
        self._still_key = 0
        # Stills asked for during one _materialise_rows batch, submitted
        # together at its end - see _queue_still for the order.
        self._still_batch = None
        # What each pending still was asked for, so a refused one can be
        # asked again - see _on_episode_still.
        self._still_asks = {}
        self._still_refused = set()
        self._still_retry_timer = QTimer(self)
        self._still_retry_timer.setSingleShot(True)
        self._still_retry_timer.timeout.connect(self._retry_refused_stills)
        # Settings > Watching, read once per list fill (see _still_tile).
        self._blur_stills = app_settings.get_blur_episode_stills()

        self._signals = _Signals()
        # After _signals: the ground may have to be composed on a
        # worker and lands back through the `art` signal.
        self._seed_backdrop_from_cover()
        self._signals.meta.connect(self._on_meta)
        self._signals.art.connect(self._on_art)
        self._signals.chapters.connect(self._on_chapters)
        self._signals.sources.connect(self._on_sources)
        self._signals.saved_cover.connect(self._on_saved_cover)
        self._signals.reading_genres.connect(self._on_reading_genres)
        self._signals.episode_still.connect(self._on_episode_still)
        self._signals.site_resolved.connect(self._on_site_resolved)
        self._signals.resolved_id.connect(self._on_resolved_id)
        self._signals.episode_ratings.connect(self._on_episode_ratings)

        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        # Debounced: a chapter list runs to 500 rows and rebuilding it on
        # every keystroke stutters under a fast typist.
        self._search_timer.timeout.connect(self._fill_rows)

        # The panel's promise that it will stop saying "Loading..." even
        # when the lookup never comes back at all - see LIST_QUIET_MS.
        self._quiet_timer = QTimer(self)
        self._quiet_timer.setSingleShot(True)
        self._quiet_timer.timeout.connect(self._on_list_quiet)

        self._build()
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._start_lookups()

    # ---- construction ---------------------------------------------------
    def _build(self):
        root = QHBoxLayout(self)
        root.setContentsMargins(36, 24, 24, 28)
        root.setSpacing(24)

        # Left: identity. Kept on plain transparent widgets so the
        # backdrop shows through - the scrim in paintEvent is what keeps
        # the text readable over it.
        left = QVBoxLayout()
        left.setSpacing(10)

        top_row = QHBoxLayout()
        self._back_btn = self._round_button(ICON_BACK, "Back (Esc)")
        self._back_btn.clicked.connect(self.leave)
        top_row.addWidget(self._back_btn)
        top_row.addStretch(1)
        left.addLayout(top_row)
        left.addStretch(1)

        self._logo = QLabel()
        self._logo.setVisible(False)
        left.addWidget(self._logo)
        self._title_label = QLabel(self.entry.get("title") or "",
                                   objectName="PanelTitle")
        self._title_label.setWordWrap(True)
        self._title_label.setStyleSheet(
            "font-size: 26pt; font-weight: 800; background: transparent;")
        left.addWidget(self._title_label)

        self._facts = QLabel("")
        self._facts.setStyleSheet(
            f"color: {theme.TEXT}; font-size: 12pt; font-weight: 600;"
            f" background: transparent;")
        left.addWidget(self._facts)

        self._genres_head = self._section_label("GENRES")
        left.addWidget(self._genres_head)
        self._genres_row = QHBoxLayout()
        self._genres_row.setSpacing(8)
        self._genres_row.addStretch(1)
        left.addLayout(self._genres_row)

        self._cast_head = self._section_label("CAST")
        left.addWidget(self._cast_head)
        self._cast_row = QHBoxLayout()
        self._cast_row.setSpacing(8)
        self._cast_row.addStretch(1)
        left.addLayout(self._cast_row)

        self._summary_head = self._section_label("SUMMARY")
        left.addWidget(self._summary_head)
        self._summary = QLabel("")
        self._summary.setWordWrap(True)
        self._summary.setMaximumWidth(720)
        self._summary.setStyleSheet(
            f"color: {theme.TEXT}; font-size: 11.5pt; background: transparent;")
        left.addWidget(self._summary)
        for widget in (self._genres_head, self._cast_head, self._summary_head,
                       self._summary):
            widget.setVisible(False)

        left.addSpacing(14)
        verb = "Reading" if self._is_reading else "Watching"
        # Much larger than the app's stock accent button, at the owner's
        # ask - this is the page's one primary action and it was sized
        # like a form's Save.
        self._continue_btn = QPushButton(f"  Continue {verb}", objectName="Accent")
        self._continue_btn.setFixedHeight(58)
        self._continue_btn.setMinimumWidth(280)
        self._continue_btn.setStyleSheet(
            "QPushButton { font-size: 14pt; padding: 10px 30px; }")
        use_hover_cursor(self._continue_btn)
        self._continue_btn.clicked.connect(self._continue)
        continue_row = QHBoxLayout()
        continue_row.addWidget(self._continue_btn)
        continue_row.addStretch(1)
        left.addLayout(continue_row)

        # Directly under Continue: one download button per medium, and it
        # always asks - which episodes (one, a range, or the season) or
        # which chapters, plus the audio wanted for video. No second
        # season button and no assumed target (the owner's ask).
        if self._is_reading:
            label = "  Download Chapters"
        elif self.entry.get("type") == "Movie":
            label = "  Download Film"
        else:
            label = "  Download Episodes"
        self._download_btn = QPushButton(label)
        self._download_btn.setFixedHeight(44)
        self._download_btn.setMinimumWidth(280)
        self._download_btn.setStyleSheet(
            f"QPushButton {{ font-size: 11.5pt; padding: 8px 24px;"
            f" background: {theme.SURFACE_HOVER}; color: {theme.TEXT};"
            f" border: 1px solid {theme.BORDER};"
            f" border-radius: {theme.RADIUS}px; }}"
            f"QPushButton:hover {{ border: 1px solid {theme.ACCENT}; }}")
        use_hover_cursor(self._download_btn)
        self._download_btn.clicked.connect(self._download_current)
        download_row = QHBoxLayout()
        download_row.addWidget(self._download_btn)
        download_row.addStretch(1)
        left.addSpacing(8)
        left.addLayout(download_row)

        left.addStretch(2)

        root.addLayout(left, stretch=1)
        root.addWidget(self._build_panel())

    def _section_label(self, text) -> QLabel:
        label = QLabel(text)
        label.setStyleSheet(
            f"color: {theme.TEXT_MUTED}; font-size: 10pt; font-weight: 700;"
            f" letter-spacing: 1px; background: transparent; padding-top: 8px;")
        return label

    def _round_button(self, glyph, tooltip):
        button = QPushButton(glyph)
        button.setToolTip(tooltip)
        button.setFixedSize(40, 40)
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        # padding: 0 or the app-wide button padding clips the glyph to a
        # sliver (the measured trap reader._glyph_button records).
        button.setStyleSheet(
            f"QPushButton {{ background: {theme.PANEL_FILL}; color: {theme.TEXT};"
            f" border: 1px solid {theme.BORDER}; padding: 0px;"
            f" font-family: {theme.FONT_STACK_ICONS}; font-size: 13pt;"
            f" border-radius: 20px; }}"
            f"QPushButton:hover {{ border: 1px solid {theme.ACCENT};"
            f" background: {theme.SURFACE_HOVER}; }}")
        use_hover_cursor(button)
        return button

    def _build_panel(self) -> QWidget:
        panel = QFrame()
        panel.setFixedWidth(PANEL_WIDTH)
        # theme.rgba over BG_ALT, not a hand-typed rgba: the old literal
        # was a warm near-black the navy re-theme would have stranded as
        # a brown pane over a cool backdrop.
        panel.setStyleSheet(
            f"QFrame {{ background: {theme.rgba(theme.BG_ALT, 210)};"
            f" border: 1px solid {theme.BORDER};"
            f" border-radius: {theme.RADIUS_LG}px; }}")
        column = QVBoxLayout(panel)
        column.setContentsMargins(14, 14, 14, 14)
        column.setSpacing(10)

        header = QHBoxLayout()
        header.setSpacing(6)
        # Larger than the stock text buttons, with the rest of this panel
        # (the owner's ask - at the old 4px padding these read as labels).
        self._prev_btn = QPushButton("‹  Prev")
        self._next_btn = QPushButton("Next  ›")
        for button, step in ((self._prev_btn, -1), (self._next_btn, 1)):
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            button.setStyleSheet(
                f"QPushButton {{ background: transparent; color: {theme.TEXT};"
                f" border: none; padding: 8px 12px; font-weight: 700;"
                f" font-size: 12.5pt; }}"
                f"QPushButton:hover {{ color: {theme.ACCENT}; }}"
                f"QPushButton:disabled {{ color: {theme.TEXT_DIM}; }}")
            use_hover_cursor(button)
            button.clicked.connect(lambda checked=False, s=step: self._step_season(s))
        # PickCombo, not QComboBox: a plain one drops the first click on
        # a row for half a second after the popup opens - see there.
        self._season_box = PickCombo()
        # Wide enough for a two-digit season plus the drop arrow. Without
        # this the box is sized for "Season 2" and "Season 22" is cut off
        # mid-word (the owner's screenshot): QComboBox sizes to its
        # *current* item, and the padding and arrow are not counted by
        # the length hint, hence a floor as well.
        self._season_box.setMinimumContentsLength(11)
        self._season_box.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToContents)
        self._season_box.setMinimumWidth(150)
        self._season_box.setStyleSheet(
            f"QComboBox {{ background: {theme.SURFACE}; color: {theme.TEXT};"
            f" border: 1px solid {theme.BORDER}; border-radius: {theme.RADIUS}px;"
            f" padding: 8px 16px; font-weight: 700; font-size: 12pt; }}"
            f"QComboBox:hover {{ border: 1px solid {theme.ACCENT}; }}"
            # The popup list, too: it inherits none of the above, and at
            # its default width the same two-digit names clipped.
            f"QComboBox QAbstractItemView {{ background: {theme.SURFACE};"
            f" color: {theme.TEXT}; border: 1px solid {theme.BORDER};"
            f" selection-background-color: {theme.SURFACE_ACTIVE};"
            f" outline: none; padding: 4px; }}")
        use_hover_cursor(self._season_box)
        self._season_box.activated.connect(self._pick_season)
        header.addWidget(self._prev_btn)
        header.addStretch(1)
        header.addWidget(self._season_box)
        header.addStretch(1)
        header.addWidget(self._next_btn)
        # Re-ask the source for this list (the owner's ask). Both lists
        # are cached - chapters on disk for six hours, the episode meta
        # by the session - so a title that has just published something
        # otherwise shows a stale list with no way to say "look again".
        # widgets.GlyphButton, not a QPushButton carrying the glyph as
        # text: this rendered as an empty box on the owner's screen, and
        # reader._glyph_button records exactly why. A widget stylesheet
        # merges with the app-wide one property by property, so a rule
        # that names no padding inherits QPushButton's `8px 16px` - on a
        # 38px button that leaves ~6px of content width and Qt draws a
        # clipped box instead of the icon. GlyphButton paints the glyph
        # onto its own rect, with no padding in the arithmetic at all.
        self._refresh_btn = GlyphButton(
            ICON_REFRESH,
            "Look for new " + ("chapters" if self._is_reading else "episodes"))
        self._refresh_btn.clicked.connect(self._refresh_list)
        header.addWidget(self._refresh_btn)
        column.addLayout(header)
        if self._is_reading:
            # One flat chapter list - seasons are a video concept.
            self._prev_btn.setVisible(False)
            self._next_btn.setVisible(False)
            self._season_box.setVisible(False)
            title = QLabel("Chapters")
            title.setAlignment(Qt.AlignmentFlag.AlignCenter)
            title.setStyleSheet(
                f"color: {theme.TEXT}; font-size: 13pt; font-weight: 700;"
                f" background: transparent; border: none;")
            header.insertWidget(2, title, stretch=1)

        # A Discover title arrives here unsaved - no id (see
        # tracker._discover_entry) - and this list is where deciding to
        # keep it happens, so Save lives at the top of it (the owner's
        # ask).
        #
        # **It is a toggle now, and it never hides**, 28 August 2026, the
        # owner: "when the read/watch is saved do not hide this btn, make
        # it says Remove From My List". It used to disappear entirely for
        # an entry opened from Saved and go dead reading "Saved to My
        # List" the moment one was saved, so this list could add a title
        # to the library but never take one out of it - the only way back
        # was to find the card on Saved. `_sync_save_button` owns both
        # faces; `_toggle_saved` is the one handler.
        self._save_btn = QPushButton()
        self._save_btn.setFixedHeight(44)
        use_hover_cursor(self._save_btn)
        self._save_btn.clicked.connect(self._toggle_saved)
        # The hover swap above lives on an event filter rather than on
        # QSS, because what changes is the *icon* and its word, and
        # neither is reachable from a stylesheet.
        self._save_hover = False
        self._save_btn.installEventFilter(self)
        # **Into the layout first, and only then made visible.** The
        # owner, 25 August 2026, with a screenshot: "a small window
        # (white) appears then closes in a moment". This is it - a
        # parentless widget that is shown is a *window* to Qt, title bar
        # and all, until something reparents it a frame later. Every
        # unsaved title opened from Discover flashed one.
        column.addWidget(self._save_btn)
        self._sync_save_button()

        # **widgets.search_field, not a bare QLineEdit.** The owner, 4
        # September 2026: "the x in the search videos or ch in the ep/ch
        # list make it has hover and finger-pointing cursor like the
        # global search". Qt's own clear button is a plain tool button
        # child with the arrow cursor and no hover state - measured 2
        # September 2026, 0 pixels changed under a synthetic hover - and
        # `search_field` is the helper that fixes exactly that, giving it
        # the hand through the cursor registry and a glyph that follows
        # the pointer (_ClearButtonHighlight). This page was the one
        # search box in the app not built through it, so it was the one
        # box whose × behaved like nothing else.
        self._search = search_field(
            "search chapters" if self._is_reading else "search videos")
        self._search.textChanged.connect(lambda _t: self._search_timer.start(220))
        column.addWidget(self._search)

        # No inline "background: transparent" on the host/area here: a
        # declaration-only stylesheet on a widget cascades to every
        # descendant and outranks the app stylesheet, which was silently
        # killing the row Cards' matte fill and their hover ring
        # (measured: with the sheet on the area, a hovered #Card painted
        # nothing; without it, the ACCENT ring and ACCENT_SOFT fill).
        # theme.py's QScrollArea and #Bare rules already make the area
        # and host transparent without touching their children.
        self._rows_host = QWidget(objectName="Bare")
        self._rows = QVBoxLayout(self._rows_host)
        self._rows.setContentsMargins(0, 0, 6, 0)
        self._rows.setSpacing(6)
        self._rows.addStretch(1)
        self._rows_area = scroll_area(self._rows_host, ground=theme.BG)
        # Rows that have not been built yet - see _queue_rows. Kept on
        # the page rather than passed around, because a refill, a search
        # keystroke and a teardown all have to be able to drop them.
        self._row_queue = []
        self._rows_area.verticalScrollBar().valueChanged.connect(
            self._maybe_extend_rows)
        column.addWidget(self._rows_area, stretch=1)

        # Centred over the list rather than tucked under it. "Finding
        # sources for S01E07..." used to sit in the bottom-left corner,
        # where it read as a footnote and was easy to miss entirely (the
        # owner's ask).
        #
        # A child of the scroll area's viewport, not a row in the column:
        # rows come and go with every refill (_clear_rows empties that
        # layout), and a note living in it would be destroyed by the
        # first list it was meant to describe. _PanelNote lays itself
        # out - see its docstring for the two bugs the by-hand placement
        # used to have.
        self._panel_note = _PanelNote("Loading...", self._rows_area.viewport())
        return panel

    # ---- lookups ----------------------------------------------------------
    def _start_lookups(self):
        import threading
        self._run += 1
        self._meta_retried = False
        self._chapters_retried = False
        if self.entry.get("imdb_id"):
            # A dedicated thread, not lookup_pool - see _art_worker.
            threading.Thread(target=_art_worker,
                             args=(self._signals, self._run, dict(self.entry)),
                             daemon=True).start()
            # Warm the arc-name season map now, in the background, so the
            # first press of an episode is already filtered rather than
            # the second - the source list is only cleaned from a warm
            # cache (see helpers/anime_identity and streams.find_streams).
            # A no-op for anything that is not anime, and deduplicated per
            # id, so this costs nothing to call on every details open.
            anime_identity.prewarm(dict(self.entry))
            # And the sources for whatever Continue would play, warmed
            # into streams' own cache while the page is being read.
            self._prefetch_sources()
        if self._is_reading:
            # **No artwork ground on a reading page at all** - the
            # owner's ask, 21 August 2026: "remove all readings bg image
            # in the ch list, make the bg use something according to the
            # app theme".
            #
            # It was never going to be consistent, and that was the
            # complaint: measured over their own titles, some resolve to
            # a 1900x400 AniList banner, some only to a 460x652 portrait
            # cover, and some to nothing at all - so the same page had a
            # sharp cinematic ground for One Piece, a blurred one for
            # Celebrity Lady and a flat one for a scanlation title. The
            # page's own theme ground (GlassPage paints theme.BG) is the
            # one answer that is the same for every title.
            #
            # _reading_art_worker and the three-catalogue chain behind it
            # are kept: the *cards* still want a cover, and the video
            # pages still want a backdrop. Only this page stopped asking.
            #
            # The *logo* is a different matter - the owner, 24 August
            # 2026: "make the logo instead of the name in the ch list if
            # it is possible, otherwise a normal name as it is now". A
            # reading title whose franchise is an anime TMDB has a title
            # treatment for (Kingdom, One Piece, Hunter x Hunter) wears
            # it where the name is, exactly as the video pages do through
            # _art_worker; anything else keeps its typed name, which is
            # what _on_art leaves up when nothing lands. Disk-cached by
            # title, so a revisit is one stat and no request.
            threading.Thread(target=_reading_logo_worker,
                             args=(self._signals, self._run, dict(self.entry)),
                             daemon=True).start()
            lookup_pool.submit_watched(_reading_genres_worker, self._signals,
                               self._run, self.entry.get("title") or "")
            if chapter_source is None:
                self._panel_note.setText("No chapter source in this build.")
            elif (not self.entry.get("url") and not self.entry.get("site_id")
                    and self._reading_site_choices()):
                # A Discover title has no site yet - offer the configured
                # reading sites first (the owner's ask), and fetch the
                # chapters from whichever one is picked.
                self._fill_site_rows()
            else:
                # **The last list seen, drawn before any request** (rule
                # 7). Measured 3 September 2026 on the owner's Kingdom
                # (WAN), whose reader_cache row was past the six-hour
                # TTL: the page sat on "Loading..." for **3.36-4.24s**
                # while 3asq listed 381 chapters it already had on disk.
                # Now **0.078-0.088s** to the first 40 rows from the
                # stale row (the 381 builders are the cost), and the
                # worker redraws only if the site's answer differs - it
                # did not, so 3asq's 3.7s answer changed nothing.
                stale = self._stale_chapters()
                if stale:
                    self._show_chapters(stale)
                lookup_pool.submit_watched(
                    _chapters_worker, self._signals, self._run,
                    dict(self.entry), False, stale or None,
                    alive=self._still_wanted(self._run))
                if not stale:
                    self._expect_list()
        elif self.entry.get("imdb_id") and stremio is not None:
            kind = "movie" if self.entry.get("type") == "Movie" else "series"
            # Same shape for the episode list: the disk copy whatever
            # its age, now - see _cached_meta for the numbers.
            stale = _cached_meta(self.entry.get("imdb_id"), kind, any_age=True)
            if stale is not None:
                self._show_meta(stale)
            lookup_pool.submit_watched(_meta_worker, self._signals, self._run,
                               self.entry.get("imdb_id"), kind,
                               stale is not None)
            if stale is None:
                self._expect_list()
        elif stremio is not None and (self.entry.get("title") or "").strip():
            # **No id, but a title - so look the title up rather than
            # giving up.** This used to go straight to the note below,
            # which is what the owner saw opening an anime from the
            # Schedule: a page headed "That Time I Got Reincarnated as a
            # Slime Season 4" over an empty list saying there was no
            # matched title. Anything that arrives without an id gets
            # one asked for here - a Schedule row, a hand-added entry, a
            # row from a catalog that never carried one - and the list
            # fills when it lands. The note only claims "no matched
            # title" once that lookup has actually failed (see
            # _on_resolved_id).
            self._panel_note.setText("Looking this title up...")
            lookup_pool.submit_watched(_resolve_id_worker, self._signals, self._run,
                               self.entry.get("title"), self.entry.get("type"))
            self._expect_list()
        else:
            self._panel_note.setText(
                "This entry has no matched title, so there is no episode "
                "list to show. Continue below still opens it.")

    def _on_resolved_id(self, run, imdb_id):
        """The title lookup landed. An id means the episode list can be
        fetched exactly as a saved entry's is; no id means the honest
        message, now that it has been earned."""
        if run != self._run or self._closed:
            return
        if not imdb_id:
            self._list_answered()
            self._say("This entry has no matched title, so there is no "
                      "episode list to show. Continue below still opens it.")
            return
        # Written onto the entry, not just used once: the player, the
        # download panel and progress syncing all read imdb_id off it,
        # and re-deriving it per feature would be three more lookups.
        self.entry["imdb_id"] = imdb_id
        # **And now the artwork, which nothing had asked for.** The owner,
        # 4 September 2026: "when I enter the watches from the schedule
        # page the ep list page bg is blurred and the logo does not
        # load". _start_lookups fires _art_worker only when the entry
        # already carries an id, and a Schedule row does not - measured
        # 0 of 40 calendar rows carry one - so both TMDB lookups
        # (artwork.backdrop_path, artwork.logo_path key off imdb_id and
        # return None without it) were skipped for exactly the entries
        # this branch exists to rescue. The page kept the blurred copy of
        # its own cover that _seed_backdrop_from_cover puts up as the
        # placeholder, and the typed title where the logo goes.
        import threading
        threading.Thread(target=_art_worker,
                         args=(self._signals, self._run, dict(self.entry)),
                         daemon=True).start()
        kind = "movie" if self.entry.get("type") == "Movie" else "series"
        lookup_pool.submit_watched(_meta_worker, self._signals, self._run,
                           imdb_id, kind)
        # Now that the id is known, warm the arc-name season map too, so a
        # Discover anime that only just resolved is filtered on its first
        # play - see the same call in _start_lookups.
        anime_identity.prewarm(dict(self.entry))
        self._expect_list()

    def _on_meta(self, run, meta):
        if run != self._run or self._closed:
            return
        self._list_answered()
        if not meta:
            # One silent retry before admitting defeat: the owner
            # reported the list "sometimes fetches nothing" on titles
            # that load fine a moment later - a single flaky Cinemeta
            # answer, and the page treated it as final. The note keeps
            # its loading text while the retry runs; the refresh button
            # stays disarmed for the same reason.
            if not self._meta_retried and self.entry.get("imdb_id"):
                self._meta_retried = True
                kind = ("movie" if self.entry.get("type") == "Movie"
                        else "series")
                lookup_pool.submit_watched(_meta_worker, self._signals, self._run,
                                   self.entry.get("imdb_id"), kind)
                self._expect_list()
                return
            self._finish_refresh()
            self._say("The episode list couldn't be loaded. Check the "
                      "connection and reopen this page.")
            return
        self._finish_refresh()
        self._show_meta(meta)

    def _show_meta(self, meta):
        """Put a Cinemeta answer on the page - the facts, the season
        picker, the rows. Called with the disk copy from _start_lookups
        and with the live one from _on_meta."""
        self._meta = meta
        self._videos = [v for v in (meta.get("videos") or [])
                        if isinstance(v, dict)]
        self._fill_facts()
        self._fill_seasons()
        self._fill_rows()

    def _stale_chapters(self):
        """This entry's last chapter list from chapter_source's store,
        whatever its age - [] when there is none, or the build has no
        chapter source."""
        reader = getattr(chapter_source, "stale_chapters", None)
        if reader is None:
            return []
        try:
            return list(reader(dict(self.entry)) or [])
        except Exception:
            logs.exception("details stale chapter read failed")
            return []

    def _still_wanted(self, run):
        """A predicate a worker can ask before paying for a slow fetch:
        is this page still open, and still on the run that asked? A
        chapter listing is up to 21.7s of site pages (olympustaff), and
        on the two watched workers a closed page's listing sat ahead of
        the page the user had moved to."""
        return lambda page=self, run=run: (not page._closed
                                           and page._run == run)

    def _seed_backdrop_from_cover(self):
        """Put a blurred ground made from the entry's **own cover** up, so
        the page never opens with nothing behind it.

        **The same picture the banners use** - the owner's follow-up, 23
        August 2026: "the ch list page bg blur is not good make it as the
        banners bg images!". The first attempt blurred the raw cover here
        in Qt (scale down 32x, back up in two passes) and it was still a
        zoomed, blocky crop: a portrait cover filling a 1600x900 page is
        upscaled past 2x before any blur happens, and Qt has no real blur
        without a QGraphicsEffect, which on a page ground would repaint on
        every hover. `hero_art.wide_ground` already solves exactly this
        with Pillow for the heroes, is disk-cached beside the cover, and
        is very often already composed by the time this page opens -
        Home's hero and the tracker's featured banner compose the same
        file from the same cover.

        Taken synchronously when it is on disk (a stat), on a worker when
        it is not - composing is Pillow work and does not belong on the
        UI thread while the page is being built.

        The owner's ask, 23 August 2026 - "in the ch list page as in img 3
        add the cover as bg image but make it blurred!" - and the
        screenshot with it was a chapter list on flat black. Measured on
        that same entry (Kingdom (WAN), a real window, 9s after open):
        `self._backdrop` was still **None**. The art worker does chain
        down to the entry's cover, but it is a network chain (AniList,
        then MangaDex, then MangaUpdates) and for this entry it delivered
        nothing at all - so the page the owner actually looks at had no
        ground on any visit.

        The cover is already on disk: it is the file the card he clicked
        was drawn from. Reading it here costs one local decode on open and
        needs no lookup, and paintEvent already knows what to do with a
        portrait source - it blurs it into a ground rather than stretching
        it (see the note there). A real landscape banner arriving later
        still replaces this through `_on_art`; this is the floor, not the
        ceiling.

        Never raises: an entry with no cover on disk simply keeps the
        flat ground it had before."""
        try:
            path = self._cover_source()
            if not path:
                # Nothing on disk yet. The cover may still be downloadable
                # (a Discover entry carries only a URL), so hand the whole
                # job to the worker rather than giving up here - it can
                # afford the round trip and this thread cannot.
                self._compose_ground_later("")
                return
            ready = hero_art.ground_ready(path)
            if ready:
                pixmap = QPixmap(str(ready))
                if not pixmap.isNull():
                    self._backdrop = pixmap
                    self._backdrop_is_ground = True
                return
            # Not composed yet. Off the UI thread, and landing through
            # the same `art` signal the network workers use, so it is
            # dropped for a closed page like every other late answer.
            #
            # `_run` is read at *emit* time, not captured here. This runs
            # from __init__, where _run is still 0 and the page's first
            # load has not bumped it yet - capturing it meant the compose
            # always landed on a stale run and was thrown away (measured:
            # the ground never appeared at all). The ground belongs to the
            # entry, not to a particular load of it, so whatever run is
            # current when it finishes is the right one.
            self._compose_ground_later(path)
        except Exception:
            pass

    def _compose_ground_later(self, path):
        """Compose the blurred ground off the UI thread and hand it back
        through the `art` signal.

        `_run` is read at *emit* time, not captured here. This runs from
        __init__, where `_run` is still 0 and the page's first load has
        not bumped it yet - capturing it meant the compose always landed
        on a stale run and was thrown away (measured: the ground never
        appeared at all). The ground belongs to the entry, not to a
        particular load of it."""
        def compose():
            try:
                source = path or self._cover_source(allow_download=True)
                made = hero_art.wide_ground(source) if source else None
            except Exception:
                made = None
            if not made and self._is_reading:
                made = self._catalogue_ground()
            if not made:
                return
            try:
                self._signals.art.emit(self._run, "ground", str(made))
            except RuntimeError:
                pass        # the page went away while this was composing

        threading.Thread(target=compose, daemon=True,
                         name="atomic-details-ground").start()

    def _catalogue_ground(self):
        """A blurred ground made from a catalogue's cover, or None.

        **The last rung, for a reading title whose own site will not
        answer.** The owner, 4 September 2026: "the readings ch list page
        when entered from searching page has no bg blurred image!!!!".

        A row from the search page carries the picture the card was drawn
        with, and for Legend of the Northern Blade that picture lives on
        meshmanga.com - which his own log says would not answer him that
        day ("cover host would not answer: meshmanga.com"). With nothing
        to download there is nothing to blur, and the page opened flat.

        The catalogues know the same title and are reachable: AniList,
        then MangaDex, then MangaUpdates, which is the chain
        _reading_art_worker has always used. Asked only here, after the
        entry's own cover has failed - a saved title with its cover on
        disk never pays for it.

        Never raises: no match, no network, no ground, same as before.
        """
        title = str(self.entry.get("title") or "").strip()
        if not title:
            return None
        for source in ("anilist", "mangadex", "mangaupdates"):
            try:
                if source == "anilist":
                    from helpers import anilist
                    url = anilist.fetch_manga_artwork(title)
                elif source == "mangadex":
                    from helpers import mangadex
                    url = mangadex.fetch_cover_url(title)
                else:
                    from helpers import mangaupdates
                    url = mangaupdates.fetch_cover_url(title)
                local = images.download(url) if url else None
                made = hero_art.wide_ground(local) if local else None
                if made:
                    return made
            except Exception:
                continue
        return None

    def _cover_source(self, allow_download=False):
        """A local image file for this entry's cover, or "".

        **Three rungs, and the second one is the fix.** The first version
        of this read `entry["cover_path"]` and nothing else - and
        `tracker.discover_entry` hardcodes `"cover_path": None`, so a
        title opened from Discover (which is most of them, and is exactly
        the screenshot the owner sent: "My Path to Killing Gods in Another
        World", flat black) never had one. What it carries instead is
        `cover_url`, and the card he clicked had *already downloaded that
        very file* into the shared image cache - so the picture was on
        disk the whole time under a name this function was not asking for.

        Rung three downloads it - **only when `allow_download` is set**,
        which is only ever on the compose worker. This is called from
        __init__, and a download there would be a network round trip on
        the UI thread while the page is being built.

        Never raises; "" simply means the page keeps its flat ground."""
        entry = self.entry or {}
        path = entry.get("cover_path") or ""
        if path and Path(path).exists():
            return str(path)
        url = entry.get("cover_url") or entry.get("poster") or ""
        if not url:
            return ""
        try:
            cached = images.cache_path_for_url(url)
            if cached and Path(cached).exists():
                return str(cached)
        except Exception:
            pass
        if not allow_download:
            return ""
        try:
            got = images.download(url)
        except Exception:
            got = None
        return str(got) if got and Path(str(got)).exists() else ""

    def _on_art(self, run, kind, path):
        if run != self._run or self._closed:
            return
        if kind in ("backdrop", "ground"):
            pixmap = QPixmap(path)
            if pixmap.isNull():
                # **Keep whatever is already up.** A failed or empty
                # backdrop used to clear the ground to None, which would
                # now throw away the cover seeded in __init__ and put the
                # page back on flat black - the very thing that seed
                # exists to stop. Nothing arriving is not a reason to
                # remove something that did.
                return
            # A real landscape banner from the network outranks the
            # composed ground; the composed one must not overwrite it.
            if kind == "ground" and self._backdrop is not None                     and not self._backdrop_is_ground:
                return
            self._backdrop = pixmap
            self._backdrop_is_ground = (kind == "ground")
            self._backdrop_scaled = None
            self._backdrop_size = None
            self.update()
            return
        pixmap = QPixmap(path)
        if pixmap.isNull():
            return
        # Scaled by devicePixelRatio and tagged, or it blurs on any
        # non-100% display (.claude/rules/ui.md).
        ratio = self.devicePixelRatioF() or 1.0
        scaled = pixmap.scaled(int(420 * ratio), int(130 * ratio),
                               Qt.AspectRatioMode.KeepAspectRatio,
                               Qt.TransformationMode.SmoothTransformation)
        scaled.setDevicePixelRatio(ratio)
        self._logo.setPixmap(scaled)
        self._logo.setVisible(True)
        self._title_label.setVisible(False)

    def _on_chapters(self, run, chapters):
        if run != self._run or self._closed:
            return
        self._list_answered()
        if chapters is None:
            # The lookup *failed* (see _chapters_worker) - distinct from
            # a title that genuinely has no chapters. One silent retry,
            # without refresh=True even if a refresh asked: letting the
            # disk cache answer the second attempt turns "error" into a
            # slightly stale list, which is the better failure.
            if not self._chapters_retried:
                self._chapters_retried = True
                lookup_pool.submit_watched(_chapters_worker, self._signals,
                                   self._run, dict(self.entry))
                self._expect_list()
                return
            self._finish_refresh()
            self._say("The chapter list couldn't be loaded. Check the "
                      "connection and press the refresh button to try again.")
            return
        self._finish_refresh()
        self._chapters = list(chapters)
        if not self._chapters:
            # _say, not setText: a *partial* emission may already have
            # filled rows and hidden the note (see _fill_chapter_rows'
            # setVisible(shown == 0)), and then this message was written
            # onto an invisible label while the panel went on showing
            # chapters this page no longer has. A surface the user
            # opened has to say what happened - CLAUDE.md rule 7.
            self._clear_rows()
            self._say("No chapters were found for this title.")
            return
        self._show_chapters(self._chapters)

    def _show_chapters(self, chapters):
        """Put a chapter list on the page - the count, then the rows.
        Called with the stale disk row from _start_lookups and with the
        site's answer from _on_chapters."""
        self._chapters = list(chapters)
        read = self._last_read()
        total = len({c.get("number") for c in self._chapters})
        self._set_facts([f"{total} chapters"]
                        + ([f"read up to {read:g}"] if read else []))
        self._fill_rows()

    # ---- the facts column ---------------------------------------------
    def _set_facts(self, parts):
        """Remember what this page knows about the title, and draw it.

        One place, because the three callers that used to `setText` here
        directly each knew about only their own half - the video facts,
        the chapter count, and the chapter count again after a
        mark-as-read."""
        self._facts_parts = [str(p) for p in parts if str(p).strip()]
        self._render_facts()

    def _render_facts(self):
        """The facts line as it stands, with what Atomic's users think of
        this title on the end of it.

        **Beside IMDb's number, for the title as a whole** (the owner's
        ask, 25 August 2026: "make it appear next to the IMDB rating for
        the whole watchable and readable"). A reading title has no IMDb
        figure to sit beside - no catalogue rates scanlations - so there
        it joins the chapter count, which is that line's own facts."""
        parts = list(getattr(self, "_facts_parts", ()))
        self._facts.setText("   ·   ".join(parts))

    def _fill_facts(self):
        meta = self._meta or {}
        parts = [str(meta.get("runtime") or "").strip(),
                 _year_text(meta.get("releaseInfo") or meta.get("year"))]
        rating = str(meta.get("imdbRating") or "").strip()
        if rating:
            parts.append(f"★ {rating} IMDb")
        self._set_facts(parts)

        def fill(row, values):
            # **Cleared first, then filled.** This ran on every meta
            # arrival and only ever appended, so pressing refresh
            # appended the same names again: the owner's screenshot
            # showed Re:Zero's three-name cast four times over, and the
            # harness reproduced it exactly (3 chips after one meta, 12
            # after four). The genre row never had the bug because
            # _fill_genre_buttons clears before it inserts - this is
            # that same clear, and the trailing stretch is what count()
            # - 1 preserves in both.
            while row.count() > 1:
                item = row.takeAt(0)
                widget = item.widget()
                if widget is not None:
                    widget.deleteLater()
            for value in values:
                row.insertWidget(row.count() - 1, _chip(value))

        genres = [g for g in (meta.get("genres") or []) if g][:5]
        # Carried on the entry the player is handed, so its audio default
        # knows an anime film from a live-action one before its own meta
        # fetch answers (player._audio_language_preference).
        try:
            self.entry["genres"] = list(meta.get("genres") or meta.get("genre") or [])
            self.entry["country"] = str(meta.get("country") or "")
        except Exception:
            pass
        if genres:
            self._genres_head.setVisible(True)
            self._fill_genre_buttons(genres)
        cast = [c for c in (meta.get("cast") or []) if c][:4]
        if cast:
            self._cast_head.setVisible(True)
            self._fill_cast_buttons(cast)
        description = str(meta.get("description") or "").strip()
        if description:
            self._summary_head.setVisible(True)
            self._summary.setVisible(True)
            self._summary.setText(description)

    # ---- genres as doors --------------------------------------------
    def _fill_genre_buttons(self, genres):
        """The genre chips, pressable: each one opens everything else
        filed under that genre (the owner's ask). Replaces whatever the
        row held - the reading lookup can land after a rebuild."""
        while self._genres_row.count() > 1:
            item = self._genres_row.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        for genre in genres:
            button = _chip_button(genre)
            button.clicked.connect(
                lambda _checked=False, g=genre: self._open_genre_browse(g))
            self._genres_row.insertWidget(self._genres_row.count() - 1, button)

    def _on_reading_genres(self, run, names):
        if run != self._run or self._closed or not names:
            return
        self._genres_head.setVisible(True)
        self._fill_genre_buttons([str(n) for n in names][:5])

    # ---- the cast as doors too ---------------------------------------
    def _fill_cast_buttons(self, names):
        """The cast chips, pressable - the owner, 2 September 2026:
        "make the cast also clickable as the genres". Cleared before it
        fills, the rule _fill_facts' fill() records (every meta arrival
        used to append the same names again)."""
        while self._cast_row.count() > 1:
            item = self._cast_row.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        for name in names:
            button = _chip_button(str(name))
            button.clicked.connect(
                lambda _checked=False, n=str(name): self._open_cast_browse(n))
            self._cast_row.insertWidget(self._cast_row.count() - 1, button)

    def _open_cast_browse(self, name):
        """One cast member's titles as a full page over the window - the
        web genre shell with a cast route (web_reader.WebCastBrowse).
        No painted fallback: the page never existed as a Qt grid, so
        without WebView2 this logs and the chip does nothing."""
        page = None
        try:
            from windows import web_reader
            page = web_reader.open_cast_browse(self.window(), name)
        except Exception:
            logs.exception("web cast browse unavailable")
        if page is not None:
            self._genre_page = page
        return page

    def _open_genre_browse(self, genre):
        """Open the genre as its own full page over the window - not a
        dialog (the owner's ask). Hosted on the central widget like this
        page is, so it covers the sidebar the same way and Back/Escape
        lands here again."""
        window = self.window()
        host = (window.overlay_host() if hasattr(window, "overlay_host")
                else (window.centralWidget() if hasattr(window, "centralWidget")
                      else window))
        host = host if host is not None else window
        # **The web page when this build has one.** The owner, 2
        # September 2026: "make the same generes page use the same scroll
        # as other pages (WebView2)". GenreBrowsePage stays as the
        # fallback, exactly as the Qt reader does behind the web one.
        page = None
        try:
            from windows import web_reader
            page = web_reader.open_genre_browse(self.window(), genre,
                                                self._is_reading)
        except Exception:
            logs.exception("web genre browse unavailable")
        if page is not None:
            self._genre_page = page
            return
        page = GenreBrowsePage(genre, self._is_reading, host)
        page.open_title = self._open_browsed_title
        page.follow(host)
        page.show()
        page.raise_()
        freeze_covered(page)   # see widgets._CoveredFreeze
        page.setFocus()
        return page

    def _open_browsed_title(self, item, entry_type):
        """A pick from the genre browse opens its own details page over
        this one - the same transient-entry road a Discover card takes
        (tracker._discover_entry states why it is id-less)."""
        try:
            from windows.tracker import discover_entry
            open_details(self.window(), discover_entry(item, entry_type))
        except Exception:
            logs.exception("genre browse could not open the details page")

    # ---- the list panel -------------------------------------------------
    def _seasons(self) -> list:
        seasons = sorted({int(v.get("season") or 0) for v in self._videos})
        # Specials (season 0) only when they are all there is.
        numbered = [s for s in seasons if s > 0]
        return numbered or seasons

    def _fill_seasons(self):
        seasons = self._seasons()
        self._season_box.blockSignals(True)
        self._season_box.clear()
        for season in seasons:
            self._season_box.addItem(f"Season {season}" if season else "Specials",
                                     season)
        # Open on the season being watched, else the newest with anything
        # aired - the one a returning viewer is actually looking for.
        watched_season, _ = self._progress()
        pick = watched_season if watched_season in seasons else (
            seasons[-1] if seasons else 0)
        index = max(0, self._season_box.findData(pick))
        self._season_box.setCurrentIndex(index)
        self._season = self._season_box.currentData() or 0
        self._season_box.blockSignals(False)
        self._sync_season_arrows()

    def _sync_season_arrows(self):
        seasons = self._seasons()
        self._prev_btn.setEnabled(bool(seasons) and self._season > seasons[0])
        self._next_btn.setEnabled(bool(seasons) and self._season < seasons[-1])

    def _step_season(self, step):
        seasons = self._seasons()
        if not seasons or self._season not in seasons:
            return
        index = seasons.index(self._season) + step
        if 0 <= index < len(seasons):
            self._season = seasons[index]
            self._season_box.setCurrentIndex(
                self._season_box.findData(self._season))
            self._sync_season_arrows()
            self._fill_rows()

    def _pick_season(self, _index):
        self._season = self._season_box.currentData() or 0
        self._sync_season_arrows()
        self._fill_rows()

    def _progress(self):
        """(season, episode) the entry's progress has reached - only when
        confirmed real, the same rule the cards follow (an unverified
        number is the tracker's guess at what is *out*, not at what has
        been watched, and a guess must not paint WATCHED badges)."""
        if not self.entry.get("progress_verified"):
            return 0, 0
        try:
            from windows.tracker import parse_episode_progress
        except ImportError:                             # pragma: no cover
            return 0, 0
        season, episode = parse_episode_progress(self.entry.get("progress"))
        return int(season or 0), int(episode or 0)

    def _last_read(self) -> float:
        try:
            return float(self.entry.get("last_watched_chapter") or 0)
        except (TypeError, ValueError):
            return 0.0

    def _queue_rows(self, builders):
        """Take the whole list as *builders* and put only the first
        screenful of them on screen.

        **Measured on the owner's own library, 21 August 2026**, the
        chapter list built one row per chapter the moment it arrived:

            One Piece      508 chapters   1258ms to fill, 725ms to clear
            Kingdom (WAN)  380 chapters   1037ms to fill, 436ms to clear

        which is exactly the owner's "clicking One Piece freezes for
        ~1.5 sec" and "the back button from the reading ch takes a few
        secs" - the clear is what back pays. Both numbers are the UI
        thread, so nothing else can happen while they run.

        A row costs 1.18ms to *build* but 2.5ms to build-and-insert,
        because every insertWidget into a live layout is another layout
        pass over everything already in it. So this does both things
        that helps: it inserts with updates switched off, and it only
        inserts what can be seen. 40 rows against a 13-row viewport is
        ~50ms, and the rest arrive as the list is scrolled - which also
        means a list nobody scrolled costs nothing to tear down again.

        Search is unaffected: filtering rebuilds this queue from the
        already-computed list, which is strings, not widgets."""
        self._row_queue = list(builders)
        self._materialise_rows(ROW_FIRST_BATCH)
        # A short list, or a tall window, can leave the first batch not
        # even filling the viewport - then no scroll ever comes to ask
        # for the next one. Ask once the layout has settled instead.
        QTimer.singleShot(0, self._maybe_extend_rows)

    def _materialise_rows(self, count):
        """Build and insert the next `count` queued rows."""
        if not self._row_queue:
            return
        self._rows_host.setUpdatesEnabled(False)
        self._still_batch = []
        try:
            # count() - 1: the trailing stretch is always the last item,
            # and rows go in above it.
            shown = self._rows.count() - 1
            for _ in range(min(int(count), len(self._row_queue))):
                widget = self._row_queue.pop(0)()
                if widget is None:
                    continue
                self._rows.insertWidget(shown, widget)
                shown += 1
        finally:
            self._rows_host.setUpdatesEnabled(True)
            batch, self._still_batch = self._still_batch, None
            self._submit_stills(batch)

    def _queue_still(self, key, url, blur):
        """Ask for one row's still. Inside a _materialise_rows batch the
        ask is held and submitted with the batch; anywhere else it goes
        straight out. Both _still_tile and the installed patch's tile
        builder come through here, so the queueing lives in one place."""
        self._still_asks[key] = (url, blur)
        if self._still_batch is not None:
            self._still_batch.append((key, url, blur))
        else:
            self._submit_stills([(key, url, blur)])

    def _submit_stills(self, batch):
        """**The cover queue, not the shared one - measured 2 September
        2026.** Stills used to go on lookup_pool.submit, the four workers
        the tracker's page-load backfill drains: with thirty 1s lookups
        queued ahead (one tracker page's worth), Reacher's eight stills
        landed at **7.02-7.05s** after the page opened, against 0.60-0.62s
        on an idle pool - the owner's "the episodes images are not
        loading". The cover queue is LIFO and three wide, built for
        exactly "the page being looked at, first".

        LIFO means the batch is pushed **last row first**, so the pops
        come out row 1, row 2, ... - the first screen fills from the
        top, and a batch built for a later scroll lands ahead of the
        rest of this one, which is where the user now is. A page that
        has been left is skipped by the worker before it downloads
        (`alive`), so a stale batch costs the next page nothing."""
        if not batch:
            return
        alive = lambda page=self: not page._closed
        for key, url, blur in reversed(batch):
            lookup_pool.submit_cover(_still_worker, self._signals, key, url,
                                     blur, alive)

    # How long a refused still waits before it is asked for again.
    STILL_RETRY_MS = 20_000

    def _retry_refused_stills(self):
        """Re-queue every still whose host was refusing, for the rows
        that still exist (a rebuilt list drops its keys from
        _still_tiles)."""
        if self._closed:
            return
        keys, self._still_refused = self._still_refused, set()
        batch = [(key,) + self._still_asks[key] for key in sorted(keys)
                 if key in self._still_tiles and key in self._still_asks]
        self._submit_stills(batch)

    def _maybe_extend_rows(self, _value=None):
        """Build the next batch when the view nears the end of what has
        been built. Guarded against a torn-down page: this is wired to a
        scrollbar that outlives nothing, but a queued singleShot can
        still land after leave()."""
        if self._closed or not self._row_queue:
            return
        try:
            bar = self._rows_area.verticalScrollBar()
        except RuntimeError:
            return
        if bar.maximum() - bar.value() <= ROW_EXTEND_MARGIN_PX:
            self._materialise_rows(ROW_BATCH)
            # Straight back round: one batch may still not reach the
            # bottom of a tall window, and stopping here would leave the
            # list stuck with no further scroll event coming.
            QTimer.singleShot(0, self._maybe_extend_rows)

    def _clear_rows(self):
        self._row_queue = []
        # Every tile in here is about to be deleted, and a still that
        # lands afterwards must not be handed a dangling widget. The
        # download itself is already paid for - _STILL_READY keeps it,
        # so the rebuilt row draws it without asking the pool again.
        self._still_tiles.clear()
        # The asks for those tiles too, or a rebuilt page re-asks nothing
        # and the dict grows one entry per still for its life.
        self._still_asks.clear()
        while self._rows.count() > 1:
            item = self._rows.takeAt(0)
            widget = item.widget()
            if widget is not None:
                # **hide() first, then setParent(None), then the
                # delete - all three, in this order.** insertWidget
                # queues _q_showIfNotHidden() on a row while the list's
                # parent is visible; a partial-batch refill clears the
                # rows before the event loop drains it, and
                # setParent(None) hides the row but *clears*
                # WA_WState_ExplicitShowHide - so the queued call sees
                # "hidden, but not explicitly hidden", shows a widget
                # that no longer has a parent, and Qt gives it a framed
                # desktop window of its own. White for its first frame,
                # then gone: measured on House of the Dragon S02E05, 84
                # of them in two seconds, one per source row, 0 after
                # this line. That is the owner's "many windows pop-ups
                # while fetching, white bg".
                #
                # setParent(None) still has to be here: a deleteLater'd
                # row goes on painting at its old geometry until the
                # delete lands (player._fill_episode_bar hit that).
                widget.hide()
                widget.setParent(None)
                widget.deleteLater()

    def _fill_rows(self):
        if self._source_pick is not None:
            self._fill_source_rows()
        elif self._site_choice_pending:
            self._fill_site_rows()
        elif self._is_reading:
            self._fill_chapter_rows()
        else:
            self._fill_episode_rows()

    def _refresh_list(self):
        """Ask the source for this list again (the panel's refresh
        button). Reading skips chapter_source's six-hour disk cache;
        video re-asks Cinemeta and redraws only if the episode list
        actually changed (see _meta_worker). The button disables itself
        until the answer lands, so a series of impatient presses cannot
        stack lookups."""
        if self._closed:
            return
        if self._site_choice_pending:
            # **The one press that used to do nothing at all.** While a
            # reading title is asking which site to read it from, this
            # returned immediately - and the button stayed enabled, so
            # it looked live and was not (measured 25 August 2026 on the
            # owner's own entries: rows unchanged, note unchanged, no
            # request made). Re-listing the sites is what a press means
            # here: the list is `manga_sites.list_sites()`, which the
            # user may have just added a row to in Settings, and the
            # page holds no other way to pick that up.
            self._fill_site_rows()
            return
        self._run += 1
        self._meta_retried = False
        self._chapters_retried = False
        self._refresh_btn.setEnabled(False)
        self._clear_rows()
        self._panel_note.setVisible(True)
        self._panel_note.setText("Looking for new "
                                 + ("chapters..." if self._is_reading
                                    else "episodes..."))
        if self._is_reading:
            if chapter_source is None:
                self._finish_refresh("No chapter source in this build.")
                return
            lookup_pool.submit_watched(_chapters_worker, self._signals, self._run,
                               dict(self.entry), True)
            self._expect_list()
        elif self.entry.get("imdb_id") and stremio is not None:
            kind = "movie" if self.entry.get("type") == "Movie" else "series"
            lookup_pool.submit_watched(_meta_worker, self._signals, self._run,
                               self.entry.get("imdb_id"), kind)
            self._expect_list()
        else:
            self._finish_refresh("This entry has no matched title, so there "
                                 "is nothing to refresh.")

    def _expect_list(self):
        """A list lookup has just been fired: start the clock on it.

        Every attempt gets its own budget, the silent retries included -
        a retry that also never answers has to be reported too."""
        self._quiet_timer.start(LIST_QUIET_MS)

    def _list_answered(self):
        """An answer landed (even a refusal). Stop the clock."""
        self._quiet_timer.stop()

    def _on_list_quiet(self):
        """The lookup has said nothing for LIST_QUIET_MS. Say so.

        Nothing is cancelled: the worker may still land, and if it does
        the rows simply replace this. All this does is stop the panel
        claiming to be loading, and give the refresh button back."""
        if self._closed or self._source_pick is not None:
            return
        if self._site_choice_pending or self._rows.count() > 1:
            return              # the panel already has something to show
        try:
            self._refresh_btn.setEnabled(True)
        except RuntimeError:
            return              # torn down under the lookup
        self._say(("The chapter list" if self._is_reading
                   else "The episode list")
                  + " hasn't answered. It may still arrive; the refresh "
                    "button asks again.")

    def _say(self, message):
        """Put `message` on the panel *and make sure it can be read*.

        setText alone is not enough: _fill_episode_rows/_fill_chapter_rows
        hide the note the moment they have rows, so a verdict arriving
        after a partial list had already filled some was written onto an
        invisible label - the panel went on showing chapters the page no
        longer had, and said nothing about it. A surface the user opened
        either shows what it has or says why it doesn't (CLAUDE.md rule
        7); it never goes quiet."""
        try:
            self._panel_note.setText(message)
            self._panel_note.setVisible(True)
            self._panel_note.raise_()
        except RuntimeError:
            pass        # the panel is being torn down under a lookup

    def _finish_refresh(self, message=""):
        """Re-arm the refresh button once an answer (or a refusal) has
        landed. Guarded: the panel can be torn down under a lookup."""
        try:
            self._refresh_btn.setEnabled(True)
        except RuntimeError:
            return
        if message:
            self._say(message)

    # ---- picking a reading site (Discover titles) ---------------------
    def _reading_site_choices(self) -> list:
        try:
            from helpers import manga_sites
            return manga_sites.list_sites()
        except Exception:
            return []

    def _fill_site_rows(self):
        """The configured reading sites as the list panel's rows - what
        a Discover manga shows before it has a site (the owner's ask).
        MangaDex closes the list as the built-in fallback, so there is
        always a road to a chapter list even with every site down."""
        self._site_choice_pending = True
        self._clear_rows()
        shown = 0
        for site in self._reading_site_choices():
            self._rows.insertWidget(shown, self._row_card(
                site.get("name") or "Site", site.get("base_url") or "",
                None, lambda s=site: self._pick_reading_site(s),
                variant="plain"))
            shown += 1
        self._rows.insertWidget(shown, self._row_card(
            "MangaDex", "The built-in chapter source", None,
            self._pick_mangadex, variant="plain"))
        self._panel_note.setVisible(True)
        self._panel_note.setText("Where should this be read from?")

    def _pick_reading_site(self, site):
        self._run += 1
        self._clear_rows()
        self._panel_note.setVisible(True)
        self._panel_note.setText(
            f"Searching {site.get('name') or 'the site'} for "
            f"'{self.entry.get('title')}'...")
        lookup_pool.submit_watched(_site_resolve_worker, self._signals, self._run,
                           dict(site), self.entry.get("title") or "")

    def _pick_mangadex(self):
        self._site_choice_pending = False
        self._run += 1
        self._chapters_retried = False
        self._panel_note.setVisible(True)
        self._panel_note.setText("Loading...")
        lookup_pool.submit_watched(_chapters_worker, self._signals, self._run,
                           dict(self.entry))

    def _on_site_resolved(self, run, fields, site_name):
        if run != self._run or self._closed:
            return
        if not fields:
            # Back to the list, with the verdict written where the
            # question was - a silent return would read as a dead click.
            self._fill_site_rows()
            self._panel_note.setText(
                f"{site_name} has nothing under this title. "
                "Pick another website.")
            return
        # Written in place on the shared dict: if the title is saved
        # later (the Save button), the binding rides along; if it is
        # already saved, record it now so the pick survives the page.
        self.entry.update(fields)
        if self.entry.get("id"):
            try:
                from windows.tracker import _progress_data_file
                storage.update_entry(_progress_data_file(self.entry),
                                     self.entry["id"], fields)
            except Exception:
                logs.exception("details page could not record the site")
        self._site_choice_pending = False
        self._panel_note.setText(f"Loading chapters from {site_name}...")
        self._run += 1
        self._chapters_retried = False
        lookup_pool.submit_watched(_chapters_worker, self._signals, self._run,
                           dict(self.entry))
        self._expect_list()

    def _season_ratings(self, season, rows):
        """(( {number: score}, label ) for this season's rows.

        **One source for the whole season, never mixed within a list.**
        Cinemeta's numbers are IMDb's; TMDB's differ by 0.4-0.6, so a
        list carrying some of each would compare unlike things. Cinemeta
        wins the season when it actually rated most of it; otherwise
        TMDB, fetched once in the background and remembered. TMDB coming
        back empty (Bleach: Thousand-Year Blood War has its own sparse
        TMDB entity) falls back to whatever Cinemeta did have, so a
        season is never left blank when a real rating exists somewhere."""
        cine = {}
        for video in rows:
            number = int(video.get("number") or video.get("episode") or 0)
            try:
                score = float(str(video.get("rating")).strip())
            except (TypeError, ValueError):
                continue
            if score > 0:
                cine[number] = score
        if rows and len(cine) >= 0.6 * len(rows):
            return cine, "IMDb"

        imdb = (self.entry.get("imdb_id")
                or (self._meta or {}).get("imdb_id") or "").strip()
        if not imdb:
            return cine, "IMDb"

        key = (imdb, season)
        if self._tmdb_ratings_key == key and self._tmdb_ratings:
            return dict(self._tmdb_ratings), "TMDB"

        # An instant answer off the disk cache (no socket), then the
        # network in the background if that missed. Asked once per
        # (series, season) per page.
        tmdb = {}
        try:
            from helpers import ratings
            tmdb = ratings.cached_episode_ratings(imdb, season,
                                                  self._videos) or {}
        except Exception:
            tmdb = {}
        if tmdb:
            self._tmdb_ratings = tmdb
            self._tmdb_ratings_key = key
            return dict(tmdb), "TMDB"
        if key not in self._tmdb_ratings_asked:
            self._tmdb_ratings_asked.add(key)
            lookup_pool.submit_watched(_episode_ratings_worker, self._signals,
                               self._run, imdb, season, list(self._videos))
        # Nothing yet - show Cinemeta's if it had any, and let the
        # fetch fill the rest in when it lands.
        return cine, "IMDb"

    def _on_episode_ratings(self, run, key, mapping):
        """A season's TMDB ratings arrived: keep them and redraw the
        rows, unless the page moved on (a new lookup, a different entry)
        or the user has since flipped to another season."""
        if run != self._run or self._closed:
            return
        if not mapping:
            return          # TMDB had nothing; the Cinemeta draw stands
        self._tmdb_ratings = dict(mapping)
        self._tmdb_ratings_key = key
        if key[1] == int(self._season or 0) and self._source_pick is None:
            self._fill_episode_rows()

    def _fill_episode_rows(self):
        self._clear_rows()
        self._blur_stills = app_settings.get_blur_episode_stills()
        wanted = self._search.text().strip().lower()
        season = int(self._season or 0)
        now = datetime.datetime.now(datetime.timezone.utc)
        watched_season, watched_episode = self._progress()
        rows = [v for v in self._videos if int(v.get("season") or 0) == season]
        rows.sort(key=lambda v: int(v.get("number") or v.get("episode") or 0))
        if not rows and self.entry.get("type") == "Movie":
            self._rows.insertWidget(0, self._row_card(
                "Play Film", _pretty_date((self._meta or {}).get("released")),
                None, lambda: self._start_episode(None, None)))
            self._panel_note.setVisible(False)
            return
        rating_map, rating_label = self._season_ratings(season, rows)
        shown = 0
        builders = []
        for video in rows:
            number = int(video.get("number") or video.get("episode") or 0)
            name = str(video.get("name") or video.get("title") or "").strip()
            # Settings > Watching > "Show episode and chapter numbers
            # only" - an episode title is often a spoiler for the
            # episode about to be watched, so the name can be left off.
            if app_settings.get_hide_entry_names():
                name = ""
            title = f"{number}. {name}" if name else f"{number}. Episode {number}"
            if wanted and wanted not in title.lower():
                continue
            aired = _aired(video.get("firstAired") or video.get("released"))
            upcoming = bool(aired and aired > now)
            # Either store may say watched: the entry's progress number
            # (saved titles) or an explicit History tick (which is all
            # an unsaved title has, and what an out-of-order tick on a
            # saved one writes).
            # The same "ticks win" rule the menu uses (_episode_menu and
            # episode_watch_state_patch.fixed_episode_menu): with any
            # explicit mark on the entry the marks decide; the progress
            # number only speaks when there are none. The badge and the
            # menu disagreed on a ticked-off episode before (review, 3
            # September 2026).
            if self._history_marks:
                watched = not upcoming and (
                    history.episode_key(season, number) in self._history_marks)
            else:
                watched = not upcoming and bool(
                    watched_episode
                    and (season < watched_season
                         or (season == watched_season
                             and number <= watched_episode)))
            # "DONE" for both media (the owner's ask) - one word for
            # "you have finished this", rather than WATCHED here and
            # READ on the chapter rows.
            badge = ("upcoming", "UPCOMING") if upcoming else (
                ("watched", "DONE") if watched else None)
            # The episode's own rating, beside the date. Cinemeta
            # carries it per video already - measured 59/59 on House of
            # the Dragon and 373/373 on Bleach - so this is free: no
            # request, no second source that could disagree with the
            # list it is printed on. "0" is Cinemeta's way of saying
            # unrated (every behind-the-scenes row carries it), and an
            # unrated episode shows nothing rather than a zero.
            meta_line = _pretty_date(video.get("firstAired")
                                     or video.get("released"))
            stars = _episode_rating(rating_map.get(number), rating_label)
            if stars:
                meta_line = f"{meta_line}   ·   {stars}" if meta_line else stars
            builders.append(
                lambda t=title, m=meta_line, b=badge, up=upcoming,
                s=season, e=number,
                still=str(video.get("thumbnail") or ""): self._row_card(
                    t, m, b,
                    (None if up else lambda s=s, e=e: self._start_episode(s, e)),
                    on_menu=(None if up
                             else lambda ev, s=s, e=e:
                             self._episode_menu(ev, s, e)),
                    variant="episode", still_url=still))
            shown += 1
        self._queue_rows(builders)
        self._panel_note.setVisible(shown == 0)
        if shown == 0:
            self._panel_note.setText("Nothing matches that search."
                                     if wanted else "No episodes in this season.")

    def _fill_chapter_rows(self):
        from windows.reader import (chapter_name, chapter_number,
                                    chapter_published, is_arabic)
        self._clear_rows()
        wanted = self._search.text().strip().lower()
        read_up_to = self._last_read()
        shown = 0
        builders = []
        for chapter in self._chapters:
            number = chapter_number(chapter)
            name = chapter_name(chapter)
            head = f"{number:g}" if number is not None else "-"
            if app_settings.get_hide_entry_names():
                name = ""
            title = f"{head}. {name}" if name else f"Chapter {head}"
            if is_arabic(chapter):
                title = f"{title}  · عربي"
            if wanted and wanted not in title.lower():
                continue
            # Same two stores as the episode rows - see there.
            read = number is not None and (
                (read_up_to and number <= read_up_to)
                or history.chapter_key(number) in self._history_marks)
            # No per-chapter score and no per-chapter control - see the
            # episode rows for the ask that moved both to the title.
            meta = chapter_published(chapter)
            builders.append(
                lambda t=title, d=meta,
                b=(("watched", "DONE") if read else None),
                n=number: self._row_card(
                    t, d, b,
                    lambda n=n: self._read(n),
                    on_menu=lambda ev, n=n: self._chapter_menu(ev, n)))
            shown += 1
        self._queue_rows(builders)
        self._panel_note.setVisible(shown == 0)
        if shown == 0 and self._chapters:
            self._panel_note.setText("Nothing matches that search.")

    # ---- the row language -------------------------------------------
    #
    # Three builders, not one, because the owner's complaint was that
    # there was only one: a resolution heading, a release under it and
    # an episode were the same 72px slab in the same fill, so nothing on
    # screen said which was which. Each kind now differs in height, in
    # what sits at its left edge, and in whether its figures are prose
    # or chips - see _group_card and _source_card.

    def _row_card(self, title, date_text, badge, on_click, on_menu=None,
                  variant="chapter", still_url=None):
        """One content row: an episode, a chapter, a reading site, or
        the source picker's back row (`variant="back"`).

        `still_url` is an episode's Cinemeta thumbnail. The tile is
        built and inserted immediately, carrying the play glyph, and the
        image replaces it when the pool answers - so the row never
        reflows and a title with no still keeps the same shape as one
        that has them."""
        from windows.reader import _ElidedLabel
        card = Card(matte=True, hoverable=on_click is not None)
        row = QHBoxLayout(card)
        row.setSpacing(12)

        if variant == "back":
            # Navigation, not content: the shortest row, no fill of its
            # own, and no play triangle - one on "Back to episodes" read
            # as a resume control (the owner's screenshot).
            card.setFixedHeight(BACK_ROW_HEIGHT)
            card.setStyleSheet(self._row_sheet(background="transparent"))
            row.setContentsMargins(10, 4, 14, 4)
            row.addWidget(_glyph_tile(ICON_BACK, (26, 26), theme.RADIUS_SM,
                                      point_size=9.5, accent=False))
            label = QLabel(str(title or ""))
            label.setStyleSheet(
                f"color: {theme.TEXT_MUTED}; font-weight: 700;"
                f" font-size: 11pt; letter-spacing: 0.6px;"
                f" background: transparent; border: none;")
            row.addWidget(label)
            row.addStretch(1)
            if on_click is not None:
                card.clicked.connect(on_click)
            return card

        if variant == "episode":
            card.setFixedHeight(EPISODE_ROW_HEIGHT)
            row.setContentsMargins(12, 12, 14, 12)
            row.addWidget(self._still_tile(still_url))
        elif variant == "plain":
            # The reading-site chooser: a name and a URL, and nothing to
            # resume - a play tile on "MangaDex" would be a lie.
            card.setFixedHeight(ROW_HEIGHT)
            row.setContentsMargins(18, 10, 14, 10)
        else:
            card.setFixedHeight(ROW_HEIGHT)
            row.setContentsMargins(14, 10, 14, 10)
            # A chapter has no per-chapter art anywhere to fetch, so the
            # play glyph stays - but as the same rounded tile the
            # episode stills are, rather than a bare triangle floating
            # at the row's left edge.
            row.addWidget(_glyph_tile(ICON_PLAY_GLYPH, (44, 44),
                                      theme.RADIUS_SM, point_size=12.0))

        column = QVBoxLayout()
        column.setSpacing(3)
        # Elided, and forced to a left-to-right base direction with a
        # leading LRM mark: an Arabic chapter name makes Qt lay the whole
        # paragraph out right-to-left, and a long one then clipped its
        # *start* - which is where the chapter number sits, so rows
        # showed a name trailing off with no number anywhere (the
        # owner's screenshot). With the base pinned LTR the number stays
        # hard left and the overflow becomes an ellipsis at the right.
        head = _ElidedLabel(_LTR_MARK + str(title or ""))
        head.setStyleSheet(
            f"color: {theme.TEXT}; font-weight: 600; font-size: 12pt;"
            f" background: transparent; border: none;")
        column.addWidget(head)
        if date_text:
            # **Elided, like the title above it, and for a reason that
            # was on screen** (the owner, 25 August 2026: "why is it
            # shifted the Done??"). A plain QLabel with word wrap off
            # reports its *whole string* as its minimum width, and this
            # row lives in a fixed 490px panel: one long meta line -
            # "Apr 6, 2013   ·   TMDB 8.8   ·   Atomic 9.5 (2)" - pushed
            # the scroll host wider than its viewport, and since the
            # host is one width for every row, *every* row's right edge
            # went past the panel and the DONE badge was cut in half.
            #
            # Measured on the real page, one variable at a time, on the
            # owner's own screenshot row (Attack on Titan S01E01) in a
            # 458px viewport:
            #
            #     as shipped (plain label + rate chip)   707px
            #     plain label, no chip                   617px
            #     elided label + chip                    313px
            #     elided label, no chip (now)            223px
            #
            # So the chip was not the cause - the meta line alone was
            # 159px over the panel - and taking the chip off (which the
            # title-level rating did anyway) would not have fixed this.
            date = _ElidedLabel(date_text)
            date.setStyleSheet(
                f"color: {theme.TEXT_MUTED}; font-size: 9.5pt;"
                f" background: transparent; border: none;")
            column.addWidget(date)
        row.addLayout(column, stretch=1)

        if badge:
            kind, text = badge
            row.addWidget(_badge(text, kind))
        if on_click is not None:
            card.clicked.connect(on_click)
        if on_menu is not None:
            card.rightClicked.connect(on_menu)
        return card

    @staticmethod
    def _row_sheet(background, border="transparent",
                   hover_background=None, hover_border=None):
        """A row's own fill and ring, written out in full.

        Both states have to be here. A widget's own stylesheet outranks
        the app one, so setting only a resting colour silently kills the
        #Card[matte] hover rule that gives every row its accent ring -
        the same trap _build_panel records for the scroll area. Written
        with a selector rather than as bare declarations for the other
        half of it: a declaration-only sheet cascades into every child
        label as well.

        Measured after writing it, since that trap has been sprung twice
        here: forcing WA_UnderMouse on a heading and on the back row
        repaints 4950-5415 pixels of each, so the ring is still there.
        The same probe reads 0 for a row *without* its own sheet - but
        so does an untouched Card dropped into the same layout, and
        28366 for the identical card outside the scroll body, so that
        zero is the probe's limit and not a missing ring."""
        hover_background = hover_background or theme.SURFACE_HOVER
        hover_border = hover_border or theme.ACCENT
        return (f"QFrame#Card {{ background: {background};"
                f" border: 1px solid {border};"
                f" border-radius: {theme.RADIUS}px; }}"
                f"QFrame#Card:hover {{ background: {hover_background};"
                f" border: 1px solid {hover_border}; }}")

    def _still_tile(self, url):
        """The episode still, or the tile that stands in for it.

        Registered under a row key rather than an episode number: the
        list is rebuilt from scratch on every search keystroke and on
        every late TMDB rating, so the widget a download started for may
        be gone by the time it lands.

        A still already on disk is drawn here, synchronously - both of
        images' caches are warm for it, so that is a dict lookup and the
        ~0.1ms pixmap conversion, and it saves queueing forty pool jobs
        on every keystroke."""
        tile = _glyph_tile(ICON_PLAY_GLYPH, STILL_SIZE, theme.RADIUS,
                           point_size=13.0, accent=False)
        if not url:
            return tile
        # Read once per fill, not once per row: app_settings has no
        # cache - every getter re-opens and re-parses settings.json - so
        # asking here would be a disk read and a JSON parse per row, 40
        # of them in the batch _queue_rows builds up front.
        blur = self._blur_stills
        ready = _STILL_READY.get((url, blur))
        if ready:
            self._draw_still(tile, ready)
            return tile
        key = self._still_key
        self._still_key += 1
        self._still_tiles[key] = tile
        self._queue_still(key, url, blur)
        return tile

    @staticmethod
    def _draw_still(tile, path):
        pixmap = images.thumbnail_or_avatar(path, "", STILL_SIZE)
        if pixmap.isNull():
            return
        # The tile's own fill has to go with the glyph: _fitted clips
        # every thumbnail to the rounded shape, so a styled square
        # background would show at the four corners of the image.
        tile.setText("")
        tile.setStyleSheet("background: transparent; border: none;")
        tile.setPixmap(pixmap)

    def _on_episode_still(self, key, path):
        if not path:
            # The still's host is refusing (see _still_worker). Keep the
            # tile registered and ask again in STILL_RETRY_MS: download
            # answers None without a request while the refusal holds,
            # so a retry costs a queue trip and nothing on the wire,
            # and the first one after the window clears (600s, or one
            # success against the host) fetches. Measured 3 September
            # 2026 on Attack on Titan, metahub refusing at open and
            # cleared 1.5s later (retry at 1s for the test): all 25
            # rows' stills refused at 0.53s, and the first 12 landed
            # 0.9-2.4s after the retry that found the host clear (a
            # cold connection, three cover workers) - against never.
            if key in self._still_tiles:
                self._still_refused.add(key)
                if not self._still_retry_timer.isActive():
                    self._still_retry_timer.start(self.STILL_RETRY_MS)
            return
        tile = self._still_tiles.pop(key, None)
        self._still_asks.pop(key, None)
        if tile is None:
            return
        try:
            self._draw_still(tile, path)
        except RuntimeError:
            pass            # the row went out under the download

    def _group_card(self, label, count, seeders, open_now, on_click):
        """One resolution heading in the source list.

        **This is the row the owner could not tell from the ones under
        it.** Four things now separate them, and the point is that they
        all pull the same way rather than that any one is decisive:
        it is 18px shorter than a release row; its fill is a translucent
        lift over the panel ground where a row is a solid slab; its
        figures are chips pushed to the right edge where a row's are
        prose running left; and it opens with a caret in the accent
        where a row opens with artwork or a quality tag.

        Open state is the accent's own tinted fill (ACCENT_SOFT - what
        the sidebar's selected item uses), so an open heading reads as
        the thing currently being looked through rather than as one more
        item in the list."""
        card = Card(matte=True, hoverable=True)
        card.setFixedHeight(GROUP_ROW_HEIGHT)
        card.setStyleSheet(self._row_sheet(
            background=(theme.ACCENT_SOFT if open_now
                        else theme.rgba(theme.SURFACE_HOVER, 110)),
            border=(theme.rgba(theme.ACCENT, 120) if open_now
                    else theme.rgba(theme.BORDER, 150))))
        row = QHBoxLayout(card)
        row.setContentsMargins(10, 6, 12, 6)
        row.setSpacing(10)
        # The caret goes gold only when the group is open. Gold on every
        # heading at once was measured against the render as noise
        # rather than signal - four folded headings each carrying an
        # accent chip is four things claiming attention and none of them
        # meaning anything.
        caret = _glyph_tile(
            ICON_CHEVRON_DOWN if open_now else ICON_CHEVRON_RIGHT,
            (26, 26), theme.RADIUS_SM, point_size=9.5, accent=open_now)
        row.addWidget(caret)
        # Kept on the card so a fold can restyle the heading in place
        # rather than rebuilding the list - see _toggle_source_group.
        card._caret = caret
        name = QLabel(str(label))
        name.setStyleSheet(
            f"color: {theme.TEXT}; font-weight: 800; font-size: 12pt;"
            f" letter-spacing: 1px; background: transparent; border: none;")
        row.addWidget(name)
        row.addStretch(1)
        row.addWidget(_pill(f"{count} source{'s' if count != 1 else ''}"))
        if seeders:
            row.addWidget(_pill(f"{seeders} seeders", accent=True))
        card.clicked.connect(on_click)
        return card

    def _style_group_card(self, card, open_now):
        """The heading's open or closed look, applied in place."""
        try:
            card.setStyleSheet(self._row_sheet(
                background=(theme.ACCENT_SOFT if open_now
                            else theme.rgba(theme.SURFACE_HOVER, 110)),
                border=(theme.rgba(theme.ACCENT, 120) if open_now
                        else theme.rgba(theme.BORDER, 150))))
            caret = getattr(card, "_caret", None)
            if caret is not None:
                caret.setText(ICON_CHEVRON_DOWN if open_now else ICON_CHEVRON_RIGHT)
                caret.setStyleSheet(
                    f"color: {theme.ACCENT if open_now else theme.TEXT_MUTED};"
                    f" background: {theme.ACCENT_SOFT if open_now else theme.SURFACE_HOVER};"
                    f" border: none; border-radius: {theme.RADIUS_SM}px;"
                    f" font-family: {theme.FONT_STACK_ICONS}; font-size: 9.5pt;")
        except RuntimeError:
            pass

    def _source_card(self, source, quality, seeders, meta_text, on_click):
        """One release under a heading.

        Indented past the heading's caret, so the fold reads as
        containment rather than as two things in a row, and led by its
        own resolution tag - the heading says it too, but a row scrolled
        away from its heading has to keep saying what it is, and the
        search box matches on it (see _fill_source_rows)."""
        from windows.reader import _ElidedLabel
        card = Card(matte=True, hoverable=True)
        card.setFixedHeight(SOURCE_ROW_HEIGHT)
        row = QHBoxLayout(card)
        row.setContentsMargins(26, 9, 12, 9)
        row.setSpacing(10)

        tag = QLabel(str(quality or "Other").upper())
        tag.setFixedWidth(54)
        tag.setAlignment(Qt.AlignmentFlag.AlignCenter)
        # Borderless, like the pills: with a ring it read as a small
        # empty button sitting where artwork sits on the episode rows.
        tag.setStyleSheet(
            f"color: {theme.TEXT_MUTED};"
            f" background: {theme.rgba(theme.TEXT_MUTED, 26)}; border: none;"
            f" border-radius: {theme.RADIUS_SM}px; padding: 4px 0px;"
            f" font-weight: 700; font-size: 8.5pt;")
        row.addWidget(tag)

        column = QVBoxLayout()
        column.setSpacing(3)
        # **The release's own properties, never the provider's name** -
        # the owner's ask, 23 August 2026: "do not show addon/provider
        # names, setup, or configuration anywhere in the UI ... show users
        # only useful stream info: quality, size, seeders, codec/HDR".
        # Where this used to print "Torrentio" or "Anime Tosho" - which
        # tells the person choosing nothing about what they are choosing -
        # it now prints what the file actually is. `source` is still
        # carried on the stream dict and still used internally (the
        # player remembers a chosen source across episodes); it is simply
        # not shown.
        head = QLabel(_codec_label(source, quality, meta_text))
        head.setStyleSheet(
            f"color: {theme.TEXT}; font-weight: 700; font-size: 11.5pt;"
            f" background: transparent; border: none;")
        column.addWidget(head)
        detail = _ElidedLabel(_LTR_MARK + str(meta_text or ""))
        detail.setStyleSheet(
            f"color: {theme.TEXT_DIM}; font-size: 9.5pt;"
            f" background: transparent; border: none;")
        column.addWidget(detail)
        row.addLayout(column, stretch=1)

        if seeders:
            # Deliberately *not* the accent pill the heading gets: an
            # accent chip on every release turned the whole list gold
            # and left the headings with nothing of their own.
            row.addWidget(_pill(f"{seeders} seeders"))
        card.clicked.connect(on_click)
        return card

    # ---- the source picker ------------------------------------------------
    def _start_episode(self, season, episode):
        """What pressing an episode does. With "Auto choose source to
        play" on (Settings > Playback, the default - the picking step
        was the slow part of starting anything, the owner's ask), the
        player opens straight away and races the best sources itself;
        off restores the source picker. The player's own source and
        resolution panels are the manual override either way."""
        if app_settings.get_auto_pick_source():
            self._play(season, episode)
        else:
            self._open_source_picker(season, episode)

    def _open_source_picker(self, season, episode):
        """Swap the list panel to this episode's sources, best first -
        nothing plays until one is chosen."""
        import threading
        self._source_pick = {"season": season, "episode": episode,
                             "streams": None, "looking": True}
        self._run += 1
        threading.Thread(
            target=_sources_worker,
            args=(self._signals, self._run, dict(self.entry), season, episode),
            daemon=True).start()
        self._search.clear()
        self._sync_season_controls()
        self._fill_rows()

    def _close_source_picker(self):
        self._source_pick = None
        # Back to all-folded for the next episode: an open group is
        # about the list being read now, not a preference.
        self._open_source_groups = set()
        self._search.clear()
        self._sync_season_controls()
        self._fill_rows()

    def _sync_season_controls(self):
        """Hide the season picker and its Prev/Next while the source
        list is up (the owner's ask).

        They steer the *episode* list, and stepping a season under a
        list of sources for one episode of the season you just left is
        an offer that cannot mean anything. The Back row inside the
        source list is the way out of it."""
        picking = self._source_pick is not None
        reading = self._is_reading
        for widget in (self._season_box, self._prev_btn, self._next_btn):
            try:
                widget.setVisible(not picking and not reading)
            except RuntimeError:
                pass        # the panel is being torn down

    def _on_sources(self, run, found, key, looking):
        if run != self._run or self._closed:
            return
        pick = self._source_pick
        if not pick or (pick.get("season"), pick.get("episode")) != tuple(key):
            return
        playable = [s for s in (found or []) if s.get("kind") != "drm"]
        if looking and not playable:
            # A batch carrying only the DRM row is not an answer yet.
            # Storing it would flip the note to "No playable source was
            # found" while the fan-out is still running, and the next
            # batch would flip it back - a panel that says the lookup
            # failed and then unsays it.
            return
        pick["streams"] = playable
        pick["looking"] = bool(looking)
        self._fill_rows()
        # **No speculative warm-up here, and that is deliberate.** A
        # previous version fetched the top row's metadata while the
        # picker was being read, on the theory that the wait was free.
        # It is not: libtorrent's session runs `active_downloads = 8`,
        # and a warmed torrent is *held* - measured 22 August 2026,
        # warming five releases left five of those eight slots occupied,
        # and the race that followed took 15.3s against the 4.9s the same
        # race costs on a clean session. Browsing a few episodes would
        # quietly starve the one being played.

    def _fill_source_rows(self):
        """The sources for the picked episode: a back row, then one
        heading per resolution - 4K first, then 1080p, and so on - with
        that resolution's releases under it, most seeders first.

        **Grouped, and sorted by seeders only inside a group.** This was
        briefly one flat list ordered by seeders across the whole set,
        reading the owner's "from top to bottom based on seeds num" as a
        global order; they asked for the headings back. So the shape is
        the original one and the ordering rule applies within each
        heading, which is what `streams.list_sort_key` is for - the
        addons return roughly sorted rows and `_rank` caps seeders at
        200 outside the preferred resolution, so without it the order
        inside a group is whatever arrived first.

        Seeder counts therefore restart at each heading, by design: on
        House of the Dragon S02E05 the 4K group runs 939 down to 38 and
        the 1080P heading then starts again at 1876. That is the group
        boundary doing its job, not a sort failing.

        The resolution stays on each row as well as in its heading, so a
        row still says what it is when the heading has scrolled off, and
        the search box matches it."""
        self._clear_rows()
        pick = self._source_pick or {}
        season, episode = pick.get("season"), pick.get("episode")
        name = (f"S{int(season or 1):02d}E{int(episode):02d}"
                if episode else "this film")
        self._rows.insertWidget(0, self._row_card(
            "Back to episodes", "", None, self._close_source_picker,
            variant="back"))
        shown = 1

        streams_found = pick.get("streams")
        if streams_found is None:
            self._panel_note.setVisible(True)
            self._panel_note.setText(f"Finding sources for {name}...")
            return
        if not streams_found:
            self._panel_note.setVisible(True)
            self._panel_note.setText(
                f"No playable source was found for {name}. Try again in "
                "a moment.")
            return

        try:
            from helpers import streams as streams_helper
        except Exception:                               # pragma: no cover
            streams_helper = None
        wanted = self._search.text().strip().lower()
        # One bucket per resolution, in the order qualities() ranks them
        # (best first), plus a trailing "" for releases that state none.
        groups = (streams_helper.qualities(streams_found)
                  if streams_helper else [])
        if any(not _quality_label(s.get("quality")) for s in streams_found):
            groups = list(groups) + [""]
        for quality in groups:
            visible = []
            for stream in streams_found:
                if _quality_label(stream.get("quality")) != quality:
                    continue
                # The resolution is searched as well as the source and
                # the release name, so typing "2160" narrows to 4K.
                text = (f"{_quality_label(stream.get('quality'))} "
                        f"{stream.get('source') or ''} "
                        f"{stream.get('title') or ''}").lower()
                if wanted and wanted not in text:
                    continue
                visible.append(stream)
            if not visible:
                continue
            # Most seeders first *within this heading* - the owner's ask.
            if streams_helper is not None:
                visible.sort(key=streams_helper.list_sort_key)
            # **Every resolution is a foldable group, and they all
            # start folded** - the owner's ask, 22 August 2026. A real
            # lookup answers with sixty-odd releases; laid out flat that
            # is a wall to scroll past before the *next* resolution's
            # heading is even reachable. Folded, the panel opens as a
            # short list of resolutions with their counts, which is the
            # choice actually being made first.
            #
            # A search overrides the fold: typing "2160" or a group name
            # is a request to see what matched, and leaving those rows
            # hidden behind a closed heading would read as no results.
            label = ("4K (2160p)" if quality == "2160p"
                     else (quality or "Other").upper())
            open_now = bool(wanted) or quality in self._open_source_groups
            best = max((int(v.get("seeders") or 0) for v in visible), default=0)
            heading = self._group_card(
                label, len(visible), best, open_now,
                (lambda q=quality: self._toggle_source_group(q)))
            self._rows.insertWidget(shown, heading)
            shown += 1
            # **The group's releases live in a body of their own, and
            # the fold is that body's height.** The owner, 6 September
            # 2026, with a recording: "the folding and unfolding of the
            # resolution sources in the ep list page is not smooth at
            # all". A heading click used to call _fill_rows, which
            # throws every row in the panel away and builds every row
            # again - a 1080P group of forty-one releases is forty-one
            # cards, each with its stylesheet and pills, made on the UI
            # thread while nothing moves, and then the list jumps to its
            # new length. Now a body holds a group's rows, is built once
            # on the first open, and the fold animates its maximumHeight
            # (see _toggle_source_group); the heading is restyled in
            # place. A late batch of sources still rebuilds the panel
            # (_on_sources), and _open_source_groups is what survives it.
            body = _GroupBody(quality, visible, self)
            self._rows.insertWidget(shown, body)
            shown += 1
            if open_now:
                body.build()
                body.setMaximumHeight(QWIDGETSIZE_MAX)
            else:
                body.setMaximumHeight(0)
        if shown > 1 and pick.get("looking"):
            # Say that the list is still growing. Not the panel note -
            # that is centred over the viewport and would sit on top of
            # the rows it is talking about. Without it a list that
            # suddenly gains twenty rows reads as a glitch rather than
            # as the slow source finally answering.
            still = QLabel("Still looking for more sources...")
            still.setStyleSheet(
                f"color: {theme.TEXT_DIM}; font-size: 10pt;"
                f" background: transparent; border: none;"
                f" padding: 10px 2px 4px 2px;")
            self._rows.insertWidget(shown, still)
            shown += 1
        self._panel_note.setVisible(shown <= 1)
        if shown <= 1:
            self._panel_note.setText("Nothing matches that search.")

    def _toggle_source_group(self, quality):
        """Open or close one resolution's releases and redraw the list.

        Folded state lives on the page rather than on the widgets, which
        are rebuilt from scratch every time a late source lands (see
        _on_sources) - a fold kept on a row would be lost on the next
        batch, which is the one moment someone is definitely reading
        it."""
        opening = quality not in self._open_source_groups
        if opening:
            self._open_source_groups.add(quality)
        else:
            self._open_source_groups.discard(quality)
        # In place, not a rebuild - see the note in _fill_source_rows.
        heading = body = None
        for index in range(self._rows.count()):
            item = self._rows.itemAt(index)
            widget = item.widget() if item is not None else None
            if isinstance(widget, _GroupBody) and widget.quality == quality:
                body = widget
                previous = self._rows.itemAt(index - 1)
                heading = previous.widget() if previous is not None else None
                break
        if body is None:
            self._fill_rows()
            return
        if heading is not None:
            self._style_group_card(heading, opening)
        body.fold(opening)

    def _play_stream_choice(self, stream):
        """Play exactly the chosen source. The rest of the list rides
        along behind it so a dead pick can still fall through, but the
        chosen one leads - nothing is re-ranked over the user's head."""
        pick = self._source_pick or {}
        season, episode = pick.get("season"), pick.get("episode")
        ordered = [stream] + [s for s in (pick.get("streams") or [])
                              if s is not stream]
        # Leave the picker by the one routine that knows how, rather than
        # clearing _source_pick by hand: picking a source is an exit from
        # it exactly as much as the Back row is.
        #
        # It used to set the field and refill the rows and nothing else,
        # so everything else the picker had switched on stayed on behind
        # the player. Traced through the real flow 22 August 2026 - open
        # details, click an episode, click a source, close the player:
        # _season_box/_prev_btn/_next_btn all came back isVisible=False,
        # because only _sync_season_controls un-hides them and nothing on
        # this path called it. That is the owner's "the season list and
        # the prev/next btns do not show", and only leaving the page and
        # re-entering it cleared the state. Two more leaked the same way:
        # the open resolution group (so the *next* episode's picker
        # opened unfolded, against _fill_source_rows' all-folded rule)
        # and the search text (a "2160" typed to narrow sources was left
        # filtering the episode list underneath).
        self._close_source_picker()
        try:
            from windows import player
            page = player.open_player(self.window(), self.entry,
                                      season=season, episode=episode,
                                      streams=ordered)
            if page is not None:
                page.closed.connect(self._on_overlay_closed)
        except Exception:
            logs.exception("details page could not open the chosen source")
            # Out loud as well as in the log: a swallowed constructor error
            # read as a dead button - the owner's "Play Film does nothing",
            # 24 August 2026, was 31 of these in his own log.
            show_toast(self, "Could Not Open the Player")

    # ---- actions ---------------------------------------------------------
    def _play(self, season, episode):
        try:
            from windows import player
            page = player.open_player(self.window(), self.entry,
                                      season=season, episode=episode)
            if page is not None:
                # Re-badge the rows the moment the player closes: an
                # episode just watched must read WATCHED without leaving
                # and reopening this page.
                page.closed.connect(self._on_overlay_closed)
        except Exception:
            logs.exception("details page could not open the player")
            # Out loud as well as in the log: a swallowed constructor error
            # read as a dead button - the owner's "Play Film does nothing",
            # 24 August 2026, was 31 of these in his own log.
            show_toast(self, "Could Not Open the Player")

    def _read(self, number):
        try:
            from windows import reader
            page = reader.open_reader(self.window(), self.entry,
                                      chapter_number=number)
            if page is not None:
                page.closed.connect(self._on_overlay_closed)
        except Exception:
            logs.exception("details page could not open the reader")

    def _on_overlay_closed(self):
        """A player or reader opened from a row has closed - re-read this
        entry from disk (that is where they wrote progress) and redraw
        the badges."""
        if self._closed:
            return
        try:
            from windows.tracker import _progress_data_file
            fresh = next((e for e in storage.load(_progress_data_file(self.entry), [])
                          if e.get("id") == self.entry.get("id")), None)
            if fresh:
                self.entry.update(fresh)
        except Exception:
            logs.exception("details page could not re-read the entry")
        # The ticks too: playing an episode writes one whether or not
        # this title is saved, and an unsaved title has nothing else to
        # re-read (see helpers/history).
        try:
            self._history_marks = history.watched_keys(self.entry)
        except Exception:
            pass
        if self._is_reading:
            read = self._last_read()
            total = len({c.get("number") for c in self._chapters})
            if self._chapters:
                self._facts.setText(
                    f"{total} chapters"
                    + (f"  ·  read up to {read:g}" if read else ""))
        self._fill_rows()

    # ---- marking watched / read ---------------------------------------
    def _aired_last_episode(self, season) -> int:
        """The last already-aired episode number of `season`, from the
        Cinemeta list this page is already holding."""
        now = datetime.datetime.now(datetime.timezone.utc)
        last = 0
        for video in self._videos:
            if int(video.get("season") or 0) != int(season or 0):
                continue
            aired = _aired(video.get("firstAired") or video.get("released"))
            if aired is not None and aired > now:
                continue
            last = max(last, int(video.get("number")
                                 or video.get("episode") or 0))
        return last

    def _episode_menu(self, event, season, episode):
        """Right-click on an episode row: move the watched mark - one
        episode, or the whole season - in either direction, through
        tracker.correct_progress (the one writer allowed to move a
        number down)."""
        try:
            from windows.tracker import correct_progress
        except ImportError:                             # pragma: no cover
            return
        watched_season, watched_episode = self._progress()
        # **The tick wins where there are ticks.** Either store counts,
        # exactly as the row badge reads them - but the progress number
        # alone says "everything up to here", so with progress at S01E03
        # episode 1 read as watched whatever its own tick said, and the
        # menu never once offered to mark it. The owner: "sometimes the
        # 1st episode cannot be marked as watched/unwatched" - it is the
        # earliest episode of a started season every time, because that
        # is the one the number always covers.
        #
        # So: once this title has per-episode ticks, they are the answer
        # and each episode is independent. A title with none falls back
        # to the number, which is all it has.
        key = history.episode_key(season, episode)
        if self._history_marks:
            already = key in self._history_marks
        else:
            already = bool(watched_episode
                           and (watched_season, watched_episode) >= (season, episode))
        menu = QMenu(self)
        # The explicit way into the source list while auto-pick is on -
        # the left click plays right away then (see _start_episode).
        pick_source = menu.addAction("Choose Source...")
        menu.addSeparator()
        mark = menu.addAction("Mark as Unwatched" if already
                              else "Mark as Watched")
        menu.addSeparator()
        mark_all = menu.addAction("Mark All as Watched")
        clear_all = menu.addAction("Mark All as Unwatched")
        chosen = menu.exec(event.globalPosition().toPoint())
        if chosen is pick_source:
            self._open_source_picker(season, episode)
            return
        if chosen is mark:
            watched = not already
            episodes = [episode]
            if already:
                if episode <= 1:
                    target = ((season - 1, self._aired_last_episode(season - 1))
                              if season > 1 else (0, 0))
                else:
                    target = (season, episode - 1)
            else:
                target = (season, episode)
        elif chosen is mark_all:
            watched, episodes = True, self._season_episodes(season)
            target = (season, max(1, self._aired_last_episode(season)))
        elif chosen is clear_all:
            watched, episodes = False, self._season_episodes(season)
            target = ((season - 1, self._aired_last_episode(season - 1))
                      if season > 1 else (0, 0))
        else:
            return

        # History first, and unconditionally: it is the only store an
        # unsaved title has, and it is what makes a per-episode tick
        # possible at all - the entry's single progress number cannot
        # say "5 and 7 but not 6".
        self._mark_history([history.episode_key(season, number)
                            for number in episodes], watched)
        if self.entry.get("id"):
            if target[1] <= 0:
                # Nothing watched at all: cleared directly, because
                # correct_progress refuses a zero episode.
                self._clear_video_progress()
            elif not correct_progress(self.entry, season=target[0],
                                      episode=target[1]):
                # The tick is already written and is the thing the row
                # draws, so the rows are refreshed either way - returning
                # here left the list showing the old state after a mark
                # that had in fact been saved.
                show_toast(self, "Could Not Save That")
        self._fill_rows()

    def _season_episodes(self, season) -> list:
        """Every episode number this page knows of in `season` - what
        "mark all" ticks. Read off the loaded meta rather than counted,
        so a season with gaps ticks exactly what exists."""
        numbers = []
        for video in self._videos:
            if int(video.get("season") or 0) != int(season or 0):
                continue
            number = int(video.get("number") or video.get("episode") or 0)
            if number:
                numbers.append(number)
        return numbers

    def _mark_history(self, marks, watched: bool):
        """Write ticks and keep the page's cached set in step. The title
        is touched either way, so marking something watched is itself
        enough to put it in History - which is the point for a title
        that was never saved."""
        marks = [m for m in marks if m]
        if not marks:
            return
        try:
            history.set_watched(self.entry, marks, watched)
            if watched:
                self._history_marks.update(marks)
            else:
                self._history_marks.difference_update(marks)
        except Exception:
            logs.exception("details page could not write history")
        # A mark is a deliberate statement of where watching stands, and
        # it must outrank any stored half-watched position - including
        # for an unsaved title, which never reaches correct_progress (no
        # id to write against) but still owns resume records by identity.
        # Without this, Continue reopened the last episode *entered*
        # regardless of the mark (player.clear_entry_resume has the whole
        # story).
        if not self._is_reading:
            try:
                from windows import player
                player.clear_entry_resume(self.entry)
            except Exception:
                logs.exception("details page could not clear resume state")

    def _clear_video_progress(self):
        from helpers import storage
        from windows.tracker import _progress_data_file
        # **`progress_cleared_at` is the statement, not just the empty
        # field.** The owner, 4 September 2026, after the number came
        # back: "when 1st ep season one is marked as unwatched that means
        # that the user did not watch anything in this watchable yet".
        #
        # Emptying `progress` alone cannot hold, because the Stremio sync
        # is forward-only against whatever is stored - and *every* number
        # is forward of nothing, so the next sync writes 51 straight back
        # with progress_verified True (tracker._on_progress_synced). His
        # own log has the app asking for "The Apothecary Diaries S1E52"
        # on a title he had just declared himself at the start of.
        #
        # So the clear leaves a mark, and the sync steps around a title
        # carrying one. It is dropped the moment anything real happens
        # here - playing an episode, or marking one - because
        # tracker._write_progress clears it on every successful write.
        fields = {"progress": "", "progress_verified": False,
                  "progress_cleared_at": storage.now_iso(),
                  "updated_at": storage.now_iso()}
        self.entry.update(fields)
        try:
            storage.update_entry(_progress_data_file(self.entry),
                                 self.entry.get("id"), fields)
        except Exception:
            logs.exception("details page could not clear progress")
        # Same rule as correct_progress: "nothing watched" is a deliberate
        # statement, so no stored position may outrank it.
        try:
            from windows import player
            player.clear_entry_resume(self.entry)
        except Exception:
            logs.exception("details page could not clear resume state")

    def _chapter_menu(self, event, number):
        """Right-click on a chapter row - the reading mirror of
        _episode_menu, against the read-up-to chapter number."""
        try:
            from windows.tracker import correct_progress
        except ImportError:                             # pragma: no cover
            return
        if number is None:
            return
        from windows.reader import chapter_number
        already = bool((self._last_read() and number <= self._last_read())
                       or history.chapter_key(number) in self._history_marks)
        menu = QMenu(self)
        mark = menu.addAction("Mark as Unread" if already else "Mark as Read")
        menu.addSeparator()
        mark_all = menu.addAction("Mark All as Read")
        clear_all = menu.addAction("Mark All as Unread")
        # See _episode_menu: rating moved to the title.
        chosen = menu.exec(event.globalPosition().toPoint())
        numbers = [c_num for c_num in
                   (chapter_number(c) for c in self._chapters)
                   if c_num is not None]
        if chosen is mark:
            read, marked = not already, [number]
            if already:
                earlier = [n for n in numbers if n < number]
                target = max(earlier) if earlier else 0.0
            else:
                target = number
        elif chosen is mark_all:
            read, marked = True, numbers
            target = max(numbers) if numbers else 0.0
        elif chosen is clear_all:
            read, marked = False, numbers
            target = 0.0
        else:
            return

        # See _episode_menu: History is written whether or not this
        # title is saved, and is the only store when it is not.
        self._mark_history([history.chapter_key(n) for n in marked], read)
        if self.entry.get("id") and not correct_progress(self.entry,
                                                         chapter=target):
            show_toast(self, "Could Not Save That")
            return
        read = self._last_read()
        total = len({c.get("number") for c in self._chapters})
        self._set_facts([f"{total} chapters"]
                        + ([f"read up to {read:g}"] if read else []))
        self._fill_rows()

    # ---- downloading ----------------------------------------------------
    def _download_current(self):
        if self._is_reading:
            self._open_chapter_download_dialog()
        else:
            self._open_episode_download_dialog()

    def _dialog_shell(self, title):
        dialog = QDialog(self)
        dialog.setMinimumWidth(520)
        column = QVBoxLayout(dialog)
        column.setContentsMargins(20, 18, 20, 18)
        column.setSpacing(12)
        # With the layout already in place, so the heading lands at its
        # top and the caller's rows append after it.
        frameless_dialog(dialog, title=title)
        return dialog, column

    @staticmethod
    def _dialog_buttons(dialog, column, on_start, extra=()):
        """Cancel, any `extra` buttons, then the accent Download."""
        buttons = QHBoxLayout()
        buttons.addStretch()
        cancel_btn = QPushButton("Cancel")
        use_hover_cursor(cancel_btn)
        cancel_btn.clicked.connect(dialog.reject)
        buttons.addWidget(cancel_btn)
        for button in extra:
            buttons.addWidget(button)
        start_btn = QPushButton("Download", objectName="Accent")
        use_hover_cursor(start_btn)
        start_btn.clicked.connect(on_start)
        buttons.addWidget(start_btn)
        column.addLayout(buttons)
        return start_btn

    def _folder_row(self, column, dialog):
        """The shared saving-to row; returns a callable yielding the
        currently chosen folder."""
        from windows import downloads_page
        state = {"folder": downloads_page.saved_folder()}
        row = QHBoxLayout()
        row.addWidget(QLabel("Saving to:", objectName="Muted"))
        label = QLabel(state["folder"])
        label.setToolTip(state["folder"])
        row.addWidget(label, stretch=1)
        change = QPushButton("Change...")
        use_hover_cursor(change)

        def pick():
            chosen = downloads_page.choose_folder(dialog, state["folder"])
            if chosen:
                state["folder"] = chosen
                label.setText(chosen)
                label.setToolTip(chosen)
        change.clicked.connect(pick)
        row.addWidget(change)
        column.addLayout(row)
        return lambda: state["folder"]

    def _open_episode_download_dialog(self):
        """Which episodes, which audio, where - then queue.

        One dialog instead of the two buttons the owner rightly called
        redundant: scope covers this episode, a chosen range, or the
        whole season."""
        from helpers import downloads
        is_movie = self.entry.get("type") == "Movie"
        season = int(self._season or 1)
        last = self._aired_last_episode(season)
        if not is_movie and last <= 0:
            show_toast(self, "No Aired Episodes to Download in This Season")
            return

        dialog, column = self._dialog_shell(
            "Download Film" if is_movie else "Download Episodes")

        # PickCombo everywhere in this file - see there for the
        # half-second of dropped clicks a plain QComboBox has.
        scope = PickCombo()
        first_box, last_box = PickCombo(), PickCombo()
        if not is_movie:
            scope.addItems(["One Episode", "A Range of Episodes",
                            "The Whole Season"])
            use_hover_cursor(scope)
            scope_row = QHBoxLayout()
            scope_row.addWidget(QLabel(f"Season {season}:"))
            scope_row.addWidget(scope, stretch=1)
            column.addLayout(scope_row)

            # Default to what Continue would play, clamped to aired.
            watched_season, watched_episode = self._progress()
            current = (min(watched_episode + 1, last)
                       if watched_season == season and watched_episode else 1)
            for box in (first_box, last_box):
                box.addItems([f"Episode {n}" for n in range(1, last + 1)])
                box.setCurrentIndex(current - 1)
                use_hover_cursor(box)
            # **One picker when only one thing is being picked**, 28
            # August 2026, the owner: "while the selected is One
            # Episode, do not show From ep/ch to ep/ch, make it only one
            # selection button". "From" and "To" over two identical
            # drop-downs describe a range; with the scope set to a single
            # episode, the second one was disabled and the first was still
            # captioned "From:", which reads as half a range somebody
            # forgot to finish. The caption becomes the plain "Episode:"
            # and the second half of the row goes away entirely.
            from_label = QLabel("From:")
            to_label = QLabel("To:")
            range_row = QHBoxLayout()
            range_row.addWidget(from_label)
            range_row.addWidget(first_box, stretch=1)
            range_row.addWidget(to_label)
            range_row.addWidget(last_box, stretch=1)
            column.addLayout(range_row)

        # Which audio the picked release should carry. A preference over
        # release names (dual-audio/dub tags), not a track switch - see
        # downloads._order_by_audio for what it can and cannot promise.
        #
        # **Anime only**, 28 August 2026, the owner: "only show the Audio
        # selection option while download page while in Anime, and
        # completely remove this button selection from series and
        # movies". It used to offer "Japanese (original)" against
        # "English dub" for everything, which for a live-action series
        # was steering the release pick with a language nothing in it
        # speaks - and there is no second audio to choose between for a
        # title watched in the language it was made in. A row with one
        # real answer is not a choice, so it is not drawn.
        #
        # The value still travels with the job: "orig" means "prefer the
        # ordinary release, not a dub-tagged one", which is the right
        # request for every non-anime title and is what anime's old "jp"
        # always meant (downloads._split_by_audio treats them alike).
        from helpers import stremio as _stremio
        audio_box = PickCombo()
        audio_box.addItem("Japanese (original)", "orig")
        if _stremio.is_anime_entry(self.entry):
            audio_box.addItem("English dub (when a release has one)", "en")
            use_hover_cursor(audio_box)
            audio_row = QHBoxLayout()
            audio_row.addWidget(QLabel("Audio:"))
            audio_row.addWidget(audio_box, stretch=1)
            column.addLayout(audio_row)

        count_label = QLabel("", objectName="Muted")
        count_label.setWordWrap(True)
        column.addWidget(count_label)
        folder_of = self._folder_row(column, dialog)

        # **"Download in Browser"** - the owner's ask, 2 September 2026
        # (roadmap #14): a download at the line's speed rather than the
        # source's. A browser can take a direct http(s) link and nothing
        # else, so this resolves one (downloads.direct_url_for - a
        # release the debrid service holds answered a Range request 206
        # from its CDN, measured) and falls back to the in-app queue,
        # saying so, when only a torrent exists. One episode at a time:
        # a range would be a browser tab per episode.
        browser_btn = QPushButton("Download in Browser")
        browser_btn.setToolTip("Open a direct link in your browser, which "
                               "downloads it at your connection's full speed")
        use_hover_cursor(browser_btn)

        def picked_numbers():
            if is_movie:
                return []
            mode = scope.currentIndex()
            if mode == 0:
                return [first_box.currentIndex() + 1]
            if mode == 2:
                return list(range(1, last + 1))
            low = min(first_box.currentIndex(), last_box.currentIndex()) + 1
            high = max(first_box.currentIndex(), last_box.currentIndex()) + 1
            return list(range(low, high + 1))

        def sync(*_args):
            if is_movie:
                count_label.setText("The film will be queued for download.")
                return
            whole = scope.currentIndex() == 2
            ranged = scope.currentIndex() == 1
            one = len(picked_numbers()) == 1
            browser_btn.setEnabled(one)
            browser_btn.setToolTip(
                "Open a direct link in your browser, which downloads it "
                "at your connection's full speed" if one else
                "One episode at a time - a range would open a browser "
                "tab per episode")
            last_box.setEnabled(ranged)
            # Hidden rather than merely disabled: a greyed-out "To:" is
            # still a caption saying a range is being chosen, and a
            # greyed-out "Episode: 4" under "The Whole Season" is a
            # number that has nothing to do with what will be queued.
            from_label.setText("From:" if ranged else "Episode:")
            from_label.setVisible(not whole)
            first_box.setVisible(not whole)
            to_label.setVisible(ranged)
            last_box.setVisible(ranged)
            count = len(picked_numbers())
            count_label.setText(
                f"{count} episode{'s' if count != 1 else ''} of season "
                f"{season} will be queued.")
        scope.currentIndexChanged.connect(sync)
        first_box.currentIndexChanged.connect(sync)
        last_box.currentIndexChanged.connect(sync)
        sync()

        def start():
            numbers = picked_numbers()
            audio = audio_box.currentData()
            try:
                if is_movie:
                    downloads.queue_episode(self.entry, audio=audio,
                                            folder=folder_of())
                elif len(numbers) == 1:
                    downloads.queue_episode(self.entry, season=season,
                                            episode=numbers[0], audio=audio,
                                            folder=folder_of())
                else:
                    if not confirm(
                            dialog, "Download Episodes",
                            f"Queue {len(numbers)} episodes of season "
                            f"{season} for download?"):
                        return
                    downloads.queue_season(self.entry, season=season,
                                           episodes=numbers, audio=audio,
                                           folder=folder_of())
            except Exception:
                logs.exception("details page could not queue a download")
                show_toast(self, "Could Not Queue That Download")
                dialog.reject()
                return
            dialog.accept()
            show_toast(self, "Queued - See the Downloads Page")

        def in_browser():
            numbers = picked_numbers()
            if not is_movie and len(numbers) != 1:
                return          # the button is disabled for a range
            dialog.accept()
            self._open_episode_in_browser(
                season=None if is_movie else season,
                episode=None if is_movie else numbers[0],
                audio=audio_box.currentData(), folder=folder_of())
        browser_btn.clicked.connect(in_browser)

        self._dialog_buttons(dialog, column, start, extra=[browser_btn])
        dialog.exec()

    def _open_episode_in_browser(self, *, season, episode, audio, folder):
        """Resolve a direct link on a watched worker, then hand it to the
        system browser - or queue the episode in Atomic, saying why,
        when only the swarm can serve it. The sticky toast goes up at
        once (rule 7): the lookup is a stream fan-out (6.9s cold on
        Reacher S1E2) plus the debrid round trips (0.70s)."""
        bridge = getattr(self, "_browser_link_bridge", None)
        if bridge is None:
            bridge = self._browser_link_bridge = _BrowserLinkBridge(self)
            bridge.resolved.connect(self._on_browser_link)
        request = {"entry": dict(self.entry), "season": season,
                   "episode": episode, "audio": audio, "folder": folder}
        self._browser_link_toast = show_toast(
            self, "Finding a Direct Link...", duration_ms=None)
        lookup_pool.submit_watched(_browser_link_worker, bridge, request)

    def _on_browser_link(self, result, request):
        from helpers import downloads
        from helpers.widgets import finish_toast
        toast = getattr(self, "_browser_link_toast", None)
        self._browser_link_toast = None
        url = str((result or {}).get("url") or "")
        if url:
            # The URL carries an account token: opened, never logged.
            import webbrowser
            webbrowser.open(url)
            finish_toast(toast, self, "Opened in Your Browser")
            return
        try:
            downloads.queue_episode(request["entry"], season=request["season"],
                                    episode=request["episode"],
                                    audio=request["audio"],
                                    folder=request["folder"])
        except Exception:
            logs.exception("details page could not queue a download")
            finish_toast(toast, self, "Only a Torrent Is Available - "
                                      "Could Not Queue It")
            return
        finish_toast(toast, self, "Only a Torrent Is Available - "
                                  "Queued in Atomic Instead", duration_ms=4000)

    def _open_chapter_download_dialog(self):
        """Which chapters - one, or a chosen range - then queue as .cbz.
        The same shape the reader's own download dialog has."""
        from helpers import downloads
        from windows.reader import chapter_number, chapter_title, is_arabic
        if not self._chapters:
            show_toast(self, "The Chapter List Is Still Loading")
            return
        # One language, like the reader's dialog: the list arrives
        # grouped by language, and a numeric range across the join holds
        # the same chapter twice - the second file overwrites the first.
        anchor_lang = str(self._chapters[0].get("lang") or "").lower()
        arabic = [c for c in self._chapters if is_arabic(c)]
        candidates = arabic or [
            c for c in self._chapters
            if str(c.get("lang") or "").lower() == anchor_lang]
        if not candidates:
            candidates = list(self._chapters)

        dialog, column = self._dialog_shell("Download Chapters")
        scope = PickCombo()
        scope.addItems(["One Chapter", "A Range of Chapters"])
        use_hover_cursor(scope)
        scope_row = QHBoxLayout()
        scope_row.addWidget(QLabel("Download:"))
        scope_row.addWidget(scope, stretch=1)
        column.addLayout(scope_row)

        # Default to the chapter being read, when it is in the list.
        read = self._last_read()
        current = next((i for i, c in enumerate(candidates)
                        if chapter_number(c) == read), 0)
        labels = [chapter_title(c) for c in candidates]
        first_box, last_box = PickCombo(), PickCombo()
        for box in (first_box, last_box):
            box.addItems(labels)
            box.setCurrentIndex(current)
            use_hover_cursor(box)
            box.view().setMinimumWidth(420)
            box.view().setFont(box.font())
        # **One picker when only one thing is being picked**, 28
        # August 2026, the owner: "while the selected is One
        # Episode, do not show From ep/ch to ep/ch, make it only one
        # selection button". "From" and "To" over two identical
        # drop-downs describe a range; with the scope set to a single
        # chapter, the second one was disabled and the first was still
        # captioned "From:", which reads as half a range somebody
        # forgot to finish. The caption becomes the plain "Chapter:"
        # and the second half of the row goes away entirely.
        from_label = QLabel("From:")
        to_label = QLabel("To:")
        range_row = QHBoxLayout()
        range_row.addWidget(from_label)
        range_row.addWidget(first_box, stretch=1)
        range_row.addWidget(to_label)
        range_row.addWidget(last_box, stretch=1)
        column.addLayout(range_row)

        count_label = QLabel("", objectName="Muted")
        count_label.setWordWrap(True)
        column.addWidget(count_label)
        # No "Download in Browser" here, and the label says why in the
        # owner's terms (roadmap #14, 2 September 2026): a reading site
        # hands out its page images only to a request carrying the
        # chapter page as Referer (3asq answers 403 without it - see
        # chapter_source.chapter_pages), and a browser link cannot carry
        # one. The speed went into the fetch instead: six pages at a
        # time (downloads._run_chapter), measured 5.95-10.73s to
        # 2.66-2.87s on a 21-page chapter.
        why = QLabel("Chapters are saved by Atomic itself, six pages at a "
                     "time: the site only hands out pages to a request "
                     "carrying the app's own headers, so a browser link "
                     "would be refused.", objectName="Muted")
        why.setWordWrap(True)
        column.addWidget(why)
        folder_of = self._folder_row(column, dialog)

        def picked():
            if scope.currentIndex() == 0:
                return [candidates[first_box.currentIndex()]]
            low = min(first_box.currentIndex(), last_box.currentIndex())
            high = max(first_box.currentIndex(), last_box.currentIndex())
            # Reversed: the list runs newest first, and a range should
            # land on disk oldest first so the queue reads in order.
            return list(reversed(candidates[low:high + 1]))

        def sync(*_args):
            ranged = scope.currentIndex() == 1
            last_box.setEnabled(ranged)
            from_label.setText("From:" if ranged else "Chapter:")
            to_label.setVisible(ranged)
            last_box.setVisible(ranged)
            count = len(picked())
            count_label.setText(
                f"{count} chapter{'s' if count != 1 else ''} will be saved "
                f"as .cbz file{'s' if count != 1 else ''}.")
        scope.currentIndexChanged.connect(sync)
        first_box.currentIndexChanged.connect(sync)
        last_box.currentIndexChanged.connect(sync)
        sync()

        def start():
            chapters = picked()
            if len(chapters) > 1 and not confirm(
                    dialog, "Download Chapters",
                    f"Queue {len(chapters)} chapters for download?"):
                return
            try:
                downloads.queue_chapters(self.entry, chapters,
                                         folder=folder_of())
            except Exception:
                logs.exception("details page could not queue chapters")
                show_toast(self, "Could Not Queue Those Chapters")
                dialog.reject()
                return
            dialog.accept()
            show_toast(self, "Queued - See the Downloads Page")

        self._dialog_buttons(dialog, column, start)
        dialog.exec()

    # ---- saving and unsaving --------------------------------------
    def _sync_save_button(self):
        """Put the one button into the face this entry calls for.

        Saved entries get the destructive face, and it is deliberately
        *not* #Accent: this is the largest button on the page and the
        first thing a thumb lands on, and dressing "delete a tracked
        title" in the same green as the page's primary action is how a
        mis-tap loses somebody's progress. Nothing else about it moves -
        same place, same height - so it still reads as the same control
        having changed its mind."""
        saved = bool(self.entry.get("id"))
        hovering = bool(getattr(self, "_save_hover", False))
        # **A tick when it is saved, a plus when it is not, and the bin
        # only under the pointer.** The owner, 4 September 2026: "in the
        # ch/ep list pages in the save button make it shows
        # correct-icon like the banner in the discover page if saved and
        # + icon when not saved, and make in both ch/ep list page and
        # the discover page banner when hover and it is saved make the
        # correct-icon turns into bin icon".
        #
        # So the resting state says what *is* - saved, or not - and the
        # destructive reading appears only when the pointer is on the
        # thing that would do it. That also fixes a smaller wrong note:
        # a bin sitting on the page permanently made "you have this"
        # look like a warning.
        self._save_btn.setText(
            ("Remove From My List" if hovering else "Saved to My List")
            if saved else "Save to My List")
        # A QIcon, not a glyph in the label: a QPushButton paints its
        # text in one font, and the app font has no U+E74D - the bin
        # would be a hollow box beside the words. widgets.glyph_icon
        # renders it in the Fluent font at the screen's own ratio.
        if saved:
            glyph = TRASH_GLYPH if hovering else CHECK_GLYPH
            # The bin reads on the red ground, so it takes the same
            # colour the words do - see the objectName below.
            self._save_btn.setIcon(glyph_icon(
                glyph, theme.ON_ACCENT if hovering else theme.TEXT))
            self._save_btn.setIconSize(QSize(TRASH_ICON_SIZE, TRASH_ICON_SIZE))
        else:
            self._save_btn.setIcon(glyph_icon(PLUS_GLYPH, theme.ON_ACCENT))
            self._save_btn.setIconSize(QSize(TRASH_ICON_SIZE, TRASH_ICON_SIZE))
        # **And it goes red under the pointer.** The owner, 4 September
        # 2026, with a picture of this button hovered: "make it goes red
        # when hover like the discover banner". The banner has done
        # exactly this since the day before (`.hero .act.danger`, which
        # fills with --danger and darkens its text) while this one only
        # swapped its words and its icon and kept the ordinary surface -
        # so the two surfaces the same ask was made about disagreed.
        #
        # Only while hovered, and that is still the rule the note above
        # states: a resting saved title says what *is*, and the
        # destructive reading appears on the control that would do it.
        # #Danger is the app's one red button - the same style the
        # selection bar's bin wears - so this needed no new colour.
        self._save_btn.setObjectName(
            ("Danger" if hovering else "") if saved else "Accent")
        # A QSS objectName change is not re-applied on its own - the
        # style has already been computed for the old name. Measured the
        # hard way once on the sidebar: unpolish/polish is what makes it
        # take, and the repaint after it is what makes it show.
        self._save_btn.style().unpolish(self._save_btn)
        self._save_btn.style().polish(self._save_btn)
        self._save_btn.update()

    def _toggle_saved(self):
        """The button's one handler: keep it, or stop keeping it."""
        if self.entry.get("id"):
            self._unsave_entry()
        else:
            self._save_entry()

    def _unsave_entry(self):
        """Take this title back out of its tracker file.

        The whole record is copied before it goes and offered back on an
        undo toast, rather than guarded by a confirm dialog. That is the
        house pattern for a removal from a list (tracker._delete_entry,
        games._remove), and it is the right one here for a reason of its
        own: this is a *toggle*, and a modal question in the middle of a
        toggle is worse than an offer to put it back - an entry carries
        far more than its title (progress, cover, the cached schedule and
        its checked-at stamp, resolved site ids), so "just save it again"
        is not the same thing as undoing.

        The page behind the overlay drops the row when this one closes -
        see tracker._on_inapp_closed, which had no "it is gone from disk"
        branch until this button could make one happen."""
        if not self.entry.get("id"):
            return
        # The file work is remove_from_library (module scope, so the
        # Discover banner can call it too); what is left here is the
        # page's own half - the button's face and the undo offer.
        data_file, index, removed = remove_from_library(self.entry)
        if removed is None:
            if data_file is None:
                show_toast(self, "Could Not Remove That")
                return
            # Nothing to remove - another surface deleted it while this
            # overlay sat open, so the button is wrong, not the file.
            self._sync_save_button()
            return
        self._sync_save_button()
        title = removed.get("title") or "that"
        show_undo_toast(
            self, f"Removed '{title}' From Saved - Click to Undo",
            lambda rec=removed, at=index, f=data_file:
                self._restore_entry(rec, at, f))

    def _restore_entry(self, record, index, data_file):
        """Put an unsaved record back where it was, with everything it
        carried. Clamped, because another surface can have shortened the
        list while the toast was up."""
        try:
            entries = storage.load(data_file, [])
            if not any(e.get("id") == record.get("id") for e in entries):
                entries.insert(min(index, len(entries)), record)
                storage.save(data_file, entries)
            self.entry["id"] = record.get("id")
            history.link_entry(self.entry)
            self._sync_save_button()
        except Exception:
            logs.exception("details page could not restore the entry")
            return "Could Not Restore That"
        return f"Restored '{record.get('title') or 'that'}'"

    def _save_entry(self):
        """Write this title into its tracker file.

        The id stamped here is what flips every "is it saved" check in
        the app, this button's own face included; the page that opened
        this one adopts the new row when the overlay closes.

        **This docstring used to claim the id reaches the caller's own
        dict because it is "written in place rather than onto a copy".
        It is not: `__init__` does `self.entry = dict(entry or {})`, so
        the stamp lands on this page's copy and the opener's dict never
        sees it.** Believed rather than checked, that sentence cost a
        real bug - a title saved off Discover stayed invisible in Saved
        until the page was rebuilt, because tracker's overlay-close hook
        read the id off the dict that never gained one, and worse, an
        unadopted row of the page's own type is what _save_entries reads
        as "this page deleted it". The hook now falls back to reading
        `page.entry["id"]` off the overlay itself (see
        tracker._wire_overlay_refresh); the copy stays, because sharing
        the dict would let a page the user never saved write through to
        the grid behind it."""
        if self.entry.get("id"):
            return
        # The file work is save_to_library (module scope, so the Discover
        # banner can call it too): it stamps the id, the status and the
        # two timestamps onto this dict and links the History row.
        data_file = save_to_library(self.entry)
        if not data_file:
            show_toast(self, "Could Not Save That")
            return
        # Not "Saved to My List" and disabled any more - the button
        # stays live and turns into the way back out (_sync_save_button).
        self._sync_save_button()
        show_toast(self, f"'{self.entry.get('title')}' Added to Saved")
        # A reading title picked off Discover can arrive with no
        # cover_url at all (the Madara search shape names none) - its
        # own card resolved the art from the series page, so the save
        # does the same through page_url rather than storing an entry
        # that stays coverless on Saved and Home forever (the owner's
        # Kingdom (WAN) report).
        page_url = (self.entry.get("url") or "") if self._is_reading else ""
        title = self.entry.get("title") or ""
        # A reading title with neither art nor a page still has a
        # name, and the catalogues answer to that (cover_for_title).
        if self.entry.get("cover_url") or page_url or (title and self._is_reading):
            lookup_pool.submit(self._saved_cover_worker, self._signals,
                               self.entry["id"], data_file,
                               self.entry.get("cover_url") or "", page_url,
                               title if self._is_reading else "")

    @staticmethod
    def _saved_cover_worker(signals, entry_id, data_file, url, page_url="",
                            title=""):
        """Download the poster the Discover row carried - or, for a
        reading title that never carried one, the cover its series page
        names - off the UI thread; the write happens back on it. Never
        raises."""
        resolved = ""
        try:
            from helpers import manga_sites
            if not url and page_url:
                found = manga_sites.fetch_manga_details(
                    page_url, title=title) or {}
                url = resolved = found.get("cover_url") or ""
            if not url and title:
                url = resolved = manga_sites.cover_for_title(title) or ""
            path = images.download(url) if url else None
        except Exception:
            path = None
        if path:
            signals.saved_cover.emit(entry_id, data_file, resolved, str(path))

    def _on_saved_cover(self, entry_id, data_file, url, path):
        fields = {"cover_path": path}
        if url:
            # The worker had to resolve the URL itself - record it, so
            # the tracker's sharper-cover backfill has something to
            # re-derive from later.
            fields["cover_url"] = url
        try:
            storage.update_entry(data_file, entry_id, fields)
        except Exception:
            logs.exception("details page could not record the cover")
        if self.entry.get("id") == entry_id:
            self.entry.update(fields)

    # How long the page is given before it warms the sources it expects
    # to be asked for. Not zero: the open is already spending its first
    # moments on Cinemeta, the artwork and the episode list, and a
    # six-way stream fan-out started in the same breath competes with
    # the three things actually on screen.
    SOURCE_PREFETCH_DELAY_MS = 700

    def _prefetch_sources(self):
        """Warm `find_streams` for the episode Continue would play.

        **Measured 24 August 2026, Attack on Titan S01E05, the owner's
        own addon list.** Pressing an episode costs the fan-out before
        anything can be raced: 0.65-0.71s to the first batch of rows and
        1.97-4.16s to the finished list (the spread is one slow addon -
        WatchHub answered 0 rows in 2.44s on one run). All of that is
        HTTP the page could have paid while it was being looked at, and
        `streams._RESULT_CACHE` keys on exactly (entry, season, episode)
        for 15 minutes, so a warmed answer makes the press free.

        A/B on the real page, same title, same list of 79 rows:

            cold press-to-sources    4850ms
            warmed by this page         0ms

        Deliberately *not* the torrent race: warming that would mean
        joining swarms for something nobody has pressed. This is the
        cheap half - the addons and the indexers, one fan-out, once per
        open.

        Everything about it is soft. It picks the part-watched episode
        if there is one (`player.resume_point` - the same target the
        Continue button uses) and otherwise the next one after stored
        progress, which is what `PlayerPage._starting_episode` would
        land on; a wrong guess costs one fan-out and caches nothing,
        since an empty answer is never stored."""
        if self._is_reading or self._prefetch_started:
            return
        self._prefetch_started = True

        def worker(entry, run):
            try:
                from helpers import streams as streams_module
                from windows import player as player_module
                season = episode = None
                point = player_module.resume_point(entry)
                if point:
                    season, episode = point
                else:
                    stored_season, stored_episode = self._progress()
                    if stored_episode:
                        season, episode = stored_season, stored_episode + 1
                    elif entry.get("type") != "Movie":
                        season, episode = 1, 1
                if run != self._run or self._closed:
                    return
                streams_module.find_streams(entry, season=season,
                                            episode=episode)
            except Exception:
                logs.exception("details source prefetch failed")

        QTimer.singleShot(
            self.SOURCE_PREFETCH_DELAY_MS,
            lambda run=self._run: (
                None if (self._closed or run != self._run)
                else threading.Thread(target=worker,
                                      args=(dict(self.entry), run),
                                      daemon=True).start()))

    def _continue(self):
        """Resume where watching/reading stopped - the page's primary
        action, and with the cards' round button gone, the only one.
        Through open_tracker_entry so an entry the in-app player cannot
        serve still falls back to its browser route."""
        try:
            from windows import tracker
            tracker.open_tracker_entry(self, self.entry, resume=True)
        except Exception:
            logs.exception("details page could not continue the entry")

    def _on_inapp_closed(self, _entry_id):
        """tracker._wire_overlay_refresh's hook - a player or reader
        opened by Continue has closed; same refresh as a row's own."""
        self._on_overlay_closed()

    # ---- painting ----------------------------------------------------------
    def paintEvent(self, event):
        super().paintEvent(event)
        if self._backdrop is None:
            return
        painter = QPainter(self)
        rect = self.rect()
        # Scaled once per size, not per paint: backdrops are the
        # full-resolution originals now (artwork.BACKDROP_SIZE), and
        # smooth-scaling several megapixels on every repaint would make
        # hovering the list stutter.
        #
        # Cut at devicePixelRatio and tagged with it, or the ground is
        # rendered at logical size and stretched by Qt on any non-100%
        # display - which is what made the chapter list's background
        # look soft (the owner's report; the same trap HeroBanner had,
        # and .claude/rules/ui.md states it plainly). The ratio is part
        # of the cache key, so dragging the window to a differently
        # scaled monitor re-cuts rather than reusing the other one's.
        ratio = self.devicePixelRatioF() or 1.0
        if (self._backdrop_scaled is None
                or self._backdrop_size != (rect.size(), ratio)):
            # **Scaled to the width, not to cover the whole page.**
            # Covering meant taking whichever of width/height needed the
            # bigger factor, and for reading that is always the height:
            # AniList's manga banners are 1900x400 (measured - Kingdom
            # 1900x400, Solo Leveling 1900x492) and there is no larger
            # asset, so filling a 1600x900 page upscaled them 2.25x.
            # That is the owner's "the read ep list bg image is blurry".
            #
            # Width-scaling changes nothing for the video pages, whose
            # TMDB backdrops are 16:9 and land on exactly the same
            # rectangle they always did (1920x1080 -> 1600x900 is the
            # same factor either way, and still a *downscale*). A short
            # banner is now drawn sharp across the top instead, with the
            # scrim below it doing what it already does.
            target = QSize(max(1, int(rect.width() * ratio)),
                           max(1, int(rect.height() * ratio)))
            source = self._backdrop.size()
            if source.height() > source.width():
                # **A portrait image is never stretched across the page.**
                # Measured over the owner's reading titles, 21 August
                # 2026: most resolve to a real AniList banner (One Piece,
                # Kingdom, Hunter x Hunter, Rise of the Fallen Kingdom's
                # all 1900x400), but a title AniList knows only as a
                # *cover* comes back portrait - Celebrity Lady at
                # 460x652 - and the cover-fill below then blew it up 2.5x
                # and cropped it to a letterbox strip of somebody's chin.
                # That is the owner's "some reading ch list bg uses an
                # image as the wrong size".
                #
                # So a portrait cover becomes a deliberately blurred
                # ground instead, which is what every media app does with
                # one and what the scrim over it already assumes. Blurred
                # by scaling down hard and back up - Qt has no blur
                # without QGraphicsEffect, and an effect on the page's
                # ground would repaint on every hover.
                # **32x, up from 24.** A portrait cover has to cover a
                # 1600x900 page, which blows a ~460px-wide image up more
                # than 3x before any blur is applied, so the old divisor
                # left recognisable faces across the whole window - see
                # the note on SCRIM for the photograph that showed it. At
                # 32 the source is 50px wide before it is blown back up,
                # which is the colour-and-shape wash a ground should be.
                small = self._backdrop.scaled(
                    max(1, target.width() // 32), max(1, target.height() // 32),
                    Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                    Qt.TransformationMode.SmoothTransformation)
                # **Back up in two steps, not one.** A single 50px -> 1600px
                # smooth scale still leaves visible square blocks - it was
                # photographed doing exactly that - because Qt's filter
                # samples a neighbourhood that is tiny relative to the jump.
                # An intermediate pass gives the second scale something
                # already smooth to interpolate, which is what turns the
                # blocks into an actual blur. Two passes, not more: the
                # third was not distinguishable from the second.
                middle = small.scaled(
                    max(1, target.width() // 6), max(1, target.height() // 6),
                    Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                    Qt.TransformationMode.SmoothTransformation)
                self._backdrop_scaled = middle.scaled(
                    target, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                    Qt.TransformationMode.SmoothTransformation)
            elif self._backdrop_is_ground:
                # Already blurred by hero_art - fill the page. Cropped
                # to the page's aspect *before* the smooth scale: a
                # 1900x400 ground expanded to cover 1600x900 was being
                # scaled to 4275x900 and then mostly discarded - 2.7x the
                # pixels actually drawn. Measured 23 August 2026: 5.7ms ->
                # 2.5ms at DPR 1, 13.7 -> 6.9 at DPR 2, on every first
                # paint and every resize.
                src = self._backdrop
                want_w = max(1, src.height() * target.width() // max(1, target.height()))
                if want_w < src.width():
                    left = (src.width() - want_w) // 2
                    src = src.copy(left, 0, want_w, src.height())
                self._backdrop_scaled = src.scaled(
                    target, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                    Qt.TransformationMode.SmoothTransformation)
            else:
                covers = (source.height() * target.width()
                          >= source.width() * target.height())
                self._backdrop_scaled = self._backdrop.scaled(
                    target,
                    (Qt.AspectRatioMode.KeepAspectRatioByExpanding if covers
                     else Qt.AspectRatioMode.KeepAspectRatio),
                    Qt.TransformationMode.SmoothTransformation)
            self._backdrop_scaled.setDevicePixelRatio(ratio)
            self._backdrop_size = (rect.size(), ratio)
        scaled = self._backdrop_scaled
        # Centred on the pixmap's *logical* size - width()/height() are
        # device pixels once it carries a ratio.
        width = scaled.width() / ratio
        height = scaled.height() / ratio
        # Anchored to the top when it is shorter than the page: the art
        # belongs behind the title, and centring a 337px band in a 900px
        # page floats it in the middle of nothing.
        top = 0 if height < rect.height() else int((rect.height() - height) / 2)
        left = int((rect.width() - width) / 2)
        # **Rounded, not square into the window's corners** - the owner's
        # ask ("make sure the edges of the top result / featured / main
        # page are all rounded"). Measured before changing it: the
        # details page's own top-left and top-right pixels carried
        # artwork (58,58,40 and 58,26,17) while its bottom two were the
        # page ground - so this was the one large surface in the app
        # still meeting a corner squarely. The hero banners were already
        # right (their corner pixels read as BG, checked the same way).
        #
        # Painted as a *brush through a path*, not setClipPath: Qt's
        # raster clip is applied per whole pixel and is not antialiased,
        # which is exactly how HeroBanner's artwork kept hard corners
        # under a rounded panel (widgets.py carries the same note, from
        # the same screenshot).
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        brush = QBrush(scaled)
        transform = QTransform()
        transform.translate(left, top)
        # **No 1/ratio scale here, and that line was the owner's "on my
        # 2K monitor the image did not fit".** It used to read "the
        # brush tiles in device pixels, so the pixmap has to be scaled
        # back down". Measured 24 August 2026 with a four-quadrant
        # marker texture (200 device px, DPR 2) painted as a path brush
        # into a DPR-2 target: the tile repeated every 200 *device*
        # pixels - i.e. every 100 logical, the texture's own logical
        # size. Qt's raster brush already applies the pixmap's
        # devicePixelRatio; the extra scale applied it twice.
        #
        # What that looked like on his LG 27GS950 (2560x1440 at 125%,
        # DPR 1.25): the backdrop was cut to 2560x1440 device = 2048x1152
        # logical, exactly the page - then drawn at 1638x922, and the
        # brush *tiled* the rest. Reproduced offscreen at that geometry
        # before the fix: a hard seam at device row 1152 (= 1152/1.25 =
        # 921.6 logical) with the top of the picture starting again
        # underneath it. At DPR 1.0 the branch never ran, which is why
        # it survived every earlier screenshot.
        brush.setTransform(transform)
        # **Intersected with the artwork's own rectangle, or the brush
        # tiles.** A QBrush repeats to fill whatever path it is given,
        # and a 1900x400 banner scaled to a 1500px-wide page is 316px
        # tall against 900 - so the first attempt drew One Piece three
        # times down the page and twice across (caught by looking at the
        # render, not by the corner pixels, which were correct). Filling
        # only where the art actually is leaves the rest to the page's
        # own ground, exactly as the plain drawPixmap did.
        shape = QPainterPath()
        shape.addRoundedRect(QRectF(rect), theme.RADIUS_LG, theme.RADIUS_LG)
        art = QPainterPath()
        art.addRect(QRectF(left, top, width, height))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(brush)
        painter.drawPath(shape.intersected(art))
        gradient = QLinearGradient(0.0, 0.0, 0.0, float(rect.height()))
        for stop, red, green, blue, alpha in SCRIM:
            gradient.setColorAt(stop, QColor(red, green, blue, alpha))
        painter.fillRect(rect, gradient)
        painter.end()

    # ---- lifetime ----------------------------------------------------------
    def follow(self, host):
        """Track the host's size - the same overlay contract the reader
        documents (there is no layout to join over hand-positioned
        pages)."""
        self._host = host
        host.installEventFilter(self)
        self.setGeometry(host.rect())

    def eventFilter(self, obj, event):
        if obj is getattr(self, "_host", None) and event.type() == QEvent.Type.Resize:
            self.setGeometry(obj.rect())
        # The Save/Remove button's own hover, so the tick can become a
        # bin under the pointer - see _sync_save_button.
        if obj is getattr(self, "_save_btn", None):
            if event.type() == QEvent.Type.Enter:
                self._save_hover = True
                self._sync_save_button()
            elif event.type() == QEvent.Type.Leave:
                self._save_hover = False
                self._sync_save_button()
        return super().eventFilter(obj, event)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.leave()
            return
        # **R re-asks for the list**, 28 August 2026, the owner's ask -
        # the same thing the header's refresh button does, which is the
        # key the reader has always used for the same idea. Safe beside
        # the search field without a focus check: Qt hands a key to the
        # focused widget first and a QLineEdit consumes its own letters,
        # so this is only ever reached when the list itself has focus.
        if event.key() == Qt.Key.Key_R and not event.modifiers():
            self._refresh_list()
            return
        super().keyPressEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.BackButton:
            self.leave()
            event.accept()
            return
        super().mousePressEvent(event)

    def leave(self):
        if self._closed:
            return
        self._closed = True
        self._run += 1
        host = getattr(self, "_host", None)
        if host is not None:
            host.removeEventFilter(self)
        self.closed.emit()
        self.hide()
        self.deleteLater()


class _GenreBrowseSignals(QObject):
    rows = Signal(str, object)      # section label, catalog rows
    poster = Signal(int, str)       # row key, local poster path
    done = Signal()


# The genre page's grid: the tracker's own poster size, so a genre
# browse and Discover are visibly the same surface. Written out rather
# than imported from windows.tracker - that module imports this one at
# call time and a top-level import each way is a cycle.
_BROWSE_POSTER_SIZE = (160, 216)
# Nine across (the owner's ask): at six the grid left a wide empty gutter
# on the right of a maximised window, so the rows read as half-filled.
# The *most* columns worth drawing, not the number always drawn. The
# grid takes its column count from the viewport now (see
# GenreBrowsePage._columns_for): nine fixed columns is 9*180 + 8*12 =
# 1716px of cards, and on the owner's 1873px window the sidebar leaves
# about 1633px - so the last column sat off the right edge and the page
# could only be read by scrolling sideways (their report).
# Enough to fill those rows rather than leave the last one ragged.
_BROWSE_LIMIT = 36

# One genre's rows, kept for the session: (genre, kind) -> rows. A genre
# is one press away from every details page, and a catalog's answer does
# not move inside a sitting - re-asking made every visit pay the full
# round trip again. Bounded, because browsing can touch many.
_GENRE_CACHE = {}
_GENRE_CACHE_MAX = 24

# And the same rows on disk, so the first open of a session is as fast
# as the second (the owner, 23 August 2026: "the same genre page
# loading takes ~5-10 sec make it <500 ms" - the session cache above
# never survived a restart, so "the same page" paid the full fetch
# every launch). A day matches the discover browse cache's own disk
# TTL; these listings are most-followed orderings that move slower
# than that.
_GENRE_DISK_FILE = "genre_cache.json"
_GENRE_DISK_TTL_S = 24 * 60 * 60
_GENRE_DISK_MAX = 40


def _genre_disk_load(kind, genre):
    """Rows for one (kind, genre) from disk, or None past the TTL.
    Never raises - a missing or corrupt cache is a cold open."""
    try:
        stored = storage.load(_GENRE_DISK_FILE, {}) or {}
        entry = stored.get(f"{kind}|{genre.strip().lower()}")
        if not isinstance(entry, dict):
            return None
        if time.time() - float(entry.get("at", 0)) > _GENRE_DISK_TTL_S:
            return None
        rows = entry.get("rows")
        return [r for r in rows if isinstance(r, dict)] if rows else None
    except Exception:
        return None


def _genre_disk_store(kind, genre, rows):
    """Best-effort write-through, oldest keys pruned past the bound."""
    try:
        stored = storage.load(_GENRE_DISK_FILE, {}) or {}
        if not isinstance(stored, dict):
            stored = {}
        stored[f"{kind}|{genre.strip().lower()}"] = {
            "at": time.time(), "rows": list(rows)}
        while len(stored) > _GENRE_DISK_MAX:
            oldest = min(stored, key=lambda k: stored[k].get("at", 0)
                         if isinstance(stored[k], dict) else 0)
            stored.pop(oldest, None)
        storage.save(_GENRE_DISK_FILE, stored)
    except Exception:
        pass


class GenreBrowsePage(GlassPage):
    """Everything else filed under one genre - a full page, the shape
    Discover has (the owner's ask: "a page like discovery, not a small
    window"). Opened over the window like the details page and the
    reader, with the same Back/Escape way out.

    Video genres come from Cinemeta's own genre catalogs (series and
    movies, a row each); reading genres from MangaDex's tag browse. A
    poster opens that title's details page over this one, the same
    transient-entry road a Discover card takes."""

    closed = Signal()

    def __init__(self, genre, is_reading, host):
        super().__init__(parent=host)
        self._genre = str(genre)
        self._is_reading = bool(is_reading)
        self._closed = False
        # One painted grid (helpers/poster_grid) instead of sections of
        # Card widgets in a scroll area - the same conversion the
        # category pages and the reader strip got, for the same measured
        # reason: moving child widgets through Qt's scroll machinery
        # costs ~14ms a frame on the owner's machine (70/s against the
        # panel's 240), and this page was the one grid still doing it -
        # his "fix the low fps in the same genre pages while
        # scrolling!!". The Series/Movies section headings become a
        # neutral badge on each card's head; the rows land appended in
        # arrival order.
        self._items = []          # grid index -> catalogue row
        self.open_title = None    # set by the caller

        column = QVBoxLayout(self)
        column.setContentsMargins(28, 20, 28, 20)
        column.setSpacing(14)

        header = QHBoxLayout()
        header.setSpacing(12)
        # GlyphButton for the same reason the refresh control is one -
        # see _build_panel: a text glyph on a QSS button inherits the
        # app-wide padding and clips to a box.
        back = GlyphButton(ICON_BACK, "Back", size=(40, 40))
        back.clicked.connect(self.leave)
        header.addWidget(back)
        heading = QLabel(self._genre, objectName="PanelTitle")
        header.addWidget(heading)
        header.addStretch(1)
        column.addLayout(header)

        # Deliberately no inline stylesheet on the host or the area: the
        # declaration-only sheet that used to sit here cascaded into
        # every descendant and outranked the app stylesheet, so these
        # poster Cards never painted their #Card hover ring - the one
        # visual where Discover's identical cards did (the owner's
        # report). theme.py's QScrollArea/#Bare rules keep the ground
        # transparent without reaching the children.
        self._grid = PosterGrid(_BROWSE_POSTER_SIZE, ground=theme.BG,
                                parent=self)
        self._grid.clicked.connect(self._on_grid_pick)
        self._grid.needs_cover.connect(self._on_grid_needs_cover)
        column.addWidget(self._grid, stretch=1)

        self._note = QLabel(f"Looking for {self._genre} titles...")
        self._note.setWordWrap(True)
        self._note.setStyleSheet(
            f"color: {theme.TEXT_MUTED}; background: transparent; border: none;")
        column.addWidget(self._note)

        self._signals = _GenreBrowseSignals()
        self._signals.rows.connect(self._on_rows)
        self._signals.poster.connect(self._on_poster)
        self._signals.done.connect(self._on_done)
        self._got_rows = False
        import threading
        threading.Thread(target=self._fetch_worker, daemon=True).start()

    # ---- the overlay contract, same as DetailsPage --------------------
    def follow(self, host):
        self._host = host
        host.installEventFilter(self)
        self.setGeometry(host.rect())

    def eventFilter(self, obj, event):
        if obj is getattr(self, "_host", None) and event.type() == QEvent.Type.Resize:
            self.setGeometry(obj.rect())
        return super().eventFilter(obj, event)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.leave()
            return
        super().keyPressEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.BackButton:
            self.leave()
            return
        super().mousePressEvent(event)

    def leave(self):
        if self._closed:
            return
        self._closed = True
        host = getattr(self, "_host", None)
        if host is not None:
            host.removeEventFilter(self)
        self.closed.emit()
        self.hide()
        self.deleteLater()

    # ---- filling it ---------------------------------------------------
    def _fetch_worker(self):
        """Fill the page, asking every catalog at once.

        **The two video catalogs used to be asked one after the other**,
        in a loop, so opening a genre cost their *sum* - and each is a
        full Cinemeta round trip. They go out together now and each row
        is drawn the moment it lands, the same shape
        streams.find_streams and subtitles.search already use here.

        Answers are cached per (genre, kind) for the session, so
        re-opening a genre - one press away from every details page -
        costs no request at all. Never raises; `done` clears the
        looking-note whatever happened."""
        import threading
        from helpers import discover

        def one(kind, label):
            key = (self._genre, kind)
            rows = _GENRE_CACHE.get(key)
            if rows is None:
                # Disk before network (measured 23 August 2026: the
                # session cache never survived a restart, so "the same
                # genre page" cost the full 5.5s fetch every launch;
                # from here it costs a JSON read).
                rows = _genre_disk_load(kind, self._genre)
            if rows is None and kind == "reading":
                # Second no-network answer: the category sections'
                # cached browse filtered by the genre verdicts their
                # classification sweep already stored. On the owner's
                # machine this held a full page for every common genre
                # (41 rows Action, 34 Fantasy, 30 Drama) against the
                # 2.3-5.5s the live browse costs warm - and the live
                # path reads the same sites the cache mirrors, so a
                # genre thin here is thin there too. Not written to
                # the disk cache: it re-derives from caches that
                # refresh themselves, so storing a copy would only
                # freeze it for a day. [] falls through - a cold
                # machine still gets the real browse.
                try:
                    rows = list(discover.reading_genre_cached(
                        self._genre, limit=_BROWSE_LIMIT)) or None
                except Exception:
                    rows = None
            if rows is None:
                try:
                    # **Reading genres come from the owner's own sites**
                    # (the owner's ask, 22 August 2026) - not MangaDex's
                    # tag browse, whose cards can only ever open the
                    # "where should this be read from" flow because they
                    # carry no site url. See
                    # discover.reading_genre_sites, which is the same
                    # browse the Manga/Manhwa/Manhua sections use.
                    rows = (discover.reading_genre_sites(
                                self._genre, limit=_BROWSE_LIMIT)
                            if kind == "reading" else
                            discover.discover_video(kind, genre=self._genre,
                                                    limit=_BROWSE_LIMIT))
                    rows = list(rows or [])
                except Exception:
                    logs.exception("genre browse lookup failed")
                    rows = []
                if rows:
                    _genre_disk_store(kind, self._genre, rows)
            if rows:
                if len(_GENRE_CACHE) >= _GENRE_CACHE_MAX:
                    _GENRE_CACHE.clear()
                _GENRE_CACHE[key] = list(rows)
            try:
                self._signals.rows.emit(label, list(rows))
            except RuntimeError:
                pass        # the page closed under the fetch

        jobs = ([("reading", "")] if self._is_reading
                else [("series", "Series"), ("movie", "Movies")])
        threads = [threading.Thread(target=one, args=job, daemon=True)
                   for job in jobs]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        try:
            self._signals.done.emit()
        except RuntimeError:
            pass

    def _on_rows(self, label, rows):
        if self._closed or not rows:
            return
        self._got_rows = True
        first = len(self._items)
        records = []
        for item in rows:
            row = dict(item)
            row["_badge"] = str(label or "")
            self._items.append(row)
            title = (row.get("title") or "").strip()
            bits = [_year_text(row.get("year"))]
            rating = str(row.get("imdbRating") or "").strip()
            records.append({
                "title": title,
                "year": "  ·  ".join(b for b in bits if b),
                "rating": f"★ {rating}" if rating else "",
                "badge": str(label or ""),
                "saved": False,
                "pixmap": None,
            })
        self._grid.append_items(records)

    def _on_grid_pick(self, index):
        if 0 <= index < len(self._items):
            self._pick(dict(self._items[index]))

    def _on_grid_needs_cover(self, index):
        if not (0 <= index < len(self._items)):
            return
        url = self._items[index].get("poster") or ""
        if not url:
            return
        lookup_pool.submit(self._poster_worker, self._signals, index, url)

    @staticmethod
    def _poster_worker(signals, key, url):
        # Never raises - the pool's worker thread dies silently otherwise.
        try:
            path = images.download(url)
            if path:
                # Decoded on the worker; the slot only converts a cached
                # tile (the split every cover path here uses).
                images.warm(path, tuple(_BROWSE_POSTER_SIZE))
        except Exception:
            path = None
        if path:
            try:
                signals.poster.emit(key, str(path))
            except RuntimeError:
                pass

    def _on_poster(self, key, path):
        if self._closed or not (0 <= key < len(self._items)):
            return
        title = (self._items[key].get("title") or "").strip()
        try:
            self._grid.set_cover(key, images.thumbnail_or_avatar(
                path, title, _BROWSE_POSTER_SIZE))
        except RuntimeError:
            pass          # the page closed under the download

    def _on_done(self):
        if self._closed:
            return
        if self._got_rows:
            self._note.setVisible(False)
        elif self._is_reading:
            # Named for what it actually browsed. A reading genre is
            # searched across the *configured* sites and nowhere else
            # (the owner's ask - see discover.reading_genre_sites), so
            # "check the connection" would be the wrong thing to blame
            # when the honest answer is that none of those sites' current
            # listings are filed under this genre.
            self._note.setText(
                f"None of your reading sites have anything under "
                f"{self._genre} right now.")
        else:
            self._note.setText(f"Nothing was found under {self._genre}. "
                               "Check the connection and try again.")

    def _pick(self, item):
        callback = self.open_title
        if callable(callback):
            callback(item, item.get("type")
                     or ("Manga" if self._is_reading else "Series"))


def open_details(window, entry, host=None):
    """Put the details page over `window`. The one entry point - the same
    central-widget host trick reader.open_reader documents, so the
    sidebar is covered rather than hidden.

    `host` overrides where it lands, and exists for one caller. The
    genre and cast pages are drawn in web_reader's shell, which takes
    `immersive_host` (the central widget); the details page's own host
    is `overlay_host`, the row *under* the app's bar - and that row is a
    **child** of the central widget, so a details page opened from one
    of those pages was a sibling underneath the shell and could not be
    seen until the shell closed. The owner, 3 September 2026: *"the
    genre and the cast pages still do not open on clicking the cards,
    instead if I click one it will take me to it when I click the back
    button"* - it had opened every time, behind.
    """
    if host is None:
        host = (window.overlay_host() if hasattr(window, "overlay_host")
                else (window.centralWidget() if hasattr(window, "centralWidget")
                      else window))
    host = host if host is not None else getattr(window, "container", window)
    # **The video child starts while the list is being read.** The
    # player's own prewarm ran after its handle was created, so the
    # first play of a session still paid the spawn on the UI thread
    # (3.03s in the frozen build - review, 3 September 2026). A details
    # page for something watchable is the earliest honest signal that a
    # play is coming; the child costs a resident process and nothing
    # else, and mpv_proxy.start joins it if it is still connecting.
    try:
        kind = str((entry or {}).get("type") or "").strip().lower()
        if kind in ("anime", "series", "movie", "movies"):
            from helpers import mpv_proxy
            mpv_proxy.prewarm()
    except Exception:
        pass
    page = DetailsPage(entry, host)
    page.follow(host)
    page.show()
    page.raise_()
    freeze_covered(page)   # see widgets._CoveredFreeze
    page.setFocus()
    return page


# ---- the library, as two functions rather than two methods ----------
#
# **Extracted 3 September 2026 so the Discover banner could save.** The
# owner asked for "Save to My List" / "Remove from My List" on that
# banner (web_pages._list_action), and the only implementation of either
# was inside DetailsPage._save_entry / _unsave_entry - bound to a page
# with a button to re-face, a toast to show and an undo to offer. A
# second copy of the file work is exactly the shape that produces a
# saved row none of the rest of the app recognises, so the file work is
# here and both surfaces call it.
#
# What stays on the page: the button's face, the toast wording, and the
# undo offer, because an undo has to know which page to put the row back
# on. What moves here: the id, the status, the stamps, the append, and
# history.link_entry - the five things that make a dict a saved entry.


def _note_change():
    """Every page that is looking redraws - helpers/changes."""
    try:
        from helpers import changes
        changes.bump()
    except Exception:
        pass


def _seed_progress_from_history(entry, reading):
    """Copy History's position onto a fresh entry, when it has one and
    the entry does not. Never raises."""
    try:
        record = history.get(entry) or {}
        shown = str(record.get("progress") or "").strip()
        if not shown:
            return
        if reading:
            if entry.get("last_watched_chapter"):
                return
            number = shown[3:] if shown.lower().startswith("ch ") else shown
            entry["last_watched_chapter"] = float(number)
        else:
            if entry.get("progress"):
                return
            from windows.tracker import parse_episode_progress
            season, episode = parse_episode_progress(shown)
            if not episode:
                return
            entry["progress"] = shown
            entry["progress_verified"] = True
    except Exception:
        logs.exception("could not seed a saved entry's progress from History")


def save_to_library(entry):
    """Write `entry` into its tracker file. Returns the file it landed
    in, or "" if it could not be written.

    `entry` is stamped in place: id, status, added_at, updated_at - the
    caller's dict gains the id, which is what every "is it saved" check
    in the app reads.
    """
    if not isinstance(entry, dict) or entry.get("id"):
        return ""
    try:
        from windows.tracker import _progress_data_file
        data_file = _progress_data_file(entry)
        stamp = storage.now_iso()
        entry["id"] = str(uuid.uuid4())
        # MANGA_TYPES is this module's own list and what
        # DetailsPage._is_reading is computed from, so the status a
        # banner save writes is the status the page would have written.
        reading = entry.get("type") in MANGA_TYPES
        entry.setdefault("status", "Reading" if reading else "Watching")
        entry["added_at"] = stamp
        entry["updated_at"] = stamp
        # **What History already knows about this title is the entry's
        # starting point.** The owner, 6 September 2026: "when I watch
        # any watchable and reach e.g. ep 9 THEN add to saved, it says
        # S01E01 until something is watched or marked". The player, the
        # reader and the tracker all write the position to
        # history.touch(progress=...) whether or not the title is saved,
        # so a save that started from nothing was throwing that away.
        # Verified, because it is what was actually watched - never the
        # tracker's guess at the latest episode out.
        _seed_progress_from_history(entry, reading)
        # A plain append of a fresh read: update_entry cannot create,
        # and any tracker page open behind an overlay re-reads the file
        # the moment the overlay closes.
        entries = storage.load(data_file, [])
        entries.append(dict(entry))
        storage.save(data_file, entries)
    except Exception:
        logs.exception("could not save an entry to the library")
        entry.pop("id", None)
        return ""
    # History and the tracker are one story about the same title - a row
    # already in History gains the new entry's id rather than sitting
    # there looking unsaved forever.
    history.link_entry(entry)
    _note_change()
    return data_file


def remove_from_library(entry):
    """Take `entry` out of its tracker file.

    Returns (data_file, index, the record as it was) so a caller can
    offer to put it back. Two ways it can decline, told apart because a
    caller has different words for them: `("", None, None)` is "there
    was nothing to remove" (another surface got there first - the
    button is wrong, not the file) and `(None, None, None)` is "the
    write failed", which is already in the log. `entry` loses its id,
    and History is unlinked with it.
    """
    entry_id = (entry or {}).get("id")
    if not entry_id:
        return "", None, None
    try:
        from windows.tracker import _progress_data_file
        data_file = _progress_data_file(entry)
        entries = storage.load(data_file, [])
        index = next((i for i, e in enumerate(entries)
                      if e.get("id") == entry_id), None)
        if index is None:
            # Already gone - another surface deleted it. The caller's
            # dict is what is wrong, not the file.
            entry.pop("id", None)
            return "", None, None
        removed = copy.deepcopy(entries.pop(index))
        storage.save(data_file, entries)
    except Exception:
        logs.exception("could not remove an entry from the library")
        return None, None, None
    _note_change()
    entry.pop("id", None)
    # link_entry writes `entry.get("id") or None`, so calling it with the
    # id gone is what unlinks the History row - History and the tracker
    # stay one story in both directions.
    history.link_entry(entry)
    return data_file, index, removed
