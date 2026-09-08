"""Requested 29-Aug fixes. Stable wheel/native-refresh physics stay untouched."""
from __future__ import annotations

import ctypes
import importlib.abc
import importlib.machinery
import sys

_INSTALLED = False
_PATCHED = set()
_TARGETS = {
    "windows.reader": "reader",
    "helpers.settings_dialog": "settings",
    "helpers.video_backend": "video",
}


def _scroll_rearm(window):
    """Clear only stale paint acknowledgement after immersive teardown."""
    if window is None:
        return
    try:
        from PyQt6.QtCore import QObject, QTimer
        from PyQt6.QtWidgets import QAbstractScrollArea
    except Exception:
        return

    def run():
        try:
            page = getattr(window, "_current_page", None)
            if page is None:
                return
            for obj in page.findChildren(QObject):
                motion = getattr(obj, "motion", None)
                if motion is not None and hasattr(motion, "_atomic_waiting_paint"):
                    motion._atomic_waiting_paint = False
                    motion._atomic_waiting_since = 0.0
                if hasattr(obj, "_atomic_waiting_paint"):
                    obj._atomic_waiting_paint = False
                    obj._atomic_waiting_since = 0.0
            for area in page.findChildren(QAbstractScrollArea):
                try:
                    area.viewport().update()
                    body = area.widget()
                    if body is not None:
                        body.update()
                except (AttributeError, RuntimeError):
                    pass
            page.update()
        except RuntimeError:
            pass

    # The stable scroll model is not changed: only discard a stale hidden-page
    # paint latch once the old page is visible again.
    for delay in (0, 16, 60):
        QTimer.singleShot(delay, run)


