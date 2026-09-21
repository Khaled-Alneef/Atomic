"""The bridge installer: what an older install downloads as Atomic.exe.

**Since 21 September 2026 it installs the folder build.** The app stopped
being one exe (packaging/Atomic.spec, the COLLECT note: every launch
unpacked 290MB into %TEMP%, Home at 6.5-7.1s against 2.0-2.1s as a
folder). Every install up to the last single-file release updates by
swapping one .exe into its own place - out of Atomic.zip (2.0 on) or
committed at the tag (1.0-1.10) - so both now carry this program. It
takes the folder build out of the release's Atomic.zip (its app.zip -
or one already beside it, from a zip extracted by hand), installs it into
%LOCALAPPDATA%\\Programs\\Atomic, puts Atomic.lnk on the Desktop and in
the Start menu, starts the installed app, and removes its own loose exe.
The installed app re-points the startup task at itself on its first
launch (helpers/startup.reconcile).

What follows is the 2.0 history of the same program, which still holds.

**Why this exists at all.** Every Atomic ever released updates itself by
reading one file out of this repository at the release tag
(`/contents/Atomic.exe?ref=v1.9`) and swapping it in. Atomic 2.0 is
126MB and GitHub refuses any file over 100MiB on push, so the real build
cannot be committed there any more - it ships as a release *asset*,
where the limit is 2GB. An install running 1.10 or older knows nothing
about assets, and would be told "v2.0 has no Atomic.exe committed to it"
for ever.

So the file committed as `Atomic.exe` at the v2.0 tag is this: a small
program that finishes the update the old updater started. It is what the
old app downloads, verifies against GitHub's own blob hash, and
relaunches - and the first thing it does is fetch the real Atomic.zip
from the release, check it against the asset's sha256, unpack it and
replace itself with it.

Deliberately standalone - it imports nothing from `src/` and nothing
outside the standard library, so it stays a small download and can be
built when the app cannot. tkinter rather than Qt for the same reason:
PyQt6 would make this bigger than some of the releases it repairs.

It never touches the user's data. Settings, entries, covers and caches
live in %APPDATA%\\Atomic and are not read, written or moved here - the
new build finds everything exactly where it left it, and shows what
changed off the version marker the old build wrote on its own last
launch (helpers/whats_new reads last_seen_version for exactly this
case).

If anything fails it says so and offers both Try Again and the release
page, and it is still Atomic.exe - so closing it and opening Atomic
again simply tries once more.
"""

import ctypes
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
import urllib.request
import zipfile
from pathlib import Path
from tkinter import ttk

REPO = "Khaled-Alneef/Atomic"
# ATOMIC_SETUP_API points a harness at a stand-in for GitHub's API.
API_ROOT = os.environ.get("ATOMIC_SETUP_API") or ("https://api.github.com/repos/" + REPO)
RELEASES_PAGE = "https://github.com/" + REPO + "/releases/latest"
# The release asset, and the folder build packed inside it
# (packaging/build.py _write_release_zip; helpers/updater.APP_PAYLOAD_NAME).
ZIP_NAME = "Atomic.zip"
PAYLOAD_NAME = "app.zip"
EXE_NAME = "Atomic.exe"

# The same test the app itself applies: two numeric parts is a release,
# three is a development build, and a pre-release is a test build that
# must never be installed over somebody's app.
RELEASE_TAG_RE = re.compile(r"^v?\d+\.\d+$")

HEADERS = {
    "Accept": "application/vnd.github+json",
    "User-Agent": "AtomicSetup/2.0",
}

# Atomic's own palette (helpers/theme.py), copied rather than imported -
# see the module docstring on why nothing here reaches into src/.
BG = "#0a0e16"
SURFACE = "#141b28"
BORDER = "#2a3548"
TEXT = "#e8eef6"
TEXT_MUTED = "#93a1b5"
ACCENT = "#2fb9a6"
DANGER = "#ff5470"

CONNECT_TIMEOUT = 20
READ_TIMEOUT = 120

