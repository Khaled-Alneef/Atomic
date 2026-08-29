"""Run libmpv in a dedicated process while keeping Atomic's existing UI/API.

Why this exists
---------------
The owner's repeated A/B tests ruled out DWM/MMCSS, callback pacing, hidden Qt
overlays, interpolation, cadence locking and ordinary decoder/drop counters.
The remaining architectural difference from standalone mpv is process
ownership: Atomic's libmpv lives inside the same Python/Qt process.

This patch keeps the exact same libmpv build, HWND embedding, player options,
source URLs and PlayerPage code, but moves libmpv itself into a spawned child
process.  The parent sees a small proxy with the python-mpv surface PlayerPage
already expects.  Commands/properties cross a multiprocessing Pipe; mpv's
decode/render/core threads never share the Atomic GUI process.

On Windows/PyInstaller this depends on multiprocessing.freeze_support() being
called before the GUI imports. helpers/__init__.py does that as its first real
action. If process startup fails for any reason, create() falls back to the
existing in-process backend rather than making the player unavailable.
"""

from __future__ import annotations

import itertools
import multiprocessing as _mp
import os
import threading
from types import SimpleNamespace

_INSTALLED = False
_READY_TIMEOUT_S = 8.0
_REQUEST_TIMEOUT_S = 2.0
_SHUTDOWN_GRACE_S = 0.35


def _event_name(event) -> str:
    text = str(getattr(event, "event_id", "") or "")
    lowered = text.lower()
    for known in ("file-loaded", "end-file", "shutdown", "start-file",
                  "video-reconfig", "audio-reconfig", "seek", "playback-restart"):
        if known in lowered:
            return known
    return lowered.strip("<> ")


def _worker_main(conn, window_id: int, options: dict):
    """Child-process body. Nothing Qt-related is imported here."""
    send_lock = threading.Lock()

    def send(message):
        try:
            with send_lock:
                conn.send(message)
            return True
        except (BrokenPipeError, EOFError, OSError):
            return False

    try:
        from helpers import video_backend
        video_backend._load()
        mpv = video_backend._mpv
        if mpv is None:
            send(("fatal", video_backend._load_error or "no video engine"))
            return
        handle = mpv.MPV(wid=str(int(window_id)), **dict(options))
    except Exception as error:
        send(("fatal", f"isolated mpv could not start: {error}"))
        return

    observed = []

    def on_event(event):
        name = _event_name(event)
        if name in {"file-loaded", "end-file", "shutdown", "start-file",
                    "video-reconfig", "audio-reconfig", "seek",
                    "playback-restart"}:
            send(("event", name))

    try:
        handle.register_event_callback(on_event)
    except Exception:
        pass

    if not send(("ready",)):
        try:
            handle.terminate()
        except Exception:
            pass
        return

    def reply(request_id, ok=True, value=None):
        if request_id is not None:
            send(("reply", request_id, bool(ok), value))

    try:
        while True:
            try:
                message = conn.recv()
            except (EOFError, OSError):
                break
            if not message:
                continue
            op = message[0]

            if op == "terminate":
                break

            if op == "observe":
                _, observer_id, name = message

                def callback(prop, value, oid=observer_id):
                    send(("property", oid, prop, value))

                observed.append(callback)
                try:
                    handle.observe_property(str(name), callback)
                except Exception as error:
                    send(("observe-error", observer_id, str(error)))
                continue

            if op == "get":
                _, request_id, name = message
                try:
                    value = getattr(handle, str(name).replace("-", "_"))
                    reply(request_id, True, value)
                except Exception as error:
                    reply(request_id, False, str(error))
                continue

            if op == "set":
                _, request_id, name, value = message
                try:
                    handle[str(name)] = value
                    reply(request_id, True, None)
                except Exception as error:
                    reply(request_id, False, str(error))
                continue

            if op == "play":
                _, request_id, url = message
                try:
                    handle.play(url)
                    reply(request_id, True, None)
                except Exception as error:
                    reply(request_id, False, str(error))
                continue

            if op == "loadfile":
                _, request_id, url, mode, kwargs = message
                try:
                    handle.loadfile(url, mode, **dict(kwargs or {}))
                    reply(request_id, True, None)
                except Exception as error:
                    reply(request_id, False, str(error))
                continue

            if op == "seek":
                _, request_id, seconds, reference, precision = message
                try:
                    handle.seek(float(seconds), reference=reference,
                                precision=precision)
                    reply(request_id, True, None)
                except Exception as error:
                    reply(request_id, False, str(error))
                continue

            if op == "command":
                _, request_id, args = message
                try:
                    value = handle.command(*tuple(args))
                    reply(request_id, True, value)
                except Exception as error:
                    reply(request_id, False, str(error))
                continue

            if op == "command-noreply":
                _, args = message
                try:
                    handle.command(*tuple(args))
                except Exception:
                    pass
                continue

            if op == "sub-add":
                _, request_id, path, select = message
                try:
                    value = handle.sub_add(path, select)
                    reply(request_id, True, value)
                except Exception as error:
                    reply(request_id, False, str(error))
                continue

            if op == "ping":
                reply(message[1], True, "pong")
                continue

            request_id = message[1] if len(message) > 1 else None
            reply(request_id, False, f"unknown isolated-mpv operation: {op}")
    finally:
        try:
            handle.terminate()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass


class _RemoteEvent:
    def __init__(self, name):
        self.event_id = name


