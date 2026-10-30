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
        self.close_to_tray_changes: list[bool] = []
        self.status_changes: list[bool] = []
        self.command_changes: list[list[dict[str, str]]] = []
        self.editor_appearance_changes = []
        self.parent = Gtk.Window()
        self.dialog = SettingsDialog(
            self.parent,
            {
                "general": {"close_to_tray": True},
                "revisions": {"font_size": 11},
                "files": {"font_size": 13},
                "editor": {"font_size": 15},
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
            self._record_close_to_tray_change,
            self._record_status_change,

            self._record_command_change,
            on_editor_appearance_change=self._record_editor_appearance_change,
        )

    def tearDown(self) -> None:
        """Destroy GTK windows created by each test."""

        self.dialog.destroy()
        self.parent.destroy()

    def _record_editor_appearance_change(self, values: dict) -> None:
        """Record appearance changes emitted by the actual native settings controls."""

        self.editor_appearance_changes.append(values)

    def test_editor_colors_and_theme_update_the_small_preview_and_reset_to_defaults(self) -> None:
        """Changing a color updates the preview and callback without repainting syntax tokens."""

        from gi.repository import Gdk
        self.assertEqual(set(self.dialog.editor_color_buttons), {
            "text_color", "diff_added_color", "diff_removed_color"})
        self.assertTrue(self.dialog.editor_theme.set_active_id("oblivion"))
        self.assertEqual(self.dialog.editor_preview_buffer.get_style_scheme().get_id(), "oblivion")
        rgba = Gdk.RGBA()
        rgba.parse("#aabbcc")
        self.dialog.editor_color_buttons["text_color"].set_rgba(rgba)
        self.dialog.editor_color_buttons["text_color"].emit("color-set")
        self.assertEqual(self.editor_appearance_changes[-1]["text_color"], "#aabbcc")
        self.assertEqual(self.editor_appearance_changes[-1]["style_scheme"], "oblivion")
        foreground = self.dialog.editor_preview.get_style_context().get_color(Gtk.StateFlags.NORMAL)
        self.assertAlmostEqual(foreground.red, rgba.red, places=3)
        self.assertAlmostEqual(foreground.green, rgba.green, places=3)
        rgba.parse("#334455")
        self.dialog.editor_color_buttons["diff_added_color"].set_rgba(rgba)
        self.dialog.editor_color_buttons["diff_added_color"].emit("color-set")
        background = self.dialog.editor_preview_added.get_property("background-rgba")
        self.assertAlmostEqual(background.red, rgba.red, places=3)
        self.assertFalse(self.dialog.editor_preview_added.get_property("foreground-set"))
        self.assertEqual(self.dialog.editor_preview_buffer.get_language().get_id(), "python")
        calls = len(self.editor_appearance_changes)
        self.dialog._on_editor_colors_reset(None)
        self.assertEqual(len(self.editor_appearance_changes), calls + 1)
        self.assertEqual(self.editor_appearance_changes[-1], {
            "font_family": "monospace", "style_scheme": "", "text_color": None,
            "diff_added_color": None, "diff_removed_color": None})
        self.assertEqual(self.dialog.font_spins["editor"].get_value_as_int(), 15)
        self.assertTrue(self.dialog.editor_preview_buffer.get_highlight_syntax())

    def test_editor_font_selector_lists_only_monospace_and_previews_font_and_size(self) -> None:
        """Only installed fixed-width families can be chosen, and size changes retain that family."""

        from slate.editor_appearance import monospaced_font_families
        families = {family.get_name() for family in self.dialog.editor_preview.get_pango_context().list_families()
                    if family.is_monospace()}
        combo = self.dialog.editor_font
        identifiers = {row[combo.get_id_column()] for row in combo.get_model()}
        self.assertEqual(identifiers - {"monospace"}, set(monospaced_font_families()))
        self.assertTrue((identifiers - {"monospace"}).issubset(families))
        chosen = monospaced_font_families()[0]
        self.assertTrue(combo.set_active_id(chosen))
        self.assertEqual(self.editor_appearance_changes[-1]["font_family"], chosen)
        self.assertIn(chosen, self.dialog.editor_preview_font.to_string())
        self.dialog.font_spins["editor"].set_value(18)
        self.assertEqual(self.changes[-1], ("editor", 18))
        css = self.dialog.editor_preview_font.to_string()
        self.assertIn(chosen, css)
        self.assertIn("18pt", css)
        self.dialog._on_editor_colors_reset(None)
        self.assertEqual(self.editor_appearance_changes[-1]["font_family"], chosen)
        self.assertEqual(self.dialog.font_spins["editor"].get_value_as_int(), 18)

    def test_editor_preview_has_real_code_syntax_and_separate_changed_line_backgrounds(self) -> None:
        """The larger sample exposes keywords, literals, comments and both diff backgrounds together."""

        buffer = self.dialog.editor_preview_buffer
        text = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True)
        self.assertGreaterEqual(len(text.splitlines()), 10)
        self.assertTrue(buffer.get_highlight_syntax())
        for token in ("def", "0.10", "# Removed line", '"""Return'):
            tags = buffer.get_iter_at_offset(text.index(token)).get_tags()
            self.assertTrue(any(tag.get_property("foreground-set") for tag in tags), token)
        self.assertIn(self.dialog.editor_preview_removed,
                      buffer.get_iter_at_offset(text.index("old_discount")).get_tags())
        self.assertIn(self.dialog.editor_preview_added,
                      buffer.get_iter_at_offset(text.index("discount = 0.15")).get_tags())
        self.assertNotIn(self.dialog.editor_preview_added,
                         buffer.get_iter_at_offset(text.index("amount =")).get_tags())
        self.assertFalse(self.dialog.editor_preview_added.get_property("foreground-set"))
        self.assertFalse(self.dialog.editor_preview_removed.get_property("foreground-set"))

    def _record_change(self, section: str, font_size: int) -> None:
        """Record immediate settings callbacks from spin controls."""

        self.changes.append((section, font_size))

    def _record_status_change(self, enabled: bool) -> None:
        """Record immediate tmux status-bar preference changes."""

        self.status_changes.append(enabled)

    def _record_close_to_tray_change(self, enabled: bool) -> None:
        """Record immediate close-behavior preference changes."""

        self.close_to_tray_changes.append(enabled)


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
        self.assertTrue(self.dialog.close_to_tray_switch.get_active())
        self.dialog.close_to_tray_switch.set_active(False)
        self.assertEqual(self.close_to_tray_changes, [False])
        self.assertFalse(self.dialog.status_bar_switch.get_active())
        self.dialog.status_bar_switch.set_active(True)
        self.assertEqual(self.status_changes, [True])
        self.assertFalse(hasattr(self.dialog, "external_editor_entry"))

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


    def test_fixed_browser_viewports_are_not_exposed_as_settings(self) -> None:
        """The settings sidebar omits the non-editable browser catalog."""

        self.assertFalse(hasattr(self.dialog, "browser_viewport_combo"))
        content = self.dialog.get_content_area().get_children()[0]
        stack = next(
            child
            for child in content.get_children()
            if isinstance(child, Gtk.Stack)
        )
        self.assertIsNone(stack.get_child_by_name("external-apps"))
        self.assertIsNotNone(stack.get_child_by_name("commands"))
        self.assertIsNone(stack.get_child_by_name("browser"))


if __name__ == "__main__":
    unittest.main()
