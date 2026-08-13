"""Small persisted app-wide settings (Anime open-provider, connected
Stremio account, AniList username, sidebar nav order, manga sites/music)."""

from . import storage

SETTINGS_FILE = "settings.json"

ANIME_PROVIDERS = ("stremio", "crunchyroll")


def _load():
    return storage.load(SETTINGS_FILE, {})


def get_anime_provider() -> str:
    provider = _load().get("anime_provider", "stremio")
    return provider if provider in ANIME_PROVIDERS else "stremio"


def set_anime_provider(provider: str):
    data = _load()
    data["anime_provider"] = provider if provider in ANIME_PROVIDERS else "stremio"
    storage.save(SETTINGS_FILE, data)


def get_nav_order() -> list:
    """Saved order of sidebar nav page-names (drag-to-reorder), or []
    if the user hasn't customized it yet."""
    return _load().get("nav_order", [])


def set_nav_order(order: list):
    data = _load()
    data["nav_order"] = list(order)
    storage.save(SETTINGS_FILE, data)


def get_hidden_sections() -> list:
    """Sidebar page-names the user has hidden from the main sidebar (see
    Settings > General), or [] if nothing's hidden. Hidden sections keep
    their saved data - this only controls sidebar visibility."""
    return _load().get("hidden_sections", [])


def set_hidden_sections(hidden: list):
    data = _load()
    data["hidden_sections"] = list(hidden)
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
