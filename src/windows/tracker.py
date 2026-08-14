"""Anime & Manga & Series tracker: three pages sharing this list-widget
implementation. Anime/Manga share one data file split by the entry's
`type` field (so you can reclassify one into the other); Series has its
own file since it's a different domain entirely.

Typing a title searches a matching source in the background - Stremio's
Cinemeta catalog for Anime/Series, every reading site configured in
Settings for Manga - and picking a suggestion auto-fills the title/cover
and a direct link (a stremio:// deep link for Anime/Series, or the
matched manga's page on whichever site it came from) so double-click
jumps straight there. You can also just type your own title if it's not
listed there.
"""

import re
import threading
import uuid
import webbrowser

from PyQt6.QtCore import QObject, Qt, QTimer
from PyQt6.QtCore import pyqtSignal as Signal
from PyQt6.QtWidgets import (
    QComboBox, QDialog, QDoubleSpinBox, QFrame, QGridLayout, QHBoxLayout,
    QLabel, QLineEdit, QMenu, QMessageBox, QPushButton, QSpinBox,
    QVBoxLayout, QWidget,
)

from helpers import (
    anilist, anime_sites, app_settings, images, manga_sites, release_schedule,
    storage, stremio, theme,
)
from helpers.widgets import Card, GlassPage, finish_toast, scroll_area, show_toast

SORT_OPTIONS = ["Custom Order", "Name (A-Z)", "Date Added (Newest)", "Last Updated"]

# Manga/Manhwa/Manhua are just regional flavors of the same reading
# medium - same statuses, same search/open behavior - so they're treated
# as one group everywhere except the Type dropdown itself.
MANGA_TYPES = ("Manga", "Manhwa", "Manhua")

# Status wording differs by content type: you "watch" Anime/Series but
# "read" Manga/Manhwa/Manhua.
_READING_STATUSES = ["Reading", "Completed", "On Hold", "Dropped", "Plan to Read"]
STATUSES_BY_TYPE = {
    "Anime": ["Watching", "Completed", "On Hold", "Dropped", "Plan to Watch"],
    "Series": ["Watching", "Completed", "On Hold", "Dropped", "Plan to Watch"],
    **{t: _READING_STATUSES for t in MANGA_TYPES},
}
IN_PROGRESS_STATUSES = {"Watching", "Reading"}

# Older saves used one shared "Watching/Reading"-style status list; this
# maps those legacy values onto the new per-type wording during migration.
_LEGACY_STATUS_MAP = {
    "Watching/Reading": {"Anime": "Watching", "Series": "Watching", "Manga": "Reading"},
    "Plan to Watch/Read": {"Anime": "Plan to Watch", "Series": "Plan to Watch", "Manga": "Plan to Read"},
}

# What kind of search/launch each content type uses. Manga/Manhwa/Manhua
# have no Stremio presence (it's not video), so they search the Settings-
# configured reading sites instead and open a plain browser tab.
SEARCH_PROVIDER_BY_TYPE = {"Anime": "stremio", "Series": "stremio",
                            **{t: "manga_sites" for t in MANGA_TYPES}}
STREMIO_CATALOG_BY_TYPE = {"Anime": "series", "Series": "series"}

# Shown whenever a progress field gets auto-filled from "how far this
# release currently goes" rather than "what you've actually watched/read"
# (the latter needs a connected account - only Stremio supports that so
# far, via Settings > Stremio Account).
NOT_YOUR_PROGRESS_HINT = "Filled with the latest available - not your progress, adjust if you're behind"

POSTER_SIZE = (160, 216)
GRID_COLS = 9
PREVIEW_SIZE = (90, 120)
SEARCH_DEBOUNCE_MS = 450

_EPISODE_SEASON_RE = re.compile(r"^s(\d+)\s*e(\d+)$", re.IGNORECASE)
_EPISODE_ONLY_RE = re.compile(r"^e?(\d+)$", re.IGNORECASE)
_IMDB_ID_RE = re.compile(r"tt\d+")


def _imdb_id_from_url(url):
    """Pull the "tt1234567" id back out of a saved stremio:// deep link,
    so a saved entry can be re-queried later without having re-run the
    original title search."""
    match = _IMDB_ID_RE.search(url or "")
    return match.group(0) if match else None


def _entry_imdb_id(entry):
    """The Cinemeta id to sync progress against - stored directly on the
    entry (see EntryForm._save) since an Anime entry set to open on a
    Video Website other than the built-in "Stremio" one deliberately
    saves no url (see EntryForm._on_suggestion_selected), which would
    otherwise leave no way to recover it. Falls back to pulling it out of
    a saved stremio:// url for older entries saved before this field
    existed (see _migrate)."""
    return entry.get("imdb_id") or _imdb_id_from_url(entry.get("url"))


def _release_content(stored):
    """What a stored `next_release` says is *coming* - the episode or
    chapter number - as opposed to when it lands.

    This, and not the whole record, is what decides whether the refresh
    button turned up anything new. The time on its own can't: the manga
    estimate is extrapolated from a title's release rhythm (see
    mangadex._predict), so once a predicted slot has come and gone the
    next check simply projects the slot after it. That moving timestamp
    means nothing has happened yet, not that something has - reporting it
    as an update would make "no new chapter" indistinguishable from a
    chapter actually landing."""
    if not isinstance(stored, dict):
        return None
    return (stored.get("season"), stored.get("episode"), stored.get("chapter"))


def _latest_known_chapter(entry):
    """The highest chapter number this entry knows about - whichever is
    further along of the reading site's latest release and the user's own
    last-read chapter. Scanlation sites routinely run ahead of MangaDex,
    so this is what "next chapter" gets counted from rather than however
    far MangaDex's own feed happens to have got (see mangadex.
    fetch_next_chapter)."""
    if entry.get("type") not in MANGA_TYPES:
        return None
    return max(parse_chapter_progress(entry.get("progress")),
               entry.get("last_watched_chapter") or 0.0)


def parse_episode_progress(text):
    """"S1E10" / "E10" / "10" -> (season, episode) ints, or (0, 0) if the
    text doesn't match any of those (e.g. old freeform notes) - the form
    falls back to preserving that original text on save rather than
    silently replacing it with a blank/zero progress."""
    text = (text or "").strip()
    match = _EPISODE_SEASON_RE.match(text)
    if match:
        return int(match.group(1)), int(match.group(2))
    match = _EPISODE_ONLY_RE.match(text)
    if match:
        return 0, int(match.group(1))
    return 0, 0


def format_episode_progress(season, episode):
    if not season and not episode:
        return ""
    if season:
        return f"S{season:02d}E{episode:02d}"
    return f"E{episode:02d}"


def parse_chapter_progress(text):
    """Chapter numbers are sometimes fractional (scanlations split a
    chapter into e.g. "24.5"), so this is a float, not an int."""
    try:
        return float((text or "").strip())
    except ValueError:
        return 0.0


def format_chapter_progress(value):
    return "" if not value else f"{value:g}"


def open_tracker_entry(parent, entry):
    """Open an entry's page: for Manga, its matched page on its reading
    site (or that site's search results for the title, if no specific
    page was matched) - the configured Manga Music site (if any) opens
    first, so it starts loading/playing before the reading tab takes
    focus. For Anime, a saved stremio:// deep link (opens Stremio) if the
    entry uses the built-in Stremio option, or its configured Video
    Website's search results otherwise. For Series, a saved stremio://
    link or plain URL as-is. Returns False if there's nothing to open."""
    if entry.get("type") in MANGA_TYPES:
        return _open_manga_entry(parent, entry)
    if entry.get("type") == "Anime":
        return _open_anime_entry(parent, entry)

    url = entry.get("url")
    if url:
        if url.startswith("stremio://"):
            stremio.launch(url)
        else:
            webbrowser.open(url)
        return True
    return False


