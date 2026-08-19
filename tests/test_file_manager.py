"""GTK tests for the asynchronous minimal project file manager."""

import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import gi

gi.require_version("Gdk", "3.0")
gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, Gio, GLib, Gtk

from slate.file_manager import ProjectFileManager


class ProjectFileManagerTest(unittest.TestCase):
    """Verify filters, expansion and reusable file shortcuts."""

    @classmethod
    def setUpClass(cls) -> None:
        """Require the GTK display shared by the repository test session."""

        initialized, _arguments = Gtk.init_check(None)
        if not initialized:
            raise unittest.SkipTest("display GTK non disponibile")

    def setUp(self) -> None:
        """Create a representative project tree and an active browser."""

        self.temporary = tempfile.TemporaryDirectory()
        self.outside_temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.outside = Path(self.outside_temporary.name)
        (self.root / "src").mkdir()
        (self.root / "src" / "app.py").write_text("print('ok')\n", encoding="utf-8")
        (self.root / "normal.txt").write_text("normal\n", encoding="utf-8")
        (self.root / "file-link").symlink_to("normal.txt")
        (self.root / "directory-link").symlink_to("src", target_is_directory=True)
        (self.root / "broken-link").symlink_to("missing.txt")
        (self.outside / "external.txt").write_text("external\n", encoding="utf-8")
        (self.root / "external-link").symlink_to(self.outside / "external.txt")
        (self.root / ".hidden").write_text("hidden\n", encoding="utf-8")
        (self.root / "ignored.txt").write_text("ignored\n", encoding="utf-8")
        (self.root / "node_modules").mkdir()
        (self.root / "node_modules" / "package.js").write_text("x\n", encoding="utf-8")
        (self.root / ".hg").mkdir()
        (self.root / ".hg" / "dirstate").write_text("metadata", encoding="utf-8")
        self.previewed: str | None = None
        self.viewed: str | None = None
        self.edited_internal: str | None = None
        self.edited_external: str | None = None
        self.new_file_parent: str | None = None
        self.new_directory_parent: str | None = None
        self.renamed: str | None = None
        self.terminal_directory: str | None = None
        self.deleted: str | None = None
        self.preferences: list[dict[str, object]] = []
        self.manager = ProjectFileManager(
            self._record_preview,
            self._record_view,
            self._record_edit_internal,
            self._record_edit_external,
            self._record_new_file,
            self._record_new_directory,
            self._record_rename,
            self._record_open_terminal,
            self._record_delete,
            self._record_preferences,
        )
        self.manager.set_active(True)
        self.manager.set_project(
            "project",
            str(self.root),
            {"show_hidden": False, "show_excluded": False, "expanded_paths": []},
            {"ignored.txt"},
            True,
        )
        self._wait_for_path("")

    def tearDown(self) -> None:
        """Cancel GIO resources before deleting the temporary project."""

        self.manager.clear_project()
        self.temporary.cleanup()
        self.outside_temporary.cleanup()

    def _record_preview(self, relative: str | None) -> None:
        """Record preview selection changes."""

        self.previewed = relative

    def test_native_typeahead_search_is_disabled(self) -> None:
        """Single-key file commands must never open GTK's search popup."""

        self.assertFalse(self.manager.tree.get_enable_search())

    def _record_view(self, relative: str) -> None:
        """Record default-viewer actions."""

        self.viewed = relative

    def _record_edit_internal(self, relative: str) -> None:
        """Record internal editor actions."""

        self.edited_internal = relative

    def _record_edit_external(self, relative: str) -> None:
        """Record gVim editor actions."""

        self.edited_external = relative

    def _record_new_file(self, parent: str) -> None:
        """Record the parent requested by a new-file action."""

        self.new_file_parent = parent

    def _record_new_directory(self, parent: str) -> None:
        """Record the parent requested by a new-directory action."""

        self.new_directory_parent = parent

    def _record_delete(self, relative: str) -> None:
        """Record delete actions without mutating the fixture."""

        self.deleted = relative

    def _record_rename(self, relative: str) -> None:
        """Record rename actions without mutating the fixture."""

        self.renamed = relative

    def _record_open_terminal(self, relative: str) -> None:
        """Record terminal-directory actions without creating a Vte child."""

        self.terminal_directory = relative

    def _record_preferences(self, preferences: dict[str, object]) -> None:
        """Retain persisted per-project browser preferences."""

        self.preferences.append(preferences)

    def _wait_for_path(self, relative: str) -> None:
        """Iterate the GLib loop until one asynchronous directory finishes."""

        deadline = time.monotonic() + 2
        context = GLib.MainContext.default()
        while relative not in self.manager.loaded_paths and time.monotonic() < deadline:
            context.iteration(True)
        self.assertIn(relative, self.manager.loaded_paths)

    def _root_names(self) -> list[str]:
        """Return current top-level display names in model order."""

        names: list[str] = []
        tree_iter = self.manager.store.get_iter_first()
        while tree_iter:
            names.append(self.manager.store.get_value(tree_iter, self.manager.COL_NAME))
            tree_iter = self.manager.store.iter_next(tree_iter)
        return names

    def _press(self, keyval: int) -> bool:
        """Send one unmodified key event to the file tree."""

        event = SimpleNamespace(keyval=keyval, state=Gdk.ModifierType(0))
        return self.manager._on_key_press(self.manager.tree, event)

    def test_file_font_size_updates_tree_renderer(self) -> None:
        """File settings change the renderer without rebuilding its model."""

        original_store = self.manager.store
        with patch.object(self.manager.tree, "queue_resize") as resize:
            with patch.object(self.manager.tree, "queue_draw") as draw:
                self.manager.set_font_size(15)
        self.assertEqual(self.manager.text_renderer.get_property("size-points"), 15.0)
        self.assertIs(self.manager.store, original_store)
        resize.assert_called_once_with()
        draw.assert_called_once_with()

    def test_hidden_excluded_and_metadata_filters_remain_independent(self) -> None:
        """Visibility toggles reveal their own class but never VCS metadata."""

        self.assertEqual(
            self._root_names(),
            [
                "directory-link",
                "src",
                "broken-link",
                "external-link",
                "file-link",
                "normal.txt",
            ],
        )
        self.manager.hidden_check.set_active(True)
        self._wait_for_path("")
        self.assertIn(".hidden", self._root_names())
        self.assertNotIn(".hg", self._root_names())
        self.manager.excluded_check.set_active(True)
        self._wait_for_path("")
        self.assertIn("ignored.txt", self._root_names())
        self.assertIn("node_modules", self._root_names())
        self.assertNotIn(".hg", self._root_names())
        self.assertTrue(self.preferences[-1]["show_hidden"])
        self.assertTrue(self.preferences[-1]["show_excluded"])

    def test_only_safe_internal_directory_symlinks_are_expandable(self) -> None:
        """Internal directory links expand while unsafe links remain inert leaves."""

        rows = {name: tree_iter for name, tree_iter in self._root_rows()}
        for name in ("file-link", "directory-link", "broken-link", "external-link"):
            tree_iter = rows[name]
            self.assertTrue(
                self.manager.store.get_value(tree_iter, self.manager.COL_SYMLINK)
            )
        self.assertTrue(
            self.manager.store.get_value(
                rows["directory-link"], self.manager.COL_DIRECTORY
            )
        )
        directory_link_icon = self.manager.store.get_value(
            rows["directory-link"], self.manager.COL_ICON
        )
        self.assertIsInstance(directory_link_icon, Gio.EmblemedIcon)
        self.assertEqual(
            directory_link_icon.get_icon().get_names()[0], "folder"
        )
        file_link_icon = self.manager.store.get_value(
            rows["file-link"], self.manager.COL_ICON
        )
        self.assertIsInstance(file_link_icon, Gio.EmblemedIcon)
        self.assertNotEqual(
            file_link_icon.get_icon().get_names()[0], "inode-symlink"
        )
        self.manager.tree.expand_row(
            self.manager.store.get_path(rows["directory-link"]), False
        )
        self._wait_for_path("directory-link")
        child = self.manager.store.iter_children(rows["directory-link"])
        self.assertEqual(
            self.manager.store.get_value(child, self.manager.COL_PATH),
            "directory-link/app.py",
        )
        for name in ("file-link", "broken-link", "external-link"):
            tree_iter = rows[name]
            self.assertFalse(
                self.manager.store.get_value(tree_iter, self.manager.COL_DIRECTORY)
            )

    def test_broken_and_external_symlinks_have_no_actions(self) -> None:
        """Unsafe links have no actions and a broken link uses the error icon."""

        rows = {name: tree_iter for name, tree_iter in self._root_rows()}
        for name in ("broken-link", "external-link"):
            tree_iter = rows[name]
            self.assertTrue(
                self.manager.store.get_value(tree_iter, self.manager.COL_BLOCKED)
            )
            self.manager.tree.set_cursor(self.manager.store.get_path(tree_iter))
            self.assertIsNone(self.previewed)
            with patch.object(self.manager, "_show_entry_menu") as show_menu:
                self.assertTrue(self.manager._on_popup_menu(self.manager.tree))
                show_menu.assert_not_called()
            self.assertTrue(self._press(Gdk.KEY_Delete))
            self.assertIsNone(self.deleted)
        broken_icon = self.manager.store.get_value(
            rows["broken-link"], self.manager.COL_ICON
        )
        self.assertIsInstance(broken_icon, Gio.ThemedIcon)
        self.assertEqual(broken_icon.get_names()[0], "dialog-error")

    def test_shortcuts_dispatch_file_actions_and_directory_mutations(self) -> None:
        """V/M/E stay file-only while R/T/Delete target their valid entries."""

        file_iter = next(
            tree_iter
            for name, tree_iter in self._root_rows()
            if name == "normal.txt"
        )
        self.manager.tree.set_cursor(self.manager.store.get_path(file_iter))
        self.assertEqual(self.previewed, "normal.txt")
        self.assertTrue(self._press(Gdk.KEY_v))
        self.assertTrue(self._press(Gdk.KEY_m))
        self.assertTrue(self._press(Gdk.KEY_e))
        self.assertTrue(self._press(Gdk.KEY_r))
        self.assertFalse(self._press(Gdk.KEY_t))
        self.assertTrue(self._press(Gdk.KEY_Delete))
        self.assertEqual(self.viewed, "normal.txt")
        self.assertEqual(self.edited_internal, "normal.txt")
        self.assertEqual(self.edited_external, "normal.txt")
        self.assertEqual(self.renamed, "normal.txt")
        self.assertEqual(self.deleted, "normal.txt")

        directory_iter = next(
            tree_iter for name, tree_iter in self._root_rows() if name == "src"
        )
        self.manager.tree.set_cursor(self.manager.store.get_path(directory_iter))
        self.assertIsNone(self.previewed)
        self.assertTrue(self._press(Gdk.KEY_r))
        self.assertTrue(self._press(Gdk.KEY_t))
        self.assertTrue(self._press(Gdk.KEY_Delete))
        self.assertEqual(self.renamed, "src")
        self.assertEqual(self.terminal_directory, "src")
        self.assertEqual(self.deleted, "src")

    def test_creation_actions_use_directory_or_file_parent(self) -> None:
        """Toolbar and context creation resolve the target parent consistently."""

        directory_iter = next(
            tree_iter for name, tree_iter in self._root_rows() if name == "src"
        )
        self.manager.tree.set_cursor(self.manager.store.get_path(directory_iter))
        self.manager._on_new_file_clicked(self.manager.new_file_button)
        self.assertEqual(self.new_file_parent, "src")

        file_iter = next(
            tree_iter for name, tree_iter in self._root_rows() if name == "normal.txt"
        )
        self.manager.tree.set_cursor(self.manager.store.get_path(file_iter))
        self.manager._on_new_directory_clicked(self.manager.new_directory_button)
        self.assertEqual(self.new_directory_parent, "")

        self.manager.context_relative = None
        self.manager.context_directory = False
        self.manager._on_context_new_file(Gtk.MenuItem())
        self.assertEqual(self.new_file_parent, "")

    def test_rename_state_follows_a_directory_subtree(self) -> None:
        """Renamed directories retain expanded descendants and requested cursor."""

        self.manager.expanded_paths = {"src", "src/nested", "unrelated"}
        self.manager.relocate_path("src", "source")
        self.assertEqual(
            self.manager.expanded_paths,
            {"source", "source/nested", "unrelated"},
        )
        self.assertEqual(self.manager.pending_cursor_path, "source")

    def test_switching_projects_reuses_the_completed_tree_model(self) -> None:
        """A previously opened repository returns without clearing its loaded rows."""

        first_store = self.manager.store
        first_names = self._root_names()
        self.manager.set_active(False)
        with tempfile.TemporaryDirectory() as other_directory:
            Path(other_directory, "other.txt").write_text("other", encoding="utf-8")
            self.manager.set_project(
                "other",
                other_directory,
                {"show_hidden": False, "show_excluded": False, "expanded_paths": []},
                set(),
                True,
            )
            self.assertIsNot(self.manager.store, first_store)
            self.manager.set_project(
                "project",
                str(self.root),
                {"show_hidden": False, "show_excluded": False, "expanded_paths": []},
                {"ignored.txt"},
                True,
            )
        self.assertIs(self.manager.store, first_store)
        self.assertEqual(self._root_names(), first_names)
        self.manager.set_active(True)
        self.assertIs(self.manager.store, first_store)

    def test_nested_only_watcher_change_reloads_workspace_symlinks(self) -> None:
        """Losing root watcher coverage invalidates cache and reveals a new link."""

        self.manager.set_active(False)
        with tempfile.TemporaryDirectory() as other_directory:
            self.manager.set_project(
                "other",
                other_directory,
                {"show_hidden": False, "show_excluded": False, "expanded_paths": []},
                set(),
                True,
            )
            (self.root / "late-link").symlink_to("normal.txt")
            self.manager.set_project(
                "project",
                str(self.root),
                {"show_hidden": False, "show_excluded": False, "expanded_paths": []},
                {"ignored.txt"},
                False,
            )
        self.assertTrue(self.manager.dirty)
        self.manager.set_active(True)
        self._wait_for_path("")
        self.assertIn("late-link", self._root_names())

    def test_file_icons_use_colored_gio_choices_with_four_pixel_spacing(self) -> None:
        """The file column uses legible MIME icons and keeps the required gap."""

        column = self.manager.tree.get_columns()[0]
        self.assertEqual(column.get_spacing(), 4)
        icon_renderer, text_renderer = column.get_cells()
        self.assertEqual(
            icon_renderer.get_property("stock-size"), Gtk.IconSize.LARGE_TOOLBAR
        )
        self.assertEqual(icon_renderer.get_property("ypad"), 2)
        # 2026-08-16: una scala neutra rende la misura in punti scelta nelle
        # impostazioni esatta, invece di applicare un moltiplicatore nascosto.
        self.assertAlmostEqual(text_renderer.get_property("scale"), 1.0)
        self.assertEqual(text_renderer.get_property("ypad"), 2)
        file_iter = next(
            tree_iter
            for name, tree_iter in self._root_rows()
            if name == "normal.txt"
        )
        icon = self.manager.store.get_value(file_iter, self.manager.COL_ICON)
        self.assertIsInstance(icon, Gio.Icon)
        if isinstance(icon, Gio.ThemedIcon):
            self.assertFalse(icon.get_names()[0].endswith("-symbolic"))

    def test_expand_label_remains_visible_on_the_single_toolbar_row(self) -> None:
        """The fixed expand label shares one row with both visibility filters."""

        toolbar = self.manager.expand_button.get_parent()
        self.assertEqual(toolbar.get_orientation(), Gtk.Orientation.HORIZONTAL)
        self.assertEqual(
            toolbar.get_children(),
            [
                self.manager.new_file_button,
                self.manager.new_directory_button,
                self.manager.expand_button,
                self.manager.hidden_check,
                self.manager.excluded_check,
            ],
        )
        self.assertEqual(self.manager.new_file_button.get_child().get_spacing(), 4)
        self.assertEqual(self.manager.new_directory_button.get_child().get_spacing(), 4)
        self.assertEqual(self.manager.new_file_button.get_accessible().get_name(), "+ File")
        self.assertEqual(
            self.manager.new_directory_button.get_accessible().get_name(),
            "+ Folder",
        )
        expand_content = self.manager.expand_button.get_child()
        self.assertEqual(expand_content.get_spacing(), 4)
        self.assertIs(expand_content.get_children()[0], self.manager.expand_icon)
        self.assertEqual(self.manager.expand_label.get_text(), "Expand")
        width_request, _height_request = self.manager.expand_button.get_size_request()
        self.assertGreaterEqual(width_request, 100)
        self.assertTrue(self.manager.expand_label.get_visible())

    def test_first_click_and_filter_rebuild_keep_directory_expanded(self) -> None:
        """One left click opens a folder and filters preserve that expansion."""

        window = Gtk.Window()
        window.add(self.manager)
        window.show_all()
        self.addCleanup(window.destroy)
        while Gtk.events_pending():
            Gtk.main_iteration_do(False)
        source_iter = next(
            tree_iter for name, tree_iter in self._root_rows() if name == "src"
        )
        source_path = self.manager.store.get_path(source_iter)
        column = self.manager.tree.get_columns()[0]
        area = self.manager.tree.get_cell_area(source_path, column)
        event = SimpleNamespace(
            button=1,
            type=Gdk.EventType.BUTTON_PRESS,
            x=area.x + max(area.width // 2, 1),
            y=area.y + max(area.height // 2, 1),
        )
        self.assertTrue(self.manager._on_button_press(self.manager.tree, event))
        self._wait_for_path("src")
        self.assertIs(window.get_focus(), self.manager.tree)
        self.assertTrue(self._press(Gdk.KEY_Delete))
        self.assertEqual(self.deleted, "src")
        self.assertIn("src", self.manager.expanded_paths)
        self.assertTrue(self.manager.tree.row_expanded(source_path))

        self.manager.hidden_check.set_active(True)
        self._wait_for_path("")
        self._wait_for_path("src")
        restored_iter = next(
            tree_iter for name, tree_iter in self._root_rows() if name == "src"
        )
        restored_path = self.manager.store.get_path(restored_iter)
        self.assertIn("src", self.manager.expanded_paths)
        self.assertTrue(self.manager.tree.row_expanded(restored_path))

        self.manager.excluded_check.set_active(True)
        self._wait_for_path("")
        self._wait_for_path("src")
        restored_iter = next(
            tree_iter for name, tree_iter in self._root_rows() if name == "src"
        )
        self.assertTrue(
            self.manager.tree.row_expanded(self.manager.store.get_path(restored_iter))
        )

    def test_activation_restores_tree_page_after_show_all_side_effect(self) -> None:
        """An already configured project cannot remain on the inactive empty page."""

        self.manager.content_stack.set_visible_child_name("empty")
        self.manager.set_active(True)
        self.assertEqual(self.manager.content_stack.get_visible_child_name(), "tree")

    def _root_rows(self) -> list[tuple[str, Gtk.TreeIter]]:
        """Return top-level names and iterators for cursor-oriented assertions."""

        rows: list[tuple[str, Gtk.TreeIter]] = []
        tree_iter = self.manager.store.get_iter_first()
        while tree_iter:
            rows.append(
                (
                    self.manager.store.get_value(tree_iter, self.manager.COL_NAME),
                    tree_iter,
                )
            )
            tree_iter = self.manager.store.iter_next(tree_iter)
        return rows

    def test_expand_all_stops_at_excluded_roots(self) -> None:
        """Global expansion recursively loads source dirs but skips dependency trees."""

        self.manager.excluded_check.set_active(True)
        self._wait_for_path("")
        self.manager._on_expand_all_clicked(self.manager.expand_button)
        self._wait_for_path("src")
        deadline = time.monotonic() + 2
        context = GLib.MainContext.default()
        while not self.manager.expand_all_complete and time.monotonic() < deadline:
            context.iteration(True)
        self.assertTrue(self.manager.expand_all_complete)
        self.assertNotIn("node_modules", self.manager.loaded_paths)
        node_iter = next(
            tree_iter
            for name, tree_iter in self._root_rows()
            if name == "node_modules"
        )
        self.assertFalse(self.manager.tree.row_expanded(self.manager.store.get_path(node_iter)))
        self.assertEqual(self.manager.expand_label.get_text(), "Collapse")


if __name__ == "__main__":
    unittest.main()
