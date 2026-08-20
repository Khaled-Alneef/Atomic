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

from . import net, storage

JOBS_FILE = "downloads.json"

# Job states. Kept as plain strings: they are written to disk and read
# back by a later version, and an enum would only make that brittle.
QUEUED = "queued"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
CANCELLED = "cancelled"

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")

_lock = threading.RLock()
_jobs = None
_worker = None
_cancelled = set()


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
        pass


def cancel(job_id):
    _cancelled.add(job_id)
    _update(job_id, state=CANCELLED)


def clear_finished():
    with _lock:
        jobs = _load()
        jobs[:] = [j for j in jobs if j.get("state") in (QUEUED, RUNNING)]
        _save()


def default_folder() -> str:
    """Where downloads land unless the user picks somewhere else."""
    base = os.path.join(os.path.expanduser("~"), "Downloads", "Atomic")
    return base


_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_name(text: str, fallback: str = "download") -> str:
    """A filename Windows will accept, without losing the Arabic in it -
    Arabic characters are perfectly legal in a filename and stripping
    them would turn a title into an empty string."""
    cleaned = _UNSAFE.sub("_", str(text or "")).strip(" .")
    return cleaned[:120] or fallback


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
        if running:
            row["state"] = RUNNING
        elif waiting:
            row["state"] = QUEUED
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


_DUB_RE = re.compile(r"dual[\s._-]?audio|multi[\s._-]?audio|\bdub(?:bed)?\b"
                     r"|english\s*audio|\beng\b.{0,12}\baudio\b", re.I)


def _order_by_audio(candidates, audio):
    """Reorder releases toward the asked-for audio, without dropping any.

    "en" floats releases whose names say dual-audio/dub; "jp" (the
    original) floats the ones that do not. Reordered rather than
    filtered: release names lie by omission all the time, and an empty
    queue because no name said "dual audio" would be worse than the
    wrong-order fallback these are."""
    if audio not in ("en", "jp"):
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
    pack_key = (entry.get("id") or entry.get("title"), season)
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
        ready = streams.prepare_fastest(candidates, season=season,
                                        episode=episode)
        if not ready or not ready.get("info_hash"):
            return ""
        info_hash = ready["info_hash"]
    else:
        # add() re-picks the file for this episode on the held torrent;
        # metadata is already there, so this returns immediately.
        if not torrent_engine.add(info_hash, season=season, episode=episode):
            _season_packs.pop(pack_key, None)
            return _run_video(job)
    if season and episode:
        _season_packs[pack_key] = info_hash
    torrent_engine.download_whole(info_hash)
    _prefetch_group_siblings(job, info_hash)

    folder = job.get("folder") or default_folder()
    os.makedirs(folder, exist_ok=True)

    while True:
        if job_id in _cancelled:
            torrent_engine.release(info_hash)
            return ""
        state = torrent_engine.file_progress(info_hash)
        if not state:
            return ""
        _update(job_id, progress=round(state.get("fraction") or 0.0, 4),
                detail=f"{(state.get('rate') or 0)/1e6:.1f} MB/s · "
                       f"{state.get('peers', 0)} peers")
        if state.get("finished"):
            break
        time.sleep(1.5)

    source = state.get("path")
    target = os.path.join(folder, safe_name(os.path.basename(source)))
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

    folder = os.path.join(job.get("folder") or default_folder(),
                          safe_name(entry.get("title") or "Manga"))
    os.makedirs(folder, exist_ok=True)
    target = os.path.join(folder,
                          safe_name(f"{entry.get('title')} - {chapter.get('number')}") + ".cbz")

    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for index, url in enumerate(pages, 1):
            if job_id in _cancelled:
                break
            request = urllib.request.Request(url, headers=headers)
            deadline = net.deadline_in(30)
            try:
                with urllib.request.urlopen(request, timeout=20) as response:
                    data = net.read_bytes(response, deadline)
            except Exception:
                continue         # a missing page must not lose the chapter
            extension = os.path.splitext(url.split("?")[0])[1].lower() or ".jpg"
            # Zero-padded so readers show pages in order rather than
            # 1, 10, 11, 2 - the classic cbz mistake.
            archive.writestr(f"{index:03d}{extension}", data)
            _update(job_id, progress=round(index / len(pages), 4),
                    detail=f"page {index} of {len(pages)}")

    if job_id in _cancelled:
        try:
            os.remove(target)
        except OSError:
            pass
        return ""
    return target
