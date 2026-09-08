"""Targeted follow-up for the four regressions reported after 1.10.144.

Scope is intentionally narrow:
- make the live player top bar actually drop its full-width native background;
- remove resume-only startup waits so playback can start from a tiny buffer;
- make Settings Cancel restore settings without rebuilding the current page;
- give Skip Intro / Next Episode the same deep-teal action colours as Continue.

No scroll cadence, wheel physics, chapter logic or unrelated UI is changed.
"""
from __future__ import annotations

import sys

_INSTALLED = False
_PATCHED_PLAYER = set()
_PATCHED_SETTINGS = set()


def _install_live_topbar_renderer():
    """Use the clipped live-bar path and make sure it runs at frame one.

    The earlier clip code was sound in principle but its decision was based on
    `_awaiting_first_frame` and was never guaranteed to run on the transition
    where that flag flips.  1.10.144 then disabled the clip entirely, which is
    why the full native rectangle still becomes visible over real video.

    Loading keeps the existing full bar (it already looks correct there).  Once
    the first real frame arrives, the HWND is clipped to the actual visible bar
    children so the empty full-width rectangle cannot cover the picture.
    """
    try:
        from PyQt6.QtGui import QRegion
        from PyQt6.QtWidgets import QWidget
        from . import player_top_bar_transparency_patch as topbar
    except Exception:
        return

    def clip_live(player, page):
        bar = getattr(page, "top_bar", None)
        if bar is None:
            return
        try:
            if getattr(page, "_awaiting_first_frame", True):
                bar.clearMask()
                return
            layout = bar.layout()
            if layout is not None:
                layout.activate()
            region = QRegion()
            for child in bar.children():
                if not isinstance(child, QWidget) or not child.isVisible():
                    continue
                rect = child.geometry().adjusted(-2, -2, 2, 2)
                if rect.width() > 0 and rect.height() > 0:
                    region = region.united(QRegion(rect))
            if region.isEmpty():
                bar.clearMask()
            else:
                bar.setMask(region)
        except (AttributeError, RuntimeError, TypeError):
            pass

    # All existing top-bar wrappers call this module global dynamically, so
    # replacing it also fixes build/layout/control-wake paths already installed.
    topbar._clip_live_bar = clip_live


def _speed_up_resume_start():
    """Do not hold playback behind resume/index pre-buffering.

    The local HTTP server is range-aware and re-focuses libtorrent on every mpv
    read.  Waiting for the first chosen-file piece is enough to prove the swarm
    is alive; the Cues/tail and resume-position pieces can continue arriving at
    priority 7 while mpv is already opening the stream.

    This deliberately does NOT use total-file completion as a gate.  A few
    percent of a large episode is already far more data than first-frame startup
    needs, and the exact required range is more important than the percentage.
    """
    try:
        from . import torrent_engine
    except Exception:
        return

    # Normal startup was already made non-blocking in 1.10.144.  Resume must not
    # secretly restore a six-second index gate or the separate three-second band
    # gate on top of it.
    if hasattr(torrent_engine, "RESUME_INDEX_WAIT"):
        torrent_engine.RESUME_INDEX_WAIT = 0.0
    if hasattr(torrent_engine, "RESUME_BAND_WAIT"):
        torrent_engine.RESUME_BAND_WAIT = 0.0
    if hasattr(torrent_engine, "INDEX_WAIT"):
        torrent_engine.INDEX_WAIT = 0.0

    def no_resume_pre_wait(info_hash, wait=None):
        # `arm_start_band` has already raised the target range to priority 7.
        # Hand control to mpv immediately; its Range request is the authoritative
        # statement of what byte is actually needed next.
        return False

    torrent_engine.await_start_band = no_resume_pre_wait

    # Keep mpv's own initial cache gate tiny as well.  1.10.144 already turned
    # cache-pause-initial off; this tightens only startup runway, not the ongoing
    # torrent readahead that prevents stalls once playback is moving.
    try:
        from . import video_backend
        previous = video_backend.default_options
        if not getattr(previous, "_atomic_145", False):
            def defaults():
                options = dict(previous())
                options["cache_pause_initial"] = False
                options["cache_pause_wait"] = 0.05
                options["demuxer_readahead_secs"] = 2
                options["demuxer_max_bytes"] = "16MiB"
                return options
            defaults._atomic_145 = True
            video_backend.default_options = defaults
    except Exception:
        pass


