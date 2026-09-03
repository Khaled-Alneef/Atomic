"""Keeping a copy: episodes, seasons and chapters saved to disk.

Everything needed for this already existed and was being thrown away.
The torrent engine writes real files while streaming, `subtitles.fetch`
already returns decoded Arabic text, and `chapter_source.chapter_pages`
already yields page URLs with the headers those hosts demand. A download
is those three things pointed at a folder the user keeps, plus a queue
so closing the player does not abandon them.

Shapes deliberately chosen to be ordinary rather than clever, so the
files are useful outside Atomic too:

  * Video keeps the release's own filename, with the subtitle written
    beside it as `<same name>.ar.srt`. That is the convention every
    player looks for, so the Arabic track loads by itself in VLC, mpv or
    anything else - a subtitle saved anywhere else is a subtitle nobody
    finds again.
  * Chapters become a **.cbz**, which is a zip of images in page order
    and what every comic reader on every platform opens. A folder of
    loose jpgs would work here and nowhere else.

Jobs survive a restart because they are written to disk as they change;
a half-downloaded season that vanished when the app closed would be the
main reason not to trust the feature.
"""

import os
import re
import threading
import time
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor

from . import logs, net, storage

JOBS_FILE = "downloads.json"

# Job states. Kept as plain strings: they are written to disk and read
# back by a later version, and an enum would only make that brittle.
QUEUED = "queued"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
CANCELLED = "cancelled"
# Held by the user rather than stopped. Distinct from CANCELLED because
# the pieces already fetched are kept and the job can be started again
# where it left off (the owner's ask for a pause button).
PAUSED = "paused"

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")

_lock = threading.RLock()
_jobs = None
_worker = None
_cancelled = set()
# Job ids the user has held. Checked in the same places as _cancelled,
# but a paused job keeps its partial data and can be started again.
_paused = set()


# ---------------------------------------------------------------- store

def _load():
    global _jobs
    with _lock:
        if _jobs is None:
            rows = storage.load(JOBS_FILE, [])
            _jobs = rows if isinstance(rows, list) else []
            # Anything that claimed to be running when the app closed
            # was not; it stopped with the process.
            for job in _jobs:
                if job.get("state") == RUNNING:
                    job["state"] = QUEUED
        return _jobs


def _save():
    with _lock:
        storage.save(JOBS_FILE, _jobs or [])


def list_jobs() -> list:
    """Every job, newest first - what a Downloads page renders."""
    with _lock:
        return [dict(job) for job in reversed(_load())]


# Progress moves constantly; the *state* of a job changes a handful of
# times in its life. Only the second kind is worth a disk write.
_PROGRESS_SAVE_GAP = 2.0
_last_progress_save = 0.0


def _update(job_id, **fields):
    """Update a job, writing to disk only when it is worth it.

    **The write used to happen on every single update**, and a chapter
    reports once per page - so a 21-page chapter rewrote the whole jobs
    file 21 times, each rewrite holding the lock that the downloads page
    and the sidebar indicator both poll twice a second. The UI thread
    then waits on disk I/O it has no reason to touch, which is exactly
    what a frozen window looks like while the rest of the app keeps
    working.

    A state change is always written immediately - that is the part that
    must survive a crash. Pure progress is written at most every couple
    of seconds, because a percentage that is two seconds stale after an
    unexpected shutdown costs nothing."""
    global _last_progress_save
    changes_state = "state" in fields or "path" in fields
    with _lock:
        target = None
        for job in _load():
            if job.get("id") == job_id:
                job.update(fields)
                target = dict(job)
                break
        if target is None:
            return None
        now = time.time()
        should_save = changes_state or (now - _last_progress_save) >= _PROGRESS_SAVE_GAP
        if should_save:
            _last_progress_save = now
    # Outside the lock: the readers only need the in-memory list, which
    # is already correct by the time we get here, and holding a lock
    # across a file write is what put the UI thread behind disk I/O.
    if should_save:
        _save_unlocked()
    return target


def _save_unlocked():
    try:
        storage.save(JOBS_FILE, list(_jobs or []))
    except Exception:
        # Swallowed so a worker never raises, but no longer silently: a
        # queue that stopped persisting had nothing in atomic.log
        # (review, 3 September 2026).
        logs.exception("Could not save the download queue")


def cancel(job_id):
    _cancelled.add(job_id)
    _update(job_id, state=CANCELLED)


def pause(job_id):
    """Hold a job. A running one stops at its next progress tick and
    keeps whatever it has fetched - the torrent engine holds the pieces,
    so resuming continues rather than starting over."""
    _paused.add(job_id)
    _update(job_id, state=PAUSED)


def resume(job_id):
    """Put a paused job back in the queue, and make sure a worker is
    awake to take it."""
    _paused.discard(job_id)
    _update(job_id, state=QUEUED)
    _ensure_worker()


def pause_group(group_id):
    for job in list_jobs():
        if job.get("group") == group_id and job.get("state") in (QUEUED, RUNNING):
            pause(job["id"])


def resume_group(group_id):
    for job in list_jobs():
        if job.get("group") == group_id and job.get("state") == PAUSED:
            resume(job["id"])


def resume_pending():
    """Start work again on whatever was still queued when the app last
    closed - the owner's ask ("make it continue downloading if I close
    and re-open the app").

    _load already turns a stale RUNNING back into QUEUED, so the queue
    is honest by the time this looks; all that was missing was anything
    waking the worker at startup. Paused jobs stay paused: the user held
    those on purpose, and a restart is not consent to resume them."""
    try:
        waiting = [job for job in _load() if job.get("state") == QUEUED]
    except Exception:
        return 0
    if waiting:
        _ensure_worker()
    return len(waiting)