def _open_manga_entry(parent, entry):
    opened_url = entry.get("url")
    if not opened_url:
        site = manga_sites.get_site(entry["site_id"]) if entry.get("site_id") else None
        if site and site.get("base_url"):
            opened_url = manga_sites.search_page_url(site["base_url"], entry["title"])
    if not opened_url:
        return False
    music_url = app_settings.get_manga_music_url()
    if music_url:
        webbrowser.open(music_url)
    webbrowser.open(opened_url)
    return True


def _open_anime_entry(parent, entry):
    url = entry.get("url")
    if url:
        if url.startswith("stremio://"):
            stremio.launch(url)
        else:
            webbrowser.open(url)
        return True
    site = anime_sites.get_site(entry["site_id"]) if entry.get("site_id") else None
    if site and site.get("search_url"):
        webbrowser.open(anime_sites.search_page_url(site["search_url"], entry["title"]))
        return True
    return False


def _migrate(entries, data_file):
    changed = False
    # Anime used to have one global Settings toggle ("Anime Opens In":
    # Stremio or Crunchyroll) instead of today's per-entry Video Website.
    # Entries saved under the old Crunchyroll choice have no url and no
    # site_id - point them at a Crunchyroll site (adding the default one
    # back if the user had since removed it) so they keep opening the
    # same place instead of silently going nowhere.
    old_provider = storage.load(app_settings.SETTINGS_FILE, {}).get("anime_provider")
    crunchyroll_site_id = None
    if old_provider == "crunchyroll":
        crunchyroll_site_id = next(
            (s["id"] for s in anime_sites.list_sites() if s["name"] == "Crunchyroll"), None)
        if crunchyroll_site_id is None:
            crunchyroll_site_id = anime_sites.add_site(
                "Crunchyroll", "https://www.crunchyroll.com/search?q=")["id"]

    for entry in entries:
        if (crunchyroll_site_id and entry.get("type") == "Anime"
                and not entry.get("url") and not entry.get("site_id")):
            entry["site_id"] = crunchyroll_site_id
            changed = True
        if "added_at" not in entry:
            entry["added_at"] = storage.now_iso()
            changed = True
        if "updated_at" not in entry:
            entry["updated_at"] = entry["added_at"]
            changed = True
        if "url" not in entry:
            entry["url"] = ""
            changed = True
        # Fix up links saved by earlier versions: missing 3rd slash
        # (Stremio parsed "detail" as an addon-manifest host instead of
        # a path), and a trailing videoId segment that made Stremio jump
        # into a specific episode instead of the show's overview page.
        if entry.get("url", "").startswith("stremio://detail/"):
            entry["url"] = entry["url"].replace("stremio://detail/", "stremio:///detail/", 1)
            changed = True
        if entry.get("url", "").startswith("stremio:///detail/"):
            parts = entry["url"].split("/")
            if len(parts) >= 2 and parts[-1] == parts[-2]:
                entry["url"] = "/".join(parts[:-1])
                changed = True
        # Backfill the id progress-syncing needs from a saved stremio://
        # link, for entries saved before it got its own field (see
        # _entry_imdb_id/EntryForm._save) - a Video-Website Anime entry
        # has no url to pull this from at all, so those need re-picking
        # the suggestion once to start syncing, but that's a one-time gap
        # for entries saved in the narrow window before this existed.
        if not entry.get("imdb_id"):
            found_id = _imdb_id_from_url(entry.get("url"))
            if found_id:
                entry["imdb_id"] = found_id
                changed = True
        # Older saves used one shared "Watching/Reading" status wording;
        # split it into the per-type wording (Watching vs Reading).
        legacy = _LEGACY_STATUS_MAP.get(entry.get("status"))
        if legacy:
            entry["status"] = legacy.get(entry.get("type"), entry["status"])
            changed = True
    if changed:
        storage.save(data_file, entries)
    return entries


class _ProgressSyncSignals(QObject):
    # entry id, progress season/episode, whether a real (not estimated)
    # progress result was found, total available season/episode, whether
    # this came from the bulk "refresh all" (vs one entry's right-click
    # Sync Progress) - silent ones skip the per-entry not-found popup.
    resolved = Signal(str, int, int, bool, int, int, bool)


class _CoverSignals(QObject):
    ready = Signal(str, str, str)  # entry id, new cover_url, local cache path


class _ScheduleSignals(QObject):
    # entry id, next_release dict (or None when nothing's scheduled), and
    # which refresh run asked for it (0 = none, see TrackerPage._refresh_run)
    resolved = Signal(str, object, int)


