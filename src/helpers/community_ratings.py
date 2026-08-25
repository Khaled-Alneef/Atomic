"""What people using Atomic thought of an episode or a chapter.

**The owner's ask, 25 August 2026:** *"next to the imdb rating on each of
ep/ch lists pages, add Atomic Users Rating, make write and read the
ratings from github"*.

## Where it lives, and why there

A branch of the app's own repository - `ratings` - holding one JSON file
per title under `ratings/<key>.json`. A branch rather than a folder on
`development`, because `main` carries released snapshots and
`development` carries source: neither should gain a commit every time
somebody rates an episode, and a release snapshot must not ship other
people's opinions. Nothing on that branch is ever merged anywhere.

One file per *title*, not per episode: the details page needs a whole
season's scores at once, and per-episode files would be one request per
row. Per-title also means two people rating different titles never
collide.

## Reading costs nothing and needs nobody's permission

`raw.githubusercontent.com` serves that branch publicly - measured 25
August 2026, HTTP 200 with no credential of any kind - so every install
can show the scores whether or not its owner has ever pasted a token.
A 404 is the normal answer for a title nobody has rated and reads as an
empty result, never an error.

## Writing goes through a proxy, so nobody has to hold a token

The GitHub contents API needs write access, and a token for it **cannot
be shipped inside the exe**. Measured 25 August 2026 rather than
assumed: the TMDB token bundled in Atomic.exe comes back out of the
archive by name in 0ms with PyInstaller's own reader, and Atomic.exe is
committed to `main` at every release - so a GitHub PAT put there would
be published in a public repository and revoked by secret scanning
within minutes, breaking ratings for everyone rather than for whoever
extracted it.

So the token lives on a small endpoint of the owner's instead
(`tools/ratings-worker/worker.js`, a Cloudflare Worker) and the app
carries only its URL, which is not a secret. `DEFAULT_PROXY` is that
URL; with it set, an ordinary copy of Atomic can rate with nothing
pasted anywhere, which is the whole point.

A token pasted in Settings still works and is the fallback - it is what
the owner tests with, and what keeps ratings working if the endpoint is
ever taken down. With neither, the rating control is drawn read-only and
says so rather than failing when pressed.

**The path is built from a sanitised key and nothing else**, on both
sides. A write token can reach a whole repository, so `_safe_key`
reduces an id to `[a-z0-9_-]` before it is ever put in a URL - a title
carrying `../` must not be able to reach `src/` - and the worker checks
the same shape again before it touches anything.

## Who voted

A random id made once per install (`app_settings.get_voter_id`). It
exists only so that rating something twice replaces the first score
instead of stacking a second one; it carries no name, no account and
nothing derived from the machine.

Everything here fails soft: a flaky connection means no community score
beside the IMDb one, never an error dialog and never a blocked page.
"""

import base64
import hashlib
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import app_settings, logs, net, storage

_UA = "Atomic/1.0"
DEFAULT_TIMEOUT = 8

# **Where a rating is posted so that anybody can leave one.**
#
# Filled in once the worker in tools/ratings-worker/ is deployed; a URL
# is not a secret and shipping one is safe, which is exactly what
# shipping the token would not be - measured 25 August 2026: the TMDB
# token bundled in Atomic.exe comes back out of the archive by name in
# 0ms, and Atomic.exe is committed to `main` at every release, so a
# GitHub PAT put there would be published and auto-revoked by secret
# scanning within minutes.
#
# Empty means no proxy: writing then needs a token the owner pasted, and
# without one the app says so instead of failing when pressed.
DEFAULT_PROXY = ""

# Where the store lives. Overridable in settings so it can be pointed at
# a fork or a private mirror without a new build - see `_repo`/`_branch`.
DEFAULT_REPO = "Khaled-Alneef/Atomic"
DEFAULT_BRANCH = "ratings"
FOLDER = "ratings"

RAW_ROOT = "https://raw.githubusercontent.com"
API_ROOT = "https://api.github.com"

# How long a fetched file is believed. Short, because the whole point is
# that somebody else's rating shows up - but not so short that flipping
# between seasons re-fetches. raw.githubusercontent has a CDN cache of
# its own (~5 minutes), so anything under that would buy nothing anyway.
TTL_SECONDS = 15 * 60
# A title nobody has rated is the common case and answers 404. Remembered
# for a while so an unrated series does not cost a request per visit.
MISS_TTL_SECONDS = 60 * 60

# The scale, matching the one IMDb is printed on so the two numbers beside
# each other mean the same thing.
MIN_SCORE = 1
MAX_SCORE = 10

