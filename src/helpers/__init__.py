"""Atomic helper package bootstrap."""

# PyQt6 exposes QRegion from QtGui, not QtCore. motion_patch is installed
# before helpers.widgets is imported, so provide the correct QtGui class on
# QtCore as a narrow compatibility alias for the patch's deferred import.
# This keeps startup working in both source runs and the PyInstaller build.
from PyQt6 import QtCore
from PyQt6.QtGui import QRegion

QtCore.QRegion = QRegion

# Establish the real first-page DPR as soon as the finished main window reports
# it. The patch also keeps a harmless early bootstrap for image work performed
# while MainWindow is still being constructed.
from . import startup_dpr_patch as _startup_dpr_patch

_startup_dpr_patch.install()

# Install the scroll-rendering patches before their target modules are imported
# by any page. highhz_visual_patch deliberately installs after motion_patch: its
# loader applies the base Quick compositor first, then aligns only the >=200 Hz
# raster transform to physical device pixels.
from . import motion_patch as _motion_patch
from . import highhz_visual_patch as _highhz_visual_patch
from . import poster_grid_motion_patch as _poster_grid_motion_patch

_motion_patch.install()
_highhz_visual_patch.install()
_poster_grid_motion_patch.install()
