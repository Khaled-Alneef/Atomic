"""Final development build identity after all runtime patches install."""


def install():
    from . import updater
    updater.APP_VERSION = "1.10.151"
    try:
        updater._HEADERS["User-Agent"] = f"Atomic/{updater.APP_VERSION}"
    except Exception:
        pass