"""Native settings dialog for user-adjustable SLATE presentation options."""

from __future__ import annotations

import shlex
from typing import Callable

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # noqa: E402

from .config import DEFAULT_EXTRA_COMMAND_ICON, is_extra_command_icon


def _command_icon_sort_key(name: str) -> str:
    """Sort available colored theme icons by their stable identifier."""

    return name.casefold()


def _available_command_icons(theme: Gtk.IconTheme) -> list[str]:
    """Return every safe icon exposed by the active system theme."""

    # 2026-08-21: IconTheme risolve directory, ereditarietà e risorse del tema;
    # non imponiamo limiti numerici al catalogo richiesto dall'utente.
    names = {
        name
        for name in theme.list_icons(None)
        if is_extra_command_icon(name)
        and not name.startswith("process-working")
        and "-symbolic" not in name
    }
    return sorted(names, key=_command_icon_sort_key)


class CommandIconDialog(Gtk.Dialog):
    """Let the user choose one available icon from the curated GTK catalog."""

    def __init__(self, parent: Gtk.Window, current_icon: str) -> None:
        """Build an icon-only grid without exposing filesystem selection."""

        super().__init__(
            title="Choose Icon",
            transient_for=parent,
            modal=True,
            destroy_with_parent=True,
        )
        self.selected_icon = current_icon
        self.add_button("Cancel", Gtk.ResponseType.CANCEL)
        theme = Gtk.IconTheme.get_default()
        available = _available_command_icons(theme)
        icon_store = Gtk.ListStore(str)
        for icon_name in available:
            icon_store.append((icon_name,))
        icon_view = Gtk.IconView.new()
        self.icon_view = icon_view
        icon_view.set_model(icon_store)
        icon_view.set_selection_mode(Gtk.SelectionMode.SINGLE)
        icon_view.set_item_width(48)
        # 2026-08-21: nove colonne stabili evitano il ciclo di riallocazione tra
        # larghezza automatica e comparsa della scrollbar che causava sfarfallio.
        icon_view.set_columns(9)
        icon_view.set_column_spacing(8)
        icon_view.set_row_spacing(8)
        icon_view.set_margin(12)
        icon_view.set_tooltip_column(0)
        renderer = Gtk.CellRendererPixbuf()
        renderer.set_property("stock-size", Gtk.IconSize.DND)
        icon_view.pack_start(renderer, True)
        icon_view.add_attribute(renderer, "icon-name", 0)
        icon_view.connect("selection-changed", self._on_icon_selected)
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_hexpand(True)
        scroller.set_vexpand(True)
        scroller.set_propagate_natural_width(True)
        scroller.set_propagate_natural_height(True)
        scroller.set_max_content_height(450)
        scroller.set_shadow_type(Gtk.ShadowType.IN)
        # 2026-08-21: IconView conserva 800 nomi nel modello ma renderizza le
        # sole celle visibili, evitando il costo di 800 GtkButton e GtkImage.
        scroller.add(icon_view)
        self.get_content_area().pack_start(scroller, True, True, 0)

    def _on_icon_selected(self, icon_view: Gtk.IconView) -> None:
        """Accept the single system icon selected in the virtualized grid."""

        paths = icon_view.get_selected_items()
        if not paths:
            return
        self.selected_icon = icon_view.get_model()[paths[0]][0]
        self.response(Gtk.ResponseType.OK)


