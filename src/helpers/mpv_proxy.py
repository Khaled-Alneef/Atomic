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


# The one event this module makes up itself: pushed to every registered
# event callback when the child goes away without being asked to, so the
# player can say so (or reload) instead of holding a handle that answers
# None to everything. A dict rather than an mpv event object - it never
# came from mpv - and named so nothing mpv emits can be mistaken for it.
HOST_LOST = "atomic-host-lost"


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
class _Missing:
    """"nothing has been pushed for this property yet" - distinct from
    a real None, which several mpv properties genuinely hold."""

    def __repr__(self):
        return "<no value yet>"


_MISSING = _Missing()


class RemoteMPV:
    """What the player holds. Looks like mpv, lives in another process."""

    def __init__(self, sock, process):
        self._sock = sock
        self._process = process
        self._lock = threading.Lock()
        self._next = 1
        self._waiting = {}
        self._observers = {}
        # **The last value mpv pushed for each observed property.**
        #
        # This is what makes the separate process worth having. A
        # property get is a round trip, and the child answers it from
        # mpv's own play loop - the same loop that blocks while a stream
        # read waits on the swarm (torrent_engine._Torrent.wait_for
        # carries that measurement: up to 45s). So every `get` the UI
        # thread made could stall for the proxy's whole 8s timeout, and
        # the player polls properties on a timer. The core froze the
        # interface again, over a socket this time, which is exactly the
        # thing moving mpv out of process was for - the owner's "the
        # video player freeze after playing for sometime".
        #
        # Every property the player reads on a timer is already
        # observed (windows/player: time-pos, duration, pause,
        # demuxer-cache-time, track-list, volume, mute, speed,
        # core-idle, paused-for-cache, cache-buffering-state, path), so
        # mpv pushes each one as it changes and a read costs nothing.
        # Anything not observed still round-trips.
        self._values = {}
        self._events = []
        self._alive = True
        # True once terminate() has been asked for, so the listener can
        # tell "the parent closed the socket" from "the child went away
        # on its own" - only the second is a loss worth announcing.
        self._stopping = False
        self._pump = threading.Thread(target=self._listen, daemon=True,
                                      name="mpv-proxy")
        self._pump.start()

    @property
    def alive(self):
        """False once the child is known to be gone. A real class
        attribute, so it is answered here and never round-tripped
        through __getattr__ as an mpv property named `alive`."""
        return self._alive

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
                name = message.get("name")
                self._values[name] = message.get("value")
                for callback in self._observers.get(name, []):
                    _safely(callback, name, message.get("value"))
            elif kind == "event":
                for callback in self._events:
                    _safely(callback, message.get("event"))
        self._alive = False
        # **Every caller still waiting for a reply is released now, not
        # when its 8s runs out.** The wait in _call is bounded, but a
        # bound is not a wake-up: a loadfile sent in the last moment
        # before the child went sat on the UI thread for the whole 8s -
        # the owner's "sometimes when I refresh the vid player it
        # freezes then refresh", 2 September 2026. Measured on the
        # harness after this: loadfile against a killed child returns in
        # 0.000s.
        for ident in list(self._waiting):
            slot = self._waiting.pop(ident, None)
            if slot is not None:
                slot[0].set()
        # Only an *unasked-for* loss is an event. The player closes the
        # socket itself on the way out (terminate), and announcing that
        # back to a page that is already leaving would make it react to
        # its own Back press.
        if not self._stopping:
            for callback in self._events:
                _safely(callback, {"event": HOST_LOST})

    @staticmethod
    def _mpv_name(name):
        """mpv spells its properties with dashes; python-mpv accepts
        underscores and translates. The cache is keyed the way
        observe_property was called, so reads have to agree."""
        return str(name).replace("_", "-")

    def _cached(self, name):
        """The last pushed value for `name`, or _MISSING."""
        return self._values.get(self._mpv_name(name), _MISSING)

    def _call(self, op, *args, wait=True, **kwargs):
        # A read of an observed property never goes near the socket.
        if op == "get" and args:
            value = self._cached(args[0])
            if value is not _MISSING:
                return value
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
        # The listener releases every waiting slot when the socket goes
        # (see _listen) - but a slot registered *after* that sweep, in
        # the gap between the `_alive` check above and the send, would
        # sit out the whole bound. Checked again here, after the send:
        # once `_alive` is False the sweep has either taken this slot
        # or will never see it, and either way there is no reply coming.
        # Closed 3 September 2026 by reading, not by catching it: the
        # window is the few microseconds between the sweep and this
        # send, and the harness kill (the child killed 3s into a play,
        # loadfile at once) never landed in it - 0.000s each run.
        if not self._alive:
            self._waiting.pop(ident, None)
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
        self._remember(name, value)
        self._call("set", name, value, wait=False)

    def _remember(self, name, value):
        """Write a set value straight into the cache.

        Without this a set-then-read reads the value from before the
        set, because mpv's own push has not arrived yet - and the player
        does exactly that when it toggles pause. mpv confirms (or
        corrects) it a moment later through the observer."""
        key = self._mpv_name(name)
        if key in self._values:
            self._values[key] = value

    # **Properties by subscript, which is how player.py sets most of
    # them.** python-mpv's own object supports both `player.pause = x`
    # and `player["pause"] = x`, and this proxy had only the attribute
    # form - so every one of the 22 `self.handle[...] = ...` lines in
    # windows/player.py raised TypeError.
    #
    # One of those is `self.handle["pause"] = False`, in the same try
    # block as loadfile, whose except shows "That source could not be
    # opened. Try another one." So playback failed on every release from
    # every source, and the message named the release - the owner's
    # "still no sources can play to any watchable". Measured either side:
    # find_streams returns 103 sources for Attack on Titan and the
    # prepared URL serves HTTP 206, so nothing below this was ever wrong.
    def __setitem__(self, name, value):
        self._remember(name, value)
        self._call("set", name, value, wait=False)

    def __getitem__(self, name):
        return self._call("get", name)

    # **A stream protocol is registered by name, never by handing over
    # the function.** python-mpv takes a Python callable here, and a
    # callable cannot cross a JSON pipe: every play logged
    #
    #   could not register the seekable engine stream
    #   TypeError: Object of type function is not JSON serializable
    #
    # from player_seekable_stream_patch.attach - which swallows it, and
    # then rewrites the URL to atomic:// anyway. So mpv was handed a
    # scheme its own process had never heard of, on every source, from
    # every episode. That is the owner's "it does not play videos, gets
    # stuck in sourcing": the sources were found (47-103 rows in the
    # same logs) and not one of them could be opened.
    #
    # The child is this same program (--mpv-host), so it holds the same
    # module and registers its own copy of the opener - see serve()'s
    # "protocol" op. The openers only speak HTTP to 127.0.0.1 and to
    # debrid hosts, both of which a second process reaches exactly as
    # this one does, so nothing about them depends on being here.
    def register_stream_protocol(self, name, _opener=None):
        # Anything but the child's own True is a failure, and has to be
        # raised rather than returned: _call answers None for a timeout
        # as well as for a dead socket, and attach() must be able to
        # tell "registered" from "did not answer" - swallowing that
        # difference is the whole shape of the bug this replaced.
        if self._call("protocol", name) is not True:
            raise RuntimeError(f"the video process did not register {name}://")
        return True

    def observe_property(self, name, callback):
        self._observers.setdefault(name, []).append(callback)
        # Present but unknown, so _cached can tell "nothing has been
        # pushed yet" (round-trip) from "observed and currently None"
        # (answer None, which is what mpv would say).
        self._values.setdefault(name, _MISSING)
        self._call("observe", name, wait=False)

    def register_event_callback(self, callback):
        self._events.append(callback)

    def command_async(self, *args):
        return self._call("command", *args, wait=False)

    def terminate(self):
        self._stopping = True
        self._alive = False
        # No "terminate" op is sent: _alive is already False, so _call
        # would drop it, and the child leaves serve() on the socket close
        # below - which was always the mechanism (the child exits rc=0
        # 0.09s after this, measured 3 September 2026). The op and the
        # child's branch for it were removed as unreachable.
        try:
            self._sock.close()
        except OSError:
            pass
        # **Reaped on a thread, never on the caller's.** This waited up
        # to four seconds here, and `here` is the UI thread: leaving the
        # player calls terminate, so pressing Back froze the window for
        # exactly as long as the child took to notice - the owner, 2
        # September 2026, "sometimes when I hit the back button in the
        # vid player, it takes ~4 sec to leave". A child that has just
        # been asked to stop while it is inside a stream read is
        # precisely the case that takes the whole four.
        #
        # Nothing needs the answer. The socket is already closed above,
        # which is what tells the child to go; this only makes sure it
        # does, and kills it if it will not.
        process = self._process
        if process is not None:
            def reap():
                try:
                    process.wait(timeout=4)
                except Exception:
                    try:
                        process.kill()
                    except Exception:
                        pass

            threading.Thread(target=reap, daemon=True,
                             name="mpv-reap").start()


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
# Set when a prewarm has finished (with a child or without), so start()
# can join one that is still in flight instead of spawning a second
# child beside it. Measured 3 September 2026: from source a spawn takes
# 0.27s to connect and the frozen build 3.03s; a start() that found
# _warming True used to pay that again on the UI thread while the warm
# child arrived a moment later and sat idle.
_warm_done = threading.Event()
WARM_JOIN_S = 15.0


