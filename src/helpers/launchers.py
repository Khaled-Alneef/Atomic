"""Bulk-discover installed games from launcher install directories
(Steam/Battle.net/Epic Games/Riot Games/Xbox), instead of adding each one
by hand through a file picker.

Each launcher installs every game as its own subfolder somewhere under
the directory the user points at in Settings > Games - Steam nests that
one level deeper, under steamapps/common; the others put game folders
directly under the root (their default install layout). Scanning is a
best-effort heuristic (see _pick_game_exe) since there's no manifest
telling us which .exe in a game's folder is "the game" versus an
updater/uninstaller/redistributable installer living alongside it - the
result is meant to be reviewed (and pruned via Edit/Delete on the Games
page) after import, not blindly trusted.
"""

import hashlib
import os
import uuid
from pathlib import Path

from . import game_launch, icon_extract, images, storage

GAMES_FILE = "games.json"
ICON_EXTRACT_SIZE = 96

# (settings key, display label, subpath from the configured root down to
# where each game's own folder lives - None means directly under root).
LAUNCHERS = [
    ("steam", "Steam", "steamapps/common"),
    ("battlenet", "Battle.net", None),
    ("epicgames", "Epic Games", None),
    ("riotgames", "Riot Games", None),
    # Xbox app / PC Game Pass installs put each game in its own folder
    # directly under the configured root too (the actual .exe then sits a
    # level or two deeper, under a "Content" subfolder - already handled
    # by _candidate_exes' recursive per-game-folder search, same as the
    # nesting in every other launcher above).
    ("xbox", "Xbox", None),
]

# How many folder levels deep to look for a .exe inside one game's own
# folder - generous enough for the common "Binaries/Win64/Game.exe"
# nesting pattern without walking into every last asset subfolder.
_MAX_DEPTH = 4

_EXCLUDE_PATTERNS = (
    "unins", "uninstall", "setup", "install", "redist", "vcredist",
    "directx", "dxsetup", "crashpad", "crashhandler", "crashreporter",
    "battleye", "easyanticheat", "eac_", "eossdk", "dotnet", "updater",
    "vc_redist", "vulkan", "prereq", "launcher", "runtime",
)

# Folder names that are never a game, no matter how game-folder-shaped
# they look - a launcher's own app/update-cache folders (or, for Steam,
# its shared-redistributables folder) often sit right alongside real
# game folders under the same configured root. Checked against the
# *normalized* name (see _normalize), so case/punctuation don't matter.
_KNOWN_NON_GAME_FOLDERS = {"launcher", "riotclient", "epiconlineservices", "steamworksshared"}


def _is_launcher_internal_folder(folder_name: str, launcher_label: str) -> bool:
    """True for a launcher's own folder, not a game - either a known
    fixed name (e.g. Epic's "Launcher" subfolder) or the launcher's own
    name optionally followed by a version/build number, the pattern
    Battle.net uses for its self-update cache ("Battle.net.17296",
    "Battle.net.651", ...) - those sit directly under the same root as
    real game folders like "Overwatch", so a plain per-game .exe scan
    would otherwise treat them as games too."""
    key = _normalize(folder_name)
    if key in _KNOWN_NON_GAME_FOLDERS:
        return True
    label_key = _normalize(launcher_label or "")
    if label_key and key.startswith(label_key):
        return key == label_key or key[len(label_key):].isdigit()
    return False


def _candidate_exes(game_dir: Path):
    """Every .exe under `game_dir`, up to _MAX_DEPTH levels deep, minus
    the obvious non-game noise (installers/anti-cheat/redistributables)
    that ships alongside the real binary in a lot of game folders. The
    exclude-pattern check runs against the *whole relative path* under
    game_dir, not just the file's own name - a lot of that noise lives
    in a vendor-named subfolder instead of having a giveaway filename of
    its own (e.g. "_CommonRedist/DotNet/4.7/NDP472-KB4054530....exe",
    whose own filename gives no hint it's a .NET Framework installer,
    but whose path clearly does)."""
    root_depth = len(game_dir.parts)
    for dirpath, dirnames, filenames in os.walk(game_dir):
        depth = len(Path(dirpath).parts) - root_depth
        if depth >= _MAX_DEPTH:
            dirnames[:] = []
        for name in filenames:
            if not name.lower().endswith(".exe"):
                continue
            full_path = Path(dirpath) / name
            relative = str(full_path.relative_to(game_dir)).lower()
            if any(pattern in relative for pattern in _EXCLUDE_PATTERNS):
                continue
            yield full_path


def _normalize(text: str) -> str:
    return "".join(ch for ch in text.lower() if ch.isalnum())


def _pick_game_exe(game_dir: Path):
    """Best-effort guess at which .exe in a game's folder is the actual
    game: prefer one whose filename matches the folder name (e.g.
    "Portal 2/portal2.exe"), else fall back to the largest .exe found
    (installers/updaters/uninstallers that slip past the name filter are
    typically much smaller than the real game binary). None if no .exe
    was found at all."""
    candidates = list(_candidate_exes(game_dir))
    if not candidates:
        return None
    folder_key = _normalize(game_dir.name)
    for exe in candidates:
        if _normalize(exe.stem) == folder_key:
            return exe
    return max(candidates, key=lambda p: p.stat().st_size if p.exists() else 0)