# PyInstaller's bootloader variables. A child that inherits them looks
# into *this* program's unpacked folder for its own bundle, which is
# exactly what broke the relaunch when the app's own updater was written
# (helpers/child_process carries the long version).
_BOOTLOADER_VARS = ("_PYI_APPLICATION_HOME_DIR", "_PYI_ARCHIVE_FILE",
                    "_PYI_PARENT_PROCESS_LEVEL", "_PYI_SPLASH_IPC",
                    "_MEIPASS2")

class InstallError(Exception):
    """Anything that stopped the install, with a message worth showing."""


def clean_env() -> dict:
    environment = os.environ.copy()
    for name in _BOOTLOADER_VARS:
        environment.pop(name, None)
    return environment


def target_exe() -> Path:
    """Where this program is - which is where Atomic.exe has to end up,
    because the old updater has already moved this into its place.

    Run from source it refuses to guess. The first version answered
    `cwd/Atomic.exe`, and the first test of it would have overwritten the
    development build sitting in the repository root; ATOMIC_SETUP_TARGET
    is how a harness says where it may write instead."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve()
    override = os.environ.get("ATOMIC_SETUP_TARGET")
    if override:
        return Path(override).resolve()
    raise InstallError(
        "This is the setup program running from source, so there is "
        "nothing to replace. Set ATOMIC_SETUP_TARGET to test it.")


def get_json(url: str):
    request = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(request, timeout=CONNECT_TIMEOUT) as response:
        return json.loads(response.read().decode("utf-8", "replace"))


def version_of(text: str):
    return tuple(int(part) for part in re.findall(r"\d+", text or "")) or (0,)


def newest_release():
    """The newest published release carrying an Atomic.zip asset.

    Not `/releases/latest`: that is whatever GitHub last marked latest,
    and it answers 404 while a repository holds only pre-releases.
    Walking the list and comparing the versions as numbers is the same
    rule the app uses, so this cannot land somewhere the app would not."""
    try:
        releases = get_json(API_ROOT + "/releases?per_page=100")
    except Exception as exc:
        raise InstallError(readable(exc)) from exc

    best = None
    for release in releases or []:
        if release.get("draft") or release.get("prerelease"):
            continue
        tag = (release.get("tag_name") or "").strip()
        if not RELEASE_TAG_RE.match(tag):
            continue
        for asset in release.get("assets") or []:
            if (asset.get("name") or "") != ZIP_NAME:
                continue
            if not asset.get("browser_download_url"):
                continue
            found = {
                "tag": tag,
                "version": tag.lstrip("vV"),
                "url": asset["browser_download_url"],
                "size": asset.get("size") or 0,
                "sha256": (asset.get("digest") or "").split(":")[-1].lower(),
            }
            if best is None or version_of(tag) > version_of(best["tag"]):
                best = found
            break
    if best is None:
        raise InstallError(
            "No published release carries " + ZIP_NAME + " yet. Download "
            "Atomic from the releases page instead.")
    return best


def download(url: str, expected_size: int, on_progress) -> bytes:
    request = urllib.request.Request(url, headers=HEADERS)
    chunks, received = [], 0
    try:
        with urllib.request.urlopen(request, timeout=READ_TIMEOUT) as response:
            total = expected_size or int(
                response.headers.get("Content-Length") or 0)
            while True:
                chunk = response.read(512 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
                received += len(chunk)
                on_progress(received, total)
    except Exception as exc:
        raise InstallError(readable(exc)) from exc
    data = b"".join(chunks)
    if expected_size and len(data) != expected_size:
        raise InstallError(
            "The download was incomplete ({:,} of {:,} bytes).".format(
                len(data), expected_size))
    return data


def readable(exc) -> str:
    text = str(exc)
    if "403" in text:
        return ("GitHub is rate-limiting anonymous requests from this "
                "network - try again in a little while.")
    if "404" in text:
        return "The download could not be found on GitHub."
    return "Could not reach GitHub - check your connection."


def install_dir() -> Path:
    """%LOCALAPPDATA%\\Programs\\Atomic - where the folder build lives
    (helpers/startup.install_dir says the same). ATOMIC_SETUP_INSTALL_DIR
    is how a harness points it at a sandbox."""
    override = os.environ.get("ATOMIC_SETUP_INSTALL_DIR")
    if override:
        return Path(override).resolve()
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser(r"~\AppData\Local")
    return Path(base) / "Programs" / "Atomic"


def shortcut_dirs():
    """The Desktop (following a redirected one) and the per-user Start
    menu. ATOMIC_SETUP_SHORTCUT_DIRS (';'-separated) overrides both, and
    an empty value makes none - a harness must not touch the real Desktop."""
    override = os.environ.get("ATOMIC_SETUP_SHORTCUT_DIRS")
    if override is not None:
        return [Path(p) for p in override.split(";") if p.strip()]
    found = []
    for csidl in (0x10, 0x02):   # CSIDL_DESKTOPDIRECTORY, CSIDL_PROGRAMS
        buf = ctypes.create_unicode_buffer(260)
        try:
            if ctypes.windll.shell32.SHGetFolderPathW(None, csidl, None, 0, buf) == 0:
                found.append(Path(buf.value))
        except Exception:
            pass
    return found


def unpack_folder(data: bytes, parent: Path) -> Path:
    """The folder build (app.zip) unpacked into `parent\\Atomic.new-<pid>`;
    returns that folder's `Atomic`. Every entry must sit under `Atomic/`,
    land inside the staging folder, and the result must hold Atomic.exe and
    _internal - anything else is a packaging mistake and is refused."""
    import shutil
    staging = parent / "Atomic.new-{}".format(os.getpid())
    shutil.rmtree(staging, ignore_errors=True)
    try:
        staging.mkdir(parents=True)
        root = staging.resolve()
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = archive.namelist()
            if not names or any(not n.startswith("Atomic/") for n in names):
                raise InstallError("The download is not an Atomic folder "
                                   "build - it has not been installed.")
            for name in names:
                if not (root / name).resolve().is_relative_to(root):
                    raise InstallError("The download holds an unsafe path - "
                                       "it has not been installed.")
            archive.extractall(root)
        folder = root / "Atomic"
        if not (folder / EXE_NAME).is_file() or not (folder / "_internal").is_dir():
            raise InstallError("The download is missing Atomic.exe or its "
                               "_internal folder - it has not been installed.")
        return folder
    except InstallError:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    except Exception as exc:
        shutil.rmtree(staging, ignore_errors=True)
        raise InstallError("The download could not be unpacked: {}".format(exc)) from exc


def place_folder(new_folder: Path, target: Path):
    """Put `new_folder` at `target`: an existing install is stepped aside
    and removed after, and put back if the new one will not go in. A
    folder in use (an Atomic already running from it) refuses the rename,
    which is reported rather than half-installed over."""
    import shutil
    old = target.with_name(target.name + ".old")
    shutil.rmtree(old, ignore_errors=True)
    stepped = False
    if target.exists():
        try:
            os.replace(target, old)
            stepped = True
        except OSError:
            raise InstallError("Atomic is already installed and running from "
                               "{}. Close it and open this again.".format(target))
    try:
        os.replace(new_folder, target)
    except OSError as exc:
        if stepped:
            try:
                os.replace(old, target)
            except OSError:
                pass
        raise InstallError("Could not install into {}: {}".format(target, exc)) from exc
    shutil.rmtree(old, ignore_errors=True)
    shutil.rmtree(new_folder.parent, ignore_errors=True)


def make_shortcuts(exe: Path) -> list:
    """Atomic.lnk in each of shortcut_dirs(), through the shell's own
    WScript.Shell (no pywin32 in this program - see the module note).
    Returns the ones written; a shortcut that cannot be made is not a
    failed install."""
    made = []
    for folder in shortcut_dirs():
        link = folder / "Atomic.lnk"
        script = ("$s = (New-Object -ComObject WScript.Shell).CreateShortcut('{link}'); "
                  "$s.TargetPath = '{exe}'; $s.WorkingDirectory = '{cwd}'; "
                  "$s.IconLocation = '{exe},0'; $s.Description = 'Atomic'; $s.Save()"
                  ).format(link=str(link).replace("'", "''"),
                           exe=str(exe).replace("'", "''"),
                           cwd=str(exe.parent).replace("'", "''"))
        try:
            folder.mkdir(parents=True, exist_ok=True)
            subprocess.run(["powershell", "-NoProfile", "-NonInteractive",
                            "-ExecutionPolicy", "Bypass", "-Command", script],
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                           timeout=30, capture_output=True)
            if link.exists():
                made.append(link)
        except Exception:
            pass
    return made


# Removes this program's own loose exe once it has exited - it replaced
# the old single-file Atomic.exe, and the installed folder plus its
# shortcut take that place now. %1 the exe, %2 this process id.
# System32's tools by full path: a PATH carrying Git's usr\bin (measured,
# 21 September 2026, a harness launched from Git Bash) answers `find` with
# GNU find, which fails, so the wait ended at once and the delete met a
# still-running exe and gave up. The delete is retried for the same reason.
_REMOVE_SELF = """@echo off
set /a WAITED=0
:wait
"%SystemRoot%\\System32\\tasklist.exe" /FI "PID eq %~2" 2>nul | "%SystemRoot%\\System32\\find.exe" "%~2" >nul || goto gone
set /a WAITED+=1
if %WAITED% geq 30 goto gone
"%SystemRoot%\\System32\\ping.exe" -n 2 127.0.0.1 >nul
goto wait
:gone
set /a WAITED=0
:remove
del /f /q "%~1" >nul 2>&1
if not exist "%~1" goto done
set /a WAITED+=1
if %WAITED% geq 30 goto done
"%SystemRoot%\\System32\\ping.exe" -n 2 127.0.0.1 >nul
goto remove
:done
(goto) 2>nul & del "%~f0"
"""


def payload_from_release(data: bytes) -> bytes:
    """The folder build (app.zip) out of a verified Atomic.zip."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            if PAYLOAD_NAME not in archive.namelist():
                raise InstallError("The download does not carry the Atomic "
                                   "app - it has not been installed.")
            return archive.read(PAYLOAD_NAME)
    except InstallError:
        raise
    except Exception as exc:
        raise InstallError("The download could not be unpacked: {}".format(exc)) from exc


