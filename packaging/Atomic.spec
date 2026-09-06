# -*- mode: python ; coding: utf-8 -*-

import os
import re

# SPECPATH (injected by PyInstaller) is this file's own directory
# (packaging/) - resolve everything from there so the build works
# regardless of the working directory it's invoked from.
SRC_DIR = os.path.join(SPECPATH, "..", "src")
# Everything the app ships as an image now lives under src/assets/,
# with the nav icons in src/assets/icons/ - SVG since 25 August 2026,
# which is also when the 1.2MB Icons.png sheet the previous PNG set was
# cut from went away. The README.txt that sat beside them is gone as of
# 26 August 2026 - it was never bundled (nothing reads it at runtime)
# and the owner asked for it out of the tree.
ASSETS_DIR = os.path.join(SRC_DIR, "assets")
ICONS_DIR = os.path.join(ASSETS_DIR, "icons")
ICON_FILE = os.path.join(ASSETS_DIR, "app_icon.ico")
LOGO_FILE = os.path.join(ASSETS_DIR, "atomic_icon.png")
FILTER_ICON_FILE = os.path.join(ASSETS_DIR, "filter_icon.png")
BUILD_DIR = os.path.join(SPECPATH, "build")
# The video player's decode engine. Not in the repository (see
# fetch_libmpv.py for why); the build stops here rather than producing an
# exe whose player silently cannot open anything.
# Home and Discover render in a WebView2 hosted inside the Qt window
# (helpers/webview2_host.py). Three things travel with the exe for that:
# the pages, Edge's .NET assemblies, and the native loader. Vendored
# rather than read out of site-packages because build.py resolves every
# datas entry with `ast` and cannot follow a call into an installed
# package - and a build that depends on what happens to be pip-installed
# breaks on the next machine.
WEB_DIR = os.path.join(SRC_DIR, "web", "static")
WEBVIEW_LIB = os.path.join(SPECPATH, "..", "vendor", "webview2")

LIBMPV_FILE = os.path.join(SPECPATH, "..", "vendor", "libmpv-2.dll")
if not os.path.isfile(LIBMPV_FILE):
    raise SystemExit(
        "vendor/libmpv-2.dll is missing - the video player cannot decode "
        "without it.\nRun: python packaging/fetch_libmpv.py")

