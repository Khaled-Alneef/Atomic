"""Finding something playable for a tracked entry, for the in-app player.

Three routes, asked **at the same time** rather than one after another:

  1. **Addons, by IMDb id.** The addon protocol is a plain GET -
     `<addon>/stream/{type}/{id}.json` where id is `tt1234567` for a
     film or `tt1234567:1:5` for series S1E5 - answering with a list of
     streams that each carry either a direct `url` or a torrent
     `infoHash`. These are public HTTP endpoints and need nothing
     installed; the table of them is DEFAULT_ADDONS below, and anything
     extra can be added by URL.
  2. **Indexers, by title** (helpers/indexers.py). Anime Tosho and
     SubsPlease, which answer for the fansub releases no id-keyed index
     carries. See that module for which of the sites the owner named
     are in it and, for each one that is not, the measurement that
     kept it out.
  3. **Any open site.** `resolve_page(url)` takes *any* watch/episode
     page and digs out the media URL. That is the path for the sites
     the user has configured in anime_sites, and for anything else they
     paste in.

**Nothing here needs Stremio.** The account-addon import and the local
streaming server both used to live in this file and are gone at the
owner's ask - a source may need an API key, never another installed
application (.claude/rules/integrations.md). Torrents play through
torrent_engine.py, which is libtorrent inside this app. The Stremio
*account* in Settings is still read, but only by helpers/stremio.py, and
only for what was watched elsewhere.

Everything fails soft to an empty list, and the whole chain is bounded
by one deadline rather than a timeout per request (three dead hosts at
6s each is not a 6s bound - see .claude/rules/integrations.md).

DRM is not a failure to be retried. Netflix and Crunchyroll are
Widevine-protected and there is no CDM in this app; reading Crunchyroll
directly is a settled dead end here, tried and removed twice. Those come
back as a `"drm"` stream carrying the service name so the player can say
so and offer the browser, which is the honest answer rather than a
spinner that never resolves.
"""

import concurrent.futures
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from . import app_settings, net, storage

# Imported defensively for the same reason chapter_source is in
# reader.py: a lookup with no indexers is a shorter list, not a
# player that will not open.
try:
    from . import indexers
except Exception:  # pragma: no cover
    indexers = None

# The arc-name season resolver (helpers/anime_identity). Imported the
# same defensive way: a build where it fails to import is one that
# filters on numbers only, not one whose player will not open.
try:
    from . import anime_identity
except Exception:  # pragma: no cover
    anime_identity = None

SITES_FILE = "stream_addons.json"

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")

# Per-request ceiling. The deadline passed in bounds the whole chain;
# this only stops one request eating all of it.
DEFAULT_TIMEOUT = 6


def _headers(referer: str = None) -> dict:
    headers = {"User-Agent": _UA, "Accept": "*/*"}
    if referer:
        parts = urllib.parse.urlsplit(referer)
        headers["Referer"] = referer
        headers["Origin"] = f"{parts.scheme}://{parts.netloc}"
    return headers


def _get(url: str, timeout: float, referer: str = None,
         max_bytes: int = net.MAX_RESPONSE_BYTES) -> str:
    request = urllib.request.Request(url, headers=_headers(referer))
    deadline = net.deadline_in(timeout)
    with net.urlopen(request, timeout=timeout) as response:
        return net.read_text(response, deadline, max_bytes)


def _get_json(url: str, timeout: float):
    return json.loads(_get(url, timeout))


# ---------------------------------------------------------------- addons

def _normalize_addon(url: str) -> str:
    """An addon's base URL, with the manifest filename trimmed.

    Stremio addons are shared *as* their manifest URL - that is what a
    user copies - but every other endpoint hangs off the directory, so
    keeping the filename produces `/manifest.json/stream/...` and a 404
    that looks like the addon being down."""
    url = (url or "").strip()
    if not url:
        return ""
    if url.startswith("stremio://"):
        url = "https://" + url[len("stremio://"):]
    return url[:-len("/manifest.json")] if url.endswith("/manifest.json") else url.rstrip("/")


def _load() -> list:
    return storage.load(SITES_FILE, [])


def list_addons() -> list:
    return _load()


def add_addon(url: str, timeout: int = DEFAULT_TIMEOUT):
    """Add an addon by URL, validating it by reading its manifest.

    Validated rather than trusted: an addon that does not answer a
    manifest will not answer a stream either, and finding that out at
    playback time means an empty list with nothing saying why."""
    base = _normalize_addon(url)
    if not base:
        return None
    try:
        manifest = _get_json(base + "/manifest.json", timeout)
    except Exception:
        return None
    if not isinstance(manifest, dict) or "id" not in manifest:
        return None
    addons = _load()
    for existing in addons:
        if existing.get("base_url") == base:
            return existing
    addon = {
        "id": uuid.uuid4().hex[:12],
        "name": manifest.get("name") or base,
        "base_url": base,
        "types": manifest.get("types") or [],
        "from_account": False,
    }
    addons.append(addon)
    storage.save(SITES_FILE, addons)
    return addon


def remove_addon(addon_id: str):
    addons = [a for a in _load() if a.get("id") != addon_id]
    storage.save(SITES_FILE, addons)


def _supports_streams(manifest: dict) -> bool:
    """Whether this addon answers /stream at all.

    Most of a user's installed collection does not - Cinemeta is
    metadata, OpenSubtitles is subtitles - and asking those for streams
    is a guaranteed 404 per entry, per lookup. The manifest says so;
    read it instead of finding out the expensive way."""
    resources = manifest.get("resources") or []
    for resource in resources:
        if resource == "stream":
            return True
        if isinstance(resource, dict) and resource.get("name") == "stream":
            return True
    return False


# Public, keyless addons that answer /stream, each measured against a
# real anime episode (Bleach TYBW S1E1) before being put here - the same
# rule the subtitle sources follow. Nothing goes in this table that was
# not seen to return streams.
#
#   TorrentsDB        50 streams   (more than Torrentio, and the reason
#                                   this table exists at all)
#   Torrentio         33 streams
#   Torrentio Anime   23 streams   same addon, pinned to the anime
#                                  trackers - nyaa/tokyotosho/anidex -
#                                  so a title indexed there but not on
#                                  the general trackers still resolves
#
# Measured and deliberately left out: MediaFusion answered 0, Jackettio
# 1, Comet 403s without its own configuration, StremThru 404s, and
# AIOStreams/Anime Kitsu declare no stream resource at all (Kitsu is a
# catalogue, not a source).
DEFAULT_ADDONS = (
    ("TorrentsDB", "https://torrentsdb.com",
     ("movie", "series", "anime", "other")),
    ("Torrentio", "https://torrentio.strem.fun",
     ("movie", "series", "anime", "other")),
    ("Torrentio Anime", "https://torrentio.strem.fun/providers=nyaasi,tokyotosho,anidex",
     ("movie", "series", "anime", "other")),
)


# The standard public trackers, used only for a stream whose addon
# supplied none of its own (see prepare). These are the long-standing
# open announce URLs that every torrent client ships defaults from -
# nothing title-specific, just a way for the swarm to be discoverable at
# all when the addon did not say where to look.
DEFAULT_TRACKERS = (
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.tracker.cl:1337/announce",
    "udp://open.demonii.com:1337/announce",
    "udp://tracker.torrent.eu.org:451/announce",
    "udp://exodus.desync.com:6969/announce",
    "udp://tracker.openbittorrent.com:6969/announce",
    "udp://explodie.org:6969/announce",
    "udp://tracker1.bt.moack.co.kr:80/announce",
)

SEEDED_FILE = "stream_addons_seeded.json"


def seed_default_addons() -> list:
    """Add the measured defaults, once ever.

    Runs alongside importing the user's own Stremio collection rather
    than instead of it: their collection is what they chose, these are
    what makes a title resolve when their collection has nothing for it.

    "Once ever" is recorded separately from the addon list rather than
    inferred from it, because those are different questions. Inferring
    it - "this default isn't present, so add it" - means an addon the
    user deliberately removed comes back on the very next lookup, and
    there is no way to get rid of it. A version marker also lets a later
    release add a newly-measured default without re-adding the ones
    already dismissed."""
    seeded = storage.load(SEEDED_FILE, {})
    if not isinstance(seeded, dict):
        seeded = {}
    already = set(seeded.get("urls") or [])
    addons = _load()
    known = {a.get("base_url") for a in addons}
    added = []
    for name, base_url, types in DEFAULT_ADDONS:
        base = _normalize_addon(base_url)
        if base in known or base in already:
            continue
        addons.append({"id": uuid.uuid4().hex[:12], "name": name,
                       "base_url": base, "types": list(types),
                       "from_account": False, "built_in": True})
        added.append(name)
        known.add(base)
        already.add(base)
    if added:
        storage.save(SITES_FILE, addons)
        storage.save(SEEDED_FILE, {"urls": sorted(already)})
    return added