_warming = False


def prewarm():
    """Start the video process now, so playing does not wait for it.

    **Returns at once, and that is the whole of it.** `_spawn` blocks
    until the child connects - 3.03s for the frozen build, which is the
    cost this function exists to pay early - and the caller is the
    player's own start path, on the UI thread. Waiting there did not
    remove the delay, it moved it to the front of the episode: the
    owner, 2 September 2026, "the player now does not start the video
    immediately on buffering".

    So the spawn goes on a thread and `start()` simply finds a warm
    socket or does not. Missing it costs what it always cost; finding it
    saves the 3.03s, and either way nothing waits here.
    """
    global _warming
    if not enabled():
        return
    with _warm_lock:
        if _warm is not None or _warming:
            return              # one already up, or one already coming
        _warming = True
        _warm_done.clear()

    def spawn():
        global _warm, _warming
        try:
            made = _spawn()
        except Exception as error:
            made = None
            logs.info(f"could not pre-start the video process: {error}")
        with _warm_lock:
            _warm = made
            _warming = False
        _warm_done.set()

    threading.Thread(target=spawn, daemon=True, name="mpv-prewarm").start()


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


# How long the parent gives the child to answer a handshake. `ping` is
# answered from the child's read loop with no mpv in the way, so 1s is
# generous; `open` builds the mpv core (measured 0.3-0.4s from source).
PING_TIMEOUT = 1.0
OPEN_TIMEOUT = 20.0


