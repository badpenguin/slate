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
from gi.repository import GLib, Gtk

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

    def test_new_python_file_uses_syntax_highlighting(self) -> None:
        """A recognizable new-file extension selects a GtkSource language."""

        self.preview._set_source_text("print('SLATE')\n", "example.py")
        buffer = self.preview.view.get_buffer()
        self.assertIsNotNone(buffer.get_language())
        self.assertTrue(buffer.get_highlight_syntax())

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