def scan_launcher(root_dir: str, common_subpath: str = None, launcher_label: str = None,
                  launcher_key: str = None):
    """Every game found under one launcher's install directory, as
    {"name": folder name, "path": guessed .exe, "launch": how to start
    it through its launcher (see helpers/game_launch) or None}. Skips
    folders no plausible .exe could be found in, and the launcher's own
    internal folders (see _is_launcher_internal_folder) - `launcher_label`
    is needed for the latter check and is optional only for convenience
    (callers with just a raw directory and no known launcher identity
    still get the fixed-name checks, just not the label-based ones).
    `launcher_key` is what resolves the launch command; without it the
    games still import and still start by path."""
    root = Path(root_dir)
    games_root = (root / common_subpath) if common_subpath else root
    if not games_root.is_dir():
        return []
    # Built once for the whole directory, not per game - see index_for.
    index = game_launch.index_for(launcher_key, root_dir) if launcher_key else None
    results = []
    for entry in sorted(games_root.iterdir()):
        if not entry.is_dir() or _is_launcher_internal_folder(entry.name, launcher_label):
            continue
        exe = _pick_game_exe(entry)
        if exe:
            command = game_launch.command_for(launcher_key, index, entry) if launcher_key else None
            results.append({"name": entry.name, "path": str(exe), "launch": command})
    return results


def scan_all(launcher_dirs: dict):
    """`launcher_dirs` maps launcher key -> configured root dir; entries
    with no directory configured are skipped. Returns a flat list of
    {"name", "path", "launcher"} across every configured launcher."""
    by_key = {key: (label, subpath) for key, label, subpath in LAUNCHERS}
    results = []
    for key, root_dir in launcher_dirs.items():
        if not root_dir:
            continue
        label, subpath = by_key.get(key, (None, None))
        for game in scan_launcher(root_dir, subpath, label, key):
            results.append({**game, "launcher": key})
    return results


def extract_and_cache_icon(path, size=ICON_EXTRACT_SIZE):
    """Shared with windows.games (which also lets a user pick/override a
    game's icon by hand) - kept here rather than only there since
    Settings' per-launcher auto-import (see settings_dialog.py) needs it
    too, without a helpers/ module reaching into windows/ just for this.

    Cached under a name derived from the executable's path rather than a
    random one, so re-extracting the same game (a re-import, or the
    backfill below re-running) lands on the file it already wrote
    instead of leaving a trail of orphaned copies behind it."""
    digest = hashlib.sha1(os.path.normcase(str(path)).encode("utf-8")).hexdigest()
    dest = images.CACHE_DIR / f"game_{digest}.png"
    if dest.exists():
        return str(dest)
    img = icon_extract.extract_icon(path, size=size)
    if img is None:
        return None
    try:
        img.save(dest)
        return str(dest)
    except Exception:
        return None


def backfill_missing_icons(games=None):
    """Give every saved game an icon it can actually show, re-extracting
    for any whose icon was never captured or whose cached file has since
    gone missing. Returns the up-to-date list.

    Called by both the Games page and Home: Home renders the same icons
    but used to just fall back to a letter avatar when one was absent,
    so a game could sit there as a colored initial until the Games page
    happened to be opened and fix it."""
    games = storage.load(GAMES_FILE, []) if games is None else games
    for game in games:
        icon = game.get("icon")
        if icon and os.path.exists(icon):
            continue
        path = game.get("path")
        if not path or not os.path.exists(path):
            continue
        extracted = extract_and_cache_icon(path)
        if extracted and extracted != icon:
            game["icon"] = extracted
            storage.update_entry(GAMES_FILE, game.get("id"), {"icon": extracted})
    return games


def backfill_launch_commands(games=None):
    """Give games imported before launch commands existed (and any whose
    launcher has since been configured, or reinstalled somewhere else)
    the command that starts them through their launcher. Returns the
    up-to-date list.

    Matching is by path: a saved game whose .exe lives under a configured
    launcher root belongs to that launcher, and its own folder is the
    first one below that root. Nothing is re-scanned - this reads the
    same manifests a scan would, for the entries that lack an answer.

    Called from the Games page and Home for the same reason
    backfill_missing_icons is: the alternative is telling the user to
    re-import a library they already have."""
    from . import app_settings  # helpers-level cycle: app_settings imports storage, not this

    games = storage.load(GAMES_FILE, []) if games is None else games
    pending = [g for g in games if not g.get("launch") and g.get("path")]
    if not pending:
        return games
    dirs = app_settings.get_launcher_dirs()
    indexes = {}
    for key, label, subpath in LAUNCHERS:
        root_dir = dirs.get(key)
        if not root_dir:
            continue
        games_root = Path(root_dir) / subpath if subpath else Path(root_dir)
        prefix = os.path.normcase(os.path.normpath(str(games_root))) + os.sep
        for game in pending:
            if game.get("launch"):
                continue
            path = os.path.normpath(game["path"])
            if not os.path.normcase(path).startswith(prefix):
                continue
            # The game's own folder, not the .exe's - a game exe often
            # sits several levels down (Binaries/Win64, _retail_). Taken
            # off the un-normcased path: the folder name is matched
            # against a manifest's, and normcase would lowercase it.
            game_dir = games_root / Path(os.path.relpath(path, games_root)).parts[0]
            if key not in indexes:
                indexes[key] = game_launch.index_for(key, root_dir)
            command = game_launch.command_for(key, indexes[key], game_dir)
            if command:
                game["launch"] = command
                game["launcher"] = key
                storage.update_entry(GAMES_FILE, game.get("id"),
                                     {"launch": command, "launcher": key})
    return games