def clear_finished():
    with _lock:
        jobs = _load()
        jobs[:] = [j for j in jobs if j.get("state") in (QUEUED, RUNNING)]
        _save()


# **Finished videos are filed, not dumped.** The owner's ask, 27 August
# 2026: episodes go into a folder of their own "exactly like the
# readings". Readings have had a per-title subfolder since _run_chapter
# was written, so videos get the same scheme with one folder above it -
# every episode of a series together under its own title, and the whole
# watchable library under one roof instead of loose in a download folder
# that holds everything else too.
#
# Only new downloads move. Nothing renames or relocates what is already
# on disk, for the same reason _run_chapter gives about its own folder
# scheme: his files are not this code's to reorganise.
WATCHABLE_DIR = "Watchable"


def default_folder() -> str:
    """Where downloads land unless the user picks somewhere else."""
    base = os.path.join(os.path.expanduser("~"), "Downloads", "Atomic")
    return base


_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
# What reads as a gap between words rather than as part of one: any
# whitespace, and the brackets a title carries around a disambiguator.
# "Kingdom (WAN)" is a real title here, and keeping its parentheses would
# put back the very characters the 22 August 2026 rename removed.
_SEPARATORS = re.compile(r"[\s()\[\]{}]+")
# A run of the characters that join words. Collapsed to a single "_" when
# the run contains one - so " - " between title and subtitle becomes one
# separator - and left alone when it is a hyphen inside a word
# ("Spider-Man"). One character class with one quantifier, so it is a
# single linear pass; the obvious "[-_]*_[-_]*" is ambiguous and
# backtracks quadratically on a long run of dashes.
_JOINERS = re.compile(r"[-_]+")
# Device names Windows still refuses whatever extension follows: CON.mkv
# is not a file, it is the console. Only ever matched against the whole
# stem, so the "[Atomic] " prefix means saved_name can never produce one
# - but _run_chapter names a *folder* from a bare title, and "Nul" is a
# perfectly plausible manga.
_RESERVED = frozenset(["CON", "PRN", "AUX", "NUL"]
                      + [f"COM{d}" for d in "123456789"]
                      + [f"LPT{d}" for d in "123456789"])


def safe_name(text: str, fallback: str = "download") -> str:
    """A filename Windows will accept, without losing the Arabic in it -
    Arabic characters are perfectly legal in a filename and stripping
    them would turn a title into an empty string.

    Spaces become underscores because the whole saved name is underscore
    separated (see saved_name); the stripping at the end runs again
    *after* the length cap, since a cut can land on a trailing "_" or "."
    and Windows rejects a name ending in either."""
    cleaned = _UNSAFE.sub("_", str(text or ""))
    cleaned = _SEPARATORS.sub("_", cleaned)
    cleaned = _JOINERS.sub(
        lambda run: "_" if "_" in run.group() else run.group(), cleaned)
    cleaned = cleaned.strip(" ._-")[:120].strip(" ._-")
    if cleaned.split(".")[0].upper() in _RESERVED:
        cleaned = f"_{cleaned}"
    return cleaned or fallback


def _tag(prefix, number, width) -> str:
    """One numbered part of a saved name: `EP01`, `S01`, `CH884`.

    The owner's prefixes, 22 August 2026 - "EP" for an episode, "S" for a
    season, "CH" for a chapter. They live here rather than at the two
    call sites sixty lines apart, which is how the video path and the
    reading path drift.

    `width` is a floor, never a cap: One Piece episode 1136 is EP1136,
    not a truncation. A chapter keeps a real fraction ("CH884.5") and
    drops a meaningless one (884.0 arrives as a float and is CH884), and
    anything that is not a number at all is written as it came - some
    sources label a chapter "Extra"."""
    text = str(number if number is not None else "").strip()
    if not text:
        return None
    head, _, tail = text.partition(".")
    tail = tail.rstrip("0")
    try:
        text = f"{int(head):0{width}d}" + (f".{tail}" if tail else "")
    except ValueError:
        pass                       # not a number - keep whatever it says
    return f"{prefix}{text}"


def episode_tag(number) -> str:
    """Two digits, which is what the owner's example asked for ("EP01").
    Known cost, left in deliberately: a folder of a 1000-episode series
    sorts EP100 before EP99. Widening this to three is a one-word change
    if he ever wants it."""
    return _tag("EP", number, 2)


def season_tag(number) -> str:
    return _tag("S", number, 2)


def chapter_tag(number) -> str:
    """Three digits, unlike the episodes' two, because manga numbering
    routinely runs past 99 - Kingdom is at 884 and One Piece past 1100 in
    the owner's own library - so two would sort CH5 after CH1000 in the
    folder. His own example ("CH884") is unaffected either way, which is
    why this could be chosen on the sorting alone."""
    return _tag("CH", number, 3)