def _patch_reader(module):
    key = ("reader", id(module))
    if key in _PATCHED:
        return
    _PATCHED.add(key)
    Page = module.ReaderPage
    old_load = Page._load_chapters
    old_partial = Page._on_chapters_partial
    old_show = Page._show_only
    old_leave = Page.leave
    old_hold = module._hold_edge_reach
    old_release = module._release_edge_reach

    def load(self, refresh=False):
        # Continue is one trip to a chapter. Do not briefly advertise the list
        # while a warm cache is delivered on the next event-loop turn.
        if not refresh and getattr(self, "_opening_chapter", False):
            self._set_message("Loading chapter...", browser=True)
        return old_load(self, refresh=refresh)

    def partial(self, run, chapters):
        # Partial lists help browsing, but a Continue/details-row click already
        # named a chapter destination and must not show the list in between.
        if getattr(self, "_opening_chapter", False):
            return
        return old_partial(self, run, chapters)

    def show_only(self, widget):
        if (widget is getattr(self, "_list_view", None)
                and getattr(self, "_opening_chapter", False)):
            return
        return old_show(self, widget)

    def mark(self, index, finished):
        """Apply read boundaries in the chapter list's newest->oldest order."""
        if not (0 <= index < len(self.chapters)):
            return
        number = module.chapter_number(self.chapters[index])
        if number is None:
            module.show_toast(self, "This Chapter Has No Number to Record")
            return
        if finished:
            # The clicked chapter and every older row below it are read.
            rows, target = self.chapters[index:], number
        else:
            # The clicked chapter and every newer row above it are unread.
            rows, target = self.chapters[:index + 1], 0.0
            for chapter in self.chapters[index + 1:]:
                older = module.chapter_number(chapter)
                if older is not None:
                    target = older
                    break
        values, seen = [], set()
        for chapter in rows:
            value = module.chapter_number(chapter)
            if value is not None and value not in seen:
                seen.add(value)
                values.append(value)
        module._mark_history(self.entry, values, bool(finished))
        if (not module.correct_progress(self.entry, chapter=target)
                and self.entry.get("id")):
            module.show_toast(self, "Could Not Save That Chapter")
            return
        current = (self.chapter_index
                   if 0 <= self.chapter_index < len(self.chapters)
                   else self._row_for_number(target))
        self._list_view.set_chapters(
            self.chapters, current, self._last_read_number())
        self._sync_controls()

    def mark_all(self, finished):
        values, seen = [], set()
        for chapter in self.chapters:
            value = module.chapter_number(chapter)
            if value is not None and value not in seen:
                seen.add(value)
                values.append(value)
        if not values:
            module.show_toast(self, "These Chapters Carry No Numbers to Record")
            return
        target = values[0] if finished else 0.0  # first numeric row is newest
        module._mark_history(self.entry, values, bool(finished))
        if (not module.correct_progress(self.entry, chapter=target)
                and self.entry.get("id")):
            module.show_toast(self, "Could Not Save That")
            return
        current = (self.chapter_index
                   if 0 <= self.chapter_index < len(self.chapters) else -1)
        self._list_view.set_chapters(
            self.chapters, current, self._last_read_number())
        self._sync_controls()

    def hold(window):
        """Snapshot Windows' placement before the 4px edge-reach mutates it."""
        try:
            if window is not None and not getattr(window, "_edge_reach_on", False):
                window._atomic_reader_saved_geometry = window.saveGeometry()
                window._atomic_reader_was_fullscreen = bool(window.isFullScreen())
                window._atomic_reader_native_placement = None
                if sys.platform == "win32":
                    try:
                        hwnd = ctypes.c_void_p(int(window.winId()))
                        placement = module.window_chrome._placement(hwnd)
                        if placement is not None:
                            window._atomic_reader_native_placement = ctypes.string_at(
                                ctypes.addressof(placement), ctypes.sizeof(placement))
                    except Exception:
                        pass
        except Exception:
            pass
        return old_hold(window)

    def release(window):
        """Restore the pre-reader native placement in one non-painted step.

        saveGeometry/restoreGeometry was not sufficient here: edge-reach calls
        setGeometry on a maximised frameless window, so Qt and Windows no longer
        agree about the maximise state. Restoring the actual WINDOWPLACEMENT is
        the state transition Windows itself understands and avoids drawing the
        restored-size window between Reader and the maximised app.
        """
        if window is None or not getattr(window, "_edge_reach_on", False):
            return old_release(window)

        native = getattr(window, "_atomic_reader_native_placement", None)
        saved = getattr(window, "_atomic_reader_saved_geometry", None)
        was_fullscreen = bool(getattr(window, "_atomic_reader_was_fullscreen", False))

        try:
            window._edge_reach_on = False

            def restore_once():
                if was_fullscreen:
                    # Qt owns the fullscreen flag, so restore it through Qt; the
                    # animation suppression below prevents an intermediate rect.
                    window.showFullScreen()
                    return
                if native and sys.platform == "win32":
                    placement_type = module.window_chrome._WINDOWPLACEMENT
                    placement = placement_type.from_buffer_copy(native)
                    placement.length = ctypes.sizeof(placement_type)
                    hwnd = ctypes.c_void_p(int(window.winId()))
                    if not ctypes.windll.user32.SetWindowPlacement(
                            hwnd, ctypes.byref(placement)):
                        raise OSError("SetWindowPlacement failed")
                    return
                if saved and window.restoreGeometry(saved):
                    return
                raise RuntimeError("no reader placement could be restored")

            quiet = getattr(module.theme, "without_window_animation", None)
            if callable(quiet):
                quiet(window, restore_once)
            else:
                restore_once()

            try:
                module.window_chrome.ensure_snap_styles(window)
            except Exception:
                pass
            window._atomic_reader_native_placement = None
            window._atomic_reader_saved_geometry = None
            window.update()
            return
        except Exception:
            # Put the flag back so the original, well-tested fallback sees the
            # state it expects. It may animate, but it is still better than a
            # window left in edge-reach geometry if Win32 restoration failed.
            try:
                window._edge_reach_on = True
            except Exception:
                pass
            return old_release(window)

    def leave(self):
        try:
            window = self.window()
        except RuntimeError:
            window = None
        result = old_leave(self)
        _scroll_rearm(window)
        return result

    Page._load_chapters = load
    Page._on_chapters_partial = partial
    Page._show_only = show_only
    Page._mark_chapter = mark
    Page._mark_all_chapters = mark_all
    Page.leave = leave
    module._hold_edge_reach = hold
    module._release_edge_reach = release


