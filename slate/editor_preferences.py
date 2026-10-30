"""Validate persisted editor appearance without importing graphical dependencies."""

import re


EDITOR_APPEARANCE_DEFAULTS = {
    "font_family": "monospace",
    "style_scheme": "",
    "text_color": None,
    "diff_added_color": None,
    "diff_removed_color": None,
}


def normalize_editor_appearance(values: object) -> dict:
    """Return safe optional RGB overrides and a bounded syntax scheme identifier."""

    normalized = dict(EDITOR_APPEARANCE_DEFAULTS)
    if not isinstance(values, dict):
        return normalized
    # 2026-10-30: la configurazione conserva solo colori RGB e identificatori,
    # mai CSS libero; None e stringa vuota mantengono i predefiniti del tema.
    for key in ("text_color", "diff_added_color", "diff_removed_color"):
        color = values.get(key)
        if isinstance(color, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", color):
            normalized[key] = color.lower()
    family = values.get("font_family")
    if isinstance(family, str) and 0 < len(family.strip()) <= 128 and family.isprintable():
        normalized["font_family"] = family.strip()
    scheme = values.get("style_scheme")
    if isinstance(scheme, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", scheme):
        normalized["style_scheme"] = scheme
    return normalized
