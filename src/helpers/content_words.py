"""The names other services give the content this app keeps out of it.

Kept encoded, so the plain words appear in no file of a release - the
owner's ask, 15 October 2026. Decoded once at import: code compares
against these names, never against a literal.

    RESTRICTED_GENRE   AniList's genre for explicit works
    SUGGESTIVE_GENRE   AniList's fan-service genre - never drawn as a genre
    ANILIST_FLAG       AniList's Media field marking an explicit work
    IMDB_TAG           IMDb's genre for explicit titles (Cinemeta passes it on)
    TMDB_FLAG          TMDB's search parameter for explicit titles
"""

import base64


def _name(encoded: str) -> str:
    return base64.b64decode(encoded).decode("ascii")


RESTRICTED_GENRE = _name("SGVudGFp")
SUGGESTIVE_GENRE = _name("RWNjaGk=")
ANILIST_FLAG = _name("aXNBZHVsdA==")
IMDB_TAG = _name("QWR1bHQ=")
TMDB_FLAG = _name("aW5jbHVkZV9hZHVsdA==")
