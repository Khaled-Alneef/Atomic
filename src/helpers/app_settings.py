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