class CommandEditDialog(Gtk.Dialog):
    """Edit the label, shell line and themed icon of one extra command."""

    def __init__(
        self,
        parent: Gtk.Window,
        command: dict[str, str] | None,
    ) -> None:
        """Build fields prefilled from an existing command when supplied."""

        super().__init__(
            title="Edit Command" if command is not None else "Add Command",
            transient_for=parent,
            modal=True,
            destroy_with_parent=True,
        )
        values = command or {}
        self.icon_name = values.get("icon", DEFAULT_EXTRA_COMMAND_ICON)
        self.add_buttons(
            "Cancel",
            Gtk.ResponseType.CANCEL,
            "Save",
            Gtk.ResponseType.OK,
        )
        self.set_default_response(Gtk.ResponseType.OK)
        content = self.get_content_area()
        content.set_spacing(6)
        content.set_border_width(12)
        label_title = Gtk.Label(label="Label")
        label_title.set_xalign(0)
        self.label_entry = Gtk.Entry(text=values.get("label", ""))
        self.label_entry.set_width_chars(48)
        command_title = Gtk.Label(label="Command")
        command_title.set_xalign(0)
        self.command_entry = Gtk.Entry(text=values.get("command", ""))
        self.command_entry.set_width_chars(72)
        self.command_entry.set_activates_default(True)
        icon_title = Gtk.Label(label="Icon")
        icon_title.set_xalign(0)
        self.icon_button = Gtk.Button()
        self.icon_button.set_halign(Gtk.Align.START)
        self.icon_button.set_hexpand(False)
        self._refresh_icon_button()
        self.icon_button.connect("clicked", self._on_choose_icon)
        self.error_label = Gtk.Label()
        self.error_label.set_xalign(0)
        self.error_label.set_line_wrap(True)
        self.error_label.get_style_context().add_class("error")
        for widget in (
            label_title,
            self.label_entry,
            command_title,
            self.command_entry,
            icon_title,
            self.icon_button,
            self.error_label,
        ):
            content.pack_start(widget, False, False, 0)

    def _refresh_icon_button(self) -> None:
        """Show the currently selected themed icon and its identifier."""

        content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        content.pack_start(
            Gtk.Image.new_from_icon_name(self.icon_name, Gtk.IconSize.BUTTON),
            False,
            False,
            0,
        )
        content.pack_start(Gtk.Label(label=self.icon_name), False, False, 0)
        current = self.icon_button.get_child()
        if current is not None:
            self.icon_button.remove(current)
        self.icon_button.add(content)
        content.show_all()

    def _on_choose_icon(self, _button: Gtk.Button) -> None:
        """Open the curated themed-icon dialog and apply its accepted choice."""

        dialog = CommandIconDialog(self, self.icon_name)
        dialog.show_all()
        response = dialog.run()
        if response == Gtk.ResponseType.OK:
            self.icon_name = dialog.selected_icon
            self._refresh_icon_button()
        dialog.destroy()

    def command_values(self) -> dict[str, str]:
        """Return trimmed values currently entered by the user."""

        return {
            "label": self.label_entry.get_text().strip(),
            "command": self.command_entry.get_text().strip(),
            "icon": self.icon_name,
        }


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
        on_commands_change: Callable[[list[dict[str, str]]], None],
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
        self.on_commands_change = on_commands_change
        self.commands = [dict(item) for item in values["commands"]["items"]]
        self.external_editor_command = list(
            values["external_apps"]["editor_command"]
        )
        self.font_spins: dict[str, Gtk.SpinButton] = {}
        self.set_default_size(680, 420)
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
        stack.add_titled(
            self._build_commands_page(),
            "commands",
            "Commands",
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

    def _build_commands_page(self) -> Gtk.Widget:
        """Create the ordered editor for global extra terminal commands."""

        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        page.get_style_context().add_class("settings-page")
        title_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        title = Gtk.Label(label="Additional commands")
        title.set_xalign(0)
        title.get_style_context().add_class("settings-page-title")
        self.add_command_button = Gtk.Button(label="Add")
        self.add_command_button.connect("clicked", self._on_add_command)
        title_row.pack_start(title, True, True, 0)
        title_row.pack_end(self.add_command_button, False, False, 0)
        self.command_rows = Gtk.ListBox()
        self.command_rows.set_selection_mode(Gtk.SelectionMode.NONE)
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.add(self.command_rows)
        page.pack_start(title_row, False, False, 0)
        page.pack_start(scroll, True, True, 0)
        self._rebuild_command_rows()
        return page

    def _rebuild_command_rows(self) -> None:
        """Recreate compact command summaries after an ordered-list mutation."""

        for child in self.command_rows.get_children():
            self.command_rows.remove(child)
        self.add_command_button.set_sensitive(len(self.commands) < 32)
        # 2026-08-21: ogni riga mantiene azioni legate a un indice esplicito;
        # dopo ogni mutazione l'intera lista riallinea indici e sensibilità.
        for index, command in enumerate(self.commands):
            row = Gtk.ListBoxRow()
            content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            content.set_border_width(4)
            content.pack_start(
                Gtk.Image.new_from_icon_name(command["icon"], Gtk.IconSize.BUTTON),
                False,
                False,
                0,
            )
            text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            label = Gtk.Label(label=command["label"])
            label.set_xalign(0)
            summary = Gtk.Label(
                label=(
                    command["command"]
                    if len(command["command"]) <= 70
                    else f"{command['command'][:67]}…"
                )
            )
            summary.set_xalign(0)
            summary.get_style_context().add_class("dim-label")
            text.pack_start(label, False, False, 0)
            text.pack_start(summary, False, False, 0)
            content.pack_start(text, True, True, 0)
            for icon_name, tooltip, callback, sensitive in (
                ("go-up-symbolic", "Move up", self._on_move_command_up, index > 0),
                (
                    "go-down-symbolic",
                    "Move down",
                    self._on_move_command_down,
                    index < len(self.commands) - 1,
                ),
                ("document-edit-symbolic", "Edit", self._on_edit_command, True),
                ("edit-delete-symbolic", "Remove", self._on_remove_command, True),
            ):
                button = Gtk.Button()
                button.set_image(
                    Gtk.Image.new_from_icon_name(icon_name, Gtk.IconSize.MENU)
                )
                button.set_tooltip_text(tooltip)
                button.set_sensitive(sensitive)
                button.connect("clicked", callback, index)
                content.pack_start(button, False, False, 0)
            row.add(content)
            self.command_rows.add(row)
        self.command_rows.show_all()

    def _validate_command(
        self,
        values: dict[str, str],
        editing_index: int | None,
    ) -> str | None:
        """Return an explanatory validation error or accept command values."""

        label = values["label"]
        command = values["command"]
        if not label or len(label) > 80 or any(char in label for char in "\r\n\0"):
            return "Label must contain 1–80 characters on one line."
        if any(
            item["label"].casefold() == label.casefold() and index != editing_index
            for index, item in enumerate(self.commands)
        ):
            return "Command labels must be unique."
        if (
            not command
            or len(command) > 4096
            or any(char in command for char in "\r\n\0")
        ):
            return "Command must contain 1–4096 characters on one line."
        try:
            words = shlex.split(command, posix=True)
        except ValueError:
            return "The command quoting is invalid."
        if not words or not is_extra_command_icon(values["icon"]):
            return "Choose a valid command and icon."
        return None

    def _edit_command(self, index: int | None) -> None:
        """Run the command editor until valid values are saved or cancelled."""

        command = self.commands[index] if index is not None else None
        dialog = CommandEditDialog(self, command)
        dialog.show_all()
        while dialog.run() == Gtk.ResponseType.OK:
            values = dialog.command_values()
            error = self._validate_command(values, index)
            if error is not None:
                dialog.error_label.set_text(error)
                continue
            if index is None:
                self.commands.append(values)
            else:
                self.commands[index] = values
            self._publish_commands()
            break
        dialog.destroy()

    def _publish_commands(self) -> None:
        """Refresh command rows and publish an independent settings snapshot."""

        self._rebuild_command_rows()
        self.on_commands_change([dict(item) for item in self.commands])

    def _on_add_command(self, _button: Gtk.Button) -> None:
        """Open a blank editor while the configured limit permits another row."""

        if len(self.commands) < 32:
            self._edit_command(None)

    def _on_edit_command(self, _button: Gtk.Button, index: int) -> None:
        """Edit the command currently represented by a summary row."""

        if 0 <= index < len(self.commands):
            self._edit_command(index)

    def _on_move_command_up(self, _button: Gtk.Button, index: int) -> None:
        """Move one command earlier in the HeaderBar popover order."""

        if 0 < index < len(self.commands):
            self.commands[index - 1], self.commands[index] = (
                self.commands[index],
                self.commands[index - 1],
            )
            self._publish_commands()

    def _on_move_command_down(self, _button: Gtk.Button, index: int) -> None:
        """Move one command later in the HeaderBar popover order."""

        if 0 <= index < len(self.commands) - 1:
            self.commands[index + 1], self.commands[index] = (
                self.commands[index],
                self.commands[index + 1],
            )
            self._publish_commands()

    def _on_remove_command(self, _button: Gtk.Button, index: int) -> None:
        """Confirm and remove one explicitly targeted extra command."""

        if not 0 <= index < len(self.commands):
            return
        dialog = Gtk.MessageDialog(
            transient_for=self,
            modal=True,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.NONE,
            text=f"Remove “{self.commands[index]['label']}”?",
        )
        dialog.add_buttons(
            "Cancel",
            Gtk.ResponseType.CANCEL,
            "Remove",
            Gtk.ResponseType.OK,
        )
        response = dialog.run()
        dialog.destroy()
        if response == Gtk.ResponseType.OK:
            self.commands.pop(index)
            self._publish_commands()

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