def saved_name(title, *, number=None, season=None, fallback="download") -> str:
    """The owner's naming scheme for everything kept, revised 22 August
    2026 - underscore separated, no parentheses:

        [Atomic] Re_Zero_Starting_Life_in_Another_World_EP01_S01
        [Atomic] Kingdom_WAN_CH884

    It was "[Atomic] (Name) (E01) (S01)", and the brackets came off on
    his ask. Two consequences of that, both deliberate:

      * a title's *own* brackets go too, so "Kingdom (WAN)" saves as
        "Kingdom_WAN" - keeping them would leave parentheses in a name he
        asked to have none, and the old scheme nested them anyway
        ("[Atomic] (Kingdom (WAN)) (CH884).cbz" is a real file he has);
      * the title's spaces become underscores, since an underscore is
        what now separates the parts and a name half-spaced half-joined
        reads as two schemes.

    Nothing already on disk is renamed - this is the format for names
    written from here on. A job recorded in downloads.json keeps the path
    it was finished under, which is the path its Open Folder button uses.

    Parts with nothing to say are dropped rather than written empty - a
    film has no episode and a chapter has no season. The release's own
    name is deliberately not used: it carried the group, the resolution
    and the hash, so a folder of episodes sorted by whoever released them
    instead of by episode number.

    No extension - the caller appends the source's own, since that is
    the one part of a release name worth keeping."""
    parts = [safe_name(title, fallback)]
    for part in (number, season):
        if part is not None and str(part).strip() != "":
            parts.append(safe_name(part))
    return "[Atomic] " + "_".join(parts)


# ----------------------------------------------------------- queueing

def _add(job: dict) -> dict:
    import uuid
    job.setdefault("id", uuid.uuid4().hex[:12])
    job.setdefault("state", QUEUED)
    job.setdefault("added_at", storage.now_iso())
    job.setdefault("progress", 0.0)
    job.setdefault("detail", "")
    with _lock:
        _load().append(job)
        _save()
    _ensure_worker()
    return dict(job)


def queue_episode(entry, *, season=None, episode=None, quality=None,
                  subtitle=None, folder=None, audio=None) -> dict:
    """One episode (or a film, with season/episode left out).

    `audio` is a soft preference over which *release* gets picked -
    "en" prefers dual-audio/dub releases, "jp" (or None) the ordinary
    original-audio fansubs. Soft because a torrent's tracks are whatever
    the release carries; the preference reorders candidates, it cannot
    conjure a dub that was never released (see _order_by_audio)."""
    title = entry.get("title") or "Video"
    label = title if not episode else f"{title} S{int(season or 1):02d}E{int(episode):02d}"
    return _add({
        "kind": "video",
        "label": label,
        "entry_id": entry.get("id"),
        "entry": {k: entry.get(k) for k in
                  ("id", "title", "type", "imdb_id", "url", "site_id")},
        "season": season, "episode": episode,
        "quality": quality,
        "subtitle": subtitle,
        "audio": audio,
        "folder": folder or default_folder(),
    })


def queue_season(entry, *, season=None, episodes=(), quality=None,
                 subtitle=None, folder=None, audio=None) -> list:
    """A whole season, as one job per episode sharing a group.

    One job each rather than a single job for the lot: episodes come
    from different releases with different swarms, and a season that
    reports one failure because its ninth episode has no seeders would
    hide the eight that worked.

    They carry a shared `group` so the UI can still show one bar for
    "season 4" while keeping each episode's own success or failure -
    which is the whole point of splitting them."""
    import uuid
    group = uuid.uuid4().hex[:12]
    numbers = [int(n) for n in episodes]
    label = f"{entry.get('title') or 'Season'} - season {int(season or 1)}"
    if numbers and (min(numbers), max(numbers)) != (1, len(numbers)):
        # A chosen range, not the whole season - say which one, so the
        # queue row reads as what was actually asked for.
        label += f" (E{min(numbers):02d}-E{max(numbers):02d})"
    jobs = []
    for number in episodes:
        job = queue_episode(entry, season=season, episode=number,
                            quality=quality, subtitle=subtitle, folder=folder,
                            audio=audio)
        jobs.append(_update(job["id"], group=group, group_label=label) or job)
    return jobs


def list_groups() -> list:
    """Jobs collapsed into what the user actually asked for.

    A season queued as 28 jobs is one request, and a page listing 28
    bars for it buries everything else. Each row here is either a single
    job or a whole season, with progress averaged across its episodes
    and a count of how many are finished."""
    rows, groups, order = [], {}, []
    for job in list_jobs():
        key = job.get("group")
        if not key:
            rows.append({"kind": "job", "job": job, "label": job.get("label"),
                         "progress": job.get("progress") or 0.0,
                         "state": job.get("state"), "detail": job.get("detail"),
                         "jobs": [job]})
            continue
        if key not in groups:
            groups[key] = {"kind": "group", "label": job.get("group_label")
                           or "Season", "jobs": []}
            order.append(key)
            rows.append(groups[key])
        groups[key]["jobs"].append(job)

    for key in order:
        row = groups[key]
        jobs = row["jobs"]
        done = [j for j in jobs if j.get("state") == DONE]
        failed = [j for j in jobs if j.get("state") == FAILED]
        running = [j for j in jobs if j.get("state") == RUNNING]
        # Averaged over the whole season, counting a finished episode as
        # a full one - so the bar reflects the season, not whichever
        # episode happens to be downloading now.
        row["progress"] = (sum(1.0 if j.get("state") == DONE
                               else (j.get("progress") or 0.0) for j in jobs)
                           / max(len(jobs), 1))
        # Cancelled has to be weighed here or a season nobody is
        # downloading any more reports itself queued forever: the
        # indicator then shows work in progress with no worker, and
        # never comes down. A season is only still QUEUED if something
        # in it genuinely is.
        cancelled = [j for j in jobs if j.get("state") == CANCELLED]
        waiting = [j for j in jobs if j.get("state") == QUEUED]
        held = [j for j in jobs if j.get("state") == PAUSED]
        if running:
            row["state"] = RUNNING
        elif waiting:
            row["state"] = QUEUED
        elif held:
            # Nothing running or queued but something held: the season
            # reads as paused, which is what its button then offers to
            # undo.
            row["state"] = PAUSED
        elif len(done) == len(jobs):
            row["state"] = DONE
        elif cancelled and not failed and not done:
            row["state"] = CANCELLED
        elif failed:
            row["state"] = FAILED
        else:
            # A mix of finished and abandoned: what survives is what was
            # saved, so say Done rather than Cancelled.
            row["state"] = DONE if done else CANCELLED
        parts = [f"{len(done)} of {len(jobs)} episodes"]
        if failed:
            parts.append(f"{len(failed)} failed")
        if running:
            parts.append(running[0].get("detail") or "")
        row["detail"] = " · ".join(p for p in parts if p)
    return rows


