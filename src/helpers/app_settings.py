"""Small persisted app-wide settings (connected Stremio account, AniList
username, sidebar nav order, manga sites/music)."""

from . import storage

SETTINGS_FILE = "settings.json"


def _load():
    return storage.load(SETTINGS_FILE, {})


def get_nav_order() -> list:
    """Saved order of sidebar nav page-names (drag-to-reorder), or []
    if the user hasn't customized it yet."""
    return _load().get("nav_order", [])


def set_nav_order(order: list):
    data = _load()
    data["nav_order"] = list(order)
    storage.save(SETTINGS_FILE, data)


def get_hidden_sections() -> list:
    """Page-names the user has hidden (see Settings > General), or [] if
    nothing's hidden. Hidden sections keep their saved data - this only
    controls visibility, in the sidebar always and on Home as well if
    get_hide_sections_from_home() is on."""
    return _load().get("hidden_sections", [])


def set_hidden_sections(hidden: list):
    data = _load()
    data["hidden_sections"] = list(hidden)
    storage.save(SETTINGS_FILE, data)


def get_hide_sections_from_home() -> bool:
    """Whether hiding a section also leaves it off the Home page, rather
    than only removing its sidebar entry.

    Defaults off, which is what hiding has always meant here - turning it
    on for existing users would silently strip previews off their Home
    page as a side effect of an unrelated update."""
    return bool(_load().get("hide_sections_from_home", False))


def set_hide_sections_from_home(enabled: bool):
    data = _load()
    data["hide_sections_from_home"] = bool(enabled)
    storage.save(SETTINGS_FILE, data)


def get_fullscreen_on_startup() -> bool:
    """Whether an Atomic started by Windows' own startup entry opens full
    screen instead of maximized.

    Only meaningful alongside "launch on Windows startup" (see
    helpers.startup) - a hand-launched Atomic ignores this entirely, so
    the setting is stored on its own but only offered while that one is
    on. Defaults off: this changes how the app looks the moment it
    opens, which is not something an update should decide for anyone."""
    return bool(_load().get("fullscreen_on_startup", False))


def set_fullscreen_on_startup(enabled: bool):
    data = _load()
    data["fullscreen_on_startup"] = bool(enabled)
    storage.save(SETTINGS_FILE, data)


def set_updated_from(version: str):
    """Leave a marker that this build is being replaced by an update, so
    the *new* build knows to show what changed (see helpers.whats_new).

    Written by the outgoing version right before it quits, because only
    it knows what version was running - the new executable starts with
    no idea what it replaced. Stored rather than passed on the command
    line: the swap script relaunches Atomic itself, and arguments there
    would also survive into every later hand-launch."""
    data = _load()
    data["updated_from"] = version or ""
    storage.save(SETTINGS_FILE, data)


def take_updated_from() -> str:
    """The version an update replaced, clearing it as it's read - the
    summary is shown once, not on every launch afterwards. Empty string
    when this launch didn't follow an update."""
    data = _load()
    previous = data.get("updated_from") or ""
    if previous:
        data.pop("updated_from", None)
        storage.save(SETTINGS_FILE, data)
    return previous


def get_last_seen_version() -> str:
    """The version that last finished starting up, or "" if no build has
    ever recorded one. Backs up the updated_from marker: an executable
    replaced by hand leaves no marker, and neither does any build older
    than the one that introduced set_updated_from."""
    return _load().get("last_seen_version") or ""


def set_last_seen_version(version: str):
    data = _load()
    data["last_seen_version"] = version or ""
    storage.save(SETTINGS_FILE, data)


def has_run_before() -> bool:
    """Whether this profile holds settings written by some earlier run.

    The one way to tell an upgrade from a first-ever install when
    neither version marker is present - which is exactly the state a
    build older than those markers leaves behind. Anything at all in
    settings.json means the app has run here before, since a genuine
    first launch writes the file only once something is saved."""
    return bool({k: v for k, v in _load().items()
                 if k not in ("updated_from", "last_seen_version")})


def get_manga_music_url() -> str:
    return _load().get("manga_music_url", "")


def set_manga_music_url(url: str):
    data = _load()
    data["manga_music_url"] = url or ""
    storage.save(SETTINGS_FILE, data)


def get_stremio_auth():
    """(email, auth_key) of the connected Stremio account, or (None,
    None) if not connected. Only the session key is stored - never the
    password."""
    data = _load()
    return data.get("stremio_email"), data.get("stremio_auth_key")


def set_stremio_auth(email: str, auth_key: str):
    data = _load()
    data["stremio_email"] = email or ""
    data["stremio_auth_key"] = auth_key or ""
    storage.save(SETTINGS_FILE, data)


def clear_stremio_auth():
    set_stremio_auth("", "")


def get_crunchyroll_token() -> str:
    """The Crunchyroll session token copied out of the browser.

    A token rather than a password because Crunchyroll issues no client
    credential to other apps, so there is no way to turn an email and
    password into a token at all - the published one is deactivated. The
    browser already holds a valid token; this is that one."""
    return _load().get("crunchyroll_token", "")


def get_crunchyroll_account_id() -> str:
    return _load().get("crunchyroll_account_id", "")


def set_crunchyroll_token(token: str, account_id: str):
    data = _load()
    data["crunchyroll_token"] = (token or "").strip()
    data["crunchyroll_account_id"] = account_id or ""
    storage.save(SETTINGS_FILE, data)


def clear_crunchyroll_token():
    set_crunchyroll_token("", "")


def get_crunchyroll_session():
    """The connected Crunchyroll account as {email, refresh_token,
    account_id}, or None.

    Only the refresh token is kept - the password is used for the one
    sign-in request and never written down, exactly as the Stremio
    account above works. Crunchyroll is the only source that can answer
    "what have I actually watched on Crunchyroll"; AniList only ever
    knew what some other tracker put on it."""
    data = _load()
    if not data.get("crunchyroll_refresh_token"):
        return None
    return {
        "email": data.get("crunchyroll_email") or "",
        "refresh_token": data["crunchyroll_refresh_token"],
        "account_id": data.get("crunchyroll_account_id") or "",
    }


def set_crunchyroll_session(email: str, refresh_token: str, account_id: str):
    data = _load()
    data["crunchyroll_email"] = email or ""
    data["crunchyroll_refresh_token"] = refresh_token or ""
    data["crunchyroll_account_id"] = account_id or ""
    storage.save(SETTINGS_FILE, data)


def clear_crunchyroll_session():
    set_crunchyroll_session("", "", "")


def get_crunchyroll_client_id() -> str:
    """Crunchyroll issues no client credential to third parties, so the
    one this signs in with lives in settings.json and can be replaced
    when Crunchyroll deactivates it - see helpers/crunchyroll.py."""
    return _load().get("crunchyroll_client_id", "")


def get_crunchyroll_client_secret() -> str:
    return _load().get("crunchyroll_client_secret", "")


def get_anilist_username() -> str:
    return _load().get("anilist_username", "")


def set_anilist_username(username: str):
    data = _load()
    data["anilist_username"] = (username or "").strip()
    storage.save(SETTINGS_FILE, data)


def get_launcher_dirs() -> dict:
    """{"steam": "G:\\Steam", ...} for whichever game launchers have a
    directory configured in Settings > Games - see helpers.launchers."""
    return _load().get("launcher_dirs", {})


def set_launcher_dir(key: str, path: str):
    data = _load()
    dirs = data.get("launcher_dirs", {})
    dirs[key] = path or ""
    data["launcher_dirs"] = dirs
    storage.save(SETTINGS_FILE, data)
