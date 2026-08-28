"""Atomic helper package bootstrap."""

# PyQt6 exposes QRegion from QtGui, not QtCore. motion_patch is installed
# before helpers.widgets is imported, so provide the correct QtGui class on
# QtCore as a narrow compatibility alias for the patch's deferred import.
# This keeps startup working in both source runs and the PyInstaller build.
from PyQt6 import QtCore
from PyQt6.QtGui import QRegion

QtCore.QRegion = QRegion

# Install the launch-DPI bootstrap before main.py constructs its first page.
# main.py still adopts the QWindow's exact screen ratio as soon as the window
# exists; this only prevents first-page covers being cut for the wrong monitor.
from . import startup_dpr_patch as _startup_dpr_patch

_startup_dpr_patch.install()

# Install the scroll-rendering patches before their target modules are imported
# by any page. The hooks patch each module only after its normal module body has
# finished, so package import itself does not construct widgets.
from . import motion_patch as _motion_patch
from . import poster_grid_motion_patch as _poster_grid_motion_patch

_motion_patch.install()
_poster_grid_motion_patch.install()