# The owner's TMDB read token, bundled so nobody using Atomic needs a key
# of their own - ever (the owner's standing requirement).
#
# **It is committed to the repository now** (see .gitignore, which
# records why). It was gitignored, with an encrypted copy beside it for
# moving between machines, and that failed the first time it mattered:
# a second machine could not build, the passphrase was not to hand, and
# no released exe carried the token to recover from - v1.10 predates the
# bundling by two days. So this check should now only ever fire on a
# tree where the file was deleted by hand.
#
# The build stops rather than continuing without it, exactly like the
# libmpv check above: a tokenless exe is one that asks users for a TMDB
# key, which is the outcome this whole arrangement exists to prevent. It
# used to succeed with artwork silently dark.
TMDB_TOKEN_FILE = os.path.join(SPECPATH, "tmdb_token.txt")
if not os.path.isfile(TMDB_TOKEN_FILE):
    raise SystemExit(
        "packaging/tmdb_token.txt is missing - the exe would ship without "
        "the TMDB token and users would need their own key.\nIt is "
        "committed to the repository, so this usually means it was "
        "deleted locally:\n  git checkout -- packaging/tmdb_token.txt\n"
        "The older encrypted copy is still there as a fallback:\n"
        "  openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 "
        "-in packaging/tmdb_token.txt.enc -out packaging/tmdb_token.txt")



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
    datas=[(ICON_FILE, 'assets'), (LOGO_FILE, 'assets'),
           (FILTER_ICON_FILE, 'assets'),
           # The nav icons, listed one by one rather than globbed:
           # build.py reads this list with `ast` and byte-compares every
           # entry against the file on disk, and a comprehension it
           # cannot resolve makes it reject the build outright. Verbose
           # on purpose - the spec's own note above records that a
           # missing asset ships a button with no icon and says nothing.
           #
           # **SVG since 25 August 2026** (the owner's icon pack, which
           # replaced the cut PNG sheet): rendered at the device size by
           # images._rendered_svg instead of being resampled from one
           # cut, so a folded row at 29px on a 150% display is as sharp
           # as an expanded one at 26px on a 100% one. All nineteen are
           # listed, including the two no rail row maps yet (search,
           # settings) - a file in the pack that is not in the bundle is
           # how the next row added here would ship blank.
           (os.path.join(ICONS_DIR, "addons.svg"), 'assets/icons'),
           (os.path.join(ICONS_DIR, "anime.svg"), 'assets/icons'),
           (os.path.join(ICONS_DIR, "apps.svg"), 'assets/icons'),
           (os.path.join(ICONS_DIR, "calendar.svg"), 'assets/icons'),
           (os.path.join(ICONS_DIR, "discover.svg"), 'assets/icons'),
           (os.path.join(ICONS_DIR, "games.svg"), 'assets/icons'),
           (os.path.join(ICONS_DIR, "history.svg"), 'assets/icons'),
           (os.path.join(ICONS_DIR, "home.svg"), 'assets/icons'),
           (os.path.join(ICONS_DIR, "library.svg"), 'assets/icons'),
           (os.path.join(ICONS_DIR, "live-tv.svg"), 'assets/icons'),
           (os.path.join(ICONS_DIR, "manga.svg"), 'assets/icons'),
           (os.path.join(ICONS_DIR, "manhua.svg"), 'assets/icons'),
           (os.path.join(ICONS_DIR, "manhwa.svg"), 'assets/icons'),
           (os.path.join(ICONS_DIR, "movies.svg"), 'assets/icons'),
           (os.path.join(ICONS_DIR, "saved.svg"), 'assets/icons'),
           (os.path.join(ICONS_DIR, "search.svg"), 'assets/icons'),
           (os.path.join(ICONS_DIR, "schedule.svg"), 'assets/icons'),
           (os.path.join(ICONS_DIR, "settings.svg"), 'assets/icons'),
           (os.path.join(ICONS_DIR, "shows.svg"), 'assets/icons'),
           (os.path.join(ICONS_DIR, "websites.svg"), 'assets/icons'),
           # libmpv goes in as data, not as a `binaries` entry: it lands
           # at the bundle root either way, and PyInstaller does not need
           # to walk its dependency tree - it has none outside the
           # system, and analysing a 120MB DLL costs build time for an
           # answer that is always empty. helpers/video_backend.py looks
           # for it in sys._MEIPASS, which is exactly here.
           (LIBMPV_FILE, '.'),
           # In the literal list, not appended to a.datas afterwards (the
           # old shape, from when the file was optional): build.py's
           # bundle check verifies exactly this list, so being in it is
           # what makes a stale or missing token a rejected build rather
           # than a quietly artwork-less exe.
           (TMDB_TOKEN_FILE, '.'),
           (os.path.join(WEB_DIR, "index.html"), 'static'),
           (os.path.join(WEB_DIR, "app.css"), 'static'),
           (os.path.join(WEB_DIR, "app.js"), 'static'),
           (os.path.join(WEBVIEW_LIB, "Microsoft.Web.WebView2.Core.dll"),
            'webview/lib'),
           (os.path.join(WEBVIEW_LIB, "Microsoft.Web.WebView2.WinForms.dll"),
            'webview/lib'),
           (os.path.join(WEBVIEW_LIB, "WebView2Loader.dll"),
            'webview/lib'),
           # And at the bundle root as well. WebView2Loader.dll is a
           # *native* DLL the .NET assembly loads by name, so it has to
           # be on the search path - which is the unpacked bundle root,
           # not the folder the managed assemblies were put in.
           (os.path.join(WEBVIEW_LIB, "WebView2Loader.dll"), '.')],
    # python-mpv is loaded by helpers/video_backend.py only after the DLL
    # directory is registered, so the import is inside a function and
    # PyInstaller's static analysis never sees it. libtorrent is imported
    # inside a try/except in helpers/torrent_engine.py for the same
    # reason - the analysis skips guarded imports, and without it named
    # here the exe ships with no torrent engine and every torrent falls
    # back to needing Stremio installed.
    # PyQt6.QtSvg for the same reason as the two above: helpers/images.py
    # imports it inside a try/except so a machine without it degrades to
    # the bullet fallback instead of failing to start, and a guarded
    # import is precisely what PyInstaller's static analysis skips.
    # Without it named here every rail icon in the frozen exe renders as
    # nothing - a null pixmap, silently (25 August 2026).
    hiddenimports=['mpv', 'libtorrent', 'PyQt6.QtSvg',
                   # Reached only from inside a function, so the
                   # analysis never sees them.
                   'clr', 'clr_loader', 'clr_loader.netfx', 'webview',
                   'web', 'web.server', 'web.backend',
                   'windows.web_pages', 'windows.web_reader',
                   'helpers.webview2_host',
                   # The public CA roots, imported inside
                   # helpers/net.ssl_context. Named here so PyInstaller's
                   # certifi hook runs and bundles cacert.pem: without it
                   # the app trusts only the Windows root store, which
                   # ships small and fills in on demand for Windows' own
                   # TLS stack and never for Python's. That is why the
                   # owner's second machine could reach every other host
                   # and failed api.themoviedb.org with
                   # SSLCertVerificationError - its store had no Amazon
                   # Root CA 1. See net.ssl_context for the measurement.
                   'certifi'],
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
    # **QtWebEngine is not shipped** (6 September 2026). Measured on the
    # 1.10.268 archive: 539MB unpacked, 507MB of it written to %TEMP% by
    # the onefile bootloader on every launch in 2.88s, before the first
    # line of app code runs - and Qt6WebEngineCore.dll alone is 203MB of
    # that, with icudtl.dat, the resource .pak files and
    # QtWebEngineProcess.exe behind it. Every page on screen has been
    # WebView2 since 31 August (windows/web_pages), helpers/__init__
    # imports the engine inside a try that already says "no web engine
    # in this build - web_grid falls back", and the Qt pages keep their
    # painted grids for a machine without the WebView2 runtime. The
    # owner's ask that day: "improve app launching time".
    excludes=['numpy', 'PyQt6.QtWebEngineCore', 'PyQt6.QtWebEngineWidgets',
              'PyQt6.QtWebEngineQuick'],
    noarchive=False,
    optimize=0,
)

