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

What gets downloaded is the release's **Atomic.zip asset**, and the
file committed at the tag only as a fallback. That split is a
measurement, not a preference: the 2.0 build is 126MB and GitHub
refuses any file over 100MiB on push, so the artifact cannot live in
the repository at all any more - while a release asset may be 2GB.
Assets carry a `digest` ("sha256:...") and repository files carry a git
blob hash; either one is checked before anything is replaced, so a
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
APP_VERSION = "2.2.1"

# What counts as a release: exactly two numeric parts, with or without the
# leading v. Development builds are tagged (if at all) with three, and are
# ignored here - GitHub's tag list is per repository, not per branch, so
# this is what keeps work on `development` from reaching anyone running
# the app, however it gets tagged.
RELEASE_TAG_RE = re.compile(r"^v?\d+\.\d+$")

REPO = "Khaled-Alneef/Atomic"
EXE_NAME = "Atomic.exe"
# **A release ships as a zip, and the updater looks for that first.**
#
# The owner's rule of 25 August 2026 (CLAUDE.md rule 8), and it is a
# measurement: the bare exe was refused on download as
# `Trojan:Win32/Wacatac.B!ml` - Microsoft's ML classifier, no signature
# match - while the identical bytes inside a zip downloaded cleanly.
# Seven builds were compared before concluding that (same bundled
# entries, same modules, byte-identical bootloader, all scanning clean
# locally with cloud protection on), so it is the container that is
# refused, not anything in the file.
#
# EXE_NAME stays as the fallback and must: every release already
# published carries the bare exe, and an install updating from one of
# those has to keep working.
ZIP_NAME = "Atomic.zip"
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
    with net.urlopen(request, timeout=timeout) as response:
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
    {"version", "tag", "url", "size", "sha"/"sha256", "asset"}. None means
    already current.

    Two places are asked, newest wins, and the release asset wins a tie:

      * `/releases` - a published release's attached Atomic.zip. This is
        where every release from 2.0 on lives, because the build passed
        GitHub's 100MiB file limit (126MB measured, 8 September 2026) and
        can no longer be committed to the repository at all.
      * `/tags` + `/contents` - the artifact committed at the tag, which
        is how 1.0 to 1.10 shipped and how the bridge installer at v2.0
        reaches installs whose updater knows only this route.

    Raises UpdateError with something worth reading on failure."""
    found, failure = [], None
    for source in (_from_releases, _from_repository):
        try:
            candidate = source(timeout)
        except UpdateError:
            raise
        except Exception as exc:
            failure = failure or exc
            continue
        if candidate:
            found.append(candidate)

    if not found:
        if failure is not None:
            raise UpdateError(_readable_network_error(failure))
        raise UpdateError("No releases have been published yet.")

    # Sorted on the version *and* on whether it is an asset, so a 2.0
    # published both ways (the tag carries the bridge installer for old
    # updaters; the release carries the real zip) hands this build the
    # zip rather than the bridge it does not need.
    newest = max(found, key=lambda item: (parse_version(item["tag"]),
                                          item.get("from_release", False)))
    if parse_version(newest["tag"]) <= parse_version(APP_VERSION):
        return None
    return newest


def _from_releases(timeout: int):
    """The newest published release carrying a downloadable asset.

    Drafts and pre-releases are skipped: test builds are published as
    pre-releases, and one must never be offered to somebody running the
    app. RELEASE_TAG_RE is applied to the tag for the same reason it is
    applied to the tag list - two numeric parts is what a release is."""
    releases = _get_json(f"{API_ROOT}/releases?per_page=100", timeout)
    best = None
    for release in releases or []:
        if release.get("draft") or release.get("prerelease"):
            continue
        tag_name = (release.get("tag_name") or "").strip()
        if not RELEASE_TAG_RE.match(tag_name):
            continue
        by_name = {(a.get("name") or ""): a for a in release.get("assets") or []}
        for name in (ZIP_NAME, EXE_NAME):
            asset = by_name.get(name)
            if not asset or not asset.get("browser_download_url"):
                continue
            candidate = {
                "version": tag_name.lstrip("vV"),
                "tag": tag_name,
                "url": asset["browser_download_url"],
                "size": asset.get("size") or 0,
                # GitHub reports an asset's digest as "sha256:<hex>".
                # There is no git blob hash for an asset - it is not in
                # the repository - so this is what the download is
                # checked against.
                "sha256": (asset.get("digest") or "").split(":")[-1],
                "asset": name,
                "from_release": True,
            }
            if best is None or parse_version(tag_name) > parse_version(best["tag"]):
                best = candidate
            break
    return best


def _from_repository(timeout: int):
    """The newest release tag whose artifact is committed in the repo.

    This is the whole of what 1.0-1.10 could see, and it is kept because
    it is the only route an old install has - v2.0's tag carries the
    bridge installer as Atomic.exe precisely for them."""
    tags = _get_json(f"{API_ROOT}/tags", timeout)
    names = [(tag.get("name") or "").strip() for tag in tags or []]
    names = [name for name in names if RELEASE_TAG_RE.match(name)]
    if not names:
        return None
    tag_name = max(names, key=parse_version)
    if parse_version(tag_name) <= parse_version(APP_VERSION):
        # Nothing to fetch, and asking for two files that would be
        # discarded costs the user two round trips on every check.
        return None

    # The zip first, the bare exe second - see ZIP_NAME.
    for name in (ZIP_NAME, EXE_NAME):
        try:
            meta = _get_json(f"{API_ROOT}/contents/{name}?ref={tag_name}",
                             timeout)
        except Exception:
            continue
        if meta.get("download_url"):
            return {
                "version": tag_name.lstrip("vV"),
                "tag": tag_name,
                "url": meta.get("download_url"),
                "size": meta.get("size") or 0,
                "sha": meta.get("sha") or "",
                "asset": name,
                "from_release": False,
            }
    return None


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
        with net.urlopen(request, timeout=timeout) as response:
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
    # Whichever hash the source of this update carries: a repository
    # file reports git's own blob hash, a release asset reports a plain
    # sha256 ("digest"). Neither is optional when it is there, and a
    # download that arrives with neither is still size-checked above.
    if update.get("sha") and _git_blob_sha(data) != update["sha"]:
        raise UpdateError(
            "The downloaded file didn't match the checksum GitHub reported, "
            "so it hasn't been installed.")
    expected_sha256 = (update.get("sha256") or "").strip().lower()
    if expected_sha256 and hashlib.sha256(data).hexdigest() != expected_sha256:
        raise UpdateError(
            "The downloaded file didn't match the checksum GitHub reported, "
            "so it hasn't been installed.")

    if (update.get("asset") or "").lower().endswith(".zip"):
        # **Unpacked here, not by the swap script.** The verification
        # above is of the bytes GitHub served, so it has to happen on
        # the zip; what apply_update hands to the swap has to be the
        # executable. Anything that is not one .exe inside is refused
        # rather than guessed at - a release whose zip holds something
        # else is a packaging mistake, and installing it would be worse
        # than saying so.
        data = _exe_from_zip(data)

    handle, temp_path = tempfile.mkstemp(prefix="Atomic-update-", suffix=".exe")
    try:
        with os.fdopen(handle, "wb") as file:
            file.write(data)
    except OSError as exc:
        raise UpdateError(f"Couldn't write the download to disk: {exc}") from exc
    return Path(temp_path)


def _exe_from_zip(data: bytes) -> bytes:
    """The one executable inside a downloaded release zip."""
    import io as _io
    import zipfile
    try:
        with zipfile.ZipFile(_io.BytesIO(data)) as archive:
            names = [n for n in archive.namelist()
                     if n.lower().endswith(".exe") and not n.endswith("/")]
            if len(names) != 1:
                raise UpdateError(
                    f"The download should hold exactly one .exe and holds "
                    f"{len(names)} - it has not been installed.")
            return archive.read(names[0])
    except UpdateError:
        raise
    except Exception as exc:
        raise UpdateError(
            f"The download could not be unpacked: {exc}") from exc


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
