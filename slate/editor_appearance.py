"""Resolve native editor themes and optional colors shared by settings and diff views."""

import json
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("GtkSource", "4")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Gdk, Gtk, GtkSource, PangoCairo


def monospaced_font_families() -> list[str]:
    """List installed fixed-width Pango families without offering proportional fonts."""

    return sorted({family.get_name() for family in PangoCairo.FontMap.get_default().list_families()
                   if family.is_monospace() and family.get_name().casefold() != "monospace"}, key=str.casefold)


def editor_font_css(preferences: dict, points: int) -> bytes:
    """Resolve an installed monospaced family and safely build the shared editor font rule."""

    # 2026-10-30: una preferenza trasferita da un altro sistema può indicare
    # un font assente o proporzionale. Risolvere solo famiglie mono presenti,
    # conservando la preferenza nel JSON e usando monospace come fallback.
    requested = preferences.get("font_family", "monospace")
    family = "monospace"
    # 2026-11-07: il font generico non richiede di enumerare i font installati
    # per ogni documento o variazione della dimensione.
    if isinstance(requested, str) and requested.casefold() != "monospace":
        for installed in monospaced_font_families():
            if installed.casefold() == requested.casefold():
                family = installed
                break
    css_family = "monospace" if family == "monospace" else json.dumps(family, ensure_ascii=False) + ", monospace"
    return f"textview {{ font-family: {css_family}; font-size: {points}pt; }}".encode("utf-8")


def editor_scheme_manager():
    """Expose installed and bundled syntax schemes while preserving user-theme precedence."""

    manager = GtkSource.StyleSchemeManager.get_default()
    bundled = str(Path(__file__).with_name("styles"))
    # 2026-10-30: il tema distribuito deve funzionare anche in un'installazione
    # pulita. Aggiungerlo in coda mantiene prioritarie le personalizzazioni utente.
    if bundled not in manager.get_search_path():
        manager.append_search_path(bundled)
        manager.force_rescan()
    return manager


def editor_scheme(preferences: dict):
    """Resolve the selected installed scheme, falling back to the system-theme default."""

    manager = editor_scheme_manager()
    selected = manager.get_scheme(preferences.get("style_scheme", ""))
    if selected is not None:
        return selected
    settings = Gtk.Settings.get_default()
    theme = str(settings.get_property("gtk-theme-name") if settings else "")
    return manager.get_scheme("oblivion" if "dark" in theme.lower() else "classic")


def editor_text_color(preferences: dict, scheme) -> str:
    """Return the effective plain-text color displayed by the settings color selector."""

    if preferences.get("text_color"):
        return preferences["text_color"]
    style = scheme.get_style("text") if scheme is not None else None
    if style is None:
        return "#000000"
    return style.get_property("foreground") or "#000000"


def diff_colors(preferences: dict, scheme, *, compact: bool = False) -> tuple[str, str]:
    """Use explicit diff backgrounds or theme-aware defaults without overriding syntax colors."""

    dark = scheme is not None and scheme.get_id() == "oblivion"
    style = scheme.get_style("text") if scheme is not None else None
    background = style.get_property("background") if style is not None else None
    rgba = Gdk.RGBA()
    if background and rgba.parse(background):
        dark = 0.2126 * rgba.red + 0.7152 * rgba.green + 0.0722 * rgba.blue < 0.5
    defaults = ("#205c32", "#702d38") if dark and not compact else ("#a8eba8", "#f5a3ab")
    return (preferences.get("diff_added_color") or defaults[0],
            preferences.get("diff_removed_color") or defaults[1])


def apply_editor_text_color(provider: Gtk.CssProvider, preferences: dict) -> None:
    """Set only the view's base foreground, leaving GtkSource syntax tags untouched."""

    # 2026-10-30: color sul nodo text definisce il testo privo di tag, senza
    # aggiungere un tag globale che coprirebbe i colori della sintassi.
    color = preferences.get("text_color")
    css = f"textview, textview text {{ color: {color}; }}" if color else ""
    provider.load_from_data(css.encode("utf-8"))
