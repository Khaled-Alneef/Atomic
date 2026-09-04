"""Atomic's Home and Discover, over HTTP, with no Qt in it.

Reads the same JSON files the app writes and hands them to the page as
plain rows. Nothing here is a view, and nothing here knows what a card
looks like.

Served over http:// deliberately. Every origin problem that cost days on
the QtWebEngine attempt - covers refused from an opaque origin, a page
the scheme handler could not answer - simply does not arise when a page
and its images share one ordinary origin.
"""

import io
import json
import pathlib
import re
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import backend

DATA = backend.DATA_DIR
COVERS = DATA / "image_cache"

if getattr(sys, "frozen", False):
    # PyInstaller unpacks datas to _MEIPASS; __file__ points inside the
    # archive, where nothing can be opened.
    STATIC = pathlib.Path(sys._MEIPASS) / "static"
else:
    STATIC = pathlib.Path(__file__).resolve().parent / "static"

MAGIC = {"image/png": b"\x89PNG\r\n\x1a\n", "image/jpeg": b"\xff\xd8"}

# What discover_cache.json calls each section, and what to call it on
# screen. The file is a *dictionary* keyed by these names, each holding
# {at, rows} - reading it as a list walks the keys instead, which are
# strings, so every row is filtered out and the page shows nothing
# against 1,287 cached titles. That was the bug.
DISCOVER_SECTIONS = (
    ("anime", "Anime"),
    ("series", "Series"),
    ("movie", "Movies"),
    ("reading_latest", "Latest chapters"),
    ("reading", "Reading"),
    ("medium:Manga", "Manga"),
    ("medium:Manhwa", "Manhwa"),
    ("medium:Manhua", "Manhua"),
    ("medium:Other", "Other"),
)

# Per section, not per page. The cache holds well over a thousand rows,
# and a strip that long is a thousand pictures fetched for a row nobody
# scrolls sideways.
DISCOVER_LIMIT = 60


def _rows(name):
    try:
        rows = json.loads((DATA / name).read_text(encoding="utf-8-sig"))
    except Exception:
        return []                # a lost list, never a lost page
    return [r for r in rows if isinstance(r, dict)]


def _history(kinds):
    """Watch or read history, newest first.

    series.json holds only what has been *saved* - three entries on his
    machine against 41 in History - so a page built from saved entries
    alone looks empty while he has plainly been watching things.
    """
    wanted = tuple(k.lower() for k in kinds)
    rows = [r for r in _rows("history.json")
            if str(r.get("type", "")).lower() in wanted]
    rows.sort(key=lambda r: str(r.get("last_opened") or ""), reverse=True)
    return rows


# Every title's watched marks, rebuilt only when history.json changes.
# _row is called once per card and a page can be five hundred of them, so
# reading the file per card would be five hundred reads of the same list.
_MARKS = {"at": None, "index": {}}


def _marks():
    try:
        stamp = (DATA / "history.json").stat().st_mtime
    except OSError:
        stamp = None
    if _MARKS["at"] != stamp:
        found = {}
        for row in _rows("history.json"):
            marks = [str(k) for k in (row.get("watched") or []) if k]
            if not marks:
                continue
            # The row's own progress travels with its marks - see
            # _last_mark for the one case where it outranks them.
            said = str(row.get("progress") or "").strip()
            for key in (str(row.get("entry_id") or ""),
                        str(row.get("title") or "").strip().lower()):
                if key:
                    found[key] = (marks, said)
        _MARKS["index"] = found
        _MARKS["at"] = stamp
    return _MARKS["index"]


def _last_mark(marks, kind="", said=""):
    """**The episode or chapter he is on, not the one he finished.**

    The owner, 3 September 2026: *"make the ep and season number on the
    cards on main page, saved and all pages display the progress, shows
    the ep you are watching right now not the one you finished!"* - so
    the furthest tick plus one, which is where Continue on the same card
    already takes him (web_pages._next_chapter_index does exactly this
    at the other end, one index *newer* than the furthest read).

    A tick means finished: history.set_watched is called when the reader
    opens a chapter and when the player crosses its watched threshold.
    So "S01E05 ticked" and "showing S01E05" disagreed with the ring
    beside it, which opened E6 - the number said the past and the button
    meant the present.

    The furthest tick is what the next one is counted from, and the two
    shapes are told apart the way history writes them: episodes as
    `season:number` (history.episode_key) and chapters as `c<number>`.
    Compared as numbers, never as text - "c964" against "c1185" sorts
    the wrong way as text and 1185 is the one he has read.

    A season boundary is not guessable here (nothing in history.json
    says how many episodes a season has), so E+1 is offered inside the
    same season; the player corrects itself the moment the next episode
    is actually opened and writes its own mark.
    """
    episodes, chapters = [], []
    for mark in marks:
        if ":" in mark:
            season, _, number = mark.partition(":")
            try:
                episodes.append((int(season), int(number)))
            except ValueError:
                pass
        elif mark[:1] in ("c", "C"):
            try:
                chapters.append(float(mark[1:]))
            except ValueError:
                pass
    # **An episode number for something watched, a chapter for something
    # read - never the other.** Found 3 September 2026 on a screenshot of
    # Discover from the source run: the *anime* One Piece card read "1999
    # Ch 1191", which is the chapter of the manga he is reading. `_marks`
    # is indexed by lowercased title as well as by id, and "one piece" is
    # one string for two works on two different sides of the app, so the
    # anime card inherited the manga's ticks.
    #
    # The shape of the mark is what tells them apart, and the entry
    # already knows which shape it wants - the same rule
    # web_pages._find applies to the other half of this collision (a
    # reading card that opened the anime's details page). A video entry
    # with only chapter marks against its name now says nothing, which
    # is the honest answer.
    reading = str(kind or "").strip().lower() in READING_KINDS
    # **A season-0 mark is not a position and is dropped here.** The
    # owner, 4 September 2026, testing Jujutsu Kaisen: marking episode 1
    # showed S01E02 and unmarking it showed a number out of nowhere.
    # Measured on that title's own history row, live: its ticks are
    # `0:1 0:2 0:3 0:4 0:6 0:7 0:8` plus the `1:1` he had just added -
    # seven marks whose season is 0 because the release they were written
    # from named none (history.episode_key stores 0 then). Taking the max
    # over all of them gave (1,1) while `1:1` was there and (0,8) the
    # moment it was not, which is the whole of "where did that come
    # from?". A mark with no season cannot be placed against one that has
    # one, so it does not get a vote.
    episodes = [(s2, n) for s2, n in episodes if s2 >= 1]
    if episodes and not reading:
        season, number = max(episodes)
        # **A tick list is only a position when it runs from the start.**
        # The owner, 4 September 2026: "when I selected the 1st ep as
        # watched then as unwatched the issue happened again!!"
        #
        # Unmarking episode 1 *clears* the entry's progress
        # (details._clear_video_progress, because correct_progress
        # refuses a zero episode), so this fallback is asked again - and
        # with fifty older ticks still on the row it answered S01E51 for
        # a title he had just declared himself at the start of.
        #
        # `max + 1` says "where you are" only if everything below it is
        # ticked too; a set with holes is a set of episodes, which is
        # what history.py says it is. So the run is checked, and a
        # broken one offers nothing rather than a number nobody meant.
        run = {n for s2, n in episodes if s2 == season}
        if len(run) != number or min(run) != 1:
            return ""
        # **A season of 0 is "no season", and it loses to what the entry
        # says about itself.** The owner, 4 September 2026, twice: first
        # a card reading **S00E52**, then the same card reading **E52**
        # with "I am watching ep 1 maybe this is why!".
        #
        # history.episode_key writes `season:number` and stores 0 when
        # the release named no season, so such a mark carries a number
        # with nothing to anchor it - and measured over his own
        # history.json, exactly **one** of the twelve rows that have
        # episode marks has them in this shape, with fourteen of them
        # against a stored progress of S01E01. A number nothing agrees
        # with is worse than the entry's own answer, so `said` wins when
        # it has one; a row with no progress of its own still gets the
        # episode, which is all there is to give.
        return f"S{season:02d}E{number + 1:02d}"
    if chapters and (reading or not kind):
        # ":g" so 1185.0 reads "1186" and a half chapter (1185.5) reads
        # "1186.5" rather than being rounded into a chapter that is not
        # the next one.
        return f"Ch {max(chapters) + 1:g}"
    return ""


def _marked_progress(entry):
    """This entry's furthest tick, or "" if it has none.

    The id is asked first and the title second: an id names one work,
    while a title can name two (see _last_mark for the anime that wore
    the manga's chapter number). The entry's own type is carried through
    so a mark of the wrong shape is not offered at all.
    """
    index = _marks()
    kind = str(entry.get("type") or "")
    for key in (str(entry.get("id") or entry.get("entry_id") or ""),
                str(entry.get("title") or "").strip().lower()):
        if key and key in index:
            marks, said = index[key]
            # The entry's own progress, if it has one, else the history
            # row's - either is an answer a season-0 mark must beat.
            found = _last_mark(marks, kind,
                               str(entry.get("progress") or "").strip() or said)
            if found:
                return found
    return ""


_SAID_RE = re.compile(r"^[Ss](\d+)[Ee](\d+)$")


def _one_on(said, reading):
    """The number a card shows, given the furthest **finished** one.

    Two facts that were not lined up before, and the whole of "readings
    progress ch in the main page cards do not reflect real" (the owner,
    4 September 2026):

      * an entry's stored `progress` is what was last *opened or ticked*
        - tracker._write_progress is called by the reader with the
          chapter it opened and by the mark menu with the one marked, and
          details' own facts line calls it "read up to";
      * `_last_mark` reports the furthest tick **plus one**, which is his
        rule from 3 September: "shows the ep you are watching right now
        not the one you finished".

    So the same shelf printed Kingdom as "Ch 884" off the entry and
    "Ch 885" off its ticks - one card, one number, two meanings. Both
    are finished-positions; this is the single place either becomes the
    one he is on.

    **S01E01 is the not-started state and is shown as itself**, his ask
    the same day, with a screenshot: "when 1st ep season one is marked as
    unwatched that means that the user did not watch anything in this
    watchable yet so stop showing these random numbers and just show
    S01E01 and make it start there when resuming". So S01E01 is what a
    card says when nothing has been watched (see _progress_text), and a
    *stored* S01E01 - episode one watched - reads S01E02, which is the
    episode he is on. The two states are one apart and both say a true
    thing, which is what the earlier "show nothing at all" could not.

    A season boundary is not guessable here (nothing stored says how
    many episodes a season has), so E+1 stays inside the season - the
    same rule, and the same reason, as _last_mark's.
    """
    said = str(said or "").strip()
    if not said:
        return ""
    if reading or said.replace(".", "", 1).isdigit():
        try:
            number = float(said.split()[-1].lstrip("Cch "))
        except (TypeError, ValueError):
            return said if said.lower().startswith("ch") else f"Ch {said}"
        if number <= 0:
            return ""
        return f"Ch {number + 1:g}"
    found = _SAID_RE.match(said)
    if not found:
        return said                 # a shape nothing here writes - left alone
    season, number = int(found.group(1)), int(found.group(2))
    if season <= 0 or number <= 0:
        return ""
    return f"S{season:02d}E{number + 1:02d}"


def _starts_at_one(entry):
    """Whether "nothing confirmed" on this entry means S01E01.

    True for any anime or series **this app has a record of** - a saved
    entry, a History row, a schedule row it opened. What it excludes is a
    catalogue row: a Discover card carries a title, a poster and an
    imdb_id and nothing else, and must not be given a progress line it
    never had. A film has no episode to be on, and a game, app or website
    is not a watchable at all - both go through this same function.

    Widened from "saved only" on 4 September 2026, because the two cards
    he photographed reading S01E52 and S01E05 are **not saved** - neither
    is in series.json - so the saved-only gate left them on exactly the
    tick-and-guess path the rule exists to end.
    """
    if str(entry.get("type") or "").strip().lower() not in ("anime", "series"):
        return False
    return bool(entry.get("id") or entry.get("entry_id") or entry.get("key")
                or entry.get("last_opened") or entry.get("progress"))


# The saved entries, by id and by lowercased title, rebuilt only when
# one of the two files moves. _row is called once per card and a
# catalogue page is hundreds of them, so resolving a twin per card
# against _rows() would be two file reads each - 1800 on a full Discover
# page. Same shape, and the same reason, as _MARKS above.
_TWINS = {"at": None, "watch": {}, "read": {}}


def _twin_index():
    stamps = []
    for name in ("series.json", "tracker.json"):
        try:
            stamps.append((DATA / name).stat().st_mtime)
        except OSError:
            stamps.append(None)
    stamp = tuple(stamps)
    if _TWINS["at"] != stamp:
        for side, name in (("watch", "series.json"), ("read", "tracker.json")):
            found = {}
            for saved in _rows(name):
                for key in (str(saved.get("id") or ""),
                            str(saved.get("title") or "").strip().lower()):
                    if key:
                        found.setdefault(key, saved)
            _TWINS[side] = found
        _TWINS["at"] = stamp
    return _TWINS


def _saved_twin(entry):
    """The saved row this one is a copy of, by id or by title, or None.

    A History row, a schedule row and a Home card can all name the same
    work, and only one of them is the store that gets written when
    progress moves. Resolving to it here is what stops the same title
    reading one number in History and another on Home - and stops the
    live patch (which sends every row through _progress_text, see
    _progress_now) disagreeing with the card it lands on.

    Kept to the entry's own side: "One Piece" is a manga he reads and an
    anime he does not have, and a title is one string for both (see
    _saved_titles for the third face of that collision).
    """
    side = _row_side(str(entry.get("type") or ""))
    if not side:
        return None
    index = _twin_index()[side]
    for key in (str(entry.get("id") or entry.get("entry_id") or ""),
                str(entry.get("title") or "").strip().lower()):
        if key and key in index:
            return index[key]
    return None


