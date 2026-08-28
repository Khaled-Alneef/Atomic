"""Refine the shared native Flickable without touching its compositor core.

The first Flickable pass fixed the ownership problem (Qt Quick owns the visible
positions), but its input profile was intentionally too aggressive: with
9000 px/s^2 deceleration a normal 58px notch starts at roughly 1020 px/s and
finishes in ~113ms.  That reads as a kick/brake pair even when every presented
frame is perfect.

Keep the exact same travel and let the native Flickable timeline deliver it more
gently.  Also stop the optional QWidget scrollbar-shadow timer immediately after
each wheel kick.  The timer does not move content, but waking the GUI thread and
repainting the thumb every 32ms is needless contention on a 4.17ms (240Hz)
content budget.  The real scrollbar is committed once when the Flickable settles.
"""

from __future__ import annotations

_INSTALLED = False


def install():
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    try:
        from . import widgets
        overlay = getattr(widgets, "_atomic_render_thread_overlay", None)
        if overlay is None:
            return

        # Same stopping-distance arithmetic as the compositor, but with a much
        # flatter velocity envelope.  For a 58px notch this is ~609px/s for
        # ~190ms rather than ~1022px/s for ~113ms. Reader's 76px notch is about
        # ~697px/s for ~218ms. Travel is unchanged because v^2/(2a) is still the
        # requested notch distance.
        overlay.DECELERATION = 3200.0
        overlay.MAX_VELOCITY = 5000.0

        old_ensure = overlay._ensure_quick

        def ensure_quick(top):
            ok = old_ensure(top)
            if ok:
                try:
                    overlay.root.setProperty("flickDeceleration", 3200.0)
                    overlay.root.setProperty("maximumFlickVelocity", 5000.0)
                except (AttributeError, RuntimeError):
                    pass
            return ok

        overlay._ensure_quick = ensure_quick

        old_kick = overlay.kick

        def quiet_kick(area, body, ground, motion, distance, direction):
            handled = old_kick(area, body, ground, motion, distance, direction)
            if handled:
                # Content now lives entirely on Flickable's C++/scene-graph
                # timeline.  Do not wake Qt Widgets every 32ms merely to make
                # the thumb chase it; the final commit puts the thumb exactly
                # where the content lands.
                try:
                    overlay._thumb.stop()
                except RuntimeError:
                    pass
            return handled

        overlay.kick = quiet_kick
    except Exception:
        # Refinement must never make scrolling unavailable.  The compositor is
        # already installed underneath and remains valid if anything here is
        # unsupported by a particular Qt build.
        return
