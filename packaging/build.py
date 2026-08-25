"""Run this to build Atomic.exe: `python build.py`.

Wraps `pyinstaller Atomic.spec` (which bundles src/assets/app_icon.ico into the
build - the plain `pyinstaller src/main.py` form skips that and the
taskbar/title-bar icon comes up blank at runtime). Installs PyInstaller
first if it isn't already available. PyInstaller's own work/dist folders
are kept inside this packaging/ directory; the finished exe is then
copied to the project root so Atomic.exe stays the one loose file there.

Then it proves the exe belongs to the source tree it was built from,
because a build log that says "completed successfully" does not. 1.4 was
tagged with an executable built before its own last two commits: it was
missing src/assets/filter_icon.png outright (173 bundled entries where the tree
produces 174), and the release notes recorded the size and hash of a
build that was never the one committed. Nothing failed at the time -
PyInstaller re-copied a cached binary and reported success. Both checks
below exist for that one incident.
"""

import ast
import os
import shutil
import subprocess
import sys
from pathlib import Path

PACKAGING_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGING_DIR.parent
SRC_DIR = PROJECT_ROOT / "src"
SPEC_FILE = PACKAGING_DIR / "Atomic.spec"
# Everything the app ships as an image lives under src/assets/ now (the
# nav icons, SVG since 25 August 2026, in src/assets/icons/).
# Atomic.spec resolves the same paths.
ASSETS_DIR = SRC_DIR / "assets"
ICON_FILE = ASSETS_DIR / "app_icon.ico"
DIST_DIR = PACKAGING_DIR / "dist"
WORK_DIR = PACKAGING_DIR / "build"


# The interpreter the exe must be built with. Not a preference: the
# built-in torrent engine is libtorrent, which publishes wheels for
# CPython 3.9-3.13 only. This machine's default python is 3.15, a beta
# with no wheels for it at all, so a build run there produces an exe
# whose player cannot stream anything on its own.
BUILD_PYTHON_TAG = "3.13"
ENGINE_MODULES = ("libtorrent",)


def _reexec_on_build_python():
    """Re-run this script under the interpreter that has the engine.

    Done here rather than by telling the user to type a different
    command: `python packaging/build.py` is what the docs and habit say,
    and a build that silently omits the torrent engine is exactly the
    class of "succeeded but wrong" this file already exists to catch."""
    missing = [m for m in ENGINE_MODULES if not _importable(m)]
    if not missing:
        return
    if os.environ.get("ATOMIC_BUILD_REEXEC"):
        raise SystemExit(
            f"{', '.join(missing)} is missing from {sys.executable}.\n"
            f"Install it there, or install Python {BUILD_PYTHON_TAG} and "
            f"run: py -{BUILD_PYTHON_TAG} -m pip install libtorrent")
    launcher = shutil.which("py")
    if not launcher:
        raise SystemExit(
            f"{', '.join(missing)} is missing and the `py` launcher was not "
            f"found to switch to Python {BUILD_PYTHON_TAG}.")
    print(f"{', '.join(missing)} missing here - rebuilding under "
          f"Python {BUILD_PYTHON_TAG}...")
    environment = dict(os.environ, ATOMIC_BUILD_REEXEC="1")
    result = subprocess.run([launcher, f"-{BUILD_PYTHON_TAG}",
                             str(Path(__file__).resolve()), *sys.argv[1:]],
                            env=environment)
    raise SystemExit(result.returncode)


def _importable(name: str) -> bool:
    import importlib.util
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _ensure_pyinstaller():
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("PyInstaller not found - installing...")
        subprocess.run([sys.executable, "-m", "pip", "install", "pyinstaller>=6.0"], check=True)


