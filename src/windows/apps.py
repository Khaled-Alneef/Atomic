"""Apps page: a customizable grid of local applications you launch often.

Adding them one at a time through a file picker is how this started, and
it is why the page stayed near-empty: Games has been able to read a
launcher's library since it existed, and every program on this machine
already writes itself into the Start menu. Import reads that (see
helpers.launchers.scan_start_menu).
"""

import threading

from PyQt6.QtCore import QObject
from PyQt6.QtCore import pyqtSignal as Signal

from helpers import launchers
from helpers.widgets import finish_toast, show_toast
from windows.link_grid import LinkGridPage


class _ImportSignals(QObject):
    # The scan reads a few hundred shortcuts off disk and extracts an
    # icon for each, so it runs off the UI thread; this carries the count
    # back onto it.
    done = Signal(int)


class AppsPage(LinkGridPage):
    DATA_FILE = "apps.json"
    TITLE = "Apps"
    DEFAULT_ENTRIES = []
    TARGET_KIND = "app"

    def __init__(self, app=None):
        self._import_signals = _ImportSignals()
        self._import_signals.done.connect(self._on_import_done)
        self._import_toast = None
        super().__init__(app)

    def _discovery_actions(self):
        return [("Import", "Add the programs in your Windows Start menu that "
                           "aren't on this page yet.", self._import_from_start_menu)]

    def _import_from_start_menu(self):
        if self._import_toast is not None:
            return  # already scanning - let it finish
        # Sticky toast rather than a dialog: this takes a couple of
        # seconds (an icon is extracted per shortcut) and the page is
        # still usable while it runs. Same shape as Games' own import.
        self._import_toast = show_toast(self, "Scanning...", duration_ms=None)
        threading.Thread(target=self._import_worker, daemon=True).start()

    def _import_worker(self):
        # Must never raise: this thread's only job is to report back, and
        # dying silently would leave "Scanning..." on screen forever.
        try:
            added = launchers.import_scanned_apps(launchers.scan_start_menu())
        except Exception:
            added = 0
        self._import_signals.done.emit(added)

    def _on_import_done(self, added):
        toast, self._import_toast = self._import_toast, None
        finish_toast(toast, self, launchers.import_app_result_message(added))
        if added:
            # Re-read from disk rather than trusting this page's copy:
            # the import writes through storage, and this page's list was
            # loaded when the page was built (the reasoning _mutate
            # records). No change of its own to apply - the reload is
            # the point.
            self._mutate(lambda _entries: None)
