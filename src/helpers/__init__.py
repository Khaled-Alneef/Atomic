"""Atomic helper package bootstrap."""

# Install the scroll-rendering patch before helpers.widgets is imported by
# any page. The hook patches widgets only after its normal module body has
# finished, so package import itself stays lightweight and non-GUI tools do
# not eagerly construct Qt widgets.
from . import motion_patch as _motion_patch

_motion_patch.install()