# **import_account_addons() used to live here and is gone.** It read the
# user's own Stremio addon collection over their account key, which made
# what plays here depend on a second application's configuration - and
# it was measured adding nothing the table above does not already carry.
# The addons that answer are public HTTP endpoints; they are listed
# above, and anything else goes in by URL through add_addon().


# **Press a source, see a picture: the whole path, measured 22 August
# 2026 on the owner's connection and his own entries, cold cache, six
# titles across film, anime and series.** Phases are find_streams (the
# source list) / prepare_fastest (the race) / mpv url-to-first-frame.
#
#                          before            after
#   Re:Zero      S01E01     6.56s           11.54s
#   Frieren      S01E02    12.63s           14.14s
#   Demon Slayer S01E05    18.38s           22.39s
#   House Dragon S01E05    23.41s            8.93s
#   Bleach TYBW  S01E12     6.72s            8.38s
#   Top Gun Maverick       12.64s           14.84s
#            mean          13.39s           13.37s
#
# **The mean did not move, and saying otherwise would be reading noise.**
# Repeating a single title on unchanged code spans most of the range the
# table does: House of the Dragon S01E05 came back at 8.93, 11.90, 23.41
# and 36.32 seconds, and once did not draw a frame at all inside 90s;
# Demon Slayer S01E05 over six runs ran 12.70-42.25s. Which releases the
# race happens to hit dominates everything else, so a six-sample suite
# cannot see a change of a few seconds, and the owner's "about twelve
# seconds" is the middle of a very wide distribution rather than a
# steady cost.
#
# What *is* repeatable is where the time goes:
#
#   find_streams      0.4-4.3s, and off the critical path anyway -
#                     on_partial puts rows on screen at 0.3-0.5s
#   prepare_fastest   3.7-5.9s when the first candidates are alive,
#                     11-22s when they are not
#   mpv -> picture    0.03-15.4s
#
# So the owner's "~12 seconds" is not one slow step; it is a race that
# costs about five seconds when it guesses right and twenty when it does
# not, because a lane spends its whole budget on a release that turns
# out to have no swarm. **Getting to five seconds means knowing which
# release is alive before trying it, and nothing in a torrent listing
# tells you that** - the seeder count is measured wrong often enough to
# be no help (a 2081-seeder film release that never completed a piece,
# and a 701-seeder House of the Dragon release that answered no-peers).
# The next thing worth measuring is an early-out: a release with zero
# peers and zero connect candidates a couple of seconds after its
# metadata lands is dead, and giving up on it then rather than at the
# end of DATA_WAIT would cover more of the list in the same wall clock.
# That has *not* been measured, so it is not in.
#
# What did move, and is not a swarm accident, is the stall: see
# torrent_engine._apply_windows and _Torrent.refresh_windows for the
# window that used to empty out and take the entire peer set with it -
# 36.2s at zero peers on House of the Dragon S01E05, one run that never
# drew a frame at all in 90s, and the same Demon Slayer release opening
# in 15.4s instead of 36.6s once the swarm stopped being thrown away.
#
# **And the other thing that moved, measured 22 August 2026, was the
# race feeding on the wrong episode.** For an arc-named anime the wrong
# arcs sort to the *top*, because they are the well-seeded ones - Demon
# Slayer S01E01 returned the Hashira Training arc (season 5) at 327
# seeders and the Entertainment District arc (season 3) at 290 above
# every real season-1 release, so the race spent its first lanes fetching
# metadata for episodes nobody asked for. Dropping them (see
# _drop_wrong_season and helpers/anime_identity) took prepare_fastest on
# that title from 22.13s to 9.24 / 6.69 / 6.34s across three runs, each
# winning a real S01E01 file - not because any release got faster but
# because the race stopped trying the wrong ones first. This is the same
# lesson as the stall: the wins here are in *what* the race attempts, not
# in waiting on it differently.
#
# The two waits one release is given, and they are deliberately short.
#
# **Waiting longer never rescued a dead release; it only delayed a live
# one.** A swarm that is going to answer answers quickly - the trackers
# are in the magnet and the first piece follows within seconds - so a
# 45s metadata wait plus a 25s data wait bought nothing but 70 seconds
# per dud, and with three duds ahead of a good release that was most of
# the "sourcing takes ages" the owner reported. The rolling race below
# is what makes short waits safe: a release given up on is replaced
# immediately by the next one, so the cost of being wrong is one more
# attempt rather than one more minute.
METADATA_TIMEOUT = 12.0
DATA_WAIT = 12.0

# The budgets a release gets while there are still untried candidates
# behind it in the race.
#
# Measured 21 August 2026 against the owner's connection, on a real
# 2160p lookup (Solo Leveling S01E03, five 2160p torrents): a live
# release published its metadata in 1.66, 2.06 and 3.46 seconds, and the
# winner's first piece landed 1.61s after that - the whole
# prepare_fastest took 3.67s. A dead release, by contrast, costs exactly
# the budget and nothing else: timed at 12.04s against a 12.0s
# metadata_timeout, 7.02s against 7.0, 5.01s against 5.0.
#
# So a lane sitting on a dead release for twelve seconds is a lane not
# trying the next candidate, and halving the budget while a queue exists
# covers twice as much of the list in the same wall clock. The *last*
# candidates keep the full twelve: by then there is nothing better to
# move on to, and a slow-but-live release is worth the wait when it is
# the only thing left. 6.0 rather than 4.0 because the slowest live
# metadata measured was 3.46s and one sample is not a distribution.
# **4.0, down from 6.0, measured again 22 August 2026.** Every live
# release timed on the owner's connection publishes its metadata inside
# 3.46s (1.07-2.48s in the runs taken that day) and lands its first
# piece inside 2.93s (2.01s that day), so four seconds covers every one
# of them with margin - while a dead release costs precisely its budget
# and nothing else. On Bleach S01E12 ten consecutive releases answered
# `no-peers` at 6.9-8.1s each; at these budgets that walk is a third
# shorter, which is a third less of the loading screen the owner is
# looking at. The *last* candidates still get the full twelve
# (METADATA_TIMEOUT/DATA_WAIT): by then there is nothing better to move
# on to, and a slow-but-live release is worth waiting for when it is
# all that is left.
QUEUED_METADATA_TIMEOUT = 6.0
QUEUED_DATA_WAIT = 6.0

# How many releases are in flight at once, and how long the whole race
# may take. Six, up from four: each one is mostly idle while it waits
# for a tracker, so width is nearly free, and the bandwidth only
# matters once pieces start arriving - which is the moment there is a
# winner and the rest are released. Two more lanes is two fewer serial
# retries when the top of the list is dead.
RACE_WIDTH = 6
RACE_TIMEOUT = 45.0

# **What a source the *user* picked by hand is allowed to cost before
# the rest are tried anyway.**
#
# This is the owner's "sourcing still takes 15 to 30 seconds", and the
# cause is not the fan-out: with `auto_pick_source` off - which is how
# their settings are set - pressing an episode opens the source picker,
# and choosing a row plays it with `solo=True`. Solo skips the race on
# purpose (six releases racing split one connection, and whichever
# answered first would silently override the row that was chosen), but
# it also meant the pick got the *full* budgets: 12s of metadata plus
# 12s of data before `_try_next_source` was even reached. One dead pick
# is therefore 24 seconds of loading screen, which is exactly the range
# reported.
#
# Measured on the owner's connection, so these are not guesses: a live
# release publishes metadata in 0.83-2.61s and its first piece lands
# 2.93s later; a dead one costs precisely whatever budget it is given.
# 4s each therefore keeps every live release measured and cuts a dead
# pick from 24s to 8s before the rest of the list is raced.
#
# The pick still gets first refusal, which is what solo was for - it is
# just no longer allowed to spend half a minute failing.
SOLO_METADATA_TIMEOUT = 4.0
SOLO_DATA_WAIT = 4.0


# --------------------------------------------------------- the engine
#
# **Stremio's local streaming server used to live here and is gone.**
# It worked - it was measured serving 560 KB/s - but it only works on a
# machine that has Stremio Desktop installed, and nothing in Atomic may
# require another application to be present
# (.claude/rules/integrations.md, revised by the owner). Keeping it as
# "just a fallback" was not free either: every torrent paid an
# `ensure_local_server()` wait of up to twelve seconds, plus a six-second
# peer poll against a server that might not exist, before the built-in
# engine's first byte was even asked for.
#
# torrent_engine.py (libtorrent, inside the app) is now the only route,
# and a build that cannot import it says "no-engine" out loud rather
# than depending on what else the user happens to have installed.


