"""Reusable in-buffer search controls for read-only GtkSource previews."""

from __future__ import annotations

from typing import Callable

import gi

gi.require_version("Gdk", "3.0")
gi.require_version("Gtk", "3.0")
gi.require_version("GtkSource", "4")
from gi.repository import Gdk, Gtk, GtkSource  # noqa: E402


class TextSearchBar(Gtk.Revealer):
    """Provide consistent find controls for one GtkSource buffer and view."""

    def __init__(
        self,
        buffer: GtkSource.Buffer,
        view: GtkSource.View,
        focus_target: Gtk.Widget | None = None,
    ) -> None:
        """Build a hidden search bar bound to the supplied preview surface."""

        super().__init__()
        self.buffer = buffer
        self.view = view
        self.focus_target = focus_target or view
        self.settings = GtkSource.SearchSettings()
        self.settings.set_wrap_around(True)
        # 2026-09-27: un solo componente mantiene identici shortcut e ricerca
        # locale nelle preview senza duplicare letture o processi esterni.
        self.context = GtkSource.SearchContext.new(buffer, self.settings)
        self.connect("key-press-event", self._on_bar_key_press)

        search_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        search_box.get_style_context().add_class("editor-search")
        self.entry = Gtk.SearchEntry()
        self.entry.set_placeholder_text("Find…")
        self.entry.connect("search-changed", self._on_search_changed)
        self.entry.connect("key-press-event", self._on_entry_key_press)
        search_box.pack_start(self.entry, True, True, 0)
        search_box.pack_start(
            self._button("go-up", "Previous result (Shift+F3)", self._on_previous),
            False,
            False,
            0,
        )
        search_box.pack_start(
            self._button("go-down", "Next result (F3)", self._on_next),
            False,
            False,
            0,
        )
        search_box.pack_start(
            self._button("window-close-symbolic", "Close search (Esc)", self._on_close),
            False,
            False,
            0,
        )
        self.add(search_box)

    @staticmethod
    def _button(
        icon: str, tooltip: str, callback: Callable[[Gtk.Button], None]
    ) -> Gtk.Button:
        """Create one accessible icon-only search action button."""

        button = Gtk.Button()
        button.set_relief(Gtk.ReliefStyle.NONE)
        button.set_image(Gtk.Image.new_from_icon_name(icon, Gtk.IconSize.BUTTON))
        button.set_tooltip_text(tooltip)
        button.get_accessible().set_name(tooltip)
        button.connect("clicked", callback)
        return button

    def show_search(self) -> None:
        """Reveal search and initialize its query from the current selection."""

        bounds = self.buffer.get_selection_bounds()
        if bounds:
            self.entry.set_text(self.buffer.get_text(bounds[0], bounds[1], True))
        self.set_reveal_child(True)
        self.entry.grab_focus()
        self.entry.select_region(0, -1)

    def close_search(self) -> None:
        """Hide search and return keyboard focus to its source preview."""

        self.set_reveal_child(False)
        self.focus_target.grab_focus()

    def search(self, backwards: bool = False) -> None:
        """Select the next or previous match, wrapping at buffer boundaries."""

        if not self.settings.get_search_text():
            return
        bounds = self.buffer.get_selection_bounds()
        if bounds:
            # La selezione GTK mantiene l'insert mark sul principio del match;
            # riutilizzarlo nella ricerca forward ritroverebbe sempre la stessa
            # occorrenza. Si parte invece dal lato coerente con la direzione.
            cursor = bounds[0] if backwards else bounds[1]
        else:
            cursor = self.buffer.get_iter_at_mark(self.buffer.get_insert())
        match = (
            self.context.backward(cursor)
            if backwards
            else self.context.forward(cursor)
        )
        found, start, end, _wrapped = match
        if not found:
            boundary = (
                self.buffer.get_end_iter()
                if backwards
                else self.buffer.get_start_iter()
            )
            match = (
                self.context.backward(boundary)
                if backwards
                else self.context.forward(boundary)
            )
            found, start, end, _wrapped = match
        if found:
            self.buffer.select_range(start, end)
            self.view.scroll_to_iter(start, 0.15, False, 0.0, 0.0)

    def handle_key(self, event: Gdk.EventKey) -> bool:
        """Handle the shared Ctrl+F, F3, Shift+F3 and Escape shortcuts."""

        modifiers = event.state & Gtk.accelerator_get_default_mod_mask()
        if (
            Gdk.keyval_to_lower(event.keyval) == Gdk.KEY_f
            and modifiers == Gdk.ModifierType.CONTROL_MASK
        ):
            self.show_search()
            return True
        if event.keyval == Gdk.KEY_F3:
            self.search(bool(modifiers & Gdk.ModifierType.SHIFT_MASK))
            return True
        if event.keyval == Gdk.KEY_Escape and self.get_reveal_child():
            self.close_search()
            return True
        return False

    def _on_search_changed(self, entry: Gtk.SearchEntry) -> None:
        """Update highlighting immediately when the query changes."""

        self.settings.set_search_text(entry.get_text())

    def _on_bar_key_press(
        self, _bar: Gtk.Revealer, event: Gdk.EventKey
    ) -> bool:
        """Keep shared shortcuts active while a search button owns focus."""

        return self.handle_key(event)

    def _on_entry_key_press(
        self, _entry: Gtk.SearchEntry, event: Gdk.EventKey
    ) -> bool:
        """Navigate from the entry while retaining the shared key bindings."""

        if event.keyval == Gdk.KEY_Escape:
            self.close_search()
            return True
        if event.keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            self.search(bool(event.state & Gdk.ModifierType.SHIFT_MASK))
            return True
        return self.handle_key(event)

    def _on_previous(self, _button: Gtk.Button) -> None:
        """Select the previous search result."""

        self.search(True)

    def _on_next(self, _button: Gtk.Button) -> None:
        """Select the next search result."""

        self.search(False)

    def _on_close(self, _button: Gtk.Button) -> None:
        """Close search from its toolbar button."""

        self.close_search()
