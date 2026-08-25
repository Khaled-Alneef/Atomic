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


# The section rail's saved drag order moved key on 22 August 2026, when
# that rail became three fixed blocks with a row of air between them
# (the owner's ask - see tracker._section_groups). The old "section_order"
# was one flat list and could no longer be honoured: an order saved
# before the blocks existed names Saved, History, Schedule in the order
# they used to sit in, which would quietly override the order the owner
# has just asked for. Read under a new key so a stale flat list is
# ignored exactly once rather than migrated into the wrong answer; the
# old value is left where it is, unread, because deleting a setting to
# fix a layout is not a trade worth making.
SECTION_ORDER_KEY = "section_order_blocks"


def get_section_order() -> list:
    """Saved order of the tracker pages' sub-sections (the section
    rail's drag-to-reorder blocks), flattened, or [] before the user has
    dragged one. Keys are tracker.TABS and category keys; every tracker
    page shares the one order, since they share the sections."""
    return _load().get(SECTION_ORDER_KEY, [])


def set_section_order(order: list):
    data = _load()
    data[SECTION_ORDER_KEY] = list(order)
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


def get_setup_completed_at() -> str:
    """When the first-run setup was finished, skipped, or waived because
    the install already held data - ISO timestamp, or "" while it has
    never been decided, which is what tells setup_wizard to offer
    itself. One flag for every outcome on purpose: however the wizard
    was left, it must never appear twice."""
    return _load().get("setup_completed_at") or ""


def set_setup_completed_at(when: str):
    data = _load()
    data["setup_completed_at"] = when or ""
    storage.save(SETTINGS_FILE, data)


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


def get_auto_pick_source() -> bool:
    """Whether pressing an episode starts playing right away instead of
    opening the source list first.

    **Defaults off** - the owner's ask, 23 August 2026: "in the settings
    make the default for auto chose source is False when 1st time using
    the app". It defaulted on because picking was the slow part of
    starting anything; that is no longer true (a cached release resolves
    in about a second), so the default can be the one that shows the
    user what they are about to play. The player's own source and
    resolution controls remain either way.

    Only the *default* moves: `_load().get` returns a stored value
    untouched, so anyone who has already turned it on keeps it on."""
    return bool(_load().get("auto_pick_source", False))


def set_auto_pick_source(enabled: bool):
    data = _load()
    data["auto_pick_source"] = bool(enabled)
    storage.save(SETTINGS_FILE, data)


def get_blur_episode_stills() -> bool:
    """Whether the episode rows' stills are drawn blurred (Settings >
    Watching). The owner asked for the option because a still is a
    picture of the episode you have not watched yet.

    **Defaults off.** The stills were asked for in the same breath, and
    a feature that arrives smeared until a checkbox is found reads as
    broken rather than as careful - the guard is one click away for
    whoever wants it. See windows.details._blurred_still."""
    return bool(_load().get("blur_episode_stills", False))


def set_blur_episode_stills(enabled: bool):
    data = _load()
    data["blur_episode_stills"] = bool(enabled)
    storage.save(SETTINGS_FILE, data)


def get_manga_music_url() -> str:
    return _load().get("manga_music_url", "")


def music_launch_url(url: str) -> str:
    """The saved music URL as it should actually be opened: exactly what
    the user pasted, plus `autoplay=1`.

    The owner's ask, 23 August 2026, with his own link as the example -
    he pastes

        https://www.youtube.com/watch?v=DtVmMBrUXMA&list=PLCKc9G3SIVum...

    and the app opens

        https://www.youtube.com/watch?v=DtVmMBrUXMA&list=PLCKc9G3SIVum...&autoplay=1

    **The URL is preserved, not rebuilt.** The previous version (in
    reader.py) reconstructed YouTube links from just `v` and `list`,
    which silently dropped every other parameter the user had put there -
    a `t=` start offset, an `index=`, the fragment - and deliberately
    left `autoplay=1` off watch pages on the reasoning that YouTube
    ignores it there. That reasoning is sound and does not matter: an
    ignored parameter costs nothing, and the owner asked for it to be
    there. Preserving the rest is a real fix either way.

    The one rewrite kept is **/embed/ -> /watch**, because that one fixed
    a bug that was on screen: an embed URL opened as a top-level document
    has no embedding origin to check, so YouTube refuses it with "Error
    153 - Video player configuration error". The embed form simply cannot
    be the thing a browser navigates to, whatever parameters it carries.

    Never raises: a custom scheme, or anything unparseable, comes back
    exactly as it went in - those are for ShellExecute to judge."""
    import urllib.parse
    raw = str(url or "").strip()
    if not raw.lower().startswith(("http://", "https://")):
        return raw
    try:
        parts = urllib.parse.urlsplit(raw)
        host = parts.netloc.lower().removeprefix("www.")
        path, query = parts.path, parts.query
        if host in ("youtube.com", "m.youtube.com", "music.youtube.com") \
                and path.startswith("/embed/"):
            video = path[len("/embed/"):].strip("/")
            if video:
                pairs = [(k, v) for k, v in
                         urllib.parse.parse_qsl(query, keep_blank_values=True)
                         if k != "v"]
                query = urllib.parse.urlencode([("v", video)] + pairs)
                path = "/watch"
        # Appended rather than merged, so a URL that already says
        # autoplay keeps the value the user chose.
        pairs = urllib.parse.parse_qsl(query, keep_blank_values=True)
        if not any(k == "autoplay" for k, _ in pairs):
            pairs.append(("autoplay", "1"))
        return urllib.parse.urlunsplit((
            parts.scheme, parts.netloc, path,
            urllib.parse.urlencode(pairs), parts.fragment))
    except Exception:
        return raw