def _resume_seconds(start_at, duration):
    """Where playback will begin, in seconds into the file, or None.

    `duration`, when the caller has it, is only a sanity check - a
    stored position past the end of the file is a stale resume record,
    not a place to fetch. Both come straight from the resume record
    (`player.load_resume` keeps position and duration), and anything
    implausible answers None, which means "prime nothing", i.e. exactly
    what happened before this existed."""
    try:
        start_at = float(start_at or 0)
        duration = float(duration or 0)
    except (TypeError, ValueError):
        return None
    if start_at <= 0:
        return None
    if duration and start_at >= duration:
        return None
    return start_at


def prepare(stream: dict, *, season=None, episode=None,
            metadata_timeout: float = METADATA_TIMEOUT,
            data_wait: float = DATA_WAIT,
            start_at=None, duration=None) -> dict:
    """Make a torrent stream actually playable, immediately before playing.

    `start_at`/`duration` (seconds) say playback is going to *resume*
    part-way in rather than start at the beginning, so the engine primes
    that offset as well as the head. Both optional and both default to
    None, in which case nothing changes.

    **This is the step whose absence made every torrent stream time out.**
    A bare infoHash gives the server nothing to find peers with: DHT
    alone is slow enough to look like a hang, and the first attempt sat
    through twelve 20s reads without a byte. Handing it the addon's own
    `sources` - 27 trackers on a typical Torrentio result - turned the
    same stream into 262KB in 0.8s.

    Done here rather than in find_streams on purpose: a lookup returns
    30-plus streams and creating every one of them would announce to
    every tracker for torrents the user is never going to watch. One
    stream, at the moment it is chosen.

    Returns the stream with its url filled in, or with `reason` set to
    something the player can say out loud.

    **There is one engine now.** Stremio's local streaming server used to
    sit behind this as a fallback for a build without libtorrent, and it
    is gone at the owner's ask: nothing in Atomic may need another
    application installed to work (.claude/rules/integrations.md). A
    build that cannot import libtorrent now says so, instead of quietly
    depending on whether Stremio happens to be on the machine - which
    also cost every torrent an `ensure_local_server()` wait of up to
    twelve seconds before the first byte was even asked for."""
    stream = dict(stream or {})
    if stream.get("kind") != "torrent":
        return stream
    info_hash = (stream.get("info_hash") or "").lower()
    if not info_hash:
        return stream

    prepared = _prepare_with_own_engine(stream, info_hash, season, episode,
                                       metadata_timeout, data_wait,
                                       _resume_seconds(start_at, duration))
    if prepared is not None:
        return prepared
    stream["url"] = None
    stream["reason"] = "no-engine"
    return stream


def _prepare_with_own_engine(stream, info_hash, season, episode,
                             metadata_timeout=None, data_wait=None,
                             resume_at=None):
    """Stream through Atomic's own libtorrent engine.

    Returns the finished stream, or None to mean "this build has no
    engine at all" - deliberately distinct from returning a stream with
    no url, which means "the engine tried and this release is dead".

    Trackers are supplied from `DEFAULT_TRACKERS` when the indexer gave
    none of its own; that is not optional, see the note in `prepare`."""
    try:
        from . import torrent_engine
    except Exception:
        return None
    if not torrent_engine.available():
        return None

    trackers = list(stream.get("sources") or [])
    if not trackers:
        trackers = [f"tracker:{url}" for url in DEFAULT_TRACKERS]
    try:
        added = torrent_engine.add(
            info_hash, trackers=trackers, season=season, episode=episode,
            file_index=stream.get("file_index"),
            metadata_timeout=(METADATA_TIMEOUT if metadata_timeout is None
                              else metadata_timeout),
            start_at=resume_at)
    except Exception:
        added = None
    if not added:
        stream["url"] = None
        stream["reason"] = "no-metadata"
        return stream
    # **Does this release actually hold the episode asked for?** Known
    # the moment the metadata lands, and answered by the file names
    # rather than by whoever listed the release - see
    # torrent_engine._pick_file. A pack whose files are numbered
    # absolutely used to fall through to "the largest video", which on
    # the owner's Frieren S01E02 served **episode 4** (measured 22
    # August 2026, both SubsPlease batches in the list).
    #
    # Checked here rather than after the data wait because it costs
    # nothing and saves everything: the lane gives up now and takes the
    # next candidate, instead of spending six seconds fetching an
    # episode nobody asked for.
    if season and episode:
        try:
            serves = torrent_engine.episode_file(added)
        except Exception:
            serves = ""
        if serves is None:
            _release_quietly(info_hash)
            stream["url"] = None
            stream["reason"] = "wrong-episode"
            return stream
        stream["file_name"] = serves
    # **The first piece and the container's seek index, waited on
    # together.** mpv's first read of a fresh torrent is the opening of
    # the file and its *second* is a seek to 100% of it (Matroska writes
    # its Cues at the end), so both have to be there before a url is
    # worth handing over - but they arrive in parallel, and waiting for
    # one and then the other only added the two waits together: 3.14s +
    # 6.01s measured on Frieren S01E02. See torrent_engine.await_start.
    # Missing the index is not a failure; missing the data is.
    try:
        got_data, _got_index = torrent_engine.await_start(
            info_hash, data_wait=(DATA_WAIT if data_wait is None
                                  else data_wait),
            # Resuming needs the index for a different reason than
            # playback does - the resume offset is *read out of it* -
            # so it is worth a longer wait when there is one.
            index_wait=(torrent_engine.RESUME_INDEX_WAIT
                        if resume_at is not None else None))
    except Exception:
        got_data = False
    if not got_data:
        # Nothing arriving. Release it rather than leaving a dead
        # torrent announcing in the background while the player moves on
        # to the next source.
        try:
            torrent_engine.release(info_hash)
        except Exception:
            pass
        stream["url"] = None
        stream["reason"] = "no-peers"
        return stream
    # Only now does the resume offset become worth wanting: it is a
    # guess (position/duration), and until the url exists the only
    # things that matter are the first piece and the index. See
    # torrent_engine.arm_start_band.
    if resume_at is not None:
        try:
            torrent_engine.arm_start_band(info_hash)
            # And wait for it, briefly. Handing the url over the moment
            # the index lands means mpv draws a frame at 0 and asks for
            # the seek a quarter of a second later, with nothing
            # fetched - measured, the seek then cost 3.5s of frozen
            # picture *after* the "Resumed From" toast. Better spent
            # here, on a loading screen the viewer is already watching.
            torrent_engine.await_start_band(info_hash)
        except Exception:
            pass
    stream["url"] = torrent_engine.stream_url(info_hash)
    stream["engine"] = "atomic"
    stream["reason"] = "" if stream["url"] else "engine-failed"
    return stream




