"""Websites page: a customizable grid of streaming sites and other
websites you visit often."""

from windows.link_grid import LinkGridPage


class WebsitesPage(LinkGridPage):
    DATA_FILE = "websites.json"
    TITLE = "Websites"
    DEFAULT_ENTRIES = []
    TARGET_KIND = "site"