def set_manga_music_url(url: str):
    data = _load()
    data["manga_music_url"] = url or ""
    storage.save(SETTINGS_FILE, data)


# Every key the user can supply, as {id: (label, what it unlocks)}. One
# table rather than a pair of accessors each: they all behave
# identically - stored in settings.json under `<id>_api_key`, empty by
# default, and the feature that needs one stays dark and says so until
# it has it - and a table is what lets Settings draw the whole list
# without being edited every time a source is added.
#
# Debrid is playback speed: a release the service has cached plays as a
# plain HTTPS stream in seconds instead of waiting on a swarm (see
# helpers/debrid.py for the 29-run measurement that bought this row).
# TMDB is artwork. The subtitle ones are sources. The AI ones are
# translators: with no Arabic subtitle available for a title (which is
# the normal case for seasonal anime - measured repeatedly), an English
# track can be translated instead, which is the only route that does not
# depend on somebody having published Arabic for that exact episode.
#
# Insertion order is the order Settings draws them, so it runs from the
# one that changes what is on screen to the ones that change what a
# track says.
# **No "debrid" row, deliberately.** The owner's ask, 23 August 2026:
# "the Debrid API key in the settings, remove it, I do not want the user
# to enter it at all!! let all users use mine I provided you with!". The
# service is not optional plumbing the user configures - it is how this
# app starts playback in a couple of seconds, and it runs on the token
# bundled in the build (helpers/debrid._bundled_token). Offering a field
# for it advertised a decision nobody has to make.
#
# `get_debrid_key`/`set_debrid_key` still exist and still read
# settings.json, so a key a user pasted before this simply keeps working
# and keeps overriding the bundled one - it just cannot be entered or
# seen any more. Removing the row is what was asked for; removing
# somebody's stored key with it would be destroying their data.
API_KEYS = {
    "github": ("GitHub", "Rating episodes and chapters"),
    "tmdb": ("TMDB (The Movie Database)", "Title logos and backdrops"),
    "subdl": ("SubDL", "Arabic subtitles"),
    "subsource": ("SubSource", "Arabic subtitles"),
    "openai": ("OpenAI (ChatGPT)", "AI subtitle translation"),
    "deepseek": ("DeepSeek", "AI subtitle translation"),
    "gemini": ("Google Gemini", "AI subtitle translation"),
    "anthropic": ("Anthropic (Claude)", "AI subtitle translation"),
}

AI_KEYS = ("openai", "deepseek", "gemini", "anthropic")

# The heading each key sits under in Settings, in this order. A key with
# no group here would simply not be drawn, so a new one has to be filed.
API_KEY_GROUPS = (
    # **No GitHub row** (the owner's ask, 25 August 2026: "remove the
    # github token insert in settings"). It only ever unlocked *sending*
    # a rating, and asking somebody to make a fine-grained token with
    # write access to a repository - so that a score can be posted - is
    # the wrong price for the feature. Reading the community scores has
    # never needed anything.
    #
    # The key itself stays in API_KEYS above and community_ratings still
    # reads it, so an install that already has one keeps working; there
    # is simply nowhere to type a new one. Writing is the write proxy's
    # job (tools/ratings-worker), which needs nothing from the user.
    ("Artwork", ("tmdb",)),
    ("Subtitle Sources", ("subdl", "subsource")),
    ("AI Subtitle Translation", AI_KEYS),
)

# Where the owner goes to get each one. Shown beside the field, because
# "paste your API key" with no idea which page issues it is not an
# instruction anybody can follow.
API_KEY_HELP = {
    "github": ("github.com/settings/tokens - a fine-grained token with "
               "Contents: read and write on the Atomic repository"),
    "tmdb": "themoviedb.org/settings/api - the v4 read access token, or the v3 key",
    "subdl": "subdl.com/panel/api",
    "subsource": "subsource.net - account settings",
    "openai": "platform.openai.com/api-keys",
    "deepseek": "platform.deepseek.com/api_keys",
    "gemini": "aistudio.google.com/apikey",
    "anthropic": "console.anthropic.com/settings/keys",
}

