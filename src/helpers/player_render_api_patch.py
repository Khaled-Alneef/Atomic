"""Opt-in libmpv Render API presentation for the Windows player.

Atomic's normal player currently gives mpv a QWidget HWND (`wid=`). That makes
mpv own a separate native child window and swapchain inside Qt's top-level
window, leaving DWM to reconcile two presentation systems. mpv's Render API is
the architectural alternative: Qt owns the OpenGL surface/FBO and libmpv
renders the decoded frame into that surface.

This path is deliberately gated by ATOMIC_MPV_RENDER_API=1 for now. Atomic's
currently pinned Stremio libmpv is a large LuaJIT-enabled Windows build, and
python-mpv issue #305 documents a non-recoverable Windows SEH crash when
MpvRenderContext is used with LuaJIT-enabled libmpv. The code is therefore ready
for a no-Lua libmpv A/B without making today's stable wid path randomly crash.

When enabled this patch changes presentation only. PlayerPage still receives the
same python-mpv MPV object, so source selection, buffering, seeking, tracks,
subtitles, progress observation and all controls continue through the existing
code untouched.
"""

from __future__ import annotations

import os
import sys
import weakref

_INSTALLED = False
_PATCHED = set()


def enabled() -> bool:
    return os.name == "nt" and os.environ.get("ATOMIC_MPV_RENDER_API") == "1"


