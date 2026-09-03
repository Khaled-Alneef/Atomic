"""Harbor-style source coverage for Atomic's watch pipeline.

Atomic already has a signed-in Stremio session for watch progress, but its
stream resolver stopped using the account's addon collection and therefore
queries only the small local/default addon list. Harbor's source coverage comes
from doing the opposite: load the user's addon collection, keep only addons that
actually expose the stream resource, and fan them out together.

This patch restores that behavior without depending on Stremio Desktop. It uses
Stremio's account API with the authKey Atomic already stores, converts compatible
addons to Atomic's existing addon shape, and injects them only for a stream
lookup so Settings/local persistence remains untouched.
"""

from __future__ import annotations

import threading
import time

_INSTALLED = False
_CACHE_TTL_S = 300.0
_cache_lock = threading.Lock()
_lookup_lock = threading.RLock()
_cache = {"at": 0.0, "auth": "", "addons": [], "fingerprint": ()}

# Harbor gives slow stream addons substantially more than Atomic's old 6-second
# ceiling. Atomic keeps one shared lookup deadline, so 16 seconds is a practical
# compromise: enough for configured Torrentio/Comet/MediaFusion-style endpoints
# while still leaving room in the caller's 20-second budget for its numbering
# fallback. All providers run concurrently, so this is not multiplied per addon.
_STREAM_REQUEST_TIMEOUT_S = 16.0
_FANOUT_BUDGET_S = 16.0
_LOOKUP_WORKERS = 32


def _stream_resource_shape(manifest):
    """Return (types, accepts_imdb) for a manifest's stream resource."""
    if not isinstance(manifest, dict):
        return set(), False
    resources = manifest.get("resources") or []
    manifest_types = {str(x) for x in (manifest.get("types") or []) if x}
    manifest_prefixes = [str(x) for x in (manifest.get("idPrefixes") or []) if x]

    found = False
    types = set()
    accepts_imdb = False
    for resource in resources:
        if resource == "stream":
            found = True
            types.update(manifest_types)
            prefixes = manifest_prefixes
            if not prefixes or any("tt".startswith(p) or p.startswith("tt") for p in prefixes):
                accepts_imdb = True
            continue
        if not isinstance(resource, dict) or resource.get("name") != "stream":
            continue
        found = True
        rtypes = {str(x) for x in (resource.get("types") or []) if x}
        types.update(rtypes or manifest_types)
        prefixes = [str(x) for x in (resource.get("idPrefixes") or []) if x]
        if not prefixes:
            prefixes = manifest_prefixes
        if not prefixes or any("tt".startswith(p) or p.startswith("tt") for p in prefixes):
            accepts_imdb = True

    if not found:
        return set(), False
    # Empty type lists mean unrestricted in the Stremio protocol. Atomic uses an
    # empty list the same way, so preserve that rather than inventing types.
    return types, accepts_imdb


def _account_addons():
    from . import app_settings, stremio, streams

    _email, auth_key = app_settings.get_stremio_auth()
    auth_key = str(auth_key or "").strip()
    if not auth_key:
        return [], ()

    now = time.monotonic()
    with _cache_lock:
        if (_cache["auth"] == auth_key and _cache["addons"]
                and now - float(_cache["at"] or 0) < _CACHE_TTL_S):
            return [dict(a) for a in _cache["addons"]], _cache["fingerprint"]

    try:
        body = stremio._api_post("addonCollectionGet", {
            "authKey": auth_key,
            "type": "user",
            "update": False,
        }, 12)
        stremio._raise_if_auth_error(body)
        raw = ((body or {}).get("result") or {}).get("addons") or []
    except Exception:
        # Do not make a transient Stremio account/API failure erase the last
        # working provider set for this session.
        with _cache_lock:
            if _cache["auth"] == auth_key and _cache["addons"]:
                return [dict(a) for a in _cache["addons"]], _cache["fingerprint"]
        return [], ()

    converted = []
    seen = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        manifest = item.get("manifest") or {}
        transport = str(item.get("transportUrl") or "").strip()
        if not transport:
            continue
        types, accepts_imdb = _stream_resource_shape(manifest)
        # Atomic's current metadata identity is IMDb. Do not spend lookup slots
        # on catalogue-only or Kitsu-only addons that cannot answer the id we
        # can actually ask for; Harbor's same manifest filtering principle is
        # applied here before fan-out.
        if not accepts_imdb:
            continue
        base = streams._normalize_addon(transport)
        if not base or base in seen:
            continue
        seen.add(base)
        converted.append({
            "id": str(manifest.get("id") or base),
            "name": str(manifest.get("name") or base),
            "base_url": base,
            "types": sorted(types),
            "from_account": True,
            "manifest": manifest,
        })

    fingerprint = tuple(sorted((a["base_url"], a["name"]) for a in converted))
    with _cache_lock:
        _cache.update({
            "at": now,
            "auth": auth_key,
            "addons": [dict(a) for a in converted],
            "fingerprint": fingerprint,
        })
    return converted, fingerprint


