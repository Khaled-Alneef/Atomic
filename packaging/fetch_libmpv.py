"""Fetch the pinned libmpv-2.dll used by Atomic's Windows player.

Atomic used to download the newest shinchiro/mpv-winbuild-cmake snapshot on
every machine. That made two builds from the same Atomic commit capable of
shipping different video engines, and the current test machine ended up on
mpv 0.41.0-923 while Stremio deliberately pins mpv 0.41.0.

For player-stutter work reproducibility matters more than chasing nightly mpv
commits. Pin the exact x64 libmpv archive committed by Stremio in the commit
whose message is "Bump libmpv to mpv 0.41.0". The commit-addressed raw URL is
immutable. A small local marker records the extracted DLL hash so normal builds
can prove the vendor copy is the pinned engine and automatically replace an
older/nightly DLL.

The binary itself is still not committed to Atomic; it remains a build artifact
under vendor/ and is bundled into Atomic.exe by packaging/Atomic.spec.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import urllib.request
import zipfile

STREMIO_COMMIT = "d188ec337cf090f417cacd58e2952a5074db81c8"
STREMIO_ARCHIVE = "libmpv-2_x64.zip"
PINNED_URL = (
    "https://raw.githubusercontent.com/Stremio/stremio-shell-ng/"
    f"{STREMIO_COMMIT}/{STREMIO_ARCHIVE}"
)

VENDOR = os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "vendor")
)
DLL = os.path.join(VENDOR, "libmpv-2.dll")
MARKER = os.path.join(VENDOR, "libmpv-source.json")


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _marker_matches() -> bool:
    if not os.path.isfile(DLL) or not os.path.isfile(MARKER):
        return False
    try:
        with open(MARKER, encoding="utf-8") as stream:
            marker = json.load(stream)
        return (
            marker.get("source_commit") == STREMIO_COMMIT
            and marker.get("dll_sha256") == _sha256(DLL)
        )
    except (OSError, ValueError, TypeError):
        return False


def _extract_dll(archive_path: str, destination: str) -> None:
    with zipfile.ZipFile(archive_path) as archive:
        candidates = [
            name for name in archive.namelist()
            if os.path.basename(name).lower() == "libmpv-2.dll"
        ]
        if not candidates:
            raise SystemExit(
                f"{STREMIO_ARCHIVE} did not contain libmpv-2.dll"
            )
        # Stremio currently stores one copy. Keep the extraction tolerant of a
        # future containing-folder change while rejecting ambiguous archives.
        if len(candidates) != 1:
            raise SystemExit(
                f"{STREMIO_ARCHIVE} contained {len(candidates)} libmpv-2.dll files"
            )
        with archive.open(candidates[0]) as source, open(destination, "wb") as target:
            while True:
                chunk = source.read(1024 * 1024)
                if not chunk:
                    break
                target.write(chunk)


def ensure_pinned(force: bool = False) -> str:
    """Ensure vendor/libmpv-2.dll is Stremio's pinned mpv 0.41.0 build.

    Returns the DLL path. Existing verified copies are left untouched so normal
    offline rebuilds keep working once the pinned engine has been fetched once.
    """
    if not force and _marker_matches():
        print(f"Pinned libmpv already present: {DLL}")
        return DLL

    os.makedirs(VENDOR, exist_ok=True)
    with tempfile.TemporaryDirectory() as work:
        archive_path = os.path.join(work, STREMIO_ARCHIVE)
        dll_path = os.path.join(work, "libmpv-2.dll")
        request = urllib.request.Request(
            PINNED_URL,
            headers={"User-Agent": "Atomic-libmpv-fetcher"},
        )
        print("Downloading Stremio-pinned mpv 0.41.0 libmpv ...")
        with urllib.request.urlopen(request, timeout=60) as response, open(
            archive_path, "wb"
        ) as target:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                target.write(chunk)

        _extract_dll(archive_path, dll_path)
        digest = _sha256(dll_path)

        # os.replace keeps a half-written DLL from ever becoming the active
        # player engine if a download/extraction fails midway through.
        os.replace(dll_path, DLL)
        marker = {
            "source": "Stremio/stremio-shell-ng",
            "source_commit": STREMIO_COMMIT,
            "archive": STREMIO_ARCHIVE,
            "dll_sha256": digest,
        }
        marker_tmp = MARKER + ".tmp"
        with open(marker_tmp, "w", encoding="utf-8") as stream:
            json.dump(marker, stream, indent=2, sort_keys=True)
            stream.write("\n")
        os.replace(marker_tmp, MARKER)

    print(
        f"{DLL}\n"
        f"  {os.path.getsize(DLL) / 1e6:.1f} MB\n"
        f"  mpv 0.41.0 source commit {STREMIO_COMMIT}\n"
        f"  sha256 {_sha256(DLL)}"
    )
    return DLL


def main() -> None:
    ensure_pinned(force="--force" in sys.argv[1:])


if __name__ == "__main__":
    main()
