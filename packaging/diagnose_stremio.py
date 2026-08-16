"""Is your Stremio watch progress reaching Atomic, and if not, where did
it stop?

Run:  python packaging/diagnose_stremio.py [--data-dir PATH] [--limit N]

No password - only the session key Settings already stored, and it is
never printed. Reads your data directory and writes nothing to it, which
is enforced rather than intended: storage.load moves a file it cannot
parse aside, and storage/logs both write into the data directory, so all
three are neutered below before anything is read.

Stremio is the only watch-progress source there is (see
.claude/rules/integrations.md, "settled" - AniList by username, two
Crunchyroll clients and a pasted Crunchyroll token were each tried and
removed). That makes "my card shows the wrong episode" a chain with five
links, and until stremio.AuthFailed existed, links 3 and 5 arrived
looking exactly alike - a revoked key read as "not in your library", for
every entry, indefinitely:

  1. an account is connected in Settings
  2. Stremio answers this machine at all
  3. the stored session key is still accepted
  4. the entry carries a Cinemeta id to look up - an Anime entry set to
     open on a video website saves that site's page url instead of a
     stremio:// link, and one saved with no id at all never syncs no
     matter how healthy the account is
  5. the title is in your Stremio library with progress on it - Stremio
     only records an episode you resumed or ticked watched

Exit code is 0 when the chain is intact, 1 when a link is broken.
"""
import argparse
import json
import logging
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from helpers import app_settings, logs, storage, stremio  # noqa: E402

# A real IMDb id that is not in anybody's library by default. It is here
# to ask the account API one question - "is this key still accepted?" -
# separately from any of the user's own titles, because a valid key and a
# dead one both answer None for a title you have not watched.
PROBE_IMDB_ID = "tt0944947"  # Game of Thrones

# tracker.json holds Anime (and Reading, filtered out below); series.json
# is the Series page. Named here rather than imported from the page
# classes so this script does not depend on Qt importing cleanly.
DATA_FILES = ("tracker.json", "series.json")


def _make_read_only():
    """Make it impossible for this tool to write into the data directory.

    Not paranoia about a hypothetical: storage.load renames an unparseable
    file to <name>.corrupt, and logs.exception creates atomic.log in
    DATA_DIR - so simply *reading* a damaged tracker.json with the real
    directory selected would modify it. A diagnostic that alters the thing
    it was called to inspect is worse than no diagnostic."""
    blocked = []

    def refuse(*_args, **_kwargs):
        blocked.append(1)
        return None

    storage.save = refuse
    storage._quarantine = refuse
    # Pre-seeding the cached logger stops logger() from ever building its
    # RotatingFileHandler, which is what would create the file.
    quiet = logging.getLogger("atomic-diagnose")
    quiet.addHandler(logging.NullHandler())
    quiet.propagate = False
    logs._logger = quiet
    return blocked


def _pick_data_dir(override):
    """Which data directory to inspect.

    Run from source, storage.DATA_DIR is src/data - the dev copy, not the
    one the installed Atomic.exe actually uses (%APPDATA%\\Atomic). This
    tool is reached for when the *installed* app looks wrong, so the
    frozen app's directory wins when it exists, and the choice is printed
    rather than assumed."""
    if override:
        return Path(override), "given on the command line"
    frozen_dir = Path(os.getenv("APPDATA") or Path.home()) / "Atomic"
    if frozen_dir.exists():
        return frozen_dir, "where the installed Atomic.exe keeps its data"
    return Path(storage.DATA_DIR), "storage.DATA_DIR (no installed copy found)"


def _imdb_id(entry):
    """Same rule as tracker._entry_imdb_id: the id stored on the entry,
    falling back to the one inside a saved stremio:// url for entries
    written before that field existed. Only a stremio:// url is mined - a
    site slug containing "tt" and digits would otherwise resolve every
    later lookup to the wrong show."""
    stored = entry.get("imdb_id")
    if stored:
        return stored
    url = entry.get("url") or ""
    if not url.startswith("stremio://"):
        return None
    match = re.search(r"tt\d+", url)
    return match.group(0) if match else None