_CACHE_VERSION = 1
_memory = {}
_lock = threading.RLock()
_KEY_RE = re.compile(r"[^a-z0-9_-]+")


# ---------------------------------------------------------------- keys

def _safe_key(text: str) -> str:
    """A key reduced to what may appear in a path. See the module note:
    the write token can reach the whole repository, so this is the only
    thing standing between a title's name and `src/`."""
    return _KEY_RE.sub("-", str(text or "").strip().lower()).strip("-")[:80]


def key_for(entry) -> str:
    """The file a title's ratings live in.

    An IMDb id where there is one - it is stable, shared between every
    install, and already what the rest of the app keys video titles on.
    Otherwise the normalised title, hashed: reading titles have no such
    id, and hashing keeps a filename out of a URL while still agreeing
    between two people reading the same series."""
    entry = entry or {}
    imdb_id = _safe_key(entry.get("imdb_id") or "")
    if imdb_id.startswith("tt"):
        return imdb_id
    title = str(entry.get("title") or "").strip().lower()
    if not title:
        return ""
    try:
        from . import title_match
        title = title_match.normalize(title) or title
    except Exception:
        pass
    return "t-" + hashlib.sha1(title.encode("utf-8", "replace")).hexdigest()[:16]


def episode_item(season, episode) -> str:
    """The bucket one episode's votes sit in - "1x5"."""
    try:
        return f"{int(season or 1)}x{int(episode)}"
    except (TypeError, ValueError):
        return ""


def chapter_item(number) -> str:
    """The bucket one chapter's votes sit in. `%g` so 247 and 247.0 are
    the same bucket while 247.5 keeps its half."""
    try:
        return f"c{float(number):g}"
    except (TypeError, ValueError):
        return ""


# ------------------------------------------------------------- settings

def _repo() -> str:
    return (app_settings.get_ratings_repo() or DEFAULT_REPO).strip("/ ")


def _branch() -> str:
    return app_settings.get_ratings_branch() or DEFAULT_BRANCH


def _token() -> str:
    return app_settings.get_api_key("github")


def _proxy() -> str:
    return (app_settings.get_ratings_proxy() or DEFAULT_PROXY).strip().rstrip("/")


def can_rate() -> bool:
    """Whether this install can *add* a rating. Reading never needs
    this.

    True as soon as there is a proxy to post to, whether or not the user
    has a token of their own - which is the point of having one."""
    return bool(_proxy() or _token())


# ---------------------------------------------------------------- cache

def _cache_dir():
    path = storage.DATA_DIR / "community_ratings"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _cache_path(key):
    return _cache_dir() / f"{key}.json"


def _load_cached(key):
    with _lock:
        record = _memory.get(key)
    if record is not None:
        return record
    try:
        path = _cache_path(key)
        if not path.is_file():
            return None
        record = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return None
    if not isinstance(record, dict) or record.get("v") != _CACHE_VERSION:
        return None
    with _lock:
        _memory[key] = record
    return record


def _store_cached(key, record):
    with _lock:
        _memory[key] = record
    try:
        _cache_path(key).write_text(json.dumps(record), encoding="utf-8")
    except Exception:
        pass        # a cache that cannot be written is not a failure


def _fresh(record) -> bool:
    if not record:
        return False
    ttl = MISS_TTL_SECONDS if record.get("missing") else TTL_SECONDS
    return (time.time() - float(record.get("at") or 0)) < ttl


# ----------------------------------------------------------- the scores

def summarise(document) -> dict:
    """`{item: {"score": 8.4, "votes": 12}}` out of a stored file.

    Scores are averaged and rounded to one decimal, which is the
    precision IMDb prints beside it; a bucket with no usable vote is
    left out entirely rather than reported as zero."""
    out = {}
    items = (document or {}).get("items")
    if not isinstance(items, dict):
        return out
    for item, bucket in items.items():
        votes = (bucket or {}).get("votes")
        if not isinstance(votes, dict):
            continue
        scores = []
        for vote in votes.values():
            try:
                score = float((vote or {}).get("score"))
            except (TypeError, ValueError):
                continue
            if MIN_SCORE <= score <= MAX_SCORE:
                scores.append(score)
        if scores:
            out[str(item)] = {"score": round(sum(scores) / len(scores), 1),
                              "votes": len(scores)}
    return out


def cached(entry) -> dict:
    """What is already known for this title, without asking anybody.
    `{}` when nothing has been fetched yet - which is not the same as
    "nobody has rated it", and the caller should start `fetch` either
    way."""
    key = key_for(entry)
    if not key:
        return {}
    record = _load_cached(key)
    return dict(record.get("summary") or {}) if record else {}


