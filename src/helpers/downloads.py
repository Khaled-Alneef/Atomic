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


def _update(job_id, **fields):
    with _lock:
        for job in _load():
            if job.get("id") == job_id:
                job.update(fields)
                _save()
                return dict(job)
    return None


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
                  subtitle=None, folder=None) -> dict:
    """One episode (or a film, with season/episode left out)."""
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
        "folder": folder or default_folder(),
    })


def queue_season(entry, *, season=None, episodes=(), quality=None,
                 subtitle=None, folder=None) -> list:
    """A whole season, as one job per episode.

    One job each rather than a single job for the lot: episodes come
    from different releases with different swarms, and a season that
    reports one failure because its ninth episode has no seeders would
    hide the eight that worked."""
    return [queue_episode(entry, season=season, episode=number,
                          quality=quality, subtitle=subtitle, folder=folder)
            for number in episodes]


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


def _run_video(job) -> str:
    from . import streams, subtitles, torrent_engine

    entry = job.get("entry") or {}
    season, episode = job.get("season"), job.get("episode")
    job_id = job["id"]

    _update(job_id, detail="Looking for a source...")
    found = streams.find_streams(entry, season=season, episode=episode,
                                 deadline=net.deadline_in(40))
    wanted = job.get("quality")
    ordered = streams.matching_quality(found, wanted) if wanted else []
    candidates = [s for s in (ordered or found) if s.get("info_hash")]
    if not candidates:
        return ""

    _update(job_id, detail="Connecting to peers...")
    ready = streams.prepare_fastest(candidates, season=season, episode=episode)
    if not ready or not ready.get("info_hash"):
        return ""
    info_hash = ready["info_hash"]
    torrent_engine.download_whole(info_hash)

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
    try:
        # Copy rather than move: the engine may still be serving this
        # file to a player, and pulling it out from under mpv mid-frame
        # is a crash rather than a tidy-up.
        import shutil
        shutil.copy2(source, target)
    except Exception:
        target = source          # leave it where it is rather than lose it

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
