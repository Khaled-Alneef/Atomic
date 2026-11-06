"""A log file, and the hook that keeps a slot's exception from killing
the app.

Two long-standing gaps, and they compound each other:

  * Any exception escaping a Qt slot ends the whole process. PyQt calls
    `qFatal()` for it, which aborts with exit `0xc0000409` and prints
    nothing - the ~20s startup crash was one instance, and it left
    nothing behind to diagnose it from.
  * There was no logging anywhere in the app, so anything caught by one
    of the deliberate fail-soft `except` blocks vanished silently too.
    storage.load swallowing an unreadable settings.json is what
    destroyed a real AniList username with no trace (see storage.save).

Replacing `sys.excepthook` is what stops the abort: PyQt only calls
`qFatal()` when the default hook is still installed. With one of our own
in place the traceback is written to the log and the event loop carries
on, which for a UI callback is nearly always the right outcome - the
window survives one broken card or one bad response.

Nothing here may raise. A logger that throws while reporting an error
would turn a survivable bug into the crash it was meant to prevent, so
every call is wrapped and a failure to log is silently accepted.
"""

import logging
import sys
import threading
from logging.handlers import RotatingFileHandler

LOG_NAME = "atomic.log"

# Small on purpose. A slot that raises on every paint would otherwise
# fill the disk with the same traceback; one rotation keeps the previous
# session's tail around, which is what a bug report actually needs.
_MAX_BYTES = 512_000
_BACKUP_COUNT = 1

_lock = threading.Lock()
_logger = None


def _log_dir():
    # Deferred: storage is imported here rather than at module scope
    # because storage logs *through this module*, and importing both ways
    # at import time is circular. It also means DATA_DIR is read when the
    # first message is logged, so a test that redirects it (see
    # .claude/rules/testing.md) gets its own log file rather than writing
    # into the real one.
    from . import storage
    return storage.DATA_DIR


def logger():
    global _logger
    with _lock:
        if _logger is not None:
            return _logger
        log = logging.getLogger("atomic")
        log.setLevel(logging.INFO)
        log.propagate = False
        try:
            handler = RotatingFileHandler(
                _log_dir() / LOG_NAME, maxBytes=_MAX_BYTES,
                backupCount=_BACKUP_COUNT, encoding="utf-8")
            handler.setFormatter(logging.Formatter(
                "%(asctime)s %(levelname)s %(threadName)s %(message)s"))
            log.addHandler(handler)
        except Exception:
            # No writable data directory is not a reason to fail to
            # start; the app simply runs without a log, as it always has.
            log.addHandler(logging.NullHandler())
        _logger = log
        return _logger


def info(message: str):
    try:
        logger().info(message)
    except Exception:
        pass


def warning(message: str):
    try:
        logger().warning(message)
    except Exception:
        pass


def exception(message: str):
    """Log `message` with the exception currently being handled. Call
    from inside an `except` block."""
    try:
        logger().exception(message)
    except Exception:
        pass


def install_excepthook():
    """Log anything that escapes, instead of letting Qt abort the
    process. Call once, before QApplication is constructed."""
    def hook(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            # Ctrl+C should still stop a source run rather than being
            # logged and ignored.
            sys.__excepthook__(exc_type, exc, tb)
            return
        try:
            logger().error("Unhandled exception", exc_info=(exc_type, exc, tb))
        except Exception:
            pass

    sys.excepthook = hook


# ---- ATOMIC_MEMTRACE: where the memory goes ----------------------------
# Set ATOMIC_MEMTRACE=1 before a run and every ten seconds the log gets the
# process's working set beside Python's own traced total and its eight
# biggest allocation sites. Written 6 September 2026 for "Atomic 256MB ->
# 676MB over sixteen sidebar switches": the page slide's pictures were
# the first theory and measured as not it, which is what this exists to
# settle before the next theory. Costs nothing when the variable is
# unset; a traced run is slower and is never a release measurement.
def start_memtrace():
    import os
    if not os.environ.get("ATOMIC_MEMTRACE"):
        return
    try:
        import tracemalloc, ctypes, ctypes.wintypes as wt
        from PyQt6.QtCore import QTimer
        tracemalloc.start(12)

        class _Counters(ctypes.Structure):
            _fields_ = [("cb", wt.DWORD), ("PageFaultCount", wt.DWORD),
                        ("PeakWorkingSetSize", ctypes.c_size_t),
                        ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t),
                        ("PeakPagefileUsage", ctypes.c_size_t),
                        ("PrivateUsage", ctypes.c_size_t)]

        psapi = ctypes.WinDLL("psapi")
        kernel = ctypes.WinDLL("kernel32")
        # argtypes, or the handle and the pointer are truncated to
        # c_int and the call quietly fills nothing (rules/testing.md).
        psapi.GetProcessMemoryInfo.argtypes = [wt.HANDLE, ctypes.POINTER(_Counters), wt.DWORD]
        psapi.GetProcessMemoryInfo.restype = wt.BOOL
        kernel.GetCurrentProcess.restype = wt.HANDLE

        def working_set():
            counters = _Counters(); counters.cb = ctypes.sizeof(counters)
            psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(),
                                       ctypes.byref(counters), counters.cb)
            return counters.WorkingSetSize // (1 << 20), counters.PrivateUsage // (1 << 20)
        start_memtrace.working_set = working_set

        def report():
            try:
                ws, private = working_set()
                traced, peak = tracemalloc.get_traced_memory()
                info(f"memtrace: working set {ws}MB private {private}MB "
                     f"python traced {traced // (1 << 20)}MB")
                snap = tracemalloc.take_snapshot()
                for stat in snap.statistics("lineno")[:8]:
                    frame = stat.traceback[0]
                    info(f"memtrace:   {stat.size // 1024}KB x{stat.count} "
                         f"{frame.filename.replace(chr(92), '/')[-60:]}:{frame.lineno}")
            except Exception:
                pass

        timer = QTimer()
        timer.timeout.connect(report)
        timer.start(10000)
        start_memtrace._timer = timer          # kept alive on the function
        info("memtrace: on")
    except Exception:
        exception("memtrace could not start")


