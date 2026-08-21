"""GTK tests for the extensible SLATE settings dialog."""

import unittest

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk

from slate.config import is_extra_command_icon
from slate.settings import (
    CommandEditDialog,
    CommandIconDialog,
    SettingsDialog,
    _available_command_icons,
)


class SettingsDialogTest(unittest.TestCase):
    """Verify section controls publish independent font sizes."""

    @classmethod
    def setUpClass(cls) -> None:
        """Require the GTK display shared by the final test session."""

        initialized, _arguments = Gtk.init_check(None)
        if not initialized:
            raise unittest.SkipTest("display GTK non disponibile")

    def setUp(self) -> None:
        """Create a dialog with distinct persisted values for both sections."""

        self.changes: list[tuple[str, int]] = []
        self.status_changes: list[bool] = []
        self.external_editor_changes: list[list[str]] = []
        self.command_changes: list[list[dict[str, str]]] = []
        self.parent = Gtk.Window()
        self.dialog = SettingsDialog(
            self.parent,
            {
                "revisions": {"font_size": 11},
                "files": {"font_size": 13},
                "editor": {"font_size": 15},
                "external_apps": {"editor_command": ["gvim", "-f"]},
                "terminal": {"status_bar": False},
                "commands": {
                    "items": [
                        {
                            "label": "Logs",
                            "command": "journalctl -f",
                            "icon": "text-x-script",
                        }
                    ]
                },
            },
            self._record_change,
            self._record_status_change,
            self._record_external_editor_change,
            self._record_command_change,
        )

    def tearDown(self) -> None:
        """Destroy GTK windows created by each test."""

        self.dialog.destroy()
        self.parent.destroy()

    def _record_change(self, section: str, font_size: int) -> None:
        """Record immediate settings callbacks from spin controls."""

        self.changes.append((section, font_size))

    def _record_status_change(self, enabled: bool) -> None:
        """Record immediate tmux status-bar preference changes."""

        self.status_changes.append(enabled)

    def _record_external_editor_change(self, command: list[str]) -> None:
        """Record validated external-editor command changes."""

        self.external_editor_changes.append(command)

    def _record_command_change(self, commands: list[dict[str, str]]) -> None:
        """Record ordered extra-command settings snapshots."""

        self.command_changes.append(commands)

    def test_sections_restore_and_publish_independent_values(self) -> None:
        """Revisioni, File and Editor retain distinct values and callbacks."""

        self.assertEqual(
            self.dialog.font_spins["revisions"].get_value_as_int(), 11
        )
        self.assertEqual(self.dialog.font_spins["files"].get_value_as_int(), 13)
        self.assertEqual(self.dialog.font_spins["editor"].get_value_as_int(), 15)
        self.dialog.font_spins["files"].set_value(16)
        self.assertEqual(self.changes, [("files", 16)])
        self.assertFalse(self.dialog.status_bar_switch.get_active())
        self.dialog.status_bar_switch.set_active(True)
        self.assertEqual(self.status_changes, [True])
        self.assertEqual(self.dialog.external_editor_entry.get_text(), "gvim -f")
        self.dialog.external_editor_entry.set_text("code --wait")
        self.dialog.external_editor_entry.emit("activate")
        self.assertEqual(self.external_editor_changes, [["code", "--wait"]])

    def test_command_validation_and_reordering_publish_snapshots(self) -> None:
        """Commands validate labels and publish their explicit list order."""

        valid = {
            "label": "Deploy",
            "command": "./deploy --dry-run",
            "icon": "system-run",
        }
        self.assertIsNone(self.dialog._validate_command(valid, None))
        self.assertIsNotNone(
            self.dialog._validate_command(
                {
                    "label": "logs",
                    "command": "tail -f app.log",
                    "icon": "utilities-terminal",
                },
                None,
            )
        )
        self.dialog.commands.append(valid)
        self.dialog._on_move_command_up(Gtk.Button(), 1)
        self.assertEqual(
            [item["label"] for item in self.command_changes[-1]],
            ["Deploy", "Logs"],
        )

    def test_command_dialog_is_wide_and_icon_catalog_exceeds_one_hundred(self) -> None:
        """Command editing has working width and browses the active GTK theme."""

        editor = CommandEditDialog(self.dialog, None)
        chooser = CommandIconDialog(self.dialog, "utilities-terminal")
        try:
            icons = _available_command_icons(Gtk.IconTheme.get_default())
            expected_icons = {
                name
                for name in Gtk.IconTheme.get_default().list_icons(None)
                if is_extra_command_icon(name)
                and not name.startswith("process-working")
                and "-symbolic" not in name
            }
            self.assertEqual(set(icons), expected_icons)
            self.assertGreater(len(icons), 800)
            self.assertFalse(
                any(name.startswith("process-working") for name in icons)
            )
            self.assertFalse(any("-symbolic" in name for name in icons))
            self.assertEqual(editor.label_entry.get_width_chars(), 48)
            self.assertEqual(editor.command_entry.get_width_chars(), 72)
            self.assertEqual(editor.icon_button.get_halign(), Gtk.Align.START)
            scroller = chooser.get_content_area().get_children()[0]
            self.assertIsInstance(scroller, Gtk.ScrolledWindow)
            self.assertTrue(scroller.get_vexpand())
            icon_view = scroller.get_child()
            self.assertIsInstance(icon_view, Gtk.IconView)
            self.assertEqual(len(icon_view.get_model()), len(icons))
            self.assertEqual(icon_view.get_columns(), 9)
        finally:
            chooser.destroy()
            editor.destroy()

    def test_invalid_external_editor_command_restores_previous_value(self) -> None:
        """An incomplete quoted command never reaches persistent settings."""

        self.dialog.external_editor_entry.set_text("editor '")
        self.dialog.external_editor_entry.emit("activate")
        self.assertEqual(self.dialog.external_editor_entry.get_text(), "gvim -f")
        self.assertEqual(self.external_editor_changes, [])

    def test_fixed_browser_viewports_are_not_exposed_as_settings(self) -> None:
        """The settings sidebar omits the non-editable browser catalog."""

        self.assertFalse(hasattr(self.dialog, "browser_viewport_combo"))
        content = self.dialog.get_content_area().get_children()[0]
        stack = next(
            child
            for child in content.get_children()
            if isinstance(child, Gtk.Stack)
        )
        self.assertIsNotNone(stack.get_child_by_name("external-apps"))
        self.assertIsNotNone(stack.get_child_by_name("commands"))
        self.assertIsNone(stack.get_child_by_name("browser"))


if __name__ == "__main__":
    unittest.main()
