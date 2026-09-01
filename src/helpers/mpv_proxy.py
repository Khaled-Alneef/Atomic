"""mpv in its own process, behind an object that looks like mpv.

**Why this exists, measured rather than argued.** An mpv core anywhere in
this process permanently wrecks Qt's timers - not the GPU, not painting,
not the clock:

    clean process          QTimer precise 250/s   coarse 250/s
    while mpv plays                       243/s          66/s
    after mpv is stopped                   64/s          64/s
    after mpv is torn down                 64/s          64/s   (26%)
    a plain Python thread   226/s -> 226/s                       (100%)

64.1/s is exactly 1000/15.625ms, the Windows default timer resolution, so
the obvious answer was that the resolution had fallen. It has not:
NtQueryTimerResolution reports 0.500ms at startup, while mpv plays, after
it is gone, and after re-claiming it - unchanged throughout. Nor is it
power throttling; opting out of that changed nothing either. The damage
is inside Qt's own event dispatcher and nothing in this process can undo
it.

The same measurement showed mpv in a *separate* process costs nothing at
all: 250 -> 250, 100% of baseline. So that is what this does.

**Nothing above it has to change.** `video_backend.create` returns one of
these instead of an `mpv.MPV`, and it forwards attributes, calls,
property observers and events over a local socket. windows/player.py goes
on calling `self.handle.loadfile(...)`, reading `self.handle.time_pos`
and registering callbacks exactly as before.

The child renders into the *parent's* HWND: mpv is handed `wid` and
parents its own window under it, which Windows allows across processes on
the same desktop. So the video still appears inside the player page.

ATOMIC_MPV_INPROC=1 puts the old in-process core back, without a rebuild.
"""

import json
import os
import socket
import struct
import subprocess
import sys
import threading

from . import logs

# The child is this same program, started with this flag. Frozen, that is
# Atomic.exe; from source it is python main.py - either way sys.executable
# and sys.argv[0] describe how to run it again.
HOST_FLAG = "--mpv-host"


def enabled() -> bool:
    return os.environ.get("ATOMIC_MPV_INPROC") != "1"


# ---------------------------------------------------------------- wire
# One JSON object per message, length-prefixed. Length-prefixed rather
# than newline-delimited because a subtitle path or a track title may
# carry anything at all, and a framing that depends on the payload is a
# framing that breaks on the first unusual file.

def _send(sock, obj):
    body = json.dumps(obj).encode("utf-8")
    sock.sendall(struct.pack("!I", len(body)) + body)


def _recv(sock):
    head = _read_exactly(sock, 4)
    if head is None:
        return None
    (size,) = struct.unpack("!I", head)
    body = _read_exactly(sock, size)
    if body is None:
        return None
    return json.loads(body.decode("utf-8"))


def _read_exactly(sock, count):
    chunks = []
    while count:
        try:
            chunk = sock.recv(count)
        except OSError:
            return None
        if not chunk:
            return None
        chunks.append(chunk)
        count -= len(chunk)
    return b"".join(chunks)


