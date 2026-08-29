"""Final development playback restore and build identity."""


def install():
    # This module is imported last by helpers/__init__.py. Restore the exact
    # 1.10.139 playback/startup mechanics only after every newer UI/runtime
    # patch has installed, so their unrelated fixes remain while their player
    # startup wrappers are removed.
    from . import regression_fixes_152
    regression_fixes_152.install()

    from . import updater
    updater.APP_VERSION = "1.10.152"
    try:
        updater._HEADERS["User-Agent"] = f"Atomic/{updater.APP_VERSION}"
    except Exception:
        pass