def local_payload():
    """app.zip beside this program - a release zip extracted by hand -
    or None. Installing from it needs no download at all."""
    try:
        here = target_exe().parent / PAYLOAD_NAME
    except InstallError:
        return None
    try:
        return here.read_bytes() if here.is_file() else None
    except OSError:
        return None


def install(data: bytes) -> Path:
    """Install the folder build (the app.zip bytes) and start it. Returns
    the installed exe."""
    target = install_dir()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise InstallError("Could not create {}: {}".format(target.parent, exc)) from exc
    folder = unpack_folder(data, target.parent)
    place_folder(folder, target)
    exe = target / EXE_NAME
    make_shortcuts(exe)
    allow_foreground()
    subprocess.Popen([str(exe)], cwd=str(target), close_fds=True, env=clean_env(),
                     creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    # This program's own exe goes once it has exited - unless it is the
    # installed one (a build that never should be, but must not delete
    # the app it just installed).
    try:
        me = target_exe()
        if me.exists() and target not in me.parents:
            script = Path(tempfile.gettempdir()) / "atomic-setup-{}.bat".format(os.getpid())
            script.write_text(_REMOVE_SELF, encoding="utf-8")
            subprocess.Popen(["cmd", "/c", str(script), str(me), str(os.getpid())],
                             creationflags=(getattr(subprocess, "CREATE_NO_WINDOW", 0)
                                            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)),
                             close_fds=True, env=clean_env())
    except InstallError:
        pass                            # running from source: nothing to remove
    return exe