def _progress_text(entry):
    """The number under a card: **the one Continue will open.**

    That is the whole rule now, and it is the one the owner keeps asking
    for in different words - 3 September, "shows the ep you are watching
    right now not the one you finished", and 4 September, with a
    screenshot of two cards reading S01E52 and S01E05: "when 1st ep
    season one is marked as unwatched that means that the user did not
    watch anything in this watchable yet so stop showing these random
    numbers and just show S01E01 and make it start there when resuming".

    So the card is written against player._starting_episode, which is
    what actually opens when he presses the round button:

        confirmed S01E02  -> plays S01E03  -> the card says S01E03
        nothing confirmed -> plays S01E01  -> the card says S01E01

    Three things it therefore does **not** do, each of which produced one
    of his reports:

      * **it does not read a tick list as a position.** Unmarking episode
        1 clears the entry's own number (details._clear_video_progress),
        and the fallback to ticks then answered with every older mark
        still on the row - 51 of them on one card, 4 on the other, which
        is his S01E52 and S01E05 exactly. `_starting_episode` never looks
        at ticks, so neither does this.
      * **it does not state an unconfirmed number as fact.** An
        unverified `progress` is a lookup's guess at the latest episode
        *aired* (tracker._progress_display says so, and refuses it too),
        and `_starting_episode` refuses it as well - it plays S01E01.
      * **it does not read a reading entry's `progress`.** For manga that
        field holds the *site's newest chapter*; the chapter he has read
        is `last_watched_chapter` (tracker._write_progress). Measured on
        his own file: Kingdom `progress` 884 against `last_watched_chapter`
        883, Survival 302 against 295, The Eternal Supreme no progress at
        all against 558 - which is why nothing he did to a chapter ever
        moved that number.

    Every row is resolved to its saved twin first, so the same title
    cannot read one number in History and another on Home.
    """
    entry = _saved_twin(entry) or entry
    kind = str(entry.get("type") or "").strip().lower()
    # A film has no episode to be on - tracker.UNTRACKED_TYPES, and the
    # Qt card returns "" for one. Said here because the rule below hands
    # a video row S01E01 when nothing is confirmed, and every film in
    # History carries a stored "S01E01" that means only "opened".
    if kind == "movie":
        return ""
    if entry.get("show_last_watched") is False:
        return ""                   # his own per-entry tick in Add/Edit

    if kind in READING_KINDS:
        chapter = entry.get("last_watched_chapter")
        said = f"{chapter:g}" if chapter else ""
        if not said:
            # A History row has no `last_watched_chapter` - it is not a
            # saved entry - and its own `progress` *is* his position,
            # written by the reader as it opened the chapter. The two
            # stores are told apart by shape, which is how they are
            # actually written: history formats it ("Ch 883"), a tracker
            # entry keeps the site's release count bare ("884").
            raw = str(entry.get("progress") or "").strip()
            said = raw if raw[:1].lower() == "c" else ""
        return _one_on(said, True) or _marked_progress(entry)

    # Video. Only a *confirmed* number counts, and it counts as finished,
    # so the card says the one after it - which is the one Continue
    # opens. Everything else is S01E01, for the same reason: that is what
    # player._starting_episode plays when there is nothing confirmed.
    if entry.get("progress_verified"):
        found = _one_on(str(entry.get("progress") or "").strip(), False)
        if found:
            return found
    elif not entry.get("id"):
        # **No saved store, so the ticks are the record.** He marked
        # episode 1 of Jujutsu Kaisen - a title he has not saved - and
        # expected S01E02, which is right: nothing else knows he watched
        # it. A saved entry is the other way round and deliberately so;
        # its own number is the record, and a tick list that disagrees
        # with an emptied one is the stale marks that read S01E52.
        #
        # _last_mark is strict about what counts: a clean run from
        # episode 1, no season-0 marks. Anything else falls through to
        # S01E01, which is what Continue would play.
        found = _marked_progress(entry)
        if found:
            return found
    return "S01E01" if _starts_at_one(entry) else ""


def _year_text(entry):
    """This entry's year, with an open range closed - see
    backend.years_text, which is the one rule for every surface."""
    return backend.years_text(entry.get("year"))


def _row(entry, kind="title", resume=False):
    """One entry, as the page wants it.

    `kind` is what the app should *do* with a click, decided here where
    the source file is known rather than guessed at the far end. A game
    launches, an app or website opens its targets, everything else opens
    the details page - and getting that wrong sent a clicked game to
    another title's episode list.
    """
    # The tick wins over `progress`: see _last_mark for why they differ
    # and why the difference read as marking being broken.
    progress = _progress_text(entry)
    bits = [_year_text(entry), progress,
            str(entry.get("quality") or "").strip()]
    # **The meta line, and the same line with the number left out.**
    # A mark written by the reader, the player or the details page has
    # to move this number *without* redrawing the page (the owner, 3
    # September 2026: "make the ep and season / ch numbers change
    # immediately when marked as watched/unwatched"), and a page that is
    # redrawn loses the scroll and the rows a catalogue has paged in. So
    # the card carries the parts either side of the number as well, and
    # app.js re-joins them against /api/progress - see `progressInto`.
    others = [b for b in (bits[0], bits[2]) if b]
    # Whether the cover offers a continue ring. Home gave every tracker
    # medium two targets (the owner's ask - anime, series and movies used
    # to be one); games, apps and websites have nothing to resume, and
    # neither does a Discover row, which is not in the library at all -
    # `resume` is opt-in per section for exactly that reason.
    return {
        "kind": kind,
        "resume": bool(resume),
        "id": str(entry.get("id") or entry.get("entry_id")
                  or entry.get("key") or ""),
        "title": str(entry.get("title") or entry.get("name") or "").strip(),
        "type": str(entry.get("type") or ""),
        "meta": "  ".join(b for b in bits if b)[:40],
        "prog": progress,
        "metabase": "  ".join(others)[:40],
        # For the filter's genre checkboxes - see _row_genres.
        "genres": _row_genres(entry),
        # A cover the site named small - the card asks for a better one
        # rather than this page waiting on it. See _thin_cover.
        "thin": _thin_cover(entry),
        "cover": backend.cover_url(entry),
        # **The picture's real address, beside the proxied one.** The
        # owner, 3 September 2026: "when I press on a card on the genre
        # and cast pages, it shows me the ep list correctly but the bg
        # image and the logo are not loading."
        #
        # A card clicked on one of those pages has no saved entry to
        # find, so the app builds a transient one - and it was building
        # it with `cover_url: ""`. The details page's ground is a blur of
        # *the entry's own cover* (details._seed_backdrop_from_cover),
        # resolved through `cover_path` then `cover_url`/`poster`, so an
        # entry carrying neither opens on flat black however good the
        # card that opened it looked. `cover` cannot stand in for it:
        # that is this server's `/img/<token>`, which means nothing to
        # helpers/images' cache.
        "art": _remote_art(entry),
        "url": str(entry.get("url") or ""),
        "imdb": str(entry.get("imdb_id") or ""),
    }


def _remote_art(entry):
    """The entry's picture as the rest of the app knows it: an http URL,
    or a path on this machine. Empty when it has neither."""
    for key in ("cover_url", "poster", "cover", "image", "art"):
        value = str(entry.get(key) or "").strip()
        if value.startswith("http"):
            return value
    for key in ("cover_path", "cover", "poster"):
        value = str(entry.get(key) or "").strip()
        if value and not value.startswith("http"):
            return value
    return ""


# The three files a card's *look* depends on: what has been ticked, and
# what is in the library. A page patches itself against them rather than
# being redrawn - see app.js progressInto.
LIVE_FILES = ("history.json", "series.json", "tracker.json")


def _live_stamp():
    marks = []
    for name in LIVE_FILES:
        try:
            marks.append(int((DATA / name).stat().st_mtime_ns))
        except OSError:
            marks.append(0)
    return marks


def _progress_now():
    """Every marked title's current number, and what is saved.

    Keyed exactly as `_marked_progress` looks one up - the entry id and
    the lowercased title - so a card can be matched by whichever of the
    two it carries.

    `saved` is here for the same reason the numbers are. The owner, 3
    September 2026: *"the color of year and rating changes colors in the
    cards in the watch pages, but ... they do not change immediately
    when I save or unsave, I need to go to another page then it will
    refresh"*. That colour is `_grid_row`'s `saved` flag, and it is the
    only mark on a catalogue card saying "you have this" - so it moves
    with the numbers, off the same answer, without the page being
    redrawn.

    `stamp` covers all three files (LIVE_FILES), so the page can tell an
    answer it has already applied from a new one.
    """
    stamp = _live_stamp()
    # **Both shapes per key, because a key can name two works.** The
    # page patches a card by id or by lowercased title (app.js
    # progressInto) and the card knows its own type, so the answer
    # carries the episode form and the chapter form separately rather
    # than picking one here - "one piece" is a manga and an anime.
    #
    # **The saved row wins over the history row of the same name.**
    # Measured 4 September 2026 on two screenshots of the same Home,
    # seconds apart: the shelf drew Reacher S01E02 / Kingdom Ch 884 -
    # `_row`, off series.json and tracker.json - and this answer then
    # patched them to S01E01 and Ch 883, which are history.json's copies
    # of the same numbers, one step behind. Every visit to Home therefore
    # showed the right number and quietly walked it backwards, which is
    # the shape of every "still shows that 5" report. history.json is
    # written when something is *opened*; the saved entry when progress
    # is *edited*, so the two disagree until the next open catches up.
    # **One answer, computed the one way.** The owner, 4 September 2026,
    # with a second screenshot of the same two cards: "STILL THERE THE
    # ISSUE".
    #
    # This used to re-derive the number here - the saved row's, else the
    # history row's, else the ticks - and every time _progress_text's
    # rule changed, this one did not. It is why a History row drawn
    # S01E01 was patched to S01E02 a second later, and why the earlier
    # walk (Reacher drawn S01E02, patched to S01E01) happened at all.
    #
    # So the rows themselves go through _progress_text, exactly as `_row`
    # sends them, and the patch cannot say anything the card would not.
    # Saved entries are applied last so they win over a History row of
    # the same name - the saved store is the one that gets written when
    # progress moves, and it is what the card was drawn from.
    marks, read = {}, {}
    for row in _rows("history.json") + _rows("series.json") + _rows("tracker.json"):
        side = _row_side(str(row.get("type") or ""))
        if not side:
            continue
        said = _progress_text(row)
        if not said:
            continue
        into = read if side == "read" else marks
        for key in (str(row.get("id") or row.get("entry_id") or ""),
                    str(row.get("title") or "").strip().lower()):
            if key:
                into[key] = said

    return {"stamp": stamp,
            # Exactly what the card says, because it is the same call -
            # see _progress_text, which is the single rule for the number
            # under every card in this app.
            "marks": marks,
            "read": read,
            # Both sides, kept apart: a card knows its own type and the
            # page picks the list its side asks for - see _saved_titles
            # for the anime that wore the manga's accent.
            "saved": sorted(_saved_titles("watch")),
            "saved_read": sorted(_saved_titles("read"))}


# Reading genres live in their own map, title -> [tags], written by
# helpers/discover as MangaDex answers about each title
# (discover._save_reading_meta). Video rows carry theirs on the row
# already, out of Cinemeta's meta. Re-read only when the file moves: it
# is 936 titles on his machine and a page asks about sixty of them.
_READING_GENRES = {"at": None, "map": {}}


def _reading_genres():
    path = DATA / "reading_meta.json"
    try:
        stamp = path.stat().st_mtime
    except OSError:
        stamp = None
    if _READING_GENRES["at"] != stamp:
        found = {}
        try:
            body = json.loads(path.read_text(encoding="utf-8-sig"))
            for title, names in (body.get("genres") or {}).items():
                if isinstance(names, list) and names:
                    found[str(title).strip().lower()] = [str(n) for n in names]
        except Exception:
            found = {}
        _READING_GENRES["map"] = found
        _READING_GENRES["at"] = stamp
    return _READING_GENRES["map"]


# **The genres a page offers to tick, whether or not its loaded rows
# happen to carry them.** The owner, 4 September 2026: "add to the watch
# and read pages genres in the filter, like add to anime filter: Romance
# and other genres."
#
# Built from the rows alone, the Anime page offered six ticks - the
# genres its thirty cached rows happened to name - and Romance was not
# among them. These are the two vocabularies the app already browses by:
# Cinemeta's genre catalogs on the watch side (the same names
# details.GenreBrowsePage opens) and MangaDex's tags on the read side,
# less the ones discover.BLOCKED_GENRES filters out everywhere else.
#
# A tick with nothing loaded behind it is not a dead end: the page asks
# the genre route for it and merges what comes back (app.js pullGenre),
# which is the same source the genre page uses.
WATCH_GENRES = ("Action", "Adventure", "Animation", "Comedy", "Crime",
                "Documentary", "Drama", "Family", "Fantasy", "History",
                "Horror", "Music", "Mystery", "Romance", "Sci-Fi",
                "Sport", "Thriller", "War", "Western")
READ_GENRES = ("Action", "Adventure", "Comedy", "Drama", "Fantasy",
               "Historical", "Horror", "Isekai", "Martial Arts",
               "Mystery", "Psychological", "Romance", "School Life",
               "Sci-Fi", "Slice of Life", "Sports", "Supernatural",
               "Thriller", "Tragedy")


def _genre_choices(side):
    return list(READ_GENRES if side == "read" else WATCH_GENRES)


# How many genres a card carries. The filter builds its checkbox list
# from what the page's rows actually name, so this only bounds the
# payload - six is more than any of these sources puts on one title
# that is worth ticking.
ROW_GENRES = 6


def _row_genres(entry):
    """This row's genres, for the filter's checkboxes.

    The owner, 4 September 2026: "add to the filters a checkbox for each
    genre like Romance and etc...". A video row already carries them
    (discover._video_row copies Cinemeta's); a reading row does not, and
    its tags are in reading_meta.json under the lowercased title - the
    same map the reading genre browse reads.
    """
    names = entry.get("genres") or entry.get("genre") or []
    if isinstance(names, str):
        names = [names]
    if not names and _row_side(entry.get("type")) == "read":
        names = _reading_genres().get(
            str(entry.get("title") or "").strip().lower(), [])
    out, seen = [], set()
    for name in names:
        name = str(name or "").strip()
        key = name.lower()
        if name and key not in seen:
            seen.add(key)
            out.append(name)
        if len(out) >= ROW_GENRES:
            break
    return out


def _person_row(row):
    """One face as a card - `kind` "person", so a click opens that name's
    own page rather than a details page it has no entry for.

    The owner, 3 September 2026: *"add cast for discover page and the
    search page also with anime, series and etc... also to the search
    suggestion, and take their images from IMDB or TMDB or any API"*.
    TMDB is the API: it is the only one of the two that publishes a
    person endpoint without an account (IMDb has no public API at all),
    and this app already carries a TMDB read token for artwork, so the
    faces cost no new key - see helpers/people.
    """
    return {"kind": "person",
            "resume": False,
            "id": "",
            "title": str(row.get("title") or ""),
            "type": "Person",
            # What they are known for, which is what tells two faces
            # apart. No progress and no year on a person, so `metabase`
            # is the same line and the live progress patch leaves these
            # cards alone.
            "meta": str(row.get("note") or "")[:40],
            "prog": "",
            "metabase": str(row.get("note") or "")[:40],
            "cover": backend.remote_url(row.get("poster") or ""),
            "url": "",
            "imdb": ""}


CAST_LIMIT = 20


def _cast_section(query=""):
    """The Cast row for Discover (nothing typed) or a search (a query).

    Its own section rather than rows mixed into the others: a face is
    not a title, it opens a different page, and a card that looks like
    the rest but behaves differently is the trap _row's `kind` exists to
    avoid. Empty when TMDB has no token or nothing answers, and an empty
    section is simply not drawn (app.js sectionsInto).
    """
    try:
        from helpers import people
        rows = (people.search(query, CAST_LIMIT) if query
                else people.popular(CAST_LIMIT))
    except Exception:
        return None
    rows = [r for r in rows or [] if r.get("title")]
    if not rows:
        return None
    return {"title": f"Cast  ({len(rows)})", "style": "person",
            "rows": [_person_row(r) for r in rows]}


