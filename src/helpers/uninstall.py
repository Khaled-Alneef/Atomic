"""Full app removal, for Settings > Uninstall - distinct from Settings >
Clear Data (which only wipes one content category at a time and leaves
the app itself alone). This wipes the entire per-user data folder and
deletes the running exe.

A running Windows executable can't delete its own file while it's still
executing, so the exe removal is handed off to a short-lived detached
helper command that waits for this process to fully exit first, instead
of deleting anything itself.
"""

import shutil
import subprocess
import sys
from pathlib import Path

from . import startup, storage


def run():
    """Deregister the Windows-startup entry (so a stale one doesn't try
    to launch a file that's about to stop existing), wipe the whole data
    folder, and queue up self-deletion of the running exe. Does *not*
    quit the app - the caller does that right after, which is what lets
    the queued delete's short wait be enough."""
    startup.set_enabled(False)
    shutil.rmtree(storage.DATA_DIR, ignore_errors=True)

    if not getattr(sys, "frozen", False):
        return  # running from source - there's no installed exe to remove

    exe_path = Path(sys.executable)
    if exe_path.suffix.lower() != ".exe" or not exe_path.exists():
        return

    # "ping ... >nul" is a dependency-free ~2s delay available on every
    # Windows install, long enough for this process to finish quitting
    # (unlike this process's own file, a plain cmd.exe launched detached
    # isn't holding the exe open, so the delete succeeds once we're gone).
    command = f'ping 127.0.0.1 -n 3 >nul & del /f /q "{exe_path}"'
    subprocess.Popen(
        ["cmd", "/c", command],
        creationflags=subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS,
        close_fds=True,
    )