def prepare_fastest(candidates, *, season=None, episode=None,
                    width: int = RACE_WIDTH, timeout: float = RACE_TIMEOUT,
                    failed=None, start_at=None, duration=None):
    """Start several releases at once; play whichever delivers data
    first, and keep replacing the failures until one does.

    **Trying them one at a time is most of the wait.** A release's
    advertised seeder count says nothing about whether its swarm will
    actually answer - measured, a film's top result claimed 2081 seeders
    and never completed a piece, and finding that out cost 45 seconds
    before the next one was even started.

    **And a fixed batch of three is most of the rest.** The first
    version started three and waited for the batch; three dead ones cost
    the whole timeout with nothing else attempted, and the caller then
    started over serially on the first of them again. This one is a
    rolling race: `width` workers pull from the candidate list, and a
    worker whose release fails takes the next candidate immediately. So
    a title whose first eight releases are dead is eight short attempts
    rather than three long ones followed by a repeat.

    The losers are released as soon as there is a winner - a torrent
    left added keeps announcing and keeps taking bandwidth from the one
    actually playing.

    **A hand-picked release is raced too, and that is a reversal.**
    Picking one by hand used to skip this entirely (`solo`), so the
    choice could not be overridden and six releases could not split one
    connection. Measured 22 August 2026, that cost far more than it
    saved: the top-seeded 1080p release for House of the Dragon S01E05
    answered `no-peers` after its full data wait, and only then was the
    rest of the list tried - the owner's "23 seconds from pressing the
    episode until it plays".

    Giving the pick a head start before the others joined was tried and
    is *worse still*: 18.54s against 5.55s for the same title with the
    field racing from the first moment. So the pick goes in as the first
    candidate and everything of its resolution races beside it - it gets
    the first lane and the best chance, and it can no longer hold the
    loading screen alone.

    **`start_at`/`duration`** are passed straight through to `prepare`:
    they say playback is going to resume part-way in, so the winner
    primes that offset as well as the head. Optional, and absent they
    change nothing.

    **`failed`, if given, is filled with the info hash of every release
    this race proved dead.** Without it the caller learns about exactly
    one - the index it asked for - so the *next* attempt raced the same
    twenty releases again, at the same twelve seconds each. Measured 22
    August 2026 on House of the Dragon S02E06: two attempts, 52.6s to a
    playable url, the second of them re-walking what the first had
    already disproved.

    Returns the prepared stream, or None if none of them started."""
    live = [c for c in (candidates or []) if c.get("info_hash")]
    if not live:
        return None

    import threading
    winner = {}
    started = []
    done = threading.Event()
    lock = threading.Lock()
    cursor = [0]
    deadline = time.monotonic() + timeout

    def worker():
        try:
            _race_lane()
        finally:
            # **The last lane out ends the race.** Without this the call
            # below waited the whole RACE_TIMEOUT even when every
            # candidate had already been tried and failed - measured 22
            # August 2026 on two episodes that had only two 1080p
            # releases each, both dead: 45.0s of loading screen for an
            # answer that was known in about twelve. With it, "none of
            # these work" is said as soon as it is true.
            with lock:
                running[0] -= 1
                spent = running[0] <= 0
            if spent:
                done.set()

    def _race_lane():
        while not done.is_set() and time.monotonic() < deadline:
            with lock:
                if cursor[0] >= len(live):
                    return
                candidate = live[cursor[0]]
                cursor[0] += 1
                started.append(candidate.get("info_hash"))
                # Anything still untried behind this one? If so this
                # release gets the short budget - see
                # QUEUED_METADATA_TIMEOUT for the numbers behind that.
                queued = cursor[0] < len(live)
            try:
                got = prepare(
                    candidate, season=season, episode=episode,
                    metadata_timeout=(QUEUED_METADATA_TIMEOUT if queued
                                      else METADATA_TIMEOUT),
                    data_wait=QUEUED_DATA_WAIT if queued else DATA_WAIT,
                    start_at=start_at, duration=duration)
            except Exception:
                continue
            if not got.get("url"):
                _release_quietly(candidate.get("info_hash"))
                if failed is not None:
                    # A plain list append under the GIL - the caller
                    # reads it only after this call returns.
                    failed.append(candidate.get("info_hash"))
                continue
            with lock:
                if winner:
                    # Someone else already won; give the swarm back.
                    _release_quietly(candidate.get("info_hash"))
                    return
                winner.update(got)
            done.set()
            return

    lanes = max(1, min(width, len(live)))
    running = [lanes]
    threads = [threading.Thread(target=worker, daemon=True)
               for _ in range(lanes)]
    for thread in threads:
        thread.start()
    done.wait(timeout)
    with lock:
        result = dict(winner) if winner else None
        attempted = list(started)
    if result:
        for info_hash in attempted:
            if info_hash != result.get("info_hash"):
                _release_quietly(info_hash)
    return result


def _release_quietly(info_hash):
    if not info_hash:
        return
    try:
        from . import torrent_engine
        torrent_engine.release(info_hash)
    except Exception:
        pass

# ------------------------------------------------------------- streams

_QUALITY_RE = re.compile(r"\b(2160p|1440p|1080p|720p|480p|360p|4k)\b", re.I)


def _quality_of(text: str) -> str:
    match = _QUALITY_RE.search(text or "")
    return match.group(1).lower() if match else ""


def _quality_rank(quality: str) -> int:
    order = {"2160p": 5, "4k": 5, "1440p": 4, "1080p": 3, "720p": 2,
             "480p": 1, "360p": 0}
    return order.get((quality or "").lower(), -1)


# How many seeders count as "as good as it gets". Past this, more peers
# stop meaningfully changing how fast playback starts.
_SEEDER_CEILING = 200


def _default_pick_key(stream, preferred):
    """The sort key deciding what plays by default. Smaller sorts first.

    Three rules, in this order:

    **The resolution the user asked for wins outright.** Settings carries
    a preferred resolution (1080p unless changed) and anything matching
    it is ranked above everything else. A picker that quietly starts
    something other than the chosen resolution is not a preference, it
    is a suggestion.

    **Within the preferred resolution, the most seeders lead** - the
    owner's ask ("the one that has more seeds, not the size"), replacing
    the smallest-real-file rule that briefly lived here: a big swarm is
    what actually starts fast and downloads fast, and the small file it
    replaced as the tiebreak still wins between equals. Raw seeder
    counts, not capped at _SEEDER_CEILING - an ordering has to be able
    to tell 250 from 2000 even though both are "plenty". A release whose
    title states no usable size keeps sorting after the sized ones when
    seeders tie, rather than gaming the tiebreak.

    **Everywhere else, resolution then seeders** - the old rule, kept:
    sorting on resolution alone once picked a 2160p release with 21
    seeders over a 1080p one with 236, which cold is minutes of black
    screen. "best" as a preference restores highest-resolution-first."""
    drm = 1 if stream.get("kind") == "drm" else 0
    # A torrent has no url until prepare() runs, so "has a url" would
    # sort every torrent to the bottom as if it were broken. What makes
    # one unplayable is having no way to resolve it at all.
    resolvable = 0 if (stream.get("url") or stream.get("info_hash")) else 1
    quality = (stream.get("quality") or "").lower()
    if quality == "4k":
        quality = "2160p"
    raw_seeders = int(stream.get("seeders") or 0)
    seeders = min(raw_seeders, _SEEDER_CEILING)
    if preferred != "best" and quality == preferred:
        size = int(stream.get("size_bytes") or 0)
        size_key = size if size >= _MIN_REAL_SIZE else float("inf")
        return (drm, resolvable, 0, -raw_seeders, size_key)
    return (drm, resolvable, 1, -_quality_rank(quality), -seeders)


def _stremio_id(entry, season=None, episode=None) -> str:
    imdb_id = (entry or {}).get("imdb_id") or ""
    if not imdb_id:
        return ""
    if season and episode:
        return f"{imdb_id}:{int(season)}:{int(episode)}"
    if episode:
        return f"{imdb_id}:1:{int(episode)}"
    return imdb_id


def _stream_from_addon(item: dict, addon_name: str):
    """One entry of an addon's `streams[]`, as this app's shape."""
    title = (item.get("title") or item.get("name") or "").replace("\n", " ").strip()
    quality = _quality_of(title) or _quality_of(item.get("name") or "")
    url = item.get("url")
    if url:
        kind = "hls" if ".m3u8" in url.lower() else "direct"
        headers = {}
        # Addons pass proxy headers through behaviorHints; a stream that
        # needs a Referer and does not get one is a 403 at play time.
        hints = item.get("behaviorHints") or {}
        proxy = hints.get("proxyHeaders") or {}
        if isinstance(proxy.get("request"), dict):
            headers = {str(k): str(v) for k, v in proxy["request"].items()}
        return {"title": title or addon_name, "url": url, "kind": kind,
                "source": addon_name, "quality": quality, "reason": "",
                "headers": headers}
    info_hash = item.get("infoHash")
    if info_hash:
        # No url yet, and that is not a failure - `prepare()` fills it in
        # at the moment this stream is chosen, because the engine has to
        # be given the trackers before it can serve a byte.
        return {"title": title or addon_name, "url": None, "kind": "torrent",
                "source": addon_name, "quality": quality,
                "reason": "",
                "headers": {},
                "info_hash": str(info_hash).lower(),
                "file_index": item.get("fileIdx"),
                # The trackers to find peers through. Without these the
                # stream resolves to a URL that never returns anything.
                "sources": item.get("sources") or [],
                "seeders": _seeders_of(title),
                # Bytes, from the title's own '💾 1.42 GB' line - what
                # the source lists show and what the default pick now
                # sorts by within the preferred resolution.
                "size_bytes": _size_of(title)}
    return None


_SEEDERS_RE = re.compile(r"(?:👤|seeders?[:\s]*)\s*(\d+)", re.I)


def _seeders_of(title: str) -> int:
    """Torrentio writes the seeder count into the stream title. It is the
    best available predictor of whether something will actually play, so
    it rides along and sorts."""
    match = _SEEDERS_RE.search(title or "")
    return int(match.group(1)) if match else 0


_SIZE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(GB|GiB|MB|MiB)\b", re.I)

