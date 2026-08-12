"""Run this to build Atomic.exe: `python build.py`.

Wraps `pyinstaller Atomic.spec` (which bundles app_icon.ico from this
same directory into the build - the plain `pyinstaller main.py` form
skips that and the taskbar/title-bar icon comes up blank at runtime).
Installs PyInstaller first if it isn't already available.
"""

import subprocess
import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
SPEC_FILE = APP_DIR / "Atomic.spec"
ICON_FILE = APP_DIR / "app_icon.ico"


def _ensure_pyinstaller():
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("PyInstaller not found - installing...")
        subprocess.run([sys.executable, "-m", "pip", "install", "pyinstaller>=6.0"], check=True)


def main():
    if not ICON_FILE.exists():
        sys.exit(f"Missing {ICON_FILE.name} in {APP_DIR} - put the icon there before building.")
    if not SPEC_FILE.exists():
        sys.exit(f"Missing {SPEC_FILE.name} in {APP_DIR}.")

    _ensure_pyinstaller()

    print(f"Building from {SPEC_FILE.name}...")
    result = subprocess.run(
        [sys.executable, "-m", "PyInstaller", str(SPEC_FILE), "--noconfirm"],
        cwd=APP_DIR,
    )
    if result.returncode != 0:
        sys.exit(result.returncode)

    exe_path = APP_DIR / "dist" / "Atomic.exe"
    print(f"\nDone: {exe_path}" if exe_path.exists() else "\nBuild finished, but dist/Atomic.exe wasn't found - check the log above.")


if __name__ == "__main__":
    main()
