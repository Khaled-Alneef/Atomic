"""Watch progress read from Crunchyroll itself, with your own account.

Why this exists, given the module docstring in anime_sites.py says
Crunchyroll cannot be read: that is true *anonymously*. Signed in, your
own history is right there - crunchyroll.com/history lists it - and
AniList only ever knew what a separate tracker put on it. The owner
watched One-Punch Man to episode 2 on Crunchyroll while the card said
episode 7, because the number came from Stremio and nothing said so.

Read this before changing anything here:

**Crunchyroll publishes no public API and offers no key.** Everything
below is their own apps' internal API, which they change without notice
and without documentation. Accessing it with a third-party client is
against their terms of service; that is the account holder's decision to
make, and this module exists because the owner made it explicitly.

**No password, and no minting of tokens.** The primary path is a token
copied out of the browser session the user is already signed into. That
needs nothing from Crunchyroll that they don't already hand the browser,
and it is the only route that works: minting a token from an email and
password requires a client credential Crunchyroll issues to no third
party, and the published one is **measured dead** - both
`www.crunchyroll.com` and `beta-api.crunchyroll.com` answer
`auth.obtain_access_token.client_inactive` to it, which a login rejects
before it ever looks at an account.

`login`/`refresh` below still implement that grant, because it costs
nothing to keep and starts working the moment a live client credential
is put in settings.json. Nothing in the UI reaches them today.

**A pasted token is short-lived** - Crunchyroll's own session token,
minutes to an hour. So this is a "press sync when you want it" source,
not a background one, and an expired token has to say so plainly rather
than looking like "you haven't watched anything".
"""

import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import net, title_match

API = "https://www.crunchyroll.com"

# Crunchyroll's own apps identify themselves with a client id/secret
# pair. There is no way to register one - they issue none to third
# parties - so a client that talks to this API at all carries one of
# theirs, and they deactivate the ones that become widely used.
#
# Left overridable in settings.json (`crunchyroll_client_id` /
# `crunchyroll_client_secret`) precisely because of that: when a login
# starts failing with `client_inactive`, the fix is a new value here,
# not a new build. Empty by default rather than a wrong guess baked in -
# a hardcoded dead credential would make every login fail with an error
# that looks like the user's fault.
CLIENT_ID = ""
CLIENT_SECRET = ""

_TOKEN_PATH = "/auth/v1/token"
_ME_PATH = "/accounts/v1/me"
_HISTORY_PATH = "/content/v2/{account_id}/watch-history"

# Their web player's own consumer string. Sent because the content API
# rejects requests that don't identify a known consumer.
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "X-Consumer": "com.crunchyroll.cxweb",
}

# How close a history title has to be to the tracked one. Matches
# anilist.py rather than mangadex.py's stricter 0.85: this is the user's
# own history, so the candidate set is tiny and already theirs - the
# risk of a franchise collision is far lower than in an open search.
_MATCH_THRESHOLD = 0.8

_HISTORY_PAGE_SIZE = 100

# The token as it appears in whatever the user copied out of devtools.
# The character class is what a JWT is made of, which is also what stops
# the match running past the value into the rest of the JSON.
_TOKEN_IN_JSON_RE = re.compile(r'access_token"?\s*[:=]\s*"?([A-Za-z0-9._\-]+)')


class CrunchyrollError(Exception):
    """A login or lookup that failed for a reason worth showing.

    Carries `code` - Crunchyroll's own error code where it sent one, so
    Settings can tell "your password is wrong" (`invalid_grant`) from
    "the client credential this app uses has been deactivated"
    (`client_inactive`), which are the same red text otherwise and have
    completely different fixes."""

    def __init__(self, message, code=""):
        super().__init__(message)
        self.code = code


def _credentials():
    """The client credential, preferring anything set in settings.json.
    Imported here rather than at module scope - app_settings imports
    storage, and this module is imported by the tracker page."""
    from . import app_settings
    return (app_settings.get_crunchyroll_client_id() or CLIENT_ID,
            app_settings.get_crunchyroll_client_secret() or CLIENT_SECRET)


def _basic_auth() -> str:
    import base64
    client_id, client_secret = _credentials()
    if not client_id:
        raise CrunchyrollError(
            "No Crunchyroll client credential is configured. Crunchyroll "
            "issues none to third-party apps, so one has to be supplied in "
            "settings.json (crunchyroll_client_id / crunchyroll_client_secret) "
            "before signing in can work at all.",
            code="no_client_credential")
    raw = f"{client_id}:{client_secret}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


