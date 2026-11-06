"""GTK tests for persistent internal GtkSource editor behavior."""

import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import gi

gi.require_version("Gdk", "3.0")
gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, Gio, GLib, Gtk

from slate.anchored_save import AnchoredFileSave
from slate.editor import EditorDocument, EditorWorkspace
from slate.processes import CommandResult
from slate.preview import FilePreview
from slate.scm.base import FileStatus
from slate.window import SlateWindow


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

    def test_retargeted_parent_blocks_save_and_external_reads(self) -> None:
        """Replacing a parent with an external link never redirects an existing editor's I/O."""

        for operation in ("save", "load", "metadata"):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory(dir="/tmp") as external:
                folder = self.root / operation
                folder.mkdir()
                (folder / "file.txt").write_text("original\n")
                outside = Path(external) / "file.txt"
                shutil.copy2(folder / "file.txt", outside)
                editor = EditorDocument("project", str(self.root), f"{operation}/file.txt", 11,
                                        self._state_changed, self._removed)
                try:
                    self._wait_until(lambda: editor.etag is not None and not editor.loading)
                    with patch("gi.repository.Gio.File.load_contents_async") as contents:
                        if operation == "metadata":
                            editor._begin_load("external")
                        folder.rename(self.root / f"old-{operation}")
                        folder.symlink_to(Path(external), target_is_directory=True)
                        if operation == "save":
                            editor.buffer.set_text("local edits\n")
                            for force in (False, True):
                                editor.force_next_save = force
                                completed = []
                                editor.save(completed.append)
                                self.assertEqual(completed, [False])
                            self.assertTrue(editor.dirty)
                        elif operation == "load":
                            with patch.object(editor.file, "query_info_async") as query:
                                editor._begin_load("external")
                                query.assert_not_called()
                        else:
                            self._wait_until(lambda: not editor.loading)
                        contents.assert_not_called()
                    self.assertEqual(outside.read_text(), "original\n")
                    self.assertTrue(editor.attention)
                    self.assertTrue(editor.external_conflict)
                finally:
                    editor.close()
                    editor.destroy()

    def test_parent_change_during_save_preserves_external_file_and_dirty_buffer(self) -> None:
        """A raced save remains anchored and cannot certify the replaced logical path as clean."""

        folder = self.root / "sub"
        folder.mkdir()
        (folder / "file.txt").write_text("original")
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            outside = Path(directory) / "file.txt"
            shutil.copy2(folder / "file.txt", outside)
            editor = EditorDocument("project", str(self.root), "sub/file.txt", 11,
                                    self._state_changed, self._removed)
            try:
                self._wait_until(lambda: editor.etag is not None and not editor.loading)
                editor.buffer.set_text("local snapshot")
                original = Gio.File.replace_async

                def switched(file, *args):
                    """Replace the pathname after validation and before asynchronous replacement."""

                    folder.rename(self.root / "old-sub")
                    folder.symlink_to(directory, target_is_directory=True)
                    original(file, *args)

                completed = []
                with patch.object(Gio.File, "replace_async", switched):
                    editor.save(completed.append)
                    self._wait_until(lambda: bool(completed))
                self.assertEqual(completed, [False])
                self.assertEqual(outside.read_text(), "original")
                self.assertEqual((self.root / "old-sub" / "file.txt").read_text(), "local snapshot")
                self.assertTrue(editor.dirty)
                self.assertTrue(editor.external_conflict)
            finally:
                editor.close()
                editor.destroy()

    def test_literal_backslash_file_remains_distinct_from_a_nested_path(self) -> None:
        """Loading and saving a Linux backslash name never touches its slash-separated counterpart."""

        literal = self.root / "a\\b.txt"
        nested = self.root / "a" / "b.txt"
        nested.parent.mkdir()
        literal.write_text("literal")
        nested.write_text("nested")
        editor = EditorDocument("project", str(self.root), "a\\b.txt", 11,
                                self._state_changed, self._removed)
        try:
            self._wait_until(lambda: editor.etag is not None and not editor.loading)
            text = editor.buffer.get_text(editor.buffer.get_start_iter(), editor.buffer.get_end_iter(), True)
            self.assertEqual(text, "literal")
            editor.buffer.set_text("edited literal")
            completed = []
            editor.save(completed.append)
            self._wait_until(lambda: bool(completed))
            self.assertEqual(completed, [True])
            self.assertEqual(literal.read_text(), "edited literal")
            self.assertEqual(nested.read_text(), "nested")
        finally:
            editor.close()
            editor.destroy()

    def test_fifo_is_rejected_before_reading_contents(self) -> None:
        """A FIFO without a writer is rejected without starting a blocking content read."""

        self.path.unlink()
        os.mkfifo(self.path)
        editor = None
        try:
            with patch.object(Gio.File, "load_contents_async") as load:
                editor = EditorDocument("project", str(self.root), "TODO.md", 11,
                                        self._state_changed, self._removed)
                self._wait_until(self.removed.__len__)
                load.assert_not_called()
            self.assertFalse(editor.loading)
            self.assertIsNone(editor.load_cancellable)
            self.assertFalse(editor.view.get_editable())
            self.assertIn("regular file", self.removed[0])
        finally:
            if editor is not None:
                editor.close()

    def test_external_fifo_replacement_preserves_existing_buffer(self) -> None:
        """Replacing a document with a FIFO never reads it or discards local edits."""

        for dirty in (False, True):
            with self.subTest(dirty=dirty):
                self.path.unlink()
                self.path.write_text("original\n")
                editor = self._document()
                try:
                    if dirty:
                        editor.buffer.set_text("unsaved edits\n")
                    self.path.unlink()
                    os.mkfifo(self.path)
                    with patch.object(Gio.File, "load_contents_async") as load:
                        editor._begin_load("external")
                        self._wait_until(lambda: not editor.loading)
                        load.assert_not_called()
                    self.assertEqual(
                        editor.buffer.get_text(editor.buffer.get_start_iter(), editor.buffer.get_end_iter(), True),
                        "unsaved edits\n" if dirty else "original\n",
                    )
                    self.assertEqual(editor.dirty, dirty)
                    self.assertEqual(editor.view.get_editable(), dirty)
                    self.assertEqual(editor.external_conflict, dirty)
                    self.assertEqual(self.removed, [])
                finally:
                    editor.close()

    def test_oversized_file_is_rejected_before_reading_contents(self) -> None:
        """The metadata preflight never starts a content read for an oversized file."""

        editor = self._document()
        original_file = editor.file
        source = MagicMock()
        source.query_info_finish.return_value.get_size.return_value = editor.MAX_TEXT_BYTES + 1
        source.query_info_finish.return_value.get_file_type.return_value = Gio.FileType.REGULAR
        editor.file = source
        try:
            editor._begin_load("initial")
            callback = source.query_info_async.call_args.args[4]
            request = source.query_info_async.call_args.args[5]
            callback(source, MagicMock(), request)
            source.load_contents_async.assert_not_called()
            self.assertFalse(editor.loading)
            self.assertIn("over 5 MiB", self.removed[-1])
        finally:
            editor.file = original_file
            editor.close()

    def test_superseded_size_query_cannot_start_an_old_content_load(self) -> None:
        """A canceled size check cannot overwrite the newer load request."""

        editor = self._document()
        original_file = editor.file
        source = MagicMock()
        editor.file = source
        try:
            editor._begin_load("external")
            callback = source.query_info_async.call_args.args[4]
            old_request = source.query_info_async.call_args.args[5]
            editor._begin_load("external")
            callback(source, MagicMock(), old_request)
            source.query_info_finish.assert_not_called()
            source.load_contents_async.assert_not_called()
        finally:
            editor.file = original_file
            editor.close()

    def test_typing_during_save_stays_dirty_and_prevents_waiting_close(self) -> None:
        """Only the submitted snapshot is saved; newer typing requires another successful save."""

        editor = self._document()
        original_file = editor.file
        source = MagicMock()
        editor.file = source
        callbacks = []
        write_patch = patch("slate.editor.AnchoredFileSave")
        write = write_patch.start()
        self.addCleanup(write_patch.stop)
        try:
            editor.buffer.set_text("first snapshot\n")
            editor.save(callbacks.append)
            request = write.call_args.args
            self.assertEqual(request[3], b"first snapshot\n")
            editor.buffer.insert(editor.buffer.get_end_iter(), "later typing\n")
            source.replace_contents_finish.return_value = (True, "saved-first")
            request[6](source, MagicMock(), request[7])
            self.assertTrue(editor.dirty)
            self.assertTrue(editor.save_button.get_sensitive())
            self.assertEqual(callbacks, [False])
            self.assertEqual(editor.etag, "saved-first")
            editor.save(callbacks.append)
            request = write.call_args.args
            self.assertEqual(request[3], b"first snapshot\nlater typing\n")
            self.assertEqual(request[4], "saved-first")
            source.replace_contents_finish.return_value = (True, "saved-second")
            request[6](source, MagicMock(), request[7])
            self.assertFalse(editor.dirty)
            self.assertEqual(callbacks, [False, True])
        finally:
            editor.file = original_file
            editor.close()

    def test_undo_redo_follow_the_snapshot_written_during_a_concurrent_save(self) -> None:
        """Undo cannot mark the original content clean after a newer snapshot reaches disk."""

        editor = self._document()
        original_file = editor.file
        source = MagicMock()
        editor.file = source
        write_patch = patch("slate.editor.AnchoredFileSave")
        write = write_patch.start()
        self.addCleanup(write_patch.stop)
        try:
            editor.buffer.begin_user_action()
            editor.buffer.insert(editor.buffer.get_end_iter(), "first edit\n")
            editor.buffer.end_user_action()
            editor.save()
            request = write.call_args.args
            saved_text = request[3].decode("utf-8")
            editor.buffer.begin_user_action()
            editor.buffer.insert(editor.buffer.get_end_iter(), "later edit\n")
            editor.buffer.end_user_action()
            source.replace_contents_finish.return_value = (True, "saved-first")
            request[6](source, MagicMock(), request[7])
            for action, expected_dirty in (("undo", False), ("undo", True), ("redo", False), ("redo", True)):
                getattr(editor.buffer, action)()
                current = editor.buffer.get_text(editor.buffer.get_start_iter(), editor.buffer.get_end_iter(), True)
                self.assertEqual(editor.dirty, expected_dirty)
                self.assertEqual(editor.buffer.get_modified(), expected_dirty)
                self.assertEqual(editor.dirty, current != saved_text)
                self.assertEqual(editor.save_button.get_sensitive(), expected_dirty)
        finally:
            editor.file = original_file
            editor.close()

    def test_save_all_rechecks_documents_after_the_last_delayed_write(self) -> None:
        """Changes to already saved or newly opened documents prevent final closure."""

        for target in ("already-saved", "new-document"):
            with self.subTest(target=target):
                workspace = EditorWorkspace(Gtk.Box(), 11, MagicMock(), MagicMock(), MagicMock(), MagicMock())
                self.addCleanup(workspace.destroy)
                first, second = MagicMock(), MagicMock()
                first.dirty = second.dirty = True
                workspace.editors = {
                    ("project", "first"): SimpleNamespace(project_name="project", document=first),
                    ("project", "second"): SimpleNamespace(project_name="project", document=second),
                }
                callbacks = []
                workspace._save_many([first, second], callbacks.append)
                first.dirty = False
                first.save.call_args.args[0](True)
                if target == "already-saved":
                    first.dirty = True
                else:
                    workspace.editors[("project", "new")] = SimpleNamespace(
                        project_name="project", document=SimpleNamespace(dirty=True))
                second.dirty = False
                second.save.call_args.args[0](True)
                self.assertEqual(callbacks, [False])

    def test_project_save_all_ignores_dirty_documents_in_other_projects(self) -> None:
        """A project close validates its own editors without blocking on another project."""

        workspace = EditorWorkspace(Gtk.Box(), 11, MagicMock(), MagicMock(), MagicMock(), MagicMock())
        self.addCleanup(workspace.destroy)
        editor = MagicMock()
        editor.dirty = True
        workspace.editors = {
            ("project", "file"): SimpleNamespace(project_name="project", document=editor),
            ("other", "file"): SimpleNamespace(project_name="other", document=SimpleNamespace(dirty=True)),
        }
        callbacks = []
        workspace._save_many([editor], callbacks.append, project_name="project")
        editor.dirty = False
        editor.save.call_args.args[0](True)
        self.assertEqual(callbacks, [True])

    def test_save_completion_for_an_old_path_cannot_mark_a_renamed_document_clean(self) -> None:
        """A late write to the old pathname is reported as a conflict, never as a successful new-file save."""

        editor = self._document()
        old_file = editor.file
        original_write = AnchoredFileSave
        saved = []
        try:
            editor.buffer.set_text("edited\n")
            with patch("slate.editor.AnchoredFileSave") as write:
                editor.save(saved.append)
                request = write.call_args.args
            destination = self.root / "renamed.md"
            self.path.rename(destination)
            editor.relocate("renamed.md")
            original_write(*request)
            self._wait_until(lambda: bool(saved))
            self.assertEqual(saved, [False])
            self.assertTrue(editor.dirty)
            self.assertTrue(editor.external_conflict)
            self.assertEqual(destination.read_text(), "# TODO\n")
        finally:
            editor.close()

    def test_rename_lock_rejects_an_editor_with_an_inflight_save(self) -> None:
        """A pending write prevents a rename lock even when the parent directory is targeted."""

        workspace = EditorWorkspace(Gtk.Box(), 11, MagicMock(), MagicMock(), MagicMock(), MagicMock())
        editor = workspace.open_file("project", str(self.root), "TODO.md", focus=False)
        self._wait_until(lambda: editor.etag is not None)
        try:
            editor.buffer.set_text("edited\n")
            with patch("slate.editor.AnchoredFileSave"):
                editor.save()
                self.assertFalse(workspace.set_rename_pending("project", "TODO.md", True))
                self.assertEqual(workspace.renaming_paths, set())
                self.assertFalse(editor.path_changing)
        finally:
            workspace.shutdown()
            workspace.destroy()

    def test_stale_content_callbacks_cannot_cancel_or_replace_a_new_load(self) -> None:
        """Both canceled and successful old reads leave the newer request intact."""

        for cancelled in (False, True):
            with self.subTest(cancelled=cancelled):
                editor = self._document()
                original_file = editor.file
                source = MagicMock()
                source.query_info_finish.return_value.get_size.return_value = 10
                source.query_info_finish.return_value.get_file_type.return_value = Gio.FileType.REGULAR
                editor.file = source
                try:
                    editor._begin_load("external")
                    arguments = source.query_info_async.call_args.args
                    arguments[4](source, MagicMock(), arguments[5])
                    old_read = source.load_contents_async.call_args.args
                    editor._begin_load("external")
                    newest_token = editor.load_cancellable
                    newest_info = source.query_info_async.call_args.args
                    if cancelled:
                        source.load_contents_finish.side_effect = GLib.Error.new_literal(
                            Gio.io_error_quark(), "cancelled", Gio.IOErrorEnum.CANCELLED)
                    else:
                        source.load_contents_finish.return_value = (True, b"stale contents\n", "old-etag")
                    old_read[1](source, MagicMock(), old_read[2])
                    self.assertIs(editor.load_cancellable, newest_token)
                    self.assertTrue(editor.loading)
                    self.assertEqual(editor.buffer.get_text(
                        editor.buffer.get_start_iter(), editor.buffer.get_end_iter(), True), "# TODO\n")
                    newest_info[4](source, MagicMock(), newest_info[5])
                    newest_read = source.load_contents_async.call_args.args
                    source.load_contents_finish.side_effect = None
                    source.load_contents_finish.return_value = (True, b"newest contents\n", "newest-etag")
                    newest_read[1](source, MagicMock(), newest_read[2])
                    self.assertIsNone(editor.load_cancellable)
                    self.assertFalse(editor.loading)
                    self.assertEqual(editor.etag, "newest-etag")
                    self.assertEqual(editor.buffer.get_text(
                        editor.buffer.get_start_iter(), editor.buffer.get_end_iter(), True), "newest contents\n")
                finally:
                    editor.file = original_file
                    editor.close()

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

    def test_double_click_selects_identifier_instead_of_entire_line(self) -> None:
        """Source identifiers include underscores and PHP variable prefixes."""

        editor = self._document()
        editor.buffer.set_text("echo $my_variable + next_word;\n")
        clicked = editor.buffer.get_iter_at_offset(9)

        self.assertTrue(editor._select_identifier_at(clicked))

        start, end = editor.buffer.get_selection_bounds()
        self.assertEqual(editor.buffer.get_text(start, end, True), "$my_variable")
        editor.close()

    def test_double_click_in_line_number_gutter_keeps_native_selection(self) -> None:
        """The identifier handler does not override GTK's line-number gutter."""

        owner = SimpleNamespace(_select_identifier_at=MagicMock())
        view = MagicMock()
        view.get_window_type.return_value = Gtk.TextWindowType.LEFT
        event = SimpleNamespace(
            button=1,
            type=Gdk.EventType.DOUBLE_BUTTON_PRESS,
            window=object(),
        )

        self.assertFalse(EditorDocument._on_view_button_press(owner, view, event))
        owner._select_identifier_at.assert_not_called()

    def test_empty_document_save_completes(self) -> None:
        """Truncating a document still resolves the callback used by Save all."""

        editor = self._document()
        editor.buffer.set_text("")
        self.assertTrue(editor.dirty)
        completed: list[bool] = []
        editor.save(completed.append)
        self._wait_until(lambda: bool(completed))
        self.assertEqual(completed, [True])
        self.assertEqual(self.path.read_bytes(), b"")
        self.assertFalse(editor.dirty)
        editor.close()

    def test_php_save_checks_syntax_and_reports_failure(self) -> None:
        """Saving PHP starts async lint and shows its syntax error only on failure."""

        self.path = self.root / "example.php"
        self.path.write_text("<?php echo 1;\n", encoding="utf-8")
        editor = EditorDocument(
            "project", str(self.root), "example.php", 11,
            self._state_changed, self._removed,
        )
        self._wait_until(lambda: not editor.loading and editor.etag is not None)
        editor.buffer.set_text("<?php echo ;\n")
        completed: list[bool] = []
        command = MagicMock()

        with patch("slate.editor.run_async", return_value=command) as launch:
            editor.save(completed.append)
            self._wait_until(lambda: bool(completed))

        self.assertEqual(completed, [True])
        self.assertEqual(launch.call_args.args[0], ["php", "-l", str(self.path)])
        self.assertEqual(launch.call_args.kwargs["cwd"], str(self.root))
        callback = launch.call_args.args[1]
        failed = CommandResult(("php", "-l", str(self.path)), 255, "", "Parse error")
        with patch("slate.editor.Gtk.MessageDialog") as dialog_class:
            callback(failed)
        dialog_class.assert_called_once()
        self.assertEqual(
            dialog_class.call_args.kwargs["text"],
            "⛔ PHP syntax error: example.php",
        )
        error_icon = dialog_class.return_value.set_image.call_args.args[0]
        self.assertEqual(error_icon.get_icon_name()[0], "dialog-error")
        dialog_class.return_value.format_secondary_text.assert_called_once_with(
            "Parse error"
        )
        dialog_class.return_value.show_all.assert_called_once_with()
        editor.close()

    def test_save_preserves_executable_file_mode(self) -> None:
        """Atomic replacement must not remove execute bits from a script."""

        editor = self._document()
        self.path.chmod(0o751)
        editor.buffer.insert(editor.buffer.get_end_iter(), "echo deploy\n")
        completed: list[bool] = []

        editor.save(completed.append)

        self._wait_until(completed.__len__)
        self.assertEqual(completed, [True])
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o751)
        editor.close()

    def test_save_invalidates_completed_external_read_before_its_delivery(self) -> None:
        """A completed old read cannot replace a newer successfully saved buffer."""

        editor = self._document()
        pending = []
        original = Gio.File.load_contents_async

        def deferred(file, cancellable, callback, request):
            """Complete a real file read but defer delivery to the editor."""

            def completed(source, result, user_data):
                """Retain the real result until the newer save has finished."""

                pending.append((callback, source, result, user_data))

            original(file, cancellable, completed, request)

        try:
            with patch.object(Gio.File, "load_contents_async", deferred):
                editor._begin_load("external")
                self._wait_until(pending.__len__)
            old_token = editor.load_cancellable
            editor.buffer.set_text("New saved contents\n")
            saved = []
            editor.save(saved.append)
            self._wait_until(saved.__len__)
            saved_etag = editor.etag

            callback, source, result, request = pending.pop()
            callback(source, result, request)

            self.assertEqual(saved, [True])
            self.assertTrue(old_token.is_cancelled())
            self.assertIsNone(editor.load_cancellable)
            self.assertFalse(editor.loading)
            self.assertEqual(editor.etag, saved_etag)
            self.assertEqual(self.path.read_text(), "New saved contents\n")
            self.assertEqual(
                editor.buffer.get_text(editor.buffer.get_start_iter(), editor.buffer.get_end_iter(), True),
                "New saved contents\n",
            )
            self.assertFalse(editor.dirty)
            self.assertFalse(editor.external_conflict)
        finally:
            editor.close()

    def test_unchanged_external_read_restores_save_after_concurrent_edit(self) -> None:
        """Typing during an unchanged disk check leaves Save enabled and state published."""

        editor = self._document()
        editor._begin_load("external")
        editor.buffer.insert(editor.buffer.get_end_iter(), "local edit")
        self.assertFalse(editor.save_button.get_sensitive())
        self._wait_until(lambda: not editor.loading)
        self.assertTrue(editor.dirty)
        self.assertTrue(editor.save_button.get_sensitive())
        self.assertEqual(self.states[-1], (True, False))
        self.assertFalse(editor.external_conflict)
        editor.close()

    def test_dirty_external_check_uses_metadata_even_if_disk_file_is_oversized(self) -> None:
        """A large external replacement preserves local text and the explicit conflict choice."""

        editor = self._document()
        editor.buffer.set_text("local version")
        self.path.write_bytes(b"x" * (editor.MAX_TEXT_BYTES + 1))
        with patch("gi.repository.Gio.File.load_contents_async") as load:
            editor._handle_external_change()
            self._wait_until(lambda: not editor.loading)
            load.assert_not_called()
        self.assertTrue(editor.external_conflict)
        self.assertTrue(editor.view.get_editable())
        self.assertTrue(editor.save_button.get_sensitive())
        self.assertEqual(editor.buffer.get_text(editor.buffer.get_start_iter(), editor.buffer.get_end_iter(), True),
                         "local version")
        editor.close()

    def test_snapshot_dirty_comparison_is_cached_until_text_changes(self) -> None:
        """Repeated dirty queries reuse their result, but edits invalidate the cache."""

        editor = self._document()
        editor.saved_snapshot = "saved version"
        with patch.object(editor.buffer, "get_text", wraps=editor.buffer.get_text) as read:
            self.assertTrue(editor.dirty)
            self.assertTrue(editor.dirty)
            self.assertEqual(read.call_count, 1)
        editor.buffer.set_text("saved version")
        self.assertFalse(editor.dirty)
        editor.saved_snapshot = "different saved version"
        self.assertTrue(editor.dirty)
        editor.close()

    def test_completed_save_callback_is_inert_after_document_destruction(self) -> None:
        """A completed write cannot revive a closed PHP document, update UI or launch lint."""

        path = self.root / "closed.php"
        path.write_text("<?php echo 1;\n")
        editor = EditorDocument("project", str(self.root), "closed.php", 11, MagicMock(), MagicMock())
        self._wait_until(lambda: editor.etag is not None and not editor.loading)
        editor.buffer.set_text("<?php echo 2;\n")
        original = editor._on_saved
        queued = []
        callbacks = []

        def deferred(*args):
            """Hold the completed write until the original editor has been destroyed."""

            queued.append(args)

        with patch.object(editor, "_on_saved", side_effect=deferred):
            editor.save(callbacks.append)
            self._wait_until(lambda: bool(queued))
        editor.close()
        self.assertEqual(callbacks, [False])
        editor.destroy()
        editor.on_state.reset_mock()
        with patch("slate.editor.run_async") as lint, patch.object(editor, "_hide_notice") as notice:
            original(*queued[0])
            lint.assert_not_called()
            notice.assert_not_called()
        editor.on_state.assert_not_called()
        self.assertFalse(editor.saving)
        self.assertEqual(callbacks, [False])
        with patch("slate.editor.AnchoredFileSave") as write:
            editor.save(callbacks.append)
            write.assert_not_called()
        self.assertEqual(callbacks, [False, False])

    def test_failed_save_callback_cannot_show_errors_after_document_closure(self) -> None:
        """Finishing a failed Gio request after closure consumes its error without touching widgets."""

        editor = self._document()
        editor.close()
        editor.destroy()
        source = MagicMock()
        source.replace_contents_finish.side_effect = GLib.Error.new_literal(
            Gio.io_error_quark(), "late failure", Gio.IOErrorEnum.FAILED)
        editor.on_state = MagicMock()
        with patch.object(editor, "_show_notice") as notice:
            editor._on_saved(source, MagicMock(), "old snapshot")
            notice.assert_not_called()
        source.replace_contents_finish.assert_called_once()
        editor.on_state.assert_not_called()

    def test_late_close_after_save_preserves_a_reopened_document(self) -> None:
        """An old save may close only the original registered document, never its replacement."""

        old = SimpleNamespace(reference=("project", "TODO.md"), dirty=False)
        replacement = SimpleNamespace(reference=old.reference, dirty=True)
        owner = SimpleNamespace(editors={old.reference: SimpleNamespace(document=replacement)},
                                _remove_editor=MagicMock())
        EditorWorkspace._close_after_save(owner, old, True)
        owner._remove_editor.assert_not_called()
        owner.editors.clear()
        EditorWorkspace._close_after_save(owner, old, True)
        owner._remove_editor.assert_not_called()
        owner.editors[old.reference] = SimpleNamespace(document=old)
        EditorWorkspace._close_after_save(owner, old, True)
        owner._remove_editor.assert_called_once_with(old.reference)

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

    def test_editor_search_moves_next_and_previous_between_real_matches(self) -> None:
        """Editor navigation advances by position rather than matching text alone."""

        editor = self._document()
        editor.buffer.set_text("needle first\nneedle second\n")
        editor.search_settings.set_search_text("needle")
        editor.buffer.place_cursor(editor.buffer.get_start_iter())

        editor.search(False)
        first = editor.buffer.get_selection_bounds()
        self.assertEqual(first[0].get_line(), 0)
        editor.search(False)
        second = editor.buffer.get_selection_bounds()
        self.assertEqual(second[0].get_line(), 1)
        editor.search(True)
        previous = editor.buffer.get_selection_bounds()
        self.assertEqual(previous[0].get_line(), 0)
        editor.close()

    def test_clean_external_deletion_keeps_the_editor_open(self) -> None:
        """A deleted clean file remains recoverable until the user chooses an action."""

        editor = self._document()
        self.path.unlink()

        editor._handle_external_change()
        self._wait_until(lambda: not editor.loading)

        self.assertEqual(self.removed, [])
        self.assertTrue(editor.external_conflict)
        self.assertTrue(editor.deleted_conflict)
        self.assertTrue(editor.attention)
        self.assertTrue(editor.info_bar.get_visible())
        self.assertIsNotNone(editor.monitor)
        editor.close()

    def test_deleted_clean_file_reloads_when_an_agent_recreates_it(self) -> None:
        """A replacement appearing after deletion follows the standard clean reload."""

        editor = self._document()
        self.path.unlink()
        self._wait_until(lambda: editor.deleted_conflict)
        self.path.write_text("# Recreated by agent\n", encoding="utf-8")

        def recreated_buffer_is_current() -> bool:
            """Report when the recreated file has completed its external reload."""

            text = editor.buffer.get_text(
                editor.buffer.get_start_iter(), editor.buffer.get_end_iter(), True
            )
            return not editor.loading and text == "# Recreated by agent\n"

        self._wait_until(recreated_buffer_is_current)

        text = editor.buffer.get_text(
            editor.buffer.get_start_iter(), editor.buffer.get_end_iter(), True
        )
        self.assertEqual(text, "# Recreated by agent\n")
        self.assertEqual(self.removed, [])
        self.assertFalse(editor.external_conflict)
        self.assertFalse(editor.deleted_conflict)
        self.assertFalse(editor.attention)
        self.assertFalse(editor.info_bar.get_visible())
        editor.close()

    def test_recreate_choice_makes_a_clean_deleted_buffer_savable(self) -> None:
        """Choosing recreation marks the retained last disk version for saving."""

        editor = self._document()
        self.path.unlink()
        editor._handle_external_change()

        editor._on_info_response(editor.info_bar, editor.RESPONSE_RECREATE)

        self.assertTrue(editor.dirty)
        self.assertTrue(editor.force_next_save)
        self.assertFalse(editor.external_conflict)
        completed: list[bool] = []
        editor.save(completed.append)
        self._wait_until(lambda: bool(completed))
        self.assertEqual(completed, [True])
        self.assertEqual(self.path.read_text(encoding="utf-8"), "# TODO\n")
        editor.close()

    def test_recreate_restores_clean_snapshot_after_concurrent_save(self) -> None:
        """Recreate writes retained text even when a concurrent-save snapshot matches it."""

        editor = self._document()
        try:
            editor.buffer.set_text("saved text\n")
            editor.save()
            editor.buffer.insert(editor.buffer.get_end_iter(), "later typing\n")

            def saved() -> bool:
                """Wait for the real asynchronous snapshot write."""

                return not editor.saving

            self._wait_until(saved)
            self.assertEqual(self.path.read_text(), "saved text\n")
            self.assertIsNotNone(editor.saved_snapshot)
            editor.buffer.set_text("saved text\n")
            self.assertFalse(editor.dirty)
            self.path.unlink()
            editor.mute_monitor_until = 0
            editor._handle_external_change()

            def deleted() -> bool:
                """Wait for missing-file metadata to trigger the conflict."""

                return editor.deleted_conflict

            self._wait_until(deleted)
            editor._on_info_response(editor.info_bar, editor.RESPONSE_RECREATE)
            self.assertTrue(editor.dirty)
            completed = []
            editor.save(completed.append)

            def recreated() -> bool:
                """Wait for the actual recreation write to complete."""

                return bool(completed)

            self._wait_until(recreated)
            self.assertEqual(completed, [True])
            self.assertEqual(self.path.read_text(), "saved text\n")
            self.assertFalse(editor.dirty)
        finally:
            editor.close()
            editor.destroy()

    def test_repeated_atomic_replacements_keep_clean_markdown_current(self) -> None:
        """Agent-style atomic writes keep refreshing an already open document."""

        editor = self._document()

        for index, revision in enumerate(("# Agente uno\n", "# Agente due\n")):
            if index == 0:
                # Riproduce la finestra di soppressione lasciata da un salvataggio
                # dell'editor immediatamente precedente alla scrittura dell'agente.
                editor.mute_monitor_until = (
                    GLib.get_monotonic_time() // 1000 + 500
                )
            replacement = self.root / ".TODO.md.agent"
            replacement.write_text(revision, encoding="utf-8")
            replacement.replace(self.path)

            def buffer_is_current(expected: str = revision) -> bool:
                """Report when the asynchronous monitor reload reaches this revision."""

                text = editor.buffer.get_text(
                    editor.buffer.get_start_iter(),
                    editor.buffer.get_end_iter(),
                    True,
                )
                return not editor.loading and text == expected

            self._wait_until(buffer_is_current)
            self.assertIsNotNone(editor.monitor)

        editor.close()

    def test_replaced_parent_or_ancestor_reloads_clean_buffers_and_protects_dirty_ones(self) -> None:
        """Real ancestor replacements refresh clean text, preserve edits and keep later writes observable."""

        for deep, dirty in ((False, False), (True, False), (False, True), (True, True)):
            with self.subTest(nested=deep, unsaved=dirty):
                container = self.root / f"case-{deep}-{dirty}"
                replaced = container / "src"
                directory = replaced / "deep" if deep else replaced
                directory.mkdir(parents=True)
                path = directory / "file.txt"
                path.write_text("initial\n")
                editor = EditorDocument("project", str(self.root), str(path.relative_to(self.root)),
                                        11, self._state_changed, self._removed)
                try:
                    def loaded() -> bool:
                        """Wait until the real initial read has installed its monitor."""

                        return editor.etag is not None and not editor.loading

                    self._wait_until(loaded)
                    if dirty:
                        editor.buffer.insert(editor.buffer.get_end_iter(), "local edit\n")
                    # A build/deployment tool can replace the whole containing directory.
                    replaced.rename(container / "retired-src")
                    directory.mkdir(parents=True)
                    path.write_text("replacement\n")

                    def updated() -> bool:
                        """Wait for an automatic reload or an explicit dirty-buffer conflict."""

                        text = editor.buffer.get_text(editor.buffer.get_start_iter(), editor.buffer.get_end_iter(), True)
                        return not editor.loading and (editor.external_conflict if dirty else text == "replacement\n")

                    self._wait_until(updated)
                    if dirty:
                        text = editor.buffer.get_text(editor.buffer.get_start_iter(), editor.buffer.get_end_iter(), True)
                        self.assertEqual(text, "initial\nlocal edit\n")
                        self.assertEqual(path.read_text(), "replacement\n")
                        editor._on_info_response(editor.info_bar, editor.RESPONSE_DISK)

                    def replacement_loaded() -> bool:
                        """Wait for the replacement to become the clean current buffer."""

                        text = editor.buffer.get_text(editor.buffer.get_start_iter(), editor.buffer.get_end_iter(), True)
                        return not editor.loading and not editor.dirty and text == "replacement\n"

                    self._wait_until(replacement_loaded)
                    path.write_text("later external write\n")

                    def later_loaded() -> bool:
                        """Verify subsequent writes use the renewed parent monitor."""

                        text = editor.buffer.get_text(editor.buffer.get_start_iter(), editor.buffer.get_end_iter(), True)
                        return not editor.loading and text == "later external write\n"

                    self._wait_until(later_loaded)
                    self.assertFalse(editor.external_conflict)
                finally:
                    editor.close()
                    editor.destroy()

    def test_missing_ancestor_can_return_later_and_all_monitors_close(self) -> None:
        """A delayed recreation recovers automatically and closing cancels every real monitor."""

        replaced = self.root / "src"
        directory = replaced / "deep"
        directory.mkdir(parents=True)
        path = directory / "file.txt"
        path.write_text("initial\n")
        editor = EditorDocument("project", str(self.root), "src/deep/file.txt", 11,
                                self._state_changed, self._removed)
        try:
            def loaded() -> bool:
                """Wait for the initial read and installed monitors."""

                return editor.etag is not None and not editor.loading

            self._wait_until(loaded)
            replaced.rename(self.root / "retired-src")

            def missing() -> bool:
                """Allow the missing directory to persist until its conflict is visible."""

                return editor.deleted_conflict

            self._wait_until(missing)
            directory.mkdir(parents=True)
            path.write_text("returned\n")

            def returned() -> bool:
                """Wait for recovery driven by the ancestor creation event."""

                text = editor.buffer.get_text(editor.buffer.get_start_iter(), editor.buffer.get_end_iter(), True)
                return not editor.loading and not editor.external_conflict and text == "returned\n"

            self._wait_until(returned)
            self.assertFalse(editor.info_bar.get_visible())
        finally:
            monitors = list(editor.ancestor_monitors.values())
            if editor.monitor is not None:
                monitors.append(editor.monitor)
            editor.close()
            editor.destroy()
            for monitor in monitors:
                self.assertTrue(monitor.is_cancelled())

    def test_directory_monitor_survives_atomic_replacement_gap(self) -> None:
        """The retained parent monitor observes replacements without inode gaps."""

        editor = self._document()
        original_monitor = editor.monitor
        replacement = self.root / ".TODO.md.agent"
        replacement.write_text("# Replaced atomically\n", encoding="utf-8")
        replacement.replace(self.path)

        def buffer_is_current() -> bool:
            """Report when the directory event has reloaded the replacement."""

            text = editor.buffer.get_text(
                editor.buffer.get_start_iter(), editor.buffer.get_end_iter(), True
            )
            return not editor.loading and text == "# Replaced atomically\n"

        self._wait_until(buffer_is_current)
        self.assertIs(editor.monitor, original_monitor)
        editor.close()

    def test_dirty_external_change_requires_explicit_version_choice(self) -> None:
        """Dirty content survives external writes until the user chooses a side."""

        editor = self._document()
        editor.buffer.set_text("# Locale\n")
        self.path.write_text("# Esterno\n", encoding="utf-8")
        editor._handle_external_change()
        self._wait_until(lambda: not editor.loading)
        self.assertTrue(editor.external_conflict)
        self.assertTrue(editor.attention)
        editor._on_info_response(editor.info_bar, editor.RESPONSE_MINE)
        self.assertFalse(editor.external_conflict)
        self.assertTrue(editor.force_next_save)
        self.assertFalse(editor.info_bar.get_revealed())
        self.assertFalse(editor.info_bar.get_visible())
        def saved() -> bool:
            """Wait for Keep my version to finish its automatic write."""

            return not editor.saving

        self._wait_until(saved)
        self.assertEqual(self.path.read_text(encoding="utf-8"), "# Locale\n")
        editor.close()

    def test_keep_my_version_saves_immediately_and_undo_remains_unsaved(self) -> None:
        """The explicit overwrite writes now; a later Undo differs from the new disk save point."""

        editor = self._document()
        try:
            original = self.path.read_text()
            editor.buffer.begin_user_action()
            editor.buffer.insert(editor.buffer.get_end_iter(), "local change\n")
            editor.buffer.end_user_action()
            self.path.write_text("external version\n")
            editor._handle_external_change()

            def conflicted() -> bool:
                """Wait for the real external metadata conflict."""

                return editor.external_conflict

            self._wait_until(conflicted)
            editor._on_info_response(editor.info_bar, editor.RESPONSE_MINE)

            def saved() -> bool:
                """Wait for the write started by the explicit conflict choice."""

                return not editor.saving

            self._wait_until(saved)
            self.assertEqual(self.path.read_text(), original + "local change\n")
            self.assertFalse(editor.dirty)
            editor.undo()
            self.assertTrue(editor.dirty)
            self.assertTrue(editor.save_button.get_sensitive())
            self.assertEqual(self.path.read_text(), original + "local change\n")
            completed = []
            editor.save(completed.append)

            def saved_undo() -> bool:
                """Wait for saving the version restored by Undo."""

                return bool(completed)

            self._wait_until(saved_undo)
            self.assertEqual(completed, [True])
            self.assertEqual(self.path.read_text(), original)
        finally:
            editor.close()
            editor.destroy()

    def test_keep_my_version_after_undo_writes_and_preserves_failed_save_intent(self) -> None:
        """Undo before the choice cannot suppress overwrite, and failed writes remain retryable."""

        for fail_first in (False, True):
            with self.subTest(failed_write=fail_first):
                self.path.write_text("original\n")
                editor = self._document()
                try:
                    editor.buffer.begin_user_action()
                    editor.buffer.insert(editor.buffer.get_end_iter(), "local edit\n")
                    editor.buffer.end_user_action()
                    self.path.write_text("external version\n")
                    editor._handle_external_change()

                    def conflicted() -> bool:
                        """Wait for the external write to produce its real conflict."""

                        return editor.external_conflict

                    self._wait_until(conflicted)
                    editor.undo()
                    self.assertFalse(editor.dirty)
                    self.assertTrue(editor.external_conflict)
                    if fail_first:
                        with patch("slate.editor.AnchoredFileSave") as write:
                            editor._on_info_response(editor.info_bar, editor.RESPONSE_MINE)
                            self.assertTrue(editor.saving)
                            request = write.call_args.args
                        source = MagicMock()
                        source.replace_contents_finish.side_effect = GLib.Error.new_literal(
                            Gio.io_error_quark(), "Write denied", Gio.IOErrorEnum.PERMISSION_DENIED
                        )
                        request[6](source, None, request[7])
                        self.assertTrue(editor.dirty)
                        self.assertTrue(editor.save_button.get_sensitive())
                        self.assertEqual(self.path.read_text(), "external version\n")
                        editor.save()
                    else:
                        editor._on_info_response(editor.info_bar, editor.RESPONSE_MINE)
                    self.assertTrue(editor.saving)

                    def saved() -> bool:
                        """Wait for the real overwrite or retry to reach disk."""

                        return not editor.saving

                    self._wait_until(saved)
                    self.assertEqual(self.path.read_text(), "original\n")
                    self.assertFalse(editor.force_next_save)
                    self.assertFalse(editor.dirty)
                finally:
                    editor.close()
                    editor.destroy()

    def test_typing_during_external_reload_preserves_the_local_buffer(self) -> None:
        """A late asynchronous disk read becomes a conflict after local typing."""

        editor = self._document()
        editor.loading = True
        editor.buffer.set_text("# Digitato durante il reload\n")
        source = MagicMock()
        source.load_contents_finish.return_value = (
            True,
            b"# Versione agente\n",
            "etag-agente",
        )

        editor.load_cancellable = Gio.Cancellable()
        EditorDocument._on_loaded(editor, source, MagicMock(), ("external", editor.load_cancellable))

        text = editor.buffer.get_text(
            editor.buffer.get_start_iter(), editor.buffer.get_end_iter(), True
        )
        self.assertEqual(text, "# Digitato durante il reload\n")
        self.assertTrue(editor.external_conflict)
        self.assertTrue(editor.attention)
        self.assertTrue(editor.info_bar.get_visible())
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

    def test_workspace_reuses_editor_through_file_and_directory_symlinks(self) -> None:
        """File aliases and linked parent directories share the original buffer."""

        source = self.root / "src"
        source.mkdir()
        (source / "file.md").write_text("# Source\n", encoding="utf-8")
        (self.root / "alias.md").symlink_to(source / "file.md")
        (self.root / "linked").symlink_to(source, target_is_directory=True)
        workspace = EditorWorkspace(
            Gtk.Box(), 10, MagicMock(), MagicMock(), MagicMock(), MagicMock()
        )

        original = workspace.open_file("project", str(self.root), "src/file.md")
        file_alias = workspace.open_file("project", str(self.root), "alias.md")
        directory_alias = workspace.open_file("project", str(self.root), "linked/file.md")

        self.assertIs(file_alias, original)
        self.assertIs(directory_alias, original)
        self.assertEqual(len(workspace.editors), 1)
        self.assertEqual(workspace.active_ref, ("project", "src/file.md"))
        workspace.shutdown()

    def test_revision_preview_e_opens_or_reuses_real_editor_buffer(self) -> None:
        """The complete E path activates one buffer without losing existing edits."""

        for already_open in (False, True):
            with self.subTest(already_open=already_open):
                workspace = EditorWorkspace(
                    Gtk.Box(), 10, MagicMock(), MagicMock(), MagicMock(), MagicMock()
                )
                self.addCleanup(workspace.shutdown)
                existing = None
                if already_open:
                    existing = workspace.open_file("project", str(self.root), "TODO.md")

                    def initial_content_loaded() -> bool:
                        """Wait for the existing buffer before adding an unsaved edit."""

                        return existing.etag is not None and not existing.loading

                    self._wait_until(initial_content_loaded)
                    existing.buffer.insert(existing.buffer.get_end_iter(), "unsaved\n")
                preview = FilePreview(MagicMock())
                preview.current_path = "TODO.md"
                preview._set_diff_text(
                    "diff --git a/TODO.md b/TODO.md\n"
                    "--- a/TODO.md\n+++ b/TODO.md\n@@ -1 +1 @@\n-old\n+new\n"
                )
                preview.set_no_show_all(False)
                preview.show_all()
                preview.set_no_show_all(True)
                panel = Gtk.Box()
                panel.message = Gtk.TextView()
                panel._cancel_pending_preview = MagicMock()
                owner = SimpleNamespace(
                    preview=preview, preview_status=FileStatus("TODO.md", "modified"),
                    panel=panel, editor_workspace=workspace, project_tree=Gtk.TreeView(),
                    active_project_name="project", project_search=MagicMock(),
                    get_focus=MagicMock(return_value=preview), browser_manager=MagicMock(),
                    config=SimpleNamespace(
                        find_project=MagicMock(return_value={"name": "project", "path": str(self.root)}),
                        save=MagicMock(),
                    ),
                    _workspace_repository_path=MagicMock(return_value="TODO.md"),
                    _project_file_path=MagicMock(return_value=str(self.path)),
                    _append_project_item=MagicMock(return_value=False),
                    _select_tree_row=MagicMock(),
                )
                owner.project_search.get_visible.return_value = False
                owner._close_preview = SlateWindow._close_preview.__get__(owner)
                owner._edit_status_internal = SlateWindow._edit_status_internal.__get__(owner)
                owner._open_experimental_file = SlateWindow._open_experimental_file.__get__(owner)
                owner._configure_experimental = SlateWindow._configure_experimental.__get__(owner)
                owner.scm_by_repository = {}
                owner._search_result_status = MagicMock(return_value=None)
                owner._edit_project_file_internal = SlateWindow._edit_project_file_internal.__get__(owner)
                preview.on_action = SlateWindow._handle_file_preview_action.__get__(owner)
                SlateWindow._protect_preview_background(owner)
                event = SimpleNamespace(keyval=Gdk.KEY_e, state=Gdk.ModifierType(0))

                self.assertTrue(SlateWindow._on_key_press(owner, None, event))

                self.assertFalse(preview.get_visible())
                self.assertTrue(workspace.get_sensitive())
                self.assertEqual(len(workspace.editors), 1)
                if existing is not None:
                    self.assertIs(workspace.current_editor(), existing)
                    self.assertTrue(existing.dirty)
                    self.assertEqual(
                        existing.buffer.get_text(
                            existing.buffer.get_start_iter(), existing.buffer.get_end_iter(), True
                        ),
                        "# TODO\nunsaved\n",
                    )
                else:
                    self.assertEqual(workspace.current_editor().relative_path, "TODO.md")
                workspace.shutdown()

    def test_saving_file_symlink_updates_target_without_replacing_link(self) -> None:
        """The editor writes the resolved target and preserves a file alias."""

        alias = self.root / "alias.md"
        alias.symlink_to(self.path)
        editor = EditorDocument(
            "project", str(self.root), "alias.md", 11,
            self._state_changed, self._removed,
        )
        self._wait_until(lambda: not editor.loading and editor.etag is not None)
        editor.buffer.set_text("# Updated\n")
        completed: list[bool] = []

        editor.save(completed.append)

        self._wait_until(lambda: bool(completed))
        self.assertEqual(completed, [True])
        self.assertTrue(alias.is_symlink())
        self.assertEqual(self.path.read_text(encoding="utf-8"), "# Updated\n")
        editor.close()

    def test_workspace_dispatches_every_documented_editor_shortcut(self) -> None:
        """The complete Ctrl shortcut matrix reaches only its intended command."""

        editor = MagicMock()
        editor.experimental = None
        editor.go_to_definition.return_value = True
        owner = SimpleNamespace(
            current_editor=MagicMock(return_value=editor),
            request_close=MagicMock(),
        )

        cases = (
            (Gdk.KEY_s, Gdk.ModifierType.CONTROL_MASK, editor.save),
            (Gdk.KEY_b, Gdk.ModifierType.CONTROL_MASK, editor.go_to_definition),
            (Gdk.KEY_f, Gdk.ModifierType.CONTROL_MASK, editor.show_search),
            (Gdk.KEY_g, Gdk.ModifierType.CONTROL_MASK, editor.goto_line),
            (Gdk.KEY_w, Gdk.ModifierType.CONTROL_MASK, owner.request_close),
            (Gdk.KEY_z, Gdk.ModifierType.CONTROL_MASK, editor.undo),
            (
                Gdk.KEY_z,
                Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK,
                editor.redo,
            ),
            (Gdk.KEY_y, Gdk.ModifierType.CONTROL_MASK, editor.redo),
        )
        for keyval, state, callback in cases:
            before = callback.call_count
            event = SimpleNamespace(keyval=keyval, state=state)
            self.assertTrue(EditorWorkspace.handle_key(owner, event))
            self.assertEqual(callback.call_count, before + 1)
        owner.request_close.assert_called_once_with(editor)

        plain = SimpleNamespace(keyval=Gdk.KEY_s, state=Gdk.ModifierType(0))
        unknown = SimpleNamespace(
            keyval=Gdk.KEY_x, state=Gdk.ModifierType.CONTROL_MASK
        )
        self.assertFalse(EditorWorkspace.handle_key(owner, plain))
        self.assertFalse(EditorWorkspace.handle_key(owner, unknown))

    def test_ctrl_b_requests_definition_for_the_symbol_at_cursor(self) -> None:
        """The active editor forwards Ctrl+B with project source context."""

        requested = MagicMock()
        workspace = EditorWorkspace(
            Gtk.Box(),
            10,
            MagicMock(),
            MagicMock(),
            MagicMock(),
            MagicMock(),
            requested,
        )
        editor = workspace.open_file("project", str(self.root), "TODO.md")

        def editor_loaded() -> bool:
            """Report when the asynchronously materialized editor is ready."""

            return not editor.loading

        self._wait_until(editor_loaded)
        editor.buffer.set_text("function deploy() {}\ndeploy();\n")
        editor.buffer.place_cursor(editor.buffer.get_iter_at_offset(23))
        event = SimpleNamespace(
            keyval=Gdk.KEY_b,
            state=Gdk.ModifierType.CONTROL_MASK,
        )

        self.assertTrue(workspace.handle_key(event))
        requested.assert_called_once_with(str(self.root), "TODO.md", "deploy")
        workspace.shutdown()

    def test_definition_line_waits_for_scheduled_initial_load(self) -> None:
        """A newly opened definition applies its line after content is loaded."""

        self.path.write_text("first\nsecond\ntarget\n", encoding="utf-8")
        workspace = EditorWorkspace(
            Gtk.Box(),
            10,
            MagicMock(),
            MagicMock(),
            MagicMock(),
            MagicMock(),
        )
        editor = workspace.open_file("project", str(self.root), "TODO.md")
        editor.goto_line_number(3)

        self.assertEqual(editor.pending_line, 3)

        def initial_load_finished() -> bool:
            """Report when both the scheduled and asynchronous load have ended."""

            return (
                editor.initial_load_id is None
                and not editor.loading
                and editor.line_navigation_id is None
            )

        with patch.object(editor.view, "grab_focus") as grab_focus:
            self._wait_until(initial_load_finished)
        grab_focus.assert_not_called()
        cursor = editor.buffer.get_iter_at_mark(editor.buffer.get_insert())
        self.assertEqual(cursor.get_line(), 2)
        self.assertIsNone(editor.pending_line)
        workspace.shutdown()

    def test_line_navigation_idle_does_not_repeat_focus_request(self) -> None:
        """Delayed scroll confirmation leaves the user's newer focus choice alone."""

        editor = self._document()
        self.addCleanup(editor.close)
        with patch.object(editor.view, "grab_focus") as grab_focus, patch(
            "slate.editor.GLib.idle_add", return_value=71
        ) as idle:
            editor.goto_line_number(1)
            grab_focus.assert_called_once_with()
            grab_focus.reset_mock()
            callback, *arguments = idle.call_args.args
            self.assertFalse(callback(*arguments))
            grab_focus.assert_not_called()

    def test_delayed_line_navigation_preserves_newer_cursor_selection_and_edits(self) -> None:
        """An idle scroll confirmation never replaces newer user navigation or selection."""

        editor = self._document()
        self.addCleanup(editor.close)
        for action in ("cursor", "selection", "typing"):
            with self.subTest(action=action):
                editor.buffer.set_text("one\ntwo\nthree\n")
                with patch("slate.editor.GLib.idle_add", return_value=71) as idle:
                    editor.goto_line_number(2, focus=False)
                callback, *arguments = idle.call_args.args
                if action == "cursor":
                    editor.buffer.place_cursor(editor.buffer.get_iter_at_line(2))
                elif action == "selection":
                    editor.buffer.select_range(editor.buffer.get_iter_at_line(1),
                                               editor.buffer.get_iter_at_line(2))
                else:
                    editor.buffer.insert_at_cursor("new text")
                cursor = editor.buffer.get_iter_at_mark(editor.buffer.get_insert()).get_offset()
                bound = editor.buffer.get_iter_at_mark(editor.buffer.get_selection_bound()).get_offset()
                with patch.object(editor.view, "scroll_to_iter") as scroll:
                    self.assertFalse(callback(*arguments))
                    scroll.assert_not_called()
                self.assertEqual(editor.buffer.get_iter_at_mark(editor.buffer.get_insert()).get_offset(), cursor)
                self.assertEqual(editor.buffer.get_iter_at_mark(editor.buffer.get_selection_bound()).get_offset(), bound)

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

    def test_restore_collapses_existing_symlink_alias_tabs(self) -> None:
        """Old duplicate references restore one lazy editor and its active row."""

        alias = self.root / "alias.md"
        alias.symlink_to(self.path)
        terminal_page = Gtk.Box()
        workspace = EditorWorkspace(
            terminal_page, 10, MagicMock(), MagicMock(), MagicMock(), MagicMock()
        )
        state = {
            "tabs": [
                {"project": "project", "path": "TODO.md"},
                {"project": "project", "path": "alias.md"},
            ],
            "active_tab": {"project": "project", "path": "alias.md"},
        }

        workspace.restore(state, [{"name": "project", "path": str(self.root)}])

        self.assertEqual(list(workspace.editors), [("project", "TODO.md")])
        self.assertEqual(workspace.active_ref, ("project", "TODO.md"))
        self.assertEqual(workspace.get_children(), [terminal_page])
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

    def test_binary_file_is_not_left_open_in_editor_sidebar(self) -> None:
        """A rejected WebP-like initial load removes its editor identity."""

        (self.root / "image.webp").write_bytes(b"RIFF\x00\x00\x00\x00WEBP\x00")
        error = MagicMock()
        persisted = MagicMock()
        workspace = EditorWorkspace(
            Gtk.Box(),
            10,
            persisted,
            error,
            MagicMock(),
            MagicMock(),
        )
        reference = ("project", "image.webp")

        workspace.open_file(reference[0], str(self.root), reference[1])
        self._wait_until(lambda: reference not in workspace.editors)

        error.assert_called_once_with("Binary file: internal editing is unavailable.")
        self.assertIsNone(workspace.active_ref)
        self.assertIs(workspace.get_visible_child(), workspace.terminal_page)
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
