"""Atomic helper package bootstrap."""

# PyQt6 exposes QRegion from QtGui, not QtCore. motion_patch is installed
# before helpers.widgets is imported, so provide the correct QtGui class on
# QtCore as a narrow compatibility alias for the patch's deferred import.
from PyQt6 import QtCore
from PyQt6.QtGui import QRegion

QtCore.QRegion = QRegion

# Keep the authoritative first-page DPR rebuild: this is the fix confirmed to
# make first-launch card artwork sharp on the 1080p monitor.
from . import startup_dpr_patch as _startup_dpr_patch

_startup_dpr_patch.install()

# Keep the last compositor configuration that was explicitly measured by the
# owner as fully clean on the 165 Hz display.  Do not layer timing/alignment
# experiments over it; 240 Hz work must be isolated from this baseline.
from . import motion_patch as _motion_patch

_motion_patch.install()
