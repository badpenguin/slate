"""GTK tests for SLATE's read-only internal file preview."""

import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("GtkSource", "4")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gdk, GdkPixbuf, GLib, Gtk

from slate.preview import FilePreview
from slate.scm.base import FileStatus


class FilePreviewTest(unittest.TestCase):
    """Verify diff decoration and source-language selection without disk mutation."""

    @classmethod
    def setUpClass(cls) -> None:
        """Require the display server shared by the final GTK test session."""

        initialized, _arguments = Gtk.init_check(None)
        if not initialized:
            raise unittest.SkipTest("display GTK non disponibile")

    def setUp(self) -> None:
        """Create a preview with an inert named close callback."""

        self.preview = FilePreview(self._ignore_close)

    def _ignore_close(self) -> None:
        """Accept close signals that are irrelevant to rendering tests."""

    def test_diff_lines_receive_semantic_tags(self) -> None:
        """Added and removed rows expose their distinct GtkTextBuffer tags."""

        self.preview._set_diff_text("@@ -1 +1 @@\n-old\n+new\n")
        buffer = self.preview.view.get_buffer()
        text = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True)
        self.assertEqual(text, "@@ -1 +1 @@\n-old\n+new\n")
        removed_tags = {
            tag.get_property("name") for tag in buffer.get_iter_at_line(1).get_tags()
        }
        added_tags = {
            tag.get_property("name") for tag in buffer.get_iter_at_line(2).get_tags()
        }
        self.assertIn("diff-removed", removed_tags)
        self.assertIn("diff-added", added_tags)

    def test_diff_rows_map_to_current_working_file_lines(self) -> None:
        """Context, additions and removals resolve without another SCM command."""

        self.preview._set_diff_text(
            "diff --git a/file.py b/file.py\n"
            "--- a/file.py\n"
            "+++ b/file.py\n"
            "@@ -10,3 +20,4 @@ function\n"
            " context\n"
            "-removed\n"
            "+replacement\n"
            "+extra\n"
            " tail\n"
        )
        expected = (None, None, None, 20, 20, 21, 21, 22, 23)
        for line_index, target in enumerate(expected):
            self.preview.buffer.place_cursor(
                self.preview.buffer.get_iter_at_line(line_index)
            )
            self.assertEqual(self.preview.edit_line_at_cursor(), target)

    def test_edit_change_action_forwards_only_mappable_diff_rows(self) -> None:
        """The contextual action cannot open metadata lines at an arbitrary row."""

        actions: list[str] = []
        preview = FilePreview(self._ignore_close, on_action=actions.append)
        preview._set_diff_text("--- a/file.py\n+++ b/file.py\n@@ -1 +1 @@\n-old\n+new\n")
        preview.buffer.place_cursor(preview.buffer.get_iter_at_line(0))
        preview._on_edit_change_activate(Gtk.MenuItem())
        preview.buffer.place_cursor(preview.buffer.get_iter_at_line(4))
        preview._on_edit_change_activate(Gtk.MenuItem())
        self.assertEqual(actions, ["edit-internal"])

    def test_preview_search_uses_shared_ctrl_f_and_f3_shortcuts(self) -> None:
        """Local preview search opens, finds and closes without reloading content."""

        self.preview._set_source_text("first needle\nsecond needle\n", "sample.txt")
        open_event = SimpleNamespace(
            keyval=Gdk.KEY_f,
            state=Gdk.ModifierType.CONTROL_MASK,
        )
        self.assertTrue(self.preview._on_key_press(self.preview.view, open_event))
        self.assertTrue(self.preview.search_bar.get_reveal_child())
        self.preview.search_bar.entry.set_text("needle")
        self.preview.search_bar._on_search_changed(self.preview.search_bar.entry)
        next_event = SimpleNamespace(keyval=Gdk.KEY_F3, state=Gdk.ModifierType(0))
        self.assertTrue(self.preview._on_key_press(self.preview.view, next_event))
        bounds = self.preview.buffer.get_selection_bounds()
        self.assertEqual(
            self.preview.buffer.get_text(bounds[0], bounds[1], True), "needle"
        )
        close_event = SimpleNamespace(
            keyval=Gdk.KEY_Escape, state=Gdk.ModifierType(0)
        )
        self.assertTrue(self.preview._on_key_press(self.preview.view, close_event))
        self.assertFalse(self.preview.search_bar.get_reveal_child())

    def test_preview_definition_uses_clicked_symbol_and_source_context(self) -> None:
        """Read-only previews can dispatch the same definition lookup as editors."""

        requested = MagicMock()
        preview = FilePreview(self._ignore_close, requested)
        preview.current_root = "/tmp/project"
        preview.current_path = "src/app.php"
        preview.buffer.set_text("+function deploy() {}\n")
        preview.buffer.place_cursor(preview.buffer.get_iter_at_offset(12))

        self.assertTrue(preview.go_to_definition())
        requested.assert_called_once_with(
            "/tmp/project", "src/app.php", "deploy"
        )

    def test_focused_preview_forwards_all_file_action_shortcuts(self) -> None:
        """V, E, M and D remain available after the preview receives focus."""

        actions: list[str] = []
        preview = FilePreview(self._ignore_close, on_action=actions.append)
        preview.current_path = "src/app.php"
        for keyval in (Gdk.KEY_v, Gdk.KEY_e, Gdk.KEY_m, Gdk.KEY_d):
            event = SimpleNamespace(
                keyval=keyval, state=Gdk.ModifierType(0)
            )
            self.assertTrue(preview._on_key_press(preview.view, event))
        self.assertEqual(
            actions, ["view", "edit-internal", "edit-external", "meld"]
        )

    def test_modified_preview_exposes_both_editor_modes(self) -> None:
        """A diff context menu offers direct editing and the companion dialog."""

        self.preview._set_diff_text("@@ -1 +1 @@\n-old\n+new\n")
        menu = Gtk.Menu()
        self.preview._on_populate_popup(self.preview.view, menu)
        labels = [
            item.get_label()
            for item in menu.get_children()
            if isinstance(item, Gtk.MenuItem)
        ]
        self.assertIn("Edit this change", labels)
        self.assertIn("Edit with diff…", labels)

    def test_modified_preview_does_not_consume_shifted_global_shortcuts(self) -> None:
        """Shift-modified keys remain available to GTK and desktop accelerators."""

        actions: list[str] = []
        preview = FilePreview(self._ignore_close, on_action=actions.append)
        preview.current_path = "src/app.py"
        event = SimpleNamespace(
            keyval=Gdk.KEY_e,
            state=Gdk.ModifierType.SHIFT_MASK,
        )
        self.assertFalse(preview._on_key_press(preview.view, event))
        self.assertEqual(actions, [])

    def test_preview_uses_a_passive_focus_owner_for_special_keys(self) -> None:
        """GtkSourceView cannot consume unhandled XF86 keys after a preview click."""

        self.assertTrue(self.preview.get_can_focus())
        self.assertFalse(self.preview.view.get_can_focus())
        event = SimpleNamespace(
            keyval=Gdk.keyval_from_name("XF86Launch5"),
            state=Gdk.ModifierType.MOD2_MASK,
        )
        self.assertFalse(self.preview._on_key_press(self.preview, event))

    def test_preview_context_delete_forwards_the_current_file(self) -> None:
        """The preview menu exposes deletion through the owner's confirmed flow."""

        actions: list[str] = []
        preview = FilePreview(self._ignore_close, on_action=actions.append)
        preview.current_path = "missing.php"
        menu = Gtk.Menu()

        preview._on_populate_popup(preview.view, menu)
        self.assertFalse(menu.get_take_focus())
        delete_item = next(
            child
            for child in menu.get_children()
            if isinstance(child, Gtk.MenuItem) and child.get_label() == "Delete"
        )
        delete_item.activate()

        self.assertEqual(actions, ["delete"])

    def test_new_python_file_uses_syntax_highlighting(self) -> None:
        """A recognizable new-file extension selects a GtkSource language."""

        self.preview._set_source_text("print('SLATE')\n", "example.py")
        buffer = self.preview.view.get_buffer()
        self.assertIsNotNone(buffer.get_language())
        self.assertTrue(buffer.get_highlight_syntax())

    def test_markdown_uses_gfm_html_without_remote_images(self) -> None:
        """Markdown rendering keeps GFM tables while omitting every image source."""

        markdown_page = Gtk.Box()
        markdown_view = MagicMock()

        def ensure_markdown_view() -> None:
            """Install an inert stack child instead of starting a WebKit process."""

            self.preview.content_stack.add_named(markdown_page, "markdown")
            markdown_page.show()
            self.preview.markdown_view = markdown_view

        with patch.object(
            self.preview,
            "_ensure_markdown_view",
            side_effect=ensure_markdown_view,
        ):
            self.preview._set_markdown_text(
                "# Title\n\n| A | B |\n|---|---|\n| 1 | 2 |\n\n"
                "- [ ] Pending\n- [x] Done\n\n"
                "![remote](https://example.invalid/image.png)\n",
                "README.md",
            )

        document = markdown_view.load_html.call_args.args[0]
        self.assertIn("<table>", document)
        self.assertEqual(document.count('class="task-list-item"'), 2)
        self.assertIn('<ul class="task-list">', document)
        self.assertIn('checked="" disabled=""', document)
        self.assertIn("body>ul,body>ol{padding-left:1.4em}", document)
        self.assertIn("li{margin-bottom:.5rem}", document)
        self.assertIn("padding-left:1.45em", document)
        self.assertIn("[Image omitted]", document)
        self.assertNotIn("example.invalid", document)
        self.assertEqual(
            self.preview.content_stack.get_visible_child_name(), "markdown"
        )

    def test_render_modes_keep_one_view_buffer(self) -> None:
        """Preview refreshes never replace the buffer owned by the text layout."""

        buffer = self.preview.view.get_buffer()
        self.preview._set_source_text("value = 1\n", "example.py")
        self.assertIs(self.preview.view.get_buffer(), buffer)
        self.preview._set_diff_text("-old\n+new\n")
        self.assertIs(self.preview.view.get_buffer(), buffer)
        self.assertIsNone(buffer.get_language())
        self.preview._set_plain_text("Caricamento…")
        self.assertIs(self.preview.view.get_buffer(), buffer)

    def test_normal_project_file_loads_without_scm_status(self) -> None:
        """The file-manager preview reads and highlights a clean project file."""

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "clean.py"
            path.write_text("value = 42\n", encoding="utf-8")
            self.preview.show_file(directory, "clean.py")
            deadline = time.monotonic() + 2
            context = GLib.MainContext.default()
            while self.preview.file_cancellable is not None and time.monotonic() < deadline:
                context.iteration(True)
            buffer = self.preview.view.get_buffer()
            text = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True)
            self.assertEqual(text, "value = 42\n")
            self.assertIsNotNone(buffer.get_language())

    def test_project_image_loads_inline_without_enlargement(self) -> None:
        """A raster file uses the image surface and retains its smaller dimensions."""

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "preview.png"
            source = GdkPixbuf.Pixbuf.new(
                GdkPixbuf.Colorspace.RGB, True, 8, 32, 16
            )
            source.fill(0x336699FF)
            source.savev(str(path), "png", [], [])

            self.preview.set_no_show_all(False)
            self.preview.show_all()
            self.preview.show_file(directory, "preview.png")
            deadline = time.monotonic() + 2
            context = GLib.MainContext.default()
            while (
                self.preview.file_cancellable is not None
                and time.monotonic() < deadline
            ):
                context.iteration(True)

            self.preview._fit_image(100, 100)
            displayed = self.preview.image.get_pixbuf()
            self.assertIsNotNone(displayed)
            self.assertEqual((displayed.get_width(), displayed.get_height()), (32, 16))
            self.assertEqual(
                self.preview.content_stack.get_visible_child_name(), "image"
            )
            self.assertEqual(
                self.preview.title.get_text(),
                f"preview.png — 32×16 px — {GLib.format_size(path.stat().st_size)}",
            )

    def test_large_image_refits_when_the_preview_area_changes(self) -> None:
        """Image rendering follows the current viewport rather than a fixed cap."""

        source = GdkPixbuf.Pixbuf.new(
            GdkPixbuf.Colorspace.RGB, True, 8, 400, 200
        )
        self.preview.image_source_pixbuf = source

        self.preview._fit_image(204, 104)

        displayed = self.preview.image.get_pixbuf()
        self.assertEqual(
            (displayed.get_width(), displayed.get_height()), (200, 100)
        )

    def test_modified_image_uses_current_file_instead_of_binary_diff(self) -> None:
        """Revision image rows bypass SCM diff generation and use the image loader."""

        status = FileStatus("assets/logo.png", "modified")
        scm = SimpleNamespace(root="/tmp/repo", environment={})
        with patch.object(self.preview, "_load_working_file") as load:
            self.preview.show_status("/tmp/repo", scm, status)

        load.assert_called_once_with(
            "/tmp/repo", "assets/logo.png", self.preview.request_serial
        )

    def test_modified_markdown_uses_rendered_working_file(self) -> None:
        """Revision Markdown rows bypass the textual diff for visual rendering."""

        status = FileStatus("README.md", "modified")
        scm = SimpleNamespace(root="/tmp/repo", environment={})
        with patch.object(self.preview, "_load_working_file") as load:
            self.preview.show_status("/tmp/repo", scm, status)

        load.assert_called_once_with(
            "/tmp/repo", "README.md", self.preview.request_serial
        )

    def test_removed_markdown_renders_parent_revision(self) -> None:
        """A removed Markdown file renders the completed parent-revision content."""

        result = SimpleNamespace(ok=True, stdout="# Removed\n", stderr="")
        self.preview.current_path = "removed.md"
        self.preview.request_serial = 7
        with patch.object(self.preview, "_set_markdown_text") as render:
            self.preview._on_base_loaded(7, "removed.md", result)

        render.assert_called_once_with("# Removed\n", "removed.md")

    def test_missing_image_does_not_start_any_preview_read(self) -> None:
        """A missing image never reads either its absent path or an SCM base."""

        status = FileStatus("assets/missing.png", "missing")
        scm = SimpleNamespace(
            root="/tmp/repo",
            environment={},
            base_argv=MagicMock(return_value=["hg", "cat", "assets/missing.png"]),
        )
        with patch.object(self.preview, "_load_working_file") as load:
            with patch("slate.preview.run_async", return_value=MagicMock()) as run:
                self.preview.show_status("/tmp/repo", scm, status)

        load.assert_not_called()
        scm.base_argv.assert_not_called()
        run.assert_not_called()
        self.assertEqual(
            self.preview.buffer.get_text(
                self.preview.buffer.get_start_iter(),
                self.preview.buffer.get_end_iter(),
                True,
            ),
            "The file is missing from the working copy.",
        )

    def test_moved_file_uses_one_rename_aware_diff(self) -> None:
        """Move preview labels both endpoints and requests the Git-style patch."""

        scm = SimpleNamespace(
            root="/tmp/repo",
            environment={"HGPLAIN": "1"},
            preview_move_diff_argv=MagicMock(return_value=["hg", "diff", "--git"]),
        )
        moved = FileStatus("new.py", "moved", source_path="old.py")
        with patch("slate.preview.run_async", return_value=MagicMock()) as run:
            self.preview.show_status("/tmp/repo", scm, moved)
        self.assertEqual(self.preview.title.get_text(), "old.py → new.py")
        scm.preview_move_diff_argv.assert_called_once_with("old.py", "new.py")
        run.assert_called_once()


if __name__ == "__main__":
    unittest.main()