# Below this an "episode" is a sample or a corrupt listing, not a file
# worth preferring for being small - it sorts as size-unknown instead.
_MIN_REAL_SIZE = 30 * 1024 * 1024


def _size_of(title: str) -> int:
    """The release's file size in bytes, parsed out of the title
    (Torrentio/TorrentsDB write it there as '💾 1.42 GB'), or 0 when the
    title does not state one."""
    match = _SIZE_RE.search(title or "")
    if not match:
        return 0
    value = float(match.group(1))
    unit = match.group(2).lower()
    return int(value * (1024 ** 3 if unit.startswith("g") else 1024 ** 2))


def format_size(size_bytes) -> str:
    """'1.4 GB' / '807 MB' for the source lists, '' when unknown."""
    size_bytes = int(size_bytes or 0)
    if size_bytes <= 0:
        return ""
    if size_bytes >= 1000 * 1024 * 1024:
        return f"{size_bytes / 1024 ** 3:.1f} GB"
    return f"{size_bytes / 1024 ** 2:.0f} MB"


def _drm_stream(entry):
    """The honest answer for a service that cannot be decrypted here."""
    from . import anime_sites
    provider = anime_sites.streaming_provider(entry.get("site_id")) if entry.get("site_id") else None
    if not provider:
        return None
    return {"title": provider.title(), "url": entry.get("url") or "", "kind": "drm",
            "source": provider, "quality": "", "reason": provider, "headers": {}}


def episode_fallbacks(season, episode, entry=None):
    """Other ways to ask for the same episode, in order of confidence.

    The indexers do not agree with the tracker about season numbering,
    and asking for the one combination that does not exist returns an
    empty list rather than an error. Measured on Bleach: the entry sits
    at S04E04 so the player asked for **S04E05 and got 0 streams**,
    while S01E05 returned 35 and S02E01 returned 30 - the show is there,
    under different numbers. Reporting "no source" for that is wrong; it
    is a numbering mismatch, not a missing title.

    Absolute numbering is the usual reason: anime is very often indexed
    as one long season, so a later season's episode 5 may be filed under
    season 1.

    **Only the season is ever guessed, never the episode number.** An
    earlier version also tried `episode - 1` and `S01E01`, which turned a
    request for an episode that does not exist into *a different episode
    playing silently* - pressing Next on the last episode of season 4
    asked for S04E05, fell through to S01E05, and played that. Substituting
    a different episode is a worse answer than admitting there is none,
    so the ladder now only ever offers the same episode under another
    season numbering."""
    if not episode:
        return []
    season = int(season or 1)
    episode = int(episode)
    tried, order = set(), []

    def add(s, e):
        if e < 1 or (s, e) in tried:
            return
        tried.add((s, e))
        order.append((s, e))

    add(season, episode)
    if season > 1:
        # Same episode number under season 1 - what an absolutely-numbered
        # index looks like for a continuing show. Only ever the *same*
        # episode number: a different one is a different episode.
        add(1, episode)
    return order


# How many source requests run at once inside one lookup. Three addons
# plus two indexers is the whole population today, and one worker per
# request is what turns a lookup from the sum of their times into the
# slowest of them - measured on the owner's real entries, 3.7-4.5s
# serial against 1.3-2.6s here, on the same answers.
LOOKUP_WORKERS = 6


# Where an addon stops printing the release and starts printing its own
# opinion of it. Torrentio and TorrentsDB both append a stats line -
# `👤 371 💾 705.17 MB ⚙️ nyaa` - and TorrentsDB puts an episode
# annotation in front of it: `📅 S01E01`.
#
# **That annotation is the mapping being checked, so it cannot be part of
# the evidence.** Measured 22 August 2026, asking TorrentsDB for
# tt5607616:1:1 (Re:Zero S01E01):
#
#   '[FLE] Re ZERO ... - S04E01 (WEB 1080p HEVC E-AC-3) [Dual Audio] |
#    Re: Zero Kara Hajimeru Isekai Seikatsu Season 4 Episode 1 | Re:ZERO
#    | ReZero 📅 S01E01 👤 371 💾 705.17 MB ⚙️ nyaa'
#
# The release says season 4 twice in its own words and the addon says
# S01E01 at the end. Read whole, that row states seasons {1, 4}, the
# asked-for season is in the set, and the first version of this check
# kept it - eleven of eleven wrong-season rows survived. Cutting at the
# marker leaves the release's own words, which say 4, and it goes.
#
# Everything before the marker is kept, the file path inside the torrent
# included: that path is the addon's claim about which *file* it will
# serve, and it is checked for exactly the same reason.
_ADDON_STATS_RE = re.compile("[\U0001F300-\U0001FAFF⚙⚡⬇]")


def _release_name(title: str) -> str:
    """An addon stream title with the addon's own annotations cut off."""
    match = _ADDON_STATS_RE.search(title or "")
    return (title[:match.start()] if match else (title or "")).strip()


_ENTRY_SEASON_RE = re.compile(r"s(\d{1,3})", re.I)


def _max_known_season(entry) -> int:
    """The highest season number this entry is known to have, or 0.

    Read off `latest_available` ("S04E13"), which the tracker keeps for
    every entry that has a schedule. It is the bound that tells one kind
    of wrong season from another - see _drop_wrong_season."""
    match = _ENTRY_SEASON_RE.search((entry or {}).get("latest_available") or "")
    return int(match.group(1)) if match else 0


def _contiguous_seasons(all_stated) -> int:
    """How far an unbroken run of seasons starting at 1 reaches in what
    a source actually returned. The fallback bound for an entry with no
    `latest_available` - Re:Zero's answer states 1, 2, 3 and 4 and its
    seasons really do run 1-4; Bleach's states 1 and 17, and the gap is
    the point."""
    if 1 not in all_stated:
        return 0
    highest = 1
    while highest + 1 in all_stated:
        highest += 1
    return highest


def _drop_wrong_season(rows, season, episode, entry=None, arc_map=None) -> list:
    """One source's whole answer, with the rows that contradict the
    request taken out.

    **An id-keyed addon can and does answer with the wrong season, and
    that is the owner's report of 22 August 2026** - "many of the sources
    download a diff ep from a diff season". This file's own docstring
    used to say an id cannot resolve to the wrong show; measured against
    the owner's Re:Zero entry (tt5607616) at S01E01, TorrentsDB returned
    `[FLE] ... - S04E01`, `[PMR] ... S03E01-12`, `[neoDESU] ...
    [Season 3]`, `[Yameii] ... - S03`, `[Breeze]/[Sokudo] ... S04 P01`
    and `... Re Zero S02P01` - and the highest-seeded of them sorts near
    the top of the list, which is where both the eye and the default
    pick land. Those addons map an IMDb id onto their own scraped
    release names, and for anime that mapping is wrong often enough to
    matter.

    **But "the row says a different season" is not by itself a mistake,
    and a first version that assumed it was broke Bleach.** Bleach:
    Thousand-Year Blood War is season 1 of its own IMDb id and season 17
    of Bleach, and every index uses the latter: asking Torrentio for
    tt14986406:1:12 returned eleven S17 rows out of sixty-three, among
    them `[LostYears] ... - S17E12`, which is exactly the episode asked
    for, and `[Judas] Bleach (Season 17)` at 236 seeders, which is what
    the race had been winning. Rejecting on the season alone threw all
    eleven away.

    Nor can the answer be trusted to use one numbering: measured on that
    same lookup, **Torrentio's own rows state season 1 eleven times and
    season 17 seven times**, both correctly, because the release groups
    disagree with each other.

    So what separates the two cases is a bound - **how many seasons this
    entry actually has.** The tracker already knows (`latest_available`,
    "S04E04" for Bleach and "S04E13" for Re:Zero): season 3 of a
    four-season entry is that entry's season 3 and a row naming it when
    season 1 was asked for is wrong; season 17 of a four-season entry is
    somebody else's numbering and says nothing. A season past the bound
    is therefore ignored rather than believed, and the row is judged on
    its episode number alone - which still drops an S17E05 row when
    episode 12 was asked for.

    Done per source rather than over the merged list, so a batch handed
    to `on_partial` is already final: rows appearing and then vanishing
    under the pointer would be worse than either answer alone.

    **`arc_map`** is helpers/anime_identity's `{season -> {tokens}}` for
    this franchise, or `{}`. It is what lets this catch the arc-named
    case numbers cannot - "Kimetsu no Yaiba - Hashira Geiko Hen" states
    no season, but "Hashira Geiko" is season 5 in the map, so the row is
    judged as an S5 row and dropped when S1 was asked for. Empty when the
    map is not cached yet (see anime_identity.arc_map), in which case this
    behaves exactly as it did before - the numbers only. Because the map
    comes from TMDB, whose numbering *is* the entry's, an arc-derived
    season is always inside the bound and always trusted.

    Fails open when indexers could not be imported: a shorter list beats
    a wrong episode, but an empty one does not, and without that module
    there is nothing to check with."""
    if indexers is None or not episode or not rows:
        return rows
    try:
        season = int(season or 1)
        arc_map = arc_map or {}
        names = [_release_name(row.get("title")) for row in rows]
        stated = [indexers.stated_seasons(name) for name in names]
        # The arc tokens each name carries, as seasons - empty unless the
        # franchise map is warm and the name names an arc.
        arced = [(anime_identity.seasons_named(name, arc_map)
                  if anime_identity is not None else set())
                 for name in names]
        every = set().union(*stated, *arced) if stated else set()
        bound = max(_max_known_season(entry), _contiguous_seasons(every),
                    season)
        kept = []
        for name, seasons, arc_seasons, row in zip(names, stated, arced, rows):
            # Only seasons inside this entry's own range are evidence
            # about *which* season; the rest are another numbering. Arc
            # seasons are TMDB's, i.e. this entry's own, so they are
            # always in range.
            in_range = {s for s in (seasons | arc_seasons) if s <= bound}
            if indexers.episode_conflict(name, season, episode,
                                         compare_season=bool(in_range),
                                         extra_seasons=arc_seasons):
                continue
            kept.append(row)
        return kept
    except Exception:
        return rows


