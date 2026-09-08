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
"""
from __future__ import annotations

import sys
import time

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

    from PyQt6.QtCore import QEvent, QObject, QPoint, QTimer, Qt

    Page = module.PlayerPage
    old_build = Page._build_top_bar
    old_layout = Page._layout_overlays
    old_wake = Page._wake_controls
    old_veil = Page._veil
    old_close = Page.close_player
    old_load = Page._load_into_mpv

    class _TopBarFollower(QObject):
        """Keep the owned transparent bar attached to the player window."""
        def __init__(self, page, owner, bar):
            super().__init__(page)
            self.page = page
            self.owner = owner
            self.bar = bar

        def sync(self):
            try:
                if self.page._closing:
                    return
                origin = self.page.mapToGlobal(QPoint(0, 0))
                self.bar.setGeometry(origin.x(), origin.y(),
                                     self.page.width(), module.TOPBAR_HEIGHT)
                if self.bar.isVisible():
                    self.bar.raise_()
            except (AttributeError, RuntimeError):
                pass

        def eventFilter(self, watched, event):
            et = event.type()
            if watched is self.owner and et in (
                    QEvent.Type.Move, QEvent.Type.Resize,
                    QEvent.Type.WindowStateChange, QEvent.Type.Show):
                QTimer.singleShot(0, self.sync)
            elif watched is self.bar and et == QEvent.Type.MouseButtonPress:
                # Drag the REAL Atomic window, not the promoted tool window.
                try:
                    if (event.button() == Qt.MouseButton.LeftButton
                            and not self.owner.isFullScreen()):
                        handle = self.owner.windowHandle()
                        if handle is not None:
                            handle.startSystemMove()
                            return True
                except (AttributeError, RuntimeError):
                    pass
            return False

    def _promote_topbar(self):
        bar = getattr(self, "top_bar", None)
        if bar is None:
            return
        if getattr(bar, "_atomic_alpha_overlay_146", False):
            follower = getattr(self, "_atomic_topbar_follower_146", None)
            if follower is not None:
                follower.sync()
            return
        try:
            owner = self.window()
            was_visible = bar.isVisible()
            flags = (Qt.WindowType.Tool
                     | Qt.WindowType.FramelessWindowHint
                     | Qt.WindowType.NoDropShadowWindowHint)
            # An owned top-level translucent window is the crucial difference:
            # Windows supports per-pixel alpha here, unlike on the native child
            # that produced the dark rectangles over mpv.
            bar.setParent(owner, flags)
            bar.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
            bar.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
            bar.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
            bar.setAutoFillBackground(False)
            bar.setWindowOpacity(1.0)
            bar.clearMask()
            bar.setStyleSheet("background: transparent; border: none;")
            bar._atomic_alpha_overlay_146 = True

            follower = _TopBarFollower(self, owner, bar)
            owner.installEventFilter(follower)
            bar.installEventFilter(follower)
            self._atomic_topbar_follower_146 = follower
            follower.sync()
            if was_visible:
                bar.show()
                bar.raise_()
        except (AttributeError, RuntimeError, TypeError):
            # If promotion is unavailable, leave the old bar alive rather than
            # losing the controls altogether.
            pass

    def build_top_bar(self):
        result = old_build(self)
        _promote_topbar(self)
        return result

    def layout_overlays(self):
        result = old_layout(self)
        _promote_topbar(self)
        follower = getattr(self, "_atomic_topbar_follower_146", None)
        if follower is not None:
            follower.sync()
        return result

    def wake_controls(self):
        result = old_wake(self)
        _promote_topbar(self)
        follower = getattr(self, "_atomic_topbar_follower_146", None)
        if follower is not None:
            follower.sync()
        return result

    def veil(self, widget, alpha):
        # Do not apply uniform DWM alpha to the new per-pixel-alpha top bar.
        # Doing so would dim its text/buttons and can switch the layered-window
        # mode away from the Qt surface we deliberately use here.
        if (widget is getattr(self, "top_bar", None)
                and getattr(widget, "_atomic_alpha_overlay_146", False)):
            try:
                widget.setWindowOpacity(1.0)
            except RuntimeError:
                pass
            return
        return old_veil(self, widget, alpha)

    def _stop_deferred_resume(self):
        timer = getattr(self, "_atomic_resume_timer_146", None)
        if timer is not None and timer.isActive():
            timer.stop()
        self._atomic_resume_job_146 = None

    def _target_piece_ready(stream):
        """Whether the one piece containing the saved resume offset exists."""
        info_hash = str((stream or {}).get("info_hash") or "").strip().lower()
        if not info_hash:
            return True                 # HTTP/debrid: let mpv seek normally
        try:
            from helpers import torrent_engine
            torrent = getattr(torrent_engine, "_torrents", {}).get(info_hash)
            if torrent is None:
                return False
            offset = getattr(torrent, "start_offset", None)
            if offset is None:
                return False            # Cues still arriving; video keeps going
            piece = torrent.piece_at(int(offset))
            return bool(torrent.have(piece))
        except Exception:
            return False

    def _defer_resume(self, stream, seat):
        _stop_deferred_resume(self)
        job = {"run": self._run, "seat": float(seat),
               "stream": dict(stream or {})}
        self._atomic_resume_job_146 = job

        # Prime the saved range exactly once, immediately, but DO NOT wait for
        # it. arm_start_band owns its own background retry when Cues are not yet
        # readable; calling it from the 100ms poll would create duplicate retry
        # threads fighting over the same torrent.
        info_hash = str(job["stream"].get("info_hash") or "").strip().lower()
        if info_hash:
            try:
                from helpers import torrent_engine
                torrent_engine.set_start_seconds(info_hash, job["seat"])
                torrent_engine.arm_start_band(info_hash)
            except Exception:
                pass

        timer = getattr(self, "_atomic_resume_timer_146", None)
        if timer is None:
            timer = QTimer(self)
            timer.setInterval(100)
            self._atomic_resume_timer_146 = timer

            def poll():
                current = getattr(self, "_atomic_resume_job_146", None)
                if not current:
                    timer.stop()
                    return
                if self._closing or current["run"] != self._run:
                    _stop_deferred_resume(self)
                    return
                # First picture always wins. This is the behaviour the old
                # start=<seat> path made impossible.
                if self._awaiting_first_frame:
                    return
                seat_now = float(current["seat"])
                if float(getattr(self, "_position", 0.0) or 0.0) >= \
                        seat_now - module.RESUME_LANDED_TOLERANCE_S:
                    _stop_deferred_resume(self)
                    return
                if not _target_piece_ready(current["stream"]):
                    return

                _stop_deferred_resume(self)
                # Now the exact target range exists. Reuse the player's seat
                # reporting, but the seek itself is no longer allowed to gate
                # the initial frame or cover the playing video while it waits.
                self._resume_target = seat_now
                self._seat_reported = None
                self._seat_deadline = time.monotonic() + module.SEAT_GIVE_UP_S
                self._seat_timer.start()
                if getattr(self, "_work_toast", None) is not None:
                    self._say_working(
                        f"Skipping To {module._format_time(seat_now)}...")
                self._seek_absolute(seat_now, resuming=True)

            timer.timeout.connect(poll)
        timer.start()

    def load_into_mpv(self, stream, resume_at=None):
        """Open immediately; apply the saved seat only after its range exists."""
        try:
            seat = self._prime_seat(resume_at)[0]
        except Exception:
            seat = None
        if not seat:
            _stop_deferred_resume(self)
            return old_load(self, stream, resume_at)

        # old_load decides whether to use mpv's blocking start=<seat> by asking
        # _prime_seat. Suppress that one internal read only. The stream engine
        # was already given the real seat by _prepare_stream_worker, and the
        # deferred poll above keeps it primed after the URL is handed to mpv.
        sentinel = object()
        prior = self.__dict__.get("_prime_seat", sentinel)
        self._prime_seat = lambda _resume_at=None: (None, None)
        try:
            result = old_load(self, stream, resume_at)
        finally:
            if prior is sentinel:
                try:
                    del self.__dict__["_prime_seat"]
                except KeyError:
                    pass
            else:
                self.__dict__["_prime_seat"] = prior

        _defer_resume(self, stream, seat)
        return result

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

    Page._build_top_bar = build_top_bar
    Page._layout_overlays = layout_overlays
    Page._wake_controls = wake_controls
    Page._veil = veil
    Page._load_into_mpv = load_into_mpv
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
