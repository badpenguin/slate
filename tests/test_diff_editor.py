"""Tests for the internal compact diff-editor surface."""

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

import gi

gi.require_version("Gdk", "3.0")
gi.require_version("Gtk", "3.0")
gi.require_version("GtkSource", "4")
from gi.repository import Gdk, Gtk, GtkSource, Pango  # noqa: E402

from slate.diff_editor import DiffEditorSurface


class _EditorStub(Gtk.Box):
    """Provide the widget and editor attributes required by the surface."""

    def __init__(self) -> None:
        """Build an inert editable GtkSource child."""

        super().__init__()
        self.buffer = GtkSource.Buffer()
        self.view = GtkSource.View.new_with_buffer(self.buffer)
        self.view.set_editable(True)
        self.info_bar = Gtk.InfoBar()
        self.toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        self.toolbar_container = Gtk.Overlay()
        self.toolbar_container.add(self.toolbar)
        self.path_label = Gtk.Label(label="file.py")
        self.path_label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        self.save_button = Gtk.Button()
        self.toolbar.pack_start(self.path_label, True, True, 0)
        self.toolbar.pack_start(self.save_button, False, False, 0)
        self.pack_start(self.toolbar_container, False, False, 0)
        self.dirty = False
        self.goto_line_number = MagicMock()
        self.save = MagicMock()
        self.show_search = MagicMock()