def _discard(sock, process):
    """Throw away a child that is dead or not answering."""
    try:
        sock.close()
    except OSError:
        pass
    try:
        process.kill()
    except Exception:
        pass


def _answers(sock, process):
    """Whether a pre-started child is still there to be used.

    **Checked before it is trusted, because a warm child used to die
    quietly.** prewarm() runs minutes before the next play, and until 2
    September 2026 the child's socket carried a 20s recv timeout that
    ended the process (see serve). start() then took the corpse: its
    `open` either raised WinError 10053 - logged as "the video process
    failed ... falling back to an in-process core", which is the core
    that wrecks Qt's timers for the rest of the session - or got no
    reply at all and returned a handle that answered None to everything
    ("the video process did not register atomic://"). Both are in the
    owner's log for that day. The timeout is gone, but a child can still
    be killed, crash, or be reaped by the OS, so this is what decides
    rather than the fact of it having been spawned."""
    if process.poll() is not None:
        return False
    try:
        sock.settimeout(PING_TIMEOUT)
        _send(sock, {"id": 0, "op": "ping", "args": [], "kwargs": {},
                     "wait": True})
        return _recv(sock) is not None
    except OSError:
        return False
    finally:
        try:
            sock.settimeout(None)
        except OSError:
            pass


def _open(sock, process, window_id, options):
    """Hand the child its window and options; the handle, or raise."""
    try:
        sock.settimeout(OPEN_TIMEOUT)
        _send(sock, {"id": 0, "op": "open", "args": [int(window_id)],
                     "kwargs": options, "wait": True})
        reply = _recv(sock)
    except OSError as error:
        _discard(sock, process)
        raise RuntimeError(f"the video process went away: {error}")
    finally:
        try:
            sock.settimeout(None)
        except OSError:
            pass
    # None is "no reply", and used to be read as success (`or {}`).
    if reply is None:
        _discard(sock, process)
        raise RuntimeError("the video process did not answer open")
    if reply.get("error"):
        _discard(sock, process)
        raise RuntimeError(str(reply["error"])[:200])
    return RemoteMPV(sock, process)