# One resolved cover per (title, page url), for the life of the process.
# A search is re-run on every keystroke the owner commits and a card is
# re-drawn on every visit; the chain behind this is up to three requests
# and must be paid once.
_CARD_COVERS = {}
_CARD_COVER_CAP = 400


# A season suffix, for _cover_titles. Deliberately anchored to the end
# and deliberately not matching "Part": a cour split ("Season 3 Part 2")
# is still season 3, which is the same reason anilist._SEASON_NUMBER_RE
# leaves Part alone.
_SEASON_TAIL_RE = re.compile(
    r"\s*(?::|-)?\s*(?:season\s*\d+|s\d+|\d+(?:st|nd|rd|th)\s*season)\s*$",
    re.IGNORECASE)


def _cover_titles(title):
    """The names to try for one title's *artwork*, best first.

    **Only ever for art.** The owner, 4 September 2026, asked for the one
    schedule row a forced-failure test could not recover: "Link Click
    Season 3" is a real title with no catalogue entry of its own, so
    cover_fetch answered nothing and the row stayed blank while its two
    neighbours healed.

    Dropping a trailing season is exactly the shortened variant
    `.claude/rules/integrations.md` forbids for *identity* - asking
    Wikidata for "Bleach" instead of "Bleach: Thousand-Year Blood War"
    scored the 2004 series at 1.00 and pinned the wrong pages onto the
    entry, permanently. A cover is not identity: the franchise poster is
    the right picture for a season of it, nothing is written back onto
    the entry, and the alternative on screen is an empty rectangle. So
    the full title is asked first and always, and the shortened form is
    only ever a fallback for the picture.
    """
    title = str(title or "").strip()
    if not title:
        return []
    names = [title]
    short = _SEASON_TAIL_RE.sub("", title).strip(" -:–—")
    if short and short.lower() != title.lower() and len(short) >= 3:
        names.append(short)
    return names


def _card_cover(title, page_url, thin=False, imdb_id="", kind=""):
    """Art for one card the sweep gave none for - or gave one that does
    not work.

    **Asked by the card, not by the search.** The owner, 3 September
    2026: "in the searching page the readings images are not loading" -
    measured that day, "solo leveling" comes back with thirteen rows and
    3asq's carries no `cover_url` at all, because Madara's search
    endpoint answers titles and URLs and no art. The Qt tracker never
    saw this: it called `fetch_manga_details` for the entry it saved,
    which is the chain that ends in a cover.

    Filling them *inside* the search was the first shape and it was
    measured and rejected the same hour: three runs of the real
    `_search` against his own sites went from a 1.33s median to 2.23s
    and 3.34s with a 5.62s worst case, because the chain is site round
    trips and the answer waited on all of them. Rule 7 says show what
    there is and fill the rest in, so the page draws the row at once
    with a blank slab and asks for this per card (app.js askForCover).
    Every blank reading card benefits, not only a search's: "kingdom"
    measured 16 rows without art.

    **Every candidate is fetched before it is offered, and that was
    measured too.** The first version answered with whichever URL the
    chain named and left the page to find out: on Hunter X Hunter it
    named MangaDex's cover, which this server cannot fetch without a
    browser User-Agent (`image fetch failed for uploads.mangadex.org:
    HTTPError` in his own log), so the card replaced a small-but-working
    250x350 with a 404 and went blank. A cover that cannot be fetched is
    not an answer, and proving it costs nothing that was not going to be
    paid anyway - the blob lands in the same on-disk cache the page's own
    request would have filled.
    """
    title = str(title or "").strip()
    page_url = str(page_url or "").strip()
    imdb_id = str(imdb_id or "").strip()
    kind = str(kind or "").strip().lower()
    key = (title.lower(), page_url, bool(thin), imdb_id, kind)
    if key in _CARD_COVERS:
        return {"cover": _CARD_COVERS[key]}

    # **A watched title asks a different chain, and it had none at all.**
    # The owner, 4 September 2026: "in the schedule page some of the
    # cover images do not load" and "the history still does not load
    # images". Neither page reproduced with his cache warm - every one of
    # the 39 schedule and 41 history rows answered 200 here. What his own
    # log holds is why: `image fetch failed for images.metahub.space:
    # HTTPError` and `image fetch failed for 3asq.online: TimeoutError`.
    # The page's capture-phase error handler then hides that <img> and
    # strips its src for good, so one timeout is a permanently blank row.
    #
    # A grid card has survived that since 3 September, because it asks
    # here for something better (app.js askForCover). A history or
    # schedule row had no such ask, and this route had nothing to answer
    # a video row with anyway - it is the reading chain end to end. So
    # the same `cover_fetch.resolve` the search suggestions have used
    # since 30 August answers those: TMDB by IMDb id, then the catalogue
    # by title, with the file landing in the same image_cache the rest of
    # the app reads.
    if kind and kind not in READING_KINDS:
        found = ""
        try:
            from helpers import cover_fetch
            want = "anime" if kind == "anime" else "video"
            for name in _cover_titles(title):
                path = cover_fetch.resolve("", imdb_id=imdb_id, title=name,
                                           kind=want, timeout=6)
                if path:
                    found = backend.local_url(str(path))
                    if found:
                        break
        except Exception:
            found = ""
        while len(_CARD_COVERS) >= _CARD_COVER_CAP:
            try:
                del _CARD_COVERS[next(iter(_CARD_COVERS))]
            except (StopIteration, KeyError, RuntimeError):
                break
        _CARD_COVERS[key] = found
        return {"cover": found}

    answer_url = ""
    try:
        from helpers import manga_sites
        site = (str((manga_sites.fetch_manga_details(
                    page_url, timeout=5, title=title) or {}).get("cover_url")
                    or "") if page_url else "")
        catalogue = (lambda: str(manga_sites._external_cover(title, 5) or "")
                     ) if title else (lambda: "")
        # **Which one is asked first depends on why we are asking.** A
        # row with *no* art wants the site's own cover for this exact
        # slug before anybody else's art for a matching title - that is
        # fetch_manga_details' own rule and the reason it exists. A row
        # whose art is too small for a card is a different question: the
        # site has already given what it has (Hunter X Hunter's 3asq
        # cover is the file `cover_250x350.jpg`, measured), so the
        # catalogue is asked first and the site's small file is what we
        # fall back to. Either way a candidate under the floor is
        # skipped while a better one is still untried, and nothing is
        # offered that could not be fetched.
        order = ([catalogue, lambda: site] if thin
                 else [lambda: site, catalogue])
        # **Two passes, not a running floor.** The first accepts only a
        # candidate big enough for a card; the second accepts whatever
        # can be fetched. So a thin row is never left with nothing when
        # the catalogue has no better picture than the site's own - it
        # keeps the small one it already had, which is still a picture.
        floors = [manga_sites.CARD_COVER_MIN_WIDTH, 0] if thin else [0]
        dead = set()
        for floor in floors:
            for get in order:
                candidate = manga_sites.upgrade_cover_url(get() or "")
                if not candidate or candidate in dead:
                    continue
                named = manga_sites.named_cover_width(candidate)
                if floor and named and named < floor:
                    continue
                # remote_url answers the *path* ("/img/<token>") and
                # fetch_image wants the bare token - passing one where
                # the other was wanted made every candidate look
                # unfetchable (found 3 September 2026: _card_cover
                # answered "" in 1.6s with nothing in the log, because
                # fetch_image's "no such token" path does not log).
                path = backend.remote_url(
                    candidate, _cover_headers(candidate, page_url))
                if not path:
                    continue
                blob, _kind = backend.fetch_image(path[len("/img/"):])
                if blob:
                    answer_url = path
                    break
                dead.add(candidate)   # proven unfetchable; do not retry
            if answer_url:
                break
    except Exception:
        answer_url = ""
    while len(_CARD_COVERS) >= _CARD_COVER_CAP:
        try:
            del _CARD_COVERS[next(iter(_CARD_COVERS))]
        except (StopIteration, KeyError, RuntimeError):
            break
    _CARD_COVERS[key] = answer_url
    return {"cover": answer_url}


# What a cover host wants to be asked with. urllib's default User-Agent
# is refused outright by MangaDex (measured: HTTPError on every
# uploads.mangadex.org cover) and the scanlation sites check the Referer
# - chapter_source has sent both for its pages since it was written, and
# a cover is the same kind of request.
def _cover_headers(url, page_url=""):
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                             "AppleWebKit/537.36",
               "Accept": "image/avif,image/webp,image/*,*/*;q=0.8"}
    origin = ""
    try:
        parts = urllib.parse.urlsplit(page_url or url)
        if parts.scheme and parts.netloc:
            origin = f"{parts.scheme}://{parts.netloc}/"
    except Exception:
        origin = ""
    if origin:
        headers["Referer"] = origin
    return headers


def _people(query):
    """Faces for the window's search suggestions - the Qt panel's half
    of the same ask. Answered on its own route because the suggestion
    list is filled by helpers/global_search, not by this page."""
    section = _cast_section(str(query or "").strip())
    return {"rows": (section or {}).get("rows") or []}


def _saved_titles(side=""):
    """Titles already in the library, lowercased.

    The catalogue grid writes a saved row's meta line in ACCENT, which is
    the only mark on a card saying "you have this" - tracker._grid_record
    does the same test against the same three files.

    **Which file is asked depends on the side.** The owner, 3 September
    2026: *"the one piece anime date on the card on the anime pages has
    a teal color like it is saved while it is not saved"*. He reads the
    manga One Piece and does not have the anime, and this was one set
    over both files - so a title he keeps on one side of the app marked
    every card of that name on the other. The same collision put the
    manga's chapter number on the anime's card (see _last_mark) and sent
    a reading card to the anime's details page (web_pages._find); this
    is the third face of it.

    `side` is "watch", "read", or "" for the old both-files answer,
    which nothing should want and which is kept only so a caller that
    genuinely does not know a row's medium is not forced to guess.
    """
    files = {"watch": ("series.json",), "read": ("tracker.json",)}.get(
        side, ("series.json", "tracker.json"))
    found = set()
    for name in files:
        for entry in _rows(name):
            title = str(entry.get("title") or "").strip().lower()
            if title:
                found.add(title)
    return found


def _saved_sides():
    """{"watch": {...}, "read": {...}} - both, read once."""
    return {"watch": _saved_titles("watch"), "read": _saved_titles("read")}


def _row_side(kind):
    """"read", "watch", or "" for a type this app does not file.

    windows/web_pages.entry_side by the same table - not imported from
    it, because that module pulls in Qt and run.py serves these routes
    with no Qt in the process at all.
    """
    kind = str(kind or "").strip().lower()
    if kind in READING_KINDS:
        return "read"
    if kind in ("anime", "series", "movie", "movies"):
        return "watch"
    return ""


def _is_saved(entry, saved):
    """Whether this row is in the library, on its own side of the split.

    `saved` is either a `_saved_sides()` mapping or a plain set, so a
    caller that still hands a set through keeps working. A row whose
    type names no side is asked of both, which is the old answer and the
    honest one when nothing is known about it.
    """
    title = str(entry.get("title") or "").strip().lower()
    if not title:
        return False
    if not isinstance(saved, dict):
        return title in saved
    side = _row_side(entry.get("type"))
    if side:
        return title in saved.get(side, ())
    return any(title in group for group in saved.values())


def _grid_row(entry, saved_titles):
    """One catalogue row, as tracker._grid_record builds it.

    The meta line is year and rating joined by two spaces (web_grid line
    649), the rating being "* 7.4" from a Cinemeta row's imdbRating to
    one decimal - "7.0" written ":g" renders "7", which reads as a
    different number. Reading rows carry neither and get a blank line,
    which is why .m has a min-height.
    """
    row = _row(entry)
    year = _year_text(entry)
    raw = entry.get("imdbRating")
    rating = ""
    if raw not in (None, ""):
        try:
            rating = f"★ {float(raw):.1f}"
        except (TypeError, ValueError):
            rating = f"★ {str(raw).strip()}" if str(raw).strip() else ""
    row["meta"] = "  ".join(part for part in (year, rating) if part)
    # **The same two facts as numbers, for the page's Sort control.**
    # The owner, 4 September 2026: "in the watch, and read pages, add a
    # list button on the right that change the sort". app.js sorts the
    # cards it already holds, so it needs the values rather than the
    # display line above - "2020-2025  * 6.8" is not a number, and the
    # year in it is a range. Both are optional: a reading row has
    # neither, and applySort leaves those rows in the source's order
    # rather than inventing one.
    try:
        row["year"] = int(str(year)[:4]) if str(year)[:4].isdigit() else 0
    except (TypeError, ValueError):
        row["year"] = 0
    try:
        row["rating"] = round(float(raw), 1) if raw not in (None, "") else 0.0
    except (TypeError, ValueError):
        row["rating"] = 0.0
    row["saved"] = _is_saved(entry, saved_titles)
    return row


# How many titles the banner rotates through. Home shows one at a time
# and pages between them; more than a handful is a pager nobody reads.
HERO_SLIDES = 6


def _heroes(candidates):
    """Every banner-worthy entry, in order, without repeats.

    Saved entries are asked before history, because hero_backdrop and
    hero_logo are written onto the saved entry - a history row for the
    same show has neither.
    """
    found, seen = [], set()
    for entry in candidates:
        hero = backend.hero_for(entry)
        if not hero:
            continue
        key = (hero.get("id") or hero.get("title") or "").lower()
        if key in seen:
            continue
        seen.add(key)
        found.append(hero)
        if len(found) >= HERO_SLIDES:
            break
    # A cover-as-backdrop fallback lived here for a day, so that Movies
    # and Anime - whose entries carry no hero_backdrop - got a banner
    # too. Removed with the banners themselves; Home is back to drawing
    # exactly the art helpers/hero_art has written, and nothing else.
    return found


def _recent_first(name, stamp):
    """One shelf's entries, most recently opened first.

    The owner, 4 September 2026: *"make the apps and games in the main
    pages when opened become the first in sort in the main page after
    ~1.5 sec"*. Launching something already writes the stamp - games.py
    and link_grid record `last_played` / `last_used` the moment they run
    a target, and that is what those shelves' own "Last Played" sort
    reads - so Home was simply never sorting by it. The ~1.5s is the
    page noticing: web_pages._check_covered stats the file every 150ms
    and Home redraws when it moves.

    Entries never opened keep the file's own order, behind the ones that
    have been: a shelf is a list he arranged by hand, and this lifts
    what he just used out of it rather than replacing his order with a
    usage ranking.

    Sorted as text, deliberately - these are fixed-shape ISO stamps from
    storage.now_iso, so ordering them as strings is ordering them in
    time, and a malformed one sorts with the rest instead of raising.
    """
    def key(pair):
        index, entry = pair
        when = str(entry.get(stamp) or "").strip()
        # (0, ...) for opened and (1, ...) for never, so the two groups
        # cannot interleave; newest first inside the first group, which
        # is what `reverse=True` could not express alongside a stable
        # tail in file order.
        return (0, _reverse_text(when), index) if when else (1, "", index)

    return [entry for _index, entry in sorted(enumerate(_rows(name)), key=key)]