def _ask_addon(addon, kind, stream_id, deadline, season=None,
               episode=None, entry=None, arc_map=None) -> list:
    """One addon, one numbering. Never raises - it runs on a worker, and
    a dead worker would take the whole lookup's pool with it.

    `season`/`episode` are **the numbering this call asked for**, not the
    entry's own. That distinction is the whole reason they are passed in
    rather than read from the entry: the fallback numbering deliberately
    asks for the same episode under season 1 (see episode_fallbacks), and
    checking its answers against the entry's season would throw away
    precisely the rows it exists to find - Bleach TYBW sits at season 4
    here and every real release of it states S01."""
    timeout = net.step_timeout(deadline, DEFAULT_TIMEOUT)
    if timeout is None:
        return []
    url = (f"{addon['base_url']}/stream/{kind}/"
           f"{urllib.parse.quote(stream_id, safe='')}.json")
    try:
        body = _get_json(url, timeout)
    except Exception:
        return []
    found = []
    for item in (body or {}).get("streams") or []:
        stream = _stream_from_addon(item, addon.get("name") or "addon")
        if stream:
            found.append(stream)
    return _drop_wrong_season(found, season, episode, entry, arc_map)


# How long the fan-out may run before the fallback numbering is given
# whatever is left of the caller's budget.
#
# **Measured, 21 August 2026, the owner's own entries, eight cold runs:**
# every source that was going to answer had answered inside 2.6s -
# Torrentio 0.38-1.34s, TorrentsDB 0.30-0.52s, Torrentio Anime
# 0.37-2.35s, the indexers 0.49-2.57s. The recorded worst case is a
# source answering *nothing* after 12.40s. So ten seconds cuts that tail
# without cutting a real answer, and what remains of the caller's budget
# is left for the fallback numbering - which only runs when nothing
# answered at all, and must not be starved by a source that hung.
FANOUT_BUDGET_S = 10.0


# **There used to be an early return here and it is gone on purpose.**
# The rule was: once every *essential* job has reported and ENOUGH_RESULTS
# releases are in hand, abandon the stragglers. It existed because the
# caller got one list at the end, so every second spent waiting was a
# second of empty screen.
#
# `on_partial` removes that trade entirely - rows are on screen from the
# first source that answers, and a late arrival only ever *adds* to a
# list already being read. What the early return cost, measured on House
# of the Dragon S02E05: the run that broke early returned **62 streams**
# where the run that waited returned **110**, because TorrentsDB's 80
# rows were abandoned a quarter of a second before they landed. Which
# 48 releases the owner is offered should not depend on a race.
#
# So the fan-out now waits for every source, bounded by FANOUT_BUDGET_S
# rather than by a result count.


def _run_all(jobs, deadline, *, on_result=None) -> list:
    """Every job at once, results flattened, nothing raising out.

    `on_result`, if given, is called with the accumulated results each
    time a job reports - that is what lets a caller draw the sources it
    has while the rest are still out. It is called on *this* thread, one
    call at a time, and a callback that raises is swallowed: a caller
    that cannot draw must not kill the lookup.

    A plain ThreadPoolExecutor rather than lookup_pool: that pool is
    shared with the tracker's per-entry background lookups and is
    deliberately only four workers wide, so a page-load backfill would
    sit in front of the one lookup the user is actually watching. This
    pool exists for the length of one lookup and then goes away.

    Not used as a context manager on purpose: `with` shuts the pool down
    waiting, which would block on a straggler past the deadline. Those
    are left to finish into a result nobody reads, bounded by their own
    per-request timeouts."""
    if not jobs:
        return []
    results = []
    workers = min(LOOKUP_WORKERS, len(jobs))
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=workers)
    try:
        futures = [pool.submit(job) for job in jobs]
        # `deadline` is a monotonic timestamp (net.deadline_in), so what
        # is left is simply the difference - floored, because a deadline
        # already past still has to let the completed jobs be collected.
        remaining = None
        if deadline is not None:
            remaining = max(0.1, deadline - time.monotonic())
        try:
            for future in concurrent.futures.as_completed(futures,
                                                          timeout=remaining):
                try:
                    found = future.result() or []
                except Exception:
                    continue        # one dead source is not a dead lookup
                if not found:
                    continue
                results.extend(found)
                if on_result is not None:
                    try:
                        on_result(list(results))
                    except Exception:
                        pass
        except concurrent.futures.TimeoutError:
            pass                    # the deadline is the answer
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return results


# One lookup's answers, kept for a while. Episode release lists barely
# change inside a session, and re-running the whole fan-out to re-learn
# them is most of the wait between pressing Next and hearing sound - so
# the player prefetches the next episode into this the moment the
# current one starts, and Next then costs only the prepare step.
# In memory only, deliberately: torrents live and die by their swarms,
# and yesterday's list served from disk would be stale in the one way
# that matters.
_RESULT_CACHE = {}
_RESULT_CACHE_TTL = 15 * 60.0
_RESULT_CACHE_MAX = 48


def _cache_key(entry, season, episode):
    identity = ((entry or {}).get("imdb_id")
                or ((entry or {}).get("title") or "").strip().lower())
    if not identity:
        return None
    return (identity, (entry or {}).get("type"),
            int(season or 0), int(episode or 0))


def _cache_get(key):
    if key is None:
        return None
    row = _RESULT_CACHE.get(key)
    if not row or time.monotonic() - row[0] > _RESULT_CACHE_TTL:
        _RESULT_CACHE.pop(key, None)
        return None
    # Copies, not the cached dicts themselves: the player swaps prepared
    # streams into the list it holds, and the next reader of this cache
    # must get the unprepared originals.
    return [dict(s) for s in row[1]]


def _cache_put(key, results):
    if key is None:
        return
    playable = [s for s in results if s.get("kind") != "drm"]
    if not playable:
        return          # an empty answer is worth retrying, not keeping
    if len(_RESULT_CACHE) >= _RESULT_CACHE_MAX:
        oldest = min(_RESULT_CACHE, key=lambda k: _RESULT_CACHE[k][0])
        _RESULT_CACHE.pop(oldest, None)
    _RESULT_CACHE[key] = (time.monotonic(), [dict(s) for s in results])