# The owner's Real-Debrid token, bundled **only when the file exists** -
# the deliberate opposite of the TMDB check above, in both directions:
#
#   * It is appended to a.datas here rather than listed in datas=[...],
#     because build.py reads that literal list with `ast` and rejects a
#     build over any entry it cannot resolve to an existing file - which
#     would turn "no rd_token.txt" into a failed build. A tokenless
#     build is a *working app* here (episodes play from the swarm, the
#     player never knew debrid existed), so the file must stay optional.
#     The cost accepted: build.py's byte-compare does not verify this
#     one entry, since it only checks the literal list.
#   * It is gitignored, not committed like the TMDB one: that is a free
#     read-only metadata key, this is a paid account credential on a
#     public repository. It reaches users inside the exe, never through
#     the repo - the owner places packaging/rd_token.txt by hand on the
#     build machine (helpers/debrid._bundled_token reads it out of
#     sys._MEIPASS, and a pasted Settings key always overrides it).
RD_TOKEN_FILE = os.path.join(SPECPATH, "rd_token.txt")
if os.path.isfile(RD_TOKEN_FILE):
    a.datas += [("rd_token.txt", RD_TOKEN_FILE, "DATA")]

# Two payloads PyInstaller collects that nothing in this app can reach,
# dropped after the analysis rather than through `excludes=`: both are
# *binaries* pulled in by their package's hook, and excludes= only
# reaches Python imports.
#
# Measured 26 August 2026 against the 1.10.46 archive - 244.5MB
# uncompressed / 99.2MB compressed, all of which the onefile bootloader
# inflates into %TEMP% before any app code runs (1.85s and 1.86s to a
# visible window here, against 0.90s and 1.18s for the same build as
# onedir - so the unpack is about half of startup on a fast disk, and
# more than that on the slow one somebody downloads to):
#
#   PIL/_avif.cp313-win_amd64.pyd       7.89MB raw / 4.32MB zipped
#   PyQt6/Qt6/bin/Qt6Pdf.dll            4.61MB raw / 2.46MB zipped
#   PyQt6/.../imageformats/qpdf.dll     0.04MB raw
#
# 12.5MB off every launch's unpack and 6.8MB off every download, for two
# formats grep finds no mention of anywhere in src/. Pillow's hook
# collects _avif because the wheel ships it; Qt6Pdf arrives with PyQt6
# whether or not QtPdf is ever imported.
#
# opengl32sw.dll (20.64MB, the largest single candidate) is deliberately
# **not** here. It is Qt's software OpenGL fallback, so the machine that
# needs it is one whose drivers cannot give Qt a GL context - exactly the
# machine this cannot be tested on. 20MB is not worth a blank window
# somebody else gets.
_DEAD_WEIGHT = ("/pil/_avif", "/qt6pdf.dll", "/imageformats/qpdf.dll",
                "/qt6webengine", "/qtwebengine", "/qt6/resources/icudtl.dat")
a.binaries = [entry for entry in a.binaries
              if not any(dead in "/" + entry[0].lower().replace("\\", "/")
                         for dead in _DEAD_WEIGHT)]

# **QtWebEngine's developer tools, and Qt's translations.** Measured
# 31 August 2026 against this build - 248.5MB, of which QtWebEngine is
# 120.9MB, 49% of the whole executable:
#
#   qtwebengine_devtools_resources.debug.pak   13.8MB
#   qtwebengine_devtools_resources.pak         11.1MB
#   PyQt6/Qt6/translations/*                    2.4MB
#
# The devtools are the F12 inspector's own UI. Nothing in this app can
# open it - web_grid sets no devtools shortcut and the page it serves is
# generated here - and the *debug* copy is not shipped by browsers at
# all. The translations are Qt's own strings in forty languages for an
# app whose every string is English.
#
# Datas rather than binaries: these arrive through PyQt6's hook as data
# files, which `excludes=` cannot reach either.
_DEAD_DATA = ("qtwebengine", "/qt6/translations/", "/qt6/resources/icudtl.dat")
a.datas = [entry for entry in a.datas
           if not any(dead in "/" + entry[0].lower().replace("\\", "/")
                      for dead in _DEAD_DATA)]

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
    # libmpv is left uncompressed on purpose. UPX has to unpack a DLL in
    # memory before it can be loaded, and on a 120MB library that is both
    # the slowest thing in startup and the one PyInstaller/UPX pairing
    # with a history of producing a binary that loads everywhere except
    # the machine you need it on. The exe is larger; it works.
    upx_exclude=['libmpv-2.dll'],
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
