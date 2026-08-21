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

# Everything that is not a letter or a digit **in any script**. This was
# `[^a-z0-9]`, which deleted an Arabic or Japanese title down to the
# empty string - and `similarity` answers 0.0 the moment either side is
# empty, so those titles were unmatchable by construction rather than by
# evidence. Measured 21 August 2026 on the owner's own blank Discover
# tile: MangaDex answers the query "ون بيس" with One Piece and carries
# that exact string as its `ar` alt title, and the score came back 0.00.
# Two other callers were quietly wrong for the same reason - two
# different Arabic titles both normalized to "" and therefore compared
# *equal* (anime_sites' exact-match check), and `len(normalize(name))`,
# the shortest-name tiebreak in anilist/wikidata, rated every Japanese
# title as length 0 and let it win every tie.
_NON_WORD_RE = re.compile(r"[\W_]+", re.UNICODE)
# The Latin-only reduction this used to be, kept as a floor - see
# similarity.
_LATIN_ONLY_RE = re.compile(r"[^a-z0-9]+")

# Arabic writes one letter several ways, and a reading site's spelling of
# a title differs from a catalogue's by exactly that: hamza carriers
# (أ إ آ ٱ) for bare alef, alef maqsura and the Persian ya (ى ی) for ya,
# ta marbuta for ha, the Persian kaf for kaf - plus vowel marks and the
# tatweel stretch character, which carry no meaning at all, and the
# Arabic-Indic digits. Folded the way `.lower()` folds Latin: it merges
# spellings of one title, never two different titles.
#
# The mark range is written as escapes rather than as the characters: a
# literal right-to-left range inside a bracket expression displays in an
# order that has nothing to do with the order it is stored in.
_ARABIC_MARKS_RE = re.compile("[\u0610-\u061a\u064b-\u065f\u0670\u06d6-\u06ed\u0640]")
_ARABIC_TABLE = str.maketrans({
    "أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا", "ٲ": "ا", "ٳ": "ا",
    "ى": "ي", "ی": "ي", "ئ": "ي", "ؤ": "و", "ة": "ه", "ک": "ك",
    **{chr(0x0660 + digit): str(digit) for digit in range(10)},
    **{chr(0x06F0 + digit): str(digit) for digit in range(10)},
})


def _fold(text: str) -> str:
    """Case, quote and Arabic letter-form folding, before either of the
    two character filters below runs."""
    text = (text or "").lower().replace("’", "'").replace("‘", "'").replace("`", "'")
    text = _BRACKETED_RE.sub(" ", text)
    return _ARABIC_MARKS_RE.sub("", text).translate(_ARABIC_TABLE)


def normalize(text: str) -> str:
    return " ".join(_NON_WORD_RE.sub(" ", _fold(text)).split())


def _latin(text: str) -> str:
    """`normalize` as it was before it learned other scripts: the Latin
    letters and digits only, everything else dropped."""
    return " ".join(_LATIN_ONLY_RE.sub(" ", _fold(text)).split())


def search_query(text: str) -> str:
    """The title with site-added tags dropped, for sending to an external
    catalog's search. Reading sites label their own version in the title
    ("Kingdom (WAN)" for a particular scanlation group's), and searching
    an external catalog for that literal string finds nothing - but
    unlike `normalize`, this keeps the punctuation/casing a search engine
    can still make use of."""
    cleaned = _BRACKETED_RE.sub(" ", text or "")
    return " ".join(cleaned.split()) or (text or "").strip()


def search_variants(text: str) -> list:
    """The strings to send an external catalog for one tracked title, in
    order: the title exactly as the user's own site wrote it, then the
    same with the site's group tag dropped.

    **Measured 21 August 2026 on the owner's own entry "Kingdom (WAN)"** -
    "(WAN)" is the scanlation group's tag on 3asq, not part of the name,
    and every external lookup that sent the literal string came back
    empty: MangaDex answered it with five unrelated isekai and nothing
    called Kingdom, AniList answered it with no artwork at all, and
    `discover.reading_genres` returned [] where "Kingdom" returns
    Historical / Action / Drama. Searching the other five configured
    sites for it found the title on none of them.

    The full title still goes first, and this is a *retry* rather than a
    replacement, because brackets are occasionally part of the real name
    ("Kingdom (2013)") and only the untouched string can match that.

    Scoring needs none of this - `normalize` already drops the tag on
    both sides, so "Kingdom (WAN)" scores 1.00 against "Kingdom". It is
    only the query text an external search engine takes literally."""
    full = " ".join((text or "").split())
    stripped = search_query(full)
    return [full] if not full or stripped == full else [full, stripped]


def _ratio(a: str, b: str) -> float:
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


def similarity(query: str, candidate: str) -> float:
    """0.0-1.0 for how well `candidate` matches `query`, after
    normalizing both. One title fully containing the other scores near
    the top: external catalogs routinely append a season/cour subtitle
    ("... - The Calamity") that the tracker's own title doesn't have.

    Scored twice, and the better of the two taken: once over every script
    both sides write in, once over the Latin part alone - which is what
    this compared before `normalize` learned other scripts, and is kept
    as a floor so a title carrying both ("One Piece ワンピース") still
    scores 1.00 against a catalogue's plain "One Piece" instead of being
    dragged down by characters the other side never had. The max can only
    lift a score where a second script genuinely agrees; an Arabic title
    against an English one still scores 0.0 down both paths, which is the
    honest answer and the one that keeps a wrong cover off a card."""
    return max(_ratio(normalize(query), normalize(candidate)),
               _ratio(_latin(query), _latin(candidate)))


def best_similarity(query: str, candidates) -> float:
    """Best score across every name a catalog knows a title by (its main
    title plus alternates in other languages/romanizations) - the one the
    user typed often only matches one of them."""
    return max((similarity(query, c) for c in candidates if c), default=0.0)