def find_streams(entry, *, season=None, episode=None, deadline=None,
                 on_partial=None) -> list:
    """Everything playable for this entry, best first.

    **Every source is asked at the same time.** This used to walk the
    addon table one host at a time, inside a loop over the episode
    numberings, so a lookup cost the sum of every request and one slow
    host delayed every source behind it. Now the addons and the
    title-keyed indexers all go out together and the deadline bounds the
    lot; only the *fallback* numbering is still sequential, because it
    must not run at all when the real numbering answered (see below).

    **`on_partial` gets the ranked list so far every time a source
    reports**, which is what the owner's "the sourcing is still slow"
    actually was: measured on the owner's entries, the first addon's
    rows are in hand after 0.30-0.78s and the caller was shown nothing
    until the slowest source finished at 1.9-2.6s (and up to the whole
    budget when one hung). Nothing about the fan-out was slow - the
    *reporting* was all-or-nothing. Same shape as subtitles.search, and
    for the same reason. Every batch is the full ranked list so far, so
    a reader just redraws; a batch is never empty, so an empty list is
    always the final answer.

    Answers are cached in memory for a short while (see _RESULT_CACHE),
    which is what makes the player's next-episode prefetch and a
    re-opened episode start without the fan-out at all.

    Never raises and never returns None - an empty list means nothing
    was found, which the player says out loud."""
    cache_key = _cache_key(entry, season, episode)
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    if deadline is None:
        deadline = net.deadline_in(20)
    # The fan-out's own bound. Deliberately not the caller's whole
    # budget: the fallback numbering runs *after* it and would otherwise
    # be starved by one source that hung (see FANOUT_BUDGET_S).
    fanout_deadline = min(deadline, net.deadline_in(FANOUT_BUDGET_S))
    results = []

    drm = _drm_stream(entry or {})
    if drm:
        results.append(drm)

    jobs = []
    imdb_id = (entry or {}).get("imdb_id")
    kind = "movie" if (entry or {}).get("type") == "Movie" else "series"
    # The franchise's arc-name -> season map, **from cache only** - a warm
    # anime returns a dict, everything else (and a cold anime) returns {}
    # and warms in the background for next time. Read once here and passed
    # down so every source's rows are judged against the same map, and so
    # a cold title's partials and final list agree (both unfiltered) -
    # anime_identity.arc_map explains why filtering only from the cache is
    # what keeps a row from appearing then vanishing under the pointer.
    arc_map = (anime_identity.arc_map(entry) if anime_identity is not None
               else {})
    attempts = []
    usable = []
    if imdb_id:
        # A film has one id; an episode has several plausible ones (see
        # episode_fallbacks).
        attempts = (episode_fallbacks(season, episode, entry)
                    if kind == "series" and episode else [None])
        # Seeding is tried on every lookup, not only when the list is
        # empty, and that is a fix rather than a nicety: gating it on
        # "no addons at all" meant a profile that already had addons
        # never got the defaults, so the lookup ran on one of them and a
        # title only TorrentsDB indexes came back empty.
        # seed_default_addons() is itself idempotent and records that it
        # has run, so a default the user deletes stays deleted.
        try:
            seed_default_addons()
        except Exception:
            pass
        usable = [a for a in _load()
                  if not (a.get("types") or []) or kind in (a.get("types") or [])]
        primary = attempts[0] if attempts else None
        stream_id = (imdb_id if primary is None
                     else f"{imdb_id}:{primary[0]}:{primary[1]}")
        jobs += [(lambda a=addon, i=stream_id:
                  _ask_addon(a, kind, i, fanout_deadline,
                             season=primary[0] if primary else None,
                             episode=primary[1] if primary else None,
                             entry=entry, arc_map=arc_map))
                 for addon in usable]

    # The indexers go out in the same breath as the addons rather than
    # after them: they answer by title, so they need no id at all and
    # can be the only thing that answers for an entry Cinemeta never
    # matched. Only for something with an episode or a name to ask for.
    if indexers is not None and (entry or {}).get("title"):
        # This is the one source that answers by *title*, so it reaches
        # fansub releases no id-keyed addon carries. It is also
        # routinely the slowest (0.49-2.57s measured), which is exactly
        # why it must not be the thing the whole list waits behind.
        #
        # **Its rows go through the same season filter the addons' do.**
        # They did not before, which is exactly how the arc-named Demon
        # Slayer rows survived: `indexers.search` matches the *title* to
        # the franchise but says nothing about the arc, so "Kimetsu no
        # Yaiba - Hashira Geiko Hen - 01" (season 5) came back for an
        # S01E01 ask. `_drop_wrong_season` with the arc map is what reads
        # that as season 5 and drops it - see anime_identity.
        jobs.append(lambda: _drop_wrong_season(
            indexers.search(entry, season=season, episode=episode,
                            deadline=fanout_deadline),
            season, episode, entry, arc_map))

    # A saved page URL is worth digging into as well - it is the only
    # route for the sites the user configured themselves.
    page_url = (entry or {}).get("url") or ""
    if page_url.startswith("http") and not drm:
        jobs.append(lambda: resolve_page(page_url, deadline=fanout_deadline))

    # Whatever was already in `results` before the fan-out started - the
    # DRM row for an entry pinned to Netflix or Crunchyroll. It belongs
    # in every partial as well as in the final list: it is the
    # *explanation*, and a batch without it reads as "nothing found".
    before = list(results)

    def on_result(accumulated):
        on_partial(_rank(before + list(accumulated)))

    results.extend(_run_all(jobs, fanout_deadline,
                            on_result=None if on_partial is None else on_result))

    # The fallback numbering, and only if nothing answered. It stays
    # sequential and conditional on purpose: a later guess returns a
    # *different episode* under another season numbering, and running it
    # alongside the real one would quietly mix the two into one list.
    # Counted against what the addons and indexers actually returned,
    # never against an empty list - `results` may already hold a DRM row
    # (an entry pinned to Netflix or Crunchyroll always gets one), and
    # treating that as "already answered" once skipped the whole addon
    # loop, so those entries found no torrents at all.
    #
    # Partials do not change that rule and must not: what makes it safe
    # is that it runs only when *nothing* real answered, in which case
    # no partial was ever emitted either - the fan-out only reports
    # batches it actually has. So a caller drawing partials is never
    # shown the real numbering and the guessed one mixed together.
    playable = [s for s in results if s.get("kind") != "drm"]
    if not playable and len(attempts) > 1:
        fallback = attempts[1]
        stream_id = f"{imdb_id}:{fallback[0]}:{fallback[1]}"
        guessed = _run_all(
            [(lambda a=addon, i=stream_id:
              _ask_addon(a, kind, i, deadline, season=fallback[0],
                         episode=fallback[1], entry=entry))
             for addon in usable], deadline,
            on_result=None if on_partial is None else on_result)
        # **Say that these are a guess.** They were fetched under a
        # numbering nobody confirmed - the same episode number filed
        # under season 1, which is what an absolutely-numbered index
        # looks like for a continuing show. They are still the best
        # answer available (nothing at all answered under the real
        # numbering), but a row that might be a different season should
        # not be indistinguishable from one that cannot be.
        for stream in guessed:
            stream["numbering"] = f"S{fallback[0]:02d}E{fallback[1]:02d}"
            stream["guessed_numbering"] = True
        results.extend(guessed)

    ranked = _rank(results)
    _cache_put(cache_key, ranked)
    return ranked


def _rank(streams: list) -> list:
    """Playable before unplayable, then the default-pick order (see
    _default_pick_key).

    DRM rows sort last but are kept: they are the explanation, and
    dropping them is what turns "Netflix can't be played here" back into
    an empty list with no reason in it."""
    try:
        preferred = app_settings.get_preferred_resolution()
    except Exception:
        preferred = "1080p"

    def key(stream):
        return _default_pick_key(stream, preferred)
    seen, unique = set(), []
    for stream in sorted(streams, key=key):
        marker = stream_key(stream)
        if marker in seen:
            continue
        seen.add(marker)
        unique.append(stream)
    return unique


def stream_key(stream):
    """What makes two rows the same release.

    Used to dedupe a ranked list, and by a caller drawing partials to
    tell which rows of a later batch it has not already got - a batch is
    always the whole list so far, so without this every redraw would be
    a full re-add."""
    stream = stream or {}
    return (stream.get("url") or stream.get("info_hash")
            or (stream.get("kind"), stream.get("title")))


def list_sort_key(stream):
    """How one resolution's releases are ordered in a *visible* source
    list. Smaller sorts first.

    Not _default_pick_key: that one decides what plays and leads with
    the preferred resolution, which says nothing inside a group that is
    already one resolution - and it caps seeders at _SEEDER_CEILING, so
    every release above 200 ties and their printed order is whatever
    the sources happened to return. Measured across the owner's four
    real titles (sixteen resolution groups) that order came out
    descending anyway on the day, the addons being roughly
    seeder-sorted already; what makes this necessary rather than
    belt-and-braces is the player's panel, which appends late-arriving
    sources behind the ranked ones.

    Raw seeders first, then the smaller file between equals (the same
    tiebreak _default_pick_key uses within the preferred resolution, so
    the list agrees with what auto-play would choose), then the title so
    two rows tying on both cannot swap places between redraws - which
    with progressive results would be a list rearranging itself under
    the pointer. A release stating no usable size sorts after the sized
    ones rather than winning on a zero."""
    stream = stream or {}
    size = int(stream.get("size_bytes") or 0)
    return (-int(stream.get("seeders") or 0),
            size if size >= _MIN_REAL_SIZE else float("inf"),
            (stream.get("title") or "").lower())


