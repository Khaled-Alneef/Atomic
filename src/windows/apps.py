"""Apps page: a customizable grid of local applications you launch often."""

from windows.link_grid import LinkGridPage


class AppsPage(LinkGridPage):
    DATA_FILE = "apps.json"
    TITLE = "Apps"
    DEFAULT_ENTRIES = []
    TARGET_KIND = "app"
    # Poster-size tiles with store artwork (the owner's ask) - see
    # LinkGridPage.POSTER_CARDS and helpers/app_art.
    POSTER_CARDS = True