def _reverse_text(text):
    """A string that sorts the other way round under an ascending sort."""
    return "".join(chr(0x10FFFF - ord(c)) for c in text)


def _home():
    """Home: what he has saved, and nothing else.

    **Watching used to be watch history**, on the reasoning in _history
    that series.json holds only saved entries and a page built from them
    "looks empty while he has plainly been watching things". The owner
    settled it on 1 September 2026: "under Watching and Reading in the
    main page show only the saved ones not the history". So Watching is
    series.json filtered to the three video types, exactly as Reading has
    always been tracker.json - two rows of the library, not of activity.

    History is still read, but only to *order* them: the row he last
    opened leads, which is what made the history version feel right, and
    the banner follows the same order.
    """
    saved = _rows("series.json")
    reading = _rows("tracker.json")
    wanted = ("anime", "series", "movie")
    watching = [e for e in saved
                if str(e.get("type", "")).lower() in wanted]

    recent = {}
    for position, row in enumerate(_history(("Anime", "Series", "Movie"))):
        for key in (str(row.get("entry_id") or ""),
                    str(row.get("title") or "").strip().lower()):
            if key:
                recent.setdefault(key, position)

    def _last_opened(entry):
        """Where this entry sits in the history, or after everything."""
        for key in (str(entry.get("id") or ""),
                    str(entry.get("title") or "").strip().lower()):
            if key in recent:
                return recent[key]
        return len(recent) + 1

    watching.sort(key=_last_opened)

    # **The banner leads on whatever was opened last, watched or read.**
    # The owner, 2 September 2026: "make the banner in the home page
    # shows the latest watched/read on the most left point". This used
    # to be every watching entry followed by every reading one, so a
    # chapter read five minutes ago sat behind three shows last touched
    # in August - the medium decided the order, not the clock.
    #
    # Its own index, over every kind rather than the three video ones,
    # because a reading row is invisible to `recent` above and would
    # score "after everything" here too.
    seen_at = {}
    for position, row in enumerate(_history(VIDEO_KINDS + READING_KINDS)):
        for key in (str(row.get("entry_id") or ""),
                    str(row.get("title") or "").strip().lower()):
            if key:
                seen_at.setdefault(key, position)

    def _touched(entry):
        for key in (str(entry.get("id") or ""),
                    str(entry.get("title") or "").strip().lower()):
            if key in seen_at:
                return seen_at[key]
        return len(seen_at) + 1

    ordered = sorted(list(watching) + list(reading), key=_touched)

    def _linked(name, kind):
        """Apps and websites: what they open is the useful second line."""
        out = []
        # Most recently opened first - see _recent_first.
        for entry in _recent_first(name, "last_used"):
            row = _row(entry, kind)
            # What it opens lives in `targets`, a list of
            # {type, target} - not in a flat `url` field like every
            # other kind of entry here.
            target = ""
            for item in (entry.get("targets") or []):
                if isinstance(item, dict) and item.get("target"):
                    target = str(item["target"])
                    break
            # **No path under the name.** home._build_quick_list draws
            # the icon and the name and nothing else - the owner, 1
            # September 2026, with a screenshot of the Qt page: this is
            # "not at all what I wanted". What it *does* carry is the
            # red "Not found" when the target is gone, on the right.
            row["meta"] = ""
            row["url"] = target
            if kind == "app":
                gone = _missing_targets(entry)
                if gone:
                    row["missing"] = "Not found"
                    row["missing_paths"] = gone
            out.append(row)
        return out

    heroes = _heroes(ordered)
    return {"kind": "rows", "note": "",
            "hero": heroes[0] if heroes else None,
            "heroes": heroes, "sections": [
        {"title": "Watching",
         "rows": [_row(e, resume=True) for e in watching]},
        {"title": "Reading",
         "rows": [_row(e, resume=True) for e in reading]},
        {"title": "Games",
         "rows": [_row(e, "game") for e in _recent_first("games.json",
                                                         "last_played")]},
        # `style: list` - these two were a list of icon, name and link
        # in the Qt Home, not a shelf of posters, and a website has no
        # poster to show anyway.
        {"title": "Quick Apps", "style": "list",
         "rows": _linked("apps.json", "app")},
        {"title": "Websites", "style": "list",
         "rows": _linked("websites.json", "site")},
    ]}


# Which banner pool each discover section feeds, and how often each pool
# is drawn from. The owner's split, 2 September 2026: "30% movie, 30%
# series, 30% anime, 10% reading" - every reading section (the latest
# strip, the popular strip and the four medium strips) is one 10% pool
# together, not six pools, or reading would be drawn six times as often
# as he asked.
_BANNER_POOL = {"anime": "anime", "series": "series", "movie": "movie"}
_BANNER_WEIGHTS = (("movie", 30), ("series", 30), ("anime", 30),
                   ("reading", 10))


def _banner_pick(pools):
    """One discover row to draw the banner from, or None.

    A weighted draw over the mediums that actually have rows, then a
    uniform draw inside the chosen one restricted to rows carrying a
    poster - the banner is the poster, so a row without one would be a
    banner with no picture. A medium with no cached rows drops out and
    its weight is shared by the rest, so a cache holding only anime
    still gets a banner rather than a 70% chance of none."""
    import random
    choices = [(name, weight, [r for r in pools.get(name) or []
                               if r.get("poster") or r.get("cover_url")])
               for name, weight in _BANNER_WEIGHTS]
    choices = [(name, weight, rows) for name, weight, rows in choices if rows]
    if not choices:
        return None
    rows = random.choices([c[2] for c in choices],
                          weights=[c[1] for c in choices], k=1)[0]
    return random.choice(rows)


def _discover():
    try:
        cached = json.loads(
            (DATA / "discover_cache.json").read_text(encoding="utf-8-sig"))
    except Exception:
        cached = {}
    if not isinstance(cached, dict):
        cached = {}

    sections, total, newest, banner = [], 0, 0.0, None
    pools = {}
    for key, label in DISCOVER_SECTIONS:
        block = cached.get(key)
        if not isinstance(block, dict):
            continue
        rows = [r for r in (block.get("rows") or [])
                if isinstance(r, dict) and r.get("title")]
        if not rows:
            continue
        total += len(rows)
        try:
            newest = max(newest, float(block.get("at") or 0))
        except (TypeError, ValueError):
            pass
        pools.setdefault(_BANNER_POOL.get(key, "reading"), []).extend(rows)
        sections.append({"title": f"{label}  ({len(rows)})",
                         "rows": [_row(e) for e in rows[:DISCOVER_LIMIT]]})

    # **A different banner every visit, weighted by medium.** The owner,
    # 2 September 2026: "make the banner in the discover changes when I
    # reload the page by going to other page then coming back, make it
    # like before it shows: 30% movie, 30% series, 30% anime, 10%
    # reading". It took rows[0] of the first cached section, which is
    # the anime block, so 300 builds of this page measured 300 times the
    # same anime title. Pages rebuild on every visit, so one weighted
    # draw per build is the whole mechanism - no state to keep. The
    # draw is over the mediums first and a row inside the chosen one
    # second, so a 198-row reading block does not outweigh a 30-row
    # movie block. Measured after, 300 builds on his cache: Anime 96 /
    # Movie 86 / Series 83 / Reading 35 (32/29/28/12%), 296 of 299
    # consecutive picks different, 117 distinct titles, and the sections
    # themselves byte-identical to before.
    first = _banner_pick(pools)
    if first is not None:
        # Discover rows carry no hero art - they are search results,
        # not saved entries - so the banner is the row's own poster,
        # widened and dimmed by the page. Better than no banner, and
        # honest about being the poster.
        picture = backend.cover_url(first)
        if picture:
            banner = {"title": str(first.get("title") or ""),
                      "imdb": str(first.get("imdb_id") or ""),
                      "type": str(first.get("type") or ""),
                      "url": str(first.get("url") or ""),
                      # The same picture twice on purpose: blurred and
                      # blown up as the wide backdrop, and sharp at its
                      # own size as the cover. A discover row has no
                      # separate wide art to use.
                      "backdrop": picture, "cover": picture,
                      "logo": "",
                      # **The same four lines Home's banner draws.** The
                      # owner, 2 September 2026: "make the discover page
                      # banner have the same". It had a single `meta`
                      # holding the year, which is also line 2 of the
                      # bullet run - so the year appeared twice and
                      # nothing else appeared at all. backend._bullets
                      # is the one method both banners now go through,
                      # which is what makes them the same by
                      # construction rather than by looking alike today.
                      "hide_title": False, "poster": True,
                      "meta": "",
                      "bullets": backend._bullets(first),
                      # **Save, not Continue.** The owner, 3 September
                      # 2026: "in the discovery page, instead of the
                      # continue btn in the banner make it 'Save to My
                      # List' or 'Remove from My List'". Continue was
                      # always wrong here: a Discover title is not in
                      # the library, so `id` is empty and the button
                      # opened the details page - the same thing the
                      # button beside it does. `list` tells the page
                      # which of the two words to draw, and it is
                      # re-read from the file rather than remembered,
                      # so a title saved from the details page shows as
                      # saved the next time the banner draws it.
                      "list": str(first.get("title") or "").strip().lower()
                              in _saved_titles(
                                  _row_side(first.get("type"))),
                      "id": ""}

    # **Cast, at the foot of the page.** Last rather than first: the
    # sections above are this machine's own cached catalogue and answer
    # with no network at all, and a TMDB call must never hold them up
    # (rule 7). The section is dropped entirely when TMDB says nothing.
    faces = _cast_section()
    if faces is not None:
        sections.append(faces)
    note = f"{total} titles in {len(sections)} sections"
    if newest:
        age = (time.time() - newest) / 3600.0
        note += f", found {age:.0f}h ago" if age >= 1 else ", found just now"
    if not sections:
        note = "nothing cached - run a discover in the app first"
    return {"kind": "rows", "sections": sections, "note": note, "hero": banner}


def _cached_posters():
    """Every reading poster the discover cache holds, by lowercased title."""
    found = {}
    try:
        cached = json.loads(
            (DATA / "discover_cache.json").read_text(encoding="utf-8-sig"))
    except Exception:
        return found
    if not isinstance(cached, dict):
        return found
    # **The reading sections, not every section.** This says "reading
    # poster" and read the whole cache - so a reading search row with no
    # cover of its own borrowed one from the *anime* block by title, and
    # a manga card came up wearing the anime's art. That is the owner's
    # "why is it showing anime under the reading when I searched Demon
    # Slayer", 2 September 2026: the rows were right and the pictures
    # were from the wrong medium.
    reading_blocks = ("reading", "reading_latest", "medium:Manga",
                      "medium:Manhwa", "medium:Manhua", "medium:Other")
    for key, block in cached.items():
        if key not in reading_blocks or not isinstance(block, dict):
            continue
        for row in (block.get("rows") or []):
            if not isinstance(row, dict):
                continue
            title = str(row.get("title") or "").strip().lower()
            picture = row.get("poster") or row.get("cover_url") or ""
            if title and picture and title not in found:
                found[title] = picture
    return found


# **One row per work in a search, never one per season.** The owner, 2
# September 2026: "do NOT show each season separated on any condition in
# the search results". Cinemeta files every season as its own title, so
# "Kingdom" came back as five identical-looking cards and "Demon Slayer"
# as six - a list of the same show, where he was looking for the show.
#
# Only the *trailing* season marker is cut, and only from the end, so
# "Kingdom" and "Animal Kingdom" stay two works while "Kingdom 3rd
# Season", "Kingdom II" and "Kingdom S3" become one. Applied to the
# search alone: a catalogue page is a library and listing the seasons
# there is right.
_SEASON_TAIL = re.compile(
    r"\s*(?:[:\-–]\s*)?(?:"
    r"season\s*\d+|\d+(?:st|nd|rd|th)\s+season|part\s*\d+|"
    # \b before the bare numeral forms: without it "Taxi" lost its "xi"
    # and "The Matrix" its "ix" (review, 3 September 2026).
    r"cour\s*\d+|s\d{1,2}|\b[ivx]{1,4}|\b\d{1,2}"
    r")\s*$", re.IGNORECASE)


def _work_key(title):
    """A title with its season marker taken off, for grouping."""
    plain = " ".join(str(title or "").strip().lower().split())
    for _ in range(3):          # "kingdom 3rd season" -> "kingdom"
        cut = _SEASON_TAIL.sub("", plain).strip(" :-–")
        if not cut or cut == plain:
            break
        plain = cut
    return plain


def _one_per_work(rows):
    """The first row of each work, in the order they arrived.

    First rather than "best": Cinemeta returns its own relevance order
    and the season the search matched most closely leads it, so keeping
    the leader keeps the answer the source gave.
    """
    seen, out = set(), []
    for row in rows:
        key = _work_key(row.get("title"))
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        out.append(row)
    return out


# How long the search page may spend before it draws what it has. Rule
# 7's one second is the target for what is already local; a search is
# five round trips to five services and cannot be, so this is the point
# at which a straggler stops being worth waiting for - measured against
# an anime lookup that took 18.5s to answer with nothing.
SEARCH_BUDGET_S = 6.0