class TrackerPage(GlassPage):
    """Base for the Anime/Manga/Series pages. Subclasses set
    DATA_FILE/ENTRY_TYPES/TITLE/TYPE_OPTIONS/PROGRESS_COLUMNS.

    Entries show as a poster grid (click to open, right-click for Edit/
    Move/Delete) - the auto-tracked progress that used to be its own tree
    columns is now a hover tooltip on each poster instead."""

    DATA_FILE = "tracker.json"
    ENTRY_TYPES = ("Anime",)
    TITLE = "Anime"
    TYPE_OPTIONS = ["Anime", "Manga"]
    PROGRESS_COLUMNS = ["Last Season", "Last Episode"]
    # Manga has no Stremio/AniList presence to sync progress against.
    SUPPORTS_PROGRESS_SYNC = True
    # Which release-schedule source the hover tooltip's "next episode/
    # chapter" lines come from for this page - see helpers/release_schedule.
    MEDIUM = release_schedule.MEDIUM_ANIME

    def __init__(self, app):
        super().__init__(parent=None)
        self.app = app

        self._sync_signals = _ProgressSyncSignals()
        self._sync_signals.resolved.connect(self._on_progress_synced)
        self._sync_pending = 0
        self._sync_changed = False
        self._sync_batch_run = 0

        # The refresh button's run, if one is in progress: the "Updating..."
        # toast waiting to be told how it went, how many background lookups
        # are still out, and whether any of them found anything new. Only
        # ever touched on the UI thread - every lookup reports back through
        # a signal - so no locking is needed.
        #
        # _refresh_run numbers the runs, and every lookup carries the
        # number of the run that asked for it. The same lookups also run on
        # their own at page load, and one of those landing mid-refresh
        # would otherwise be counted as one of the run's own results -
        # ending it early, on a verdict that came from a different lookup
        # than the one still out. Carrying the number keeps that exact.
        self._refresh_run = 0
        self._refresh_toast = None
        self._refresh_pending = 0
        self._refresh_changed = False
        self._refresh_before = {}

        self._cover_signals = _CoverSignals()
        self._cover_signals.ready.connect(self._on_sharper_cover_ready)

        self._schedule_signals = _ScheduleSignals()
        self._schedule_signals.resolved.connect(self._on_schedule_resolved)

        self.entries = _migrate(storage.load(self.DATA_FILE, []), self.DATA_FILE)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 16, 18, 16)

        panel = QFrame(objectName="Panel")
        outer.addWidget(panel)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(24, 20, 24, 24)
        layout.setSpacing(14)

        header = QHBoxLayout()
        header.addWidget(QLabel(self.TITLE, objectName="PanelTitle"))
        header.addStretch()
        refresh_btn = QPushButton("⟳", objectName="AccentIcon")
        refresh_btn.setFixedSize(40, 40)
        refresh_btn.setToolTip("Refresh progress from Stremio/AniList and re-check release schedules"
                               if self.SUPPORTS_PROGRESS_SYNC else "Re-check release schedules")
        refresh_btn.clicked.connect(self._refresh_everything)
        header.addWidget(refresh_btn)
        add_btn = QPushButton("+", objectName="AccentIcon")
        add_btn.setFixedSize(40, 40)
        add_btn.setToolTip("Add Entry")
        add_btn.clicked.connect(self._open_form)
        header.addWidget(add_btn)
        layout.addLayout(header)

        top_row = QHBoxLayout()
        top_row.addWidget(QLabel("Sort:"))
        self.sort_box = QComboBox()
        self.sort_box.addItems(SORT_OPTIONS)
        self.sort_box.currentTextChanged.connect(self._refresh_grid)
        top_row.addWidget(self.sort_box)
        top_row.addStretch()
        layout.addLayout(top_row)

        self.grid_body = QWidget()
        self.grid_layout = QGridLayout(self.grid_body)
        self.grid_layout.setSpacing(14)
        self.grid_layout.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(scroll_area(self.grid_body), stretch=1)

        self._refresh_grid()
        if self.SUPPORTS_PROGRESS_SYNC:
            self._backfill_missing_latest_available()
        self._backfill_sharper_covers()
        self._refresh_schedules()

    # ------------------------------------------------------------------
    def _refresh_everything(self):
        """Header refresh button: re-check what's out there for every
        entry on this page - both your own watched/read progress (where
        the page has a source for it) and when the next episode/chapter
        is due.

        Says so while it works, and says how it went when it's finished
        (see _refresh_step_done) - every lookup runs on its own background
        thread, so without that the button reads as doing nothing at all
        for however long the slowest source takes to answer."""
        if self._refresh_toast is not None:
            return  # already running - let it finish rather than double it
        self._refresh_run += 1
        self._refresh_changed = False
        # What every entry says right now, to judge the results against.
        # Against this rather than against whatever is stored by the time
        # a result lands: the same lookups fire on their own when a page
        # opens, so one of those finishing first would have already
        # absorbed the news, and the run would then compare the answer
        # with itself and report nothing new.
        self._refresh_before = {
            entry["id"]: (_release_content(entry.get("next_release")),
                          entry.get("progress"), entry.get("latest_available"))
            for entry in self.entries if entry["type"] in self.ENTRY_TYPES
        }
        # Starts at one and is released at the end: the lookups below
        # report back through the event loop, so they cannot land before
        # this method returns - but a page with nothing to look up would
        # otherwise sit at zero pending and never report at all.
        self._refresh_pending = 1
        self._refresh_toast = show_toast(self, "Updating...", duration_ms=None)

        self._refresh_schedules(force=True)
        if self.SUPPORTS_PROGRESS_SYNC:
            self._sync_all_progress()
        self._refresh_step_done(self._refresh_run)

    def _refresh_step_started(self):
        """Count one more background lookup into the running refresh and
        return which run it belongs to, for it to report back with. 0 when
        no refresh is running - the same lookups also happen on page load,
        where there is nothing to report to."""
        if self._refresh_toast is None:
            return 0
        self._refresh_pending += 1
        return self._refresh_run

    def _refresh_step_done(self, run, found_something_new=False):
        """One lookup back. The last one to return reports the verdict.

        Anything from another run (or from no run at all) is ignored
        rather than counted: it was never counted in, so counting it out
        would end this run one result early."""
        if not run or run != self._refresh_run or self._refresh_toast is None:
            return
        self._refresh_changed = self._refresh_changed or found_something_new
        self._refresh_pending -= 1
        if self._refresh_pending > 0:
            return
        toast, self._refresh_toast = self._refresh_toast, None
        self._refresh_before = {}
        finish_toast(toast, self, "Updated Successfully" if self._refresh_changed
                     else "There is No New Update")

    def _refresh_schedules(self, force=False):
        """Look up when each entry's next episode/chapter lands, in the
        background. Cached on the entry (see release_schedule.needs_
        refresh), so a normal page visit usually fires no requests at all
        - only entries with nothing stored, a stale check, or a release
        time that's already passed get looked up again."""
        for entry in self.entries:
            if entry["type"] not in self.ENTRY_TYPES:
                continue
            if not release_schedule.needs_refresh(entry, force):
                continue
            run = self._refresh_step_started()
            threading.Thread(target=self._fetch_schedule, args=(entry, run), daemon=True).start()

    def _fetch_schedule(self, entry, run=0):
        # Must never raise: an uncaught exception here would kill the
        # background thread silently.
        try:
            found = release_schedule.fetch(
                self.MEDIUM, entry["title"], imdb_id=_entry_imdb_id(entry),
                known_latest_chapter=_latest_known_chapter(entry))
        except Exception:
            found = None
        self._schedule_signals.resolved.emit(entry["id"], found, run)

    def _on_schedule_resolved(self, entry_id, found, run=0):
        entry = next((e for e in self.entries if e["id"] == entry_id), None)
        if not entry:
            self._refresh_step_done(run)
            return
        snapshot = self._refresh_before.get(entry_id)
        before = snapshot[0] if snapshot else _release_content(entry.get("next_release"))
        self._refresh_step_done(run, _release_content(found) != before)
        # Stored even when nothing was found, so a title with no schedule
        # (finished airing, not on the source) isn't re-looked-up on every
        # single page visit - the timestamp is what needs_refresh rate-
        # limits against.
        fields = {"next_release": found, "next_release_checked_at": storage.now_iso()}
        entry.update(fields)
        # Just these fields, not a wholesale save of self.entries - Anime
        # and Manga are separate pages backed by the same tracker.json
        # and each holds its own copy of it (see storage.update_entry).
        storage.update_entry(self.DATA_FILE, entry["id"], fields)
        # No grid rebuild: the tooltip is generated on hover from the
        # entry itself (see _build_card), so it picks this up as-is.

    # ------------------------------------------------------------------
    def _backfill_sharper_covers(self):
        """Manga covers saved before manga_sites.py started stripping
        WordPress's size-suffixed crops / TeamX's thumbnail_ prefix off
        search-result covers (see manga_sites.upgrade_cover_url) are
        stuck on those blurry small images forever otherwise - nothing
        else ever re-derives cover_path from cover_url after the initial
        save. Silently re-download the sharper original in the
        background, same one-time-catch-up pattern as
        _backfill_missing_latest_available."""
        for entry in self.entries:
            if entry["type"] not in MANGA_TYPES:
                continue
            upgraded = manga_sites.upgrade_cover_url(entry.get("cover_url"))
            if upgraded and upgraded != entry.get("cover_url"):
                threading.Thread(target=self._fetch_sharper_cover,
                                  args=(entry["id"], upgraded), daemon=True).start()

    def _fetch_sharper_cover(self, entry_id, new_url):
        # Must never raise: an uncaught exception here would kill the
        # background thread silently.
        try:
            path = images.download(new_url)
        except Exception:
            path = None
        if path:
            self._cover_signals.ready.emit(entry_id, new_url, str(path))

    def _on_sharper_cover_ready(self, entry_id, new_url, path):
        entry = next((e for e in self.entries if e["id"] == entry_id), None)
        if not entry:
            return
        entry["cover_url"] = new_url
        entry["cover_path"] = path
        storage.save(self.DATA_FILE, self.entries)
        self._refresh_grid()

    def _backfill_missing_latest_available(self):
        """Entries saved before latest_available was tracked (or from a
        pick where the background lookup didn't finish/land) leave the
        Last Season/Last Episode tooltip permanently blank otherwise -
        nothing else ever re-fetches it later. Silently top those up in
        the background on load, the same way Sync Progress would for one
        entry by hand, instead of leaving the user to notice and fix it."""
        targets = [e for e in self.entries
                   if e["type"] in self.ENTRY_TYPES and not e.get("latest_available") and _entry_imdb_id(e)]
        if not targets:
            return
        self._sync_pending += len(targets)
        for entry in targets:
            threading.Thread(
                target=self._fetch_real_progress,
                args=(entry["id"], _entry_imdb_id(entry), entry["title"], entry.get("type") == "Anime", True),
                daemon=True).start()

    # ------------------------------------------------------------------
    def _visible_entries(self):
        entries = [e for e in self.entries if e["type"] in self.ENTRY_TYPES]
        mode = self.sort_box.currentText()
        if mode == "Name (A-Z)":
            entries = sorted(entries, key=lambda e: e["title"].lower())
        elif mode == "Date Added (Newest)":
            entries = sorted(entries, key=lambda e: e.get("added_at") or "", reverse=True)
        elif mode == "Last Updated":
            entries = sorted(entries, key=lambda e: e.get("updated_at") or "", reverse=True)
        return entries

    def _progress_columns(self, entry):
        """Values matching self.PROGRESS_COLUMNS for the hover tooltip.
        Manga has no "total available" concept worth fetching, so it
        still shows its own read progress. Anime/Series show the latest
        season/episode available out there (how far the release currently
        goes) rather than what you've watched - that's the always-visible
        label under the cover already, see _progress_display."""
        if entry["type"] in MANGA_TYPES:
            text = entry.get("progress") or ""
            value = parse_chapter_progress(text)
            return [format_chapter_progress(value) if value else text]
        text = entry.get("latest_available") or ""
        season, episode = parse_episode_progress(text)
        if not season and not episode and text:
            return ["", text]  # legacy freeform text that doesn't parse - keep it visible
        return [str(season) if season else "", str(episode) if episode else ""]

    def _tooltip_html(self, entry):
        """Built fresh on every hover (see _build_card) rather than once
        per card, because the countdown at the end of it is only right at
        the moment it's shown."""
        rows = [f"<b>{entry['title']}</b>", entry["status"]]
        for label, value in zip(self.PROGRESS_COLUMNS, self._progress_columns(entry)):
            if value:
                rows.append(f"{label}: {value}")
        rows.extend(release_schedule.tooltip_lines(entry, self.MEDIUM))
        return "<br>".join(rows)

    # ------------------------------------------------------------------
    def _refresh_grid(self, *_args):
        while self.grid_layout.count():
            item = self.grid_layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()

        entries = self._visible_entries()
        if not entries:
            empty = QLabel(f"No {self.TITLE.lower()} yet - click '+' to create one.", objectName="Muted")
            self.grid_layout.addWidget(empty, 0, 0)
            return

        # Grouped into sections by status (Watching/Reading first, same
        # order as STATUSES_BY_TYPE) instead of one flat mixed grid - the
        # sort dropdown still controls ordering *within* each section.
        known_statuses = STATUSES_BY_TYPE.get(self.ENTRY_TYPES[0], STATUSES_BY_TYPE["Anime"])
        grouped = {status: [] for status in known_statuses}
        for entry in entries:
            grouped.setdefault(entry.get("status") or known_statuses[0], []).append(entry)
        extra_statuses = [s for s in grouped if s not in known_statuses]

        row = 0
        for status in [*known_statuses, *extra_statuses]:
            group = grouped.get(status) or []
            if not group:
                continue
            header = QLabel(f"{status} ({len(group)})", objectName="SectionTitle")
            self.grid_layout.addWidget(header, row, 0, 1, GRID_COLS)
            row += 1
            for index, entry in enumerate(group):
                card = self._build_card(entry)
                self.grid_layout.addWidget(card, row + index // GRID_COLS, index % GRID_COLS)
            row += (len(group) + GRID_COLS - 1) // GRID_COLS

    def _build_card(self, entry):
        card = Card(hoverable=True)
        card.setFixedWidth(POSTER_SIZE[0] + 20)
        card.set_tooltip_provider(lambda en=entry: self._tooltip_html(en))
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(8, 10, 8, 10)
        card_layout.setSpacing(6)

        cover = QLabel()
        cover.setFixedSize(*POSTER_SIZE)
        cover.setPixmap(images.thumbnail_or_avatar(entry.get("cover_path"), entry["title"], POSTER_SIZE))
        card_layout.addWidget(cover, alignment=Qt.AlignmentFlag.AlignHCenter)

        title = QLabel(entry["title"], objectName="CardTitle")
        title.setWordWrap(True)
        title.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        card_layout.addWidget(title)

        status = QLabel(entry["status"], objectName="CardMeta")
        status.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        card_layout.addWidget(status)

        # Always visible, not just on hover (the tooltip alone was too easy
        # to miss - this is the answer to "where's the progress").
        progress_text = self._progress_display(entry)
        if progress_text:
            progress_label = QLabel(progress_text)
            progress_label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
            progress_label.setStyleSheet(
                f"color: {theme.ACCENT}; font-weight: 700; font-size: 9pt; background: transparent;")
            card_layout.addWidget(progress_label)

        if entry["type"] in MANGA_TYPES:
            controls = QHBoxLayout()
            controls.setContentsMargins(0, 0, 0, 0)
            minus_btn = QPushButton("-", objectName="Icon")
            minus_btn.setFixedSize(46, 46)
            minus_btn.setToolTip("Last Watched Chapter -1")
            minus_btn.clicked.connect(lambda checked=False, en=entry: self._bump_watched_chapter(en, -1))
            controls.addWidget(minus_btn)
            controls.addStretch()
            plus_btn = QPushButton("+", objectName="Icon")
            plus_btn.setFixedSize(46, 46)
            plus_btn.setToolTip("Last Watched Chapter +1")
            plus_btn.clicked.connect(lambda checked=False, en=entry: self._bump_watched_chapter(en, 1))
            controls.addWidget(plus_btn)
            card_layout.addLayout(controls)

        card.clicked.connect(lambda en=entry: self._open_entry(en))
        card.rightClicked.connect(lambda event, en=entry: self._show_context_menu(event, en))
        return card

    def _bump_watched_chapter(self, entry, delta):
        current = entry.get("last_watched_chapter") or 0.0
        entry["last_watched_chapter"] = max(0.0, current + delta)
        entry["updated_at"] = storage.now_iso()
        storage.save(self.DATA_FILE, self.entries)
        self._refresh_grid()

    def _progress_display(self, entry):
        if entry["type"] in MANGA_TYPES:
            watched = entry.get("last_watched_chapter")
            return f"Ch {format_chapter_progress(watched)}" if watched else ""
        # Anime/Series progress is only ever auto-filled with a *guess*
        # (the latest episode currently out, not necessarily what you've
        # watched) unless it came from a connected account or you typed it
        # in yourself - only show it here once it's actually confirmed as
        # real, so the card never states a guess as fact.
        if not entry.get("progress_verified"):
            return ""
        return entry.get("progress") or ""

    def _open_entry(self, entry):
        if not open_tracker_entry(self, entry):
            self._open_form(edit=True, entry=entry)

    def _show_context_menu(self, event, entry):
        menu = QMenu(self)
        menu.addAction("Edit", lambda: self._open_form(edit=True, entry=entry))
        if entry.get("type") in ("Anime", "Series") and _entry_imdb_id(entry):
            menu.addAction("Sync Progress", lambda: self._sync_progress(entry))
        if self.sort_box.currentText() == "Custom Order":
            menu.addAction("Move Up", lambda: self._move_entry(entry, -1))
            menu.addAction("Move Down", lambda: self._move_entry(entry, 1))
        menu.addAction("Delete", lambda: self._delete_entry(entry))
        menu.exec(event.globalPosition().toPoint())

    def _sync_progress(self, entry):
        """Re-fetch this one entry's *real* progress (your connected
        Stremio account, or - for Anime - your AniList username) and
        overwrite the stored value with it. Unlike the initial add-time
        guess, this can be re-run any time, so a saved entry never has to
        stay stuck on a fallback estimate."""
        imdb_id = _entry_imdb_id(entry)
        if not imdb_id:
            return
        threading.Thread(
            target=self._fetch_real_progress,
            args=(entry["id"], imdb_id, entry["title"], entry.get("type") == "Anime", False),
            daemon=True).start()

    def _sync_all_progress(self):
        """Header refresh button: re-sync every linked entry on this page
        at once, instead of right-clicking each card individually."""
        targets = [e for e in self.entries if e["type"] in self.ENTRY_TYPES and _entry_imdb_id(e)]
        if not targets:
            # Only worth saying on its own; during a refresh the verdict
            # toast covers it, and two toasts share the same corner.
            if self._refresh_toast is None:
                show_toast(self, "Nothing to sync yet - no linked entries")
            return
        # The whole batch counts as one step of the refresh, closed out
        # when the last of them comes back (see _on_progress_synced) -
        # they already have _sync_pending counting them individually.
        self._sync_batch_run = self._refresh_step_started()
        self._sync_pending += len(targets)
        for entry in targets:
            threading.Thread(
                target=self._fetch_real_progress,
                args=(entry["id"], _entry_imdb_id(entry), entry["title"],
                      entry.get("type") == "Anime", True),
                daemon=True).start()

    def _fetch_real_progress(self, entry_id, imdb_id, title, is_anime, silent):
        result = None
        _, auth_key = app_settings.get_stremio_auth()
        if auth_key:
            try:
                result = stremio.fetch_watch_progress(imdb_id, auth_key)
            except Exception:
                result = None
        if not result and is_anime:
            anilist_username = app_settings.get_anilist_username()
            if anilist_username:
                try:
                    result = anilist.fetch_watch_progress(title, anilist_username)
                except Exception:
                    result = None
        try:
            total = stremio.fetch_latest_episode(imdb_id, "series")
        except Exception:
            total = None
        season, episode = result or (0, 0)
        total_season, total_episode = total or (0, 0)
        self._sync_signals.resolved.emit(
            entry_id, season, episode, result is not None, total_season, total_episode, silent)

    def _on_progress_synced(self, entry_id, season, episode, found, total_season, total_episode, silent):
        entry = next((e for e in self.entries if e["id"] == entry_id), None)
        if entry:
            snapshot = self._refresh_before.get(entry_id)
            before = (snapshot[1:] if snapshot
                      else (entry.get("progress"), entry.get("latest_available")))
            if found:
                entry["progress"] = format_episode_progress(season, episode)
                entry["progress_verified"] = True
                entry["updated_at"] = storage.now_iso()
            if total_season or total_episode:
                entry["latest_available"] = format_episode_progress(total_season, total_episode)
            # Only the bulk path feeds the refresh button's verdict - a
            # single entry synced by hand from its right-click menu isn't
            # part of anything being reported on.
            if silent and (entry.get("progress"), entry.get("latest_available")) != before:
                self._sync_changed = True

        if not silent:
            if not found:
                QMessageBox.information(
                    self, "Sync Progress",
                    "No real progress found for this title. Either it's not in "
                    "your Stremio library (or, for Anime, your AniList list), or "
                    "Stremio has no specific episode recorded for it - that only "
                    "happens once you've actually pressed play on one through "
                    "Stremio itself, not just added it to your library. You can "
                    "still set your progress by hand in Edit.")
                return
            storage.save(self.DATA_FILE, self.entries)
            self._refresh_grid()
            return

        # Bulk sync: results trickle in from several background threads, so
        # only save/redraw once they've all come back. Whether any of them
        # actually moved anything is what the refresh button reports; a
        # title Stremio has no per-episode history for (see the
        # single-entry message above) is not a failure, just nothing new.
        self._sync_pending -= 1
        if self._sync_pending <= 0:
            storage.save(self.DATA_FILE, self.entries)
            self._refresh_grid()
            changed, self._sync_changed = self._sync_changed, False
            run, self._sync_batch_run = self._sync_batch_run, 0
            self._refresh_step_done(run, changed)

    def _move_entry(self, entry, delta):
        idx = self.entries.index(entry)
        new_idx = idx + delta
        if 0 <= new_idx < len(self.entries):
            self.entries[idx], self.entries[new_idx] = self.entries[new_idx], self.entries[idx]
            storage.save(self.DATA_FILE, self.entries)
            self._refresh_grid()

    def _delete_entry(self, entry):
        if QMessageBox.question(self, "Delete Entry", f"Delete '{entry['title']}'?") == QMessageBox.StandardButton.Yes:
            self.entries.remove(entry)
            storage.save(self.DATA_FILE, self.entries)
            self._refresh_grid()

    # ------------------------------------------------------------------
    def _open_form(self, edit=False, entry=None):
        if edit and entry is None:
            return
        EntryForm(self, entry if edit else None, default_type=self.ENTRY_TYPES[0], type_options=self.TYPE_OPTIONS,
                  on_save=self._on_form_save)

    def _on_form_save(self, entry, is_new):
        entry["updated_at"] = storage.now_iso()
        if is_new:
            entry["added_at"] = entry["updated_at"]
            self.entries.append(entry)
        # A new entry has no schedule yet, and an edited one may have had
        # the title it's looked up by changed out from under the old one -
        # either way the stored lookup is worth redoing, so clear what
        # rate-limits it before saving.
        entry.pop("next_release_checked_at", None)
        storage.save(self.DATA_FILE, self.entries)
        self._refresh_grid()
        self._refresh_schedules()


class AnimePage(TrackerPage):
    DATA_FILE = "tracker.json"
    ENTRY_TYPES = ("Anime",)
    TITLE = "Anime"
    TYPE_OPTIONS = ["Anime"]
    MEDIUM = release_schedule.MEDIUM_ANIME


class MangaPage(TrackerPage):
    DATA_FILE = "tracker.json"
    ENTRY_TYPES = MANGA_TYPES
    TITLE = "Reading"
    TYPE_OPTIONS = list(MANGA_TYPES)
    PROGRESS_COLUMNS = ["Last Chapter"]
    SUPPORTS_PROGRESS_SYNC = False
    MEDIUM = release_schedule.MEDIUM_MANGA


class SeriesPage(TrackerPage):
    DATA_FILE = "series.json"
    ENTRY_TYPES = ("Series",)
    TITLE = "Series"
    TYPE_OPTIONS = ["Series"]
    MEDIUM = release_schedule.MEDIUM_SERIES


class _SearchSignals(QObject):
    results = Signal(str, list, int)  # provider ("stremio"/"manga_sites"), results, search sequence #
    cover_ready = Signal(str, object)
    manga_details_resolved = Signal(str, str, float)  # page url, cover url, latest chapter
    latest_episode_resolved = Signal(str, int, int)  # identity (stremio url), latest available season/episode


class EntryForm(QDialog):
    def __init__(self, parent, entry, default_type, type_options, on_save):
        super().__init__(parent)
        self.on_save = on_save
        self.entry = entry
        self.is_new = entry is None
        self.type_options = type_options

        self.selected_cover_url = entry.get("cover_url") if entry else None
        self.selected_cover_path = entry.get("cover_path") if entry else None
        # The Cinemeta id to sync progress against later, independent of
        # url (which an Anime entry set to a Video Website other than the
        # built-in "Stremio" one deliberately leaves blank) - see
        # _entry_imdb_id/_save.
        self.selected_imdb_id = entry.get("imdb_id") if entry else None
        self._search_results = {}
        self._search_seq = 0
        self._status_parts = {}  # "cover"/"progress" -> current message, composed onto status_label
        self._pending_episode_identity = None  # tracks staleness for the async episode-progress lookup
        self._latest_available = entry.get("latest_available", "") if entry else ""
        # Whether the season/episode spinners hold *confirmed* progress
        # (fetched from a connected Stremio/AniList account, or typed in
        # by hand) rather than just the unconfirmed "latest episode out"
        # guess - only verified progress shows on the card. Manual edits
        # to the spinners flip this true (see the valueChanged connect
        # below, wired up only after the initial values are set so
        # loading an existing entry doesn't misread as a fresh edit).
        self._progress_verified = bool(entry and entry.get("progress_verified"))
        # Whether a suggestion has been applied yet (by click or by exact-
        # title auto-match - see _apply_search_results). Starts True for
        # an existing entry that's already resolved, so re-opening it to
        # edit unrelated fields doesn't get its cover/link silently
        # swapped out by a coincidental search hit.
        self._suggestion_applied = bool(entry and (entry.get("cover_url") or entry.get("url")))
        self._signals = _SearchSignals()
        self._signals.results.connect(self._apply_search_results)
        self._signals.cover_ready.connect(self._on_cover_downloaded)
        self._signals.manga_details_resolved.connect(self._on_manga_details_resolved)
        self._signals.latest_episode_resolved.connect(self._on_latest_episode_resolved)

        self.setWindowTitle("Edit Entry" if entry else "Add Entry")
        self.setFixedSize(460, 620)
        theme.apply_dark_titlebar(self)

        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.timeout.connect(self._trigger_search)

        form = QVBoxLayout(self)
        form.setContentsMargins(24, 20, 24, 16)
        form.setSpacing(4)

        top = QHBoxLayout()
        fields = QVBoxLayout()
        fields.setSpacing(4)

        self.title_label = QLabel()
        fields.addWidget(self.title_label)
        self.title_combo = QComboBox()
        self.title_combo.setEditable(True)
        self.title_combo.setCurrentText(entry["title"] if entry else "")
        self.title_combo.lineEdit().textEdited.connect(self._on_title_edited)
        self.title_combo.textActivated.connect(self._on_suggestion_selected)
        fields.addWidget(self.title_combo)

        # Type dropdown only makes sense when a page's data file mixes
        # more than one type (Anime/Manga share tracker.json); Series has
        # nothing to switch to, so it's set silently instead.
        self.type_box = QComboBox()
        self.type_box.addItems(type_options)
        self.type_box.setCurrentText(entry["type"] if entry else default_type)
        self.type_box.currentTextChanged.connect(self._on_type_changed)
        if len(type_options) > 1:
            fields.addSpacing(8)
            fields.addWidget(QLabel("Type"))
            fields.addWidget(self.type_box)

        top.addLayout(fields, stretch=1)

        preview_col = QVBoxLayout()
        self.preview_label = QLabel()
        self.preview_label.setFixedSize(*PREVIEW_SIZE)
        self.preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        preview_col.addWidget(self.preview_label)
        preview_col.addStretch()
        top.addLayout(preview_col)
        self._refresh_preview()

        form.addLayout(top)

        form.addSpacing(8)
        form.addWidget(QLabel("Status"))
        self.status_box = QComboBox()
        self._populate_status_options(entry["status"] if entry else None)
        form.addWidget(self.status_box)

        # The saved value stays a plain string (it's what the tree/home
        # dashboard display directly) - these spinners just give a
        # faster, typo-proof way to set it than freeform text, and a
        # one-click +1 for "I just finished this episode/chapter".
        self._original_progress = entry.get("progress", "") if entry else ""

        form.addSpacing(8)
        self.chapter_row = QWidget()
        chapter_layout = QHBoxLayout(self.chapter_row)
        chapter_layout.setContentsMargins(0, 0, 0, 0)
        chapter_layout.setSpacing(14)

        available_col = QVBoxLayout()
        available_col.setSpacing(2)
        available_col.addWidget(QLabel("Last Chapter"))
        self.chapter_spin = QDoubleSpinBox()
        self.chapter_spin.setRange(0, 99999)
        self.chapter_spin.setDecimals(1)
        self.chapter_spin.setValue(parse_chapter_progress(self._original_progress))
        available_col.addWidget(self.chapter_spin)
        chapter_layout.addLayout(available_col)

        # Unlike Last Chapter above (auto-filled from the reading site's
        # latest release), this is never auto-filled - only you know what
        # you've actually read, same as Anime/Series' real Stremio/AniList
        # progress. Shown on the card and adjustable there via +/-.
        watched_col = QVBoxLayout()
        watched_col.setSpacing(2)
        watched_col.addWidget(QLabel("Last Watched Chapter"))
        self.watched_chapter_spin = QDoubleSpinBox()
        self.watched_chapter_spin.setRange(0, 99999)
        self.watched_chapter_spin.setDecimals(1)
        self.watched_chapter_spin.setValue((entry or {}).get("last_watched_chapter") or 0.0)
        watched_col.addWidget(self.watched_chapter_spin)
        chapter_layout.addLayout(watched_col)

        form.addWidget(self.chapter_row)

        self.episode_row = QWidget()
        episode_layout = QHBoxLayout(self.episode_row)
        episode_layout.setContentsMargins(0, 0, 0, 0)
        episode_layout.setSpacing(14)
        season0, episode0 = parse_episode_progress(self._original_progress)
        season_col = QVBoxLayout()
        season_col.setSpacing(2)
        season_col.addWidget(QLabel("Last Season"))
        self.season_spin = QSpinBox()
        self.season_spin.setRange(0, 99)
        self.season_spin.setSpecialValueText("—")
        self.season_spin.setValue(season0)
        season_col.addWidget(self.season_spin)
        episode_layout.addLayout(season_col)
        episode_col = QVBoxLayout()
        episode_col.setSpacing(2)
        episode_col.addWidget(QLabel("Last Episode"))
        self.episode_spin = QSpinBox()
        self.episode_spin.setRange(0, 9999)
        self.episode_spin.setValue(episode0)
        episode_col.addWidget(self.episode_spin)
        episode_layout.addLayout(episode_col)
        episode_layout.addStretch()
        form.addWidget(self.episode_row)

        # Wired up only after both initial values are set above, so
        # loading an existing entry's saved progress isn't mistaken for a
        # fresh manual edit - only edits from here on mark it verified.
        self.season_spin.valueChanged.connect(self._on_progress_hand_edited)
        self.episode_spin.valueChanged.connect(self._on_progress_hand_edited)

        # Series only: the Stremio deep link, freely editable. Anime/Manga
        # have no equivalent field - their open target is fully derived
        # from the site dropdown below, never manually typed.
        self.url_row = QWidget()
        url_layout = QVBoxLayout(self.url_row)
        url_layout.setContentsMargins(0, 8, 0, 0)
        url_layout.setSpacing(4)
        self.url_label = QLabel()
        url_layout.addWidget(self.url_label)
        self.url_edit = QLineEdit(entry.get("url", "") if entry else "")
        url_layout.addWidget(self.url_edit)
        form.addWidget(self.url_row)

        # Manga/Anime only: which site (from Settings) this entry opens
        # to - "Reading Website" for Manga, "Video Website" for Anime
        # (where the built-in "Stremio" option means "use the deep link"
        # instead of a configured site). Picking a search suggestion below
        # sets both this and the matched link (kept on url_edit
        # internally, just not shown).
        self.site_row = QWidget()
        site_layout = QVBoxLayout(self.site_row)
        site_layout.setContentsMargins(0, 8, 0, 0)
        site_layout.setSpacing(4)
        self.site_label = QLabel()
        site_layout.addWidget(self.site_label)
        self.site_box = QComboBox()
        self._populate_site_options(entry.get("site_id") if entry else None)
        site_layout.addWidget(self.site_box)
        form.addWidget(self.site_row)

        self._update_url_and_site_visibility()
        self._update_progress_visibility()

        self.status_label = QLabel("", objectName="Muted")
        form.addWidget(self.status_label)

        form.addStretch()
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)
        save_btn = QPushButton("Save", objectName="Accent")
        save_btn.setDefault(True)
        save_btn.clicked.connect(self._save)
        btn_row.addWidget(save_btn)
        form.addLayout(btn_row)

        self._update_labels()
        self.exec()

    # ------------------------------------------------------------------
    def _set_status_part(self, key, text):
        """The cover download and the progress auto-fill happen in
        parallel background threads and each has something to report -
        compose their messages instead of one blindly overwriting the
        other's (e.g. "Loading cover..." stomping the "not your progress"
        hint, or vice versa)."""
        self._status_parts[key] = text
        self.status_label.setText(" · ".join(p for p in self._status_parts.values() if p))

    def _provider(self):
        return SEARCH_PROVIDER_BY_TYPE.get(self.type_box.currentText(), "manga_sites")

    def _update_labels(self):
        if self._provider() == "stremio":
            self.title_label.setText("Title (type to search Stremio)")
            self.url_label.setText("Stremio link (opens Stremio on double-click)")
        else:
            self.title_label.setText("Title (type to search your reading websites)")
        self.site_label.setText(
            "Video Website (opens directly on double-click)" if self.type_box.currentText() == "Anime"
            else "Reading Website (opens directly on double-click)")

    def _status_options(self):
        return STATUSES_BY_TYPE.get(self.type_box.currentText(), STATUSES_BY_TYPE["Anime"])

    def _populate_status_options(self, current_status=None):
        options = self._status_options()
        self.status_box.blockSignals(True)
        self.status_box.clear()
        self.status_box.addItems(options)
        self.status_box.setCurrentText(current_status if current_status in options else options[0])
        self.status_box.blockSignals(False)

    def _on_type_changed(self, _text):
        # Deliberately doesn't touch selected_cover_url/path - switching
        # e.g. Manga -> Manhwa (or even Anime -> Manga) shouldn't throw
        # away a cover the user already matched. Only a title edit or a
        # fresh suggestion pick invalidates the current cover.
        self._update_labels()
        self._populate_status_options(self.status_box.currentText())
        self._populate_site_options(self.site_box.currentData())
        self._update_url_and_site_visibility()
        self._update_progress_visibility()
        self._trigger_search()

    def _update_url_and_site_visibility(self):
        current_type = self.type_box.currentText()
        has_site = current_type in MANGA_TYPES or current_type == "Anime"
        self.url_row.setVisible(not has_site)
        self.site_row.setVisible(has_site)

    def _update_progress_visibility(self):
        is_manga = self.type_box.currentText() in MANGA_TYPES
        self.chapter_row.setVisible(is_manga)
        self.episode_row.setVisible(not is_manga)

    def _refresh_preview(self):
        title = self.title_combo.currentText() or "?"
        self.preview_label.setPixmap(images.thumbnail_or_avatar(self.selected_cover_path, title, PREVIEW_SIZE))

    def _populate_site_options(self, current_site_id=None):
        self.site_box.blockSignals(True)
        self.site_box.clear()
        if self.type_box.currentText() == "Anime":
            self.site_box.addItem("Stremio", None)
            sites = anime_sites.list_sites()
        else:
            self.site_box.addItem("— None —", None)
            sites = manga_sites.list_sites()
        for site in sites:
            self.site_box.addItem(site["name"], site["id"])
        if current_site_id:
            idx = self.site_box.findData(current_site_id)
            if idx >= 0:
                self.site_box.setCurrentIndex(idx)
        self.site_box.blockSignals(False)

    def _on_title_edited(self, _text):
        self.selected_cover_url = None
        self.selected_cover_path = None
        self.selected_imdb_id = None
        self._suggestion_applied = False
        self._search_timer.start(SEARCH_DEBOUNCE_MS)

    def _trigger_search(self):
        text = self.title_combo.currentText().strip()
        if len(text) < 2:
            return
        self._search_seq += 1
        seq = self._search_seq
        provider = self._provider()
        self.status_label.setText("Searching...")
        threading.Thread(target=self._search_worker, args=(provider, text, seq), daemon=True).start()

    def _search_worker(self, provider, text, seq):
        if provider == "stremio":
            catalog = STREMIO_CATALOG_BY_TYPE.get(self.type_box.currentText(), "series")
            results = stremio.search(text, catalog)
        else:
            results = manga_sites.search_all(text)
        self._signals.results.emit(provider, results, seq)

    def _video_site_options(self):
        """(site_id, site_name) pairs matching the Video Website dropdown -
        the built-in Stremio option (None) first, then every configured
        anime site, same order as _populate_site_options."""
        return [(None, "Stremio")] + [(s["id"], s["name"]) for s in anime_sites.list_sites()]

    def _expand_for_video_sites(self, results):
        """One Stremio match can open on any configured Video Website -
        fan each raw result out into one suggestion per site (mirroring
        manga's one-result-per-site search), tagged with which site it
        represents so picking it can set the dropdown below to match."""
        expanded = []
        for r in results:
            for site_id, site_name in self._video_site_options():
                expanded.append({**r, "_video_site_id": site_id, "_video_site_name": site_name})
        return expanded

    def _label_for_result(self, provider, r):
        if provider == "manga_sites":
            return f"{r['title']} ({r['site_name']})"
        if "_video_site_name" in r:
            return f"{r['title']} ({r['_video_site_name']})"
        suffix = f" ({r['format']})" if r.get("format") else ""
        return f"{r['title']}{suffix}"

    def _apply_search_results(self, provider, results, seq):
        if seq != self._search_seq:
            return

        if provider == "stremio" and self.type_box.currentText() == "Anime":
            results = self._expand_for_video_sites(results)

        source_name = "Stremio" if provider == "stremio" else "your manga websites"
        self._search_results = {}
        labels = []
        for r in results:
            label, n = self._label_for_result(provider, r), 2
            base_label = label
            while label in self._search_results:
                label = f"{base_label} #{n}"
                n += 1
            self._search_results[label] = {**r, "_provider": provider}
            labels.append(label)

        current_text = self.title_combo.currentText()
        self.title_combo.blockSignals(True)
        self.title_combo.clear()
        self.title_combo.addItems(labels)
        self.title_combo.setCurrentText(current_text)
        self.title_combo.blockSignals(False)

        if labels:
            self.status_label.setText(f"{len(labels)} match(es) from {source_name} - pick one below")
            self.title_combo.showPopup()
        else:
            self.status_label.setText(f"No matches from {source_name} - you can still save this title as-is")

        # If what's typed already exactly matches one of the results (not
        # a suffixed dupe label, the actual title), apply it automatically
        # instead of waiting for a click on the dropdown - otherwise
        # typing the exact right name and just hitting Save skips the
        # cover/link/progress lookups entirely, which they're not there
        # to fix by hand.
        if not self._suggestion_applied:
            exact_label = next((label for label, r in self._search_results.items()
                                  if r["title"].strip().lower() == current_text.strip().lower()), None)
            if exact_label:
                self._on_suggestion_selected(exact_label)

    def _on_suggestion_selected(self, label):
        self._suggestion_applied = True
        result = self._search_results.get(label)
        if not result:
            return
        self._status_parts = {}  # a fresh pick supersedes any prior cover/progress status
        self.title_combo.setCurrentText(result["title"])
        self.selected_cover_url = result.get("cover_url")
        self.selected_cover_path = None

        if result.get("_provider") == "manga_sites":
            self.url_edit.setText(result["url"])
            idx = self.site_box.findData(result["site_id"])
            if idx >= 0:
                self.site_box.setCurrentIndex(idx)
            if result.get("latest_chapter"):
                self.chapter_spin.setValue(result["latest_chapter"])
                self._set_status_part("progress", NOT_YOUR_PROGRESS_HINT)
            if not self.selected_cover_url or not result.get("latest_chapter"):
                # This engine's search results are missing the cover
                # and/or chapter count (e.g. Madara's AJAX search returns
                # titles/links only) - fetch them from the matched page/
                # detail endpoint instead of leaving them blank.
                threading.Thread(target=self._resolve_manga_details, args=(result["url"],), daemon=True).start()
        elif result.get("stremio_url"):
            # Saved regardless of which Video Website ends up selected
            # below, so progress-syncing keeps working even for entries
            # that save no url at all (see _entry_imdb_id).
            self.selected_imdb_id = result.get("id")
            # An Anime suggestion is tagged with the Video Website it
            # represents (see _expand_for_video_sites) - picking it moves
            # the dropdown below to match, so the two always agree instead
            # of the dropdown silently staying on whatever it last was.
            video_site_id = result.get("_video_site_id")
            if self.type_box.currentText() == "Anime" and "_video_site_id" in result:
                idx = self.site_box.findData(video_site_id)
                if idx >= 0:
                    self.site_box.setCurrentIndex(idx)
            # A site other than the built-in "Stremio" option doesn't get
            # a stremio:// link saved - open_tracker_entry falls back to
            # that site's search results for the title instead when
            # there's no saved url. Explicitly clearing it (not just
            # skipping the setText below) matters when switching from an
            # earlier Stremio pick on the same title - otherwise that
            # leftover deep link stays in the hidden field and wins over
            # the site on open.
            uses_site = self.type_box.currentText() == "Anime" and video_site_id is not None
            if uses_site:
                self.url_edit.setText("")
            else:
                self.url_edit.setText(result["stremio_url"])
            # Cinemeta's catalog search has no episode data at all (title/
            # poster/year only) - a background lookup fills in the Last
            # Season/Last Episode fields with the latest aired episode
            # (how far the release currently goes), *not* your own watch
            # progress - that's a separate concept, only ever set via
            # Sync Progress (a connected Stremio account, or for Anime
            # your AniList username) after the entry's saved, same as
            # Manga's "Last Chapter" (site's latest) never doubles as
            # "Last Watched Chapter" (your own progress) either. Tracked
            # via its own identity (not url_edit's text) since site-
            # provider Anime doesn't get a url saved at all.
            self._pending_episode_identity = result["stremio_url"]
            catalog = STREMIO_CATALOG_BY_TYPE.get(self.type_box.currentText(), "series")
            threading.Thread(target=self._resolve_episode_progress,
                              args=(result["id"], catalog, result["stremio_url"]),
                              daemon=True).start()

        self._refresh_preview()
        if self.selected_cover_url:
            self._set_status_part("cover", "Loading cover...")
            threading.Thread(target=self._download_cover, args=(self.selected_cover_url,), daemon=True).start()
        else:
            self._set_status_part("cover", "")

    def _download_cover(self, url):
        # Must never raise: an uncaught exception here would kill the
        # background thread silently and leave the UI stuck on
        # "Loading cover..." forever.
        try:
            path = images.download(url)
        except Exception:
            path = None
        self._signals.cover_ready.emit(url, path)

    def _on_cover_downloaded(self, url, path):
        if url != self.selected_cover_url:
            return
        self.selected_cover_path = str(path) if path else None
        self._refresh_preview()
        self._set_status_part("cover", "" if path else "Couldn't download the cover image")

    def _resolve_manga_details(self, page_url):
        details = {}
        try:
            details = manga_sites.fetch_manga_details(page_url)
        except Exception:
            pass
        self._signals.manga_details_resolved.emit(
            page_url, details.get("cover_url") or "", details.get("latest_chapter") or 0.0)

    def _on_manga_details_resolved(self, page_url, cover_url, latest_chapter):
        if page_url != self.url_edit.text():
            return  # the user picked a different suggestion meanwhile
        # Never overwrite a value the user already has (either from the
        # search result itself or from editing it by hand while this was
        # still loading in the background).
        if latest_chapter and not self.chapter_spin.value():
            self.chapter_spin.setValue(latest_chapter)
        if cover_url and not self.selected_cover_url:
            self.selected_cover_url = cover_url
            self._set_status_part("cover", "Loading cover...")
            threading.Thread(target=self._download_cover, args=(cover_url,), daemon=True).start()
        elif not self.selected_cover_url:
            self._set_status_part("cover", "")

    def _resolve_episode_progress(self, imdb_id, catalog, identity):
        try:
            total = stremio.fetch_latest_episode(imdb_id, catalog)
        except Exception:
            total = None
        total_season, total_episode = total or (0, 0)
        self._signals.latest_episode_resolved.emit(identity, total_season, total_episode)

    def _on_latest_episode_resolved(self, identity, total_season, total_episode):
        if identity != self._pending_episode_identity:
            return  # the user picked a different suggestion meanwhile
        self._latest_available = format_episode_progress(total_season, total_episode)
        if total_episode and not self.season_spin.value() and not self.episode_spin.value():
            self.season_spin.setValue(total_season)
            self.episode_spin.setValue(total_episode)
            # The setValue() calls above also trigger _on_progress_hand_
            # edited (which assumes any spinner change is a manual edit) -
            # this line runs after them and wins: this is never your real
            # progress, only ever the latest aired episode, so it should
            # never count as verified.
            self._progress_verified = False
            self._set_status_part("progress", NOT_YOUR_PROGRESS_HINT)

    def _on_progress_hand_edited(self, _value):
        self._progress_verified = True

    # ------------------------------------------------------------------
    def _progress_text(self):
        if self.type_box.currentText() in MANGA_TYPES:
            value = self.chapter_spin.value()
            if not value and self._original_progress and not parse_chapter_progress(self._original_progress):
                return self._original_progress  # unparsed legacy text, spinner untouched - keep it
            return format_chapter_progress(value)

        season, episode = self.season_spin.value(), self.episode_spin.value()
        if not season and not episode and self._original_progress and parse_episode_progress(self._original_progress) == (0, 0):
            return self._original_progress  # unparsed legacy text, spinners untouched - keep it
        return format_episode_progress(season, episode)

    def _save(self):
        title = self.title_combo.currentText().strip()
        if not title:
            QMessageBox.warning(self, "Tracker", "Title can't be empty.")
            return

        if self.is_new:
            self.entry = {"id": str(uuid.uuid4())}
        current_type = self.type_box.currentText()
        is_manga = current_type in MANGA_TYPES
        is_anime = current_type == "Anime"
        has_site = is_manga or is_anime
        site_id = self.site_box.currentData() if has_site else None
        saved_url = self.url_edit.text().strip()
        # An Anime entry set to a configured Video Website (not the
        # built-in "Stremio" option) must never carry a stray stremio://
        # link left over from an earlier pick or from switching the
        # dropdown by hand after one - open_tracker_entry tries url
        # first, so a stale one there would silently override the site.
        if is_anime and site_id is not None:
            saved_url = ""
        self.entry.update(
            title=title,
            type=current_type,
            status=self.status_box.currentText(),
            progress=self._progress_text(),
            progress_verified=self._progress_verified,
            latest_available=self._latest_available,
            last_watched_chapter=self.watched_chapter_spin.value() if is_manga else None,
            url=saved_url,
            cover_url=self.selected_cover_url,
            cover_path=self.selected_cover_path,
            site_id=site_id,
            imdb_id=self.selected_imdb_id if (is_anime or current_type == "Series") else None,
        )
        self.on_save(self.entry, self.is_new)
        self.accept()
