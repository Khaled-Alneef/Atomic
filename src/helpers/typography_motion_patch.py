"""Use Segoe UI Variable + vertical hinting for moving UI text.

Atomic's normal text already used Segoe UI, but the sidebar used Bahnschrift and
Qt's Windows font engine was left to its default hinting preference. For the
high-refresh motion path, use Windows' variable UI face consistently and keep
vertical stem alignment while avoiding the harsher full horizontal grid-fit.

Icon faces are not replaced: Segoe Fluent Icons / Segoe MDL2 Assets stay at the
front of their existing fallback chains. Segoe UI Emoji is also untouched.
"""

from __future__ import annotations

_INSTALLED = False
_TEXT_FAMILY = "Segoe UI Variable"
_TEXT_FALLBACKS = (_TEXT_FAMILY, "Segoe UI")


def install():
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    from PyQt6.QtGui import QFont
    from . import theme, updater

    old_font = theme.font

    # Keep the public theme tokens coherent so helpers created after bootstrap
    # (including the sidebar delegate) request the same face as the stylesheet.
    theme.FONT_FAMILY = _TEXT_FAMILY
    theme.FONT_FAMILY_NAV = _TEXT_FAMILY
    theme.FONT_FAMILY_NAV_FALLBACKS = _TEXT_FALLBACKS
    theme.FONT_STACK_NAV = ", ".join(f'"{name}"' for name in _TEXT_FALLBACKS)
    theme.FONT_FAMILY_ICON_FALLBACKS = (
        theme.FONT_FAMILY_ICONS,
        "Segoe MDL2 Assets",
        _TEXT_FAMILY,
        "Segoe UI",
    )
    theme.FONT_STACK_ICONS = ", ".join(
        f'"{name}"' for name in theme.FONT_FAMILY_ICON_FALLBACKS
    )

    def _mapped_family(name):
        if name in (None, "Segoe UI", "Segoe UI Semibold", "Bahnschrift"):
            return _TEXT_FAMILY
        return name

    def _mapped_fallbacks(values):
        mapped = []
        for name in values or ():
            replacement = _mapped_family(name)
            if replacement not in mapped:
                mapped.append(replacement)
        if "Segoe UI" in values and "Segoe UI" not in mapped:
            mapped.append("Segoe UI")
        return tuple(mapped)

    def smooth_font(size=10, weight=QFont.Weight.Normal, family=None, fallbacks=()):
        chosen = _mapped_family(family)
        mapped_fallbacks = _mapped_fallbacks(fallbacks)
        # If a caller did not provide its own fallback chain, give ordinary
        # text the stable Windows fallback while leaving icon/emoji faces alone.
        if not mapped_fallbacks and chosen == _TEXT_FAMILY:
            mapped_fallbacks = _TEXT_FALLBACKS
        result = old_font(size, weight, chosen, fallbacks=mapped_fallbacks)
        result.setHintingPreference(QFont.HintingPreference.PreferVerticalHinting)
        return result

    theme.font = smooth_font

    # STYLESHEET is materialized when theme.py imports, before these token
    # assignments. Rewrite only its three known font stacks; colours, sizing,
    # spacing and all non-typography styling remain byte-for-byte unchanged.
    sheet = theme.STYLESHEET
    sheet = sheet.replace(
        'font-family: "Segoe UI";',
        'font-family: "Segoe UI Variable";',
    )
    sheet = sheet.replace(
        '"Bahnschrift", "Segoe UI Semibold", "Segoe UI"',
        '"Segoe UI Variable", "Segoe UI"',
    )
    sheet = sheet.replace(
        '"Segoe Fluent Icons", "Segoe MDL2 Assets", "Segoe UI"',
        '"Segoe Fluent Icons", "Segoe MDL2 Assets", "Segoe UI Variable", "Segoe UI"',
    )
    theme.STYLESHEET = sheet

    # Development build identity. The source updater file still carries the
    # release-line base number; bootstrap patches advance the active dev build.
    updater.APP_VERSION = "1.10.115"
    updater._HEADERS["User-Agent"] = f"Atomic/{updater.APP_VERSION}"
