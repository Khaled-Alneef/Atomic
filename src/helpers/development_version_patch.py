"""Final development playback restore and build identity."""


def install():
    # Restore the user-confirmed 1.10.139 playback/startup mechanics only after
    # every newer runtime patch has installed.
    from . import regression_fixes_152
    regression_fixes_152.install()

    # Apply the top-bar input/z-order reliability fix after the playback restore
    # so this changes chrome interaction only, not startup/buffering/resume.
    from . import regression_fixes_153
    regression_fixes_153.install()

    # Make right-click anywhere inside a chapter row reach the existing reading
    # context menu, including clicks landing on child buttons/labels/badges.
    from . import regression_fixes_154
    regression_fixes_154.install()

    from . import updater
    updater.APP_VERSION = "1.10.154"
    try:
        updater._HEADERS["User-Agent"] = f"Atomic/{updater.APP_VERSION}"
    except Exception:
        pass
