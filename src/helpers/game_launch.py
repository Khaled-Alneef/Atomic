"""Starting an imported game the way its own launcher would, instead of
by running the .exe that was found on disk.

Running the raw binary only ever looked right, and it is wrong for every
launcher game. Two failures it caused here:

  * a Steam game started outside Steam is invisible to it - the client
    still says the game isn't running, so no overlay, no playtime, no
    cloud saves;
  * Overwatch, started as `_retail_/Overwatch.exe` with no Battle.net
    ticket, puts its own sign-in screen up inside the game.

Every command below was read off the shortcuts the launchers themselves
wrote on this machine, not guessed at:

    Overwatch.lnk -> "...\\Overwatch\\Overwatch Launcher.exe" --productcode=pro
    VALORANT.lnk  -> "...\\Riot Client\\RiotClientServices.exe"
                     --launch-product=valorant --launch-patchline=live

and both identifiers are read back off disk (`.build.info`'s Product
column; Riot's RiotClientInstalls.json) rather than from a hardcoded
table of Blizzard product codes, which would need a new row per game and
silently launch nothing for anything missing from it.

Steam and Epic take their launcher's URL scheme instead - the same thing
their own desktop shortcuts resolve to, and it works whether or not the
client is already running.

Xbox / Game Pass is deliberately not handled: its target is
`shell:appsFolder\\<PackageFamilyName>!App`, and the family name (with
its publisher hash) is not in the install folder. Nothing was installed
to measure against either, so an Xbox game still starts by path.

An entry with no resolved command - a game added by hand through the
file picker, a Steam folder with no manifest, anything under a launcher
not covered here - falls back to exactly what happened before: run the
stored path.
"""

import json
import os
import re
import subprocess
from pathlib import Path

from . import child_process

# Steam's appmanifest_<appid>.acf is a Valve KeyValues file; only two of
# its fields matter here, so it is read with a regex rather than pulling
# in a VDF parser for eight lines of work.
_ACF_APPID = re.compile(r'"appid"\s+"(\d+)"')
_ACF_INSTALLDIR = re.compile(r'"installdir"\s+"([^"]+)"')

_RIOT_INSTALLS = Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "Riot Games" / "RiotClientInstalls.json"
_EPIC_MANIFESTS = Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "Epic" / "EpicGamesLauncher" / "Data" / "Manifests"


def _key(path) -> str:
    """A path in the one form two spellings of it compare equal in -
    Riot's JSON writes forward slashes and a trailing separator, Windows
    hands us backslashes and none."""
    return os.path.normcase(os.path.normpath(str(path)))


# ---------------------------------------------------------------- Steam
def _steam_index(root: Path) -> dict:
    """{install folder name -> appid} for one Steam library, read from
    the appmanifest_*.acf files sitting beside steamapps/common. A folder
    with no manifest (an uninstalled game's leftovers, or one copied in
    by hand) simply isn't in here and falls back to the path."""
    index = {}
    steamapps = root / "steamapps"
    if not steamapps.is_dir():
        return index
    for manifest in steamapps.glob("appmanifest_*.acf"):
        try:
            text = manifest.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        app_id = _ACF_APPID.search(text)
        install_dir = _ACF_INSTALLDIR.search(text)
        if app_id and install_dir:
            index[os.path.normcase(install_dir.group(1))] = app_id.group(1)
    return index


def _steam_command(index: dict, game_dir: Path):
    app_id = index.get(os.path.normcase(game_dir.name))
    # rungameid, not launch: this is the id Steam's own desktop shortcuts
    # use, and it starts the client first if it isn't up.
    return {"uri": f"steam://rungameid/{app_id}"} if app_id else None


# ----------------------------------------------------------- Battle.net
def _battlenet_product(game_dir: Path):
    """The product code out of the game's `.build.info` - a pipe-
    separated table whose header names each column ("Product!STRING:0"),
    holding `pro` for Overwatch, `wow`, `fenris`, and so on. Read by
    column name rather than position: the column order differs between
    products and changes between client versions."""
    build_info = game_dir / ".build.info"
    try:
        lines = build_info.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return None
    if len(lines) < 2:
        return None
    header = [col.split("!")[0].strip().lower() for col in lines[0].split("|")]
    if "product" not in header:
        return None
    column = header.index("product")
    for row in lines[1:]:
        fields = row.split("|")
        if len(fields) > column and fields[column].strip():
            return fields[column].strip()
    return None


def _battlenet_command(index, game_dir: Path):
    """Blizzard's own shortcut runs the game's `* Launcher.exe` with
    --productcode; that binary is what talks to Battle.net for the login
    ticket the game then needs. Note the scan itself skips this exe (any
    path containing "launcher" is filtered out as installer noise), which
    is why it's found here by name instead of reusing the stored path."""
    product = _battlenet_product(game_dir)
    if not product:
        return None
    for exe in sorted(game_dir.glob("* Launcher.exe")):
        return {"path": str(exe), "args": [f"--productcode={product}"]}
    return None


# ------------------------------------------------------------ Riot
def _riot_index() -> dict:
    try:
        return json.loads(_RIOT_INSTALLS.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}