def install():
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    from . import streams, updater

    original_find_streams = streams.find_streams
    original_load = streams._load
    last_fingerprint = [None]

    # Fan out a real account collection rather than six-at-a-time. The source
    # picker already publishes partial batches, so slower addons can finish
    # later without hiding fast results that arrived first.
    streams.LOOKUP_WORKERS = max(int(getattr(streams, "LOOKUP_WORKERS", 1)), _LOOKUP_WORKERS)
    streams.DEFAULT_TIMEOUT = max(float(getattr(streams, "DEFAULT_TIMEOUT", 0)), _STREAM_REQUEST_TIMEOUT_S)
    streams.FANOUT_BUDGET_S = max(float(getattr(streams, "FANOUT_BUDGET_S", 0)), _FANOUT_BUDGET_S)

    def find_streams_with_account(entry, *, season=None, episode=None,
                                  deadline=None, on_partial=None):
        account_addons, fingerprint = _account_addons()
        if not account_addons:
            return original_find_streams(
                entry, season=season, episode=episode,
                deadline=deadline, on_partial=on_partial)

        # The original resolver reads its addon set synchronously before its
        # worker fan-out starts. Swap _load only for that call so account addons
        # are never written into stream_addons.json by Settings/seed operations.
        def merged_load():
            local = original_load()
            merged = [dict(a) for a in (local or [])]
            seen = {streams._normalize_addon(a.get("base_url") or "") for a in merged}
            for addon in account_addons:
                # The account collection carries Stremio's own local
                # server addon too - the same six-second dead end
                # streams.usable_addon drops from the file.
                if not streams.usable_addon(addon):
                    continue
                base = streams._normalize_addon(addon.get("base_url") or "")
                if base and base not in seen:
                    merged.append(dict(addon))
                    seen.add(base)
            return merged

        with _lookup_lock:
            # A newly installed/removed/configured Stremio addon must not be
            # masked by Atomic's old result cache.
            if fingerprint != last_fingerprint[0]:
                try:
                    streams._RESULT_CACHE.clear()
                except Exception:
                    pass
                last_fingerprint[0] = fingerprint

        # **Not under the lock.** This used to hold _lookup_lock across
        # the whole original_find_streams call, so that _load could be
        # swapped for its duration - which serialised every lookup in
        # the app behind whichever one was in flight. Measured 3
        # September 2026 on the owner's Reacher: the details page
        # prefetches Continue's episode on open, that fan-out waited
        # 6.05s on the dead "Local Files" addon at 127.0.0.1:11470, and
        # the source picker the owner then pressed showed its first rows
        # **6.3s** after the press (its find_streams started the same
        # instant the prefetch's returned; the addons themselves answer
        # in 0.1-0.4s). The swap is per thread now, so lookups overlap:
        # first rows in the picker 0.13-0.15s after the press - except
        # the process's first lookup in each _CACHE_TTL_S window, which
        # pays _account_addons' addonCollectionGet before its fan-out
        # (0.69-0.91s to first rows on Reacher, measured the same day).
        _routed.merged = merged_load
        try:
            return original_find_streams(
                entry, season=season, episode=episode,
                deadline=deadline, on_partial=on_partial)
        finally:
            _routed.merged = None

    # The merged addon set is visible only to the thread inside its own
    # find_streams; every other caller of _load (Settings, seeding) keeps
    # reading the file, so account addons are never written into it.
    _routed = threading.local()

    def routed_load():
        merged = getattr(_routed, "merged", None)
        return merged() if merged is not None else original_load()

    streams._load = routed_load
    streams.find_streams = find_streams_with_account

    # Keep the development version coherent at runtime with the commit. Older
    # motion patches in this branch already use this compatibility mechanism;
    # update both the value and updater User-Agent together.
    updater.APP_VERSION = "1.10.117"
    try:
        updater._HEADERS["User-Agent"] = f"Atomic/{updater.APP_VERSION}"
    except Exception:
        pass
