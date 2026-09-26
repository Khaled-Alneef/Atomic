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
APP_VERSION = "2.9.1"

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
# **The folder build, packed inside Atomic.zip** (21 September 2026 -
# Atomic.spec's COLLECT note has the startup measurement that made the app
# a folder). Atomic.zip holds the bridge installer as its only .exe - which
# is all a single-file install can take out of it - and this, the folder
# (`Atomic/Atomic.exe` + `Atomic/_internal/...`) zipped, which a folder
# build unpacks for itself. One asset name, at the owner's ask ("the zip
# file name is Atomic not Atomic-app"); packaging/build.py writes it.
APP_PAYLOAD_NAME = "app.zip"
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


def is_folder_build() -> bool:
    """A frozen build whose files sit in `_internal` beside the exe."""
    return is_frozen() and (current_exe().parent / "_internal").is_dir()


def _asset_names():
    return (ZIP_NAME, EXE_NAME)


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
        for name in _asset_names():
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

    if is_folder_build() and (update.get("asset") or "").lower().endswith(".zip"):
        payload = _payload_from_zip(data)
        if payload is not None:
            return _stage_folder(payload)
        # No app.zip inside (a release from before the folder build):
        # the exe below is swapped in as a single-file build would.

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


def _payload_from_zip(data: bytes):
    """The folder build (APP_PAYLOAD_NAME) out of a verified release zip,
    or None when the zip carries none."""
    import io as _io
    import zipfile
    try:
        with zipfile.ZipFile(_io.BytesIO(data)) as archive:
            if APP_PAYLOAD_NAME in archive.namelist():
                return archive.read(APP_PAYLOAD_NAME)
    except Exception as exc:
        raise UpdateError(f"The download could not be unpacked: {exc}") from exc
    return None


