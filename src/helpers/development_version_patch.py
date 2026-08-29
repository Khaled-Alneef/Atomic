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

    from . import updater
    updater.APP_VERSION = "1.10.153"
    try:
        updater._HEADERS["User-Agent"] = f"Atomic/{updater.APP_VERSION}"
    except Exception:
        pass
