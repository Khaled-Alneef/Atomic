"""Atomic helper package bootstrap."""

# PyQt6 exposes QRegion from QtGui, not QtCore. motion_patch is installed
# before helpers.widgets is imported, so provide the correct QtGui class on
# QtCore as a narrow compatibility alias for the patch's deferred import.
from PyQt6 import QtCore
from PyQt6.QtGui import QRegion

QtCore.QRegion = QRegion

# Keep the authoritative first-page DPR rebuild: this is the fix that made
# first-launch card artwork sharp on the 1080p monitor.
from . import startup_dpr_patch as _startup_dpr_patch

_startup_dpr_patch.install()

# The native-refresh Qt Quick compositor remains the base motion path.
from . import motion_patch as _motion_patch

_motion_patch.install()

# 240 Hz needs presentation pacing that does not turn callback jitter into
# uneven movement. This patch is inert below 200 Hz, so the proven 165 Hz path
# stays byte-for-byte equivalent at runtime.
from . import highhz_pacing_patch as _highhz_pacing_patch

_highhz_pacing_patch.install()
