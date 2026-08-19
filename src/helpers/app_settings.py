"""Small persisted app-wide settings (connected Stremio account, sidebar
nav order, manga sites/music)."""

from . import storage

SETTINGS_FILE = "settings.json"

# Smallest saved window geometry still worth restoring anyone into. The
# sidebar alone is 220px wide, so anything under this is a corrupted or
# hand-edited value, not a size someone chose - see get_window_geometry.
MIN_WINDOW_WIDTH = 480
MIN_WINDOW_HEIGHT = 360


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


def get_notified_update_version() -> str:
    """The version the startup update check has already announced, or ""
    if it never has.

    Stored so the toast is shown once *per version*, not once per launch:
    someone who isn't ready to update shouldn't be told again every time
    they open the app. The sidebar's dot stays regardless - it is a
    marker, not an interruption."""
    return _load().get("notified_update_version") or ""


def set_notified_update_version(version: str):
    data = _load()
    data["notified_update_version"] = version or ""
    storage.save(SETTINGS_FILE, data)


def get_window_geometry() -> dict:
    """Size, position and maximized state the main window was last left
    at, as {"x", "y", "width", "height", "maximized"} - or {} if this
    profile has never saved one, which is what a first-ever launch sees
    and what makes it fall back to the built-in 1280x840/maximized.

    Plain numbers rather than Qt's own saveGeometry() blob: the blob is
    opaque in settings.json and carries its own screen assumptions, while
    these can be clamped by hand to the monitors that exist *now* (see
    main._fit_to_available_screen) - a geometry saved on a monitor that
    has since been unplugged must not reopen the window out of reach.

    Anything malformed, or too small to be a usable window, reads as "no
    saved geometry" rather than being restored: opening a 40px stub with
    no way to know why is worse than losing the position once."""
    saved = _load().get("window_geometry")
    if not isinstance(saved, dict):
        return {}
    try:
        geometry = {key: int(saved[key])
                    for key in ("x", "y", "width", "height")}
    except (KeyError, TypeError, ValueError):
        return {}
    if (geometry["width"] < MIN_WINDOW_WIDTH
            or geometry["height"] < MIN_WINDOW_HEIGHT):
        return {}
    geometry["maximized"] = bool(saved.get("maximized", False))
    return geometry


def set_window_geometry(x: int, y: int, width: int, height: int,
                        maximized: bool):
    data = _load()
    data["window_geometry"] = {
        "x": int(x), "y": int(y),
        "width": int(width), "height": int(height),
        "maximized": bool(maximized),
    }
    storage.save(SETTINGS_FILE, data)


# What the player starts with when it has a choice. 1080p rather than
# the highest available, on measured grounds: a 2160p release is picked
# from a smaller swarm and moves far larger pieces, and the one measured
# here (313 seeders) served no bytes at all inside 60s while 1080p
# started instantly. "Best" is the wrong default when it means waiting.
RESOLUTION_CHOICES = ("2160p", "1080p", "720p", "480p", "best")
DEFAULT_RESOLUTION = "1080p"


def get_preferred_resolution() -> str:
    value = str(_load().get("preferred_resolution") or DEFAULT_RESOLUTION).lower()
    return value if value in RESOLUTION_CHOICES else DEFAULT_RESOLUTION


def set_preferred_resolution(value: str):
    data = _load()
    value = str(value or "").lower()
    data["preferred_resolution"] = value if value in RESOLUTION_CHOICES else DEFAULT_RESOLUTION
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


def get_seeded_default_sites() -> list:
    """Names of the built-in video websites already offered to this
    install. Lets a *new* default (Netflix, added after Crunchyroll) show
    up for someone who already had a sites file, without it coming back
    every launch once they delete it - see anime_sites._add_new_defaults."""
    return _load().get("seeded_default_sites", [])


def set_seeded_default_sites(names: list):
    data = _load()
    data["seeded_default_sites"] = list(names)
    storage.save(SETTINGS_FILE, data)


def get_last_auto_sync(key: str) -> float:
    """When this page last synced progress on its own, as a timestamp."""
    return float((_load().get("last_auto_sync") or {}).get(key) or 0)


def set_last_auto_sync(key: str, when: float):
    data = _load()
    stamps = data.get("last_auto_sync") or {}
    stamps[key] = when
    data["last_auto_sync"] = stamps
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
