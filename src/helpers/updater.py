"""In-app updates, pulled from this project's own GitHub repository.

Atomic ships as a single executable with no installer, so without this
every update means finding the repo, downloading Atomic.exe by hand and
replacing the old one. This does the same thing from Settings.

How a version is identified. A version has two parts when it is a release
and three while it is being worked on:

    1.0      released, on `main`, tagged v1.0
    1.0.1    development build after it, on `development`
    1.0.2    the next one
    1.1      what those are released as, on `main`, tagged v1.1

Versions are compared as numbers, part by part (see parse_version), so a
development build always sorts *below* the release it is heading for -
1.0.2 < 1.1 - and correctly sees that release as newer when it lands.
That is why development builds count up from the last release rather than
carrying the number they are becoming: 1.1.2 would sort *above* 1.1 and a
build numbered that way would never accept its own release. The usual
"1.1.0-dev.2" spelling has exactly that problem here too, since only the
digits are read.

Checking asks GitHub for the tag list, keeps the ones that name a release
(RELEASE_TAG_RE - the repository's tag list covers every branch, so a
tagged development build would otherwise be offered to everyone), takes
the highest, and offers it when it is newer than APP_VERSION.

What gets downloaded is the Atomic.exe committed at that tag. GitHub's
contents API hands back the file's git blob hash alongside it, and the
download is checked against that before anything is replaced - a
truncated or tampered download is discarded rather than installed.

Everything here fails soft and reports why: no network, GitHub rate
limiting an unauthenticated caller, a release without an exe committed,
or a download that doesn't verify all end in a message rather than a
broken install.
"""

import ctypes
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

from . import child_process, net

# What this build is. Three parts while the work is in progress on
# `development`, counting up from the last release; two parts on a build
# that is being released, bumped in the same commit that tags it - or the
# new build goes on offering itself an update.
APP_VERSION = "1.4.13"

# What counts as a release: exactly two numeric parts, with or without the
# leading v. Development builds are tagged (if at all) with three, and are
# ignored here - GitHub's tag list is per repository, not per branch, so
# this is what keeps work on `development` from reaching anyone running
# the app, however it gets tagged.
RELEASE_TAG_RE = re.compile(r"^v?\d+\.\d+$")

REPO = "Khaled-Alneef/Atomic"
EXE_NAME = "Atomic.exe"
API_ROOT = f"https://api.github.com/repos/{REPO}"

_HEADERS = {
    "Accept": "application/vnd.github+json",
    "User-Agent": f"Atomic/{APP_VERSION}",
}


class UpdateError(Exception):
    """Anything that stopped an update, with a message worth showing."""


def _get_json(url, timeout):
    request = urllib.request.Request(url, headers=_HEADERS)
    # Bounded like every other lookup (net.read_text): this one runs on
    # the update check the user is actually waiting on, so a host that
    # dribbles a body forever would hang the Settings dialog rather than
    # a background worker.
    deadline = net.deadline_in(timeout)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(net.read_text(response, deadline))


def parse_version(text):
    """"v1.10" -> (1, 10). Compared as numbers rather than as text, so
    1.10 correctly sorts above 1.9."""
    return tuple(int(part) for part in re.findall(r"\d+", text or "")) or (0,)


def is_frozen() -> bool:
    """Whether this is the packaged executable. Running from source there
    is nothing to replace - the repo is already the source of truth."""
    return bool(getattr(sys, "frozen", False))


def current_exe() -> Path:
    return Path(sys.executable).resolve()


def check_for_update(timeout: int = 10):
    """The newest released version if it is newer than this one, as
    {"version", "tag", "url", "size", "sha"}. None means already current.

    Raises UpdateError with something worth reading on failure."""
    try:
        tags = _get_json(f"{API_ROOT}/tags", timeout)
    except Exception as exc:
        raise UpdateError(_readable_network_error(exc)) from exc

    releases = [tag for tag in tags
                if RELEASE_TAG_RE.match((tag.get("name") or "").strip())]
    if not releases:
        raise UpdateError("No releases have been published yet.")

    newest = max(releases, key=lambda tag: parse_version(tag.get("name")))
    tag_name = newest.get("name") or ""
    if parse_version(tag_name) <= parse_version(APP_VERSION):
        return None

    try:
        meta = _get_json(f"{API_ROOT}/contents/{EXE_NAME}?ref={tag_name}", timeout)
    except Exception as exc:
        raise UpdateError(
            f"{tag_name} is available, but its {EXE_NAME} could not be read "
            f"from the repository ({_readable_network_error(exc)})") from exc

    url = meta.get("download_url")
    if not url:
        raise UpdateError(f"{tag_name} has no {EXE_NAME} committed to it.")

    return {
        "version": tag_name.lstrip("vV"),
        "tag": tag_name,
        "url": url,
        "size": meta.get("size") or 0,
        "sha": meta.get("sha") or "",
    }


