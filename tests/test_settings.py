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
        self.parent = Gtk.Window()
        self.dialog = SettingsDialog(
            self.parent,
            {
                "revisions": {"font_size": 11},
                "files": {"font_size": 13},
                "editor": {"font_size": 15},
                "terminal": {"status_bar": False},
            },
            self._record_change,
            self._record_status_change,
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

    def test_fixed_browser_viewports_are_not_exposed_as_settings(self) -> None:
        """The settings sidebar omits the non-editable browser catalog."""

        self.assertFalse(hasattr(self.dialog, "browser_viewport_combo"))
        content = self.dialog.get_content_area().get_children()[0]
        stack = next(
            child
            for child in content.get_children()
            if isinstance(child, Gtk.Stack)
        )
        self.assertIsNone(stack.get_child_by_name("browser"))


if __name__ == "__main__":
    unittest.main()