def allow_foreground():
    """Hand the foreground right to what is about to be started, or the
    new Atomic opens behind everything blinking in the taskbar."""
    try:
        ctypes.windll.user32.AllowSetForegroundWindow(-1)
    except Exception:
        pass


def resource(name: str) -> Path:
    base = getattr(sys, "_MEIPASS", None)
    return Path(base or Path(__file__).resolve().parent) / name


class SetupWindow:
    """One window, three states: working, failed, done.

    The worker thread posts back through `after`, because tkinter is no
    safer to touch off its own thread than Qt is."""

    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Atomic Setup")
        self.root.configure(bg=BG)
        self.root.resizable(False, False)
        self._centre(560, 268)
        try:
            icon = resource("app_icon.ico")
            if icon.exists():
                self.root.iconbitmap(default=str(icon))
        except Exception:
            pass

        frame = tk.Frame(self.root, bg=BG, padx=32, pady=26)
        frame.pack(fill="both", expand=True)

        self.title = tk.Label(frame, text="Moving Atomic to its new home",
                              bg=BG, fg=TEXT, font=("Segoe UI", 15, "bold"),
                              anchor="w", justify="left")
        self.title.pack(fill="x")

        self.detail = tk.Label(
            frame,
            text=("Atomic now installs as a folder so it starts in about two "
                  "seconds. This one-time step downloads it, puts it in your "
                  "apps folder and leaves a shortcut on your Desktop.\n"
                  "Your entries, settings and covers are untouched."),
            bg=BG, fg=TEXT_MUTED, font=("Segoe UI", 9), anchor="w",
            justify="left", wraplength=496)
        self.detail.pack(fill="x", pady=(8, 18))

        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure("Atomic.Horizontal.TProgressbar", troughcolor=SURFACE,
                        bordercolor=BORDER, background=ACCENT,
                        lightcolor=ACCENT, darkcolor=ACCENT, thickness=8)
        self.bar = ttk.Progressbar(frame, style="Atomic.Horizontal.TProgressbar",
                                   mode="indeterminate", maximum=1000)
        self.bar.pack(fill="x")
        self.bar.start(12)

        self.status = tk.Label(frame, text="Asking GitHub for the latest release...",
                               bg=BG, fg=TEXT_MUTED, font=("Segoe UI", 9),
                               anchor="w", justify="left", wraplength=496)
        self.status.pack(fill="x", pady=(12, 0))

        self.buttons = tk.Frame(frame, bg=BG)
        self.buttons.pack(fill="x", pady=(16, 0))
        self.retry_btn = self._button(self.buttons, "Try Again", self.start,
                                      accent=True)
        self.page_btn = self._button(self.buttons, "Open the download page",
                                     self.open_page)

    # ------------------------------------------------------------------
    def _button(self, parent, text, command, accent=False):
        return tk.Button(
            parent, text=text, command=command, relief="flat", bd=0,
            font=("Segoe UI", 9, "bold" if accent else "normal"),
            bg=ACCENT if accent else SURFACE, fg=BG if accent else TEXT,
            activebackground=ACCENT if accent else BORDER,
            activeforeground=BG if accent else TEXT,
            padx=16, pady=7, cursor="hand2")

    def hide_buttons(self):
        self.retry_btn.pack_forget()
        self.page_btn.pack_forget()

    def show_buttons(self):
        self.retry_btn.pack(side="left")
        self.page_btn.pack(side="left", padx=(10, 0))

    def _centre(self, width, height):
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        x = int((screen_w - width) / 2)
        y = int((screen_h - height) / 2.4)
        self.root.geometry("{}x{}+{}+{}".format(width, height, x, y))

    def open_page(self):
        try:
            os.startfile(RELEASES_PAGE)
        except Exception:
            pass

    # ------------------------------------------------------------------
    def say(self, text, colour=TEXT_MUTED):
        self.status.configure(text=text, fg=colour)

    def progress(self, done, total):
        if total:
            self.root.after(0, self._progress, done, total)

    def _progress(self, done, total):
        if str(self.bar["mode"]) != "determinate":
            self.bar.stop()
            self.bar.configure(mode="determinate")
        self.bar["value"] = int(done * 1000 / total)
        self.say("Downloading Atomic... {:.0f} of {:.0f} MB".format(
            done / (1024 * 1024), total / (1024 * 1024)))

    def fail(self, message):
        self.bar.stop()
        self.bar.configure(mode="determinate")
        self.bar["value"] = 0
        self.title.configure(text="The update could not be finished")
        self.say(message, DANGER)
        self.show_buttons()

    def finished(self, version):
        self.bar.stop()
        self.bar.configure(mode="determinate", maximum=1000)
        self.bar["value"] = 1000
        self.title.configure(text=("Atomic {} is installed".format(version)
                                   if version else "Atomic is installed"))
        self.say("Starting it now...")
        self.root.after(1400, self.root.destroy)

    # ------------------------------------------------------------------
    def start(self):
        self.hide_buttons()
        self.title.configure(text="Moving Atomic to its new home")
        self.bar.configure(mode="indeterminate")
        self.bar.start(12)
        self.say("Asking GitHub for the latest release...")
        threading.Thread(target=self._work, daemon=True).start()

    def _work(self):
        release = None
        try:
            local = local_payload()
            if local is not None:
                # From a zip extracted by hand. An app.zip that is not an
                # Atomic folder build is refused by unpack_folder, and the
                # install then goes on to the download as if it were not
                # there.
                self.root.after(0, self.say, "Installing...")
                try:
                    install(local)
                    self.root.after(0, self.finished, "")
                    return
                except InstallError:
                    pass
            release = newest_release()
            self.root.after(0, self.say,
                            "Downloading Atomic {}...".format(release["version"]))
            data = download(release["url"], release["size"], self.progress)
            expected = release.get("sha256") or ""
            if expected and hashlib.sha256(data).hexdigest() != expected:
                raise InstallError(
                    "The download did not match the checksum GitHub "
                    "reported, so it has not been installed.")
            self.root.after(0, self.say, "Verified. Installing...")
            install(payload_from_release(data))
        except InstallError as exc:
            self.root.after(0, self.fail, str(exc))
        except Exception as exc:                     # never kill the thread
            self.root.after(0, self.fail, "Something went wrong: {}".format(exc))
        else:
            self.root.after(0, self.finished, release["version"])

    def run(self):
        self.root.after(300, self.start)
        self.root.mainloop()