def cancel_group(group_id):
    """Cancel the episodes of a season that have not finished.

    Only the ones still queued or running. Cancelling a season used to
    mark its *finished* episodes cancelled too, which threw away the one
    thing they had: a file on disk, its Done badge and the button that
    opens its folder. "Stop downloading this season" cannot mean
    "forget the eight episodes already saved"."""
    for job in list_jobs():
        if job.get("group") == group_id and job.get("state") in (QUEUED, RUNNING):
            cancel(job["id"])


def active_progress():
    """One number for "is anything downloading, and how far along" -
    for a badge or a strip that is visible without opening the page.

    None when nothing is active, so a caller can hide the indicator
    entirely rather than showing an idle bar.

    `count` is the exact number of episodes/chapters still queued or
    running - the owner's ask. It counted *groups* before, so a whole
    season downloading read "(1)", which answered "how many requests"
    when the question on a badge is "how many files are left"."""
    rows = [r for r in list_groups() if r.get("state") in (RUNNING, QUEUED)]
    if not rows:
        return None
    remaining = [job for row in rows for job in (row.get("jobs") or [])
                 if job.get("state") in (RUNNING, QUEUED)]
    return {"count": len(remaining) or len(rows),
            "progress": sum(r.get("progress") or 0.0 for r in rows) / len(rows),
            "label": rows[0].get("label") or ""}


def queue_chapters(entry, chapters, *, folder=None) -> list:
    """One .cbz per chapter."""
    jobs = []
    for chapter in chapters or []:
        number = chapter.get("number")
        jobs.append(_add({
            "kind": "chapter",
            "label": f"{entry.get('title') or 'Manga'} - chapter {number}",
            "entry_id": entry.get("id"),
            "entry": {k: entry.get(k) for k in ("id", "title", "type", "url")},
            "chapter": chapter,
            "folder": folder or default_folder(),
        }))
    return jobs


# ------------------------------------------------ the browser's download

# The owner's ask, 2 September 2026 (roadmap #14): "make the ep/ch
# downloads open the browser somehow to make it purely on the Wi-Fi speed
# not the source speed". What a browser can download is a plain http(s)
# URL - a debrid link served from the service's own CDN, or an addon's
# direct URL. A torrent has no such URL: a magnet needs a torrent client,
# and the app's own engine already pulls it at swarm speed, so for a
# torrent-only release the honest answer is None and the in-app queue.
#
# Measured 2 September 2026 on Reacher S1E2 (81 rows): 2 of the top 20
# releases were held by the debrid service; `playable_url` on the first
# answered in 0.70s with a URL on the service's download host, and a
# Range request against it came back **206** with `Accept-Ranges: bytes`
# (1024 bytes in 0.61s) - so the browser can download it as an ordinary
# file, at whatever the line does.
BROWSER_DEBRID_TRIES = 4
BROWSER_LINK_BUDGET_S = 25.0
BROWSER_PROBE_TIMEOUT_S = 6.0


def _browser_can_fetch(url, deadline) -> bool:
    """Whether a plain GET of `url` answers with a body - a 200, or a
    206 to the one-byte Range asked for here. Checked before the link
    is handed to a browser: a link that 403s there is a blank tab, and
    nothing in the app would ever hear about it."""
    timeout = net.step_timeout(deadline, BROWSER_PROBE_TIMEOUT_S)
    if timeout is None:
        return False
    try:
        request = urllib.request.Request(
            url, headers={"Range": "bytes=0-0", "User-Agent": _UA})
        with net.urlopen(request, timeout=timeout) as response:
            return int(getattr(response, "status", 0) or 0) in (200, 206)
    except Exception:
        return False


