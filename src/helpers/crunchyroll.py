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
can reach) - it only changes what a double-click opens.

series_url() below tries to land straight on the title's actual series
page rather than Crunchyroll's own on-site search results list, via
Google's public "site:" search + "I'm Feeling Lucky" redirect scoped to
crunchyroll.com/series - not scraping or querying Crunchyroll itself,
just a plain Google search URL. In practice Google sometimes shows its
own one-click "you're being redirected to <url>" interstitial instead of
jumping straight there (that's Google's anti-abuse behavior for the
Lucky redirect, not something this app can control) - either way it
resolves to the right page, one click away at worst, instead of a list
of search results to sift through.
"""

import urllib.parse


def series_url(title: str) -> str:
    query = f"site:crunchyroll.com/series {title or ''}"
    return f"https://www.google.com/search?q={urllib.parse.quote(query)}&btnI=1"