def has_preferred_resolution(streams) -> bool:
    """Whether this batch already carries the resolution Settings asks
    for - the test for whether a *partial* answer is worth starting
    playback on rather than waiting for the rest of the fan-out.

    Starting on the first partial unconditionally is not free: a batch
    holding only 720p would start 720p, and the 1080p that landed two
    tenths of a second later would sit unwatched for the whole episode.
    Once the preferred resolution is present no later arrival can beat
    it, because resolution wins outright in _default_pick_key.

    "best" answers False on purpose: which resolution is highest is not
    knowable until every source has reported, so that preference
    genuinely has to wait."""
    try:
        preferred = app_settings.get_preferred_resolution()
    except Exception:
        preferred = "1080p"
    if not preferred or preferred == "best":
        return False
    return preferred in qualities([s for s in streams or []
                                   if s.get("kind") != "drm"])


def qualities(streams) -> list:
    """The distinct resolutions present, best first - for the player's
    resolution picker. "4k" and "2160p" are the same thing said two ways
    and must not appear as two entries."""
    canonical, seen = [], set()
    for stream in streams or []:
        quality = (stream.get("quality") or "").lower()
        if quality == "4k":
            quality = "2160p"
        if not quality or quality in seen:
            continue
        seen.add(quality)
        canonical.append(quality)
    return sorted(canonical, key=lambda q: -_quality_rank(q))


def matching_quality(streams, quality: str) -> list:
    """Every stream at one resolution, in default-pick order (the order
    _rank already put them in)."""
    wanted = "2160p" if (quality or "").lower() == "4k" else (quality or "").lower()
    return [s for s in streams or []
            if ("2160p" if (s.get("quality") or "").lower() == "4k"
                else (s.get("quality") or "").lower()) == wanted]


# -------------------------------------------------------- any open site

# Media the player can actually open. Ordered longest-first so `.m3u8`
# wins over a bare `.ts` segment inside the same URL.
_MEDIA_RE = re.compile(
    r"""https?://[^\s'"<>\\]+?\.(?:m3u8|mpd|mp4|mkv|webm|m4v)(?:\?[^\s'"<>\\]*)?""",
    re.I)
_IFRAME_RE = re.compile(r"""<iframe[^>]+src\s*=\s*["']([^"']+)["']""", re.I)
_VIDEO_SRC_RE = re.compile(
    r"""<(?:video|source)[^>]+src\s*=\s*["']([^"']+)["']""", re.I)
# JWPlayer and the dozen themes that copy it: sources:[{file:"..."}] and
# the bare file:"..." form. Both are everywhere on these sites.
_FILE_RE = re.compile(r"""["']?file["']?\s*:\s*["']([^"']+)["']""", re.I)
_LABEL_RE = re.compile(r"""["']?label["']?\s*:\s*["']([^"']+)["']""", re.I)

# Hosts that appear in an iframe on nearly every one of these pages and
# never hold the video - following them is pure latency.
_NOISE_HOSTS = ("google.com", "googletagmanager.com", "doubleclick.net",
                "facebook.com", "disqus.com", "youtube.com/embed/subscribe",
                "recaptcha", "cloudflareinsights", "histats", "adservice")

_PACKED_RE = re.compile(
    r"eval\(function\(p,a,c,k,e,[dr]\)\{.*?\}\((.*?)\)\)", re.S)


def _unbase(value: str, base: int) -> int:
    digits = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
    total = 0
    for char in value:
        total = total * base + digits.index(char)
    return total


def _unpack_packed(body: str) -> str:
    """Undo the Dean Edwards packer these embed pages wrap their player
    config in.

    Worth the forty lines: the packed form is the single most common
    shape on open streaming sites, and without unpacking it the page
    contains no media URL at all - the resolver finds nothing and looks
    broken on exactly the sites it exists for. Failure returns "" and
    the caller carries on with the raw body."""
    out = []
    for match in _PACKED_RE.finditer(body):
        args = match.group(1)
        try:
            payload = re.match(r"""\s*['"](.*?)['"]\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*['"](.*?)['"]\.split\(['"]\|['"]\)""",
                               args, re.S)
            if not payload:
                continue
            script, radix, count, words = payload.groups()
            radix, count = int(radix), int(count)
            table = words.split("|")
            script = script.encode().decode("unicode_escape")

            def replace(token, _table=table, _radix=radix):
                index = _unbase(token.group(0), _radix)
                if index < len(_table) and _table[index]:
                    return _table[index]
                return token.group(0)

            out.append(re.sub(r"\b\w+\b", replace, script))
        except Exception:
            continue
    return "\n".join(out)


def _absolute(url: str, page_url: str) -> str:
    url = (url or "").strip()
    if url.startswith("//"):
        url = urllib.parse.urlsplit(page_url).scheme + ":" + url
    return urllib.parse.urljoin(page_url, url)


def _media_in(body: str, page_url: str, source: str) -> list:
    """Every playable URL this page states, however it states it."""
    found, seen = [], set()

    def add(url, quality=""):
        url = _absolute(url, page_url)
        if not url.lower().startswith("http") or url in seen:
            return
        if not _MEDIA_RE.match(url) and ".m3u8" not in url.lower():
            return
        seen.add(url)
        lowered = url.lower()
        found.append({
            "title": f"{source} {quality}".strip(),
            "url": url,
            "kind": "hls" if (".m3u8" in lowered or ".mpd" in lowered) else "direct",
            "source": source,
            "quality": quality or _quality_of(url),
            "reason": "",
            # Referer/Origin of the page it was found on. Most of these
            # hosts 403 the media URL without it, which is why the same
            # link plays in a browser and not in a player.
            "headers": _headers(page_url),
        })

    for match in _VIDEO_SRC_RE.finditer(body):
        add(match.group(1))
    # file:"..." with the label that usually sits beside it
    for match in _FILE_RE.finditer(body):
        window = body[match.end():match.end() + 200]
        label = _LABEL_RE.search(window)
        add(match.group(1), label.group(1) if label else "")
    for match in _MEDIA_RE.finditer(body):
        add(match.group(0))
    return found


def resolve_page(page_url: str, *, deadline=None, _depth: int = 0) -> list:
    """Playable streams from any open watch page.

    Chases iframes, because nearly every one of these sites embeds a
    third-party player rather than serving the file itself - a resolver
    that reads only the first page finds nothing on the majority of
    them. Two levels deep and a handful of frames per level: past that
    it is following adverts, and the deadline is being spent on pages
    that have never held a video."""
    if deadline is None:
        deadline = net.deadline_in(15)
    timeout = net.step_timeout(deadline, DEFAULT_TIMEOUT)
    if timeout is None or not (page_url or "").startswith("http"):
        return []
    host = urllib.parse.urlsplit(page_url).netloc
    try:
        body = _get(page_url, timeout, referer=page_url if _depth else None)
    except Exception:
        return []

    unpacked = _unpack_packed(body)
    results = _media_in(body + ("\n" + unpacked if unpacked else ""),
                        page_url, host)
    if results or _depth >= 2:
        return _rank(results)

    for match in list(_IFRAME_RE.finditer(body))[:4]:
        frame = _absolute(match.group(1), page_url)
        lowered = frame.lower()
        if not lowered.startswith("http") or any(n in lowered for n in _NOISE_HOSTS):
            continue
        if net.step_timeout(deadline, DEFAULT_TIMEOUT) is None:
            break
        results.extend(resolve_page(frame, deadline=deadline, _depth=_depth + 1))
        if results:
            break
    return _rank(results)


def playable_check(stream: dict, timeout: float = 6) -> bool:
    """Whether a resolved URL actually serves media, by asking for the
    first bytes only.

    A Range request rather than a HEAD: these hosts routinely answer 405
    or 404 to HEAD while serving the same URL perfectly to a GET, so a
    HEAD-based check discards working streams."""
    url = stream.get("url") or ""
    if not url.startswith("http"):
        return False
    headers = dict(stream.get("headers") or {})
    headers.setdefault("User-Agent", _UA)
    headers["Range"] = "bytes=0-1023"
    request = urllib.request.Request(url, headers=headers)
    try:
        with net.urlopen(request, timeout=timeout) as response:
            content_type = (response.headers.get("Content-Type") or "").lower()
            body = response.read(1024)
    except Exception:
        return False
    if any(marker in content_type for marker in
           ("video", "mpegurl", "octet-stream", "dash+xml", "mp4")):
        return True
    # An HLS playlist is text/plain on plenty of hosts; its first line is
    # not ambiguous.
    return body.lstrip().startswith(b"#EXTM3U")