# -------------------------------------------------------------- parent
class RemoteMPV:
    """What the player holds. Looks like mpv, lives in another process."""

    def __init__(self, sock, process):
        self._sock = sock
        self._process = process
        self._lock = threading.Lock()
        self._next = 1
        self._waiting = {}
        self._observers = {}
        self._events = []
        self._alive = True
        self._pump = threading.Thread(target=self._listen, daemon=True,
                                      name="mpv-proxy")
        self._pump.start()

    # ---- the socket ------------------------------------------------
    def _listen(self):
        while self._alive:
            message = _recv(self._sock)
            if message is None:
                break
            kind = message.get("kind")
            if kind == "reply":
                event = self._waiting.pop(message.get("id"), None)
                if event is not None:
                    event[1] = message
                    event[0].set()
            elif kind == "property":
                for callback in self._observers.get(message.get("name"), []):
                    _safely(callback, message.get("name"),
                            message.get("value"))
            elif kind == "event":
                for callback in self._events:
                    _safely(callback, message.get("event"))
        self._alive = False

    def _call(self, op, *args, wait=True, **kwargs):
        if not self._alive:
            return None
        with self._lock:
            ident = self._next
            self._next += 1
            slot = [threading.Event(), None]
            if wait:
                self._waiting[ident] = slot
            try:
                _send(self._sock, {"id": ident, "op": op,
                                   "args": list(args), "kwargs": kwargs,
                                   "wait": bool(wait)})
            except OSError:
                self._waiting.pop(ident, None)
                self._alive = False
                return None
        if not wait:
            return None
        # Bounded: a child that dies mid-call must not hang the UI thread.
        if not slot[0].wait(8.0):
            self._waiting.pop(ident, None)
            return None
        reply = slot[1] or {}
        if reply.get("error"):
            raise RuntimeError(str(reply["error"])[:200])
        return reply.get("value")

    # ---- what player.py uses ---------------------------------------
    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        # A method if it is called, a property if it is read. mpv's own
        # object behaves the same way, so this cannot be decided here -
        # it is decided by what the caller does next.
        return _Member(self, name)

    def __setattr__(self, name, value):
        if name.startswith("_"):
            object.__setattr__(self, name, value)
            return
        self._call("set", name, value, wait=False)

    def observe_property(self, name, callback):
        self._observers.setdefault(name, []).append(callback)
        self._call("observe", name, wait=False)

    def register_event_callback(self, callback):
        self._events.append(callback)

    def command_async(self, *args):
        return self._call("command", *args, wait=False)

    def terminate(self):
        self._alive = False
        try:
            self._call("terminate", wait=False)
        except Exception:
            pass
        try:
            self._sock.close()
        except OSError:
            pass
        if self._process is not None:
            try:
                self._process.wait(timeout=4)
            except Exception:
                try:
                    self._process.kill()
                except Exception:
                    pass


class _Member:
    """One name on the remote mpv: read it, or call it."""

    __slots__ = ("_owner", "_name", "_value", "_read")

    def __init__(self, owner, name):
        object.__setattr__(self, "_owner", owner)
        object.__setattr__(self, "_name", name)
        object.__setattr__(self, "_read", False)
        object.__setattr__(self, "_value", None)

    def __call__(self, *args, **kwargs):
        return self._owner._call("call", self._name, *args, **kwargs)

    # Read as a value: mpv properties are plain attributes, and player.py
    # reads time_pos, duration, track_list and the rest that way.
    def _fetch(self):
        if not self._read:
            object.__setattr__(self, "_value",
                               self._owner._call("get", self._name))
            object.__setattr__(self, "_read", True)
        return self._value

    def __bool__(self):
        return bool(self._fetch())

    def __float__(self):
        return float(self._fetch() or 0.0)

    def __int__(self):
        return int(self._fetch() or 0)

    def __str__(self):
        return str(self._fetch())

    def __iter__(self):
        return iter(self._fetch() or [])

    def __len__(self):
        return len(self._fetch() or [])

    def __eq__(self, other):
        return self._fetch() == other

    def __getitem__(self, item):
        return (self._fetch() or [])[item]


def _safely(callback, *args):
    try:
        callback(*args)
    except Exception:
        logs.exception("An mpv callback raised")


# A child that has been started but not yet given a window. Frozen, the
# onefile bootloader unpacks ~200MB before any of its code runs and the
# host takes 3.03s to answer - measured on the built exe. Paid while the
# player is still finding a stream (3-7s of network) rather than after
# it, so nothing waits on it.
_warm = None
_warm_lock = threading.Lock()


def prewarm():
    """Start the video process now, so playing does not wait for it."""
    global _warm
    if not enabled():
        return
    with _warm_lock:
        if _warm is not None:
            return
        try:
            _warm = _spawn()
        except Exception as error:
            _warm = None
            logs.info(f"could not pre-start the video process: {error}")


