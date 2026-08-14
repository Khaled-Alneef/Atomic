"""How this app launches other programs - the environment they are given,
and keeping their console windows off the screen.

**Environment.** PyInstaller's bootloader tells the app it starts where its
unpacked files live, using a handful of `_PYI_*` variables. Those stay in
the environment for the rest of the process's life, so every game, app,
website handler or helper script Atomic launches inherits them.

For an ordinary program that is harmless noise. For another program built
with PyInstaller it is fatal: its own bootloader finds the variables
already set, concludes it has been unpacked, and looks for its Python DLL
in *Atomic's* unpack folder instead of its own. If Atomic has since
exited, that folder is gone and the launched app dies with

    Failed to load Python DLL '...\\_MEI306802\\python315.dll'.
    LoadLibrary: The specified module could not be found.

which is what the updater hit - it relaunches Atomic itself, so the new
build inherited the old build's deleted folder. Confirmed by launching the
packaged app with these variables poisoned and then stripped: the first
shows that dialog, the second starts normally.

**Console windows.** Launching through the shell runs the target via
cmd.exe, and a console program started from a windowless app gets a
console of its own - a black window that flashes up every time a game or
app is opened, and sits there for as long as the updater's swap script
runs. CREATE_NO_WINDOW suppresses it, which is what uninstall.py already
does for the same reason.

Both are inert when running from source or off Windows.
"""

import os
import subprocess

_BOOTLOADER_VARS = (
    "_PYI_APPLICATION_HOME_DIR",
    "_PYI_ARCHIVE_FILE",
    "_PYI_PARENT_PROCESS_LEVEL",
    "_PYI_SPLASH_IPC",
    "_MEIPASS2",   # the name PyInstaller used before 6.x
)

# getattr rather than direct access: these only exist on Windows, and 0 is
# "no special flags" everywhere else.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
NEW_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)


def clean_env() -> dict:
    """This process's environment minus PyInstaller's bootloader
    variables. Pass as `env=` to anything launched from here."""
    environment = os.environ.copy()
    for name in _BOOTLOADER_VARS:
        environment.pop(name, None)
    return environment


def flags(detached: bool = False) -> int:
    """Creation flags for a launched process: never a console window, and
    optionally its own process group so it outlives this one."""
    return NO_WINDOW | (NEW_GROUP if detached else 0)