def _search(text):
    """Every source's results for one query, as sections.

    The window's search field sends Enter here through
    WebDiscoverPage.start_search - main._search_in_discover navigates to
    Discover and then calls `start_search`, and a page without that
    method silently showed the ordinary Discover instead, which is
    exactly what the owner reported.

    Anime, series, movies and reading are asked in parallel: serially
    this is the sum of four site searches, and one slow source would
    hold up every other.
    """
    text = str(text or "").strip()
    if not text:
        return {"kind": "rows", "sections": [], "note": ""}

    from concurrent.futures import ThreadPoolExecutor
    from helpers import discover as finder

    def video(kind):
        try:
            return finder.discover_video(kind, text, limit=30) or []
        except Exception:
            return []

    def reading():
        """His own reading sites, and only those.

        The owner, 1 September 2026: "in the search results, show only
        the available readings in the websites from the settings".
        discover_reading asks MangaDex, which knows every manga there is
        and cannot say whether *he* can open it - so a result was as
        likely to be a dead end as a title he could read.

        manga_sites.search_all asks the sites in Settings, in parallel
        and under one deadline, and returns only what they answered with.
        Its own docstring carries the same instruction from an earlier
        round: "only show the ones in the websites provided so that it
        definitely will show the ch list".
        """
        try:
            from helpers import manga_sites
            rows = manga_sites.search_all(text) or []
        except Exception:
            return []
        # **Only sites that can actually open a chapter list.**
        # discover._serving_sites_only exists for this and carries the
        # measurement: Mangalek browses and searches perfectly and
        # answers 403 to every series page it publishes. Its rows are
        # also the ones with no cover - seven of thirteen for "solo
        # leveling", every one of them Mangalek - so this removes the
        # blank cards and the dead ends in one pass. The owner's own
        # instruction, quoted in that function: "only show the ones in
        # the websites provided so that it definitely will show the ch
        # list".
        try:
            from helpers import discover as _finder
            rows = _finder._serving_sites_only(rows) or rows
        except Exception:
            pass
        # **A row with no cover borrows one.** Not every site puts a
        # picture on a search row - seven of thirteen came back without
        # one - and the discover cache already holds a thousand reading
        # rows with posters, indexed by title. Free, local, and it only
        # fills gaps.
        posters = _cached_posters()
        out = []
        for row in rows:
            if not isinstance(row, dict) or not row.get("title"):
                continue
            picture = (row.get("cover_url")
                       or posters.get(str(row.get("title") or "").strip().lower())
                       or "")
            out.append({"title": row.get("title"), "url": row.get("url") or "",
                        "cover_url": picture,
                        "site_id": row.get("site_id") or "",
                        "site_name": row.get("site_name") or "",
                        "type": "Manga", "imdb_id": "", "year": ""})
        return out[:30]

    # **Cast is asked with the rest, not after them.** One more worker
    # rather than a fifth round trip: people.search is a single TMDB
    # call (0.2-0.4s measured) and running it after the others would add
    # its whole cost to a page that is already the slowest here.
    def faces():
        try:
            from helpers import people
            return people.search(text, CAST_LIMIT)
        except Exception:
            return []

    # **One source may not hold the whole page.** The owner, 4 September
    # 2026: "the search still takes long when I hit enter!". The five
    # jobs already run in parallel, so the page costs the slowest of
    # them - and measured on his own connection, for "legend of the
    # northern blade":
    #
    #     discover_video anime     18,481 ms  ->  0 rows
    #     discover_video series        719 ms  -> 11
    #     discover_video movie         715 ms  -> 22
    #     manga_sites.search_all     1,628 ms  ->  3
    #     _serving_sites_only            1 ms
    #     people.search                259 ms  ->  0
    #
    # Eighteen and a half seconds spent on a section that then had
    # nothing in it. Rule 7's answer is a page that shows what has
    # arrived rather than an empty surface waiting on the slowest
    # source, so the wait is bounded and a job that misses it is simply
    # a section that is not there - which is what an 18s empty answer
    # was going to be anyway.
    #
    # The pool is not waited on: a straggler finishes into a result
    # nobody reads, on a daemon-less worker that ends with it. Shutting
    # down with wait=True here would put the whole 18s back.
    pool = ThreadPoolExecutor(max_workers=5)
    jobs = {"Anime": pool.submit(video, "anime"),
            "Series": pool.submit(video, "series"),
            "Movies": pool.submit(video, "movie"),
            "Reading": pool.submit(reading),
            "Cast": pool.submit(faces)}
    found = {}
    limit = time.monotonic() + SEARCH_BUDGET_S
    for name, job in jobs.items():
        try:
            found[name] = job.result(timeout=max(0.0, limit - time.monotonic()))
        except Exception:
            found[name] = []
    pool.shutdown(wait=False)

    sections, total = [], 0
    cast = [r for r in found.get("Cast") or [] if r.get("title")]
    for name in ("Anime", "Series", "Movies", "Reading"):
        rows = [r for r in found.get(name) or [] if isinstance(r, dict)
                and r.get("title")]
        # **Seasons collapse, sequels do not.** A trailing number on a
        # film is another film - review, 3 September 2026, on this
        # function's own key: "Iron Man 2" and "Iron Man 3" both became
        # "iron man" and a search for the series kept only the first
        # one Cinemeta returned. Only the Anime and Series sections list
        # one work as many rows.
        if name in ("Anime", "Series"):
            rows = _one_per_work(rows)
        if not rows:
            continue
        total += len(rows)
        sections.append({"title": f"{name}  ({len(rows)})",
                         "rows": [_row(e) for e in rows]})
    if cast:
        total += len(cast)
        sections.append({"title": f"Cast  ({len(cast)})", "style": "person",
                         "rows": [_person_row(r) for r in cast]})
    note = (f"{total} results for “{text}”" if total
            else f"nothing found for “{text}”")
    return {"kind": "rows", "sections": sections, "note": note, "hero": None}



# What each of the six catalogue rows reads, and out of which file. The
# app splits the same data the same way: series.json holds what is
# watched and tracker.json what is read.
SECTIONS = {
    "movies": ("series.json", ("Movie", "Movies")),
    "series": ("series.json", ("Series",)),
    "anime": ("series.json", ("Anime",)),
    "manga": ("tracker.json", ("Manga",)),
    "manhwa": ("tracker.json", ("Manhwa",)),
    "manhua": ("tracker.json", ("Manhua",)),
}

# Which discover-cache section holds each medium's catalogue. The cache
# paints instantly; the live call underneath it is a site search and
# takes seconds, so the page asks for that separately (rule 7).
BROWSE_CACHE = {
    "movies": "movie", "series": "series", "anime": "anime",
    "manga": "medium:Manga", "manhwa": "medium:Manhwa",
    "manhua": "medium:Manhua",
}
BROWSE_LIMIT = 60


def _cached_browse(key):
    try:
        cached = json.loads(
            (DATA / "discover_cache.json").read_text(encoding="utf-8-sig"))
    except Exception:
        return []
    block = cached.get(key) if isinstance(cached, dict) else None
    if not isinstance(block, dict):
        return []
    return [r for r in (block.get("rows") or [])
            if isinstance(r, dict) and r.get("title")]


MEDIUM_TITLE = {"movies": "Movies", "series": "Series", "anime": "Anime",
                "manga": "Manga", "manhwa": "Manhwa", "manhua": "Manhua"}


def _category_note(route):
    """tracker._category_note, by the same words it uses."""
    key = BROWSE_CACHE.get(route, "")
    if key.startswith("medium:"):
        return f"Most followed {key.split(':', 1)[1].lower()}"
    return "Most watched"


def _thin_cover(entry):
    """Whether this reading row's cover is too small for a card.

    The owner, 3 September 2026: *"the reading cover images from the
    3asq site are not clear at all ... make sure to take the before Qt
    as reference (in results, not in methods to fix)"*. What Qt ended up
    with was `fetch_manga_details`' chain - the site's own card art,
    then MangaDex, then AniList - because the tracker called it for the
    entry it saved. The web catalogue draws the sweep's own rows, so a
    site that publishes a small file keeps it: measured that day, Hunter
    X Hunter's 3asq cover is the file `cover_250x350.jpg`, 250px wide
    against a card that draws 201 device pixels and a details page that
    draws more.

    **Read out of the file's own name, so nothing is asked of any
    host.** The alternative was probing each cover's header, which is a
    request per row - 41 on Manhwa, hundreds once a page has been
    scrolled - and this page has to be on screen inside a second (rule
    7, and see app.js SETTLE_MS for what a catalogue page already pays).
    A named width is the case that actually bites here, because a small
    file this side of the floor is small *because the site named it
    that*: WordPress's `-193x278` crops and 3asq's literal
    `cover_250x350`. Anything else keeps its cover.

    The replacement is fetched by the card, not here (app.js
    askForCover), for the same reason: the chain is site round trips and
    the page must not wait on them.
    """
    if str(entry.get("type") or "").strip().lower() not in READING_KINDS:
        return False
    for key in ("cover_url", "poster", "cover"):
        value = str(entry.get(key) or "")
        if value.startswith("http"):
            try:
                from helpers import manga_sites
                named = manga_sites.named_cover_width(value)
                return bool(named) and named < manga_sites.CARD_COVER_MIN_WIDTH
            except Exception:
                return False
    return False


def _medium(route):
    """One medium's page, laid out as the Qt page it replaces.

    The owner, 1 September 2026: "use the same design as before but with
    the WebView2". Before is a *catalogue grid* and nothing else - the
    page name small at the top, tracker._category_note under it ("Most
    watched", "Most followed manhwa"), then one wrapping grid of every
    row the browse cache holds. It carries no banner and no library
    sections; those were mine and are gone.

    The whole cache, uncapped, exactly as the Qt grid draws it - the card
    CSS carries `content-visibility`, so the rows below the fold cost
    nothing until they are scrolled to.
    """
    rows = _cached_browse(BROWSE_CACHE.get(route, ""))
    saved = _saved_sides()
    side = _row_side(MEDIUM_TITLE.get(route, route))
    return {"kind": "grid", "hero": None, "browse": route,
            # What the filter may tick, and what a tick with nothing
            # behind it should ask for - see _genre_choices.
            "genrechoices": _genre_choices(side),
            "genrereading": 1 if side == "read" else 0,
            "genrekind": {"movies": "movies", "series": "series",
                          "anime": "anime"}.get(route, "all"),
            "title": MEDIUM_TITLE.get(route, route.title()),
            "note": _category_note(route),
            "rows": [_grid_row(e, saved) for e in rows]}


def _browse(route):
    """A live catalogue for one medium - a site sweep, so its own route.

    **tracker._fetch_browse_rows, not a fresh call of our own.** It is
    what the Qt page has always used: it writes the sweep into the same
    shared cache (memory and disk), fills all four `medium:` keys from
    the one sweep that produced them, and returns the *merged* list
    rather than the page it just fetched - so the depth already on
    screen is kept. Calling helpers.discover directly, as this did for a
    day, bypassed all three.

    Imported here rather than at module scope: tracker pulls in Qt, and
    run.py serves these same routes with no Qt in the process at all.
    """
    kind = BROWSE_CACHE.get(route, "")
    if not kind:
        return {"rows": []}
    try:
        from windows import tracker
        rows = tracker._fetch_browse_rows(kind) or []
    except Exception as error:
        return {"rows": [], "error": str(error)[:120]}
    rows = [r for r in rows if isinstance(r, dict) and r.get("title")]
    saved = _saved_sides()
    return {"rows": [_grid_row(e, saved) for e in rows]}



# ---- the shelves: Games, Apps, Websites ----------------------------
#
# windows/games.py and windows/link_grid.py are twins on purpose (the
# same SORT_OPTIONS, the same CARD_MARGINS, the same drag-reorder
# helper), so this is one route with a table rather than three.
#
# `art` is the difference that matters: a game draws its Steam poster at
# the full 160x216 card cover, an app or a website draws its icon square
# at 160x160 - link_grid's own words, "store icons are square originals,
# and cropping one to the movie tile's portrait would cut the sides off
# every logo".
SHELVES = {
    "games": {"file": "games.json", "title": "Games", "kind": "game",
              "shape": "poster", "noun": ("game", "games"),
              "sorts": ["Custom Order", "Name (A-Z)",
                        "Date Added (Newest)", "Last Played"],
              "stamp": "last_played"},
    "apps": {"file": "apps.json", "title": "Apps", "kind": "app",
             "shape": "square", "noun": ("app", "apps"),
             "sorts": ["Custom Order", "Name (A-Z)",
                       "Date Added (Newest)", "Last Used"],
             "stamp": "last_used"},
    "websites": {"file": "websites.json", "title": "Websites",
                 "kind": "site", "shape": "square",
                 "noun": ("website", "websites"),
                 "sorts": ["Custom Order", "Name (A-Z)",
                           "Date Added (Newest)", "Last Used"],
                 "stamp": "last_used"},
}


def _missing_targets(entry):
    """This entry's app targets that are no longer on disk.

    link_grid.missing_app_targets, by its own rule: app targets only. A
    "site" target is a URL and asking the same question of one means a
    network probe, which that function is explicit about not doing.
    Re-stated here rather than imported because link_grid pulls in Qt and
    run.py serves these routes with no Qt in the process.
    """
    found = []
    for target in (entry.get("targets") or []):
        if not isinstance(target, dict) or target.get("type") != "app":
            continue
        path = str(target.get("target") or "")
        if path and not pathlib.Path(path).exists():
            found.append(path)
    return found


def _shelf_row(entry, shelf):
    row = _row(entry, shelf["kind"])
    row["name"] = str(entry.get("name") or entry.get("title") or "").strip()
    row["title"] = row["name"]
    row["shape"] = shelf["shape"]
    row["cover"] = backend.cover_url(entry)
    row["meta"] = ""

    targets = [t for t in (entry.get("targets") or [])
               if isinstance(t, dict) and t.get("target")]
    missing = _missing_targets(entry)
    if missing:
        # "Not found" only when nothing on this card can launch - an
        # entry that opens three programs and lost one still works, and
        # saying otherwise would be wrong rather than cautious
        # (link_grid's own note).
        row["missing"] = ("Not found" if len(missing) >= len(targets)
                          else f"{len(missing)} of {len(targets)} not found")
        row["missing_paths"] = missing
    # What the page sorts by, so a sort never needs the server again.
    row["added_at"] = str(entry.get("added_at") or "")
    row["used_at"] = str(entry.get(shelf["stamp"]) or "")
    return row


def _shelf(route):
    shelf = SHELVES[route]
    entries = _rows(shelf["file"])
    return {"kind": "shelf", "shelf": route, "hero": None,
            "title": shelf["title"], "sorts": shelf["sorts"],
            "noun": list(shelf["noun"]),
            "rows": [_shelf_row(e, shelf) for e in entries],
            "note": ""}


def _downloads():
    """The queue as it stands, for a page that asks again every second.

    DownloadsPage polls on its own QTimer for the same reason: a download
    has no event to listen to, and a second is short enough that a
    progress bar reads as moving.
    """
    try:
        from helpers import downloads as queue
    except Exception as error:
        return {"kind": "downloads", "rows": [], "error": str(error)[:120]}
    try:
        jobs = list(queue.list_jobs() or [])          # newest first
    except Exception as error:
        return {"kind": "downloads", "rows": [], "error": str(error)[:120]}

    # downloads_page.STATE_TEXT, so the two pages call a state the same
    # thing in front of the user.
    names = {queue.QUEUED: "Queued", queue.RUNNING: "Downloading",
             queue.DONE: "Finished", queue.FAILED: "Failed",
             queue.CANCELLED: "Cancelled", queue.PAUSED: "Paused"}
    active = {queue.QUEUED, queue.RUNNING, queue.PAUSED}
    rows = []
    for job in jobs:
        if not isinstance(job, dict):
            continue
        state = str(job.get("state") or "")
        rows.append({
            "id": str(job.get("id") or ""),
            "title": str(job.get("label") or ""),
            "state": state,
            "state_text": names.get(state, state.title()),
            # `progress` is a fraction on the record; the bar wants a
            # percentage and nothing else should do that arithmetic.
            "percent": round(100.0 * float(job.get("progress") or 0.0), 1),
            "detail": str(job.get("detail") or "")[:160],
            "active": state in active,
            "can_pause": state in (queue.QUEUED, queue.RUNNING),
            "can_resume": state == queue.PAUSED,
        })
    return {"kind": "downloads", "title": "Downloads", "rows": rows,
            "folder": _download_folder()}