def _stage_folder(data: bytes) -> Path:
    """Unpack the folder build (app.zip) beside the install folder and
    return the unpacked `Atomic` folder.

    Beside it, not in %TEMP%: the swap is a rename of one folder into the
    other's place, and a rename across volumes is a copy of 1,600 files
    made while the old app is exiting. Every entry must sit under
    `Atomic/` and the folder must hold `Atomic.exe` and `_internal` - a
    zip shaped otherwise is a packaging mistake, and is refused rather
    than installed."""
    import io as _io
    import shutil
    import zipfile
    parent = current_exe().parent.parent
    staging = parent / f"Atomic.new-{os.getpid()}"
    try:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)
        root = staging.resolve()
        with zipfile.ZipFile(_io.BytesIO(data)) as archive:
            names = archive.namelist()
            if not names or any(not n.startswith("Atomic/") for n in names):
                raise UpdateError("The download is not an Atomic folder build - "
                                  "it has not been installed.")
            for name in names:
                # No entry may land outside the staging folder.
                if not (root / name).resolve().is_relative_to(root):
                    raise UpdateError("The download holds an unsafe path - "
                                      "it has not been installed.")
            archive.extractall(root)
        folder = root / "Atomic"
        if not (folder / EXE_NAME).is_file() or not (folder / "_internal").is_dir():
            raise UpdateError("The download is missing Atomic.exe or its "
                              "_internal folder - it has not been installed.")
        return folder
    except UpdateError:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    except Exception as exc:
        shutil.rmtree(staging, ignore_errors=True)
        raise UpdateError(f"The download could not be unpacked: {exc}") from exc


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
# from a throwaway script that outlives this process.
#
# **It steps the old exe aside instead of overwriting it, and it always
# relaunches something.** The first version of this was one line retried
# for a minute - `move /y new old` - and then a silent exit. The owner,
# 19 September 2026, updating 2.5 to 2.6: "it finishes then close the app
# then never re-open!!! and when I open the app manually I found it did
# not update!!!!". Reproduced by driving the real 2.5 exe through its own
# Settings > Install v2.6 in a sandbox folder, watching every process:
#
#    8.9s  download verified, windows closed, this script started
#   11.6s  both Atomic processes gone
#   69.5s  the script gave up after 60 refusals and deleted itself -
#          58 of them with no Atomic process left alive to wait for
#
# So it was never waiting for the app to let go. After that quit Windows
# answers the overwrite with "Access is denied" (error 5) and goes on
# answering it for minutes - the same exe closed from its window, with no
# update in flight, was replaceable 6s later. In that refused state, on
# the same file, measured: renaming the old exe aside works, moving the
# new one into the freed name works, and deleting the renamed one works.
# His own %TEMP% held both verified 2.6 downloads from his two attempts,
# byte-identical to the release.
#
# **Who refuses it: McAfee. Confirmed the same night - he uninstalled it
# and the same 2.5 -> 2.6 update, from the same Settings button, worked at
# once.** 2.6 shipped with this paragraph saying the cause "was not
# found", and with Defender "ruled out" - on a PC where Defender is not
# the active antivirus at all. `Get-MpComputerStatus` reads
# RealTimeProtectionEnabled False there, because McAfee (preinstalled on
# his Lenovo, kernel driver mfesec.sys) has the job; every Defender check
# was aimed at a product that was not watching. The first pass also told
# him "no one can update from 2.5", which no measurement supported - they
# were all one PC, and one that had never attempted an in-app update (its
# log begins a minute after the exe arrived on the Desktop).
#
# What separated the suspects before the uninstall settled it, so none of
# it is re-dug: his own official **2.4**, driven through Settings >
# Install in the same sandbox, failed identically (processes gone at
# 11.6s, script gone at 69.3s, exe unchanged) - so not a 2.5 regression;
# in the refused state a 1KB dummy and his official 2.5 exe were refused
# exactly as the 2.6 build was - so not the incoming file; no process that
# could be examined held the exe (3,262 file handles walked, 142 protected
# processes not examinable, McAfee's among them); and a handle held on a
# *finished* process did not stop its exe being overwritten, which cleared
# the 96 dead-process handles a Nahimic audio service leaks there.
#
# **Pausing it is not enough, and that nearly cleared it wrongly.** With
# McAfee's Real-Time Scanning switched off - its own log has
# `set_rts_enabled ... state=disabled` ahead of the attempt, and its Real
# Protect tracer, which had logged the swap script's cmd.exe as "Child of
# TraceTarget" on the attempts before, logged nothing about this one - the
# update still failed. Which part of McAfee refuses the replace is
# therefore not known; only that it survives a pause and not an
# uninstall. So "I paused my antivirus and it still fails" does not clear
# the antivirus, and the first question for a report like his is what is
# installed: `Get-CimInstance -Namespace root/SecurityCenter2 -ClassName
# AntiVirusProduct`.
#
# On a PC without such a product the one-line script always worked, which
# is every update he remembers. The script below is for the PCs that have
# one - and it takes effect for updates made *from* 2.6 on, because the
# script that runs belongs to the version being replaced.
#
# The order below follows from that. Wait for this process and the
# bootloader above it to be gone (by id, capped - the rename would succeed
# while they are still running, and the new build must not start beside
# the old one); try the plain overwrite, which is still the whole job on
# a machine that allows it; otherwise step aside, move in, and put the
# old one back if the new one will not land. And whatever happened, start
# the exe that is there: a failed update must cost the update, never the
# app.
_SWAP_SCRIPT = r"""@echo off
set "TARGET=%~1"
set "SOURCE=%~2"
set "OLD=%~1.old"
set "IMAGE=%~nx1"
set "SYS=%SystemRoot%\System32"
set /a WAITED=0
:wait
set "ALIVE="
for %%P in (%~3 %~4) do (
    "%SYS%\tasklist.exe" /FI "PID eq %%P" /FI "IMAGENAME eq %IMAGE%" 2>nul | "%SYS%\find.exe" /i "%IMAGE%" >nul && set "ALIVE=1"
)
if not defined ALIVE goto swap
set /a WAITED+=1
if %WAITED% geq 30 goto swap
"%SYS%\PING.EXE" -n 2 127.0.0.1 >nul
goto wait
:swap
set /a TRIES=0
:retry
if not exist "%SOURCE%" goto launch
move /y "%SOURCE%" "%TARGET%" >nul 2>&1
if not errorlevel 1 goto launch
if exist "%OLD%" del /f /q "%OLD%" >nul 2>&1
move /y "%TARGET%" "%OLD%" >nul 2>&1
if errorlevel 1 goto again
move /y "%SOURCE%" "%TARGET%" >nul 2>&1
if not errorlevel 1 goto launch
move /y "%OLD%" "%TARGET%" >nul 2>&1
:again
set /a TRIES+=1
if %TRIES% geq 20 goto launch
"%SYS%\PING.EXE" -n 2 127.0.0.1 >nul
goto retry
:launch
if not exist "%TARGET%" if exist "%OLD%" move /y "%OLD%" "%TARGET%" >nul 2>&1
start "" "%TARGET%"
if exist "%OLD%" del /f /q "%OLD%" >nul 2>&1
(goto) 2>nul & del "%~f0"
"""