def direct_url_for(entry, *, season=None, episode=None, quality=None,
                   audio=None, streams_found=None, prefer=None,
                   deadline=None):
    """A plain http(s) URL for this episode (or film) that the system
    browser can download by itself, or None when only the swarm can
    serve it.

    The pick follows the queue's own order (`quality` via
    streams.matching_quality, then `_order_by_audio`), with `prefer` -
    the release the player is showing - moved to the front. Releases
    the debrid service already holds are asked first, because that is
    the one source measured answering Range requests from a CDN; an
    addon's own direct URL is the fallback. Every link is probed
    (`_browser_can_fetch`) before it is returned.

    **Deliberately not streams.prepare().** That races the debrid arm
    against the engine, and the engine's win is a 127.0.0.1 URL served
    from the swarm - exactly the speed this exists to get away from -
    with a torrent left added for a download that is going to happen in
    the browser instead. Only the debrid half is wanted here, so only
    that half is asked. `streams_found` skips the fan-out when the
    caller already has the list (the player does; find_streams measured
    6.9s cold on Reacher S1E2, 0s from its cache).

    Returns {"url", "name", "size"} - the URL carries an account token
    and must never be logged or shown, only opened."""
    from . import streams
    try:
        from . import debrid
    except Exception:
        debrid = None
    if deadline is None:
        deadline = net.deadline_in(BROWSER_LINK_BUDGET_S)

    found = list(streams_found or [])
    if not found:
        budget = net.step_timeout(deadline, 40.0)
        if budget is None:
            return None
        found = list(streams.find_streams(
            entry, season=season, episode=episode,
            deadline=net.deadline_in(budget)) or [])
    ordered = streams.matching_quality(found, quality) if quality else []
    candidates = list(ordered or found)
    candidates = _order_by_audio(candidates, audio)
    if prefer:
        key = (prefer.get("info_hash") or "").lower(), prefer.get("url")
        rest = [s for s in candidates
                if ((s.get("info_hash") or "").lower(), s.get("url")) != key]
        candidates = [dict(prefer)] + rest

    torrents = [s for s in candidates
                if s.get("kind") == "torrent" and s.get("info_hash")]
    directs = [s for s in candidates
               if s.get("kind") == "direct"
               and str(s.get("url") or "").startswith("http")]
    # The playing release may already be a debrid link (prepare() turns
    # the stream into kind="direct"); that is the best answer there is.
    if prefer and prefer in directs:
        directs.remove(prefer)
        directs.insert(0, prefer)

    if debrid is not None and torrents:
        try:
            available = debrid.available()
        except Exception:
            available = False
        if available:
            hashes = [s["info_hash"].lower() for s in torrents[:20]]
            try:
                cached = debrid.cached_hashes(
                    hashes, deadline=net.deadline_in(
                        net.step_timeout(deadline, 8.0) or 1.0))
            except Exception:
                cached = set()
            tries = 0
            for stream in torrents:
                info_hash = stream["info_hash"].lower()
                if info_hash not in cached or tries >= BROWSER_DEBRID_TRIES:
                    continue
                budget = net.step_timeout(deadline, 12.0)
                if budget is None:
                    break
                tries += 1
                try:
                    got = debrid.playable_url(
                        info_hash, season=season, episode=episode,
                        deadline=net.deadline_in(budget),
                        title=entry.get("title"))
                except Exception:
                    got = None
                url = (got or {}).get("url") or ""
                if url.startswith("http") and _browser_can_fetch(url, deadline):
                    return {"url": url,
                            "name": got.get("file_name") or stream.get("name"),
                            "size": got.get("size")}
    for stream in directs:
        url = stream.get("url")
        if _browser_can_fetch(url, deadline):
            return {"url": url, "name": stream.get("name"),
                    "size": stream.get("size")}
    return None


# ------------------------------------------------------------- worker

def _ensure_worker():
    global _worker
    with _lock:
        if _worker is not None and _worker.is_alive():
            return
        _worker = threading.Thread(target=_run_queue, daemon=True,
                                   name="atomic-downloads")
        _worker.start()


def _next_queued():
    with _lock:
        for job in _load():
            if job.get("state") == QUEUED:
                return dict(job)
    return None


def _run_queue():
    """One job at a time.

    Serial on purpose: two torrents at once halve each other's speed and
    both finish later than either would alone, and the point of a
    download queue is that the first thing you asked for arrives first.
    """
    while True:
        job = _next_queued()
        if job is None:
            return
        job_id = job["id"]
        if job_id in _cancelled:
            continue
        _update(job_id, state=RUNNING, detail="Starting...")
        try:
            if job.get("kind") == "chapter":
                path = _run_chapter(job)
            else:
                path = _run_video(job)
        except Exception as error:
            _update(job_id, state=FAILED, detail=str(error)[:160])
            continue
        if job_id in _cancelled:
            _update(job_id, state=CANCELLED)
        elif job_id in _paused:
            pass        # pause() already set the state; nothing failed
        elif path:
            _update(job_id, state=DONE, progress=1.0, path=path,
                    detail=os.path.basename(path))
        else:
            _update(job_id, state=FAILED,
                    detail="Nothing could be downloaded for this.")


# (entry id or title, season) -> the info hash of the season pack a job
# in that season last resolved. In-memory only: it points into the
# torrent engine's own held torrents, which don't survive a restart
# either, and file_index_for answers None for anything no longer held.
_season_packs = {}


def _prefetch_group_siblings(job, info_hash):
    """Want the still-queued episodes of this job's season group that
    live in the same pack, so they download alongside the current one
    instead of each re-warming the swarm from zero when its turn comes.
    Their own jobs still run - instantly finding their file complete (or
    nearly) and tracking whatever remains."""
    from . import torrent_engine
    group = job.get("group")
    if not group:
        return
    try:
        with _lock:
            siblings = [(j.get("season"), j.get("episode"))
                        for j in _load()
                        if j.get("group") == group and j.get("state") == QUEUED]
        indexes = [torrent_engine.file_index_for(info_hash, s, e)
                   for s, e in siblings]
        indexes = [i for i in indexes if i is not None]
        if indexes:
            torrent_engine.raise_files(info_hash, indexes)
    except Exception:
        pass          # a failed prefetch costs nothing but the head start


# **A bare "DUAL" counts too.** The pattern wanted `dual` *followed
# by* `audio`, and scene names routinely write neither together:
# `Attack.on.Titan.S04E01.1080p.CR.WEB-DL.DUAL.AAC2.0.H.264-VARYG` is a
# dual-audio release whose next token is the codec, so it read as
# Japanese-only and was raced first for a "jp" choice - measured over
# seven real Attack on Titan season 4 names, 26 August 2026.
_DUB_RE = re.compile(r"dual[\s._-]?audio|multi[\s._-]?audio|\bdub(?:bed)?\b"
                     r"|\bdual\b"
                     r"|english\s*audio|\beng\b.{0,12}\baudio\b", re.I)


