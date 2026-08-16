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

**The password grant needs a client credential, and that is the single
point of failure.** `/auth/v1/token` answers
`invalid_client / missing_client_credentials` without an Authorization
Basic header, and Crunchyroll periodically *deactivates* the credentials
third-party clients use - the resulting error is
`auth.obtain_access_token.client_inactive`. When that happens nothing
here is broken; the credential is. It is one constant (CLIENT_ID /
CLIENT_SECRET), overridable from settings.json without a rebuild, and
`login` reports the server's own error code so the cause is legible
rather than "login failed".

**The password is never stored.** You type it once, it is exchanged for
a refresh token, and only that token is saved - the same shape as the
Stremio account this app already has (see stremio.login), and the reason
`.claude/rules/integrations.md` allows a first-party session token while
banning stored keys.
"""

import json
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
    if exc.code == 401:
        return CrunchyrollError(
            "Crunchyroll rejected the sign-in. Check the email and password, "
            f"and that the account isn't a Google/Facebook login.{' ' + detail if detail else ''}",
            code=code or "unauthorized")
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


def active_session(timeout: int = 15):
    """A signed-in session with a live access token, or None when no
    Crunchyroll account is connected. Raises CrunchyrollError if the
    stored token no longer works, so the caller can say why.

    Refreshed at most once per token lifetime rather than per lookup."""
    from . import app_settings
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
