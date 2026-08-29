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

    # 1.10.159 keeps Home/Tracker on one live QWidget surface (the snapshot
    # compositor stays blocked there), but distributes their unavoidable whole-
    # pixel scrollbar commits from the floating momentum residual instead of
    # absolute round(position). This is scoped to >=150 Hz only.
    from . import live_scroll_quantizer_patch
    live_scroll_quantizer_patch.install()

    from . import updater
    updater.APP_VERSION = "1.10.159"
    try:
        updater._HEADERS["User-Agent"] = f"Atomic/{updater.APP_VERSION}"
    except Exception:
        pass