def _download_folder():
    """Where downloads land - downloads_page.saved_folder, without Qt.

    That module reads settings.json's "download_folder" and falls back to
    downloads.default_folder(); it is two lines, and importing it here
    would pull Qt into a server run.py starts without any.
    """
    try:
        stored = json.loads(
            (DATA / "settings.json").read_text(encoding="utf-8-sig"))
        folder = stored.get("download_folder") if isinstance(stored, dict) else None
        if folder:
            return str(folder)
    except Exception:
        pass
    try:
        from helpers import downloads as queue
        return str(queue.default_folder() or "")
    except Exception:
        return ""



# ---- Saved, History and Schedule -----------------------------------
#
# The window's bar opens these three (main.open_section), and they were
# header tabs on each tracker page before that. They had no web route at
# all, so set_active_section fell through and left the page on its own
# default - the owner's "the history/saved/schedule pages take me to the
# series page when I click on them".
VIDEO_KINDS = ("anime", "series", "movie")
READING_KINDS = ("manga", "manhwa", "manhua", "other")

# The two pills every one of these three pages opens with.
TABS = [{"key": "watch", "label": "Watch"}, {"key": "read", "label": "Read"}]

# A week, matching tracker._SCHEDULE_DISK_TTL_S: the calendar covers
# seven days, so even a day-old copy still names most of what is coming
# and _calendar_rows drops whatever has already aired.
CALENDAR_TTL_S = 7 * 24 * 60 * 60
# tracker.SCHEDULE_RELEASED_LIMIT - the Read side lists what landed, not
# the whole sweep.
RELEASED_LIMIT = 20


def _side_rows(side):
    """Everything saved on one side of the Watch/Read split."""
    if side == "read":
        return [e for e in _rows("tracker.json")
                if str(e.get("type", "")).lower() in READING_KINDS
                or not e.get("type")]
    return [e for e in _rows("series.json")
            if str(e.get("type", "")).lower() in VIDEO_KINDS]


def _status_card(entry, resume=True):
    """A saved card: the status under the title, the number under that.

    tracker's own card draws all three, and the number in ACCENT - which
    is the only colour on the card and the thing being looked for.
    """
    row = _row(entry, resume=resume)
    row["status"] = str(entry.get("status") or "").strip()
    row["progress"] = _progress_text(entry)
    return row


# The statuses a saved title can carry, in the order the Qt page listed
# them - tracker._WATCHING_STATUSES / _READING_STATUSES, word for word.
# Kept here rather than imported because this module is the web server
# and must answer without a Qt page having been built.
SAVED_STATUSES = {
    "watch": ("Watching", "Completed", "On Hold", "Dropped", "Plan to Watch"),
    "read": ("Reading", "Completed", "On Hold", "Dropped", "Plan to Read"),
}
# Qt's SECTION_TYPE_PLURAL: a heading names a group, and one film is
# still filed under "Movies".
_SECTION_PLURAL = {"Movie": "Movies"}


def _saved(side="watch"):
    """The library, grouped by status and then by medium.

    **In the statuses' own order, not alphabetical** - the owner, 4
    September 2026: "re-add like the Qt the other than Watching /
    Reading like on hold and so on in the saved!". Sorting the groups
    put Completed above Watching, and the filter pills above them were
    built from whatever statuses happened to be in the file - every one
    of his entries is Watching or Reading, so On Hold, Dropped, Completed
    and Plan to Watch were nowhere on the page at all.

    tracker._sections_by_status is the rule this restores: the known
    statuses first, in their own order, anything unexpected after them,
    and a section drawn only when it holds something. What is new beside
    it is `statuses` - the full list, so the filter can offer a status
    that has nothing in it yet.
    """
    side = side if side in ("watch", "read") else "watch"
    entries = _side_rows(side)
    known = SAVED_STATUSES[side]
    groups = {}
    for entry in entries:
        status = str(entry.get("status") or "").strip() or known[0]
        medium = str(entry.get("type") or "").strip() or "Other"
        groups.setdefault((status, medium), []).append(entry)

    def rank(pair):
        status, medium = pair
        try:
            return (known.index(status), medium)
        except ValueError:
            return (len(known), f"{status} {medium}")

    sections = []
    for key in sorted(groups, key=rank):
        status, medium = key
        rows = groups[key]
        name = _SECTION_PLURAL.get(medium, medium)
        sections.append({
            "title": f"{status}  ·  {name}  ({len(rows)})",
            "rows": [_status_card(e) for e in rows]})
    total = sum(len(x["rows"]) for x in sections)
    return {"kind": "rows", "hero": None, "title": "Saved",
            "tabs": TABS, "tab": side, "cardstyle": "status",
            "statuses": list(known), "sections": sections,
            "note": f"{total} saved" if total else "nothing saved yet"}


def _history_when(stamp):
    """"Just now" / "3h ago" / "12 Aug" - tracker._history_when, to the
    word. Relative near the present, absolute past a week, which is how
    a person reads "when did I watch this"."""
    from datetime import datetime, timezone
    text = str(stamp or "").strip()
    if not text:
        return ""
    try:
        when = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return ""
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    gap = datetime.now(timezone.utc) - when
    minutes = gap.total_seconds() / 60
    if minutes < 2:
        return "Just now"
    if minutes < 60:
        return f"{int(minutes)}m ago"
    if minutes < 60 * 24:
        return f"{int(minutes // 60)}h ago"
    if gap.days < 7:
        return f"{gap.days}d ago"
    return when.astimezone().strftime("%d %b")


def _history_page(side="watch"):
    """Everything opened, newest first, on one side of the split.

    **A list of rows, not a strip of posters.** The owner, 2 September
    2026: "the history is not showing like it was in the Qt, make it the
    same design with keeping the WebView2". tracker._build_history_row is
    what it was: a 46x62 cover, the title with its progress under it, and
    on the right when it was opened over an "In Saved" / "Not saved" tag
    - that tag being the whole reason the section exists, since a title
    reached through Discover leaves a trace here and nowhere else.

    The header carries the count and Clear History, exactly as the Qt
    page's did.
    """
    side = side if side in ("watch", "read") else "watch"
    kinds = READING_KINDS if side == "read" else VIDEO_KINDS
    rows = [r for r in _rows("history.json")
            if str(r.get("type", "")).lower() in kinds]
    rows.sort(key=lambda r: str(r.get("last_opened") or ""), reverse=True)
    saved = _saved_sides()

    out = []
    for entry in rows:
        row = _row(entry)
        # **No tick count on the line at all** - the owner, 4 September
        # 2026: "do not show how many ch/ep marked at all!". It had said
        # "7 episodes marked" beside S01E01 about seven *specials*
        # (helpers/episode_watch_state_patch), and counting only the
        # placeable marks was still one more number on a row whose one
        # useful number is the one in front of it.
        #
        # `metabase` is the rest of the line either side of that number,
        # so app.js can replace the number in place when a mark is
        # written rather than redrawing the page (see _progress_now and
        # progressInto). With nothing else on the line it is empty, and
        # the separator with it.
        row["meta"] = row["prog"]
        row["metabase"] = ""
        row["sep"] = ""
        row["when"] = _history_when(entry.get("last_opened"))
        row["saved"] = _is_saved(entry, saved)
        out.append(row)
    return {"kind": "history", "hero": None, "title": "History",
            "tabs": TABS, "tab": side, "rows": out,
            "note": (f"{len(out)} title{'s' if len(out) != 1 else ''}"
                     if out else
                     "Nothing here yet. Anything you play or read shows up "
                     "here - including titles you have not saved.")}


def _when_words(at):
    """"Tomorrow", "Today", or the weekday - the Qt page's own headings."""
    from datetime import datetime, timezone
    try:
        when = datetime.fromisoformat(str(at).replace("Z", "+00:00"))
    except ValueError:
        return "Scheduled", "", ""
    local = when.astimezone()
    today = datetime.now(timezone.utc).astimezone().date()
    days = (local.date() - today).days
    head = ("Today" if days == 0 else "Tomorrow" if days == 1
            else local.strftime("%A") if 0 < days < 7
            else local.strftime("%d %b"))
    slot = local.strftime("%A %I:%M %p").replace(" 0", " ")
    left = when - datetime.now(timezone.utc)
    total = int(left.total_seconds())
    if total < 0:
        countdown = "now"
    elif total < 3600:
        countdown = f"{total // 60}m"
    elif total < 86400:
        countdown = f"{total // 3600}h {(total % 3600) // 60}m"
    else:
        countdown = f"{total // 86400}d {(total % 86400) // 3600}h"
    return head, slot, countdown


def _calendar_rows():
    """The week's airing calendar, off disk.

    `schedule_cache.json` is written by tracker._save_upcoming_calendar
    and is the *only* thing that ever knew about a show the owner has not
    saved - AniList's 403 is why it exists at all (tracker's own note).
    Read here rather than fetched: this server holds no network budget,
    and tracker.prewarm_discover queues _fetch_upcoming_calendar at every
    launch whenever the in-memory copy is over 12h old - which is what
    keeps the file current now that the Qt Schedule tab, the other thing
    that used to refill it, is no longer built at all.
    """
    from datetime import datetime, timezone
    try:
        stored = json.loads(
            (DATA / "schedule_cache.json").read_text(encoding="utf-8-sig"))
        age = time.time() - float(stored["at"])
    except Exception:
        return []                    # unreadable, or no stamp on it
    if age < 0 or age > CALENDAR_TTL_S:
        return []
    now = datetime.now(timezone.utc)
    found = []
    for row in (stored.get("rows") or []):
        if not isinstance(row, dict) or not row.get("at"):
            continue
        try:
            when = datetime.fromisoformat(str(row["at"]).replace("Z", "+00:00"))
        except ValueError:
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        if when <= now:
            continue                  # already out; the Qt page drops it too
        found.append((when.isoformat(), row))
    found.sort(key=lambda item: item[0])
    return found


def _released_rows():
    """The Read side's "Recently Released" - the same rows Discover's
    Latest strip draws, which is the one list the Qt page used here
    (tracker._fetch_released_rows shares its cache entry). No dates:
    nobody announces a scanlation, so these are what has *landed*."""
    try:
        cached = json.loads(
            (DATA / "discover_cache.json").read_text(encoding="utf-8-sig"))
    except Exception:
        return []
    block = cached.get("reading_latest") if isinstance(cached, dict) else None
    rows = (block or {}).get("rows") if isinstance(block, dict) else None
    # **Reading kinds only, one row per title.** The owner, 2 September
    # 2026: "in the reading schedule there is HxH anime!!!!". Measured
    # that day: the row was 3asq's *manga* (its poster is the volume 35
    # cover, the url its chapter list) sitting first because the site's
    # front page leads with a popular-titles slider that
    # manga_sites._browse_html now skips - so the wrong picture was a
    # symptom of the wrong order, not of a video row. This guard is for
    # the day a video row does land in the block: nothing here can show
    # an episode, so a Watch kind is dropped rather than listed as a
    # chapter. Duplicates go too - the same cache held Hunter X Hunter
    # at seven indexes (a slider card and a wall card share a url, but
    # two sites do not), and a schedule listing one title twice reads as
    # two releases.
    out, seen = [], set()
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        if str(row.get("type") or "").strip().lower() in _WATCH_KINDS:
            continue
        key = " ".join(str(row.get("title") or "").lower().split())
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(row)
        if len(out) >= RELEASED_LIMIT:
            break
    return out


_WATCH_KINDS = frozenset({"anime", "series", "movie", "movies", "video"})


def _schedule_row(entry, when="", saved=False):
    row = _row(entry)
    head, slot, countdown = _when_words(when) if when else ("", "", "")
    row.update({"day": head, "slot": slot, "countdown": countdown,
                "saved": bool(saved), "progress": _progress_text(entry)})
    return row


