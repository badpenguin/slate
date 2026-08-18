"""GTK tests for persistent internal GtkSource editor behavior."""

import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import gi

gi.require_version("Gdk", "3.0")
gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, GLib, Gtk

from slate.editor import EditorDocument, EditorWorkspace


class EditorTest(unittest.TestCase):
    """Verify loading, saving, conflicts and central stack coordination."""

    @classmethod
    def setUpClass(cls) -> None:
        """Require the GTK display shared by the integration test session."""

        initialized, _arguments = Gtk.init_check(None)
        if not initialized:
            raise unittest.SkipTest("display GTK non disponibile")

    def setUp(self) -> None:
        """Create a temporary UTF-8 Markdown file and inert editor callbacks."""

        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.path = self.root / "TODO.md"
        self.path.write_text("# TODO\n", encoding="utf-8")
        self.states: list[tuple[bool, bool]] = []
        self.removed: list[str] = []

    def tearDown(self) -> None:
        """Release the temporary project after each editor test."""

        self.temporary.cleanup()

    def _state_changed(self, editor: EditorDocument) -> None:
        """Record dirty and attention changes published by a document."""

        self.states.append((editor.dirty, editor.attention))

    def _removed(self, _editor: EditorDocument, message: str) -> None:
        """Record missing-file notifications without constructing a window."""

        self.removed.append(message)

    def _wait_until(self, predicate, timeout: float = 2.0) -> None:
        """Iterate GLib until an asynchronous editor condition becomes true."""

        deadline = time.monotonic() + timeout
        context = GLib.MainContext.default()
        while not predicate() and time.monotonic() < deadline:
            while context.pending():
                context.iteration(False)
            time.sleep(0.005)
        self.assertTrue(predicate())

    def _document(self) -> EditorDocument:
        """Create and completely load the representative Markdown document."""

        editor = EditorDocument(
            "project",
            str(self.root),
            "TODO.md",
            11,
            self._state_changed,
            self._removed,
        )
        self._wait_until(lambda: not editor.loading and editor.etag is not None)
        return editor

    def test_utf8_document_highlights_edits_and_saves_atomically(self) -> None:
        """A supported source becomes dirty and Ctrl+S's primitive writes it."""

        editor = self._document()
        self.assertIsNotNone(editor.buffer.get_language())
        self.assertFalse(editor.save_button.get_sensitive())
        self.assertFalse(editor.undo_button.get_sensitive())
        self.assertFalse(editor.redo_button.get_sensitive())
        editor.buffer.insert(editor.buffer.get_end_iter(), "- prova\n")
        self.assertTrue(editor.dirty)
        self.assertTrue(editor.save_button.get_sensitive())
        self.assertTrue(editor.undo_button.get_sensitive())
        self.assertFalse(editor.redo_button.get_sensitive())
        editor.undo()
        self.assertTrue(editor.redo_button.get_sensitive())
        editor.redo()
        completed: list[bool] = []
        editor.save(completed.append)
        self._wait_until(lambda: bool(completed))
        self.assertEqual(completed, [True])
        self.assertFalse(editor.dirty)
        self.assertFalse(editor.save_button.get_sensitive())
        self.assertEqual(self.path.read_text(encoding="utf-8"), "# TODO\n- prova\n")
        editor.close()

    def test_external_clean_reload_is_silent(self) -> None:
        """An external write silently reloads a buffer without local edits."""

        editor = self._document()
        self.path.write_text("# Esterno\n", encoding="utf-8")
        editor._handle_external_change()
        self._wait_until(lambda: not editor.loading)
        text = editor.buffer.get_text(
            editor.buffer.get_start_iter(), editor.buffer.get_end_iter(), True
        )
        self.assertEqual(text, "# Esterno\n")
        self.assertFalse(editor.attention)
        self.assertFalse(editor.external_conflict)
        self.assertFalse(editor.info_bar.get_visible())
        editor.close()

    def test_dirty_external_change_requires_explicit_version_choice(self) -> None:
        """Dirty content survives external writes until the user chooses a side."""

        editor = self._document()
        editor.buffer.set_text("# Locale\n")
        self.path.write_text("# Esterno\n", encoding="utf-8")
        editor._handle_external_change()
        self.assertTrue(editor.external_conflict)
        self.assertTrue(editor.attention)
        editor._on_info_response(editor.info_bar, editor.RESPONSE_MINE)
        self.assertFalse(editor.external_conflict)
        self.assertTrue(editor.force_next_save)
        self.assertFalse(editor.info_bar.get_revealed())
        self.assertFalse(editor.info_bar.get_visible())
        completed: list[bool] = []
        editor.save(completed.append)
        self._wait_until(lambda: bool(completed))
        self.assertEqual(completed, [True])
        self.assertEqual(self.path.read_text(encoding="utf-8"), "# Locale\n")
        editor.close()

    def test_disk_conflict_choice_hides_notice_after_reload(self) -> None:
        """Choosing disk content closes the conflict bar after the async reload."""

        editor = self._document()
        editor.buffer.set_text("# Locale\n")
        self.path.write_text("# Disco\n", encoding="utf-8")
        editor._handle_external_change()
        editor._on_info_response(editor.info_bar, editor.RESPONSE_DISK)

        def reload_finished() -> bool:
            """Report when the disk-conflict reload has completed."""

            return not editor.loading

        self._wait_until(reload_finished)
        text = editor.buffer.get_text(
            editor.buffer.get_start_iter(), editor.buffer.get_end_iter(), True
        )
        self.assertEqual(text, "# Disco\n")
        self.assertFalse(editor.external_conflict)
        self.assertFalse(editor.attention)
        self.assertFalse(editor.info_bar.get_revealed())
        self.assertFalse(editor.info_bar.get_visible())
        editor.close()

    def test_workspace_deduplicates_rows_and_limits_shortcuts_to_editor(self) -> None:
        """One file maps to one stack child and shortcuts ignore terminals."""

        persisted = MagicMock()
        workspace = EditorWorkspace(
            Gtk.Box(),
            10,
            persisted,
            MagicMock(),
            MagicMock(),
            MagicMock(),
        )
        self.assertIsInstance(workspace, Gtk.Stack)
        self.assertNotIsInstance(workspace, Gtk.Notebook)
        first = workspace.open_file("project", str(self.root), "TODO.md")
        second = workspace.open_file("project", str(self.root), "TODO.md")
        self.assertIs(first, second)
        self.assertEqual(len(workspace.editors), 1)
        self.assertIs(workspace.get_visible_child(), first)
        save_event = SimpleNamespace(
            keyval=Gdk.KEY_s,
            state=Gdk.ModifierType.CONTROL_MASK,
        )
        self.assertTrue(workspace.handle_key(save_event))
        workspace.show_terminal()
        self.assertFalse(workspace.handle_key(save_event))
        workspace.shutdown()

    def test_workspace_rekeys_dirty_editor_after_file_rename(self) -> None:
        """A renamed open file keeps its buffer and receives the new identity."""

        workspace = EditorWorkspace(
            Gtk.Box(),
            10,
            MagicMock(),
            MagicMock(),
            MagicMock(),
            MagicMock(),
        )
        editor = workspace.open_file("project", str(self.root), "TODO.md")
        self._wait_until(lambda: not editor.loading and editor.etag is not None)
        editor.buffer.set_text("# Non salvato\n")
        renamed = self.root / "TASKS.md"
        self.path.rename(renamed)
        workspace.relocate_path("project", "TODO.md", "TASKS.md")
        self.assertNotIn(("project", "TODO.md"), workspace.editors)
        self.assertIs(
            workspace.editors[("project", "TASKS.md")].document, editor
        )
        self.assertEqual(editor.relative_path, "TASKS.md")
        self.assertEqual(editor.path, str(renamed))
        self.assertTrue(editor.dirty)
        text = editor.buffer.get_text(
            editor.buffer.get_start_iter(), editor.buffer.get_end_iter(), True
        )
        self.assertEqual(text, "# Non salvato\n")
        workspace.shutdown()

    def test_restore_keeps_editor_entries_lazy_until_selected(self) -> None:
        """Startup restores rows and active identity without touching documents."""

        persisted = MagicMock()
        terminal_page = Gtk.Box()
        workspace = EditorWorkspace(
            terminal_page,
            10,
            persisted,
            MagicMock(),
            MagicMock(),
            MagicMock(),
        )
        state = {
            "tabs": [{"project": "project", "path": "TODO.md"}],
            "active_tab": {"project": "project", "path": "TODO.md"},
        }
        workspace.restore(state, [{"name": "project", "path": str(self.root)}])
        entry = workspace.editors[("project", "TODO.md")]
        self.assertIsNone(entry.document)
        self.assertEqual(workspace.get_children(), [terminal_page])
        self.assertEqual(workspace.serialized_state(), (state["tabs"], state["active_tab"]))

        self.assertTrue(workspace.show_editor("project", "TODO.md"))
        self.assertIsNotNone(entry.document)
        editor = entry.document
        self.assertIs(workspace.current_editor(), editor)
        self._wait_until(lambda: editor is not None and editor.etag is not None)
        workspace.shutdown()

    def test_missing_restored_file_is_removed_only_after_selection(self) -> None:
        """A stale lazy row survives startup and reports failure on first click."""

        error = MagicMock()
        workspace = EditorWorkspace(
            Gtk.Box(),
            10,
            MagicMock(),
            error,
            MagicMock(),
            MagicMock(),
        )
        reference = ("project", "MISSING.md")
        workspace.restore(
            {
                "tabs": [{"project": reference[0], "path": reference[1]}],
                "active_tab": None,
            },
            [{"name": "project", "path": str(self.root)}],
        )
        self.assertIn(reference, workspace.editors)
        self.assertIsNone(workspace.editors[reference].document)

        self.assertTrue(workspace.show_editor(*reference))
        self._wait_until(lambda: reference not in workspace.editors)
        error.assert_called_once()
        workspace.shutdown()

    def test_unloaded_editor_closes_and_relocates_without_materialization(self) -> None:
        """Row-only close and rename operations never construct GtkSource widgets."""

        workspace = EditorWorkspace(
            Gtk.Box(),
            10,
            MagicMock(),
            MagicMock(),
            MagicMock(),
            MagicMock(),
        )
        state = {
            "tabs": [
                {"project": "project", "path": "TODO.md"},
                {"project": "project", "path": "SECOND.md"},
            ],
            "active_tab": None,
        }
        workspace.restore(state, [{"name": "project", "path": str(self.root)}])
        workspace.relocate_path("project", "TODO.md", "TASKS.md")
        renamed = workspace.editors[("project", "TASKS.md")]
        self.assertIsNone(renamed.document)
        self.assertTrue(workspace.request_close_reference(("project", "TASKS.md")))
        self.assertNotIn(("project", "TASKS.md"), workspace.editors)
        self.assertIsNone(workspace.editors[("project", "SECOND.md")].document)
        workspace.shutdown()


if __name__ == "__main__":
    unittest.main()
