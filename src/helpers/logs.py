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
