"""Try the in-app update flow without waiting for a release to exist.

    python packaging/test_update.py            an update is waiting
    python packaging/test_update.py current    you are already up to date
    python packaging/test_update.py broken     the check fails
    python packaging/test_update.py live       ask the real GitHub, read-only

Atomic checks for an update a few seconds after launch and says so with
a toast and an accent dot on the Settings button (roadmap #13). That path
is hard to see on purpose - it only fires when a *newer* release exists,
and this build's version already sorts above the newest one - so this
stands a fake answer in front of it and launches the real app.

What it will not do: touch your real data, or replace Atomic.exe. Your
data directory is copied to a temp folder and the app is pointed at the
copy, and the download step is replaced with one that refuses - so
clicking through to "Install" in Settings tells you it declined rather
than swapping the binary under you. The temp copy is deleted when you
close the app.

Run it from the repo root, with the app closed.
"""

import shutil
import sys
import tempfile
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

from helpers import storage  # noqa: E402

REAL_DATA = Path.home() / "AppData" / "Roaming" / "Atomic"

# What the fake answer offers. Two parts, well above anything released,
# so the app's own "is this newer than me" comparison says yes.
FAKE_UPDATE = {
    "version": "9.9",
    "tag": "v9.9",
    "url": "https://example.invalid/Atomic.exe",
    "size": 47_000_000,
    "sha": "0" * 40,
}


def temp_copy_of_real_data():
    """A throwaway copy of the real data directory, and the app pointed
    at it. Never the real one: this launches the app for real, and the
    app writes - a notified-version marker, a window geometry, a page's
    own housekeeping."""
    temp = Path(tempfile.mkdtemp(prefix="atomic-update-test-"))
    data = temp / "data"
    if REAL_DATA.exists():
        shutil.copytree(REAL_DATA, data)
        print(f"copied your data to {data}")
    else:
        data.mkdir()
        print(f"no data at {REAL_DATA} - starting empty")
    storage.DATA_DIR = data
    return temp


def main(argv):
    mode = (argv[1] if len(argv) > 1 else "available").lower()
    if mode not in ("available", "current", "broken", "live"):
        raise SystemExit(__doc__)

    temp = temp_copy_of_real_data()
    from helpers import updater

    # The startup check gives up immediately when the app is not frozen -
    # from source there is no executable to replace - so without this the
    # toast, the dot and the reminder can never appear and the whole
    # start-up half of the feature goes untested. Only the *check* is
    # fooled; download_update below still refuses, so nothing can be
    # written over.
    updater.is_frozen = lambda: True

    if mode == "available":
        updater.check_for_update = lambda timeout=10: dict(FAKE_UPDATE)
        print("pretending Atomic 9.9 is available")
    elif mode == "current":
        updater.check_for_update = lambda timeout=10: None
        print("pretending this build is the newest there is")
    elif mode == "broken":
        def refuse(timeout=10):
            raise updater.UpdateError("pretending GitHub is unreachable")
        updater.check_for_update = refuse
        print("pretending the check fails")
    else:
        # The real call, with this build's version lowered so the newest
        # published release counts as newer. Nothing is downloaded.
        updater.APP_VERSION = "0.1"
        print("asking GitHub for real, as though this build were 0.1")

    # The download is refused in every mode. Clicking Install in Settings
    # then reports that rather than fetching and swapping the executable,
    # which is the one thing a test must not do to a working install.
    def no_download(update, progress=None, timeout=60):
        raise updater.UpdateError(
            "This is the update test - nothing was downloaded or replaced.")
    updater.download_update = no_download

    print("\nwhat to look for:")
    if mode == "available":
        print("  - a toast, a few seconds after the window opens:")
        print("      Atomic 9.9 Is Available - Install It in Settings")
        print("  - an accent dot on the Settings button, bottom left")
        print("  - Settings > General > Version: offers 9.9, and Install")
        print("    reports the test refused the download")
        print("  - close and run this again: the toast is replaced by an")
        print("    Update Available alert - told once, reminded after")
    elif mode == "current":
        print("  - no toast and no dot")
        print("  - Settings > General > Version: 'There is No New Update'")
    elif mode == "broken":
        print("  - no toast and no dot: a check nobody asked for stays")
        print("    silent when it fails")
        print("  - Settings > General > Version: the failure, in words")
    else:
        print("  - whatever GitHub actually has - a toast naming the newest")
        print("    published release, or nothing if none is newer than 0.1")

    print("\nclose the window when you're done; the copy is deleted then.\n")

    import main as app_main
    try:
        app_main.main()
    finally:
        shutil.rmtree(temp, ignore_errors=True)
        print(f"\ndeleted {temp}")


if __name__ == "__main__":
    main(sys.argv)