def _patch(module):
    key = ("render-api", id(module))
    if key in _PATCHED or not enabled():
        return
    _PATCHED.add(key)

    from PyQt6.QtCore import Qt
    from PyQt6.QtCore import pyqtSignal as Signal
    from PyQt6.QtGui import QColor, QOpenGLContext, QPainter
    from PyQt6.QtOpenGLWidgets import QOpenGLWidget
    from PyQt6.QtWidgets import QSizePolicy

    backend = module.video_backend
    original_create = backend.create
    original_shutdown = backend.shutdown
    render_surfaces = weakref.WeakValueDictionary()

    class RenderVideoSurface(QOpenGLWidget):
        """Qt-owned FBO into which libmpv's OpenGL Render API draws."""

        render_ready = Signal()
        drag_band = 0

        def __init__(self, parent=None):
            super().__init__(parent)
            self._render_context = None
            self._mpv_handle = None
            self._get_proc_cb = None
            self._background = QColor(module.theme.BG)
            self.setSizePolicy(QSizePolicy.Policy.Expanding,
                               QSizePolicy.Policy.Expanding)
            self.setAutoFillBackground(False)
            self.render_ready.connect(
                self._consume_mpv_update,
                Qt.ConnectionType.QueuedConnection)

        def native_handle(self):
            """Compatibility with PlayerPage._start's existing call site.

            The old VideoSurface returned an HWND here. The patched backend
            recognises this object instead and creates vo=libmpv + a render
            context, so PlayerPage itself does not need a second startup path.
            """
            return self

        def mousePressEvent(self, event):
            if (self.drag_band > 0
                    and event.position().y() <= self.drag_band
                    and module.window_chrome.begin_window_drag(self, event)):
                return
            super().mousePressEvent(event)

        def initializeGL(self):
            pass

        def paintGL(self):
            ctx = self._render_context
            if ctx is None:
                painter = QPainter(self)
                painter.fillRect(self.rect(), self._background)
                painter.end()
                return

            ratio = float(self.devicePixelRatioF() or 1.0)
            width = max(1, int(round(self.width() * ratio)))
            height = max(1, int(round(self.height() * ratio)))
            try:
                ctx.render(
                    flip_y=True,
                    opengl_fbo={
                        "fbo": int(self.defaultFramebufferObject()),
                        "w": width,
                        "h": height,
                    },
                )
            except Exception:
                # Exceptions escaping a Qt paint callback can terminate a PyQt
                # process. Log and leave the last valid frame in place instead.
                try:
                    module.logs.exception("libmpv Render API paint failed")
                except Exception:
                    pass

        def resizeGL(self, width, height):
            if self._render_context is not None:
                self.update()

        def _proc_address(self, _ctx, name):
            try:
                gl = QOpenGLContext.currentContext()
                if gl is None:
                    return 0
                address = gl.getProcAddress(name)
                return int(address) if address else 0
            except Exception:
                return 0

        def attach(self, handle):
            if self._render_context is not None:
                return
            mpv = backend._mpv
            if mpv is None:
                raise backend.PlayerError("python-mpv is not loaded")
            if not hasattr(mpv, "MpvRenderContext"):
                raise backend.PlayerError(
                    "This python-mpv build does not expose MpvRenderContext")
            if not hasattr(mpv, "MpvGlGetProcAddressFn"):
                raise backend.PlayerError(
                    "This python-mpv build does not expose MpvGlGetProcAddressFn")

            # A render context must be created while the same OpenGL context
            # that will render frames is current. QOpenGLWidget owns that
            # context and renders into a private FBO, never framebuffer 0.
            self.makeCurrent()
            try:
                if self.context() is None or not self.isValid():
                    raise backend.PlayerError(
                        "Qt could not initialise the video OpenGL surface")
                self._get_proc_cb = mpv.MpvGlGetProcAddressFn(self._proc_address)
                self._render_context = mpv.MpvRenderContext(
                    handle,
                    "opengl",
                    opengl_init_params={"get_proc_address": self._get_proc_cb},
                )
            finally:
                self.doneCurrent()

            self._mpv_handle = handle

            # mpv invokes this from an arbitrary internal thread. Emitting a Qt
            # signal is the only work done there; update() and every render API
            # call happen back on the GUI/render thread.
            def wake():
                try:
                    self.render_ready.emit()
                except RuntimeError:
                    pass

            self._render_context.update_cb = wake
            render_surfaces[id(handle)] = self
            self.update()

        def _consume_mpv_update(self):
            ctx = self._render_context
            if ctx is None:
                return
            try:
                if ctx.update():
                    self.update()
            except Exception:
                try:
                    module.logs.exception("libmpv Render API update failed")
                except Exception:
                    pass

        def detach(self):
            ctx = self._render_context
            self._render_context = None
            handle = self._mpv_handle
            self._mpv_handle = None
            if handle is not None:
                render_surfaces.pop(id(handle), None)
            if ctx is None:
                self._get_proc_cb = None
                return
            try:
                ctx.update_cb = None
            except Exception:
                pass
            try:
                self.makeCurrent()
                try:
                    ctx.free()
                finally:
                    self.doneCurrent()
            except Exception:
                try:
                    module.logs.exception("Could not free libmpv render context")
                except Exception:
                    pass
            self._get_proc_cb = None

    # Replace only the class looked up when PlayerPage creates its surface.
    # PlayerPage's existing startup method, geometry code and control code stay
    # byte-for-byte unchanged.
    module.VideoSurface = RenderVideoSurface

    def create(target, **overrides):
        if not isinstance(target, RenderVideoSurface):
            return original_create(target, **overrides)

        backend._load()
        if backend._mpv is None:
            raise backend.PlayerError(backend._load_error or "no video engine")

        options = backend.default_options()
        options.update(overrides)

        # With the Render API Qt owns presentation, so mpv must not create a
        # D3D11/Win32 swapchain. `gpu_context=d3d11` and the swapchain colour
        # hint only describe the wid path and are removed here.
        options.pop("gpu_context", None)
        options.pop("target_colorspace_hint", None)
        # A no-Lua libmpv has no OSC option at all. Atomic draws its own OSC,
        # so omitting the option is correct for both Lua and no-Lua builds.
        options.pop("osc", None)
        options["vo"] = "libmpv"
        # First implementation favours compatibility over zero-copy. Direct
        # D3D11VA -> caller-owned OpenGL interop varies by driver; copying one
        # decoded frame is a controlled cost and removes that variable from the
        # presentation A/B. If the Render API fixes cadence, zero-copy can be
        # measured separately afterwards.
        if sys.platform == "win32" and "hwdec" not in overrides:
            options["hwdec"] = "auto-copy-safe"

        handle = None
        try:
            handle = backend._mpv.MPV(**options)
            target.attach(handle)
            return handle
        except Exception as error:
            if handle is not None:
                try:
                    handle.terminate()
                except Exception:
                    pass
            if isinstance(error, backend.PlayerError):
                raise
            raise backend.PlayerError(str(error)) from error

    def shutdown(handle):
        if handle is not None:
            surface = render_surfaces.get(id(handle))
            if surface is not None:
                try:
                    surface.detach()
                except Exception:
                    pass
        return original_shutdown(handle)

    backend.create = create
    backend.shutdown = shutdown


def install():
    """Chain after Atomic's shared player patch rather than competing for import.

    requested_fixes_patch owns the one lazy `windows.player` loader used by the
    current development stack, and later regression patches intentionally chain
    around its `_patch_player` function. Joining that chain means every existing
    player fix still runs and the Render API surface is applied at the same
    deterministic patch boundary. A second independent meta-path finder would
    race/shadow that chain and silently lose fixes depending on finder order.
    """
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True
    if not enabled():
        return

    from . import requested_fixes_patch as requested

    previous = requested._patch_player

    def player(module):
        previous(module)
        _patch(module)

    requested._patch_player = player

    loaded = sys.modules.get("windows.player")
    if loaded is not None:
        _patch(loaded)