def _split_by_audio(candidates, audio):
    """`(preferred, rest)` for the asked-for audio.

    **Ordering alone never decided anything, and that is the owner's
    "when I download the ep it does not download it in JP although I
    chose JP".** `_order_by_audio` floats the right releases to the top
    and `prepare_fastest` then *races* what it is given - "play whichever
    delivers data first" - so a dual-audio release in the same opening
    batch beats a Japanese-audio fansub that merely sorted above it. The
    choice was a tie-break on a race it could not win.

    Split instead, and race the preferred group on its own. The fallback
    is why this returns two lists rather than filtering: release names
    lie by omission, and an empty queue because nothing said "dual
    audio" would be worse than the wrong-order pick this replaces - so
    the rest are still raced, but only when nothing preferred will
    start."""
    # **"orig" is "jp" for every title that is not Japanese**, 28 August
    # 2026. The split only ever knew two states - prefer a dub-tagged
    # release, or prefer one without - and "jp" was the name for the
    # second because anime was the only case that had one. A Spanish
    # series has an original audio too, so the dialog now sends "orig"
    # for it and "jp" stays accepted: jobs queued by an older build are
    # still sitting in the queue file, and a value this stopped
    # recognising would silently downgrade them to no preference at all.
    if audio not in ("en", "jp", "orig"):
        return list(candidates), []
    wants_dub = audio == "en"
    preferred, rest = [], []
    for stream in candidates:
        dubbed = bool(_DUB_RE.search(stream.get("title") or ""))
        (preferred if dubbed == wants_dub else rest).append(stream)
    return preferred, rest


def _order_by_audio(candidates, audio):
    """Reorder releases toward the asked-for audio, without dropping any.

    "en" floats releases whose names say dual-audio/dub; "jp" (the
    original) floats the ones that do not. Reordered rather than
    filtered: release names lie by omission all the time, and an empty
    queue because no name said "dual audio" would be worse than the
    wrong-order fallback these are."""
    if audio not in ("en", "jp", "orig"):
        return candidates
    wants_dub = audio == "en"
    return sorted(candidates,
                  key=lambda s: bool(_DUB_RE.search(s.get("title") or ""))
                  != wants_dub)


