"""Keep hero/banner work and page transitions out of wheel-scroll frames.

Two remaining stalls were measured in the real page code:

* PageSlide hides both live pages for the 220 ms navigation animation.  A wheel
  event delivered to that opaque snapshot had nowhere to scroll, so the first
  few notches after choosing Home/Discover could simply disappear.
* Home's hero prewarmer deliberately fires 120 ms after page creation, but it
  called thumbnail_or_avatar on the GUI thread.  The source's own profiling
  records 21-26 ms for a first hero-cover cut -- several frames at 165 Hz and
  five or six frames at 240 Hz.

The hero also has a 260 ms backdrop fade and a settled-size SmoothTransformation
re-cut.  Those are useful while idle but pure contention while the scroll is
already being shown from a frozen Qt Quick texture.  Defer them until momentum
stops instead of spending a 4.17 ms 240 Hz frame on hidden QWidget work.
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
    from PyQt6.QtWidgets import QApplication

    # ---- Wheel input during a page-slide ---------------------------------
    # The real incoming page is intentionally hidden while PageSlide paints
    # the two snapshots.  If a user starts scrolling before the 220 ms slide
    # has landed, finish the visual transition immediately and deliver that
    # same notch to the now-live widget under the pointer.  Sidebar slides use
    # axis="x" and are left alone.
    old_slide_wheel = getattr(w.PageSlide, "wheelEvent", None)

    def page_slide_wheel(self, event):
        if getattr(self, "_axis", "y") != "y":
            if old_slide_wheel is not None:
                return old_slide_wheel(self, event)
            event.ignore()
            return

        try:
            self.stop()  # on_done shows MainWindow._current_page synchronously
        except RuntimeError:
            event.ignore()
            return

        try:
            target = QApplication.widgetAt(event.globalPosition().toPoint())
        except Exception:
            target = None
        if target is not None and target is not self:
            try:
                event.setAccepted(False)
                QApplication.sendEvent(target, event)
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
                if w.momentum_active():
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
        if w.momentum_active():
            # Latest answer wins.  A quick-path backdrop followed by its sharp
            # original can arrive during one glide; there is no value in
            # painting the intermediate one underneath the Quick snapshot.
            self._atomic_pending_backdrop = (path, bool(fade))
            _pending_timer(self).start()
            return self._backdrop is not None
        return old_set_backdrop(self, path, fade=fade)

    def quiet_resmooth(self):
        if w.momentum_active():
            self._atomic_pending_resmooth = True
            _pending_timer(self).start()
            return
        return old_resmooth(self)

    def quiet_fade_tick(self, value):
        if w.momentum_active():
            # The visible page is already a Qt Quick snapshot while wheel
            # momentum runs.  Stop spending a high-rate SmoothTween repaint on
            # a hidden banner.  Land the fade before the QWidget is revealed.
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

    # The old method did thumbnail_or_avatar() here on the GUI thread.  The
    # images module already has a worker-safe warm() whose whole purpose is to
    # do that decode/cut away from Qt's event loop.  Keep the 120 ms spacing so
    # the cover pool is not flooded; each timer tick now only submits work.
    def background_hero_cover_warm(self):
        queue = getattr(self, "_hero_warm_queue", None)
        if not queue:
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
                # A failed prewarm is harmless: the normal draw path can still
                # decode it later.  Never move the fallback back to the UI.
                pass
        if queue:
            module.QTimer.singleShot(
                module.HERO_COVER_WARM_MS, self._warm_next_hero_cover)

    HomePage._warm_next_hero_cover = background_hero_cover_warm

    # Never rotate/rebuild the Home hero while a wheel glide is active.  The
    # timer remains armed and simply tries again on its next ordinary interval;
    # no animation or content semantics change while the page is idle.
    old_hero_holds = HomePage._hero_holds

    def scroll_holds_hero(self):
        widgets_now = sys.modules.get("helpers.widgets")
        if widgets_now is not None:
            try:
                if widgets_now.momentum_active():
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
