"""Build the bridge installer - the Atomic.exe committed at a release tag.

    py -3.13 packaging/bridge/build_bridge.py

Produces packaging/bridge/dist/Atomic.exe. See atomic_setup.py for why
this exists; the short version is that the real build is 126MB, GitHub
refuses any file over 100MiB on push, and every install from 1.0 to 1.10
updates itself by downloading whatever is committed at the tag under
that name.

Two things this checks that a build log will not:

  * **the size**, against the limit that is the whole reason for it. A
    bridge that grew past 100MiB could not be committed either, and one
    that grew past a few tens of MB has picked up something it does not
    need (the spec's `excludes` is the guard, and this is what notices
    when the guard slips).
  * **the payload it will fetch** is not this file. A build that somehow
    produced the app instead of the bridge would be committed as the
    bridge and downloaded by everyone.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SPEC = HERE / "AtomicSetup.spec"
DIST = HERE / "dist"
WORK = HERE / "build"
OUTPUT = DIST / "Atomic.exe"

# Comfortably under GitHub's 100MiB refusal and roughly ten times what
# tkinter + the standard library actually needs, so this only fires when
# something large has been pulled in by accident.
SIZE_CAP_MB = 40


def main() -> int:
    if sys.version_info[:2] != (3, 13):
        print("Build the bridge with Python 3.13 - the app's own build "
              "re-execs into it for libtorrent, and shipping two "
              "interpreters' idea of the standard library helps nobody.")
    for path in (DIST, WORK):
        shutil.rmtree(path, ignore_errors=True)

    result = subprocess.run(
        [sys.executable, "-m", "PyInstaller", "--clean", "--noconfirm",
         "--distpath", str(DIST), "--workpath", str(WORK), str(SPEC)],
        cwd=str(HERE),
    )
    if result.returncode != 0:
        return result.returncode
    if not OUTPUT.exists():
        print(f"The build reported success but {OUTPUT} is not there.")
        return 1

    size_mb = OUTPUT.stat().st_size / (1024 * 1024)
    print(f"\n{OUTPUT}  {size_mb:.1f} MB")
    if size_mb > SIZE_CAP_MB:
        print(f"Refusing this build: {size_mb:.1f} MB is over the "
              f"{SIZE_CAP_MB} MB cap. Check the spec's excludes.")
        return 1

    # Two markers, and they are checked in the encodings they are
    # actually written in: the script name lands in the archive's table
    # of contents as plain bytes, while the version resource is UTF-16.
    # ("AtomicSetup" as ascii matches nothing - it lives inside the
    # compressed PYZ, which is why the first version of this check
    # rejected a perfectly good build.)
    blob = OUTPUT.read_bytes()
    if b"atomic_setup" not in blob:
        print("Refusing this build: it does not hold atomic_setup - "
              "that is not the bridge installer.")
        return 1
    if "AtomicSetup".encode("utf-16-le") not in blob:
        print("Refusing this build: the version resource is missing, so "
              "Windows would show it as an unnamed binary.")
        return 1
    # **Run the thing.** The markers above prove what went in; only
    # running it proves it starts. The first release of this installer
    # passed every check above and died on its first line, because the
    # spec excluded `email` and `urllib.request` imports it at module
    # scope - on the owner's machine, mid-update, with this already in
    # place as his Atomic.exe. `--selftest` resolves the real release,
    # reads four bytes of the asset and builds the window; it reports to
    # a file because a windowed build has no stdout to print to.
    report = DIST / "selftest.txt"
    if report.exists():
        report.unlink()
    result = subprocess.run([str(OUTPUT), "--selftest", str(report)],
                            timeout=180)
    text = report.read_text(encoding="utf-8") if report.exists() else ""
    print()
    print("--- selftest ---")
    print(text or "(the build wrote no report at all)")
    if result.returncode != 0 or "SELFTEST OK" not in text:
        print()
        print(f"Refusing this build: it does not run (exit {result.returncode}).")
        return 1

    print()
    print("Built, and it runs. Commit this as Atomic.exe at the release tag.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
