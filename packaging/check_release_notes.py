"""Refuse to tag a release whose "what's new" notes were never written.

    python packaging/check_release_notes.py [version]

1.4 shipped with no entry in whats_new.NOTES, so everyone updating into
it opened an empty dialog - and `set_last_seen_version` recorded 1.4 as
seen anyway, so there was no second chance at it. The notes were written
afterwards (roadmap #4); this is what stops the next release doing the
same thing, at the one moment it can still be fixed.

Run from the release procedure, not from a build: a development build
has no notes of its own and never should.
"""

import ast
import re
import sys
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parent.parent / "src"
UPDATER_FILE = SRC_DIR / "helpers" / "updater.py"
WHATS_NEW_FILE = SRC_DIR / "helpers" / "whats_new.py"


def app_version() -> str:
    match = re.search(r'^APP_VERSION\s*=\s*"([^"]+)"',
                      UPDATER_FILE.read_text(encoding="utf-8"), re.MULTILINE)
    if not match:
        raise SystemExit(f"Couldn't find APP_VERSION in {UPDATER_FILE}.")
    return match.group(1)


def version_being_shipped(version: str) -> str:
    """Which version's notes to demand, from whatever APP_VERSION says.

    Both states are legitimate at the moment this runs. The release
    procedure sets APP_VERSION to the two-part number *before* the
    snapshot, so during a release this already reads "1.5" and that is
    the version shipping. Run earlier, on a development build, it reads
    "1.4.8" - three parts count up from the last release, so the release
    they are heading for is 1.5. Reading the two-part case as "the next
    one after this" would demand notes for 1.6 while shipping 1.5, and
    fail every release."""
    parts = version.split(".")
    if len(parts) <= 2:
        return version
    return f"{parts[0]}.{int(parts[1]) + 1}"


def notes() -> dict:
    """whats_new.NOTES, read out of the file rather than imported - the
    module pulls in helpers.storage, which would touch the data
    directory just to answer a question about a dict literal."""
    tree = ast.parse(WHATS_NEW_FILE.read_text(encoding="utf-8"))
    for node in tree.body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and getattr(node.targets[0], "id", "") == "NOTES"):
            return ast.literal_eval(node.value)
    raise SystemExit(f"Couldn't find NOTES in {WHATS_NEW_FILE}.")


def main(argv):
    if len(argv) > 2:
        raise SystemExit(f"Usage: {argv[0]} [version]")
    version = argv[1] if len(argv) == 2 else version_being_shipped(app_version())

    written = notes()
    if not written.get(version):
        raise SystemExit(
            f"\nRELEASE BLOCKED - {version} has no what's new notes.\n\n"
            f"  Add a \"{version}\" entry to NOTES in {WHATS_NEW_FILE.name}, in the\n"
            f"  same user-facing voice as the entries already there, then tag.\n"
            f"  Without them everyone updating into {version} gets an empty dialog\n"
            f"  and the version recorded as seen - there is no second chance.\n")
    print(f"{version}: {len(written[version])} what's new note(s) written.")


if __name__ == "__main__":
    main(sys.argv)