def _schedule(side="watch"):
    """What is due: everything saved first, then everything else.

    **Two groups, and every saved row above every unsaved one** - the
    owner's ask twice over, most recently 2 September 2026: "in the
    schedule make all appear and the saved on top like the old Qt
    design". tracker._schedule_rows is explicit that this is not one
    sorted list with a tiebreak, because a tiebreak only orders within a
    shared clock slot.

    It showed one row before this, because it read `next_release` off the
    saved files and nothing else - and `next_release` is only ever
    written onto a title the owner keeps. Everything the Qt page listed
    under "Airing Soon" comes from the calendar cache, which this never
    opened (see _calendar_rows), so 40 rows were sitting on disk unread.
    """
    side = side if side in ("watch", "read") else "watch"
    saved_entries = _side_rows(side)
    saved_titles = {str(e.get("title") or "").strip().lower()
                    for e in saved_entries}

    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    mine = []
    for entry in saved_entries:
        block = entry.get("next_release")
        at = str((block or {}).get("at") or "") if isinstance(block, dict) else ""
        if not at:
            continue
        try:
            when = datetime.fromisoformat(at.replace("Z", "+00:00"))
        except ValueError:
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        if when <= now:
            continue
        mine.append((when.isoformat(), entry))
    mine.sort(key=lambda item: item[0])
    saved_rows = [_schedule_row(entry, when, saved=True)
                  for when, entry in mine]

    # **Only what he actually keeps.** The owner, 4 September 2026:
    # "make sure that the schedule cards be from watch and read we really
    # have!", with a screenshot of one of these rows opening a details
    # page that read "This entry has no matched title, so there is no
    # episode list to show."
    #
    # That is what a calendar row is: `_calendar_rows` reads
    # schedule_cache.json, which is the week's whole airing calendar and
    # was written precisely because it "is the only thing that ever knew
    # about a show the owner has not saved". A title he does not keep has
    # no entry, so opening it builds a transient one with no id - nothing
    # to list episodes from, and nothing for a mark to be written onto,
    # which is the second half of his report ("mark as watched ... when I
    # enter from the schedule page they have the old issue").
    #
    # This reverses the 2 September ask that put the whole calendar here
    # ("in the schedule make all appear"), and it is the later
    # instruction: a row that cannot be opened is worse than a shorter
    # list. The Saved block above is untouched.
    # **Every row is one of his own entries.** The owner, 4 September
    # 2026: "make sure that the schedule cards be from watch and read we
    # really have!", with a screenshot of a row that opened a details
    # page reading "This entry has no matched title, so there is no
    # episode list to show."
    #
    # That is what these rows were. `_calendar_rows` reads
    # schedule_cache.json, which its own docstring calls "the *only*
    # thing that ever knew about a show the owner has not saved", and
    # this loop kept exactly those - `title.lower() in saved_titles` was
    # a *skip*. A title he does not keep has no entry, so opening it
    # builds a transient one with no id: nothing to list episodes from,
    # and nothing for a tick to be written onto, which is the second half
    # of his report ("the mark as watched and unwatched ... when I enter
    # from the schedule page they have the old issue").
    #
    # So the calendar is now a source of *dates* for titles he already
    # has, and the row itself is built from his entry - its id, its
    # cover, its progress. The Saved block above is untouched; this one
    # catches what that block misses, which is a title of his with no
    # `next_release` stamp written onto it yet.
    mine_by_title = {}
    for entry in saved_entries:
        key = " ".join(str(entry.get("title") or "").lower().split())
        if key and key not in mine_by_title:
            mine_by_title[key] = entry
    already = {str(e.get("id") or "") for _w, e in mine}

    def _his(title):
        return mine_by_title.get(" ".join(str(title or "").lower().split()))

    # **Both: his own first, then everything else airing.** The owner, 4
    # September 2026, in two steps - "make sure that the schedule cards
    # be from watch and read we really have!", and then "re-add the
    # schedule cards even the unsaved, and make sure to show the ones in
    # the app!!!! like the Qt before".
    #
    # The first version of this loop kept only what he does *not* have
    # (`title in saved_titles` was a `continue`), which is why a row
    # opened a page reading "This entry has no matched title": a title he
    # does not keep has no entry, so nothing could list its episodes or
    # take a tick. Keeping only his own answered that and emptied the
    # Watch tab - measured on his files, **none** of the forty calendar
    # rows is one of his three watched titles.
    #
    # So both, in that order. A row of his is built from his entry - its
    # id, its cover, its progress - and opens exactly as it does from
    # Home. A row that is not his is the calendar's own, as it was
    # before, and opens the details page on its title and picture; the
    # calendar carries no IMDb id for any of its forty rows (measured),
    # so an unsaved one still has no episode list to show, and that is
    # the same answer the Qt page gave.
    rest, others = [], []
    if side == "read":
        for item in _released_rows():
            entry = _his(item.get("title"))
            if entry is not None:
                if str(entry.get("id") or "") in already:
                    continue
                rest.append(_schedule_row(entry, saved=True))
                already.add(str(entry.get("id") or ""))
                continue
            row = dict(item)
            row.setdefault("cover_url", item.get("poster") or "")
            others.append(_schedule_row(row))
    else:
        for when, item in _calendar_rows():
            entry = _his(item.get("title"))
            if entry is not None:
                if str(entry.get("id") or "") in already:
                    continue
                row = _schedule_row(entry, when, saved=True)
                episode = str(item.get("episode") or "").strip()
                if episode and not row.get("progress"):
                    row["progress"] = f"E{episode}"
                rest.append(row)
                already.add(str(entry.get("id") or ""))
                continue
            row = dict(item)
            episode = str(item.get("episode") or "").strip()
            if episode:
                row["progress"] = f"E{episode}"
            others.append(_schedule_row(row, when))
    rest += others

    blocks = []
    if saved_rows:
        blocks.append({"title": "Saved",
                       "note": "Upcoming releases from your Saved list",
                       "rows": saved_rows})
    if rest:
        blocks.append({"title": ("Recently Released" if side == "read"
                                 else "Airing Soon"),
                       "note": ("The newest chapters from your sites"
                                if side == "read"
                                else "Everything else airing this week"),
                       "rows": rest})
    total = len(saved_rows) + len(rest)
    return {"kind": "schedule", "hero": None, "title": "Schedule",
            "tabs": TABS, "tab": side, "blocks": blocks,
            "note": (f"{len(saved_rows)} saved  ·  {len(rest)} more"
                     if total else "nothing scheduled")}


# How many rows one "load more" asks for. tracker._category_more_worker
# passes its own DISCOVER_LIMIT (30) for both halves of this, and the
# measurement quoted below was taken with that number - so this is 30 and
# deliberately *not* this module's DISCOVER_LIMIT, which is 60 because it
# caps a Discover *section* rather than a page.
#
# **It went missing, and that is worth recording.** A block of this file
# was duplicated - eleven functions defined twice, the stale copy
# shadowing the newer one - and removing the duplicate took this constant
# with it, because it lived only in the removed half. The check
# afterwards looked for names defined *twice* and never for names that
# had become undefined, so `_more` raised NameError on every call and
# answered `{"rows": [], "error": ...}`. The page then counted two dry
# batches and stopped asking: the owner's "the watch and read pages do
# not load when scrolling down", twice.
MORE_LIMIT = 30


def _more(route, have, skip):
    """The next batch past what is already on screen.

    tracker._category_more_worker's own split, and its measurement:
    "Video kinds page Cinemeta by `skip`, the *source's* cursor, not the
    screen's row count - the two drift apart and paging by the screen
    count is what killed the Series section. The reading kinds have no
    offset to ask for: the sweep is re-run wider (limit = `have` + one
    page) and the overlap is dropped by title" - +29 new rows for Manhwa,
    +25 Other, +11 Manhua, +3 Manga on his own sites.
    """
    if route.startswith(("genre:", "cast:")):
        return _more_browse(route, have, skip)
    kind = BROWSE_CACHE.get(route, "")
    if not kind:
        return {"rows": [], "skip": skip}
    try:
        from helpers import discover
        if kind.startswith("medium:"):
            medium = kind.split(":", 1)[1]
            found = discover.reading_sites_by_medium_all(limit=have + MORE_LIMIT)
            rows = (found or {}).get(medium) or []
        else:
            video = "movie" if route == "movies" else route
            rows = discover.discover_video(video, query="", limit=MORE_LIMIT,
                                           skip=skip)
    except Exception as error:
        return {"rows": [], "skip": skip, "error": str(error)[:120]}
    rows = [r for r in (rows or []) if isinstance(r, dict) and r.get("title")]
    saved = _saved_sides()
    return {"rows": [_grid_row(r, saved) for r in rows],
            "skip": skip + len(rows)}


# One Cinemeta genre page is 50 rows (measured 2 September 2026: a
# genre catalog asked for 60 answered 50, series and movies alike), so a
# scroll batch asks for exactly one page per kind rather than MORE_LIMIT
# - a 30-row slice of a 50-row page is the same request paid twice.
GENRE_PAGE = 50
# The first cast page. TMDB answers a whole filmography in one call
# (62 credits for Alan Ritchson, measured), so this is a slice of memory
# and the size is only how much the page draws before its first scroll.
CAST_PAGE = 60


# The three kinds a video genre or a cast page can be filtered to, and
# what each asks its source for. "All" is the page as it was.
BROWSE_TABS = (("all", "All", ()),
               ("anime", "Anime", ("anime",)),
               ("series", "Series", ("series",)),
               ("movies", "Movies", ("movie", "movies")))


def _tab_rows(rows, tab):
    """`rows` filtered to one of BROWSE_TABS. "all" keeps everything.

    The type is the row's own - discover._video_row and people._row both
    write it, and people's is already Anime/Series/Movie (it reads
    TMDB's animation genre plus a Japanese origin, see helpers/people).
    """
    wanted = dict((key, kinds) for key, _label, kinds in BROWSE_TABS).get(tab)
    if not wanted:
        return rows
    return [r for r in rows
            if str(r.get("type") or "").strip().lower() in wanted]


def _browse_tabs(tab):
    return [{"key": key, "label": label} for key, label, _k in BROWSE_TABS], (
        tab if tab in dict((k, 1) for k, _l, _n in BROWSE_TABS) else "all")


def _genre_video(name, skip, limit):
    """One page of a video genre, series and movies both, from one
    cursor: (rows, next cursor).

    **One skip for both kinds, advanced by the larger batch.** Two
    cursors packed into the one integer the page hands back was the
    alternative, and it buys nothing: both catalogs page from 0 in
    lockstep while they last, and once the shorter one is spent every
    later skip is past its end and answers [], so nothing is ever served
    twice and the cursor never has to know which of the two ran out.
    Dry when both answer nothing.

    **Both catalogs at once, not one after the other.** Measured 3
    September 2026 on Action: a Cinemeta genre page is 0.3-1.0s when its
    CDN has it and 2-10s when it does not, and the serial version paid
    both in a row - 9.9s for the skip=50 batch, 5.1s for skip=100, 6.6s
    for the empty answer past the end. Two workers make a batch cost its
    slower page rather than the sum: per-kind pages measured serially
    at skips 250-400 summed to 4.3 / 1.4 / 1.3 / 3.3s, while the
    parallel batch (skips 750-900, so not the same pages - Cinemeta's
    jitter is per request) took 1.9 / 1.1 / 2.9 / 1.2s of wall, and the
    dry answer past the end 0.44s. The page stops after two dry
    batches (app.js moreOnScroll), so the end costs under a second."""
    from concurrent.futures import ThreadPoolExecutor
    from helpers import discover

    # **The cursor has to advance by pages read, not by rows kept.** The
    # owner, 4 September 2026: "in the anime page, when I select Romance
    # in the filter it does show only 1 anime, while there are more as
    # romance!". The count is answered by app.js pullGenre walking this
    # route - and the walk was standing still. Measured that day, asking
    # for Romance anime four times in a row:
    #
    #   /api/genre        14 rows   unique 14
    #   /api/more skip=50 20 rows   unique 14   (every one already shown)
    #   skip=100          30 rows   unique 14
    #   skip=150           5 rows   unique 14
    #
    # Because anime is series-filtered-by-genre at the Cinemeta end, the
    # genre is applied to the *rows* and discover_video walks several
    # catalog pages to fill one answer (rules/integrations.md). It read
    # ~200 rows to hand back 20 - and `advanced` below counted the 20,
    # so the next batch re-walked almost exactly the same pages. Each
    # kind now reports how far it actually read, and the cursor takes
    # the furthest of the three.
    progress = {}

    def _page(kind):
        try:
            return discover.discover_video(kind, genre=name, limit=limit,
                                           skip=skip, reached=progress) or []
        except Exception:
            return []

    # **Three catalogs, not two.** The genre page grew Anime / Series /
    # Movies tabs on 3 September 2026, and the Anime tab came back empty
    # every time: Cinemeta's genre catalogs answer `series` and `movie`
    # and nothing else, so there was never an anime row to show. The
    # anime catalog is the same call with a different kind - the Anime
    # page has always used it - and asking all three costs the slowest
    # of them rather than their sum, which is why this pool exists.
    rows, advanced = [], 0
    with ThreadPoolExecutor(max_workers=3) as pool:
        for batch in pool.map(_page, ("anime", "series", "movie")):
            rows.extend(batch)
            advanced = max(advanced, len(batch))
    # `progress` is shared by the three workers and only ever grows, so
    # the read is safe without a lock (see _note_reached).
    advanced = max(advanced, int(progress.get("skip") or 0) - skip)
    return rows, skip + max(1, advanced)


def _more_browse(route, have, skip):
    """The next batch of a genre or cast page - the owner, 2 September
    2026: "make the same genres/cast member pages load when I scroll
    down until there is no more to load at all". Before this the genre
    page answered no `browse` key at all, so the page never asked (100
    rows of Action, then nothing, measured)."""
    tab = "all"
    try:
        if route.startswith("cast:"):
            body, _sep, tab = route[len("cast:"):].rpartition(":")
            if tab not in dict((k, 1) for k, _l, _n in BROWSE_TABS):
                body, tab = route[len("cast:"):], "all"
            # The whole filmography is one cached list, so a tab is a
            # slice of the filtered list rather than a new request.
            from helpers import people
            everything = _tab_rows(people.page(body, 0, 10000), tab)
            rows = everything[skip:skip + MORE_LIMIT]
            skip = skip + len(rows)
        else:
            rest, _sep2, tab = route[len("genre:"):].rpartition(":")
            if tab not in dict((k, 1) for k, _l, _n in BROWSE_TABS):
                rest, tab = route[len("genre:"):], "all"
            body, _sep, flag = rest.rpartition(":")
            if flag == "1":
                # No cursor at the reading end: the sweep is re-run wider
                # and the page drops what it already shows by title,
                # exactly as _more does for the medium pages.
                from helpers import discover
                rows = discover.reading_genre_sites(body,
                                                    limit=have + MORE_LIMIT)
                skip = skip + len(rows)
            else:
                rows, skip = _genre_video(body, skip, GENRE_PAGE)
    except Exception as error:
        return {"rows": [], "skip": skip, "error": str(error)[:120]}
    rows = [r for r in (rows or []) if isinstance(r, dict) and r.get("title")]
    if not route.startswith("cast:"):
        rows = _tab_rows(rows, tab)
    saved = _saved_sides()
    return {"rows": [_grid_row(r, saved) for r in rows], "skip": skip}


def _cast(name, tab="all"):
    """Everything one cast member was in - the cast chips as doors, the
    owner's ask of 2 September 2026 ("make the cast also clickable as
    the genres"). helpers.people asks TMDB and keeps the whole list for
    the session; this draws the first CAST_PAGE and `browse` lets the
    page scroll for the rest."""
    name = str(name or "").strip()
    if not name:
        return {"kind": "grid", "rows": [], "title": "", "note": "",
                "back": True}
    try:
        from helpers import people
        rows, note = people.filmography(name)
    except Exception as error:
        rows, note = [], str(error)[:120]
    rows = [r for r in rows if isinstance(r, dict) and r.get("title")]
    # **Anime / Series / Movies, as on the genre page.** The owner, 3
    # September 2026: "in the genre and the cast pages add tabs Anime
    # Series Movies". A filmography is one list from TMDB with the kind
    # already on every row (helpers.people._row), so this is a filter
    # rather than a second request - which is why it is applied before
    # the page is cut to CAST_PAGE and why `skip` counts the *filtered*
    # rows the page is showing.
    tabs, tab = _browse_tabs(tab)
    rows = _tab_rows(rows, tab)
    first = rows[:CAST_PAGE]
    saved = _saved_sides()
    return {"kind": "grid", "hero": None, "title": name,
            "browse": f"cast:{name}:{tab}", "skip": len(first),
            "browsetabs": tabs, "browsetab": tab,
            # **A way out.** The owner, 3 September 2026: "in the same
            # genere/ cast pages there is no back button, add it". These
            # two routes are drawn inside web_reader's shell, which
            # covers the whole window - the app's own bar is behind it
            # and Escape was the only exit. `back` is what makes app.js
            # draw the door (see backRow).
            "back": True,
            "note": (f"{len(rows)} titles" if rows
                     else (note or "nothing under this name")),
            "rows": [_grid_row(r, saved) for r in first]}