def _patch_settings(module):
    key = ("settings", id(module))
    if key in _PATCHED:
        return
    _PATCHED.add(key)
    Dialog = module.SettingsDialog
    old = Dialog._build_anime_page

    def remove_item(layout, widget):
        if layout is None:
            return False
        for index in range(layout.count() - 1, -1, -1):
            item = layout.itemAt(index)
            if item.widget() is widget:
                layout.takeAt(index)
                # Remove the spacer immediately before the Picture section too.
                if index > 0:
                    previous = layout.itemAt(index - 1)
                    if previous is not None and previous.spacerItem() is not None:
                        layout.takeAt(index - 1)
                return True
            nested = item.layout()
            if nested is not None and remove_item(nested, widget):
                return True
        return False

    def build(self):
        page = old(self)
        from PyQt6.QtWidgets import QLabel
        for label in list(page.findChildren(QLabel)):
            text = (label.text() or "").strip()
            if (text == "Picture"
                    or text.startswith("Off by default, and off is what most people want.")):
                remove_item(page.layout(), label)
                label.hide()
                label.deleteLater()
        return page

    Dialog._build_anime_page = build


def _patch_video(module):
    key = ("video", id(module))
    if key in _PATCHED:
        return
    _PATCHED.add(key)
    old = module.default_options

    def defaults():
        options = dict(old())
        # Torrent preparation has already waited for playable data. Do not make
        # mpv wait a second time for its preferred cache runway before frame 1.
        options["cache_pause_initial"] = False
        options["cache_pause_wait"] = 0.35
        return options

    module.default_options = defaults


def _patch_player(module):
    key = ("player", id(module))
    if key in _PATCHED:
        return
    _PATCHED.add(key)
    Page = module.PlayerPage
    old_close = Page.close_player
    old_snapshot = Page._startup_snapshot

    def close(self):
        try:
            window = self.window()
        except RuntimeError:
            window = None
        result = old_close(self)
        _scroll_rearm(window)
        return result

    def startup_snapshot(self):
        target, text = old_snapshot(self)
        # The engine's head-byte meter deliberately printed 99 even when the
        # entire startup head was already present. At that point the remaining
        # work is container/index opening, not buffering another 1%, so a frozen
        # "99%" is both misleading and exactly the pause the owner reported.
        if (getattr(self, "_awaiting_first_frame", False)
                and str(text).startswith("Buffering... 99%")):
            return min(float(target), 0.97), "Opening video..."
        return target, text

    Page.close_player = close
    Page._startup_snapshot = startup_snapshot


def _patch_details(module):
    key = ("details", id(module))
    if key in _PATCHED:
        return
    _PATCHED.add(key)
    Page = module.DetailsPage

    def chapter_menu(self, event, number):
        from windows.tracker import correct_progress
        try:
            number = float(number)
        except (TypeError, ValueError):
            return
        numbers, seen = [], set()
        for chapter in getattr(self, "_chapters", ()) or ():
            try:
                value = module.chapter_number(chapter)
                if value is None:
                    continue
                value = float(value)
            except (TypeError, ValueError):
                continue
            if value not in seen:
                seen.add(value)
                numbers.append(value)  # source/UI order: newest -> oldest
        if number not in numbers:
            numbers.append(number)
            numbers.sort(reverse=True)
        index = numbers.index(number)
        last_read = float(self._last_read() or 0.0)
        already = bool(
            last_read >= number
            or module.history.chapter_key(number) in self._history_marks)

        menu = module.QMenu(self)
        mark = menu.addAction("Mark as Unread" if already else "Mark as Read")
        menu.addSeparator()
        mark_all = menu.addAction("Mark All as Read")
        clear_all = menu.addAction("Mark All as Unread")
        chosen = menu.exec(event.globalPosition().toPoint())

        if chosen is mark:
            read = not already
            if read:
                affected, target = numbers[index:], number
            else:
                affected = numbers[:index + 1]
                target = numbers[index + 1] if index + 1 < len(numbers) else 0.0
        elif chosen is mark_all:
            read, affected = True, numbers
            target = numbers[0] if numbers else 0.0
        elif chosen is clear_all:
            read, affected, target = False, numbers, 0.0
        else:
            return

        self._mark_history(
            [module.history.chapter_key(value) for value in affected], read)
        if (self.entry.get("id")
                and not correct_progress(self.entry, chapter=target)):
            module.show_toast(self, "Could Not Save That")
            return
        self._fill_rows()

    Page._chapter_menu = chapter_menu