def _riot_command(index: dict, game_dir: Path):
    """Riot ships one client for every game: RiotClientServices.exe with
    --launch-product/--launch-patchline. `associated_client` maps each
    installed patchline directory ("G:/Riot Games/VALORANT/live/") to the
    client that owns it, which gives both halves - the patchline is that
    path's last folder. rc_default covers a game whose entry is missing,
    with "live" as the patchline every install here uses."""
    game_key = _key(game_dir)
    client = patchline = None
    for install, exe in (index.get("associated_client") or {}).items():
        install_path = Path(os.path.normpath(install))
        if _key(install_path).startswith(game_key + os.sep):
            client, patchline = exe, install_path.name
            break
    client = client or index.get("rc_default") or index.get("rc_live")
    if not client or not os.path.exists(client):
        return None
    return {"path": client, "args": [
        f"--launch-product={game_dir.name.lower()}",
        f"--launch-patchline={patchline or 'live'}",
    ]}


# ------------------------------------------------------------ Epic
def _epic_index() -> dict:
    """{install location -> Epic's internal AppName} from the launcher's
    manifest folder. Unverified against a real game: no Epic titles are
    installed here, so this path has been written to the documented
    manifest shape and left to fall back to the exe if it finds nothing
    rather than guessing an AppName from the folder."""
    index = {}
    if not _EPIC_MANIFESTS.is_dir():
        return index
    for item in _EPIC_MANIFESTS.glob("*.item"):
        try:
            data = json.loads(item.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        location, app_name = data.get("InstallLocation"), data.get("AppName")
        if location and app_name:
            index[_key(location)] = app_name
    return index


def _epic_command(index: dict, game_dir: Path):
    app_name = index.get(_key(game_dir))
    if not app_name:
        return None
    return {"uri": f"com.epicgames.launcher://apps/{app_name}?action=launch&silent=true"}


# ------------------------------------------------------------ dispatch
_INDEXERS = {
    "steam": lambda root: _steam_index(Path(root)),
    "battlenet": lambda root: None,
    "riotgames": lambda root: _riot_index(),
    "epicgames": lambda root: _epic_index(),
}

_RESOLVERS = {
    "steam": _steam_command,
    "battlenet": _battlenet_command,
    "riotgames": _riot_command,
    "epicgames": _epic_command,
}


def index_for(launcher_key: str, root_dir):
    """Whatever `command_for` needs to answer for a whole launcher at
    once - Steam's manifest table, Epic's, Riot's installs file. Built
    once per scan instead of per game: a library of 25 games would
    otherwise re-read the same manifests 25 times."""
    builder = _INDEXERS.get(launcher_key)
    try:
        return builder(root_dir) if builder else None
    except OSError:
        return None


def command_for(launcher_key: str, index, game_dir):
    """How to start the game installed in `game_dir`, as
    {"uri": ...} or {"path": ..., "args": [...]}. None when this
    launcher has no known way in, or this particular game can't be
    identified - the caller then keeps launching by path."""
    resolver = _RESOLVERS.get(launcher_key)
    if not resolver:
        return None
    try:
        return resolver(index, Path(game_dir))
    except OSError:
        return None


def resolve(launcher_key: str, root_dir, game_dir):
    """One game's command, index built on the spot. For backfilling a
    handful of already-imported entries; a scan uses index_for."""
    return command_for(launcher_key, index_for(launcher_key, root_dir), game_dir)


def _stamp_played(game):
    """Record that this game was just started.

    **Here, not in the page that called.** The owner, 4 September 2026:
    "when I open a game from the main page make it also comes 1st on the
    left like the apps and the websites." Apps and websites were the same
    bug and were fixed on the same day: only the Qt shelf's own
    `_launch` wrote the stamp, and Home is a web page whose click never
    reaches that method - so server._recent_first had nothing to sort by
    and the game stayed where it was. windows/link_grid._stamp_used is
    the twin of this for the other two shelves.

    One field on one entry through storage.update_entry, never the whole
    list back - the write that once erased freshly imported games
    (rules/ui.md).
    """
    entry_id = game.get("id")
    if not entry_id:
        return
    try:
        from . import storage
        stamp = storage.now_iso()
        game["last_played"] = stamp
        storage.update_entry("games.json", entry_id, {"last_played": stamp})
    except Exception:
        pass            # a launch must never fail on bookkeeping


def run(game):
    """Start `game`, through its launcher when one was resolved. Raises
    OSError, which is what every call site already reports on."""
    _stamp_played(game)
    command = game.get("launch") or {}
    uri = command.get("uri")
    if uri:
        # os.startfile, not `cmd /c start`: a launcher URI carries query
        # parameters (Epic's has an &) that cmd would read as its own
        # syntax. ShellExecute hands the child this process's
        # environment, hence the strip - see helpers/child_process.
        with child_process.clean_environ():
            os.startfile(uri)
        return
    resolved = command.get("path")
    path = resolved or game.get("path")
    # shell=True only on the fallback: a hand-added entry may be a .lnk,
    # which CreateProcess can't run and ShellExecute can. A resolved
    # command is always a real .exe with arguments that must reach it
    # unmangled, so that one goes direct.
    subprocess.Popen([path, *(command.get("args") or [])], shell=not resolved,
                     cwd=str(Path(path).parent), env=child_process.clean_env(),
                     creationflags=child_process.flags())