def needs_fetch(entry) -> bool:
    key = key_for(entry)
    if not key:
        return False
    return not _fresh(_load_cached(key))


def fetch(entry, timeout: float = DEFAULT_TIMEOUT) -> dict:
    """This title's community scores, from GitHub or from the cache.

    Never raises. Call it from a worker thread: it is one HTTPS GET of a
    file that is usually a few hundred bytes, and a miss is a 404."""
    key = key_for(entry)
    if not key:
        return {}
    record = _load_cached(key)
    if _fresh(record):
        return dict(record.get("summary") or {})
    document, found = _read_raw(key, timeout)
    summary = summarise(document) if found else {}
    _store_cached(key, {"v": _CACHE_VERSION, "at": time.time(),
                        "missing": not found, "summary": summary,
                        "title": str((entry or {}).get("title") or "")})
    return summary


def _read_raw(key, timeout):
    """(document, found). A 404 is "nobody has rated this yet" and is a
    perfectly ordinary answer, so it is not logged as a failure."""
    url = (f"{RAW_ROOT}/{_repo()}/{_branch()}/{FOLDER}/"
           f"{urllib.parse.quote(key)}.json")
    request = urllib.request.Request(url, headers={
        "User-Agent": _UA,
        # The CDN will happily serve a copy from before somebody else's
        # vote; asking it not to costs nothing and this file is tiny.
        "Cache-Control": "no-cache",
    })
    try:
        deadline = net.deadline_in(timeout)
        with net.urlopen(request, timeout=timeout) as response:
            body = net.read_text(response, deadline)
        return json.loads(body), True
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return {}, False
        logs.info(f"community ratings: {url} answered {error.code}")
    except Exception:
        pass        # offline, DNS, a torn read - no scores this time
    return {}, False


# ---------------------------------------------------------------- write

def rate(entry, item: str, score, timeout: float = DEFAULT_TIMEOUT):
    """Record this install's score for one episode or chapter.

    Returns `(ok, message)`. Never raises. Call it from a worker thread -
    it is a GET and a PUT against api.github.com.

    A second rating from the same install *replaces* the first, which is
    what the voter id exists for."""
    key = key_for(entry)
    item = str(item or "")
    if not key or not item:
        return False, "This title has nothing to key a rating on."
    try:
        score = int(round(float(score)))
    except (TypeError, ValueError):
        return False, "That is not a score."
    if not MIN_SCORE <= score <= MAX_SCORE:
        return False, f"A score runs from {MIN_SCORE} to {MAX_SCORE}."
    voter = app_settings.get_voter_id()
    # **The proxy first, and for most installs it is the only route.**
    # It holds the write token on the owner's own endpoint rather than
    # inside the binary, so an ordinary copy of Atomic can leave a rating
    # with nothing pasted anywhere. A token, where one *has* been pasted,
    # stays as the fallback - it is what the owner tests with, and what
    # works if the proxy is ever taken down.
    proxy = _proxy()
    if proxy:
        ok, message = _rate_via_proxy(proxy, key, item, score, voter,
                                      entry, timeout)
        if ok or not _token():
            return ok, message
        # A proxy that answered badly while a token is available is worth
        # one direct attempt rather than a refusal.
    token = _token()
    if not token:
        return False, ("Ratings cannot be sent from this copy yet - there "
                       "is no rating service configured.")
    # **Two attempts, and the second is not optimism.** The contents API
    # rejects a write whose `sha` is not the file's current one, which is
    # exactly what happens when somebody else rated the same title
    # between this read and this write. Re-reading and re-merging is the
    # whole fix; a third attempt would only be a busier race.
    for attempt in range(2):
        try:
            document, sha = _read_api(key, token, timeout)
            items = document.setdefault("items", {})
            bucket = items.setdefault(item, {})
            votes = bucket.setdefault("votes", {})
            votes[voter] = {"score": score, "at": storage.now_iso()}
            document.setdefault("key", key)
            document.setdefault("title", str((entry or {}).get("title") or ""))
            document.setdefault(
                "kind", "reading" if item.startswith("c") else "video")
            ok = _write_api(key, document, sha, token, timeout,
                            f"Rate {document.get('title') or key} {item}")
            if ok:
                _store_cached(key, {"v": _CACHE_VERSION, "at": time.time(),
                                    "missing": False,
                                    "summary": summarise(document),
                                    "title": document.get("title") or ""})
                remember_my_rating(entry, item, score)
                return True, f"Rated {score}/{MAX_SCORE}"
        except Exception:
            logs.exception("community rating write failed")
            break
        if attempt == 0:
            time.sleep(0.4)
    return False, "That rating could not be saved. Try again in a moment."