def _spawn():
    """A running child and its socket, with no mpv core in it yet."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(30.0)
    port = listener.getsockname()[1]

    command = [sys.executable]
    if not getattr(sys, "frozen", False):
        # **main.py, never sys.argv[0].** From source, argv[0] is
        # whatever started this process - a test harness, say - and the
        # child would then re-run that instead of the video host, which
        # is a program starting copies of itself for ever. Frozen, the
        # executable is Atomic.exe and is already the right thing.
        import pathlib
        command.append(str(pathlib.Path(__file__).resolve().parent.parent
                           / "main.py"))
    command += [HOST_FLAG, str(port), "0"]

    creation = 0x08000000 if os.name == "nt" else 0      # no console window
    process = subprocess.Popen(command, creationflags=creation,
                               stdin=subprocess.DEVNULL)
    try:
        sock, _ = listener.accept()
    except socket.timeout:
        process.kill()
        raise RuntimeError("the video process did not start")
    finally:
        listener.close()
    return sock, process


def start(window_id, options):
    """Spawn the child and return a handle that behaves like mpv."""
    global _warm
    with _warm_lock:
        warm, _warm = _warm, None
    if warm is not None:
        sock, process = warm
        _send(sock, {"id": 0, "op": "open", "args": [int(window_id)],
                     "kwargs": options, "wait": True})
        reply = _recv(sock) or {}
        if not reply.get("error"):
            logs.info(f"mpv running in process {process.pid} (pre-started)")
            return RemoteMPV(sock, process)
        try:
            process.kill()
        except Exception:
            pass

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(20.0)
    port = listener.getsockname()[1]

    command = [sys.executable]
    if not getattr(sys, "frozen", False):
        # **main.py, never sys.argv[0].** From source, argv[0] is
        # whatever started this process - a test harness, say - and the
        # child would then re-run that instead of the video host, which
        # is a program starting copies of itself for ever. Frozen, the
        # executable is Atomic.exe and is already the right thing.
        import pathlib
        command.append(str(pathlib.Path(__file__).resolve().parent.parent
                           / "main.py"))
    command += [HOST_FLAG, str(port), str(int(window_id))]

    creation = 0x08000000 if os.name == "nt" else 0      # no console window
    process = subprocess.Popen(command, creationflags=creation,
                               stdin=subprocess.DEVNULL)
    try:
        sock, _ = listener.accept()
    except socket.timeout:
        process.kill()
        raise RuntimeError("the video process did not start")
    finally:
        listener.close()

    _send(sock, {"id": 0, "op": "open", "args": [], "kwargs": options,
                 "wait": True})
    reply = _recv(sock) or {}
    if reply.get("error"):
        process.kill()
        raise RuntimeError(str(reply["error"])[:200])
    logs.info(f"mpv running in process {process.pid}")
    return RemoteMPV(sock, process)


# --------------------------------------------------------------- child
def serve(port, window_id):
    """Run as the video process. Never returns until told to stop."""
    sock = socket.create_connection(("127.0.0.1", int(port)), timeout=20)
    handle = None

    def push(kind, **fields):
        try:
            _send(sock, dict(kind=kind, **fields))
        except OSError:
            pass

    while True:
        message = _recv(sock)
        if message is None:
            break
        op = message.get("op")
        ident = message.get("id")
        wants = message.get("wait")
        try:
            if op == "open":
                import mpv
                options = dict(message.get("kwargs") or {})
                # The window arrives with `open` when this child was
                # pre-started, and on the command line otherwise.
                args = message.get("args") or []
                target = int(args[0]) if args else int(window_id)
                handle = mpv.MPV(wid=str(target), **options)
                handle.register_event_callback(
                    lambda event: push("event", event=_plain(event)))
                value = True
            elif handle is None:
                value = None
            elif op == "call":
                name, *args = message.get("args") or []
                value = _plain(getattr(handle, name)(
                    *args, **(message.get("kwargs") or {})))
            elif op == "get":
                (name,) = message.get("args") or [None]
                value = _plain(getattr(handle, name, None))
            elif op == "set":
                name, new = message.get("args") or [None, None]
                setattr(handle, name, new)
                value = None
            elif op == "command":
                handle.command(*(message.get("args") or []))
                value = None
            elif op == "observe":
                (name,) = message.get("args") or [None]
                handle.observe_property(
                    name,
                    lambda n, v: push("property", name=n, value=_plain(v)))
                value = None
            elif op == "terminate":
                try:
                    handle.terminate()
                except Exception:
                    pass
                break
            else:
                value = None
            if wants:
                push("reply", id=ident, value=value)
        except Exception as error:
            if wants:
                push("reply", id=ident, error=str(error))
    try:
        sock.close()
    except OSError:
        pass


def _plain(value):
    """Whatever crosses the wire has to be JSON, and mpv returns its own
    node types for track lists and the like."""
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    try:
        return {str(k): _plain(v) for k, v in dict(value).items()}
    except Exception:
        return str(value)
