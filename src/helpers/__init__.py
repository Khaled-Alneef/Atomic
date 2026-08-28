"""Atomic helper package bootstrap."""

# PyQt6 exposes QRegion from QtGui, not QtCore. motion_patch is installed
# before helpers.widgets is imported, so provide the correct QtGui class on
# QtCore as a narrow compatibility alias for the patch's deferred import.
# This keeps startup working in both source runs and the PyInstaller build.
from PyQt6 import QtCore
from PyQt6.QtGui import QRegion

QtCore.QRegion = QRegion

# Install the scroll-rendering patch before helpers.widgets is imported by
# any page. The hook patches widgets only after its normal module body has
# finished, so package import itself stays lightweight and non-GUI tools do
# not eagerly construct Qt widgets.
from . import motion_patch as _motion_patch
from . import poster_grid_motion_patch as _poster_grid_motion_patch

_motion_patch.install()
_poster_grid_motion_patch.install()