def _readable_network_error(exc) -> str:
    text = str(exc)
    if "403" in text:
        return ("GitHub is rate-limiting anonymous requests from this "
                "network - try again in a little while")
    if "404" in text:
        return "the repository or file could not be found"
    return "couldn't reach GitHub - check your connection"


def _git_blob_sha(data: bytes) -> str:
    """Git's own hash for a file's contents, which is what the GitHub
    contents API reports - so the download can be checked against it
    without needing a separate checksum published anywhere."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def download_update(update: dict, progress=None, timeout: int = 60) -> Path:
    """Fetch the new executable to a temporary file and verify it.

    `progress` is called with (bytes_so_far, total) as it goes. Returns
    the downloaded path; raises UpdateError if it doesn't verify, and
    leaves nothing behind when it fails."""
    expected_size = update.get("size") or 0
    chunks = []
    received = 0
    try:
        request = urllib.request.Request(update["url"], headers=_HEADERS)
        with urllib.request.urlopen(request, timeout=timeout) as response:
            while True:
                chunk = response.read(256 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
                received += len(chunk)
                if progress:
                    progress(received, expected_size)
    except Exception as exc:
        raise UpdateError(f"Download failed - {_readable_network_error(exc)}") from exc

    data = b"".join(chunks)
    if expected_size and len(data) != expected_size:
        raise UpdateError(
            f"Download was incomplete ({len(data):,} of {expected_size:,} bytes).")
    if update.get("sha") and _git_blob_sha(data) != update["sha"]:
        raise UpdateError(
            "The downloaded file didn't match the checksum GitHub reported, "
            "so it hasn't been installed.")

    handle, temp_path = tempfile.mkstemp(prefix="Atomic-update-", suffix=".exe")
    try:
        with os.fdopen(handle, "wb") as file:
            file.write(data)
    except OSError as exc:
        raise UpdateError(f"Couldn't write the download to disk: {exc}") from exc
    return Path(temp_path)


# Windows won't let a running executable be replaced, so the swap happens
# from a throwaway script that outlives this process: it retries the move
# until the app has exited (up to a minute), relaunches the new build, and
# deletes itself. Waiting on the move succeeding rather than on a process
# id keeps it correct however the app exits.
_SWAP_SCRIPT = """@echo off
set "TARGET=%~1"
set "SOURCE=%~2"
set /a TRIES=0
:retry
move /y "%SOURCE%" "%TARGET%" >nul 2>&1
if not errorlevel 1 goto launch
set /a TRIES+=1
if %TRIES% geq 60 goto cleanup
ping -n 2 127.0.0.1 >nul
goto retry
:launch
start "" "%TARGET%"
:cleanup
(goto) 2>nul & del "%~f0"
"""


def apply_update(downloaded: Path):
    """Hand the swap to a detached script and return, so the caller can
    close the app. Raises UpdateError if the executable can't be replaced
    where it currently sits (a read-only or protected folder), which is
    worth knowing before quitting rather than after."""
    if not is_frozen():
        raise UpdateError(
            "This is running from source, so there is no executable to "
            "replace - use git to update instead.")

    target = current_exe()
    if not os.access(target.parent, os.W_OK):
        raise UpdateError(
            f"No permission to replace {target.name} in {target.parent}. "
            "Move Atomic somewhere writable, or update it by hand.")

    script = Path(tempfile.gettempdir()) / f"atomic-update-{os.getpid()}.bat"
    script.write_text(_SWAP_SCRIPT, encoding="utf-8")

    # Windows refuses to let a process that isn't in the foreground raise
    # a window, so the relaunched Atomic would open behind everything and
    # sit blinking in the taskbar. This hands that right over before
    # quitting - the documented way for an app to pass focus to something
    # it is starting.
    _allow_foreground_for_relaunch()

    # clean_env: the script goes on to launch the new Atomic, and
    # inheriting this build's PyInstaller unpack folder is precisely what
    # broke the relaunch. flags(): no console window for the swap script.
    # Both explained in helpers/child_process.
    subprocess.Popen(
        ["cmd", "/c", str(script), str(target), str(downloaded)],
        creationflags=child_process.flags(detached=True), close_fds=True,
        env=child_process.clean_env(),
    )


def _allow_foreground_for_relaunch():
    if sys.platform != "win32":
        return
    try:
        ASFW_ANY = -1
        ctypes.windll.user32.AllowSetForegroundWindow(ASFW_ANY)
    except Exception:
        pass
