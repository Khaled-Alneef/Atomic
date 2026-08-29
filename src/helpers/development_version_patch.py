"""Final development playback restore and build identity."""


def _install_sharp_watched_episode_stills():
    """Keep DONE episode thumbnails sharp even when spoiler blur is enabled.

    The episode list already knows whether each row is watched before it builds
    the still tile: its badge is ("watched", "DONE").  The ordinary setting is
    still respected for every unwatched episode.  This small import-time patch
    only changes the value seen by DetailsPage._still_tile while one DONE row
    is being constructed, so no image cache or downloader path is duplicated.
    """
    import importlib.abc
    import importlib.machinery
    import sys

    target = "windows.details"

    def patch(module):
        cls = getattr(module, "DetailsPage", None)
        if cls is None or getattr(cls, "_atomic_sharp_watched_stills", False):
            return
        old_row_card = cls._row_card

        def row_card(self, title, date_text, badge, on_click, on_menu=None,
                     variant="chapter", still_url=None):
            watched = (variant == "episode"
                       and isinstance(badge, (tuple, list))
                       and bool(badge)
                       and str(badge[0]).lower() == "watched")
            if not watched:
                return old_row_card(
                    self, title, date_text, badge, on_click,
                    on_menu=on_menu, variant=variant, still_url=still_url)

            previous = getattr(self, "_blur_stills", False)
            self._blur_stills = False
            try:
                return old_row_card(
                    self, title, date_text, badge, on_click,
                    on_menu=on_menu, variant=variant, still_url=still_url)
            finally:
                self._blur_stills = previous

        cls._row_card = row_card
        cls._atomic_sharp_watched_stills = True

    module = sys.modules.get(target)
    if module is not None:
        patch(module)
        return

    class _Loader(importlib.abc.Loader):
        def __init__(self, wrapped):
            self._wrapped = wrapped

        def create_module(self, spec):
            creator = getattr(self._wrapped, "create_module", None)
            return creator(spec) if creator is not None else None

        def exec_module(self, module):
            self._wrapped.exec_module(module)
            patch(module)

    class _Finder(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path, target_module=None):
            if fullname != target:
                return None
            spec = importlib.machinery.PathFinder.find_spec(fullname, path)
            if spec is None or spec.loader is None:
                return spec
            spec.loader = _Loader(spec.loader)
            return spec

    sys.meta_path.insert(0, _Finder())


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

    # Watched episodes are no longer spoiler-blurred. Unwatched episode stills
    # continue to follow Settings > Watching > blur episode stills.
    _install_sharp_watched_episode_stills()

    from . import updater
    # 1.10.181 keeps the exact 1.10.163 motion constants and compositor, but
    # Home/Tracker advance that motion from actual QQuickWindow frame swaps --
    # the same presentation ownership used by the clean Movies/Anime grids.
    updater.APP_VERSION = "1.10.181"
    try:
        updater._HEADERS["User-Agent"] = f"Atomic/{updater.APP_VERSION}"
    except Exception:
        pass
