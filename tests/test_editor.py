"""GTK tests for persistent internal GtkSource editor behavior."""

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

    def test_oversized_file_is_rejected_before_reading_contents(self) -> None:
        """The metadata preflight never starts a content read for an oversized file."""

        editor = self._document()
        original_file = editor.file
        source = MagicMock()
        source.query_info_finish.return_value.get_size.return_value = editor.MAX_TEXT_BYTES + 1
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
        try:
            editor.buffer.set_text("first snapshot\n")
            editor.save(callbacks.append)
            request = source.replace_contents_bytes_async.call_args.args
            self.assertEqual(request[0].get_data(), b"first snapshot\n")
            editor.buffer.insert(editor.buffer.get_end_iter(), "later typing\n")
            source.replace_contents_finish.return_value = (True, "saved-first")
            request[5](source, MagicMock(), request[6])
            self.assertTrue(editor.dirty)
            self.assertTrue(editor.save_button.get_sensitive())
            self.assertEqual(callbacks, [False])
            self.assertEqual(editor.etag, "saved-first")
            editor.save(callbacks.append)
            request = source.replace_contents_bytes_async.call_args.args
            self.assertEqual(request[0].get_data(), b"first snapshot\nlater typing\n")
            self.assertEqual(request[1], "saved-first")
            source.replace_contents_finish.return_value = (True, "saved-second")
            request[5](source, MagicMock(), request[6])
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
        try:
            editor.buffer.begin_user_action()
            editor.buffer.insert(editor.buffer.get_end_iter(), "first edit\n")
            editor.buffer.end_user_action()
            editor.save()
            request = source.replace_contents_bytes_async.call_args.args
            saved_text = request[0].get_data().decode("utf-8")
            editor.buffer.begin_user_action()
            editor.buffer.insert(editor.buffer.get_end_iter(), "later edit\n")
            editor.buffer.end_user_action()
            source.replace_contents_finish.return_value = (True, "saved-first")
            request[5](source, MagicMock(), request[6])
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
        original_write = old_file.replace_contents_bytes_async
        saved = []
        try:
            editor.buffer.set_text("edited\n")
            with patch.object(old_file, "replace_contents_bytes_async") as write:
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
            with patch.object(editor.file, "replace_contents_bytes_async"):
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
            callback, line = idle.call_args.args
            self.assertFalse(callback(line))
            grab_focus.assert_not_called()

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
