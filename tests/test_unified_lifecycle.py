"""Verify read-only selection, explicit promotion and compact diff behavior."""

from dataclasses import asdict
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk

from slate.editor import EditorWorkspace
from slate.editor_diff_model import project_diff
from slate.experimental_editor import ExperimentalEditor
from slate.scm.base import FileStatus, RepositoryRef
from slate.processes import CommandResult
from slate.project_files import ProjectFileOperations
from slate.window import SlateWindow


class UnifiedLifecycleTest(unittest.TestCase):
    """Use real GTK buffers without opening an application window."""

    @classmethod
    def setUpClass(cls) -> None:
        """Require the isolated GTK unit-test display."""

        if not Gtk.init_check(None)[0]:
            raise unittest.SkipTest("GTK display unavailable")

    def setUp(self) -> None:
        """Create a disposable project and a workspace with inert persistence hooks."""

        self.temp = tempfile.TemporaryDirectory(prefix="slate-unified-", dir="/tmp")
        self.root = Path(self.temp.name)
        (self.root / "file.css").write_text("a { width: 12px; }\n")
        self.persist = MagicMock()
        self.collection = MagicMock()
        self.workspace = EditorWorkspace(Gtk.Box(), 11, self.persist, MagicMock(), self.collection, MagicMock())

    def tearDown(self) -> None:
        """Release the test documents and their filesystem monitors."""

        self.workspace.shutdown()
        self.workspace.destroy()
        self.temp.cleanup()

    def _loaded(self, document) -> None:
        """Wait for the actual Gio load, bounded independently of product timers."""

        deadline = time.monotonic() + 3
        context = GLib.MainContext.default()
        while document.etag is None and time.monotonic() < deadline:
            while context.pending():
                context.iteration(False)
            time.sleep(0.002)
        self.assertIsNotNone(document.etag)

    def test_restored_document_never_exposes_code_before_compact_diff(self) -> None:
        """A restored tab stays in loading until its complete compact projection is ready."""

        document = self.workspace.open_file("project", str(self.root), "file.css", focus=False)
        self.assertFalse(document.source_scroller.get_visible())
        self._loaded(document)
        scm = SimpleNamespace(root=str(self.root), kind="hg", environment={})
        owner = SimpleNamespace(scm_by_repository={("project", RepositoryRef(".", "hg")): scm})
        before = "a { width: 10px; }\n"
        with patch("slate.experimental_editor.run_async") as run:
            SlateWindow._configure_experimental(owner, document, FileStatus("file.css", "modified"))
            view = document.experimental
            self.assertTrue(view.automatic)
            self.assertEqual(view.mode, "compact")
            self.assertTrue(view.mode_buttons["compact"].get_active())
            self.assertFalse(view.mode_buttons["code"].get_active())
            self.assertEqual(view.stack.get_visible_child_name(), "loading")
            self.assertFalse(view.split.get_child_visible())
            transitions = []

            def changed(stack, _property) -> None:
                """Record visible presentations during delayed repository and worker completion."""

                transitions.append(stack.get_visible_child_name())

            view.stack.connect("notify::visible-child-name", changed)
            mode_transitions = []

            def mode_changed(button, identifier) -> None:
                """Record every newly selected mode during asynchronous loading."""

                if button.get_active():
                    mode_transitions.append(identifier)

            for identifier, button in view.mode_buttons.items():
                button.connect("toggled", mode_changed, identifier)
            run.call_args.args[1](CommandResult((), 0, before, ""))
            self.assertEqual(view.mode, "compact")
            self.assertEqual(view.stack.get_visible_child_name(), "loading")
            self.assertEqual(view.projection.get_char_count(), 0)
            with patch.object(document, "get_mapped", return_value=True):
                view._refresh()
            run.call_args.args[1](CommandResult((), 0, json.dumps([
                asdict(row) for row in project_diff(before, view.text())]), ""))
            self.assertEqual(view.stack.get_visible_child_name(), "unified")
            self.assertNotIn("source", transitions)
            self.assertEqual(mode_transitions, [])
            self.assertIn("12px", view.projection.get_text(
                view.projection.get_start_iter(), view.projection.get_end_iter(), True))
            self.assertFalse(view.split.get_child_visible())

    def test_selection_is_readonly_and_never_creates_a_project_tab(self) -> None:
        """Selecting a file must not persist an editor or expose a writable buffer."""

        document = self.workspace.open_preview("project", str(self.root), "file.css")
        self._loaded(document)
        self.assertFalse(document.view.get_editable())
        self.assertTrue(document.edit_button.get_visible())
        self.assertEqual(self.workspace.editors, {})
        self.assertEqual(self.workspace.serialized_state(), ([], None))
        self.persist.assert_not_called()
        self.collection.assert_not_called()
        outcome = []
        document.save(outcome.append)
        self.assertEqual(outcome, [False])

    def test_edit_promotes_the_same_buffer_and_keeps_the_selected_mode(self) -> None:
        """Edit pins exactly one reference without resetting the document or its presentation."""

        document = self.workspace.open_preview("project", str(self.root), "file.css")
        self._loaded(document)
        original_buffer = document.buffer
        view = ExperimentalEditor(document)
        document.experimental = view
        view.base = "old\n"
        view.attach()
        view.set_mode("compact")
        document.edit_button.clicked()
        self.assertIs(document.buffer, original_buffer)
        self.assertTrue(document.view.get_editable())
        self.assertEqual(view.mode, "compact")
        self.assertEqual(len(self.workspace.editors), 1)
        self.assertFalse(document.edit_button.get_visible())
        self.assertEqual(self.workspace.serialized_state()[0], [{"project": "project", "path": "file.css"}])
        self.collection.assert_called_once()
        self.workspace.promote(document)
        self.collection.assert_called_once()

    def test_temporary_document_tracks_file_and_parent_renames_without_pinning(self) -> None:
        """Renaming a temporary file or its directory preserves the buffer and future edit target."""

        folder = self.root / "folder"
        folder.mkdir()
        (folder / "file.css").write_text("original\n")
        document = self.workspace.open_preview("project", str(self.root), "folder/file.css")
        self._loaded(document)
        buffer = document.buffer
        (folder / "file.css").rename(folder / "renamed.css")
        self.workspace.relocate_path("project", "folder/file.css", "folder/renamed.css")
        self.assertEqual(document.relative_path, "folder/renamed.css")
        folder.rename(self.root / "moved")
        self.workspace.relocate_path("project", "folder", "moved")
        self.assertEqual(document.relative_path, "moved/renamed.css")
        self.assertEqual(document.path, str(self.root / "moved/renamed.css"))
        self.assertEqual(self.workspace.active_ref, ("project", "moved/renamed.css"))
        self.assertIs(document.buffer, buffer)
        self.assertTrue(document.previewing)
        self.assertFalse(document.view.get_editable())
        self.assertEqual(self.workspace.editors, {})
        self.persist.assert_not_called()
        self.collection.assert_not_called()
        document.edit_button.clicked()
        self.assertEqual(list(self.workspace.editors), [("project", "moved/renamed.css")])
        document.buffer.insert(document.buffer.get_end_iter(), "edited\n")
        saved = []
        document.save(saved.append)
        deadline = time.monotonic() + 3
        context = GLib.MainContext.default()
        while not saved and time.monotonic() < deadline:
            while context.pending():
                context.iteration(False)
            time.sleep(0.002)
        self.assertEqual(saved, [True])
        self.assertEqual((self.root / "moved/renamed.css").read_text(), "original\nedited\n")
        self.assertFalse((self.root / "folder/file.css").exists())

    def test_directory_rename_lock_prevents_saves_and_covers_newly_opened_documents(self) -> None:
        """Saves cannot recreate old paths while an asynchronous parent rename is active."""

        folder = self.root / "folder"
        folder.mkdir()
        (folder / "file.css").write_text("original\n")
        (folder / "second.css").write_text("second\n")
        document = self.workspace.open_file("project", str(self.root), "folder/file.css", focus=False)
        self._loaded(document)
        document.buffer.insert(document.buffer.get_end_iter(), "edited\n")
        self.assertTrue(self.workspace.set_rename_pending("project", "folder", True))
        self.assertTrue(document.path_changing)
        self.assertFalse(document.save_button.get_sensitive())
        late = self.workspace.open_file("project", str(self.root), "folder/second.css", focus=False)
        self._loaded(late)
        self.assertTrue(late.path_changing)
        late.buffer.insert(late.buffer.get_end_iter(), "later\n")
        result = []
        late.save(result.append)
        self.assertEqual(result, [False])
        self.assertEqual((folder / "second.css").read_text(), "second\n")
        owner = SimpleNamespace(
            editor_workspace=self.workspace, config=SimpleNamespace(find_project=MagicMock(return_value=None)),
            _close_preview=MagicMock(), file_manager=MagicMock(),
            _queue_project_status_paths=MagicMock(), _show_file_error=MagicMock(),
        )
        moved = []

        def renamed(error) -> None:
            """Use the production completion route after the actual asynchronous directory move."""

            SlateWindow._on_project_entry_renamed(owner, "project", "folder", "moved", error)
            moved.append(error)

        ProjectFileOperations().rename_entry(str(folder), str(self.root / "moved"), renamed)
        refused = []
        document.save(refused.append)
        self.assertEqual(refused, [False])
        deadline = time.monotonic() + 3
        context = GLib.MainContext.default()
        while not moved and time.monotonic() < deadline:
            while context.pending():
                context.iteration(False)
            time.sleep(0.002)
        self.assertEqual(moved, [None])
        self.assertFalse(document.path_changing)
        self.assertFalse(late.path_changing)
        saved = []
        document.save(saved.append)
        deadline = time.monotonic() + 3
        while not saved and time.monotonic() < deadline:
            while context.pending():
                context.iteration(False)
            time.sleep(0.002)
        self.assertEqual(saved, [True])
        self.assertEqual((self.root / "moved/file.css").read_text(), "original\nedited\n")
        self.assertFalse(folder.exists())

    def test_rename_lock_is_released_when_the_filesystem_move_fails(self) -> None:
        """A failed move restores normal saving rather than leaving the editor locked."""

        document = self.workspace.open_file("project", str(self.root), "file.css", focus=False)
        self._loaded(document)
        self.assertTrue(self.workspace.set_rename_pending("project", "file.css", True))
        owner = SimpleNamespace(editor_workspace=self.workspace, _show_file_error=MagicMock())
        SlateWindow._on_project_entry_renamed(owner, "project", "file.css", "new.css", "permission denied")
        self.assertFalse(document.path_changing)
        self.assertEqual(self.workspace.renaming_paths, set())
        self.assertEqual(document.relative_path, "file.css")
        owner._show_file_error.assert_called_once()

    def test_alias_promotion_and_next_selection_do_not_duplicate_or_pin(self) -> None:
        """A symlink alias promotes the original reference and another selection stays transient."""

        (self.root / "alias.css").symlink_to(self.root / "file.css")
        original = self.workspace.open_preview("project", str(self.root), "file.css")
        self._loaded(original)
        self.assertIs(self.workspace.open_file("project", str(self.root), "alias.css"), original)
        (self.root / "other.css").write_text("other{}")
        self.workspace.open_preview("project", str(self.root), "other.css")
        self.assertEqual(list(self.workspace.editors), [("project", "file.css")])
        self.assertEqual(self.workspace.serialized_state()[0], [{"project": "project", "path": "file.css"}])

    def test_first_difference_is_positioned_once_for_each_diff_mode_without_focus(self) -> None:
        """Both complete diffs and the compact diff start on the first change only once."""

        document = self.workspace.open_file("project", str(self.root), "file.css", focus=False)
        self._loaded(document)
        before = "same\n" * 80 + "old\n" + "tail\n" * 20
        current = "same\n" * 80 + "new\n" + "tail\n" * 20
        document.buffer.set_text(current)
        view = ExperimentalEditor(document)
        document.experimental = view
        view.base = before
        view.old_buffer.set_text(before)
        view.attach()
        with patch.object(document, "get_mapped", return_value=True), patch.object(view, "schedule"), \
                patch("slate.experimental_editor.run_async") as run, patch.object(document.view, "grab_focus") as focus:
            view.set_mode("unified")
            view._refresh()
            callback = run.call_args.args[1]
            callback(CommandResult((), 0, json.dumps([asdict(row) for row in project_diff(before, current)]), ""))
            self.assertEqual(document.buffer.get_iter_at_mark(document.buffer.get_insert()).get_line(), 80)
            for mode in ("side", "compact"):
                document.buffer.place_cursor(document.buffer.get_start_iter())
                view.set_mode(mode)
                self.assertEqual(document.buffer.get_iter_at_mark(document.buffer.get_insert()).get_line(), 80)
            document.buffer.place_cursor(document.buffer.get_iter_at_line(10))
            view._position_first_change()
            self.assertEqual(document.buffer.get_iter_at_mark(document.buffer.get_insert()).get_line(), 10)
            focus.assert_not_called()

    def test_mode_cycle_shows_the_actual_source_view_and_repositions_diff(self) -> None:
        """Changing from compact to each mode reveals the source and queues layout-safe scrolling."""

        document = self.workspace.open_file("project", str(self.root), "file.css", focus=False)
        self._loaded(document)
        before = "same\n" * 80 + "old\n" + "tail\n" * 20
        current = "same\n" * 80 + "new\n" + "tail\n" * 20
        document.buffer.set_text(current)
        view = ExperimentalEditor(document)
        document.experimental = view
        view.base = before
        view.old_buffer.set_text(before)
        view.rows = project_diff(before, current)
        view.rows_serial = view.serial
        view.rows_base_serial = view.base_serial
        view.hunks = [80]
        view.attach()
        view.set_mode("compact")
        for mode in ("unified", "side", "code", "compact", "unified", "side", "code"):
            document.buffer.place_cursor(document.buffer.get_iter_at_line(10))
            with patch.object(view.projection_view, "scroll_to_mark") as scroll:
                view.set_mode(mode)
                if mode in {"unified", "compact"}:
                    scroll.assert_called()
                    self.assertEqual(view.stack.get_visible_child_name(), "unified")
                else:
                    self.assertEqual(view.stack.get_visible_child_name(), "source")
                    self.assertTrue(view.split.get_visible())
                    self.assertTrue(document.source_scroller.get_visible())
                    self.assertFalse(document.source_scroller.get_no_show_all())
                    self.assertTrue(document.view.get_visible())
                    self.assertIs(document.view.get_buffer(), document.buffer)
                    self.assertEqual(view.old_box.get_visible(), mode == "side")
                if mode != "code":
                    self.assertEqual(document.buffer.get_iter_at_mark(document.buffer.get_insert()).get_line(), 80)
        self.assertEqual(view.text(), current)

    def test_diff_navigation_starts_from_the_visible_projection_cursor(self) -> None:
        """Both diff modes navigate from the clicked visual line, not the hidden source caret."""

        document = self.workspace.open_file("project", str(self.root), "file.css", focus=False)
        self._loaded(document)
        before_lines = [f"line {index}\n" for index in range(100)]
        after_lines = before_lines.copy()
        after_lines[20], after_lines[70] = "first change\n", "second change\n"
        before, current = "".join(before_lines), "".join(after_lines)
        document.buffer.set_text(current)
        view = ExperimentalEditor(document)
        document.experimental = view
        view.base = before
        view.rows = project_diff(before, current)
        view.rows_serial = view.serial
        view.rows_base_serial = view.base_serial
        view.hunks = [20, 70]
        view.attach()
        for mode in ("unified", "compact"):
            view.set_mode(mode)
            for clicked_line, direction, expected in ((71, 1, 20), (69, -1, 20), (69, 1, 70)):
                document.buffer.place_cursor(document.buffer.get_iter_at_line(20))
                offset = view._visual_offset(document.buffer.get_iter_at_line(clicked_line).get_offset())
                view.projection.place_cursor(view.projection.get_iter_at_offset(offset))
                view._navigate(direction)
                self.assertEqual(document.buffer.get_iter_at_mark(document.buffer.get_insert()).get_line(), expected)

    def test_compact_gap_expansion_and_edits_preserve_hidden_source(self) -> None:
        """Omitted context can be expanded, and deleting visible changes never deletes hidden text."""

        document = self.workspace.open_file("project", str(self.root), "file.css", focus=False)
        self._loaded(document)
        before = "head\n" * 20 + "old\n" + "tail\n" * 20
        current = "head\n" * 20 + "new\n" + "tail\n" * 20
        document.buffer.set_text(current)
        view = ExperimentalEditor(document)
        document.experimental = view
        view.base = before
        view.rows = project_diff(before, current)
        view.rows_serial = view.serial
        view.rows_base_serial = view.base_serial
        view.attach()
        view.set_mode("compact")
        text = view.projection.get_text(view.projection.get_start_iter(), view.projection.get_end_iter(), True)
        self.assertIn("unchanged lines", text)
        self.assertLess(text.count("head"), 20)
        self.assertTrue(view.expand_gap_at(text.index("⋯")))
        self.assertEqual(view.text(), current)
        text = view.projection.get_text(view.projection.get_start_iter(), view.projection.get_end_iter(), True)
        self.assertEqual(text.count("head"), 20)
        start = text.index("new")
        view.projection.delete(view.projection.get_iter_at_offset(start), view.projection.get_iter_at_offset(start + 3))
        view.projection.insert(view.projection.get_iter_at_offset(start), "changed")
        self.assertEqual(view.text(), "head\n" * 20 + "changed\n" + "tail\n" * 20)

    def test_expanded_compact_context_survives_edits_and_recomputation(self) -> None:
        """Expanded context follows canonical marks when earlier inserted lines shift diff rows."""

        document = self.workspace.open_file("project", str(self.root), "file.css", focus=False)
        self._loaded(document)
        before = "head\n" * 20 + "old\n" + "tail\n" * 20
        current = "head\n" * 20 + "new\n" + "tail\n" * 20
        document.buffer.set_text(current)
        view = ExperimentalEditor(document)
        document.experimental = view
        view.base = before
        view.rows = project_diff(before, current)
        view.rows_serial = view.serial
        view.rows_base_serial = view.base_serial
        view.attach()
        view.set_mode("compact")
        gap = next(span for span in view.spans if span.gap is not None)
        view.expand_gap_at(view.projection.get_iter_at_mark(gap.visual_start).get_offset())
        document.buffer.insert(document.buffer.get_start_iter(), "inserted before context\n")
        with patch.object(document, "get_mapped", return_value=True), patch(
                "slate.experimental_editor.run_async") as run:
            view._refresh()
            run.call_args.args[1](CommandResult((), 0, json.dumps([
                asdict(row) for row in project_diff(before, view.text())]), ""))
        text = view.projection.get_text(view.projection.get_start_iter(), view.projection.get_end_iter(), True)
        self.assertEqual(text.count("head\n"), 20)
        self.assertTrue(view.expanded_gaps)
        self.assertEqual(view.text(), "inserted before context\n" + current)
