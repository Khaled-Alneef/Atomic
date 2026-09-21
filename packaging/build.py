"""Run this to build Atomic: `python build.py` (add `--zip` for a release).

Wraps `pyinstaller Atomic.spec` (which bundles src/assets/app_icon.ico into the
build - the plain `pyinstaller src/main.py` form skips that and the
taskbar/title-bar icon comes up blank at runtime). Installs PyInstaller
first if it isn't already available. PyInstaller's own work/dist folders
are kept inside this packaging/ directory; the finished folder is then
copied to the project root as `app\\` (Atomic.exe plus `_internal\\`) -
a folder build since 21 September 2026, see Atomic.spec's COLLECT note.

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


def _ensure_pinned_libmpv():
    """Make the player engine reproducible before PyInstaller sees it.

    fetch_libmpv.ensure_pinned() is a no-op once the Stremio-matched mpv
    0.41.0 DLL and its hash marker are present, so offline rebuilds remain
    possible after the first fetch. An unmarked/nightly DLL is replaced.
    """
    import fetch_libmpv

    fetch_libmpv.ensure_pinned()



def _ensure_webview2():
    """Put Edge's WebView2 assemblies in vendor/, from the installed
    pywebview.

    Same arrangement as libmpv above and for the same reason: vendor/ is
    gitignored, so a fresh clone has to be able to produce these rather
    than carry them. They are three small files that ship inside the
    pywebview wheel, and Atomic.spec lists them by literal path because
    build.py verifies every datas entry with `ast` and cannot follow a
    call into site-packages.

    Not fatal when they are missing: helpers/webview2_host reports itself
    unavailable and Home and Discover fall back to their Qt classes, so
    the build is a working app either way.
    """
    target = PACKAGING_DIR.parent / "vendor" / "webview2"
    wanted = ("Microsoft.Web.WebView2.Core.dll",
              "Microsoft.Web.WebView2.WinForms.dll",
              "WebView2Loader.dll")
    if all((target / name).is_file() for name in wanted):
        return
    try:
        import webview
        lib = Path(webview.__file__).parent / "lib"
    except Exception:
        print("pywebview is not installed - Home and Discover will use Qt.")
        return
    sources = {
        "Microsoft.Web.WebView2.Core.dll": lib / "Microsoft.Web.WebView2.Core.dll",
        "Microsoft.Web.WebView2.WinForms.dll": lib / "Microsoft.Web.WebView2.WinForms.dll",
        "WebView2Loader.dll": lib / "runtimes" / "win-x64" / "native" / "WebView2Loader.dll",
    }
    target.mkdir(parents=True, exist_ok=True)
    for name, source in sources.items():
        if not source.is_file():
            print(f"WebView2: {source.name} not found in pywebview.")
            return
        shutil.copy2(source, target / name)
    print(f"Vendored WebView2 assemblies into {target}")


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


def _verify_bundle(app_dir):
    """Every promised file is in the build, and is the file that is on
    disk now.

    Byte-comparing rather than checking the name is present: a cached
    build carries the *previous* copy of an asset under the right name,
    which is the failure that would otherwise still get through.

    A folder build (Atomic.spec's COLLECT) writes the datas as files under
    `_internal`, not into the exe's archive, so they are read from there.
    """
    internal = app_dir / "_internal"
    bundled = {_canon(p.relative_to(internal)): p
               for p in internal.rglob("*") if p.is_file()}

    problems = []
    for name, source in _required_datas():
        actual = bundled.get(_canon(name))
        if actual is None:
            problems.append(f"{name} is missing from the build")
            continue
        if actual.read_bytes() != source.read_bytes():
            problems.append(f"{name} in the build differs from {source}")

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

    **And on a replaced file it does not work - measured 8 September
    2026.** The owner, after updating 1.10 to 2.0 in place: the folder
    went on drawing 1.10's purple icon while the taskbar drew 2.0's
    teal one (the taskbar's comes from the running app, Qt's
    setWindowIcon, not from the shell). Reproduced with the real
    binaries - v1.10's exe out of git, replaced by 2.0's the way the
    swap script does it - and photographed from Explorer. The file was
    provably right throughout: ExtractAssociatedIcon on it returned the
    teal icon, and a copy of the same bytes under a new name in the
    same folder drew teal. What did **not** shift the old path's
    picture: this call, F5, a fresh Explorer window, `ie4uinit -show`,
    `ie4uinit -ClearIconCache`, and a full Explorer restart. What did:
    deleting `iconcache*.db` and `thumbcache*.db` (30 files) with the
    shell stopped, then restarting it.

    So this stays - it is free and it is the right ask - but it is not
    a guarantee, and the app deliberately does not do any of the things
    that would work: an application has no business deleting a user's
    shell caches or restarting their Explorer. It only ever shows when
    the icon itself changes between versions.

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

    # Every build must use the same known player engine. This deliberately runs
    # before the spec validates vendor/libmpv-2.dll so an old nightly copy is
    # replaced instead of silently becoming part of the exe.
    _ensure_pinned_libmpv()
    _ensure_webview2()
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

    built_dir = DIST_DIR / "Atomic"
    built_exe = built_dir / "Atomic.exe"
    if not built_exe.exists():
        sys.exit(f"\nBuild finished, but {built_exe} wasn't found - check the log above.")

    _verify_not_cached()
    _verify_bundle(built_dir)

    # The folder, at the project root as `app\` - a folder build cannot be
    # one loose file (Atomic.spec, the COLLECT note). Replaced whole, so a
    # file dropped from the build does not linger from the last one.
    final_dir = PROJECT_ROOT / APP_DIR_NAME
    if final_dir.exists():
        try:
            shutil.rmtree(final_dir)
        except OSError as error:
            sys.exit(f"\nCould not replace {final_dir} ({error}) - close the "
                     f"Atomic running from it and build again.")
    shutil.copytree(built_dir, final_dir)
    final_exe = final_dir / "Atomic.exe"
    # The single-file build that used to sit here would otherwise go on
    # being run by mistake: it is stale from this build on.
    stale = PROJECT_ROOT / "Atomic.exe"
    if stale.exists():
        try:
            stale.unlink()
            print(f"Removed the old single-file {stale.name} from the project root.")
        except OSError:
            print(f"(Could not remove the old {stale} - it is stale; delete it.)")
    _refresh_shell_icon(final_exe)
    print(f"\nDone: {final_exe}")
    if "--zip" in sys.argv[1:]:
        print(f"Zipped: {_write_release_zip(final_dir)}")


# Where the finished folder lands, beside src/ and packaging/.
APP_DIR_NAME = "app"


# The folder build's name inside the release zip (helpers/updater and
# packaging/bridge read the same name).
APP_PAYLOAD_NAME = "app.zip"


def _write_release_zip(app_dir: Path) -> Path:
    """`Atomic.zip`, the one release asset: the bridge installer as its
    only `Atomic.exe`, and the folder build packed inside as `app.zip`.

    **One name, and the reason it is shaped like this** (the owner, 21
    September 2026: "make sure that the zip file name is Atomic not
    Atomic-app"). Every install up to the last single-file release updates
    by taking *the one .exe* out of Atomic.zip and swapping it in for
    itself (their updater._exe_from_zip, which refuses a zip with more
    than one). The folder build holds its own Atomic.exe, so it cannot sit
    in the zip loose - an old updater would install that launcher without
    its _internal folder. Packed as app.zip it is invisible to them: they
    take the bridge, and the bridge installs app.zip. The folder build's
    updater and a hand-extracted zip read app.zip directly.

    Not made on every build (--zip): it costs time and disk, and a local
    test run needs the folder, not the archive (CLAUDE.md rule 8)."""
    import zipfile
    bridge = PACKAGING_DIR / "bridge"
    result = subprocess.run([sys.executable, str(bridge / "build_bridge.py")])
    if result.returncode != 0:
        sys.exit("\nThe bridge installer did not build - Atomic.zip not written.")
    payload = WORK_DIR / APP_PAYLOAD_NAME
    with zipfile.ZipFile(payload, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as inner:
        for path in sorted(app_dir.rglob("*")):
            if path.is_file():
                inner.write(path, "Atomic/" + path.relative_to(app_dir).as_posix())
    archive = PROJECT_ROOT / "Atomic.zip"
    with zipfile.ZipFile(archive, "w") as outer:
        outer.write(bridge / "dist" / "Atomic.exe", "Atomic.exe",
                    compress_type=zipfile.ZIP_DEFLATED)
        # Stored: it is already compressed, and deflating it again only
        # costs time.
        outer.write(payload, APP_PAYLOAD_NAME, compress_type=zipfile.ZIP_STORED)
    payload.unlink()
    for stale in (PROJECT_ROOT / "Atomic-app.zip",):
        if stale.exists():
            stale.unlink()
    names = zipfile.ZipFile(archive).namelist()
    exes = [n for n in names if n.lower().endswith(".exe")]
    if exes != ["Atomic.exe"]:
        sys.exit(f"\nAtomic.zip must hold exactly one .exe, the bridge - it holds {exes}.")
    return archive


if __name__ == "__main__":
    main()
