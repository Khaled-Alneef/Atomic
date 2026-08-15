"""Netflix title ids, from Wikidata's public data.

Netflix is the same shape of problem as Crunchyroll - its own search is
behind a sign-in, so nothing on it can be read - but the answer it needs
is a single opaque number: netflix.com/title/70300472 is Hunter x
Hunter, and that id appears nowhere on any page reachable without an
account.

Wikidata publishes exactly that as property P1874 ("Netflix ID"),
openly, with no key and no account. This asks for it in two requests: a
title search, then one batched entity fetch for the candidates. AniList
also carries Netflix links in `externalLinks`, and anime_sites still
falls back to it, but Wikidata is asked first for two reasons measured
here: it answered for every title tried, and AniList rate-limits an
ordinary home connection into a *403 on every request* that can last
hours, during which it silently reports "no link" for everything.

Matching is deliberately strict, the same posture as mangadex.py: a
near-miss that resolves to the wrong show pins an entry to the wrong
page permanently, which is far worse than falling back to a search
page. A Wikidata title search for "Hunter x Hunter" legitimately returns
both the series (70300472) and the 2013 film (80108453), so the score
decides, and the shorter label breaks a tie - the base series rather
than a sequel or a spin-off.
"""

import json
import time
import urllib.parse
import urllib.request

from . import title_match

API = "https://www.wikidata.org/w/api.php"

# P1874 is "Netflix ID" - the id in netflix.com/title/<id>.
_NETFLIX_PROPERTY = "P1874"

# How close a Wikidata label has to be before its id is trusted. Matches
# anilist.py's threshold rather than mangadex.py's stricter 0.85: the
# penalty for a miss here is a wrong link the user sees immediately, not
# a wrong schedule they might believe.
_MATCH_THRESHOLD = 0.8

_MAX_CANDIDATES = 8
_MAX_RESPONSE_BYTES = 2_000_000
_READ_CHUNK = 65536

# Wikidata asks callers to identify themselves; an honest one is also
# what keeps this from being throttled the way a generic agent is.
_HEADERS = {
    "Accept": "application/json",
    "User-Agent": "Atomic/1.x (https://github.com/Khaled-Alneef/Atomic)",
}


def _get_json(url: str, timeout: int):
    """Bounded in both size and wall-clock time, for the same reason
    anime_sites._read_body is: urlopen's timeout bounds each socket
    operation, not the transfer, so a host dribbling bytes holds the
    thread forever - and these run on lookup_pool's small worker set."""
    deadline = time.monotonic() + timeout
    request = urllib.request.Request(url, headers=_HEADERS)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        chunks, total = [], 0
        while True:
            chunk = response.read1(_READ_CHUNK)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > _MAX_RESPONSE_BYTES:
                raise ValueError("response body over the size cap")
            if time.monotonic() > deadline:
                raise TimeoutError("response body over the time budget")
    return json.loads(b"".join(chunks).decode("utf-8", "replace"))


def _search_entities(title: str, timeout: int) -> list:
    """Candidate (qid, label) pairs for a title, best first."""
    url = (f"{API}?action=wbsearchentities&format=json&language=en"
           f"&uselang=en&limit={_MAX_CANDIDATES}&search={urllib.parse.quote(title)}")
    hits = _get_json(url, timeout).get("search") or []
    return [(hit["id"], hit.get("label") or "") for hit in hits if hit.get("id")]


def _netflix_ids_for(qids: list, timeout: int) -> dict:
    """{qid: [netflix id, ...]} for the candidates that have one.

    One request for all of them rather than one each - the obvious
    per-entity loop costs eight round trips for a single lookup, and
    these are fired per tracked entry."""
    url = (f"{API}?action=wbgetentities&format=json&props=claims"
           f"&ids={'|'.join(qids)}")
    entities = _get_json(url, timeout).get("entities") or {}
    found = {}
    for qid, entity in entities.items():
        claims = (entity.get("claims") or {}).get(_NETFLIX_PROPERTY) or []
        ids = []
        for claim in claims:
            value = (claim.get("mainsnak") or {}).get("datavalue") or {}
            if isinstance(value.get("value"), str) and value["value"].isdigit():
                ids.append(value["value"])
        if ids:
            found[qid] = ids
    return found


def fetch_netflix_id(title: str, timeout: int = 8):
    """Netflix's own id for `title`, or None when nothing matches well
    enough. Never raises - every caller is a background lookup, and the
    honest answer for a title Netflix doesn't carry is the same as for a
    failed request: no id, so the caller falls back to a search page."""
    title = (title or "").strip()
    if not title:
        return None
    try:
        candidates = _search_entities(title, timeout)
        if not candidates:
            return None
        labels = {qid: label for qid, label in candidates}
        ids_by_qid = _netflix_ids_for([qid for qid, _ in candidates], timeout)
        if not ids_by_qid:
            return None

        scored = []
        for qid, netflix_ids in ids_by_qid.items():
            label = labels.get(qid) or ""
            score = title_match.best_similarity(title, [label])
            if score < _MATCH_THRESHOLD:
                continue
            # Shortest label breaks a score tie, the same rule anilist.py
            # uses: a franchise's entries all score alike, and the
            # shortest name is the base work rather than a sequel or a
            # film spun off it.
            scored.append((-score, len(title_match.normalize(label)), netflix_ids[0]))
        return min(scored)[2] if scored else None
    except Exception:
        return None


def page_url(netflix_id: str) -> str:
    return f"https://www.netflix.com/title/{netflix_id}"
