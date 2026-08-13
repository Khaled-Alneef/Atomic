# -*- mode: python ; coding: utf-8 -*-

import os

# SPECPATH (injected by PyInstaller) is this file's own directory
# (packaging/) - resolve everything from there so the build works
# regardless of the working directory it's invoked from.
SRC_DIR = os.path.join(SPECPATH, "..", "src")
ICON_FILE = os.path.join(SRC_DIR, "app_icon.ico")
LOGO_FILE = os.path.join(SRC_DIR, "atomic_icon.png")

a = Analysis(
    [os.path.join(SRC_DIR, "main.py")],
    pathex=[SRC_DIR],
    binaries=[],
    # app_icon.ico is set as the EXE's own file-icon resource below via
    # `icon=`, but that's separate from Qt's runtime app.setWindowIcon()
    # call in main.py, which needs the actual file to exist next to the
    # frozen script at runtime (sys._MEIPASS) - without bundling it here,
    # that lookup silently fails and every window/taskbar icon comes up
    # blank even though the EXE file itself looks fine in Explorer.
    # atomic_icon.png (the sidebar header logo) needs the same treatment.
    datas=[(ICON_FILE, '.'), (LOGO_FILE, '.')],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='Atomic',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=[ICON_FILE],
)
