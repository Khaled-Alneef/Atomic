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

from . import app_settings, logs, net, storage

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

# Cinemeta's episode list, for `absolute_episode`. Same defensive shape:
# without it a series is judged on its stated season and episode only,
# which is what this file did before absolute numbering was understood.
try:
    from . import stremio
except Exception:  # pragma: no cover
    stremio = None

# The debrid client (helpers/debrid). Same defensive import again: a
# build without it plays from the swarm exactly as before, and so does a
# build *with* it until a key is pasted in Settings - debrid.available()
# is the gate on every use below.
try:
    from . import debrid
except Exception:  # pragma: no cover
    debrid = None

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
    # A host that has refused twice recently is skipped rather than
    # waited on - see net.host_refusing for the measurement. Source
    # lookups fan out over many hosts at once, so on a network that
    # blocks some of them this is the difference between a lookup
    # bounded by the slowest live host and one bounded by the deadline.
    if net.host_refusing(url):
        raise ConnectionError(f"{net._url_host(url)} is refusing connections")
    deadline = net.deadline_in(timeout)
    try:
        with net.urlopen(request, timeout=timeout) as response:
            text = net.read_text(response, deadline, max_bytes)
    except Exception:
        net.note_host_failure(url)
        raise
    net.note_host_success(url)
    return text


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


# **The Stremio local server is never an addon here.** The owner's
# stream_addons.json still carried "Local Files (without catalog
# support)" at http://127.0.0.1:11470/local-addon from the account import
# that used to live in this module, and integrations.md is explicit that
# the 127.0.0.1:11470 route must not come back. Measured 3 September 2026
# on his Reacher S1E2: that addon hung 6.04-6.06s and answered 0 rows on
# every lookup, so the source picker's *final* list - and every auto-pick
# that waits for it - paid six seconds behind addons that answer in
# 0.1-1.4s. Dropped at read time rather than by editing his file: a
# Settings save that rewrites the list simply stops carrying it.
_LOCAL_SERVER_HOSTS = ("127.0.0.1:11470", "localhost:11470")


def usable_addon(addon) -> bool:
    """Whether an addon row is one this app will ask at all."""
    if not isinstance(addon, dict):
        return False
    base = str(addon.get("base_url") or "").lower()
    return not any(host in base for host in _LOCAL_SERVER_HOSTS)


def _load() -> list:
    return [a for a in storage.load(SITES_FILE, []) if usable_addon(a)]


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