# The same destinations as full URLs, for the "Get a key" links the
# first-run wizard (and anything else) can open in the browser - the
# help strings above are display text and would need guessing a scheme
# to be clickable. Keyed identically to API_KEYS; a key with no row here
# simply gets no link, never a broken one.
API_KEY_URLS = {
    "github": "https://github.com/settings/tokens",
    "tmdb": "https://www.themoviedb.org/settings/api",
    "subdl": "https://subdl.com/panel/api",
    "subsource": "https://subsource.net",
    "openai": "https://platform.openai.com/api-keys",
    "deepseek": "https://platform.deepseek.com/api_keys",
    "gemini": "https://aistudio.google.com/apikey",
    "anthropic": "https://console.anthropic.com/settings/keys",
}


def get_debrid_key() -> str:
    """The debrid API token the user pasted, or "" - which is the
    normal state: the build may carry a bundled token of its own
    (helpers/debrid._bundled_token, the same arrangement as TMDB's),
    and this is the override that wins over it when the shared one is
    locked or rate-limited. With neither, playback behaves exactly as
    it did before debrid existed."""
    return get_api_key("debrid")


def set_debrid_key(value: str):
    set_api_key("debrid", value)


def get_tmdb_key() -> str:
    """The owner's TMDB API key, or "" when they have not pasted one.

    Empty is a valid, expected state: the build carries a bundled read
    token of its own (helpers/artwork._bundled_token), and this is the
    override that replaces it when it is revoked or rate-limited."""
    return get_api_key("tmdb")


def set_tmdb_key(value: str):
    set_api_key("tmdb", value)


def get_voter_id() -> str:
    """This install's id for the community ratings, made once.

    **Not an identity.** It exists so that rating the same episode twice
    replaces the first score instead of stacking a second one, which is
    the only thing the store needs to tell two votes apart. It is a
    random uuid: no name, no account, nothing derived from the machine,
    and it never leaves the ratings file it is written into."""
    data = _load()
    voter = str(data.get("ratings_voter_id") or "")
    if not voter:
        import uuid
        voter = uuid.uuid4().hex[:16]
        data["ratings_voter_id"] = voter
        storage.save(SETTINGS_FILE, data)
    return voter


def get_ratings_proxy() -> str:
    """Where a rating is posted, when it is not written directly.

    A URL, not a secret - which is the whole point of it: the write token
    lives on that endpoint instead of inside Atomic.exe, where anything
    bundled is both extractable in milliseconds and published to a public
    repository at every release. See tools/ratings-worker/worker.js.

    Empty falls back to community_ratings.DEFAULT_PROXY, and if that is
    empty too, to writing directly with a token the owner pasted."""
    return str(_load().get("ratings_proxy") or "")


def set_ratings_proxy(value: str):
    data = _load()
    data["ratings_proxy"] = (value or "").strip()
    storage.save(SETTINGS_FILE, data)


def get_ratings_repo() -> str:
    """Where the community ratings are kept, "owner/name". Empty means
    the app's own repository (community_ratings.DEFAULT_REPO) - this is
    here so a fork or a private mirror can be pointed at without a new
    build, not because anybody is expected to set it."""
    return str(_load().get("ratings_repo") or "")


def set_ratings_repo(value: str):
    data = _load()
    data["ratings_repo"] = (value or "").strip()
    storage.save(SETTINGS_FILE, data)


def get_ratings_branch() -> str:
    """The branch inside that repository. Empty means the default."""
    return str(_load().get("ratings_branch") or "")


def set_ratings_branch(value: str):
    data = _load()
    data["ratings_branch"] = (value or "").strip()
    storage.save(SETTINGS_FILE, data)


def get_api_key(name: str) -> str:
    """One of API_KEYS, or "" when the user has not supplied it."""
    return str(_load().get(f"{name}_api_key") or "")


def set_api_key(name: str, value: str):
    data = _load()
    data[f"{name}_api_key"] = (value or "").strip()
    storage.save(SETTINGS_FILE, data)


def configured_api_keys() -> list:
    """Which keys actually have a value - what a source or translator
    checks before offering itself."""
    return [name for name in API_KEYS if get_api_key(name)]


def get_subdl_key() -> str:
    """Kept as its own name because helpers/subtitles.py already calls
    it; it now reads the same store as every other key."""
    return get_api_key("subdl")


def set_subdl_key(value: str):
    set_api_key("subdl", value)


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