class ProcessMPV:
    """python-mpv-compatible facade backed by a dedicated child process."""

    def __init__(self, window_id: int, options: dict):
        ctx = _mp.get_context("spawn")
        parent_conn, child_conn = ctx.Pipe(duplex=True)
        self._conn = parent_conn
        self._send_lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self._pending = {}
        self._request_ids = itertools.count(1)
        self._observer_ids = itertools.count(1)
        self._observers = {}
        self._event_callbacks = []
        self._ready = threading.Event()
        self._startup_error = None
        self._closed = False

        self._process = ctx.Process(
            target=_worker_main,
            args=(child_conn, int(window_id), dict(options)),
            name="Atomic-mpv",
            daemon=True,
        )
        self._process.start()
        try:
            child_conn.close()
        except Exception:
            pass

        self._reader_thread = threading.Thread(
            target=self._reader,
            name="Atomic-mpv-bridge",
            daemon=True,
        )
        self._reader_thread.start()

        if not self._ready.wait(_READY_TIMEOUT_S):
            self.terminate()
            raise RuntimeError("isolated mpv did not become ready")
        if self._startup_error:
            self.terminate()
            raise RuntimeError(self._startup_error)
        if self._request(("ping",), timeout=1.5) != "pong":
            self.terminate()
            raise RuntimeError("isolated mpv command channel did not answer")

    def _reader(self):
        try:
            while True:
                try:
                    message = self._conn.recv()
                except (EOFError, OSError):
                    break
                if not message:
                    continue
                kind = message[0]
                if kind == "ready":
                    self._ready.set()
                    continue
                if kind == "fatal":
                    self._startup_error = str(message[1])
                    self._ready.set()
                    continue
                if kind == "reply":
                    _, request_id, ok, value = message
                    with self._pending_lock:
                        slot = self._pending.get(request_id)
                    if slot is not None:
                        event, box = slot
                        box[:] = [bool(ok), value]
                        event.set()
                    continue
                if kind == "property":
                    _, observer_id, name, value = message
                    callback = self._observers.get(observer_id)
                    if callback is not None:
                        try:
                            callback(name, value)
                        except Exception:
                            pass
                    continue
                if kind == "event":
                    event = _RemoteEvent(message[1])
                    for callback in tuple(self._event_callbacks):
                        try:
                            callback(event)
                        except Exception:
                            pass
                    continue
        finally:
            self._ready.set()
            with self._pending_lock:
                slots = list(self._pending.values())
            for event, box in slots:
                if not box:
                    box[:] = [False, "isolated mpv process closed"]
                event.set()

    def _send(self, message):
        if self._closed:
            raise RuntimeError("isolated mpv is closed")
        with self._send_lock:
            self._conn.send(message)

    def _request(self, prefix, timeout=_REQUEST_TIMEOUT_S):
        request_id = next(self._request_ids)
        event = threading.Event()
        box = []
        with self._pending_lock:
            self._pending[request_id] = (event, box)
        try:
            message = (prefix[0], request_id, *prefix[1:])
            self._send(message)
            if not event.wait(timeout):
                raise TimeoutError(f"isolated mpv timed out on {prefix[0]}")
            ok, value = box
            if not ok:
                raise RuntimeError(str(value))
            return value
        finally:
            with self._pending_lock:
                self._pending.pop(request_id, None)

    def observe_property(self, name, callback):
        observer_id = next(self._observer_ids)
        self._observers[observer_id] = callback
        self._send(("observe", observer_id, str(name)))
        return callback

    def register_event_callback(self, callback):
        self._event_callbacks.append(callback)
        return callback

    def __getitem__(self, name):
        return self._request(("get", str(name)))

    def __setitem__(self, name, value):
        self._request(("set", str(name), value))

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return self._request(("get", str(name).replace("_", "-")))

    def play(self, url):
        return self._request(("play", str(url)))

    def loadfile(self, url, mode="replace", **kwargs):
        return self._request(("loadfile", str(url), str(mode), dict(kwargs)))

    def seek(self, seconds, reference="relative", precision="default-precise"):
        return self._request(("seek", float(seconds), str(reference),
                              str(precision)))

    def command(self, *args):
        return self._request(("command", tuple(args)))

    def command_async(self, *args):
        self._send(("command-noreply", tuple(args)))
        return None

    def sub_add(self, path, select="select"):
        return self._request(("sub-add", str(path), select))

    def terminate(self):
        if self._closed:
            return
        self._closed = True
        try:
            with self._send_lock:
                self._conn.send(("terminate",))
        except Exception:
            pass

        process = getattr(self, "_process", None)
        if process is not None:
            try:
                process.join(_SHUTDOWN_GRACE_S)
            except Exception:
                pass
            if process.is_alive():
                try:
                    process.terminate()
                except Exception:
                    pass
                try:
                    process.join(_SHUTDOWN_GRACE_S)
                except Exception:
                    pass
            if process.is_alive() and hasattr(process, "kill"):
                try:
                    process.kill()
                except Exception:
                    pass
        try:
            self._conn.close()
        except Exception:
            pass


def install():
    global _INSTALLED
    if _INSTALLED or os.name != "nt":
        return
    _INSTALLED = True

    from . import video_backend

    original_create = video_backend.create
    original_shutdown = video_backend.shutdown

    def create(window_id: int, **overrides):
        options = video_backend.default_options()
        options.update(overrides)
        try:
            return ProcessMPV(window_id, options)
        except Exception as error:
            try:
                from . import logs
                logs.warning(
                    f"isolated mpv unavailable; using in-process fallback: {error}")
            except Exception:
                pass
            return original_create(window_id, **overrides)

    def shutdown(handle):
        if isinstance(handle, ProcessMPV):
            try:
                handle.terminate()
            except Exception:
                pass
            return
        return original_shutdown(handle)

    video_backend.create = create
    video_backend.shutdown = shutdown