class DiffEditorSurfaceTest(unittest.TestCase):
    """Verify compact rendering and defensive surface cleanup."""

    @classmethod
    def setUpClass(cls) -> None:
        """Require the GTK display shared by the unit-test session."""

        initialized, _arguments = Gtk.init_check(None)
        if not initialized:
            raise unittest.SkipTest("display GTK non disponibile")

    @staticmethod
    def _surface() -> DiffEditorSurface:
        """Build one surface backed by a minimal persistent-editor substitute."""

        editor = _EditorStub()
        return DiffEditorSurface(
            editor,
            "file.py",
            "@@ -1 +1 @@\n-old\n+new\n",
            (1, 1, 1),
            0,
        )

    def test_partial_cleanup_does_not_block_destroy(self) -> None:
        """Missing Python attributes cannot block destruction of the surface."""

        surface = self._surface()
        del surface.editor_key_id

        surface.destroy()

        self.assertTrue(surface.cleanup_started)

    def test_destroy_restores_persistent_editor_to_original_stack(self) -> None:
        """Closing the surface must return the same editor widget to its stack."""

        stack = Gtk.Stack()
        editor = _EditorStub()
        stack.add_named(editor, "project:file.py")
        surface = DiffEditorSurface(
            editor,
            "file.py",
            "@@ -1 +1 @@\n-old\n+new\n",
            (1, 1, 1),
            0,
        )

        self.assertIs(editor.get_parent(), surface.split)
        surface.destroy()

        self.assertIs(editor.get_parent(), stack)
        self.assertEqual(
            stack.child_get_property(editor, "name"), "project:file.py"
        )

    def test_selected_hunk_is_complete_and_reuses_editor_view(self) -> None:
        """The upper pane keeps the unified diff without duplicating editor view."""

        editor = _EditorStub()
        diff_text = "@@ -1,4 +1,4 @@\n one\n-two\n+changed\n three\n four\n"
        surface = DiffEditorSurface(
            editor,
            "file.py",
            diff_text,
            (1, 1, 2, 2, 3, 4),
            0,
        )

        rendered = surface.diff_buffer.get_text(
            surface.diff_buffer.get_start_iter(),
            surface.diff_buffer.get_end_iter(),
            True,
        )
        self.assertEqual(rendered, diff_text)
        self.assertFalse(surface.diff_view.get_show_line_numbers())
        editor.goto_line_number.assert_called_with(2)
        self.assertIs(editor.get_parent(), surface.split)
        self.assertIs(surface.split.get_child2(), editor)
        surface.destroy()

    def test_hunk_navigation_replaces_the_visible_compact_fragment(self) -> None:
        """Moving to another change shows only that compact hunk."""

        editor = _EditorStub()
        diff_text = (
            "--- a/file.py\n"
            "+++ b/file.py\n"
            "@@ -1 +1 @@\n"
            "-first\n"
            "+one\n"
            "@@ -10 +10 @@\n"
            "-second\n"
            "+two\n"
        )
        surface = DiffEditorSurface(
            editor,
            "file.py",
            diff_text,
            (None, None, 1, 1, 1, 10, 10, 10),
            2,
        )

        surface._move_hunk(1)

        rendered = surface.diff_buffer.get_text(
            surface.diff_buffer.get_start_iter(),
            surface.diff_buffer.get_end_iter(),
            True,
        )
        self.assertEqual(rendered, "@@ -10 +10 @@\n-second\n+two\n")
        self.assertEqual(surface.hunk_index, 1)
        self.assertEqual(surface.diff_action_box.get_spacing(), 6)
        self.assertIs(
            surface.navigation_sector.get_parent(), editor.toolbar
        )
        self.assertIs(surface.navigation_box.get_parent(), surface.navigation_sector)
        self.assertIn(
            "diff-change-position",
            surface.position_label.get_style_context().list_classes(),
        )
        self.assertEqual(len(surface.close_button.get_child().get_children()), 1)
        surface.destroy()

    def test_diff_editor_keyboard_commands_route_without_implicit_save(self) -> None:
        """Navigation, search and save shortcuts keep their separate effects."""

        editor = _EditorStub()
        surface = DiffEditorSurface(
            editor,
            "file.py",
            "@@ -1 +1 @@\n-old\n+one\n@@ -5 +5 @@\n-before\n+after\n",
            (1, 1, 1, 5, 5, 5),
            0,
        )
        right = SimpleNamespace(
            keyval=Gdk.KEY_Right,
            state=Gdk.ModifierType.CONTROL_MASK,
        )
        left = SimpleNamespace(
            keyval=Gdk.KEY_Left,
            state=Gdk.ModifierType.CONTROL_MASK,
        )
        find = SimpleNamespace(
            keyval=Gdk.KEY_f,
            state=Gdk.ModifierType.CONTROL_MASK,
        )
        save = SimpleNamespace(
            keyval=Gdk.KEY_s,
            state=Gdk.ModifierType.CONTROL_MASK,
        )

        self.assertTrue(surface._on_key_press(surface.diff_view, right))
        self.assertEqual(surface.hunk_index, 1)
        editor.save.assert_not_called()
        self.assertTrue(surface._on_key_press(surface.diff_view, left))
        self.assertEqual(surface.hunk_index, 0)
        editor.save.assert_not_called()
        self.assertTrue(surface._on_key_press(surface.diff_view, find))
        self.assertTrue(
            surface.search_bars[surface.diff_view].get_reveal_child()
        )
        self.assertTrue(surface._on_key_press(editor.view, save))
        editor.save.assert_called_once_with()
        surface.destroy()

    def test_navigation_shares_toolbar_layout_with_long_path(self) -> None:
        """Long paths yield horizontal space without overlaying navigation."""

        editor = _EditorStub()
        editor.path_label.set_text("public/" + "very-long-directory/" * 8 + "file.php")
        surface = DiffEditorSurface(
            editor,
            editor.path_label.get_text(),
            "@@ -1 +1 @@\n-old\n+new\n",
            (1, 1, 1),
            0,
        )

        self.assertIs(surface.navigation_sector.get_parent(), editor.toolbar)
        self.assertIs(surface.navigation_box.get_parent(), surface.navigation_sector)
        self.assertIs(surface.diff_action_box.get_parent(), editor.toolbar)
        self.assertTrue(
            editor.toolbar.query_child_packing(surface.navigation_sector)[0]
        )
        self.assertFalse(
            editor.toolbar.query_child_packing(surface.diff_action_box)[0]
        )
        self.assertEqual(
            editor.path_label.get_ellipsize(), Pango.EllipsizeMode.MIDDLE
        )
        surface.destroy()

    def test_diff_height_fits_content_without_exceeding_forty_percent(self) -> None:
        """Compact hunks use their useful height and retain most room for editing."""

        surface = self._surface()
        allocation = Gdk.Rectangle()
        allocation.height = 1000

        surface._on_split_size_allocate(surface.split, allocation)

        self.assertGreaterEqual(surface.split.get_position(), 72)
        self.assertLessEqual(surface.split.get_position(), 400)
        self.assertTrue(
            surface.split.child_get_property(
                surface.split.get_child1(), "shrink"
            )
        )
        surface.destroy()

    def test_shorter_hunk_shrinks_pane_immediately(self) -> None:
        """Navigation applies a smaller content height without deferred GTK layout."""

        surface = self._surface()
        surface.split.set_position(300)

        surface._resize_diff_pane(1000)

        self.assertLess(surface.split.get_position(), 300)
        self.assertEqual(
            surface.diff_box.get_size_request()[1],
            surface.split.get_position(),
        )
        surface.destroy()


if __name__ == "__main__":
    unittest.main()