def _style_skip_action(player, page):
    """Match Continue Watching's deep-teal action palette, size untouched."""
    button = getattr(page, "skip_btn", None)
    if button is None:
        return
    try:
        # Same palette and hover/press states as theme's #Accent rule used by
        # the Continue Watching action.  Geometry is not changed here.
        button.setStyleSheet(
            f"QPushButton {{ background: {player.theme.ACCENT_BUTTON_GRADIENT};"
            f" color: {player.theme.ON_ACCENT_DEEP}; border: none;"
            f" border-radius: {player.BAR_RADIUS}px; padding: 0px;"
            f" font-size: 11pt; font-weight: 700; }}"
            f"QPushButton:hover {{ background: {player.theme.ACCENT_BUTTON_GRADIENT_HOVER}; }}"
            f"QPushButton:pressed {{ background: {player.theme.ACCENT_DEEP_ACTIVE}; }}")
    except (AttributeError, RuntimeError):
        pass


def _patch_player(module):
    key = id(module)
    if key in _PATCHED_PLAYER:
        return
    _PATCHED_PLAYER.add(key)
    Page = module.PlayerPage

    old_init = Page.__init__
    old_property = Page._on_property

    def init(self, *args, **kwargs):
        old_init(self, *args, **kwargs)
        _style_skip_action(module, self)

    def on_property(self, name, value):
        was_waiting = bool(getattr(self, "_awaiting_first_frame", False))
        result = old_property(self, name, value)
        # This is the missing transition in the previous top-bar fix: call the
        # clip immediately after the first decoded time-pos flips the flag.
        if was_waiting and not getattr(self, "_awaiting_first_frame", True):
            try:
                from . import player_top_bar_transparency_patch as topbar
                topbar._clip_live_bar(module, self)
            except Exception:
                pass
        return result

    Page.__init__ = init
    Page._on_property = on_property


def _patch_settings(module):
    key = id(module)
    if key in _PATCHED_SETTINGS:
        return
    _PATCHED_SETTINGS.add(key)
    Dialog = module.SettingsDialog
    old_reject = Dialog.reject

    def reject(self):
        """Undo Settings changes without rebuilding the page behind the dialog."""
        main_window = self.parent()
        refresh = getattr(main_window, "refresh_current_page", None)
        replaced = False
        if main_window is not None and callable(refresh):
            try:
                # The original reject correctly restores settings.json and then
                # calls _apply_section_visibility.  Keep its sidebar/nav refresh,
                # but suppress only the page recreation that made Cancel visibly
                # reload the page.  Save already closes without that rebuild.
                main_window.refresh_current_page = lambda: None
                replaced = True
            except Exception:
                replaced = False
        try:
            return old_reject(self)
        finally:
            if replaced:
                try:
                    main_window.refresh_current_page = refresh
                except Exception:
                    pass

    Dialog.reject = reject


def _chain_runtime_patches():
    try:
        from . import requested_fixes_patch as requested

        previous_player = requested._patch_player
        def player_chain(module):
            previous_player(module)
            _patch_player(module)
        requested._patch_player = player_chain
        loaded = sys.modules.get("windows.player")
        if loaded is not None:
            _patch_player(loaded)

        previous_settings = requested._patch_settings
        def settings_chain(module):
            previous_settings(module)
            _patch_settings(module)
        requested._patch_settings = settings_chain
        loaded = sys.modules.get("helpers.settings_dialog")
        if loaded is not None:
            _patch_settings(loaded)
    except Exception:
        pass


def install():
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True
    _install_live_topbar_renderer()
    _speed_up_resume_start()
    _chain_runtime_patches()