def _soften_2k_text():
    """Leave 1080p as-is; soften text grid fitting on 2560px+ displays."""
    try:
        from PyQt6.QtGui import QCursor, QFont, QGuiApplication
        from PyQt6.QtWidgets import QApplication
        from . import theme
    except Exception:
        return
    old_font, old_apply = theme.font, theme.apply_theme

    def high_res():
        try:
            app = QApplication.instance()
            if app is None:
                return False
            active = app.activeWindow()
            screen = active.screen() if active is not None else None
            if screen is None:
                screen = QGuiApplication.screenAt(QCursor.pos())
            if screen is None:
                screen = app.primaryScreen()
            if screen is None:
                return False
            physical_width = (screen.size().width()
                              * float(screen.devicePixelRatio() or 1.0))
            return physical_width >= 2500.0
        except Exception:
            return False

    def soften(font):
        if high_res():
            font.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
            font.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
        return font

    def font(*args, **kwargs):
        return soften(old_font(*args, **kwargs))

    def apply(app):
        result = old_apply(app)
        if high_res():
            app.setFont(soften(app.font()))
        return result

    theme.font, theme.apply_theme = font, apply


def _tune_streams():
    """Shorten dead-source ownership without narrowing provider coverage."""
    try:
        from . import streams
        limits = {
            "DEFAULT_TIMEOUT": 8.0,
            "FANOUT_BUDGET_S": 8.0,
            "QUEUED_METADATA_TIMEOUT": 3.5,
            "QUEUED_DATA_WAIT": 3.5,
            "SOLO_METADATA_TIMEOUT": 2.5,
            "SOLO_DATA_WAIT": 2.5,
            "RACE_TIMEOUT": 20.0,
        }
        for name, ceiling in limits.items():
            current = getattr(streams, name, None)
            if current is not None:
                setattr(streams, name, min(float(current), ceiling))
    except Exception:
        pass


def _tune_torrent_start():
    """Do not hold a fully playable head for a multi-second optional index wait."""
    try:
        from . import torrent_engine
        # await_start explicitly treats a missing index as non-fatal and keeps
        # its tail pieces at high priority after handing mpv the URL. The old
        # 3-second safety wait is therefore pure first-frame latency on the case
        # reported as "99%". Keep a short overlap for fast indexes, then let mpv
        # ask for whatever container bytes it actually needs.
        if hasattr(torrent_engine, "INDEX_WAIT"):
            torrent_engine.INDEX_WAIT = min(
                float(torrent_engine.INDEX_WAIT), 0.6)
        # RESUME_INDEX_WAIT is intentionally untouched: a resume offset is read
        # out of that index, so skipping it would make resume less reliable.
    except Exception:
        pass


def _chain_existing():
    # Use the existing player/details import-hook chains instead of competing
    # finders for the same modules.
    from . import player_watch_threshold_patch as player_patch
    previous_player = player_patch._patch

    def player_chain(module):
        previous_player(module)
        _patch_player(module)
    player_patch._patch = player_chain
    if sys.modules.get("windows.player") is not None:
        _patch_player(sys.modules["windows.player"])

    from . import episode_watch_state_patch as details_patch
    previous_details = details_patch._patch

    def details_chain(module):
        previous_details(module)
        _patch_details(module)
    details_patch._patch = details_chain
    if sys.modules.get("windows.details") is not None:
        _patch_details(sys.modules["windows.details"])


class _Loader(importlib.abc.Loader):
    def __init__(self, wrapped, patcher):
        self.wrapped, self.patcher = wrapped, patcher

    def create_module(self, spec):
        creator = getattr(self.wrapped, "create_module", None)
        return creator(spec) if creator is not None else None

    def exec_module(self, module):
        self.wrapped.exec_module(module)
        self.patcher(module)


class _Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        kind = _TARGETS.get(fullname)
        if kind is None:
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec is None or spec.loader is None:
            return spec
        patcher = {
            "reader": _patch_reader,
            "settings": _patch_settings,
            "video": _patch_video,
        }[kind]
        spec.loader = _Loader(spec.loader, patcher)
        return spec


def _lazy_targets():
    patchers = {
        "windows.reader": _patch_reader,
        "helpers.settings_dialog": _patch_settings,
        "helpers.video_backend": _patch_video,
    }
    pending = False
    for name, patcher in patchers.items():
        module = sys.modules.get(name)
        if module is None:
            pending = True
        else:
            patcher(module)
    if pending:
        sys.meta_path.insert(0, _Finder())


def install():
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True
    _soften_2k_text()
    _tune_streams()
    _tune_torrent_start()
    _chain_existing()
    _lazy_targets()