def _run_video(job) -> str:
    from . import streams, subtitles, torrent_engine

    entry = job.get("entry") or {}
    season, episode = job.get("season"), job.get("episode")
    job_id = job["id"]

    # A season queued as five jobs used to pay five full source lookups
    # and five swarm warm-ups even when every episode lived in the one
    # season pack the first job had already connected to. If the pack a
    # sibling resolved is still held and *names* this episode, reuse it -
    # the metadata, the peers and often the pieces are already here.
    # file_index_for insists on a name match, so a single-episode torrent
    # can never be mistaken for a pack (the copied-episode-1-five-times
    # defect this whole path replaces).
    # **Keyed by what was actually asked for, not just the season - the
    # owner, 24 August 2026: "when I downloaded aot s1 ep 3 it was
    # embedded English sound although I selected JP".**
    #
    # The reuse below is the right optimisation for a season queued as N
    # jobs, and it was silently the wrong one for a *changed* request:
    # keyed on (entry, season) alone, the first download of a season
    # pinned the release for every later episode of it, so the audio and
    # quality choices on the second download were never consulted at
    # all - the branch that reads them is the one this skips. Measured
    # that day on his own entry: of 74 candidates for Attack on Titan
    # S01E03, 39 name themselves dual-audio and 35 do not, and
    # `_order_by_audio("jp")` correctly floats the Japanese-audio
    # fansubs (Erai-raws, MTBB) to the top - it simply never ran.
    #
    # With the choices in the key, a season queued in one go still
    # shares one pack (every job carries the same values), and asking
    # for something different starts a fresh, correctly-ordered pick.
    pack_key = (entry.get("id") or entry.get("title"), season,
                job.get("audio"), job.get("quality"))
    info_hash = _season_packs.get(pack_key)
    if not (info_hash and torrent_engine.file_index_for(
            info_hash, season, episode) is not None):
        _update(job_id, detail="Looking for a source...")
        found = streams.find_streams(entry, season=season, episode=episode,
                                     deadline=net.deadline_in(40))
        wanted = job.get("quality")
        ordered = streams.matching_quality(found, wanted) if wanted else []
        candidates = [s for s in (ordered or found) if s.get("info_hash")]
        candidates = _order_by_audio(candidates, job.get("audio"))
        if not candidates:
            return ""

        _update(job_id, detail="Connecting to peers...")
        # **The asked-for audio gets its own race first.** See
        # _split_by_audio: handing one ordered list to prepare_fastest
        # left the choice as a tie-break on a race decided by whichever
        # swarm answered first, which is not the same thing at all.
        preferred, rest = _split_by_audio(candidates, job.get("audio"))
        ready = None
        if preferred:
            ready = streams.prepare_fastest(preferred, season=season,
                                            episode=episode)
        if (not ready or not ready.get("info_hash")) and rest:
            # Nothing in the preferred group would start. A download in
            # the other language beats no download at all, and the name
            # may simply not have said.
            ready = streams.prepare_fastest(rest, season=season,
                                            episode=episode)
        if not ready or not ready.get("info_hash"):
            return ""
        info_hash = ready["info_hash"]
    else:
        # **own=False: a download must never repoint a pack that is
        # being watched.** `file_index` is what the stream server
        # resolves `<hash>/0` through, so re-picking it here moves the
        # picture of anyone playing another episode out of the same
        # pack to this one - the wrong-episode report, from the download
        # side rather than the prewarm side that torrent_engine.add
        # already records. The file this job wants is asked for
        # separately below and never written to the torrent.
        if not torrent_engine.add(info_hash, season=season, episode=episode,
                                  own=False):
            _season_packs.pop(pack_key, None)
            return _run_video(job)
    if season and episode:
        _season_packs[pack_key] = info_hash
    torrent_engine.download_whole(info_hash)
    _prefetch_group_siblings(job, info_hash)

    # Same shape as _run_chapter's, one level deeper - see WATCHABLE_DIR.
    # safe_name for the title part so it matches the file inside it
    # ("Attack_on_Titan", not "Attack on Titan (2013)").
    # Which file in the pack is this job's, asked without moving the
    # torrent's own pointer - see torrent_engine.file_index_for.
    wanted_index = torrent_engine.file_index_for(
        info_hash, season=season, episode=episode,
        title=(entry.get("title") or None))

    folder = os.path.join(job.get("folder") or default_folder(), WATCHABLE_DIR,
                          safe_name(entry.get("title") or "Video",
                                    fallback="Video"))
    os.makedirs(folder, exist_ok=True)

    while True:
        if job_id in _cancelled:
            torrent_engine.release(info_hash, force=True)
            return ""
        if job_id in _paused:
            # Held, not stopped: the torrent stays added, so the pieces
            # already fetched are still there when this is resumed. The
            # job's state was set to PAUSED by pause() - returning ""
            # here must not be read as a failure, which is why the
            # queue checks _paused before it judges the result.
            return ""
        state = torrent_engine.file_progress(info_hash, index=wanted_index)
        if not state:
            return ""
        _update(job_id, progress=round(state.get("fraction") or 0.0, 4),
                detail=f"{(state.get('rate') or 0)/1e6:.1f} MB/s · "
                       f"{state.get('peers', 0)} peers")
        if state.get("finished"):
            break
        time.sleep(1.5)

    source = state.get("path")
    # The owner's scheme, not the release's own name - see saved_name.
    # The extension is the release's, because that is the part that says
    # what the file actually is.
    extension = os.path.splitext(source)[1] or ".mkv"
    number = episode_tag(episode)
    target = os.path.join(folder, saved_name(
        entry.get("title") or "Video",
        number=number,
        # A season number only means anything beside an episode: a film
        # that happens to carry one is still just the film.
        season=season_tag(season) if number else None,
        fallback="Video") + extension)
    _update(job_id, detail="Saving...")
    # Copy rather than move: the engine may still be serving this file
    # to a player, and pulling it out from under mpv mid-frame is a
    # crash rather than a tidy-up. Retried once - the engine can still
    # be flushing its last pieces - and a copy that still fails fails
    # the *job*: the old fallback reported Done pointing into the
    # engine's temp cache, which trim_cache later deletes, i.e. a
    # download that quietly ceases to exist.
    import shutil
    for attempt in (1, 2):
        try:
            shutil.copy2(source, target)
            break
        except Exception:
            if attempt == 2:
                raise RuntimeError("The finished file could not be saved "
                                   "into the download folder.")
            time.sleep(2.0)

    chosen = job.get("subtitle")
    if chosen:
        _update(job_id, detail="Fetching subtitle...")
        try:
            text = subtitles.fetch(chosen, net.deadline_in(30))
            if text:
                stem = os.path.splitext(target)[0]
                suffix = "ass" if str(chosen.get("format", "")).lower() in ("ass", "ssa") else "srt"
                # `.ar.` so players label the track Arabic and pick it up
                # automatically beside the video.
                with open(f"{stem}.ar.{suffix}", "w", encoding="utf-8") as handle:
                    handle.write(text)
        except Exception:
            pass                 # a missing subtitle must not fail the video
    return target


# Pages in flight at once for one chapter - see the note in _run_chapter
# for the measurement, and net.MAX_IDLE_PER_HOST for why six.
CHAPTER_PAGE_WORKERS = 6


def _fetch_page(url, headers) -> bytes:
    """One page's bytes, or an exception - the caller records which."""
    request = urllib.request.Request(url, headers=headers)
    deadline = net.deadline_in(30)
    with net.urlopen(request, timeout=20) as response:
        # net.MAX_IMAGE_BYTES, not the API ceiling - see the note there
        # for the chapter this was measured on and the page it was
        # dropping.
        return net.read_bytes(response, deadline, net.MAX_IMAGE_BYTES)