def _genre(name, reading, tab="all"):
    """Everything filed under one genre - the genre browse, as a grid.

    Same sources details.GenreBrowsePage asks: reading genres come from
    the owner's *own* sites (discover.reading_genre_sites, the browse the
    Manga/Manhwa/Manhua sections use) rather than MangaDex's tag browse,
    whose cards carry no site url and can only ever open the "where
    should this be read from" flow. Video genres come from Cinemeta's
    genre catalogs, series and movies both.
    """
    name = str(name or "").strip()
    if not name:
        return {"kind": "grid", "rows": [], "title": "", "note": "",
                "back": True}
    rows, skip = [], 0
    try:
        from helpers import discover
        if reading:
            try:
                rows = list(discover.reading_genre_cached(name, limit=120) or [])
            except Exception:
                rows = []
            if not rows:
                rows = list(discover.reading_genre_sites(name, limit=120) or [])
        else:
            rows, skip = _genre_video(name, 0, GENRE_PAGE)
    except Exception as error:
        return {"kind": "grid", "rows": [], "title": name,
                "back": True, "note": str(error)[:120]}
    rows = [r for r in rows if isinstance(r, dict) and r.get("title")]
    # A reading genre has one kind and no tabs to offer; a video genre
    # is two Cinemeta catalogs and anime mixed together, which is what
    # the tabs are for.
    tabs, tab = _browse_tabs(tab)
    if not reading:
        rows = _tab_rows(rows, tab)
    saved = _saved_sides()
    # `browse` is what makes the page ask for more on scroll (app.js
    # moreOnScroll), and `skip` is the source's cursor after this first
    # page - not the row count, which for a video genre is two catalogs'
    # worth. _more_browse continues from it.
    return {"kind": "grid", "hero": None, "title": name,
            "browse": f"genre:{name}:{'1' if reading else '0'}:{tab}",
            "skip": skip if not reading else len(rows),
            "browsetabs": [] if reading else tabs, "browsetab": tab,
            "back": True,                      # see _cast
            "note": f"{len(rows)} titles" if rows else "nothing under this one",
            "rows": [_grid_row(r, saved) for r in rows]}


def _warm_covers(body):
    """Fetch this answer's pictures behind it - see backend.warm.

    Every shape this module answers with is walked, because they all
    carry `/img/<token>` covers: a grid's `rows`, a sections page's
    `sections[].rows`, and the list pages' own `rows`. The tokens are
    handed over in the order they will be drawn in, so the head of the
    page is warmed first.
    """
    if not isinstance(body, dict):
        return body
    tokens = []

    def take(rows):
        for row in rows or ():
            if not isinstance(row, dict):
                continue
            cover = str(row.get("cover") or "")
            if cover.startswith("/img/"):
                tokens.append(cover[5:])

    take(body.get("rows"))
    for section in body.get("sections") or ():
        if isinstance(section, dict):
            take(section.get("rows"))
    for block in body.get("blocks") or ():
        if isinstance(block, dict):
            take(block.get("rows"))
    if tokens:
        backend.warm(tokens)
    return body


def answer(route, query=None):
    query = query or {}
    one = lambda k, d="": (query.get(k) or [d])[0]      # noqa: E731

    if route in SECTIONS:
        return _medium(route)
    if route == "genre":
        return _genre(one("name"), one("reading") == "1", one("tab", "all"))
    if route == "cast":
        return _cast(one("name"), one("tab", "all"))
    if route == "saved":
        return _saved(one("tab", "watch"))
    if route == "history":
        return _history_page(one("tab", "watch"))
    if route == "schedule":
        return _schedule(one("tab", "watch"))
    if route in SHELVES:
        return _shelf(route)
    if route == "downloads":
        return _downloads()
    if route == "more":
        def _int(name):
            try:
                return int(one(name, "0") or 0)
            except ValueError:
                return 0
        return _more(one("medium"), _int("have"), _int("skip"))
    if route == "browse":
        return _browse(one("medium"))
    if route == "progress":
        return _progress_now()
    if route == "people":
        return _people(one("q"))
    if route == "cover":
        return _card_cover(one("title"), one("url"), one("thin") == "1",
                           one("imdb"), one("type"))
    if route == "home":
        return _home()
    if route == "discover":
        return _discover()
    if route == "featured":
        return backend.featured_art(one("title"), one("imdb"),
                                    one("type"))
    if route == "search":
        return _search(one("q"))
    if route == "settings":
        return backend.settings()
    if route == "hero":
        return backend.hero_meta(one("id"))
    if route == "chapters":
        return backend.chapters(one("id"), live=one("live") == "1")
    if route == "pages":
        try:
            index = int(one("i", "0"))
        except ValueError:
            index = 0
        return backend.pages(one("id"), index)
    if route == "read_state":
        return backend.read_state(one("id"))
    if route == "mark":
        return backend.mark_read(one("id"), one("key"),
                                 one("read", "1") == "1")
    return {"kind": "rows", "sections": [], "note": "not built yet"}



# **Card art, decoded at the size it is drawn.** The owner: "reading card
# covers still blurry". They are not upscaled - measured, his are 844x1200
# and 764x1200 - they are downscaled by four, in the browser, on every
# paint. Qt never does that: images.thumbnail_or_avatar decodes at the
# target size and hands Qt a pixmap that needs no scaling.
#
# So the server scales instead, once, and caches the result. Through
# QImage because it is already in the process and reads every format the
# covers come in; a build without Qt (run.py serves these same routes)
# simply gets the original, which is what happened before this existed.
_SCALED = {}
_SCALED_CAP = 400


def _wanted_width(query):
    """The `w=` a card or a page asks for, in device pixels, or 0.

    **4000, not 2000.** The cap was written for cards, which ask for
    200; a reader page is drawn 1100-1900 logical pixels wide and asks
    in *device* pixels, so on a 1.25 display it asks for up to 2400 and
    the old ceiling silently halved it.
    """
    try:
        return max(0, min(4000, int((query.get("w") or ["0"])[0])))
    except (TypeError, ValueError):
        return 0


def _remember_scaled(key, value):
    """**Evict the oldest, never refuse the newest.** This used to stop
    scaling altogether once the dict held 400 entries, and a session
    crosses 400 easily - measured 3 September 2026: one Discover answer
    emits 270 distinct cover URLs, Home 32, a catalogue page 60 - after
    which every later picture went out at its original size (780x439
    originals average 382KB against 85KB scaled) and the browser
    downscaled it on every paint: the blurry-cover bug this cache exists
    to fix, back for the rest of the session. The dict is insertion
    ordered, so the oldest entry is the first key."""
    while len(_SCALED) >= _SCALED_CAP:
        try:
            del _SCALED[next(iter(_SCALED))]
        except (StopIteration, KeyError, RuntimeError):
            break
    _SCALED[key] = value


def _enlarged(blob, width):
    """`blob` enlarged to `width` pixels with PIL, or None.

    Separate from _scaled's Qt path because enlarging and reducing want
    different resamplers - see the table at the call site for the four
    that were measured. Returns None on anything unexpected, which puts
    the caller back on Qt.
    """
    try:
        from PIL import Image, ImageFilter
        image = Image.open(io.BytesIO(blob))
        image.load()
        if image.width <= 0:
            return None
        height = max(1, round(image.height * width / image.width))
        big = image.resize((width, height), Image.LANCZOS)
        big = big.filter(ImageFilter.UnsharpMask(radius=1.0, percent=60,
                                                 threshold=3))
        store = io.BytesIO()
        if big.mode in ("RGBA", "LA", "P"):
            # Same rule as the Qt path below: JPEG has no alpha and
            # writes transparent pixels black.
            big.save(store, "PNG", compress_level=1)
            return store.getvalue(), "image/png"
        if big.mode != "RGB":
            big = big.convert("RGB")
        big.save(store, "JPEG", quality=95)
        return store.getvalue(), "image/jpeg"
    except Exception:
        return None


def _scaled(blob, kind, width, exact=False):
    """`blob` resampled to `width` device pixels, or as it came.

    `exact` is the reader's mode and it is the whole of the owner's "the
    manga still has no size and quality as before in Qt", 4 September
    2026.

    **What Qt did, and what a browser does instead.** windows/reader.py
    decodes a page once at the size it will occupy - `scaledToWidth(...,
    SmoothTransformation)` - tags the pixmap with the screen's device
    ratio and blits it 1:1. The browser was handed the scan at its own
    resolution with a CSS width beside it, so *the compositor* resampled
    it on every paint, with the cheap filter it uses for that. Same
    source, same drawn size, different resampler - and on a manga page,
    which is line art on white, that filter is exactly what softens the
    inking.
    
    So the reader now asks for the page at its drawn width in device
    pixels and this does the resample, with Qt, once, and caches it -
    which is Qt's pipeline exactly. Two rules differ from the card path
    because of it:

      * **it may scale up.** A card never wants that (a 250px cover
        blown up to 200 is nonsense), but a manga page drawn at 1100
        logical px on a 1.25 display is 1375 device pixels and Qt made
        those pixels itself. Cards keep the old "already about right,
        leave it" shortcut; the reader does not.
      * **quality 95, not 92.** A page is the thing being read rather
        than a thumbnail beside a title, and the difference is a few
        percent of bytes on an image that is already megabytes.
    """
    if not blob or width <= 0:
        return blob, kind
    key = (hash(blob), width, bool(exact))
    hit = _SCALED.get(key)
    if hit is not None:
        return hit
    try:
        from PyQt6.QtCore import QBuffer, QByteArray, QIODevice
        from PyQt6.QtGui import QImage
        image = QImage()
        if not image.loadFromData(blob):
            return blob, kind
        if image.width() == int(width):
            _remember_scaled(key, (blob, kind))
            return blob, kind
        if not exact and image.width() <= width * 1.15:
            # Already about the right size - scaling it would only cost
            # a decode and lose a little.
            _remember_scaled(key, (blob, kind))
            return blob, kind
        if exact and width > image.width():
            # **An enlargement is a different job from a reduction, and
            # Qt is not the tool for it.** The owner, 4 September 2026:
            # "make the small pages bigger, keep them sharp".
            #
            # Whole chapters are scanned small - One Piece ch906 is 890
            # to 921 px across all sixteen pages, against a medium
            # target of 1100 - so drawing them at their own resolution
            # is the "small" he is complaining about, and enlarging them
            # is the only way to make them the size of every other
            # chapter. What is left is *who* enlarges, and the four
            # candidates were measured on that page, taken to the 1375
            # device pixels it occupies (mean absolute gradient - edge
            # contrast, which on line art is exactly the "sharp"):
            #
            #   the browser's own stretch (bilinear)   10.18
            #   Qt scaledToWidth, SmoothTransformation 10.33   50ms
            #   PIL LANCZOS                            12.04   31ms
            #   PIL LANCZOS + UnsharpMask(1.0, 60, 3)  13.53   86ms
            #
            # So the enlargement Qt was doing was worth almost nothing
            # over letting the compositor do it, and this is 31% more
            # edge contrast than either. PNG was measured too and buys
            # nothing (13.41) for 2.6x the bytes and 40% more time - the
            # loss is in the resampler, not the encoder. 80% unsharp
            # scored 13.77 and was left alone: past this the inking
            # starts to carry a halo.
            out = _enlarged(blob, int(width))
            if out is not None:
                _remember_scaled(key, out)
                return out
        from PyQt6.QtCore import Qt as _Qt
        small = image.scaledToWidth(
            int(width), _Qt.TransformationMode.SmoothTransformation)
        store = QByteArray()
        buffer = QBuffer(store)
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        # **PNG when the picture has transparency, JPEG when it does
        # not.** JPEG has no alpha, so Qt writes the transparent pixels
        # as black - which is the owner's "remove the black icon BG from
        # the websites logos", 2 September 2026. It was never a
        # background: every one of his website icons is a 512x512 RGBA
        # PNG, a card asks for 200 device pixels, 512 > 200 x 1.15 so
        # every one of them was scaled - and re-encoded to JPEG on the
        # way out. The four that looked right (the apps' `art`) are
        # RGB with nothing to lose.
        #
        # Covers stay JPEG: they are photographs, they have no alpha,
        # and a 200px PNG of one is several times the bytes.
        if image.hasAlphaChannel():
            saved = small.save(buffer, "PNG")
            out_kind = "image/png"
        else:
            saved = small.save(buffer, "JPG", 95 if exact else 92)
            out_kind = "image/jpeg"
        if not saved:
            return blob, kind
        buffer.close()
        out = (bytes(store), out_kind)
    except Exception:
        return blob, kind
    _remember_scaled(key, out)
    return out


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        return                    # the console is not a request log

    def _send(self, body, kind, cache=False):
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        # Pictures never change under a given URL and are worth keeping.
        # Everything else must not be cached: the app writes these JSON
        # files while the page is open, and a cached answer is a page
        # showing what was true when it last looked. Measured - a fixed
        # discover route kept reporting the old count from cache while
        # the server returned the new one to curl.
        self.send_header("Cache-Control",
                         "max-age=86400" if cache else "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path, _, raw = self.path.partition("?")
        query = urllib.parse.parse_qs(raw)

        if path.startswith("/api/"):
            # A route that raises used to print to a stderr the frozen
            # exe does not have and hand the page a dropped fetch - an
            # empty surface with nothing in atomic.log (review, 3
            # September 2026). Logged, and answered with an error the
            # page can show.
            try:
                # Warmed here rather than inside each route: every
                # answer goes through this one place, and the warming
                # must happen *after* the body is built (the tokens are
                # registered by backend.cover_url as the rows are made)
                # and *before* it is sent, so the fetches are already
                # under way when the browser starts asking.
                body = json.dumps(_warm_covers(answer(path[5:], query)),
                                  ensure_ascii=False)
            except Exception as error:
                try:
                    from helpers import logs
                    logs.exception(f"web route {path} failed")
                except Exception:
                    pass
                body = json.dumps({"kind": "rows", "sections": [],
                                   "rows": [], "note": "",
                                   "error": str(error)[:160]},
                                  ensure_ascii=False)
            self._send(body.encode("utf-8"),
                       "application/json; charset=utf-8")
            return

        if path.startswith("/cover/"):
            target = COVERS / pathlib.Path(path[7:]).name
            try:
                blob = target.read_bytes()
            except OSError:
                self.send_error(404)
                return
            kind = "image/webp"
            for mime, magic in MAGIC.items():
                if blob.startswith(magic):
                    kind = mime
                    break
            blob, kind = _scaled(blob, kind, _wanted_width(query))
            self._send(blob, kind, cache=True)
            return

        if path.startswith("/img/"):
            blob, kind = backend.fetch_image(path[5:])
            if blob is None:
                self.send_error(404)
                return
            # `exact=1` is the reader asking for the page at the size it
            # will actually be drawn - see _scaled.
            blob, kind = _scaled(blob, kind, _wanted_width(query),
                                 exact=(query.get("exact") or [""])[0] == "1")
            self._send(blob, kind, cache=True)
            return

        if path.startswith("/static/"):
            target = STATIC / pathlib.Path(path[8:]).name
            if not target.exists():
                self.send_error(404)
                return
            kind = ("text/css" if target.suffix == ".css"
                    else "application/javascript" if target.suffix == ".js"
                    else "text/plain")
            self._send(target.read_bytes(), kind + "; charset=utf-8")
            return

        self._send((STATIC / "index.html").read_bytes(),
                   "text/html; charset=utf-8")


def start(port=0):
    """Serve on a free port; returns the URL and the server."""
    server = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{server.server_address[1]}/", server