# **The folder build's swap: the same order, one folder instead of one
# file.** Wait for this process to be gone (a folder cannot be renamed
# while any file in it is open - the exe and its DLLs are); step the
# install folder aside as `<folder>.old`; move the unpacked one into its
# name; put the old one back if that is refused, retrying the way the
# file swap does (McAfee's refusal, above, is why there is a retry at
# all); and whatever happened, start the Atomic.exe that is there. The
# script's own working folder is %TEMP%, never one being moved.
#   %1 install folder   %2 unpacked Atomic folder   %3 its staging folder
#   %4 %5 process ids to wait for
_FOLDER_SWAP_SCRIPT = r"""@echo off
cd /d "%TEMP%"
set "TARGET=%~1"
set "SOURCE=%~2"
set "STAGE=%~3"
set "OLD=%~1.old"
set "SYS=%SystemRoot%\System32"
set /a WAITED=0
:wait
set "ALIVE="
for %%P in (%~4 %~5) do (
    "%SYS%\tasklist.exe" /FI "PID eq %%P" /FI "IMAGENAME eq Atomic.exe" 2>nul | "%SYS%\find.exe" /i "Atomic.exe" >nul && set "ALIVE=1"
)
if not defined ALIVE goto swap
set /a WAITED+=1
if %WAITED% geq 30 goto swap
"%SYS%\PING.EXE" -n 2 127.0.0.1 >nul
goto wait
:swap
set /a TRIES=0
:retry
if not exist "%SOURCE%\Atomic.exe" goto launch
if exist "%OLD%" rd /s /q "%OLD%" >nul 2>&1
move "%TARGET%" "%OLD%" >nul 2>&1
if errorlevel 1 goto again
move "%SOURCE%" "%TARGET%" >nul 2>&1
if not errorlevel 1 goto launch
move "%OLD%" "%TARGET%" >nul 2>&1
:again
set /a TRIES+=1
if %TRIES% geq 20 goto launch
"%SYS%\PING.EXE" -n 2 127.0.0.1 >nul
goto retry
:launch
if not exist "%TARGET%\Atomic.exe" if exist "%OLD%\Atomic.exe" move "%OLD%" "%TARGET%" >nul 2>&1
start "" "%TARGET%\Atomic.exe"
if exist "%OLD%" rd /s /q "%OLD%" >nul 2>&1
if exist "%STAGE%" rd /s /q "%STAGE%" >nul 2>&1
(goto) 2>nul & del "%~f0"
"""