def _run_chapter(job) -> str:
    from . import chapter_source

    chapter = job.get("chapter") or {}
    entry = job.get("entry") or {}
    job_id = job["id"]

    _update(job_id, detail="Reading chapter...")
    payload = chapter_source.chapter_pages(chapter, deadline=net.deadline_in(40))
    pages = (payload or {}).get("pages") or []
    if not pages:
        return ""
    headers = dict((payload or {}).get("headers") or {})
    headers.setdefault("User-Agent", _UA)

    # The per-title subfolder goes through the same safe_name as the file
    # in it, so it is underscored to match ("Kingdom_WAN", not
    # "Kingdom (WAN)"). A title downloaded under the old spelling keeps
    # its old folder - nothing here renames anything - so a series read
    # across the change ends up with chapters in two folders. Worth it
    # for one consistent scheme; renaming his files is not this code's
    # to do.
    folder = os.path.join(job.get("folder") or default_folder(),
                          safe_name(entry.get("title") or "Manga"))
    os.makedirs(folder, exist_ok=True)
    # The owner's scheme (see saved_name). A chapter carries no season,
    # so that part is simply absent rather than written empty.
    target = os.path.join(folder, saved_name(
        entry.get("title") or "Manga",
        number=chapter_tag(chapter.get("number")),
        fallback="Manga") + ".cbz")

    # **What is already in the archive is not fetched again, and this
    # loop honours a hold.** Neither was true, and the two halves are one
    # defect: `_run_chapter` checked `_cancelled` and never `_paused`,
    # and opened the archive `"w"`, which truncates it on every run.
    # Measured 22 August 2026 against the real function with the network
    # stubbed, a 20-page chapter paused after page 8: the job went on to
    # fetch **20 of 20** pages - Pause did nothing at all - and the next
    # run started again at page 1 and re-fetched all twenty. The video
    # path has kept this contract all along (see the `_paused` branch in
    # _run_video); this is the reading path catching up to it.
    existing = set()
    mode = "w"
    if os.path.exists(target):
        try:
            with zipfile.ZipFile(target) as archive:
                for name in archive.namelist():
                    stem = os.path.splitext(name)[0]
                    if stem.isdigit():
                        existing.add(int(stem))
            mode = "a"
        except (OSError, zipfile.BadZipFile):
            # Torn by a kill rather than closed by a pause. Start it
            # again rather than appending to rubble - a cbz a reader
            # cannot open is worse than one page re-downloaded.
            existing = set()
            mode = "w"

    # Pages this run could not fetch. Reported at the end rather than
    # swallowed: a chapter that saved 6 of 7 pages is not a success, and
    # saying nothing about it is how a truncated .cbz reached the owner.
    dropped = []
    # **The pages are fetched six at a time, and written in order.**
    # The owner's ask, 2 September 2026 (roadmap #14), was for downloads
    # at the line's speed rather than the source's; for a chapter the
    # browser is no answer (the site refuses a page without the chapter
    # as Referer - see chapter_source.chapter_pages), and the real cost
    # was this loop asking for one image at a time. Measured on Kingdom
    # (WAN) chapter 886 on 3asq, 21 pages, 35.7MB: **5.95s and 10.73s**
    # serial on two runs, **2.87s and 2.66s** with six in flight - the
    # same pages, the same bytes, and one connection per worker kept
    # alive by net's pool (MAX_IDLE_PER_HOST is six, which is where the
    # width comes from). Re-measured 3 September 2026, same chapter,
    # fetch time after the page list: serial 4.22s and 4.52s, six wide
    # 3.42s and 2.32s - the site was faster that day and the gain
    # smaller (1.2-1.9x against 2.1-4.0x), so the width buys most when
    # the source is slowest, which is the case it exists for. Total
    # including the page list: 4.98/5.28s serial, 4.10/3.04s parallel,
    # 35.73MB either way. The same runs also named the page every run
    # was dropping: page 21 of that chapter is over net.MAX_IMAGE_BYTES
    # and is refused by read_bytes, serial or not - a cap question for
    # net, not a fetch one. Writes stay on this thread, in page order,
    # because a zip is appended sequentially and a reader wants the
    # pages numbered as they came; a pause or cancel stops handing out
    # new fetches (cancel_futures) and the ones in flight finish into
    # nothing.
    missing = [(index, url) for index, url in enumerate(pages, 1)
               if index not in existing]
    # **Not a `with`: the executor's __exit__ joins every running fetch**,
    # so a pause or cancel held the one download worker until the six
    # in-flight 2MB pages had finished arriving (review, 3 September
    # 2026). The shutdown in the finally below hands out nothing new and
    # lets the reads in flight drain on their own threads while the
    # queue moves on to the next job.
    pool = ThreadPoolExecutor(max_workers=CHAPTER_PAGE_WORKERS,
                              thread_name_prefix="chapter-page")
    with zipfile.ZipFile(target, mode, zipfile.ZIP_DEFLATED) as archive:
        futures = {index: pool.submit(_fetch_page, url, headers)
                   for index, url in missing}
        try:
            for index, url in enumerate(pages, 1):
                if job_id in _cancelled:
                    break
                if job_id in _paused:
                    # Held, not stopped - the same contract _run_video
                    # keeps. `with` closes the archive on the way out,
                    # so the pages already written stay readable and
                    # the next run appends to them. pause() has already
                    # set the state, so returning "" here must not be
                    # read as a failure - the queue checks `_paused`
                    # before it judges the result.
                    return ""
                future = futures.get(index)
                if future is not None:
                    try:
                        data = future.result()
                    except Exception:
                        # A missing page must not lose the chapter - but
                        # it must not be silent either. This `continue`
                        # is why the size cap in net went unnoticed: the
                        # .cbz simply came out short and nothing
                        # anywhere said so.
                        dropped.append(index)
                        logs.exception(f"chapter page {index} could not "
                                       f"be downloaded: {url}")
                        continue
                    extension = os.path.splitext(url.split("?")[0])[1].lower() or ".jpg"
                    # Zero-padded so readers show pages in order rather
                    # than 1, 10, 11, 2 - the classic cbz mistake.
                    archive.writestr(f"{index:03d}{extension}", data)
                # Advanced for a page that was already there too, so a
                # resumed chapter shows where it actually is instead of
                # crawling up from zero again.
                _update(job_id, progress=round(index / len(pages), 4),
                        detail=f"page {index} of {len(pages)}")
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

    if job_id in _cancelled:
        try:
            os.remove(target)
        except OSError:
            pass
        return ""
    if dropped:
        # Kept, because a chapter missing one page is still worth having
        # - but named, so "it saved" and "it saved everything" are not
        # the same word on screen.
        _update(job_id, detail=f"saved without {len(dropped)} of "
                               f"{len(pages)} pages")
    return target
