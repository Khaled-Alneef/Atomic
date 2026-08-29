"""Final development playback restore and build identity."""


def install():
    # Restore the user-confirmed 1.10.139 playback/startup mechanics only after
    # every newer runtime patch has installed.
    from . import regression_fixes_152
    regression_fixes_152.install()

    # Older top-bar experiments still install earlier for compatibility with
    # existing code paths; 1.10.155 below removes them from the final player
    # class and restores the exact 1.10.139 bar implementation.
    from . import regression_fixes_153
    regression_fixes_153.install()

    # Preserve the chapter-row right-click forwarding fix.
    from . import regression_fixes_154
    regression_fixes_154.install()

    # Final 1.10.155 corrections: exact 1.10.139 upper bar, plus contiguous
    # chapter unread semantics (clicked chapter and every newer chapter).
    from . import regression_fixes_155
    regression_fixes_155.install()

    from . import updater
    # 1.10.163 removes the crashing custom QSG-node experiment and returns to
    # the proven 1.10.161 QQuickPaintedItem motion path. Home/Tracker alone use
    # a bounded, adaptively supersampled snapshot within the same old texture
    # budget so fractional 165/240 Hz movement has finer card/text samples.
    updater.APP_VERSION = "1.10.163"
    try:
        updater._HEADERS["User-Agent"] = f"Atomic/{updater.APP_VERSION}"
    except Exception:
        pass
