"""Netflix and Crunchyroll title ids, from Wikidata's public data.

Both are the same shape of problem - their own search is behind a
sign-in, so nothing on either can be read - and the answer each needs is
a single opaque id: netflix.com/title/70300472 is Hunter x Hunter, and
crunchyroll.com/series/GY3VKX1MR is the same show, neither of which
appears on any page reachable without an account.

Wikidata publishes exactly those, openly, with no key and no account:
P1874 ("Netflix ID") and P11330 ("Crunchyroll series ID"). This asks in
two requests: a title search, then one batched entity fetch for the
candidates. AniList also carries both as `externalLinks`, and
anime_sites still falls back to it, but Wikidata is asked first for two
reasons measured here: it answered for every title tried, and AniList
rate-limits an ordinary home connection into a *403 on every request*
that can last hours, during which it silently reports "no link" for
everything. Crunchyroll had no second source at all until P11330;
AniList going quiet meant Crunchyroll links simply stopped resolving.

Matching is deliberately strict, the same posture as mangadex.py: a
near-miss that resolves to the wrong show pins an entry to the wrong
page permanently, which is far worse than falling back to a search
page. A Wikidata title search for "Hunter x Hunter" legitimately returns
both the series (70300472) and the 2013 film (80108453), so the score
decides, and the shorter label breaks a tie - the base series rather
than a sequel or a spin-off.
"""

import json
import urllib.parse
import urllib.request

from . import net, title_match

API = "https://www.wikidata.org/w/api.php"

# P1874 is "Netflix ID" - the id in netflix.com/title/<id>.
_NETFLIX_PROPERTY = "P1874"

# P11330 is "Crunchyroll series ID" - the id in crunchyroll.com/series/<id>.
# Not P4110, which is the older slug form and is marked deprecated on
# Wikidata itself. Coverage measured over six real tracked-style titles:
# Frieren, Jujutsu Kaisen, One Piece and Hunter x Hunter all carry one;
# Vinland Saga and Kaiju No. 8 don't, and fall back to AniList as before.
_CRUNCHYROLL_PROPERTY = "P11330"

# How close a Wikidata label has to be before its id is trusted. Matches
# anilist.py's threshold rather than mangadex.py's stricter 0.85: the
# penalty for a miss here is a wrong link the user sees immediately, not
# a wrong schedule they might believe.
_MATCH_THRESHOLD = 0.8

_MAX_CANDIDATES = 8
_MAX_RESPONSE_BYTES = 2_000_000

# Wikidata asks callers to identify themselves; an honest one is also
# what keeps this from being throttled the way a generic agent is.
_HEADERS = {
    "Accept": "application/json",
    "User-Agent": "Atomic/1.x (https://github.com/Khaled-Alneef/Atomic)",
}


def _get_json(url: str, timeout: int):
    """Bounded in both size and wall-clock time (net.read_text): urlopen's
    timeout bounds each socket operation, not the transfer, so a host
    dribbling bytes holds the thread forever - and these run on
    lookup_pool's small worker set."""
    deadline = net.deadline_in(timeout)
    request = urllib.request.Request(url, headers=_HEADERS)
    with net.urlopen(request, timeout=timeout) as response:
        return json.loads(net.read_text(response, deadline, _MAX_RESPONSE_BYTES))


def _search_entities(title: str, timeout: int) -> list:
    """Candidate (qid, label) pairs for a title, best first."""
    url = (f"{API}?action=wbsearchentities&format=json&language=en"
           f"&uselang=en&limit={_MAX_CANDIDATES}&search={urllib.parse.quote(title)}")
    hits = _get_json(url, timeout).get("search") or []
    return [(hit["id"], hit.get("label") or "") for hit in hits if hit.get("id")]


def _ids_for(qids: list, prop: str, is_valid, timeout: int) -> dict:
    """{qid: [id, ...]} for the candidates carrying `prop`.

    One request for all of them rather than one each - the obvious
    per-entity loop costs eight round trips for a single lookup, and
    these are fired per tracked entry."""
    url = (f"{API}?action=wbgetentities&format=json&props=claims"
           f"&ids={'|'.join(qids)}")
    entities = _get_json(url, timeout).get("entities") or {}
    found = {}
    for qid, entity in entities.items():
        claims = (entity.get("claims") or {}).get(prop) or []
        ids = []
        for claim in claims:
            value = (claim.get("mainsnak") or {}).get("datavalue") or {}
            if isinstance(value.get("value"), str) and is_valid(value["value"]):
                ids.append(value["value"])
        if ids:
            found[qid] = ids
    return found


def _fetch_id(title: str, prop: str, is_valid, timeout: int):
    """The id `prop` records for `title`, or None when nothing matches
    well enough. Never raises - every caller is a background lookup, and
    the honest answer for a title the service doesn't carry is the same
    as for a failed request: no id, so the caller falls back to a search
    page."""
    title = (title or "").strip()
    if not title:
        return None
    try:
        candidates = _search_entities(title, timeout)
        if not candidates:
            return None
        labels = {qid: label for qid, label in candidates}
        ids_by_qid = _ids_for([qid for qid, _ in candidates], prop, is_valid, timeout)
        if not ids_by_qid:
            return None

        scored = []
        for qid, ids in ids_by_qid.items():
            label = labels.get(qid) or ""
            score = title_match.best_similarity(title, [label])
            if score < _MATCH_THRESHOLD:
                continue
            # Shortest label breaks a score tie, the same rule anilist.py
            # uses: a franchise's entries all score alike, and the
            # shortest name is the base work rather than a sequel or a
            # film spun off it.
            scored.append((-score, len(title_match.normalize(label)), ids[0]))
        return min(scored)[2] if scored else None
    except Exception:
        return None


def fetch_netflix_id(title: str, timeout: int = 8):
    """Netflix's own id for `title`. Ids are digits only."""
    return _fetch_id(title, _NETFLIX_PROPERTY, str.isdigit, timeout)


def _looks_like_crunchyroll_id(value: str) -> bool:
    """Crunchyroll's ids are short uppercase alphanumeric strings that
    start with G (GG5H5XQX4, GRMG8ZQZR, GY3VKX1MR). Checked because
    P11330 is free text and a Wikidata edit can put anything in it -
    including a full URL, which would produce a nonsense link."""
    return (3 <= len(value) <= 20 and value.isalnum()
            and value.upper() == value and value.startswith("G"))


def fetch_crunchyroll_id(title: str, timeout: int = 8):
    """Crunchyroll's own series id for `title`."""
    return _fetch_id(title, _CRUNCHYROLL_PROPERTY, _looks_like_crunchyroll_id, timeout)


def netflix_page_url(netflix_id: str) -> str:
    return f"https://www.netflix.com/title/{netflix_id}"


def crunchyroll_page_url(series_id: str) -> str:
    """No locale segment: Crunchyroll assigns one per visitor by geo-IP
    and redirects to it anyway (measured: /series/<id> answered from a
    redirect to /ar/series/<id>), so a stored link shouldn't carry the
    one this machine happened to get."""
    return f"https://www.crunchyroll.com/series/{series_id}"