def _post_form(path: str, fields: dict, timeout: int, authorization: str):
    data = urllib.parse.urlencode(fields).encode("utf-8")
    request = urllib.request.Request(API + path, data=data, headers={
        **_HEADERS,
        "Content-Type": "application/x-www-form-urlencoded",
        "Authorization": authorization,
    })
    deadline = net.deadline_in(timeout)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(net.read_text(response, deadline))
    except urllib.error.HTTPError as exc:
        raise _from_http_error(exc) from exc


def _get_json(path: str, access_token: str, timeout: int):
    request = urllib.request.Request(API + path, headers={
        **_HEADERS,
        "Authorization": f"Bearer {access_token}",
    })
    deadline = net.deadline_in(timeout)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(net.read_text(response, deadline))
    except urllib.error.HTTPError as exc:
        raise _from_http_error(exc) from exc


def _from_http_error(exc):
    """Crunchyroll's own error code out of the body, so the message can
    name the actual problem instead of the status number."""
    code, detail = "", ""
    try:
        body = json.loads(exc.read(4000).decode("utf-8", "replace"))
        code = body.get("code") or body.get("error") or ""
        detail = body.get("message") or body.get("error_description") or ""
    except Exception:
        pass
    if "client_inactive" in code:
        return CrunchyrollError(
            "Crunchyroll has deactivated the client credential this app signs "
            "in with. Nothing is wrong with your account or password - the "
            "credential in settings.json has to be replaced.", code=code)
    if exc.code in (401, 403):
        return CrunchyrollError(
            "Crunchyroll rejected that token. They expire quickly - copy a "
            "fresh one from the browser and paste it again (Settings has the "
            "steps).", code=code or "unauthorized")
    if exc.code == 429:
        return CrunchyrollError("Crunchyroll is rate-limiting this connection; "
                                "try again in a few minutes.", code="rate_limited")
    return CrunchyrollError(f"Crunchyroll answered {exc.code}"
                            f"{' (' + code + ')' if code else ''}.", code=code)


def login(email: str, password: str, timeout: int = 15) -> dict:
    """Sign in and return {refresh_token, access_token, expires_at,
    account_id}. Raises CrunchyrollError with a message worth showing.

    The password is used for this one request and never returned, so
    nothing above this line can accidentally persist it."""
    email = (email or "").strip()
    if not email or not password:
        raise CrunchyrollError("Enter your Crunchyroll email and password.")
    body = _post_form(_TOKEN_PATH, {
        "username": email,
        "password": password,
        "grant_type": "password",
        "scope": "offline_access",
    }, timeout, _basic_auth())
    session = _session_from(body)
    session["account_id"] = body.get("account_id") or _account_id(
        session["access_token"], timeout)
    return session


def refresh(refresh_token: str, timeout: int = 15) -> dict:
    """A new access token from the stored refresh token - what every
    lookup after the first sign-in uses, so the password is needed
    exactly once."""
    if not refresh_token:
        raise CrunchyrollError("Not signed in to Crunchyroll.")
    body = _post_form(_TOKEN_PATH, {
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
        "scope": "offline_access",
    }, timeout, _basic_auth())
    session = _session_from(body)
    session["account_id"] = body.get("account_id") or _account_id(
        session["access_token"], timeout)
    return session


def _session_from(body: dict) -> dict:
    access_token = body.get("access_token")
    if not access_token:
        raise CrunchyrollError("Crunchyroll returned no access token.")
    # 30s of slack: a token that expires between the check and the
    # request is a failed lookup for no reason.
    expires_in = int(body.get("expires_in") or 0)
    return {
        "access_token": access_token,
        "refresh_token": body.get("refresh_token") or "",
        "expires_at": time.time() + max(0, expires_in - 30),
    }


def _account_id(access_token: str, timeout: int) -> str:
    body = _get_json(_ME_PATH, access_token, timeout)
    account_id = body.get("account_id") or body.get("external_id") or ""
    if not account_id:
        raise CrunchyrollError("Crunchyroll returned no account id.")
    return account_id


def fetch_history(session: dict, timeout: int = 15) -> list:
    """The account's watch history, newest first, as
    [{title, season, episode, fully_watched}, ...].

    One page. The tracker asks per entry and the answer for "what am I up
    to" is always near the top; paging the whole history would be a
    request per 100 episodes on a lookup that runs per card."""
    account_id = session.get("account_id")
    if not account_id:
        raise CrunchyrollError("Not signed in to Crunchyroll.")
    path = (_HISTORY_PATH.format(account_id=urllib.parse.quote(str(account_id)))
            + f"?page_size={_HISTORY_PAGE_SIZE}")
    body = _get_json(path, session.get("access_token") or "", timeout)
    watched = []
    for item in body.get("data") or []:
        panel = item.get("panel") or item
        meta = (panel.get("episode_metadata")
                or panel.get("episodeMetadata") or {})
        title = (meta.get("series_title") or panel.get("series_title") or "").strip()
        if not title:
            continue
        watched.append({
            "title": title,
            "season": int(meta.get("season_number") or 0),
            "episode": int(meta.get("episode_number")
                           or meta.get("sequence_number") or 0),
            "fully_watched": bool(item.get("fully_watched")),
        })
    return watched


