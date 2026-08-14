"""Fuzzy title matching, shared by the release-schedule lookups.

The tracker stores whatever title the user's own source called something
("Swordmaster'S Youngest Son", "Kingdom (WAN)"), which rarely matches an
external catalog's spelling exactly - these normalize both sides and
score the leftovers so a lookup can tell a real match from a same-words
coincidence ("The World After The End" vs "The Beginning After the End").
"""

import difflib
import re

# Site-added noise that isn't part of the title: parenthetical scanlation
# tags ("(WAN)", "(Official)"), bracketed ones, and the usual punctuation
# spread (curly vs straight apostrophes, en-dashes, colons).
_BRACKETED_RE = re.compile(r"[\(\[\{][^\)\]\}]*[\)\]\}]")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def normalize(text: str) -> str:
    text = (text or "").lower().replace("’", "'").replace("‘", "'").replace("`", "'")
    text = _BRACKETED_RE.sub(" ", text)
    text = _NON_ALNUM_RE.sub(" ", text)
    return " ".join(text.split())


def search_query(text: str) -> str:
    """The title with site-added tags dropped, for sending to an external
    catalog's search. Reading sites label their own version in the title
    ("Kingdom (WAN)" for a particular scanlation group's), and searching
    an external catalog for that literal string finds nothing - but
    unlike `normalize`, this keeps the punctuation/casing a search engine
    can still make use of."""
    cleaned = _BRACKETED_RE.sub(" ", text or "")
    return " ".join(cleaned.split()) or (text or "").strip()


def similarity(query: str, candidate: str) -> float:
    """0.0-1.0 for how well `candidate` matches `query`, after
    normalizing both. One title fully containing the other scores near
    the top: external catalogs routinely append a season/cour subtitle
    ("... - The Calamity") that the tracker's own title doesn't have."""
    a, b = normalize(query), normalize(candidate)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    ratio = difflib.SequenceMatcher(None, a, b).ratio()
    # Guarded by a length floor so a short query isn't a prefix of half
    # the catalog ("Kingdom" would otherwise "contain-match" everything
    # starting with it just as strongly as the real title).
    if len(a) >= 6 and len(b) >= 6 and (b.startswith(a) or a.startswith(b)):
        ratio = max(ratio, 0.95)
    return ratio


def best_similarity(query: str, candidates) -> float:
    """Best score across every name a catalog knows a title by (its main
    title plus alternates in other languages/romanizations) - the one the
    user typed often only matches one of them."""
    return max((similarity(query, c) for c in candidates if c), default=0.0)
