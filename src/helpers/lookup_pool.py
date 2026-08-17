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


# The second queue, for work somebody is sitting in front of waiting on -
# Settings' per-site Check. Separate from the queue above rather than
# tuned differently: that one is drained by three pages' worth of
# page-load backfill, so a Check pressed after visiting a tracker page
# queued behind every one of those lookups. Measured: Crunchyroll's
# verdict needs no network at all (it is decided from a table) and the
# row still sat blank, because the job had not started. Two workers - a
# Check All overlaps a little without turning into a burst.
WATCHED_WORKERS = 2

_watched_queue = queue.Queue()
_watched_workers = []


def submit(fn, *args, **kwargs):
    """Queue one lookup. Runs on one of at most MAX_WORKERS threads."""
    _ensure_workers(_workers, _queue, MAX_WORKERS, "lookup")
    _queue.put((fn, args, kwargs))


def submit_watched(fn, *args, **kwargs):
    """Queue one job the user is watching for an answer to, on its own
    workers - never behind the backfill draining the shared queue."""
    _ensure_workers(_watched_workers, _watched_queue, WATCHED_WORKERS, "lookup-watched")
    _watched_queue.put((fn, args, kwargs))


def _ensure_workers(workers, work_queue, count, name):
    with _workers_lock:
        if workers:
            return
        for i in range(count):
            worker = threading.Thread(target=_run_forever, args=(work_queue,),
                                      name=f"{name}-{i}", daemon=True)
            worker.start()
            workers.append(worker)


_latest_jobs = {}
_latest_ready = threading.Condition()
_latest_worker = None


def submit_latest(key: str, fn, *args, **kwargs):
    """Queue one lookup of which only the newest matters, replacing any
    earlier one under the same `key` that hasn't started yet.

    For work fired by typing: the Video Website field starts a resolution
    per debounced keystroke, and every one of those but the last is
    already stale by the time it answers - the dialog throws the result
    away on an identity mismatch. Previously each got a bare
    threading.Thread with no ceiling of any kind, which is the exact
    shape that once put 651 connections in flight at once.

    Deliberately *not* the shared queue above: this is a lookup the user
    is watching a status line for, and behind a page-load backfill of
    every tracked entry it would wait minutes. One dedicated worker
    instead - which caps this path at a single connection, tighter than
    the shared pool - and superseded jobs are dropped before they ever
    run rather than raced."""
    global _latest_worker
    with _latest_ready:
        _latest_jobs[key] = (fn, args, kwargs)
        if _latest_worker is None:
            _latest_worker = threading.Thread(target=_run_latest_forever,
                                              name="lookup-latest", daemon=True)
            _latest_worker.start()
        _latest_ready.notify()


def _run_latest_forever():
    # Must never raise, for the same reason _run_forever must not.
    while True:
        with _latest_ready:
            while not _latest_jobs:
                _latest_ready.wait()
            key = next(iter(_latest_jobs))
            fn, args, kwargs = _latest_jobs.pop(key)
        try:
            fn(*args, **kwargs)
        except Exception:
            pass


def _run_forever(work_queue):
    # Must never raise: an uncaught exception would kill this worker for
    # good, and unlike a one-shot thread it takes every lookup still
    # queued behind it with it - the page's refresh counters would then
    # wait forever for results that can never arrive.
    while True:
        fn, args, kwargs = work_queue.get()
        try:
            fn(*args, **kwargs)
        except Exception:
            pass
        finally:
            work_queue.task_done()
