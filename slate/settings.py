"""Native settings dialog for user-adjustable SLATE presentation options."""

from __future__ import annotations

import shlex
from typing import Callable

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # noqa: E402


class SettingsDialog(Gtk.Dialog):
    """Show extensible settings sections and publish changes immediately."""

    FONT_MIN = 8
    FONT_MAX = 32

    def __init__(
        self,
        parent: Gtk.Window,
        values: dict[str, dict[str, object]],
        on_change: Callable[[str, int], None],
        on_status_bar_change: Callable[[bool], None],
        on_external_editor_change: Callable[[list[str]], None],
    ) -> None:
        """Build the categorized global presentation preferences."""

        super().__init__(
            title="Settings",
            transient_for=parent,
            modal=True,
            destroy_with_parent=True,
        )
        self.on_change = on_change
        self.on_status_bar_change = on_status_bar_change
        self.on_external_editor_change = on_external_editor_change
        self.external_editor_command = list(
            values["external_apps"]["editor_command"]
        )
        self.font_spins: dict[str, Gtk.SpinButton] = {}
        self.set_default_size(520, 300)
        self.add_button("Close", Gtk.ResponseType.CLOSE)

        # 2026-08-16: StackSidebar lascia spazio a nuove categorie future senza
        # dover cambiare struttura al dialogo quando le preferenze aumenteranno.
        stack = Gtk.Stack()
        stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        stack.set_transition_duration(120)
        stack.add_titled(
            self._build_font_page(
                "revisions", int(values["revisions"]["font_size"])
            ),
            "revisions",
            "Revisions",
        )
        stack.add_titled(
            self._build_font_page("files", int(values["files"]["font_size"])),
            "files",
            "Files",
        )
        stack.add_titled(
            self._build_font_page("editor", int(values["editor"]["font_size"])),
            "editor",
            "Editor",
        )
        stack.add_titled(
            self._build_external_apps_page(self.external_editor_command),
            "external-apps",
            "External apps",
        )
        stack.add_titled(
            self._build_terminal_page(bool(values["terminal"]["status_bar"])),
            "terminal",
            "Terminal",
        )
        # 2026-08-17: un catalogo fisso e non modificabile non è una
        # preferenza; la modalità Responsive conserva il proprio preset senza
        # occupare una categoria vuota nel dialogo delle Impostazioni.
        sidebar = Gtk.StackSidebar()
        sidebar.set_stack(stack)
        sidebar.set_size_request(150, -1)

        content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        content.get_style_context().add_class("settings-layout")
        content.pack_start(sidebar, False, False, 0)
        content.pack_start(stack, True, True, 0)
        self.get_content_area().pack_start(content, True, True, 0)
        self.connect("response", self._on_response)

    def _build_font_page(
        self,
        section: str,
        font_size: int,
    ) -> Gtk.Widget:
        """Create one section containing its font-size control."""

        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        page.get_style_context().add_class("settings-page")
        title = Gtk.Label(label="Font size")
        title.set_xalign(0)
        title.get_style_context().add_class("settings-page-title")
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        label = Gtk.Label(label="List font size")
        label.set_xalign(0)
        spin = Gtk.SpinButton.new_with_range(self.FONT_MIN, self.FONT_MAX, 1)
        spin.set_value(font_size)
        spin.set_numeric(True)
        spin.set_tooltip_text("Size in points")
        spin.connect("value-changed", self._on_font_size_changed, section)
        self.font_spins[section] = spin
        row.pack_start(label, True, True, 0)
        row.pack_end(spin, False, False, 0)
        page.pack_start(title, False, False, 0)
        page.pack_start(row, False, False, 0)
        return page

    def _build_external_apps_page(
        self, external_editor_command: list[str]
    ) -> Gtk.Widget:
        """Create the global page for commands delegated to external apps."""

        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        page.get_style_context().add_class("settings-page")
        title = Gtk.Label(label="File editor")
        title.set_xalign(0)
        title.get_style_context().add_class("settings-page-title")
        editor_label = Gtk.Label(label="Command")
        editor_label.set_xalign(0)
        # 2026-08-20: il percorso del file viene aggiunto separatamente al
        # vettore argv, quindi il campo non espone interpolazione o shell.
        self.external_editor_entry = Gtk.Entry(
            text=shlex.join(external_editor_command)
        )
        self.external_editor_entry.set_tooltip_text(
            "Command and arguments; the file path is appended automatically"
        )
        self.external_editor_entry.connect(
            "activate", self._on_external_editor_activated
        )
        self.external_editor_entry.connect(
            "focus-out-event", self._on_external_editor_focus_out
        )
        page.pack_start(title, False, False, 0)
        page.pack_start(editor_label, False, False, 0)
        page.pack_start(self.external_editor_entry, False, False, 0)
        return page

    def _build_terminal_page(self, status_bar: bool) -> Gtk.Widget:
        """Create the terminal page containing only the tmux status switch."""

        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        page.get_style_context().add_class("settings-page")
        title = Gtk.Label(label="tmux")
        title.set_xalign(0)
        title.get_style_context().add_class("settings-page-title")
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        label = Gtk.Label(label="Status bar")
        label.set_xalign(0)
        self.status_bar_switch = Gtk.Switch()
        self.status_bar_switch.set_active(status_bar)
        self.status_bar_switch.set_tooltip_text("Show the tmux status bar")
        self.status_bar_switch.connect(
            "notify::active", self._on_status_bar_changed
        )
        row.pack_start(label, True, True, 0)
        row.pack_end(self.status_bar_switch, False, False, 0)
        page.pack_start(title, False, False, 0)
        page.pack_start(row, False, False, 0)
        return page

    def _on_font_size_changed(
        self, spin: Gtk.SpinButton, section: str
    ) -> None:
        """Publish an integer point size as soon as the control changes."""

        self.on_change(section, spin.get_value_as_int())

    def _on_status_bar_changed(
        self, switch: Gtk.Switch, _property: object
    ) -> None:
        """Publish the tmux status preference as soon as it changes."""

        self.on_status_bar_change(switch.get_active())

    def _publish_external_editor(self) -> None:
        """Validate and publish the external-editor argument vector."""

        try:
            command = shlex.split(self.external_editor_entry.get_text())
        except ValueError:
            command = []
        if not command or len(command) > 32 or any(
            not argument or len(argument) > 4096 or "\0" in argument
            for argument in command
        ):
            self.external_editor_entry.set_text(
                shlex.join(self.external_editor_command)
            )
            return
        if command == self.external_editor_command:
            return
        self.external_editor_command = command
        self.on_external_editor_change(command)

    def _on_external_editor_activated(self, _entry: Gtk.Entry) -> None:
        """Apply a valid external-editor command when Enter is pressed."""

        self._publish_external_editor()

    def _on_external_editor_focus_out(
        self, _entry: Gtk.Entry, _event: object
    ) -> bool:
        """Apply the external-editor command when its field loses focus."""

        self._publish_external_editor()
        return False

    def _on_response(
        self, _dialog: Gtk.Dialog, _response: Gtk.ResponseType
    ) -> None:
        """Apply the active editor field and destroy the settings dialog."""

        self._publish_external_editor()
        self.destroy()
