"""Keep hero/banner work and page transitions out of wheel-scroll frames.

This patch handles two interaction boundaries that are easy to miss:

* PageSlide hides the real incoming page while its snapshot transition is
  running.  A wheel event must end that transition and then be routed to the
  actual scroll owner, not merely to whichever QLabel/button happened to be
  under the pointer.
* Hero/banner work must stay off the GUI thread while *any* Atomic scroll
  surface is moving.  That includes ordinary _Momentum/QScrollArea motion and
  PosterGrid's independent FrameMotion/Qt Quick path used by Discover.
"""

from __future__ import annotations

import importlib.abc
import importlib.machinery
import sys


_TARGET = "windows.home"
_INSTALLED = False
_PATCHED = False
_WIDGETS_PATCHED = False


def _patch_widgets(w):
    global _WIDGETS_PATCHED
    if _WIDGETS_PATCHED:
        return
    _WIDGETS_PATCHED = True

    from PyQt6.QtCore import QTimer
    from PyQt6.QtWidgets import QApplication, QAbstractScrollArea, QWidget

    def scroll_busy() -> bool:
        """True for either ordinary page momentum or PosterGrid motion."""
        try:
            if w.momentum_active():
                return True
        except Exception:
            pass

        app = QApplication.instance()
        if app is None:
            return False
        # Banner work reaches this helper only a handful of times per second,
        # so a widget walk here is far cheaper than letting one 20ms decode or
        # fade repaint steal several 240Hz frames.
        try:
            widgets = app.allWidgets()
        except Exception:
            return False
        for widget in widgets:
            try:
                surface = getattr(widget, "_atomic_grid_quick", None)
                if surface is not None and getattr(surface, "_active", False):
                    return True
                motion = getattr(widget, "_motion", None)
                if (motion is not None
                        and widget.__class__.__name__ == "PosterGrid"
                        and motion.running()):
                    return True
            except (RuntimeError, AttributeError):
                continue
        return False

    # Export this so Home's import-side patch and any future banner callback
    # can ask the same question instead of reimplementing the two scroll paths.
    w.atomic_scroll_busy = scroll_busy

    # ---- Wheel input during a page-slide ---------------------------------
    old_slide_wheel = getattr(w.PageSlide, "wheelEvent", None)

    def _deliver_to_scroll_owner(event, target):
        """Route a wheel event as Qt's normal parent propagation would.

        QApplication.sendEvent(target, event) to an arbitrary child is not
        enough: direct sendEvent does not walk parents when that QLabel/button
        ignores the wheel, which is why the first notches after navigation were
        still disappearing. Walk upward ourselves and stop at the real scroll
        surface.
        """
        node = target
        seen = set()
        while isinstance(node, QWidget) and id(node) not in seen:
            seen.add(id(node))
            try:
                if getattr(node, "accepts_relayed_wheel", False):
                    event.setAccepted(False)
                    QApplication.sendEvent(node, event)
                    return event.isAccepted()
                if isinstance(node, QAbstractScrollArea):
                    viewport = node.viewport()
                    event.setAccepted(False)
                    QApplication.sendEvent(viewport, event)
                    return event.isAccepted()
            except RuntimeError:
                return False
            try:
                node = node.parentWidget()
            except RuntimeError:
                return False
        return False

    def page_slide_wheel(self, event):
        if getattr(self, "_axis", "y") != "y":
            if old_slide_wheel is not None:
                return old_slide_wheel(self, event)
            event.ignore()
            return

        # Make the real incoming page live immediately. PageSlide.stop() calls
        # its on_done synchronously, so widgetAt below sees the page rather than
        # the transition snapshot.
        try:
            self.stop()
        except RuntimeError:
            event.ignore()
            return

        try:
            target = QApplication.widgetAt(event.globalPosition().toPoint())
        except Exception:
            target = None
        if target is not None and target is not self:
            if _deliver_to_scroll_owner(event, target):
                return

        # Last fallback: find the first visible scroll surface in the page under
        # the pointer's top-level window. This is only reached for decorative
        # margins whose widget ancestry does not include the viewport.
        try:
            window = self.window()
            candidates = window.findChildren(QAbstractScrollArea)
            for area in candidates:
                if not area.isVisible():
                    continue
                global_pos = event.globalPosition().toPoint()
                local = area.viewport().mapFromGlobal(global_pos)
                if area.viewport().rect().contains(local):
                    event.setAccepted(False)
                    QApplication.sendEvent(area.viewport(), event)
                    if event.isAccepted():
                        return
        except RuntimeError:
            pass
        event.accept()

    w.PageSlide.wheelEvent = page_slide_wheel

    # ---- Hero work while scrolling --------------------------------------
    old_set_backdrop = w.HeroBanner.set_backdrop
    old_resmooth = w.HeroBanner._resmooth
    old_on_fade = w.HeroBanner._on_fade

    def _pending_timer(banner):
        timer = getattr(banner, "_atomic_scroll_defer_timer", None)
        if timer is not None:
            return timer
        timer = QTimer(banner)
        timer.setSingleShot(True)
        timer.setInterval(48)

        def flush():
            try:
                if scroll_busy():
                    timer.start()
                    return
                pending = getattr(banner, "_atomic_pending_backdrop", None)
                banner._atomic_pending_backdrop = None
                if pending is not None:
                    old_set_backdrop(banner, pending[0], fade=pending[1])
                if getattr(banner, "_atomic_pending_resmooth", False):
                    banner._atomic_pending_resmooth = False
                    old_resmooth(banner)
            except RuntimeError:
                return

        timer.timeout.connect(flush)
        banner._atomic_scroll_defer_timer = timer
        return timer

    def quiet_set_backdrop(self, path, fade=True):
        if scroll_busy():
            self._atomic_pending_backdrop = (path, bool(fade))
            _pending_timer(self).start()
            return self._backdrop is not None
        return old_set_backdrop(self, path, fade=fade)

    def quiet_resmooth(self):
        if scroll_busy():
            self._atomic_pending_resmooth = True
            _pending_timer(self).start()
            return
        return old_resmooth(self)

    def quiet_fade_tick(self, value):
        if scroll_busy():
            # No hidden banner animation is worth a missed presentation. Land
            # the fade state and let the live widget repaint once after motion.
            try:
                self._fade.stop()
            except Exception:
                pass
            self._mix = 1.0
            self._previous = None
            return
        return old_on_fade(self, value)

    w.HeroBanner.set_backdrop = quiet_set_backdrop
    w.HeroBanner._resmooth = quiet_resmooth
    w.HeroBanner._on_fade = quiet_fade_tick


