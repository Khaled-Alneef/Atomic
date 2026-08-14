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

from . import icon_extract, images, storage

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


def scan_launcher(root_dir: str, common_subpath: str = None, launcher_label: str = None):
    """Every game found under one launcher's install directory, as
    {"name": folder name, "path": guessed .exe}. Skips folders no
    plausible .exe could be found in, and the launcher's own internal
    folders (see _is_launcher_internal_folder) - `launcher_label` is
    needed for the latter check and is optional only for convenience
    (callers with just a raw directory and no known launcher identity
    still get the fixed-name checks, just not the label-based ones)."""
    root = Path(root_dir)
    games_root = (root / common_subpath) if common_subpath else root
    if not games_root.is_dir():
        return []
    results = []
    for entry in sorted(games_root.iterdir()):
        if not entry.is_dir() or _is_launcher_internal_folder(entry.name, launcher_label):
            continue
        exe = _pick_game_exe(entry)
        if exe:
            results.append({"name": entry.name, "path": str(exe)})
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
        for game in scan_launcher(root_dir, subpath, label):
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
        })
    if new_games:
        games.extend(new_games)
        storage.save(GAMES_FILE, games)
    return len(new_games)
