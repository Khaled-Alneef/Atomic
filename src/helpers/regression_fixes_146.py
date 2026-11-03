"""Two targeted player fixes after 1.10.145.

1. The top bar must have *no rectangular ground at all* over live video.
   A native CHILD cannot do per-pixel alpha over mpv, which is why clipping the
   child merely turned one full-width dark slab into several dark rectangles.
   Promote only this bar to an owned translucent TOOL window: top-level layered
   windows do have per-pixel alpha on Windows, so transparent pixels really show
   the video and the labels/buttons keep their normal opaque pixels.

2. Resume must never block first picture.  Opening mpv with ``start=<seat>``
   makes demux open depend on the exact resume range being present, so a torrent
   can be 68% complete and still show no frame.  Open from the available head,
   keep the saved range at priority 7, and jump only when that range is ready.

No scrolling, chapter, settings or unrelated player behaviour is changed.

Both fixes are history now: the top bar is player_top_bar_live_patch's, and a
resume opens at its seat (player_resume_latency_patch). Their code was removed
3 November 2026 - see _patch_player.
"""
from __future__ import annotations

import sys

_INSTALLED = False
_PATCHED = set()


def _override_old_topbar_helpers():
    """Stop the old child-window patch from painting/clipping dark islands."""
    try:
        from PyQt6.QtWidgets import QLabel, QPushButton, QWidget
        from . import player_top_bar_transparency_patch as old
    except Exception:
        return

    def clean(player, page):
        bar = getattr(page, "top_bar", None)
        if bar is None:
            return
        try:
            bar.setAutoFillBackground(False)
            bar.setAttribute(player.Qt.WidgetAttribute.WA_NoSystemBackground, True)
            bar.setAttribute(player.Qt.WidgetAttribute.WA_TranslucentBackground, True)
            bar.setStyleSheet("background: transparent; border: none;")

            # The bar is now a true alpha surface. These controls therefore
            # need no resting rectangle of their own; their glyph/text pixels
            # are what is painted. Existing hover rules remain intact.
            for label in bar.findChildren(QLabel):
                style = label.styleSheet() or ""
                if "AtomicTopBarAlpha146" not in style:
                    label.setStyleSheet(
                        style + "\n/* AtomicTopBarAlpha146 */"
                        + "\nQLabel { background: transparent; border: none; }")
            for button in bar.findChildren(QPushButton):
                style = button.styleSheet() or ""
                if "AtomicTopBarAlpha146" not in style:
                    button.setStyleSheet(
                        style + "\n/* AtomicTopBarAlpha146 */"
                        + "\nQPushButton { background: transparent; border: none; }")

            # Layout holders can otherwise inherit the old bar fill even when
            # every visible label/button is transparent.
            for child in bar.children():
                if (isinstance(child, QWidget)
                        and not isinstance(child, (QLabel, QPushButton))):
                    child.setAutoFillBackground(False)
                    child.setAttribute(
                        player.Qt.WidgetAttribute.WA_NoSystemBackground, True)
        except (AttributeError, RuntimeError, TypeError):
            pass

    def never_clip(_player, page):
        bar = getattr(page, "top_bar", None)
        if bar is not None:
            try:
                bar.clearMask()
            except RuntimeError:
                pass

    # Existing wrappers in the older patch resolve these globals at call time.
    old._clean_controls = clean
    old._clip_live_bar = never_clip


def _patch_player(module):
    key = id(module)
    if key in _PATCHED:
        return
    _PATCHED.add(key)

    # **The promoted top bar and the deferred resume are gone**, at his word,
    # 3 November 2026 (CLAUDE.md rule 11): _TopBarFollower, _promote_topbar,
    # build_top_bar, layout_overlays, wake_controls, veil, _target_piece_ready,
    # _defer_resume and load_into_mpv. Later patches replaced every one of them
    # (player_top_bar_live_patch, regression_fixes_152's restore), and walking
    # the live functions from PlayerPage never reached them. close_player is
    # what is left, and it still runs.
    Page = module.PlayerPage
    old_close = Page.close_player

    def _stop_deferred_resume(self):
        timer = getattr(self, "_atomic_resume_timer_146", None)
        if timer is not None and timer.isActive():
            timer.stop()
        self._atomic_resume_job_146 = None

    def close_player(self):
        _stop_deferred_resume(self)
        bar = getattr(self, "top_bar", None)
        try:
            if bar is not None:
                bar.hide()
        except RuntimeError:
            pass
        result = old_close(self)
        try:
            if bar is not None and getattr(bar, "_atomic_alpha_overlay_146", False):
                bar.deleteLater()
        except RuntimeError:
            pass
        return result

    Page.close_player = close_player


def _chain_player_patch():
    # requested_fixes_patch is the standing lazy loader for windows.player.
    # Chain behind it so this works whether player was imported already or later.
    try:
        from . import requested_fixes_patch as requested
        previous = requested._patch_player

        def chained(module):
            previous(module)
            _patch_player(module)

        requested._patch_player = chained
        loaded = sys.modules.get("windows.player")
        if loaded is not None:
            _patch_player(loaded)
    except Exception:
        pass


def install():
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True
    _override_old_topbar_helpers()
    _chain_player_patch()
