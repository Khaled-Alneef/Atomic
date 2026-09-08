"""The bridge installer: what a pre-2.0 install downloads as Atomic.exe.

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
API_ROOT = "https://api.github.com/repos/" + REPO
RELEASES_PAGE = "https://github.com/" + REPO + "/releases/latest"
ZIP_NAME = "Atomic.zip"
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

# Identical in shape to the one in helpers/updater.py, and for the same
# reason: Windows will not let a running executable be replaced, so the
# swap outlives this process. It retries the move until this program has
# exited, starts what it moved into place, and deletes itself.
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


def exe_from_zip(data: bytes) -> bytes:
    """The one executable inside the release zip, and nothing guessed at:
    a zip holding anything else is a packaging mistake, and installing it
    would be worse than saying so."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = [n for n in archive.namelist()
                     if n.lower().endswith(".exe") and not n.endswith("/")]
            if len(names) != 1:
                raise InstallError(
                    "The download should hold exactly one .exe and holds "
                    "{} - it has not been installed.".format(len(names)))
            return archive.read(names[0])
    except InstallError:
        raise
    except Exception as exc:
        raise InstallError(
            "The download could not be unpacked: {}".format(exc)) from exc


def install(payload: bytes):
    """Write the new build beside the target and hand the swap over."""
    target = target_exe()
    if not os.access(target.parent, os.W_OK):
        raise InstallError(
            "No permission to replace {} in {}. Move Atomic somewhere "
            "writable and open it again.".format(target.name, target.parent))

    # Staged in the target's own folder, not %TEMP%: `move` across
    # volumes copies rather than renames, and 126MB copied while the old
    # process is still exiting is where the retry loop would be spent.
    handle, staged = tempfile.mkstemp(prefix="Atomic-new-", suffix=".exe",
                                      dir=str(target.parent))
    try:
        with os.fdopen(handle, "wb") as file:
            file.write(payload)
    except OSError as exc:
        raise InstallError(
            "Could not write the new build to disk: {}".format(exc)) from exc

    script = Path(tempfile.gettempdir()) / "atomic-setup-{}.bat".format(os.getpid())
    script.write_text(_SWAP_SCRIPT, encoding="utf-8")
    allow_foreground()
    subprocess.Popen(
        ["cmd", "/c", str(script), str(target), staged],
        creationflags=(getattr(subprocess, "CREATE_NO_WINDOW", 0)
                       | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)),
        close_fds=True, env=clean_env(),
    )


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

        self.title = tk.Label(frame, text="Finishing the update to Atomic 2.0",
                              bg=BG, fg=TEXT, font=("Segoe UI", 15, "bold"),
                              anchor="w", justify="left")
        self.title.pack(fill="x")

        self.detail = tk.Label(
            frame,
            text=("Atomic 2.0 is larger than the old update route can carry, "
                  "so this one-time step downloads it for you.\n"
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
        self.title.configure(text="Atomic {} is installed".format(version))
        self.say("Starting it now...")
        self.root.after(1400, self.root.destroy)

    # ------------------------------------------------------------------
    def start(self):
        self.hide_buttons()
        self.title.configure(text="Finishing the update to Atomic 2.0")
        self.bar.configure(mode="indeterminate")
        self.bar.start(12)
        self.say("Asking GitHub for the latest release...")
        threading.Thread(target=self._work, daemon=True).start()

    def _work(self):
        release = None
        try:
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
            install(exe_from_zip(data))
        except InstallError as exc:
            self.root.after(0, self.fail, str(exc))
        except Exception as exc:                     # never kill the thread
            self.root.after(0, self.fail, "Something went wrong: {}".format(exc))
        else:
            self.root.after(0, self.finished, release["version"])

    def run(self):
        self.root.after(300, self.start)
        self.root.mainloop()


def main():
    SetupWindow().run()


if __name__ == "__main__":
    main()
