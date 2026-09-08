# PyInstaller spec for the bridge installer - see atomic_setup.py for why
# it exists. Built as `Atomic.exe` on purpose: it is committed at the
# release tag under exactly that name, because a pre-2.0 updater asks
# GitHub for `/contents/Atomic.exe?ref=<tag>` and swaps whatever comes
# back into place.
#
# Everything the app needs and this does not is excluded by name rather
# than left to PyInstaller's analysis: an accidental PyQt6 or libtorrent
# in here would turn a 12MB one-time download into a 100MB one, and the
# whole point of this file is to be small enough to live in the
# repository at all.

import os

SPECPATH = os.path.abspath(os.path.dirname(SPEC))
ASSETS_DIR = os.path.join(SPECPATH, "..", "..", "src", "assets")
ICON_FILE = os.path.join(ASSETS_DIR, "app_icon.ico")

a = Analysis(
    [os.path.join(SPECPATH, "atomic_setup.py")],
    pathex=[SPECPATH],
    binaries=[],
    datas=[(ICON_FILE, ".")],
    hiddenimports=[],
    hookspath=[],
    runtime_hooks=[],
    # **Only third-party weight is excluded here, never the standard
    # library.** The first cut of this file excluded `email`, `xml`,
    # `asyncio` and friends to shave a megabyte or two - and
    # `urllib.request` imports `email` at module scope, so the built
    # program died on its first line with `No module named 'email'` on
    # the owner's machine, mid-update, with this exe already swapped in
    # as his Atomic. It cost nothing in size and everything in trust;
    # the standard library stays whole.
    excludes=[
        "PyQt6", "PyQt5", "PySide6", "numpy", "PIL", "libtorrent",
        "mpv", "pandas", "matplotlib", "scipy", "pytest",
    ],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="Atomic",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # upx has never been installed on this machine (CLAUDE.md rule 8
    # records the measurement), so this is off rather than aspirational.
    upx=False,
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=[ICON_FILE],
    version=os.path.join(SPECPATH, "version_info.txt"),
)