def _resolve(node, known):
    """A spec-file expression as a string, or None if it isn't one this
    understands - a literal, a name assigned earlier, or os.path.join of
    those."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return known.get(node.id)
    if isinstance(node, ast.Call) and _is_path_join(node.func) and node.args:
        parts = [_resolve(arg, known) for arg in node.args]
        if all(part is not None for part in parts):
            return os.path.join(*parts)
    return None


def _is_path_join(func):
    """os.path.join specifically. The spec calls other functions of its
    own (_write_version_resource), and treating any call as a join turns
    those into a crash rather than a "not a path"."""
    return (isinstance(func, ast.Attribute) and func.attr == "join"
            and isinstance(func.value, ast.Attribute) and func.value.attr == "path")


def _required_datas():
    """What Atomic.spec promises to bundle, as (name inside the archive,
    file on disk).

    Read with `ast` rather than by matching the spec's text. The spec
    names its files through variables, and a regex over the source finds
    nothing at all the moment one of them is spelled differently - which
    would leave this returning an empty list and every build "verified"
    against nothing. A check that passes because it looked at nothing is
    worse than no check, so an unreadable spec raises instead.
    """
    tree = ast.parse(SPEC_FILE.read_text(encoding="utf-8"))

    # SPECPATH is injected by PyInstaller; the spec resolves every path
    # from it, so it has to be seeded here the same way.
    known = {"SPECPATH": str(PACKAGING_DIR)}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name):
            value = _resolve(node.value, known)
            if value is not None:
                known[node.targets[0].id] = value

    datas = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "Analysis":
            datas = next((kw.value for kw in node.keywords if kw.arg == "datas"), None)
    if not isinstance(datas, ast.List):
        raise SystemExit(f"Couldn't read datas=[...] out of {SPEC_FILE.name} - "
                         "the bundle check has nothing to verify against.")

    required = []
    for element in datas.elts:
        source = _resolve(element.elts[0], known)
        dest = _resolve(element.elts[1], known)
        if source is None or dest is None:
            raise SystemExit(f"Couldn't resolve a datas entry in {SPEC_FILE.name}: "
                             f"{ast.dump(element)}")
        # **Joined with the OS separator, then normalised on both
        # sides at lookup.** PyInstaller writes a nested destination
        # into the archive using the platform separator - measured,
        # `assets\icons\anime.png` - while this built the name with a
        # forward slash and reported all 17 nav icons "missing from the
        # executable" on a clean build that had bundled every one of
        # them. Nothing used a nested dest before the assets folder, so
        # the bug had never been reachable.
        name = (os.path.basename(source) if dest == "."
                else os.path.join(dest, os.path.basename(source)))
        required.append((name, Path(source).resolve()))
    return required


def _canon(name):
    r"""An archive entry name with its separators flattened, so a lookup
    cannot fail on `/` against `\` - see the note in _required_datas."""
    return str(name).replace("\\", "/").lower()


def _verify_bundle(exe_path):
    """Every promised file is in the exe, and is the file that is on disk
    now.

    Byte-comparing rather than checking the name is present: a cached
    build carries the *previous* copy of an asset under the right name,
    which is the failure that would otherwise still get through.
    """
    from PyInstaller.archive.readers import CArchiveReader

    archive = CArchiveReader(str(exe_path))
    bundled = {_canon(n): n for n in archive.toc}

    problems = []
    for name, source in _required_datas():
        actual = bundled.get(_canon(name))
        if actual is None:
            problems.append(f"{name} is missing from the executable")
            continue
        if archive.extract(actual) != source.read_bytes():
            problems.append(f"{name} in the executable differs from {source}")

    if problems:
        sys.exit("\nBUILD REJECTED - the executable does not match the source tree:\n  "
                 + "\n  ".join(problems)
                 + f"\n\nThis is what shipped in 1.4. Clean and build again:\n"
                   f"  rmdir /s /q \"{WORK_DIR}\" \"{DIST_DIR}\"\n"
                   f"  python packaging/build.py\n")

    print(f"Bundle verified: {len(bundled)} entries, "
          f"{len(_required_datas())} bundled files byte-identical to src/.")


def _verify_not_cached():
    """PyInstaller rebuilt something, rather than re-copying a cached
    binary.

    The signal is its own work directory: a real build rewrites the toc,
    PYZ and PKG files in there, so the newest file under packaging/build
    ends up newer than the newest file under src/. When PyInstaller
    decides nothing changed it leaves them alone, and a source file
    edited since the last build sorts above them. False alarms are
    possible (editing a file the app never imports) and are deliberately
    left as failures: cleaning and rebuilding costs 20 seconds, and
    shipping the wrong binary cost a release.
    """
    if not WORK_DIR.exists():
        return
    newest_work = max((path.stat().st_mtime for path in WORK_DIR.rglob("*") if path.is_file()),
                      default=0)
    newest_src = max((path.stat().st_mtime for path in SRC_DIR.rglob("*") if path.is_file()),
                     default=0)
    # Only "source is newer than the build" fails. Building twice with
    # nothing changed in between legitimately writes nothing, and the
    # binary from it is correct - rejecting that would train whoever
    # releases to ignore this check, which is how the 1.4 one was missed.
    if newest_work < newest_src:
        sys.exit("\nBUILD REJECTED - PyInstaller wrote nothing new under "
                 f"{WORK_DIR.name}, so this build is its cache rather than "
                 "your source.\n"
                 f"  rmdir /s /q \"{WORK_DIR}\" \"{DIST_DIR}\"\n"
                 "  python packaging/build.py\n")


def _refresh_shell_icon(exe_path):
    """Tell Explorer this file changed, so it re-reads the icon.

    Windows caches an executable's icon per path, and a rebuild that
    keeps the same path keeps the cached picture - which is why a
    freshly gold icon can go on showing as the old one in folder view
    (the owner reported exactly that, with an app_icon.ico measured at
    21% gold and 0% blue). SHChangeNotify is the polite ask: it
    invalidates this one item rather than deleting the whole icon
    cache database and restarting the shell.

    Best effort - a build that cannot reach the shell API still
    produced a correct exe, so this never fails the build."""
    if os.name != "nt":
        return
    try:
        import ctypes
        SHCNE_UPDATEITEM = 0x00002000
        SHCNE_ASSOCCHANGED = 0x08000000
        SHCNF_PATHW = 0x0005
        SHCNF_FLUSH = 0x1000
        shell32 = ctypes.windll.shell32
        shell32.SHChangeNotify(SHCNE_UPDATEITEM, SHCNF_PATHW | SHCNF_FLUSH,
                               ctypes.c_wchar_p(str(exe_path)), None)
        shell32.SHChangeNotify(SHCNE_ASSOCCHANGED, SHCNF_FLUSH, None, None)
        print("Asked Explorer to re-read the icon.")
    except Exception as error:            # pragma: no cover - shell only
        print(f"(Could not refresh the shell icon cache: {error})")


def main():
    # Before anything else: a build without the torrent engine is not a
    # build worth doing.
    _reexec_on_build_python()
    if not ICON_FILE.exists():
        sys.exit(f"Missing {ICON_FILE.name} in {ASSETS_DIR} - put the icon there before building.")
    if not SPEC_FILE.exists():
        sys.exit(f"Missing {SPEC_FILE.name} in {PACKAGING_DIR}.")

    _ensure_pyinstaller()
    # Read before building: a spec this can't parse is a problem to hear
    # about now, not after a two-minute build.
    _required_datas()

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

    _verify_not_cached()
    _verify_bundle(built_exe)

    final_exe = PROJECT_ROOT / "Atomic.exe"
    shutil.copy2(built_exe, final_exe)
    _refresh_shell_icon(final_exe)
    print(f"\nDone: {final_exe}")
    if "--zip" in sys.argv[1:]:
        print(f"Zipped: {_write_zip(final_exe)}")


def _write_zip(exe: Path) -> Path:
    """`Atomic.zip`, holding the one executable - what a release and the
    remote-tests branch ship (CLAUDE.md rule 8).

    Not made on every build: it costs ten seconds and another 95MB on
    disk, and a local test run needs the exe rather than the archive, so
    the release and the remote-tests push ask for it with --zip.

    One file, named exactly `Atomic.exe` at the root of the archive -
    `updater._exe_from_zip` refuses anything else rather than guessing
    which of several executables to install."""
    import zipfile
    archive = PROJECT_ROOT / "Atomic.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED,
                         compresslevel=6) as bundle:
        bundle.write(exe, "Atomic.exe")
    return archive


if __name__ == "__main__":
    main()