# **A frozen window says where it is frozen.** The owner, 6 November
# 2026: "the app crashes when I open the sources window while the ep is
# running". Windows' own records called both of that night's "crashes"
# hangs (Application Hang 1002, AppHangB1 - "stopped interacting with
# Windows and was closed"), and the log said nothing at all: its last
# line both times was "seat: opening at 230.4s", and then the next
# start. A hang ends in the process being killed, so nothing after it
# can write; the stack has to be taken *while* it hangs, from another
# thread. The UI thread stamps a beat every STALL_BEAT_MS; a watcher
# thread that finds the beat older than STALL_S writes the UI thread's
# stack once, and a line with the length when the beat comes back.
# Always on: a timer at 4Hz and a thread asleep between checks.
STALL_BEAT_MS = 250
STALL_S = 1.5
STALL_SAMPLE_S = 0.1


def start_stall_watch():
    try:
        import time, traceback
        from PyQt6.QtCore import QTimer
        main_ident = threading.get_ident()
        beat = [time.monotonic()]

        def stamp():
            beat[0] = time.monotonic()

        timer = QTimer()
        timer.timeout.connect(stamp)
        timer.start(STALL_BEAT_MS)
        start_stall_watch._timer = timer       # kept alive on the function

        def where(frame, depth):
            """The UI thread's innermost `depth` frames as one line."""
            parts = []
            while frame is not None and len(parts) < depth:
                code = frame.f_code
                name = code.co_filename.replace("\\", "/").rsplit("/", 2)
                parts.append(f"{'/'.join(name[-2:])}:{frame.f_lineno} {code.co_name}")
                frame = frame.f_back
            return " < ".join(parts)

        # **One snapshot is a moment, not the stall.** The first version
        # logged the stack once, at the 1.5s mark, and it named
        # storage.load for a 2.6s freeze when a card was pressed - which
        # a 300KB file cannot account for. So the UI thread is sampled
        # every STALL_SAMPLE_S all the time into a short ring (a few
        # microseconds a sample), and when a stall ends, the samples taken
        # since the last beat before it are counted by place: a profile
        # of the whole stall, its first second included.
        import collections
        ring = collections.deque(maxlen=int(60 / STALL_SAMPLE_S))

        def watch():
            reported = None             # the beat a stall was reported for
            while True:
                time.sleep(STALL_SAMPLE_S)
                try:
                    now = time.monotonic()
                    frame = sys._current_frames().get(main_ident)
                    ring.append((now, where(frame, 7) if frame else "?"))
                    last = beat[0]
                    late = now - last
                    if late > STALL_S and reported is None:
                        reported = last
                        warning(f"ui stall: the window has not ticked for "
                                f"{late:.1f}s; UI thread at: {where(frame, 16)}")
                    elif reported is not None and reported != last:
                        counts = {}
                        for at, key in ring:
                            if reported < at < last:
                                counts[key] = counts.get(key, 0) + 1
                        total = sum(counts.values())
                        top = sorted(counts.items(), key=lambda kv: -kv[1])[:4]
                        info(f"ui stall: over, the window ticked again "
                             f"{last - reported:.1f}s after the last beat before it; "
                             f"{total} samples: " + " || ".join(
                                 f"{count}x {key}" for key, count in top))
                        reported = None
                except Exception:
                    pass

        threading.Thread(target=watch, name="ui-stall-watch", daemon=True).start()
    except Exception:
        exception("the UI stall watch could not start")


def memtrace_mark(label):
    """One working-set line, only while ATOMIC_MEMTRACE is on."""
    ws = getattr(start_memtrace, "working_set", None)
    if ws is None:
        return
    try:
        import gc
        gc.collect()
        w, p = ws()
        info(f"memtrace mark {label}: working set {w}MB private {p}MB")
    except Exception:
        pass