def _tracked_titles(limit):
    """Every entry the app would try to sync: video types only, and not
    Movie (one video, no episode to be on - tracker.UNTRACKED_TYPES).

    Returns (titles, unreadable_filenames). "Unreadable" is told apart
    from "empty" rather than reported as one thing: storage.load answers
    [] for both, and a tracker.json the app cannot parse is itself a
    broken link - the app would be showing an empty page, not a wrong
    episode."""
    syncable = []
    unreadable = []
    for filename in DATA_FILES:
        path = Path(storage.DATA_DIR) / filename
        entries = storage.load(filename, [])
        if not isinstance(entries, list):
            unreadable.append(filename)
            continue
        if path.exists() and not entries:
            try:
                raw = path.read_text(encoding="utf-8-sig").strip()
                json.loads(raw or "[]")
            except (OSError, ValueError):
                unreadable.append(filename)
        for entry in entries:
            if entry.get("type") not in ("Anime", "Series"):
                continue
            title = entry.get("title") or "(untitled)"
            syncable.append((title, _imdb_id(entry), entry.get("progress")))
    syncable.sort(key=lambda item: item[0].lower())
    return (syncable[:limit] if limit else syncable), unreadable


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data-dir", help="inspect this data directory instead")
    parser.add_argument("--limit", type=int, default=0,
                        help="check only the first N titles (each is one request)")
    args = parser.parse_args(argv)

    blocked_writes = _make_read_only()
    data_dir, why = _pick_data_dir(args.data_dir)
    storage.DATA_DIR = data_dir
    print(f"data directory: {data_dir}")
    print(f"                ({why}, read-only)\n")

    # 1. Is an account connected at all?
    try:
        email, auth_key = app_settings.get_stremio_auth()
    except Exception as exc:
        print(f"1. could not read settings.json: {type(exc).__name__}: {exc}")
        return 1
    if not auth_key:
        print("1. No Stremio account connected - Settings > Stremio Account.")
        print("   Nothing can sync until one is; this is the whole answer.")
        return 1
    # Length only. The key is a live session token, and this output is the
    # kind of thing that gets pasted into a chat.
    print(f"1. connected as: {email or '(no email stored)'} "
          f"(session key present, {len(auth_key)} chars)")

    # 2. Can this machine reach Stremio? Cinemeta rather than the account
    # API, because it needs no key - so a failure here is the network, not
    # the sign-in, and the next check can be read as meaning what it says.
    print("\n2. is Stremio reachable?")
    try:
        reachable = bool(stremio.search("Frieren", "series"))
    except Exception as exc:
        reachable = False
        print(f"   search raised {type(exc).__name__}: {exc}")
    if reachable:
        print("   yes - Cinemeta answered a search.")
    else:
        print("   no answer from Cinemeta. Everything below will look empty")
        print("   for that reason alone; fix the connection first.")

    # 3. Is the stored key still accepted? Asked against a title that is
    # almost certainly not in the library, so the only interesting outcome
    # is AuthFailed. Either answer (None or a tuple) means accepted.
    print("\n3. does Stremio still accept the saved session?")
    try:
        stremio.fetch_watch_progress(PROBE_IMDB_ID, auth_key)
        print("   yes - the account API answered without rejecting the key.")
        key_dead = False
    except stremio.AuthFailed as exc:
        key_dead = True
        print(f"   no - Stremio rejected it: {exc}")
        print("   Sign in again in Settings > Stremio Account. Every entry")
        print("   below would otherwise read as 'not in your library'.")
    except Exception as exc:
        key_dead = False
        print(f"   could not tell - {type(exc).__name__}: {exc}")

    # 4. Which entries can even be looked up?
    print("\n4. tracked Anime/Series entries")
    try:
        titles, unreadable = _tracked_titles(args.limit)
    except Exception as exc:
        print(f"   could not read the tracker files: {type(exc).__name__}: {exc}")
        return 1
    for filename in unreadable:
        print(f"   {filename} could not be read - the app would be showing an")
        print(f"   {'':{len(filename)}} empty page, which is its own broken link.")
    if not titles:
        print("   none tracked - nothing to sync.")
        # Unreadable files are a fault; genuinely having nothing tracked
        # is not, and must not report one.
        return 1 if unreadable else 0
    missing_id = [t for t, imdb, _ in titles if not imdb]
    print(f"   {len(titles)} entries, {len(titles) - len(missing_id)} with a Cinemeta id")
    for title in missing_id:
        print(f"   {title[:34]:36} no imdb id - re-pick it in Edit, it can")
        print(f"   {'':36} never sync as saved")

    if key_dead:
        print("\nStopping here: with the session rejected, every lookup below")
        print("would answer 'not in your library' whatever the truth is.")
        return 1

    # 5. What does the library actually say for each?
    print("\n5. what your Stremio library says")
    answered = 0
    absent = 0
    failed = 0
    for title, imdb, stored in titles:
        if not imdb:
            continue
        shown = f"{title[:34]:36}"
        try:
            found = stremio.fetch_watch_progress(imdb, auth_key)
        except stremio.AuthFailed as exc:
            # Reached despite the probe passing: the key expired mid-run,
            # or the probe hit a cached/edge answer. Say so and stop.
            print(f"   {shown} session rejected: {exc}")
            failed += 1
            key_dead = True
            break
        except Exception as exc:
            failed += 1
            print(f"   {shown} lookup failed: {type(exc).__name__}")
            continue
        if found is None:
            absent += 1
            print(f"   {shown} not in your library yet (Atomic shows "
                  f"{stored or '-'})")
        else:
            answered += 1
            season, episode = found
            print(f"   {shown} S{season}E{episode} (Atomic shows "
                  f"{stored or '-'})")

    print()
    if blocked_writes:
        count = len(blocked_writes)
        print(f"({count} write{'' if count == 1 else 's'} into the data "
              f"directory {'was' if count == 1 else 'were'} blocked - nothing "
              f"was modified.)")
    if key_dead:
        print("The saved session is dead. Re-connect in Settings.")
        return 1
    if answered:
        print(f"Stremio answered for {answered} of {answered + absent} titles.")
        print("Where Atomic disagrees with the number above, open the Anime")
        print("or Series page - progress syncs on arrival - and check that")
        print("the entry is the same title Stremio has (a wrong Cinemeta id")
        print("syncs the wrong show, correctly).")
        return 0
    if failed and not absent:
        print("Every lookup failed outright. That is the connection or the")
        print("Stremio API, not your library.")
        return 1
    print("The session works but no tracked title has progress in your")
    print("Stremio library. Stremio records an episode when you resume it")
    print("or tick it watched - watching in another player writes nothing,")
    print("and there is no second source that could know (integrations.md).")
    return 1 if not reachable else 0


if __name__ == "__main__":
    raise SystemExit(main())
