# -*- mode: python ; coding: utf-8 -*-

import os
import re

# SPECPATH (injected by PyInstaller) is this file's own directory
# (packaging/) - resolve everything from there so the build works
# regardless of the working directory it's invoked from.
SRC_DIR = os.path.join(SPECPATH, "..", "src")
ICON_FILE = os.path.join(SRC_DIR, "app_icon.ico")
LOGO_FILE = os.path.join(SRC_DIR, "atomic_icon.png")
FILTER_ICON_FILE = os.path.join(SRC_DIR, "filter_icon.png")
BUILD_DIR = os.path.join(SPECPATH, "build")


def _app_version():
    """The version the app reports and updates against, read straight out
    of helpers/updater.py rather than repeated here - one number, defined
    once, or the file's stated version drifts from the running one."""
    source = open(os.path.join(SRC_DIR, "helpers", "updater.py"), encoding="utf-8").read()
    return re.search(r'APP_VERSION\s*=\s*"([^"]+)"', source).group(1)


def _write_version_resource():
    """Give the executable a Windows version resource, and return the file
    describing it.

    Two reasons this is worth having. It is what fills in the Details tab
    of the file's Properties - without it Atomic.exe reports no version at
    all, which for something that updates itself is the one place a user
    would look to check what they have. And Explorer caches an
    executable's icon per file; a build that differs only in content has
    been known to keep showing the previous icon until that cache is
    rebuilt, where a changed version resource gives the shell a reason to
    treat it as a genuinely new file.
    """
    version = _app_version()
    parts = tuple(int(p) for p in version.split(".")) + (0, 0, 0, 0)
    numeric = parts[:4]
    text = f"""VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={numeric},
    prodvers={numeric},
    mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', 'Atomic'),
      StringStruct('FileDescription', 'Atomic'),
      StringStruct('FileVersion', '{version}'),
      StringStruct('InternalName', 'Atomic'),
      StringStruct('OriginalFilename', 'Atomic.exe'),
      StringStruct('ProductName', 'Atomic'),
      StringStruct('ProductVersion', '{version}')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])])
"""
    os.makedirs(BUILD_DIR, exist_ok=True)
    path = os.path.join(BUILD_DIR, "file_version_info.txt")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path


VERSION_FILE = _write_version_resource()

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
    # atomic_icon.png (the sidebar header logo) needs the same treatment,
    # and so does filter_icon.png (the tracker's filter button) - read at
    # runtime from the bundle root, so a build without it here shows a
    # button with no icon at all.
    datas=[(ICON_FILE, '.'), (LOGO_FILE, '.'), (FILTER_ICON_FILE, '.')],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # numpy is never imported by this app. Pillow's hook collects it
    # whenever it is installed, and PyInstaller's dependency analysis then
    # imports the collected packages in an isolated subprocess to resolve
    # their binary deps - where numpy 2.5.2 on this Python segfaults the
    # child (`import numpy.random` dies with no traceback, reproducibly),
    # taking the whole build down with it. Excluding it changes nothing in
    # the bundle: the released 1.9 exe carries zero numpy entries, because
    # the analysis discarded it as unused anyway.
    excludes=['numpy'],
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
    version=VERSION_FILE,
)