# What a swap can leave behind, and how old it has to be before a launch
# clears it - old enough that it cannot belong to an update in flight.
_LEFTOVER_PATTERNS = ("Atomic-update-*.exe", "atomic-update-*.bat")
_LEFTOVER_AGE_S = 15 * 60


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
    if Path(downloaded).is_dir():
        # A folder build's update (_stage_folder): the whole install
        # folder is swapped, not the exe inside it.
        script.write_text(_FOLDER_SWAP_SCRIPT, encoding="utf-8")
        _allow_foreground_for_relaunch()
        subprocess.Popen(
            ["cmd", "/c", str(script), str(target.parent), str(downloaded),
             str(Path(downloaded).parent), str(os.getpid()), str(os.getppid())],
            creationflags=child_process.flags(detached=True), close_fds=True,
            env=child_process.clean_env(),
        )
        return
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
    #
    # The two ids are this process and the one above it: a onefile build
    # is a bootloader running the app as its child, both from the same
    # exe, and the script waits for both. It checks the image name as well
    # as the id, so a parent that is not Atomic (Explorer, for a build that
    # is not onefile) is never waited on.
    subprocess.Popen(
        ["cmd", "/c", str(script), str(target), str(downloaded),
         str(os.getpid()), str(os.getppid())],
        creationflags=child_process.flags(detached=True), close_fds=True,
        env=child_process.clean_env(),
    )


def tidy_leftovers() -> int:
    """Clear what earlier swaps left behind; returns how many files went.

    A swap that failed left its verified download in %TEMP% for ever - the
    owner's two attempts at 2.6 left 252MB there - and one that stepped the
    old exe aside can leave `Atomic.exe.old` beside the new one when
    Windows would not delete it yet. Called once after startup, off the UI
    thread; never raises, and never touches anything young enough to
    belong to an update that is still running."""
    if not is_frozen():
        return 0
    import glob
    import time
    removed = 0
    candidates = [str(current_exe()) + ".old"]
    for pattern in _LEFTOVER_PATTERNS:
        candidates += glob.glob(os.path.join(tempfile.gettempdir(), pattern))
    for path in candidates:
        try:
            if time.time() - os.path.getmtime(path) < _LEFTOVER_AGE_S \
                    and not path.endswith(".old"):
                continue
            os.remove(path)
            removed += 1
        except OSError:
            pass            # not there, or still held - the next launch tries again
    if is_folder_build():
        removed += _tidy_folders(time)
    return removed


def _tidy_folders(time) -> int:
    """A folder build's leftovers: `<install>.old` and `Atomic.new-*`
    beside the install folder from a swap, and the single-file builds'
    `_MEI*` unpack folders in %TEMP%.

    **The _MEI folders are the reason this exists.** A single-file build
    unpacked 290MB there on every launch and removed it on a clean exit
    only; the owner's %TEMP% held 18 of them (~5GB) on 21 September 2026,
    left by launches that were killed or crashed. Only a folder carrying
    Atomic's own files (libmpv-2.dll and static/app.js) is touched - other
    PyInstaller programs use the same prefix - and only one that can be
    renamed: a folder a running single-file Atomic still uses has its DLLs
    open, and Windows refuses the rename, so it is never deleted from
    under that app."""
    import glob
    import shutil
    removed = 0
    beside = current_exe().parent
    folders = [str(beside) + ".old"]
    folders += glob.glob(os.path.join(str(beside.parent), "Atomic.new-*"))
    for path in folders:
        try:
            if os.path.isdir(path) and time.time() - os.path.getmtime(path) >= _LEFTOVER_AGE_S:
                shutil.rmtree(path)
                removed += 1
        except OSError:
            pass
    for path in glob.glob(os.path.join(tempfile.gettempdir(), "_MEI*")):
        try:
            if not (os.path.isfile(os.path.join(path, "libmpv-2.dll"))
                    and os.path.isfile(os.path.join(path, "static", "app.js"))):
                continue
            if time.time() - os.path.getmtime(path) < _LEFTOVER_AGE_S:
                continue
            doomed = path + ".atomic-tidy"
            os.rename(path, doomed)          # refused while anything in it is open
            shutil.rmtree(doomed, ignore_errors=True)
            removed += 1
        except OSError:
            pass
    return removed


def _allow_foreground_for_relaunch():
    if sys.platform != "win32":
        return
    try:
        ASFW_ANY = -1
        ctypes.windll.user32.AllowSetForegroundWindow(ASFW_ANY)
    except Exception:
        pass
