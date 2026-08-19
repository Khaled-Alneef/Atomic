"""Fetch libmpv-2.dll into vendor/ - the video player's decode engine.

Not committed, deliberately. The DLL is ~50MB unpacked and would be
permanent repository weight on every clone, the same argument that keeps
Atomic.exe out of `development` (.gitignore). It is an artifact:
rebuildable at any time by running this script, and the build (and the
app itself) both fail loudly with a pointer here when it is missing,
rather than silently producing a player that cannot decode anything.

Source is shinchiro/mpv-winbuild-cmake, which is the Windows build mpv.io
itself links to. Only libmpv-2.dll is kept out of the archive; the rest
of the dev package is headers and an import library that nothing here
needs, since python-mpv loads the DLL by name at runtime.
"""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import urllib.request

REPO = "shinchiro/mpv-winbuild-cmake"
VENDOR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "vendor")
DLL = os.path.join(VENDOR, "libmpv-2.dll")
# bsdtar, not GNU tar: only libarchive reads 7-Zip, and Windows ships it
# at this path. GNU tar (which is what `tar` resolves to under Git Bash)
# fails on a .7z with an unhelpful "unrecognized archive format".
BSDTAR = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "tar.exe")


def _asset_url():
    request = urllib.request.Request(
        f"https://api.github.com/repos/{REPO}/releases/latest",
        headers={"User-Agent": "Atomic"})
    with urllib.request.urlopen(request, timeout=30) as response:
        release = json.load(response)
    for asset in release["assets"]:
        name = asset["name"]
        # "-v3" is the AVX-512 variant; it crashes on CPUs without it, so
        # the plain x86_64 build is the one that runs everywhere.
        if name.startswith("mpv-dev-x86_64-") and "-v3-" not in name:
            return asset["browser_download_url"], name
    raise SystemExit("no mpv-dev-x86_64 asset in the latest release")


def main():
    if os.path.exists(DLL) and "--force" not in sys.argv:
        print(f"already present: {DLL}")
        return
    url, name = _asset_url()
    os.makedirs(VENDOR, exist_ok=True)
    with tempfile.TemporaryDirectory() as work:
        archive = os.path.join(work, name)
        print(f"downloading {name} ...")
        urllib.request.urlretrieve(url, archive)
        subprocess.run([BSDTAR, "-xf", archive, "-C", work, "libmpv-2.dll"],
                       check=True)
        os.replace(os.path.join(work, "libmpv-2.dll"), DLL)
    digest = hashlib.sha256(open(DLL, "rb").read()).hexdigest()
    print(f"{DLL}\n  {os.path.getsize(DLL) / 1e6:.1f} MB\n  sha256 {digest}")


if __name__ == "__main__":
    main()
