"""Resolve and use the real Segoe UI Variable text face at runtime.

Microsoft documents Segoe UI Variable as the Windows 11 UI family, but the
installed font exposes concrete optical-size families/faces such as Segoe UI
Variable Text, Display and Small. Qt can silently accept the generic
"Segoe UI Variable" alias and then resolve it back to classic Segoe UI, which
made the previous typography change visually indistinguishable on the owner's
machine.

Resolve the family only after QApplication exists, using QFontDatabase. Prefer
the Text optical family for Atomic's normal UI sizes, then the generic variable
family, then any installed Segoe UI Variable family. Classic Segoe UI remains
the safe fallback. Programmatic text fonts use vertical-only hinting. Icon and
emoji families are preserved.
"""

from __future__ import annotations

_INSTALLED = False
_SELECTED_TEXT_FAMILY = "Segoe UI"

# Prefer the text optical cut for normal UI labels/card titles. Microsoft ships
# Display and Small in the same variable font file; they remain fallbacks if a
# particular Windows/Qt build exposes only those concrete family names.
_VARIABLE_CANDIDATES = (
    "Segoe UI Variable Text",
    "Segoe UI Variable",
    "Segoe UI Variable Display",
    "Segoe UI Variable Small",
)


def install():
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    from PyQt6.QtGui import QFont, QFontDatabase, QFontInfo
    from PyQt6.QtWidgets import QApplication
    from . import theme, updater

    old_font = theme.font
    original_apply_theme = theme.apply_theme
    base_sheet = theme.STYLESHEET

    def _resolve_text_family():
        """Return an actually installed variable family, not an alias guess."""
        global _SELECTED_TEXT_FAMILY
        if QApplication.instance() is None:
            return _SELECTED_TEXT_FAMILY
        try:
            families = tuple(QFontDatabase.families())
            folded = {name.casefold(): name for name in families}

            candidate = None
            for wanted in _VARIABLE_CANDIDATES:
                if wanted.casefold() in folded:
                    candidate = folded[wanted.casefold()]
                    break

            if candidate is None:
                # Some Qt/DirectWrite versions expose a style-qualified family
                # name. Prefer Text, then any Segoe UI Variable entry.
                variable = [name for name in families
                            if name.casefold().startswith("segoe ui variable")]
                text = [name for name in variable if " text" in name.casefold()]
                candidate = (text or variable or [None])[0]

            if candidate:
                # QFontInfo reports what Qt really resolved for screen drawing.
                # If the requested variable family collapses to classic Segoe
                # UI, do not pretend the switch succeeded.
                probe = QFont(candidate, 10)
                resolved = QFontInfo(probe).family()
                if ("segoe ui variable" in candidate.casefold()
                        and "segoe ui variable" in resolved.casefold()):
                    _SELECTED_TEXT_FAMILY = candidate
                elif candidate in families:
                    # QFontInfo can report the foundry-normalized family on a
                    # few Windows builds. An exact QFontDatabase family is still
                    # a stronger signal than the generic alias used before.
                    _SELECTED_TEXT_FAMILY = candidate
        except Exception:
            _SELECTED_TEXT_FAMILY = "Segoe UI"
        return _SELECTED_TEXT_FAMILY

    def _is_text_family(name):
        value = (name or "").casefold()
        return (not value
                or value in {"segoe ui", "segoe ui semibold", "bahnschrift"}
                or value.startswith("segoe ui variable"))

    def smooth_font(size=10, weight=QFont.Weight.Normal, family=None, fallbacks=()):
        selected = _resolve_text_family()
        chosen = selected if _is_text_family(family) else family

        mapped = []
        for name in fallbacks or ():
            replacement = selected if _is_text_family(name) else name
            if replacement not in mapped:
                mapped.append(replacement)
        if chosen == selected and "Segoe UI" not in mapped:
            mapped.append("Segoe UI")

        result = old_font(size, weight, chosen, fallbacks=tuple(mapped))
        if _is_text_family(chosen):
            result.setHintingPreference(QFont.HintingPreference.PreferVerticalHinting)
        return result

    def _sheet_for(selected):
        # The base stylesheet is created before this patch installs. Rewrite
        # only font stacks; every colour/metric/motion-related rule is retained.
        sheet = base_sheet
        sheet = sheet.replace(
            'font-family: "Segoe UI";',
            f'font-family: "{selected}";',
        )
        sheet = sheet.replace(
            '"Bahnschrift", "Segoe UI Semibold", "Segoe UI"',
            f'"{selected}", "Segoe UI"',
        )
        sheet = sheet.replace(
            '"Segoe Fluent Icons", "Segoe MDL2 Assets", "Segoe UI"',
            f'"Segoe Fluent Icons", "Segoe MDL2 Assets", "{selected}", "Segoe UI"',
        )
        return sheet

    def apply_theme_with_resolved_variable(app):
        selected = _resolve_text_family()

        # Keep public tokens coherent for all widgets/delegates created after
        # the application theme is installed.
        theme.FONT_FAMILY = selected
        theme.FONT_FAMILY_NAV = selected
        theme.FONT_FAMILY_NAV_FALLBACKS = (selected, "Segoe UI")
        theme.FONT_STACK_NAV = f'"{selected}", "Segoe UI"'
        theme.FONT_FAMILY_ICON_FALLBACKS = (
            theme.FONT_FAMILY_ICONS,
            "Segoe MDL2 Assets",
            selected,
            "Segoe UI",
        )
        theme.FONT_STACK_ICONS = ", ".join(
            f'"{name}"' for name in theme.FONT_FAMILY_ICON_FALLBACKS
        )
        theme.STYLESHEET = _sheet_for(selected)

        # Preserve theme.apply_theme's ordering while ensuring the QFont used as
        # the app default carries PreferVerticalHinting.
        app.setStyleSheet(theme.STYLESHEET)
        app.setFont(smooth_font(10))
        return app

    theme.font = smooth_font
    theme.apply_theme = apply_theme_with_resolved_variable

    # Leave the old function reachable only for debugging/introspection; normal
    # startup calls theme.apply_theme and therefore uses the runtime resolver.
    theme._pre_variable_apply_theme = original_apply_theme

    # Development build identity.
    updater.APP_VERSION = "1.10.116"
    updater._HEADERS["User-Agent"] = f"Atomic/{updater.APP_VERSION}"
