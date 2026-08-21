"""GTK tests for the extensible SLATE settings dialog."""

import unittest

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk

from slate.settings import SettingsDialog


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
        self.parent = Gtk.Window()
        self.dialog = SettingsDialog(
            self.parent,
            {
                "revisions": {"font_size": 11},
                "files": {"font_size": 13},
                "editor": {"font_size": 15},
                "external_apps": {"editor_command": ["gvim", "-f"]},
                "terminal": {"status_bar": False},
            },
            self._record_change,
            self._record_status_change,
            self._record_external_editor_change,
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
        self.assertIsNone(stack.get_child_by_name("browser"))


if __name__ == "__main__":
    unittest.main()