def selftest(report: Path) -> int:
    """Prove the *built* program works, without a window.

    `--selftest` exists because the first release of this installer was
    verified by importing atomic_setup in the development tree and never
    by running the exe: the spec excluded `email`, `urllib.request`
    imports it at module scope, and the frozen program died on its first
    line - on the owner's machine, mid-update, with this already swapped
    in as his Atomic.exe. build_bridge.py runs this against every build
    and refuses one that cannot answer, so that class of failure cannot
    reach a tag again.

    **It writes to a file, not to stdout.** This is built with
    `console=False`, where PyInstaller leaves `sys.stdout` as None and a
    bare print() raises - a selftest that could only report through
    stdout would fail every windowed build for the wrong reason.

    It resolves the real release and reads the first bytes of the asset;
    it never writes an executable and never touches an install."""
    lines = []

    def say(text):
        lines.append(str(text))
        try:
            print(text)
        except Exception:
            pass

    code = 1
    try:
        say(f"python: {sys.version}")
        say(f"frozen: {bool(getattr(sys, 'frozen', False))}")
        head = b"PK"
        try:
            release = newest_release()
        except InstallError as exc:
            # The bridge is built *before* the first release that carries
            # app.zip is published, so "none yet" is expected then;
            # having reached the API and read the list is what is proven.
            if "No published release carries" not in str(exc):
                raise
            release = None
            say(f"release: none carries {ZIP_NAME} yet - the API answered")
        if release is not None:
            say(f"release: {release['tag']} {release['size']:,} bytes")
            say(f"url: {release['url']}")
            request = urllib.request.Request(release["url"], headers=HEADERS)
            request.add_header("Range", "bytes=0-3")
            with urllib.request.urlopen(request, timeout=CONNECT_TIMEOUT) as response:
                head = response.read(4)
            say(f"first bytes: {head!r}")
        if head[:2] != b"PK":
            say("SELFTEST FAILED: that is not a zip")
        else:
            # Built and destroyed without being shown, so a missing
            # tkinter or a bad style is caught here too.
            window = SetupWindow()
            window.root.destroy()
            say("SELFTEST OK")
            code = 0
    except Exception as exc:
        say(f"SELFTEST FAILED: {type(exc).__name__}: {exc}")
        import traceback
        lines.append(traceback.format_exc())
    try:
        report.write_text("\n".join(lines), encoding="utf-8")
    except Exception:
        pass
    return code


def main():
    argv = sys.argv[1:]
    if "--selftest" in argv:
        rest = argv[argv.index("--selftest") + 1:]
        report = Path(rest[0]) if rest else Path("atomic_setup_selftest.txt")
        raise SystemExit(selftest(report))
    SetupWindow().run()


if __name__ == "__main__":
    main()