def start(window_id, options):
    """A handle that behaves like mpv, in a child that is known to be
    alive: the pre-started one if it still answers, else a fresh one.

    Raises only when a *fresh* child cannot be started - a dead warm
    child is discarded here, never reported, so video_backend.create's
    in-process fallback is reached only when there is genuinely no way
    to run the child."""
    global _warm
    with _warm_lock:
        warm, _warm = _warm, None
        warming = _warming
    if warm is None and warming:
        # A prewarm is still connecting - wait for that child rather than
        # start a twin (see _warm_done). Bounded: the spawn's own accept
        # gives up at 30s, and a prewarm that fails clears the flag.
        _warm_done.wait(WARM_JOIN_S)
        with _warm_lock:
            warm, _warm = _warm, None
    if warm is not None:
        sock, process = warm
        if _answers(sock, process):
            try:
                handle = _open(sock, process, window_id, options)
                logs.info(f"mpv running in process {process.pid} "
                          f"(pre-started)")
                return handle
            except RuntimeError as error:
                logs.info(f"the pre-started video process {process.pid} "
                          f"could not open ({error}); starting another")
        else:
            logs.info(f"the pre-started video process {process.pid} was "
                      f"gone (exit {process.poll()}); starting another")
            _discard(sock, process)

    sock, process = _spawn()
    handle = _open(sock, process, window_id, options)
    logs.info(f"mpv running in process {process.pid}")
    return handle


# --------------------------------------------------------------- child
def serve(port, window_id):
    """Run as the video process. Never returns until told to stop."""
    sock = socket.create_connection(("127.0.0.1", int(port)), timeout=20)
    # **Blocking from here on - the connect timeout must not outlive the
    # connect.** create_connection leaves its timeout on the socket, so
    # every recv below raised TimeoutError after 20s of silence,
    # _read_exactly read that as "peer gone", and this process exited
    # cleanly (rc=0) with nothing having been said. Measured 2 September
    # 2026 on a hidden window: time-pos advanced to 19.68s and stopped,
    # child exited at t=20.5s. The parent says nothing for long
    # stretches by design - observed properties are served from
    # RemoteMPV._values, sets happen in the first seconds - so this was
    # the picture freezing at ~20s ("the video player got stuck (freeze)
    # with me when I played Reacher at ~28 sec"), a pre-started child
    # dead before the next play, and a reload waiting on a reply that
    # was never coming. The parent closing the socket is the stop
    # signal, and that arrives on a blocking recv as EOF.
    sock.settimeout(None)
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
                # **The child finds libmpv on its own.** It used to rely
                # on the PATH it inherited, which carries the vendor dir
                # only once the *parent* has run video_backend._load() -
                # so a child pre-started before the first play (from the
                # details page, since 3 September 2026) answered "Cannot
                # find mpv-1.dll, mpv-2.dll or libmpv-2.dll" to open and
                # was thrown away for a fresh spawn. _load() adds the
                # directory in this process; frozen, it is sys._MEIPASS.
                from . import video_backend
                video_backend._load()
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
            elif op == "ping":
                # Answered before the `handle is None` guard: it is how
                # start() checks a pre-started child, which has no core.
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
            elif op == "protocol":
                # The opener is this process's own, looked up by scheme:
                # see RemoteMPV.register_stream_protocol for why the
                # parent cannot send one.
                (name,) = message.get("args") or [None]
                from . import player_seekable_stream_patch as seekable
                opener = {seekable.SCHEME: seekable._open,
                          seekable.DIRECT_SCHEME: seekable._open_direct
                          }.get(str(name))
                if opener is None:
                    raise ValueError(f"unknown stream protocol {name!r}")
                handle.register_stream_protocol(str(name), opener)
                value = True
            elif op == "observe":
                (name,) = message.get("args") or [None]
                handle.observe_property(
                    name,
                    lambda n, v: push("property", name=n, value=_plain(v)))
                value = None
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
