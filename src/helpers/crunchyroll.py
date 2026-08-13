"""Minimal Crunchyroll helper.

Unlike Stremio's Cinemeta (a fully public, unauthenticated JSON API) and
the reading sites in manga_sites.py, Crunchyroll's own content/search API
sits behind an OAuth client-credential exchange *and* Cloudflare's bot-
management challenge - confirmed by hand: a same-origin fetch from
crunchyroll.com itself to their search endpoint comes back
"invalid_auth_token" without a bearer token obtained through that gated
flow. Reproducing that isn't something this app does - it would mean
lifting Crunchyroll's internal client credentials and working around
their bot detection, which crosses into unauthorized-access territory
this app stays away from.

So Crunchyroll isn't a search/metadata source here the way Stremio is -
it's just an alternate *open target*. Picking it as your Anime provider
(Settings) doesn't change where suggestions/covers come from (still
Stremio's Cinemeta - that's public metadata, unrelated to where you
watch) or how "latest episode"/real watch-progress tracking works (still
Stremio's account API, since there's no Crunchyroll equivalent this app
can reach) - it only changes what a double-click opens: a Crunchyroll
search for the title, in this one tab.

(A Google "I'm Feeling Lucky" redirect scoped to crunchyroll.com/series
was tried here to land straight on the actual show page instead of a
search list - reverted, since in real-world use Google shows its own
"you're being redirected" interstitial *and* opens the target in a
second tab, which is worse than just showing the search results.)
"""

import urllib.parse


def search_url(title: str) -> str:
    return f"https://www.crunchyroll.com/search?q={urllib.parse.quote(title or '')}"
