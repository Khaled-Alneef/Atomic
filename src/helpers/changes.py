"""What the user just changed, for every page that is looking.

**The owner's rule, 6 September 2026:** *"make any changes the user make
like un-saving the watch or read from the main page, make the effect
appear immediately in the main page no need to switch pages to
refresh!!!! make this a rule and implement it in the whole app!"*

A web page is a document fetched once (windows/web_pages), and until
now it noticed a change only by the mtime of a file it watched - which
could not be series.json or tracker.json, because the tracker's
background lookups write bookkeeping onto entries every few seconds
and every move redrew the page (the note above _WATCHED_FILES records
what that looked like). So a *user's* write went unseen until the next
page switch.

This is the other channel: every write the user makes - a save, a
removal, a status, a mark, a history edit - bumps a counter, and each
page's 150ms tick (web_pages._check_covered) compares it with the last
value it acted on. A page showing lists redraws; a catalogue grid
patches its numbers and saved marks in place; a page behind an overlay
redraws the moment it is uncovered. The background lookups never touch
this counter, which is the whole reason it is separate from the files.

`bump()` is called from the write itself (details.save_to_library,
remove_from_library, history.set_watched/forget/clear,
tracker._write_progress, the status menu), not from the surface that
asked for it, so every surface - a banner, a card menu, the details
page, the player - is covered by construction.
"""

import threading

_lock = threading.Lock()
_version = 0


def bump():
    """Note that the user changed something. Thread-safe; never raises."""
    global _version
    with _lock:
        _version += 1
        return _version


def version() -> int:
    """The current change count; equal values mean nothing happened."""
    return _version
