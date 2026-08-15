"""A bounded worker pool for the tracker's per-entry background lookups.

Every per-entry lookup used to get its own bare `threading.Thread`,
started in a loop over `self.entries` with no ceiling of any kind. With a
few dozen tracked entries that is a few dozen threads opening a few dozen
outbound connections in the same instant - and three tracker pages
(Anime, Reading, Series) each do it on load, on top of the refresh button
firing two lookups per entry. The user reproduced the consequence
directly: their *whole home network* slowed down while entries were in
tracker.json and recovered the moment they were removed, which is a
connection-table/DNS burst at the router, not Atomic being slow.

So: a fixed, small number of daemon worker threads draining one queue.
Nothing runs sooner than it used to in the single-entry case, everything
still eventually runs, and the number of connections in flight has a real
ceiling regardless of how long the tracked list gets.

Daemon threads and a plain queue rather than
`concurrent.futures.ThreadPoolExecutor`: the executor registers an
`atexit` hook that *joins* its workers, so quitting Atomic mid-lookup
would block on however long the slowest HTTP timeout has left to run
(6-10s across anilist/stremio/mangadex). Daemon workers keep the old
behaviour of the app closing instantly.
"""

import queue
import threading

# Deliberately small. This is unprompted background metadata refresh that
# fires on page load, not something the user asked for and is waiting on,
# so it should stay well clear of saturating a home router - and MangaDex
# in particular is rate-limited and self-throttles (see mangadex._get).
MAX_WORKERS = 4

_queue = queue.Queue()
_workers = []
_workers_lock = threading.Lock()


def submit(fn, *args, **kwargs):
    """Queue one lookup. Runs on one of at most MAX_WORKERS threads."""
    _ensure_workers()
    _queue.put((fn, args, kwargs))


def _ensure_workers():
    with _workers_lock:
        if _workers:
            return
        for i in range(MAX_WORKERS):
            worker = threading.Thread(target=_run_forever, name=f"lookup-{i}",
                                      daemon=True)
            worker.start()
            _workers.append(worker)


def _run_forever():
    # Must never raise: an uncaught exception would kill this worker for
    # good, and unlike a one-shot thread it takes every lookup still
    # queued behind it with it - the page's refresh counters would then
    # wait forever for results that can never arrive.
    while True:
        fn, args, kwargs = _queue.get()
        try:
            fn(*args, **kwargs)
        except Exception:
            pass
        finally:
            _queue.task_done()