_session_lock = threading.Lock()
_session = None

# One history fetch serves a whole page of cards. Without this, opening
# the Anime page would ask Crunchyroll once per entry for the same list -
# the shape lookup_pool.py exists to prevent, and a good way to get the
# account rate-limited.
_HISTORY_TTL = 60.0
_history_cache = None
_history_at = 0.0


def session_from_token(access_token: str, timeout: int = 15) -> dict:
    """A usable session from a token pasted out of the browser.

    Checked immediately against /accounts/v1/me rather than stored on
    faith: a mistyped or half-copied token would otherwise sit in
    Settings looking connected and quietly fail on every card."""
    access_token = (access_token or "").strip()
    # Whatever shape it arrives in. People paste the whole header value,
    # or the quoted JSON value straight out of the Response tab, or a
    # `"access_token": "..."` fragment - all of which are the right
    # token wearing punctuation, and none of which should be the user's
    # problem to strip by hand.
    if access_token.lower().startswith("bearer "):
        access_token = access_token[7:].strip()
    if "access_token" in access_token:
        # Quoted key or bare (devtools' Response tab shows the tree form
        # `access_token: "eyJ..."`, the raw tab shows `"access_token":
        # "eyJ..."`, and people copy either.
        found = _TOKEN_IN_JSON_RE.search(access_token)
        if found:
            access_token = found.group(1)
    access_token = access_token.strip().strip('",').strip()
    if not access_token:
        raise CrunchyrollError("Paste your Crunchyroll token first.")
    return {"access_token": access_token,
            "account_id": _account_id(access_token, timeout)}


def active_session(timeout: int = 15):
    """A session for the connected account, or None if there isn't one.

    The pasted token is used directly - no refreshing, because refreshing
    is the part that needs the client credential Crunchyroll won't give
    out. An expired token surfaces as a CrunchyrollError saying to paste
    a new one."""
    from . import app_settings
    token = app_settings.get_crunchyroll_token()
    if token:
        return {"access_token": token,
                "account_id": app_settings.get_crunchyroll_account_id()}
    stored = app_settings.get_crunchyroll_session()
    if not stored:
        return None
    global _session
    with _session_lock:
        if (_session and _session.get("source_token") == stored["refresh_token"]
                and time.time() < _session.get("expires_at", 0)):
            return _session
        session = refresh(stored["refresh_token"], timeout)
        # Crunchyroll may hand back a new refresh token; persist it or the
        # next run signs in against a rotated-out one.
        new_token = session.get("refresh_token") or stored["refresh_token"]
        session["refresh_token"] = new_token
        session["account_id"] = session.get("account_id") or stored["account_id"]
        session["source_token"] = stored["refresh_token"]
        if new_token != stored["refresh_token"]:
            app_settings.set_crunchyroll_session(
                stored["email"], new_token, session["account_id"])
            session["source_token"] = new_token
        _session = session
        return session


def cached_history(session: dict, timeout: int = 15) -> list:
    """fetch_history, shared across the cards of one page visit."""
    global _history_cache, _history_at
    with _session_lock:
        if _history_cache is not None and time.time() - _history_at < _HISTORY_TTL:
            return _history_cache
    history = fetch_history(session, timeout)
    with _session_lock:
        _history_cache, _history_at = history, time.time()
    return history


def forget_cached_history():
    """Drop the shared history - after a manual sync, where the user is
    asking precisely because they just watched something."""
    global _history_cache
    with _session_lock:
        _history_cache = None


def watch_progress(session: dict, title: str, timeout: int = 15):
    """(season, episode) for `title` from the account's own history, or
    None when it isn't there.

    The furthest episode of the matching series, not the most recently
    opened one: rewatching episode 1 of something finished should not
    move progress backwards. An episode that was started but not
    finished still counts - it is where the user is up to, which is what
    Crunchyroll's own history shows ("20m left" on the episode in
    progress)."""
    title = (title or "").strip()
    if not title:
        return None
    best = None
    for item in cached_history(session, timeout):
        if title_match.similarity(title, item["title"]) < _MATCH_THRESHOLD:
            continue
        current = (item["season"], item["episode"])
        if best is None or current > best:
            best = current
    return best