def _rate_via_proxy(proxy, key, item, score, voter, entry, timeout):
    """Post one rating to the write proxy. Returns `(ok, message)`.

    Deliberately tells the caller nothing about the endpoint: whether the
    token behind it is healthy is not this app's business, and a failure
    here reads the same as any other failed write."""
    payload = json.dumps({
        "key": key, "item": item, "score": score, "voter": voter,
        "title": str((entry or {}).get("title") or "")[:200],
        "kind": "reading" if item.startswith("c") else "video",
    }).encode("utf-8")
    request = urllib.request.Request(
        proxy, data=payload, method="POST",
        headers={"User-Agent": _UA, "Content-Type": "application/json"})
    try:
        deadline = net.deadline_in(timeout)
        with net.urlopen(request, timeout=timeout) as response:
            body = json.loads(net.read_text(response, deadline) or "{}")
    except urllib.error.HTTPError as error:
        logs.info(f"community ratings: proxy answered {error.code}")
        return False, "That rating could not be saved. Try again in a moment."
    except Exception:
        return False, "That rating could not be sent - check the connection."
    if body.get("ok"):
        remember_my_rating(entry, item, score)
        return True, f"Rated {score}/{MAX_SCORE}"
    logs.info(f"community ratings: proxy refused - {body.get('error')!r}")
    return False, "That rating was refused."


def _api_headers(token):
    return {
        "User-Agent": _UA,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Authorization": f"Bearer {token}",
    }


def _content_url(key):
    return (f"{API_ROOT}/repos/{_repo()}/contents/{FOLDER}/"
            f"{urllib.parse.quote(key)}.json")


def _read_api(key, token, timeout):
    """(document, sha) for the file as GitHub currently holds it.

    Read through the API rather than the CDN because this one is about to
    be written: the raw host serves a cached copy, and merging into a
    stale one would drop whoever voted in the last five minutes."""
    url = f"{_content_url(key)}?ref={urllib.parse.quote(_branch())}"
    request = urllib.request.Request(url, headers=_api_headers(token))
    try:
        deadline = net.deadline_in(timeout)
        with net.urlopen(request, timeout=timeout) as response:
            body = json.loads(net.read_text(response, deadline))
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return {}, None         # first rating for this title
        raise
    raw = base64.b64decode((body.get("content") or "").encode("ascii"))
    try:
        document = json.loads(raw.decode("utf-8"))
    except Exception:
        document = {}               # unreadable: start again rather than lose the write
    return (document if isinstance(document, dict) else {}), body.get("sha")


def _write_api(key, document, sha, token, timeout, message) -> bool:
    payload = {
        "message": message,
        "branch": _branch(),
        "content": base64.b64encode(
            json.dumps(document, indent=1, sort_keys=True).encode("utf-8")
        ).decode("ascii"),
    }
    if sha:
        payload["sha"] = sha
    request = urllib.request.Request(
        _content_url(key), data=json.dumps(payload).encode("utf-8"),
        headers={**_api_headers(token), "Content-Type": "application/json"},
        method="PUT")
    try:
        deadline = net.deadline_in(timeout)
        with net.urlopen(request, timeout=timeout) as response:
            net.read_text(response, deadline)
        return True
    except urllib.error.HTTPError as error:
        if error.code in (409, 422):
            return False            # somebody else wrote first; caller retries
        logs.info(f"community ratings: write answered {error.code}")
        return False


# This install's own scores, so a row can show what *you* gave it - the
# community average beside IMDb answers a different question, and after
# voting there is otherwise nothing at all to show until the CDN catches
# up several minutes later.
MINE_FILE = "my_ratings.json"


def _mine() -> dict:
    found = storage.load(MINE_FILE, {})
    return found if isinstance(found, dict) else {}


def my_rating(entry, item):
    """This install's own score for one episode or chapter, or None."""
    key = key_for(entry)
    if not key or not item:
        return None
    try:
        return int(_mine().get(f"{key}|{item}"))
    except (TypeError, ValueError):
        return None


def remember_my_rating(entry, item, score):
    """Keep a score locally the moment it is accepted. Never raises."""
    key = key_for(entry)
    if not key or not item:
        return
    try:
        data = _mine()
        data[f"{key}|{item}"] = int(score)
        storage.save(MINE_FILE, data)
    except Exception:
        logs.exception("could not remember a rating")


def clear_cache():
    with _lock:
        _memory.clear()
    try:
        for path in _cache_dir().glob("*.json"):
            path.unlink(missing_ok=True)
    except Exception:
        pass