# The public trackers merged into **every** torrent, not only the ones
# whose addon supplied no announce list. That is how Harbor does it
# (src-tauri/src/torrent_engine/trackers.rs merges its 34 into every
# add), and the reasoning transfers: the addon's list is whatever was
# scraped into the release listing, sometimes stale, and libtorrent
# announces to all trackers in parallel (`announce_to_all_trackers` in
# torrent_engine._make_session) so extra rows cost UDP packets, not
# wall-clock.
#
# **Every row here answered a live announce probe twice, 23 August 2026,
# from the owner's connection** (UDP connect handshake / HTTP announce
# returning bencode, 20 threads, timings below are the two runs). The
# previous eight-entry list was probed the same way and three of its
# rows - open.tracker.cl, tracker.openbittorrent.com and
# tracker1.bt.moack.co.kr - timed out in both runs, so a third of what
# was being handed to every trackerless magnet was dead weight. Probed
# but left out for failing or exceeding ~1s in either run: zhuqiy,
# nekomi.cn, 7471.top, gcrenwp.top, anibt.net, dhitechnical.com,
# tritan.gg, bittor.pw (http; its udp twin answered), bt1.archive.org
# (udp; its http twin answered), and Harbor's zukizuki / yemekyedim /
# tmtime.dev / manager.v6.navy.
DEFAULT_TRACKERS = (
    "udp://tracker.opentrackr.org:1337/announce",    # 293 / 395ms
    "udp://open.demonii.com:1337/announce",          # 593 / 686ms
    "udp://tracker.torrent.eu.org:451/announce",     # 317 / 289ms
    "udp://exodus.desync.com:6969/announce",         # 457 / 442ms
    "udp://explodie.org:6969/announce",              # 416 / 502ms
    "udp://open.stealth.si:80/announce",             # 325 / 328ms
    "udp://tracker.dler.org:6969/announce",          # 513 / 409ms
    "udp://tracker.qu.ax:6969/announce",             # 380 / 312ms
    "udp://tracker-udp.gbitt.info:80/announce",      # 380 / 285ms
    "udp://tracker.bittor.pw:1337/announce",         # 477 / 437ms
    "http://bt1.archive.org:6969/announce",          # 577 / 718ms
    "http://tracker.renfei.net:8080/announce",       # 479 / 514ms
    "http://tracker.waaa.moe:6969/announce",         # 601 / 694ms
    "http://tracker.mywaifu.best:6969/announce",     # 402 / 3547ms
    "https://tr.nyacat.pw:443/announce",             # 428 / 670ms
    "https://tracker.leechshield.link:443/announce", # 822 / 680ms
    "https://tracker.pmman.tech:443/announce",       # 751 / 804ms
    "https://t.213891.xyz:443/announce",             # 531 / 1023ms
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
#
# **The early-out an earlier version of this comment proposed - zero
# peers AND zero connect candidates shortly after metadata - was
# measured 22 August 2026 and never fires.** Twelve instrumented runs
# across the owner's three test titles, every added torrent's status
# sampled at 5Hz: a dead release holds 27-378 connect candidates for
# the whole of its budget, because the trackers and DHT keep supplying
# addresses that then give nothing. What does separate dead from live
# is *peers after metadata*: on every dead release the 4-18 peers that
# served the metadata were all gone within half a second of it landing
# and no payload byte ever followed, while every live one kept its
# peers and delivered within a couple of seconds. That rule - and a
# second one for the opposite failure, a *live* swarm killed at the
# queued deadline mid-delivery because its first piece is bigger than
# six seconds of its rate (House of the Dragon walked 23 of those,
# 33.0s to a url) - now lives in torrent_engine.await_start; the
# replay that set the thresholds is written above DEAD_GRACE_S there.
# A third check rides the same moment metadata lands: a release whose
# chosen file is under _MIN_REAL_SIZE (episodes) or _MIN_MOVIE_SIZE
# (films) is a sample wearing the title's name, and one such 62MB
# listing *won* a Top Gun race twice before this (reason "tiny-file").
# And the metadata itself is now kept on disk
# (torrent_engine._METADATA_DIR), because the winner paid 0.7-5.8s for
# it and the race retries the same top releases on every attempt - a
# cache hit is only possible for a release previously fetched to
# completion, so it pays on re-presses and next-episodes of the same
# pack, not on first sight.
#
# What the whole set bought, measured 22-23 August 2026 as an
# interleaved A/B - alternating runs of the old semantics and the new
# on the same titles inside the same hour, because swarm drift between
# separate sessions had already invalidated one comparison that day.
# prepare_fastest seconds, three runs each, min/median/max:
#
#                       old semantics       after (cold cache)
#   Demon Slayer S01E01   6.5 / 6.8 / 10.7    4.4 / 5.4 / 5.6
#   House Dragon S01E05   6.9 / 19.2 / 19.4   5.2 / 7.7 / 8.8
#   Top Gun Maverick     11.1 / 11.3 / 11.5   5.3 / 8.4 / 10.5
#
# Two follow-up Top Gun runs spanned 6.9 and 19.6s - the second was a
# round in which nearly every swarm refused, and no scheduling can
# manufacture a seeder. The spread is the honest shape of this: the
# median moved from ~7-19s to ~5-8s, the best case sits at 4.4-5.3s,
# and a bad swarm round still costs double digits. The biggest single
# contributor by lane logs was the warming window (piece-0 focus, see
# torrent_engine._Torrent.warming); the dead-lane rule is what walks
# the list twice as fast when the top is dead (lanes that held 6.8-11.5s
# exit at ~2.1s); the width bump is what stopped Top Gun's one live
# release waiting 5.5s for a lane.
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
# **Eight, up from six, 22 August 2026.** Top Gun: Maverick's one
# reliably-fast release sat at candidate #7 in all three interleaved
# control runs, so it spent ~5.5s waiting for a lane and the whole
# prepare pinned at 11.0-11.5s whatever else changed. The engine's
# session limits (active_downloads 32) were sized for more than this
# race already; see _make_session's note, which is the constraint that
# actually bit last time.
RACE_WIDTH = 8
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
# How long prepare() waits for its engine arm's own verdict after the
# race budget expires - the arm is bounded by the same metadata/data
# waits the budget was made of, so this is normally a few milliseconds
# and three seconds is the ceiling for a slow unwind.
ENGINE_VERDICT_GRACE_S = 3.0

# ---- debrid budgets ---------------------------------------------------
#
# A cached release at the debrid service is 3-4 HTTPS round trips to a
# CDN URL - the only route measured to make press-to-picture *consistent*
# (the swarm's own numbers: 4 of 29 runs under 4.5s, medians 3.3-16.7s,
# the same title spanning 3.2-13.3s within an hour). An uncached one is
# known to be uncached at the first status read after selection, so a
# miss costs a couple of round trips, never a swarm-sized wait.
#
# What a lone prepare() may spend asking debrid before the engine gets
# its turn. Covers a slow magnet conversion; the typical uncached exit
# is ~1-2s and the typical cached answer well inside 4.
DEBRID_PREPARE_BUDGET_S = 8.0
# The race's debrid lane: how many releases it may try and what each
# attempt gets. Four attempts, not the whole list - every attempt is
# API requests against the owner's account, and if the first few
# well-seeded releases are not cached the rest are even less likely to
# be (cache follows popularity), while the torrent lanes are already
# racing the same list.
# **Six, up from four, 23 August 2026.** Refused releases no longer cost
# an attempt (see _debrid_lane), so the budget is now spent only on
# releases that could actually answer - and on the titles measured the
# cached one was not always in the top four.
DEBRID_LANE_ATTEMPTS = 6
DEBRID_ATTEMPT_BUDGET_S = 10.0
# How long find_streams' cache-check flags may add to the *final* list's
# return. Rows are already on screen through on_partial by then; this
# only delays the finished ranking, and an answer that cannot arrive in
# a couple of seconds is not worth more of the one-second rule's budget.
DEBRID_CHECK_BUDGET_S = 2.5


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


def prepare(stream: dict, *, season=None, episode=None, title=None,
            metadata_timeout: float = METADATA_TIMEOUT,
            data_wait: float = DATA_WAIT, data_wait_max: float = None,
            start_at=None, duration=None, debrid_first: bool = True) -> dict:
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

    # Debrid before the swarm, when a key is configured. A cached
    # release answers in a few HTTPS round trips; an uncached one is
    # known uncached in one or two and falls straight through to the
    # engine. `debrid_first=False` is how prepare_fastest's torrent
    # lanes opt out - the race runs its own single debrid lane, and
    # eight lanes each re-asking the API would be eight times the
    # requests for the same answer.
    resume_at = _resume_seconds(start_at, duration)

    def _engine():
        return _prepare_with_own_engine(stream, info_hash, season, episode,
                                        metadata_timeout, data_wait,
                                        resume_at, data_wait_max, title=title)

    if not debrid_first:
        # prepare_fastest's torrent lanes: the race runs its own single
        # debrid lane, and eight lanes each re-asking the API would be
        # eight times the requests for one answer.
        prepared = _engine()
        if prepared is not None:
            return prepared
        stream["url"] = None
        stream["reason"] = "no-engine"
        return stream

    # **Debrid and the swarm at the same time, not one after the other.**
    #
    # This was serial - debrid first, and only if it came back empty was
    # the engine asked - and that is the owner's "the sourcing vid
    # waiting time doubled or more in the last fixes you did", 23 August
    # 2026. It became visible the moment a hand-picked source stopped
    # being raced (player._prepare_stream_worker): `prepare_fastest`
    # always ran its debrid lane *beside* its torrent lanes, so nothing
    # ever waited on debrid before the swarm; a lone `prepare()` did the
    # opposite, and an uncached release paid up to DEBRID_PREPARE_BUDGET_S
    # of API round trips before a single peer was contacted.
    #
    # Racing them costs nothing that matters: the debrid arm is HTTPS
    # round trips against an account, the engine arm is peers, and they
    # contend for nothing. The engine's torrent is handed back if debrid
    # wins, which is the same bookkeeping prepare_fastest already does
    # for its losers.
    import threading
    winner = {}
    engine_said = {}
    done = threading.Event()
    lock = threading.Lock()

    def run(name, fn):
        try:
            got = fn()
        except Exception:
            got = None
        if name == "engine":
            # Kept even when it carries no url: it is the only thing that
            # knows *why* (a dead swarm, or a build with no engine at
            # all), and the player prints that reason. Without this the
            # verdict below had to re-run the whole engine wait to learn
            # something that had already been established.
            with lock:
                engine_said["result"] = got
        # Only a stream with a url counts as a win: the engine answers
        # with a url-less stream for a dead release, and treating that
        # as an answer would cancel a debrid arm still in flight.
        if not got or not got.get("url"):
            return
        with lock:
            if winner:
                if name == "engine":
                    _release_quietly(info_hash)
                return
            winner.update(got)
        done.set()

    arms = [threading.Thread(target=run, args=("debrid", lambda: (
                _prepare_with_debrid(stream, info_hash, season, episode,
                                     title=title))), daemon=True),
            threading.Thread(target=run, args=("engine", _engine), daemon=True)]
    for arm in arms:
        arm.start()
    budget = max(1.0, float(metadata_timeout or METADATA_TIMEOUT)
                 + float(data_wait or DATA_WAIT))
    done.wait(budget)
    # **The engine arm's verdict is waited for, not guessed.** Its own
    # waits (metadata_timeout + data_wait) are what `budget` was built
    # from, so at this point it is finishing; a short join collects its
    # answer rather than inventing one. Before this, a budget that
    # expired with the engine arm still running fell through to
    # reason="no-engine" - and the player prints that as "This build has
    # no torrent engine", which is what the owner saw on a film whose
    # hand-picked source was merely dead for eight seconds (24 August
    # 2026). The engine was there the whole time; the race just had not
    # heard back from it.
    for arm in arms:
        arm.join(timeout=0.05)
    with lock:
        if winner:
            return dict(winner)
        verdict = engine_said.get("result")
    if verdict is None and arms[1].is_alive():
        arms[1].join(timeout=ENGINE_VERDICT_GRACE_S)
        with lock:
            if winner:
                return dict(winner)
            verdict = engine_said.get("result")
    if verdict is not None:
        return verdict
    stream["url"] = None
    # "no-engine" only when there is no engine. A release that produced
    # no answer at all inside the grace is a dead release, and the
    # player walks to the next source on that - exactly what it does for
    # "no-peers".
    try:
        from . import torrent_engine
        has_engine = torrent_engine.available()
    except Exception:
        has_engine = False
    stream["reason"] = "timeout" if has_engine else "no-engine"
    return stream


def _prepare_with_debrid(stream, info_hash, season, episode, deadline=None,
                         title=None):
    """The stream served from the debrid service's own storage, or None
    to mean "debrid cannot serve this" - no key, service dark, hash not
    cached, or no file provably the right episode - in which case the
    caller carries on to the engine exactly as if this did not exist.

    The result is a plain HTTPS URL, so the finished stream is
    `kind="direct"` - the shape the player already handles for addon
    URLs, no player change involved. `info_hash` is kept for identity
    (dedupe, the panel's selected row); every engine-side reader of it
    (`file_progress`, `stats`, `release`) answers empty/quietly for a
    hash the engine never added, checked before this was written.
    `engine` is set so prepare_fastest can tell whose winner this is
    when releasing the losers - the field is never printed, and neither
    is any provider name (the owner's ask, 23 August 2026: the row says
    what the *release* is, not who serves it)."""
    if debrid is None:
        return None
    try:
        if not debrid.available():
            return None
        got = debrid.playable_url(
            info_hash, season=season, episode=episode,
            deadline=deadline or net.deadline_in(DEBRID_PREPARE_BUDGET_S),
            title=title)
    except Exception:
        return None
    if not got or not got.get("url"):
        return None
    stream = dict(stream)
    stream["url"] = got["url"]
    stream["kind"] = "direct"
    stream["engine"] = "debrid"
    stream["reason"] = ""
    stream["headers"] = {}
    if got.get("file_name"):
        stream["file_name"] = got["file_name"]
    return stream


# How long fetching a release's published .torrent file may take before
# the magnet path proceeds without it. Short on purpose: the fetch runs
# *before* the engine add, so every millisecond here is on the critical
# path to first frame, and the magnet route it replaces costs 0.83-3.46s
# (measured) - a fetch slower than that buys nothing.
TORRENT_FILE_TIMEOUT_S = 2.5
TORRENT_FILE_MAX_BYTES = 8_000_000


def _torrent_file_bytes(info_hash):
    """The bytes of this release's .torrent file, or None.

    Only for releases whose source published a direct URL (Anime Tosho -
    see indexers.TORRENT_FILE_URLS, and the note there on why the
    hash-mirror services were measured and rejected). Handing these to
    torrent_engine.add skips the DHT/tracker metadata wait, which is the
    longest single step of a cold play. Never raises; None simply means
    the magnet path runs exactly as it always has."""
    try:
        if indexers is None:
            return None
        url = indexers.TORRENT_FILE_URLS.get(str(info_hash or "").lower())
        if not url:
            return None
        request = urllib.request.Request(url, headers={"User-Agent": _UA})
        deadline = net.deadline_in(TORRENT_FILE_TIMEOUT_S)
        with net.urlopen(request, timeout=TORRENT_FILE_TIMEOUT_S) as response:
            data = net.read_bytes(response, deadline, TORRENT_FILE_MAX_BYTES)
        # bencode starts with 'd'; anything else is an error page.
        return data if data[:1] == b"d" else None
    except Exception:
        return None


def _prepare_with_own_engine(stream, info_hash, season, episode,
                             metadata_timeout=None, data_wait=None,
                             resume_at=None, data_wait_max=None, title=None):
    """Stream through Atomic's own libtorrent engine.

    Returns the finished stream, or None to mean "this build has no
    engine at all" - deliberately distinct from returning a stream with
    no url, which means "the engine tried and this release is dead".

    `DEFAULT_TRACKERS` is merged into the addon's own list rather than
    used only when that list is empty - see the note on the table for
    why, and for the probe that chose its rows."""
    try:
        from . import torrent_engine
    except Exception:
        return None
    if not torrent_engine.available():
        return None

    # **Already on disk from the last time it was watched.** Before the
    # session is touched at all: a complete file needs no swarm, no
    # metadata wait and no piece check, and asking for them anyway was
    # measured at 8.4s of a 9.06s replay (see
    # torrent_engine.finished_file_path). Strict by construction - only
    # a file whose length matches the torrent's exactly takes this road,
    # so a partial download still goes the ordinary way.
    try:
        done = torrent_engine.finished_file_path(
            info_hash, season=season, episode=episode, title=title,
            file_index=stream.get("file_index"))
    except Exception:
        done = None
    if done:
        stream = dict(stream)
        stream["url"] = done
        stream["local_file"] = True
        return stream

    trackers = list(stream.get("sources") or [])
    have = {t[len("tracker:"):] if t.startswith("tracker:") else t
            for t in trackers}
    trackers += [f"tracker:{url}" for url in DEFAULT_TRACKERS
                 if url not in have]
    try:
        added = torrent_engine.add(
            info_hash, trackers=trackers, season=season, episode=episode,
            title=title,
            file_index=stream.get("file_index"),
            metadata_timeout=(METADATA_TIMEOUT if metadata_timeout is None
                              else metadata_timeout),
            start_at=resume_at,
            torrent_bytes=_torrent_file_bytes(info_hash))
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
    # **And is the file big enough to be the thing asked for?** Also
    # knowable the moment the metadata lands, and also checked here so
    # it costs a lane nothing. A sub-30MB "episode" is a sample or a
    # corrupt listing - the same _MIN_REAL_SIZE line the ranking
    # already draws - and the floor stays low for episodes on purpose:
    # a short anime special can be genuinely small, and a wrong drop
    # plays nothing at all. A *movie* gets a higher floor: a 62MB "FULL
    # IMAX 1080p" Top Gun: Maverick listing won two races on 22 August
    # 2026 (first bytes after 10.1s both times) purely because
    # everything above it was slower, and 62MB of H.264 is minutes of
    # video, not a feature film. 200MB is still far below any real
    # film encode.
    floor = _MIN_REAL_SIZE if (season and episode) else _MIN_MOVIE_SIZE
    try:
        served_size = torrent_engine.chosen_file_size(info_hash)
    except Exception:
        served_size = 0
    if 0 < served_size < floor:
        _release_quietly(info_hash)
        stream["url"] = None
        stream["reason"] = "tiny-file"
        return stream
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
            # A lane still receiving payload at its soft deadline keeps
            # waiting, up to this - see await_start and the race, which
            # is the caller that grants it.
            data_wait_max=data_wait_max,
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




# **What a race proved dead is remembered on disk, not just for the one
# call.** `failed` (below) already carries it between attempts inside a
# single press, but a re-press, the next episode, or tomorrow's session
# started from zero and re-walked the same dead swarms at 2-8s each -
# the ground truth being that on Bleach S01E12 ten consecutive releases
# answered `no-peers` once, and nothing stopped them answering it again.
# Harbor persists the same lesson (src/lib/dead-streams.ts, localStorage,
# 7-day TTL) and *drops* matching rows outright; this deliberately only
# **reorders** - a remembered-dead release sorts behind the untried ones
# and still gets its turn when everything ahead of it refused, so a
# swarm that came back to life costs a few seconds of ordering rather
# than being invisible for a week. Because the cost of being wrong is so
# much lower than Harbor's, the TTL is shorter too (24h, not 7d - swarm
# health in a release's first days is exactly when it moves; that number
# is judgment, not a measurement). Only swarm verdicts are remembered:
# `wrong-episode` and `tiny-file` are properties of the *metadata*, which
# the disk cache (torrent_engine._METADATA_DIR) already makes instant to
# re-check.
DEAD_FILE = "stream_dead.json"
DEAD_TTL_S = 24 * 3600
_DEAD_REASONS = ("no-peers", "no-metadata")


def _remembered_dead() -> dict:
    """{info_hash: entry} for every release proved dead inside the TTL."""
    try:
        stored = storage.load(DEAD_FILE, {})
        if not isinstance(stored, dict):
            return {}
        cutoff = time.time() - DEAD_TTL_S
        return {h: e for h, e in stored.items()
                if isinstance(e, dict) and e.get("ts", 0) >= cutoff}
    except Exception:
        return {}


def _remember_dead(proved_dead, winner_hash=None):
    """Fold this race's verdicts into the stored map. Expired entries
    fall out here, and a winner that was in the map is removed - it just
    disproved its own record."""
    try:
        kept = _remembered_dead()
        changed = False
        now = time.time()
        for info_hash, reason in proved_dead or ():
            if info_hash:
                kept[info_hash] = {"ts": now, "reason": reason}
                changed = True
        if winner_hash and kept.pop(winner_hash, None) is not None:
            changed = True
        if changed:
            storage.save(DEAD_FILE, kept)
    except Exception:
        pass


def prepare_fastest(candidates, *, season=None, episode=None, title=None,
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

    # Releases a previous race proved dead go to the back of the line -
    # never dropped, see the note on DEAD_FILE. The first candidate is
    # exempt on purpose: when the source was picked by hand it arrives
    # in slot 0 (see the docstring above), and the pick keeps its first
    # lane whatever yesterday's race thought of it.
    if len(live) > 2:
        remembered = _remembered_dead()
        if remembered:
            head, tail = live[:1], live[1:]
            fresh = [c for c in tail if c.get("info_hash") not in remembered]
            stale = [c for c in tail if c.get("info_hash") in remembered]
            if fresh and stale:
                live = head + fresh + stale

    import threading
    winner = {}
    started = []
    proved_dead = []
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
                    candidate, season=season, episode=episode, title=title,
                    metadata_timeout=(QUEUED_METADATA_TIMEOUT if queued
                                      else METADATA_TIMEOUT),
                    data_wait=QUEUED_DATA_WAIT if queued else DATA_WAIT,
                    # The queued budget is soft where the swarm is
                    # actually delivering: a live lane may keep waiting
                    # up to the full DATA_WAIT rather than hand its
                    # progress back and make the next candidate start
                    # from zero - see torrent_engine.EXTEND_WINDOW_S
                    # for the walk of 23 live-but-slow packs this ends.
                    data_wait_max=DATA_WAIT if queued else None,
                    start_at=start_at, duration=duration,
                    # The race's one debrid lane (below) covers debrid;
                    # a torrent lane asking too would multiply the same
                    # API requests by the race's width.
                    debrid_first=False)
            except Exception:
                continue
            if not got.get("url"):
                _release_quietly(candidate.get("info_hash"))
                if failed is not None:
                    # A plain list append under the GIL - the caller
                    # reads it only after this call returns.
                    failed.append(candidate.get("info_hash"))
                if got.get("reason") in _DEAD_REASONS:
                    # Same GIL-append pattern; folded into the stored
                    # map once, after the race settles.
                    proved_dead.append((candidate.get("info_hash"),
                                        got.get("reason")))
                continue
            with lock:
                if winner:
                    # Someone else already won; give the swarm back.
                    _release_quietly(candidate.get("info_hash"))
                    return
                winner.update(got)
            done.set()
            return

    # **One debrid lane rides beside the torrent lanes.** A cached
    # release resolves to a CDN URL in a few HTTPS round trips - the
    # consistent 2-4s start no swarm was measured to give - and running
    # it in parallel rather than ahead of the race means an uncached
    # hash (or a debrid outage) costs the race nothing at all: the
    # torrent lanes never waited for it. It walks the cached-flagged
    # rows first (find_streams marks them when the cache check
    # answered), keeps the hand pick's slot-0 privilege, and counts as
    # a lane in `running` so a race whose torrents all died quickly
    # still waits for a debrid answer already in flight instead of
    # reporting "none of these work" while a playable URL is seconds
    # away.
    def _debrid_lane():
        ordered = live[:1] + sorted(
            live[1:], key=lambda c: 0 if c.get("debrid_cached") else 1)
        # Releases the service has already refused outright (451
        # "infringing_file") are dropped here rather than left to be
        # skipped inside playable_url, because the budget below counts
        # *attempts* and a free skip is not an attempt. Measured 23 August
        # 2026: all five top-ranked releases for House of the Dragon
        # S01E05 and for Bleach TYBW S01E01 answer 451, so a lane that
        # counted them never reached a release that could have answered.
        try:
            refused = debrid.refused_hashes()
        except Exception:
            refused = set()
        if refused:
            ordered = [c for c in ordered
                       if (c.get("info_hash") or "").lower() not in refused]
        for attempt, candidate in enumerate(ordered):
            if attempt >= DEBRID_LANE_ATTEMPTS:
                return
            if done.is_set() or time.monotonic() >= deadline:
                return
            attempt_deadline = min(
                deadline, time.monotonic() + DEBRID_ATTEMPT_BUDGET_S)
            try:
                got = _prepare_with_debrid(
                    candidate, (candidate.get("info_hash") or "").lower(),
                    season, episode, deadline=attempt_deadline, title=title)
            except Exception:
                got = None
            if not got or not got.get("url"):
                continue
            with lock:
                if winner:
                    return
                winner.update(got)
            done.set()
            return

    def debrid_worker():
        try:
            _debrid_lane()
        finally:
            # Same last-lane-out bookkeeping as worker(): the race ends
            # when every lane - this one included - is spent.
            with lock:
                running[0] -= 1
                spent = running[0] <= 0
            if spent:
                done.set()

    lanes = max(1, min(width, len(live)))
    use_debrid = False
    if debrid is not None:
        try:
            use_debrid = debrid.available()
        except Exception:
            use_debrid = False
    running = [lanes + (1 if use_debrid else 0)]
    threads = [threading.Thread(target=worker, daemon=True)
               for _ in range(lanes)]
    if use_debrid:
        threads.append(threading.Thread(target=debrid_worker, daemon=True))
    for thread in threads:
        thread.start()
    done.wait(timeout)
    with lock:
        result = dict(winner) if winner else None
        attempted = list(started)
        verdicts = list(proved_dead)
    # Which engine-held torrent, if any, the winner actually is. A
    # debrid winner holds the same info_hash as a release a torrent
    # lane may have started - that lane's torrent is a loser like the
    # rest and must be released, or it keeps announcing under the CDN
    # stream it just lost to.
    winner_hash = None
    if result and result.get("engine") == "atomic":
        winner_hash = result.get("info_hash")
    if result:
        for info_hash in attempted:
            if info_hash != winner_hash:
                _release_quietly(info_hash)
    if verdicts or result:
        # A debrid win says nothing about the swarm, so only an engine
        # win clears its own dead record.
        _remember_dead(verdicts, winner_hash)
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

# **How big a swarm an Arabic release needs before it is preferred.**
# The owner's refinement, 25 August 2026: "make it always choose the one
# has Arabic subtitles embedded IF IT HAS > 50 SEEDS, otherwise chose
# the one has more seeds". The preference below him was unconditional,
# so a three-seeder Arabic release beat a three-hundred-seeder one - a
# subtitle track bought with minutes of black screen, and the reason
# auto-sourcing had started landing on releases that would not start.
ARABIC_MIN_SEEDERS = 50


# **Does this release carry Arabic subtitles in the file?**
#
# The owner's ask, 24 August 2026: "ALWAYS auto select when the vid play
# the source that has AR (arabic) embedded translation and more seeds".
#
# What there is to read, measured on Torrentio's own rows for Bleach
# TYBW S04E02: every title's third line is a language list, written
# either as regional-indicator flags or as the words "Multi Subs" -
#
#     Multi Subs / <flags>          [ToonsHub] ... 1080p HULU WEB-DL
#     <flags>                       ... SON OF DARKNESS 1080p DSNP
#     Multi Subs                    [DKB] Bleach - Sennen Kessen-hen
#
# so "states Arabic" is the Saudi flag or the word, and there is no
# third form. Three tiers rather than two, because "Multi Subs" is not
# a claim about Arabic and is not silence either: this project already
# measured (`.claude/rules/integrations.md`) that the multi-sub groups -
# ToonsHub above all, then Erai-raws - publish every language track of a
# release, Arabic included. So a multi-sub release is the right second
# choice and a plain English one is the third.
#
# U+1F1F8 U+1F1E6 written as escapes, for the same reason every other
# non-ASCII literal in this project is: a re-encoding tool has mangled
# bare characters in these files before.
_ARABIC_FLAG = "\U0001F1F8\U0001F1E6"
_ARABIC_WORD_RE = re.compile(r"\b(arabic|ara|ar)\b", re.I)
_MULTISUB_RE = re.compile(r"multi[\s._-]*subs?", re.I)
# **The group's name is evidence too, and for some addons it is the only
# evidence there is.**
#
# The three tiers above were measured on *Torrentio* rows, which print a
# language line - flags or the words "Multi Subs". TorrentsDB rows print
# neither, so on a TorrentsDB list arabic_rank found nothing anywhere,
# every release tied at "says nothing", and the whole field fell through
# to seeders. That is the owner's report of 26 August 2026: "the auto
# source does not open the one that has the embedded ara translation
# even if it has > 50 seeds" - on a list whose top row was a 487-seeder
# [ToonsHub] release of The Angel Next Door Spoils Me Rotten.
#
# Which groups count is not a guess: `.claude/rules/integrations.md`
# records the measurement that ToonsHub above all, then Erai-raws,
# publish every language track of a release with Arabic among them -
# the same finding subtitles._animetosho is built on, where Solo
# Leveling S02E05 carried 19 tracks including Arabic [ara, ASS].
#
# Rank 1, not 0: the group is a strong prior, not a stated fact, and a
# release that actually says Arabic should still beat it.
_MULTISUB_GROUP_RE = re.compile(r"\[\s*(toonshub|erai[\s._-]*raws)\s*\]", re.I)


# What `arabic_rank` returns for a release that says nothing about
# subtitles - and the rank an Arabic release is demoted to when its
# swarm is under ARABIC_MIN_SEEDERS.
_ARABIC_UNSTATED = 2


def arabic_rank(stream) -> int:
    """0 when the release states Arabic, 1 when it states multi-sub,
    2 otherwise. Smaller is better, so it drops straight into a sort
    key."""
    text = f"{stream.get('title') or ''}\n{stream.get('name') or ''}"
    if _ARABIC_FLAG in text or _ARABIC_WORD_RE.search(text):
        return 0
    if _MULTISUB_RE.search(text) or _MULTISUB_GROUP_RE.search(text):
        return 1
    return 2


def _side_content_rank(stream) -> int:
    """0 for an ordinary release, 1 for a franchise's extras.

    **Demoted rather than dropped**, which is this codebase's standing
    trade for "probably not what was asked for": an OVA pack is still
    better than a blank screen when nothing else in the list will start,
    and for the handful of titles whose OVAs really are the episodes it
    remains reachable. What it may not do is win by default, which it
    was doing - see indexers.is_side_content for the measured row."""
    if indexers is None:
        return 0
    try:
        return 1 if indexers.is_side_content(
            _release_name(stream.get("title"))) else 0
    except Exception:
        return 0


# **A release that names the season beats one that does not.**
# The owner, 31 August 2026: *"when I continued The Angel Next Door
# Spoils Me Rotten EP09 S01 it played an ep from season 2"*.
#
# Measured that day, asking indexers.episode_match for S01E09:
#
#     'The Angel ... S2 - 09'          rejected   (states season 2)
#     'The Angel ... Season 2 09'      rejected
#     'The Angel ... S01E09 1080p'     exact
#     'The Angel ... - 09 [1080p]'     exact      <- the hole
#
# The filter is right to take the last one: for a single-season show a
# bare "- 09" is episode 9 and nothing else. But fansub groups number a
# *second* season the same way, so for a show that has one, the same
# string is season 2 episode 9 - and it was ranked level with a release
# that says S01 outright.
#
# Rejecting bare-numbered releases would break every single-season show,
# so they are demoted instead: a release stating the season asked for
# leads, and an unmarked one still plays when nothing else is on offer.
# Placed directly behind the resolution term - above Arabic and seeders,
# which are preferences, because this one is about playing the right
# episode at all.
_SEASON_STATED_RE_CACHE = {}


def states_season(name, season) -> bool:
    """Whether this release name says outright which season it is."""
    try:
        number = int(season)
    except (TypeError, ValueError):
        return False
    if number <= 0:
        return False
    pattern = _SEASON_STATED_RE_CACHE.get(number)
    if pattern is None:
        # The leading word boundary matters: without it "s2"
        # matches inside any word ending in an s before a 2
        # ("Tools2"), and the rank stops meaning anything.
        #
        # A range is deliberately allowed to match: a pack labelled
        # S01-S04 really does hold season 1, so reading it as a
        # claim about season 1 is right. What this exists to demote
        # is a release that states no season at all - that is the
        # one that may be season 2 numbered from 1, which is what
        # played the wrong episode.
        pattern = re.compile(
            rf"(?:\bs\s*0*{number}\b"
            rf"|\bseason\s*0*{number}\b"
            rf"|\bs0*{number}e\d{{1,3}}\b)", re.I)
        _SEASON_STATED_RE_CACHE[number] = pattern
    return bool(pattern.search(str(name or "")))


def _season_rank(stream, season) -> int:
    """0 when the release names the season asked for, 1 otherwise."""
    if not season:
        return 0
    name = (stream or {}).get("name") or (stream or {}).get("title") or ""
    return 0 if states_season(name, season) else 1


def _default_pick_key(stream, preferred, season=None):
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
    # **Arabic before seeders, inside the chosen resolution** - the
    # owner's ask, 24 August 2026: "ALWAYS auto select when the vid play
    # the source that has AR (arabic) embedded translation and more
    # seeds". Inside it rather than above it, because "the resolution
    # you asked for wins outright" is his earlier ask and the two are
    # not in conflict: among the 1080p rows, prefer the one that carries
    # Arabic, and among those the biggest swarm. See `arabic_rank` for
    # what counts as carrying it.
    # Gated on the swarm, not stated alone - see ARABIC_MIN_SEEDERS.
    # Below the gate a release drops to the same rank as one that says
    # nothing about subtitles, so the whole field is then decided by
    # seeders, which is exactly "otherwise chose the one has more seeds".
    arabic = (arabic_rank(stream) if raw_seeders > ARABIC_MIN_SEEDERS
              else _ARABIC_UNSTATED)
    # **Extras sort below seasons, above everything unplayable.** Ahead
    # of the resolution term so a 1080p OVA pack cannot outrank a 1080p
    # season pack, and behind `resolvable` so it is still preferred to a
    # row with no way to play it at all.
    side = _side_content_rank(stream)
    # See states_season: right episode before nicest episode.
    stated = _season_rank(stream, season)
    if preferred != "best" and quality == preferred:
        size = int(stream.get("size_bytes") or 0)
        size_key = size if size >= _MIN_REAL_SIZE else float("inf")
        return (drm, resolvable, side, 0, stated, arabic, -raw_seeders,
                size_key)
    return (drm, resolvable, side, 1, -_quality_rank(quality), stated,
            arabic, -seeders)


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
# ...and below this a "movie" is - see the tiny-file check in
# _prepare_with_own_engine for the 62MB feature film that won twice.
_MIN_MOVIE_SIZE = 200 * 1024 * 1024


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


# The absolute episode index for one (season, episode), cached per
# entry for the session. Cinemeta's episode list is the same data
# Stremio itself numbers from, and it is already cached on disk by
# stremio.fetch_meta_cached, so a warm title costs a dict lookup.
_ABSOLUTE_CACHE = {}


def season_counts(entry):
    """How many episodes Cinemeta lists in each season, or {} when there
    is no metadata to say.

    Split out of `absolute_episode` because `episode_fallbacks` needs the
    same numbers for a different question - not "which episode is this
    overall" but "could season 1 have an episode with this number of its
    own". Same cache, same one fetch per title per session."""
    try:
        imdb_id = (entry or {}).get("imdb_id")
        if not imdb_id or stremio is None:
            return {}
        key = str(imdb_id)
        counts = _ABSOLUTE_CACHE.get(key)
        if counts is None:
            meta = stremio.fetch_meta_cached(imdb_id, "series")
            counts = {}
            for video in ((meta or {}).get("videos") or []):
                number = video.get("season")
                if number:
                    counts[int(number)] = counts.get(int(number), 0) + 1
            _ABSOLUTE_CACHE[key] = counts
        return counts
    except Exception:
        return {}


def absolute_episode(entry, season, episode):
    """Which episode of the whole series `season`x`episode` is, or None.

    **This is arithmetic over Cinemeta's own episode list, not a guess**,
    and the distinction matters because `episode_fallbacks` above
    deliberately refuses to guess an episode number - substituting a
    different episode is the worst failure this file can produce, and it
    has produced it. Counting the episodes Cinemeta lists before a season
    is not substitution: it is the same episode written the other way.

    Measured 24 August 2026 on the owner's own report. Cinemeta gives
    Bleach: Thousand-Year Blood War (tt14986406) 13 + 13 + 14 + 10
    episodes, so **S04E02 is absolute episode 42** - and every release
    of it is named `S01E42` or `S04E42`, because the groups number the
    whole run continuously. See _drop_wrong_season for what that cost.

    None when there is no metadata, no episode list, or the season is
    not in it - in which case the caller must behave exactly as it did
    before this existed."""
    try:
        imdb_id = (entry or {}).get("imdb_id")
        season, episode = int(season or 0), int(episode or 0)
        if not imdb_id or season < 1 or episode < 1 or stremio is None:
            return None
        counts = season_counts(entry)
        if season not in counts or episode > counts[season]:
            return None
        return sum(count for number, count in counts.items()
                   if number < season) + episode
    except Exception:
        return None


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
        # **The absolute index first, and it is not a guess** - it is
        # this episode counted from the start of the series
        # (`absolute_episode`, arithmetic over Cinemeta's own list). An
        # absolutely-numbered index files a later season's episode under
        # season 1 at *that* number, not at its per-season one.
        absolute = absolute_episode(entry, season, episode)
        if absolute and absolute != episode:
            add(1, absolute)
        # **And the bare episode number only when season 1 cannot own
        # it.** This is the owner's report of 28 August 2026: "in Kingdom
        # anime when I played the last ep last s it played an ep from
        # s1". Kingdom's season 1 has 38 episodes, so asking for S05E13
        # and falling through to S01E13 does not find the same episode
        # written another way - it finds a genuine, different, five-year
        # old episode, and every check downstream passes it because it
        # really is S01E13.
        #
        # The rung was written for absolutely-numbered shows and is
        # right for them; what it lacked was any test of whether the
        # number it was about to reuse is already taken. Season 1's own
        # length is that test. With no metadata at all nothing is known
        # and the old behaviour stands - this can only ever remove a
        # wrong answer, never add one.
        first_season = season_counts(entry).get(1)
        if first_season is None or episode > first_season:
            add(1, episode)
    return order


# How many source requests run at once inside one lookup. Three addons
# plus two indexers is the whole population today, and one worker per
# request is what turns a lookup from the sum of their times into the
# slowest of them - measured on the owner's real entries, 3.7-4.5s
# serial against 1.3-2.6s here, on the same answers.
LOOKUP_WORKERS = 6

# How long the finished list may wait for the arc-name season map when it
# was cold at the start of the lookup (see the final pass in
# find_streams). Small on purpose: the fan-out has already run by then, so
# the background resolve has had that long too and this is usually zero.
# 2.5s is the measured TMDB round trip plus slack - long enough to catch
# the resolve, short enough that a lookup which found nothing does not sit
# here on top of everything else.
ARC_MAP_FINAL_WAIT_S = 2.5


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


# How far past this entry's known range a stated season may still be its
# own season rather than somebody else's numbering.
#
# **Without it, "we know nothing" was read as "believe the episode
# number".** The owner, 26 August 2026: asking for The Angel Next Door
# Spoils Me Rotten S01E06 and being given
# `[ToonsHub] ... S02E06 1080p BIL`. Reproduced against the real entry:
# it lives in history.json with no `latest_available`, so
# _max_known_season is 0; TorrentsDB's answer stated no season 1 anywhere
# (SubsPlease-style rows write "- 06" and state none at all), so
# _contiguous_seasons is 0 too; the bound falls back to the season asked
# for, 1; and season 2 lands outside it, which switches the season
# comparison *off* and leaves the row judged on its episode number alone.
# Six equals six, so it was kept - and it was the highest-seeded row.
#
# 1, deliberately. Bleach: Thousand-Year Blood War is season 17 of Bleach
# and season 1 of its own id, and that gap is what the out-of-range rule
# exists for - 17 against a bound of 4 is plainly another numbering. Two
# against one is not; it is the next season, and asking for episode 6 of
# season 1 must not return episode 6 of season 2.
NEAR_SEASON_MARGIN = 1


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
        # **The same episode written the other way is not a conflict -
        # the owner, 24 August 2026: "in img 3 these are the sources list
        # in stremio, why am I not getting the same list".** Measured
        # against that screenshot: Torrentio answers `tt14986406:4:2`
        # (Bleach TYBW, "Son of Darkness") with 29 rows and this dropped
        # **13 of them**, including every row at the head of his Stremio
        # list - the 2160p Feibanyama, the 2415-seeder ToonsHub, the
        # 596-seeder DSNP. All thirteen are named `S01E42` or `S04E42`,
        # because the groups number TYBW's four cours as one continuous
        # run, and 42 is exactly what S04E02 is (13+13+14 episodes
        # precede it - see absolute_episode).
        #
        # So the row states the right episode under absolute numbering
        # and this read it as the wrong episode of the wrong season. The
        # addon was *asked* for 4:2 and had already done that mapping;
        # re-judging its answer by the release name is what threw the
        # best releases away.
        #
        # Only ever an extra way to *accept*. Nothing that was kept
        # before is dropped now, and the Re:Zero case this filter was
        # built for is untouched: those rows stated season 3 and 4 with
        # episode **01** when episode 1 of season 1 was asked for, and 1
        # is not the absolute index of anything but itself.
        absolute = absolute_episode(entry, season, episode)
        # **Only when the absolute index is a *different* number.** For
        # season 1 the two are the same, so the escape below asks "does
        # this name state episode N with the season ignored" - which a
        # season-4 pack answers yes to for every episode it holds.
        # Measured 25 August 2026 on a real S01E02 lookup: rows called
        # `Attack On Titan S04e01-30` and `... ALL SEASONS` were dropped
        # by the conflict test and then let straight back in here, and
        # they went on to fill the top of the list - which is both the
        # "wrong season played" report and, once the file picker learned
        # to refuse them, the "nothing plays at all" that replaced it.
        # The Bleach TYBW case this exists for is untouched: absolute 42
        # against episode 2 is a different number, which is the whole
        # reason that case needs an escape and this one does not.
        if absolute == episode:
            absolute = None
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
            in_range = {s for s in (seasons | arc_seasons)
                        if s <= bound + NEAR_SEASON_MARGIN}
            # **A season the name states outright beats an arc guess.**
            # The owner, 27 August 2026: The Angel Next Door S01E06
            # played S02E06. Measured on his own entry, the arc map for
            # tt19064770 is season 1 -> {"rotten"}, season 2 ->
            # {"rotten2"} - season 1's token is a word out of the show's
            # own title, so *every* release of *every* season contains
            # it. "The Angel Next Door Spoils Me Rotten S02E06" was
            # therefore read as season 1 as well as season 2, and that
            # extra season cancelled its own conflict:
            #
            #   stated={2} arc={1}  conflict True -> False, row kept
            #
            # Seven S02E06 rows survived into an S01E06 list that way,
            # and the highest-seeded of them sorted *first*, which is
            # what auto-pick plays.
            #
            # Arc seasons exist for names that state no season at all -
            # "Kimetsu no Yaiba - Hashira Geiko Hen - 01", where the arc
            # word is the only thing that says season 5. Where the name
            # does state one, it is the better evidence and the guess
            # must not be allowed to argue with it.
            extra = set() if seasons else arc_seasons
            if indexers.episode_conflict(name, season, episode,
                                         compare_season=bool(in_range),
                                         extra_seasons=extra):
                # Before rejecting it, ask whether the name states this
                # same episode under the series' absolute numbering. The
                # season is *not* compared on this route: a row calling
                # absolute episode 42 "S01E42" and one calling it
                # "S04E42" are the same file, and which of the two a
                # group writes says nothing about the episode.
                if not (absolute and not indexers.episode_conflict(
                        name, season, absolute, compare_season=False)):
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
                except Exception as error:
                    # One dead source is not a dead lookup - but a dead
                    # addon or indexer was invisible in atomic.log
                    # (review, 3 September 2026).
                    logs.info(f"stream source raised: "
                              f"{type(error).__name__}: {str(error)[:120]}")
                    continue
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
    # **Anime entries only - measured 24 August 2026, the owner's "when
    # I played the boys series 1st ep 1st season the auto source choose
    # opened an anime!!".** These indexers ask fansub feeds *by title*,
    # and `is_same_title` requires the significant words to appear - for
    # "The Boys" the one significant word is "boys", so Anime Tosho
    # answered 16 real anime releases ("Daily Lives of High School
    # Boys", "Bakumatsu Bad Boys!", ...) and whichever won the race
    # played. The feeds carry nothing but anime, so asking them for a
    # live-action title can only ever produce this failure. The cost,
    # stated: an anime *film* typed "Movie" loses the by-title fansub
    # extras too (Torrentio's anime providers still cover it by id) -
    # correctness over capability.
    if (indexers is not None and (entry or {}).get("title")
            and (entry or {}).get("type") == "Anime"):
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

    # **The season pass, once more, on the finished list.** The per-source
    # filter above uses whatever the arc map held when that source
    # answered, which on a cold cache is nothing at all - and a cold cache
    # is exactly a first play. Measured 23 August 2026: with the map cold,
    # "Kimetsu no Yaiba - Yuukaku Hen" (season 3) won the race for an
    # S01E01 ask, which is the owner's original complaint surviving in the
    # one case the cache could not cover.
    #
    # This is the list `prepare_fastest` actually races and the list that
    # gets cached, so it is the one that decides what plays. It waits a
    # moment for the resolve started at the top of this function (see
    # anime_identity.arc_map_soon); the fan-out has already run, so that
    # wait is usually over before it begins.
    #
    # Rows fetched under the *fallback* numbering are exempt: they were
    # asked for under a different season on purpose, so judging them
    # against this one would throw away precisely what they exist to find.
    if anime_identity is not None and episode and not arc_map:
        try:
            late_map = anime_identity.arc_map_soon(entry, ARC_MAP_FINAL_WAIT_S)
        except Exception:
            late_map = {}
        if late_map:
            guessed_rows = [s for s in results if s.get("guessed_numbering")]
            real_rows = [s for s in results if not s.get("guessed_numbering")]
            results = _drop_wrong_season(real_rows, season, episode, entry,
                                         late_map) + guessed_rows

    # Which of these releases the debrid service can serve instantly,
    # asked once for the whole list rather than per row (Harbor batches
    # the same way). Marked on the final list only - the partials have
    # already been drawn by now, and the flag's consumers are the race's
    # debrid lane and the source panel, both of which read the final
    # list. Fails soft to nothing marked.
    _flag_instant(results, deadline)

    # The season, so a release that states it outranks one that
    # says nothing - see states_season for the episode this
    # played wrong.
    ranked = _rank(results, season)
    # **What each source actually returned, in one line.** The owner
    # sent a log on 27 August 2026 asking whether a short source list
    # for Attack on Titan S01E02 was his connection. It could not be
    # answered from it: the log recorded every host that *failed* and
    # nothing at all about a lookup that succeeded, so "four sources"
    # and "four sources because three hosts answered empty" looked
    # identical. The per-source counts are the difference, and they cost
    # one line per lookup.
    try:
        per_source = {}
        for row in ranked:
            name = str(row.get("source") or "?")
            per_source[name] = per_source.get(name, 0) + 1
        logs.info(f"streams {(entry or {}).get('title') or '?'} "
                  f"S{season}E{episode}: {len(ranked)} rows"
                  + ("".join(f" | {k} {v}"
                             for k, v in sorted(per_source.items()))
                     or " | nothing answered"))
    except Exception:
        pass                # a diagnostic must never fail a lookup
    _cache_put(cache_key, ranked)
    return ranked


def _flag_instant(results, deadline):
    """Mark every row whose release the debrid service holds cached.

    `debrid_cached` is the machine-readable flag (the race's debrid lane
    tries those rows first); the "Instant · " title prefix is the
    visible one, and it is a claim about the *release* - it will start
    in seconds - never a provider name in the list (the owner's ask, 23
    August 2026). A prefix on the title rather than a new field because
    the panels print the release title verbatim, so the mark rides into
    every list without any UI change; it is added after all title
    parsing (seeders, size, season filtering) has already happened."""
    if debrid is None:
        return
    try:
        if not debrid.available():
            return
        hashes = {s.get("info_hash") for s in results
                  if s.get("info_hash") and s.get("kind") == "torrent"}
        if not hashes:
            return
        check_deadline = min(deadline, net.deadline_in(DEBRID_CHECK_BUDGET_S))
        cached = debrid.cached_hashes(hashes, deadline=check_deadline)
        if not cached:
            return
        for stream in results:
            if stream.get("info_hash") in cached:
                stream["debrid_cached"] = True
                title = stream.get("title") or ""
                if not title.startswith("Instant · "):
                    stream["title"] = f"Instant · {title}".strip(" ·")
    except Exception:
        pass


# **How far down the seeder order the Arabic preference may reach.**
#
# The owner, 25 August 2026: *"we are now making the auto source
# selection based on the embedded arabic, keep it but if the selected
# source is not on top5 (on seeds num), then auto select the 1st has
# most seeds num"*.
#
# Both halves matter and they pull against each other. Arabic-first is
# his standing ask (see `arabic_rank`) and it is why the pick is not
# simply the biggest swarm; but a release can state Arabic and have
# three seeders, and a source nobody is seeding is not a source. Five is
# his number: among the five healthiest releases at the chosen
# resolution, take the one carrying Arabic; if the Arabic one is not
# even in that five, its swarm is too far behind to be worth the
# subtitles and the biggest swarm plays instead.
#
# Only the *head* is corrected - the list the panel prints keeps the
# Arabic order, because that is the order he asked to read it in and the
# correction is about what plays without being asked.
SEEDER_SHORTLIST = 5


def _promote_seeded_head(ranked: list, preferred: str) -> list:
    """Put the biggest swarm first when the ranked head is not among the
    healthiest few - see SEEDER_SHORTLIST.

    Judged inside one resolution, because that is the group the pick is
    made in: the preferred one when it has anything playable, otherwise
    whatever the ranking led with, so a title with no 1080p is compared
    against itself rather than against nothing."""
    playable = [row for row in ranked
                if row.get("kind") != "drm"
                and (row.get("url") or row.get("info_hash"))
                and not _side_content_rank(row)]
    if len(playable) < 2:
        return ranked
    head = playable[0]

    def quality_of(row):
        value = (row.get("quality") or "").lower()
        return "2160p" if value == "4k" else value

    wanted = quality_of(head)
    if preferred != "best" and any(quality_of(r) == preferred for r in playable):
        wanted = preferred
    group = [row for row in playable if quality_of(row) == wanted]
    if len(group) < 2 or head not in group:
        return ranked
    by_seeders = sorted(group, key=lambda r: -int(r.get("seeders") or 0))
    if head in by_seeders[:SEEDER_SHORTLIST]:
        return ranked           # the Arabic pick is healthy enough
    best = by_seeders[0]
    if best is head or int(best.get("seeders") or 0) <= 0:
        return ranked
    moved = list(ranked)
    moved.remove(best)
    moved.insert(ranked.index(head), best)
    return moved


def _rank(streams: list, season=None) -> list:
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
        return _default_pick_key(stream, preferred, season)
    seen, unique = set(), []
    for stream in sorted(streams, key=key):
        marker = stream_key(stream)
        if marker in seen:
            continue
        seen.add(marker)
        unique.append(stream)
    return _promote_seeded_head(unique, preferred)


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

