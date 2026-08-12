"""Run this to build Atomic.exe: `python build.py`.

Wraps `pyinstaller Atomic.spec` (which bundles src/app_icon.ico into the
build - the plain `pyinstaller src/main.py` form skips that and the
taskbar/title-bar icon comes up blank at runtime). Installs PyInstaller
first if it isn't already available. PyInstaller's own work/dist folders
are kept inside this packaging/ directory; the finished exe is then
copied to the project root so Atomic.exe stays the one loose file there.
"""

import shutil
import subprocess
import sys
from pathlib import Path

PACKAGING_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGING_DIR.parent
SRC_DIR = PROJECT_ROOT / "src"
SPEC_FILE = PACKAGING_DIR / "Atomic.spec"
ICON_FILE = SRC_DIR / "app_icon.ico"
DIST_DIR = PACKAGING_DIR / "dist"
WORK_DIR = PACKAGING_DIR / "build"


def _ensure_pyinstaller():
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("PyInstaller not found - installing...")
        subprocess.run([sys.executable, "-m", "pip", "install", "pyinstaller>=6.0"], check=True)


def main():
    if not ICON_FILE.exists():
        sys.exit(f"Missing {ICON_FILE.name} in {SRC_DIR} - put the icon there before building.")
    if not SPEC_FILE.exists():
        sys.exit(f"Missing {SPEC_FILE.name} in {PACKAGING_DIR}.")

    _ensure_pyinstaller()

    print(f"Building from {SPEC_FILE.name}...")
    result = subprocess.run(
        [
            sys.executable, "-m", "PyInstaller", str(SPEC_FILE),
            "--noconfirm",
            "--distpath", str(DIST_DIR),
            "--workpath", str(WORK_DIR),
        ],
        cwd=PACKAGING_DIR,
    )
    if result.returncode != 0:
        sys.exit(result.returncode)

    built_exe = DIST_DIR / "Atomic.exe"
    if not built_exe.exists():
        sys.exit(f"\nBuild finished, but {built_exe} wasn't found - check the log above.")

    final_exe = PROJECT_ROOT / "Atomic.exe"
    shutil.copy2(built_exe, final_exe)
    print(f"\nDone: {final_exe}")


if __name__ == "__main__":
    main()