def import_scanned_games(found):
    """Add newly-discovered games (from scan_launcher/scan_all) straight
    into the saved games list, skipping any whose path is already there.
    Works directly off the saved file rather than needing a live Games
    page open, so Settings can trigger an import the moment a launcher
    directory is configured, not just via the Games page's own button.
    Returns how many were actually added."""
    games = storage.load(GAMES_FILE, [])
    existing = {os.path.normcase(os.path.normpath(g["path"])) for g in games}
    added_at = storage.now_iso()
    new_games = []
    for game in found:
        normalized = os.path.normcase(os.path.normpath(game["path"]))
        if normalized in existing:
            continue
        existing.add(normalized)
        new_games.append({
            "id": str(uuid.uuid4()), "name": game["name"], "path": game["path"],
            "icon": extract_and_cache_icon(game["path"]), "added_at": added_at, "last_played": None,
            # Which launcher owns it, and how to start it through that
            # launcher - a Steam game run straight off its .exe never
            # registers as running, and a Blizzard one asks to sign in.
            "launcher": game.get("launcher"), "launch": game.get("launch"),
        })
    if new_games:
        games.extend(new_games)
        storage.save(GAMES_FILE, games)
    return len(new_games)


def prune_uninstalled_games(launcher_dirs=None):
    """Drop saved games whose executable is no longer on disk. Returns
    the names removed, in saved order.

    The owner's ask, 28 August 2026: "make the games when I refresh
    removes the uninstalled games". Refresh already adds what a launcher
    has gained; a library that only ever grows means an uninstalled game
    keeps its tile forever and launching it does nothing.

    Two guards, both there because the failure mode is deleting somebody's
    library rather than one stale row:

    * **The game's own root has to still be there.** A path on an
      external drive that is simply not plugged in, or under a launcher
      directory that has been renamed, is missing for a reason that has
      nothing to do with the game being uninstalled - `os.path.exists`
      cannot tell those apart, but the parent directory can. A game is
      only dropped when the folder its .exe lived in is gone *and* that
      folder's own drive is still mounted.
    * **A game with no path is never touched.** Nothing on disk was ever
      claimed for it, so there is nothing to have gone away.

    `launcher_dirs` is accepted and unused for now: pruning is decided
    per game off its own path, which covers hand-added games too. It is
    in the signature because a caller that has the dirs to hand should
    not have to know that.
    """
    games = storage.load(GAMES_FILE, [])
    kept, removed = [], []
    for game in games:
        path = (game.get("path") or "").strip()
        if not path or _still_installed(path):
            kept.append(game)
        else:
            removed.append(game.get("name") or path)
    if removed:
        storage.save(GAMES_FILE, kept)
    return removed


def _still_installed(path: str) -> bool:
    """Whether `path` counts as an installed game right now.

    True whenever the file is there, and also true whenever the answer
    cannot be trusted - an unmounted drive answers False to
    `os.path.exists` for every file on it, and treating that as "these
    games were uninstalled" would empty the list on the day somebody
    unplugged a disk."""
    try:
        if os.path.exists(path):
            return True
        drive = os.path.splitdrive(os.path.abspath(path))[0]
        # No drive letter (a UNC share, or a relative path that should
        # not be here): not enough to conclude anything from.
        if not drive:
            return True
        if not os.path.exists(drive + os.sep):
            return True         # the disk is gone, not the game
    except OSError:
        return True
    return False


def prune_result_message(removed: list) -> str:
    """The removal half of a refresh, in one clause, worded to sit after
    `import_result_message`'s sentence."""
    if not removed:
        return ""
    if len(removed) == 1:
        return "1 Uninstalled Game Was Removed"
    return f"{len(removed)} Uninstalled Games Were Removed"


def import_result_message(added: int) -> str:
    """How an import went, in one sentence. Here rather than at either
    call site because both the Games page's Import button and Settings'
    per-launcher auto-import report the same thing and should word it the
    same way."""
    if not added:
        return "No New Games Found"
    if added == 1:
        return "1 Game Was Successfully Added"
    return f"{added} Games Was Successfully Added"