def _patch_home(module):
    global _PATCHED
    if _PATCHED:
        return
    _PATCHED = True

    widgets = sys.modules.get("helpers.widgets")
    if widgets is not None:
        _patch_widgets(widgets)

    HomePage = module.HomePage

    # The original Home code measured 21-26ms for the first hero-cover cut.
    # Submit it to the existing worker pool and, crucially, do not even submit
    # new warm work while a scroll is active. A queued decode can otherwise
    # finish and contend for the GIL/Qt image cache during the first glide.
    def background_hero_cover_warm(self):
        queue = getattr(self, "_hero_warm_queue", None)
        if not queue:
            return

        widgets_now = sys.modules.get("helpers.widgets")
        busy = False
        if widgets_now is not None:
            try:
                busy = bool(widgets_now.atomic_scroll_busy())
            except Exception:
                busy = False
        if busy:
            module.QTimer.singleShot(120, self._warm_next_hero_cover)
            return

        entry = queue.pop(0)
        path = entry.get("cover_path")
        if path:
            def warm_one(p=str(path)):
                try:
                    module.images.warm(p, module.HERO_COVER_SIZE)
                except Exception:
                    pass
            try:
                module.lookup_pool.submit_cover(warm_one)
            except Exception:
                pass
        if queue:
            module.QTimer.singleShot(
                module.HERO_COVER_WARM_MS, self._warm_next_hero_cover)

    HomePage._warm_next_hero_cover = background_hero_cover_warm

    old_hero_holds = HomePage._hero_holds

    def scroll_holds_hero(self):
        widgets_now = sys.modules.get("helpers.widgets")
        if widgets_now is not None:
            try:
                if widgets_now.atomic_scroll_busy():
                    return True
            except Exception:
                pass
        return old_hero_holds(self)

    HomePage._hero_holds = scroll_holds_hero


class _Loader(importlib.abc.Loader):
    def __init__(self, wrapped):
        self._wrapped = wrapped

    def create_module(self, spec):
        creator = getattr(self._wrapped, "create_module", None)
        return creator(spec) if creator is not None else None

    def exec_module(self, module):
        self._wrapped.exec_module(module)
        _patch_home(module)


class _Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname != _TARGET:
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec is None or spec.loader is None:
            return spec
        spec.loader = _Loader(spec.loader)
        return spec


def install():
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True
    module = sys.modules.get(_TARGET)
    if module is not None:
        _patch_home(module)
        return
    sys.meta_path.insert(0, _Finder())
