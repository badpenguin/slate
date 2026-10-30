"""GTK model tests for reversible experimental file presentations."""

import json
from pathlib import Path
import tempfile
import time
from dataclasses import asdict
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GtkSource", "4")
from gi.repository import Gdk, Gio, GLib, Gtk

from slate.editor import EditorDocument
from slate.editor_diff_model import project_diff
from slate.experimental_editor import ExperimentalEditor
from slate.processes import CommandResult
from slate.file_manager import ProjectFileManager
from slate.window import SlateWindow


class ExperimentalEditorTest(unittest.TestCase):
    """Exercise actual buffers and views without constructing a SLATE window."""

    @classmethod
    def setUpClass(cls) -> None:
        """Require an isolated GTK test display."""

        if not Gtk.init_check(None)[0]:
            raise unittest.SkipTest("GTK display unavailable")

    def setUp(self) -> None:
        """Build one inert document, bypassing asynchronous disk loading."""

        self.temporary = tempfile.TemporaryDirectory(prefix="slate-editor-", dir="/tmp")
        self.editor = EditorDocument("project", self.temporary.name, "style.css", 11, MagicMock(), MagicMock())
        GLib.source_remove(self.editor.initial_load_id)
        self.editor.initial_load_id = None
        self.editor.buffer.begin_not_undoable_action()
        self.editor.buffer.set_text("font-size: 12px;\n")
        self.editor.buffer.end_not_undoable_action()
        self.editor.buffer.set_modified(False)
        self.parent = Gtk.Stack()
        self.parent.add_named(self.editor, "document")
        self.presentation = ExperimentalEditor(self.editor)
        self.editor.experimental = self.presentation
        self.presentation.base = "font-size: 10px;\n"
        self.presentation.old_buffer.set_text(self.presentation.base)
        self.presentation.rows = project_diff(self.presentation.base, self.presentation.text())
        self.presentation.rows_serial = self.presentation.serial
        self.presentation.rows_base_serial = self.presentation.base_serial
        self.presentation._replace_spans()
        self.presentation.attach()
        self.presentation.set_mode("unified")

    def tearDown(self) -> None:
        """Close only test resources and remove the private temporary directory."""

        self.editor.close()
        self.parent.destroy()
        self.temporary.cleanup()

    def test_initial_diff_runs_without_typing_debounce(self) -> None:
        """The first requested diff is scheduled immediately rather than after typing delay."""

        self.presentation.rows_serial = -1
        with patch.object(self.editor, "get_mapped", return_value=True), patch(
                "slate.experimental_editor.GLib.idle_add", return_value=123) as idle, patch(
                "slate.experimental_editor.GLib.timeout_add") as delayed:
            self.presentation.schedule()
            idle.assert_called_once_with(self.presentation._refresh)
            delayed.assert_not_called()
        self.presentation.debounce = None

    def test_return_to_ready_diff_does_not_rebuild_or_recompute(self) -> None:
        """Returning to an unchanged document keeps the visible diff and its projection."""

        self.presentation.set_mode("compact")
        with patch.object(self.editor, "get_mapped", return_value=True), patch.object(
                self.presentation, "_show_mode") as show, patch(
                "slate.experimental_editor.GLib.idle_add") as idle, patch(
                "slate.experimental_editor.GLib.timeout_add") as delayed:
            self.presentation._mapped(self.editor)
            show.assert_not_called()
            idle.assert_not_called()
            delayed.assert_not_called()
        self.assertEqual(self.presentation.stack.get_visible_child_name(), "unified")

    def test_renderer_reuses_unchanged_content_but_reloads_text_and_path_changes(self) -> None:
        """Mode switches and refreshes preserve WebKit state until the document actually changes."""

        self.editor.relative_path = "document.md"
        renderer = MagicMock()
        renderer.view = Gtk.Box()
        renderer.resource_paths = {"styles/page.css"}
        with patch("slate.experimental_editor.DocumentRenderer", return_value=renderer), patch.object(
                self.editor, "get_mapped", return_value=True):
            self.presentation.set_mode("rendered")
            self.presentation._render()
            self.presentation._refresh()
            self.presentation.set_mode("code")
            self.presentation.set_mode("rendered")
            renderer.render.assert_called_once_with("font-size: 12px;\n")
            self.presentation.resource_changed("unrelated.txt")
            self.presentation._render()
            self.assertEqual(renderer.render.call_count, 1)
            self.presentation.resource_changed("styles/page.css")
            self.presentation._render()
            self.assertEqual(renderer.render.call_count, 2)
            renderer.render.reset_mock()
            self.editor.buffer.insert(self.editor.buffer.get_end_iter(), "changed\n")
            self.presentation._render()
            self.assertEqual(renderer.render.call_count, 1)
            self.editor.relative_path = "moved/document.md"
            self.presentation._render()
            self.assertEqual(renderer.render.call_count, 2)
            self.editor.historical = True
            self.presentation.base = "historical content\n"
            self.presentation._render()
            self.presentation._render()
            self.assertEqual(renderer.render.call_count, 3)
            renderer.render.assert_called_with("historical content\n")

    def test_supplemental_file_events_invalidate_renderer_without_an_scm_watcher(self) -> None:
        """Files monitors forward changed and renamed assets even while their panel is inactive."""

        self.editor.relative_path = "page.html"
        renderer = MagicMock()
        renderer.view = Gtk.Box()
        renderer.resource_paths = {"page.css"}
        owner = SimpleNamespace(editor_workspace=SimpleNamespace(
            editors={"page": SimpleNamespace(document=self.editor)}, preview_entry=None, historical_document=None))
        owner._editor_resource_changed = SlateWindow._editor_resource_changed.__get__(owner)
        files = ProjectFileManager(*[MagicMock() for index in range(9)], on_file_change=owner._editor_resource_changed)
        files.root = self.temporary.name
        files.project_name = "project"
        files.active = False
        self.addCleanup(files.destroy)
        css = Path(self.temporary.name) / "page.css"
        css.write_text("body{color:red}")
        with patch("slate.experimental_editor.DocumentRenderer", return_value=renderer), patch.object(
                self.editor, "get_mapped", return_value=True):
            self.presentation.set_mode("rendered")
            renderer.render.assert_called_once()
            css.write_text("body{color:blue}")
            files._on_supplemental_change(None, Gio.File.new_for_path(str(css)), None, Gio.FileMonitorEvent.CHANGED)
            self.assertTrue(files.dirty)
            self.assertIsNone(self.presentation.rendered_document)
            self.presentation._render()
            self.assertEqual(renderer.render.call_count, 2)
            files._on_supplemental_change(None, Gio.File.new_for_path(str(css.parent / "temporary.css")),
                                         Gio.File.new_for_path(str(css)), Gio.FileMonitorEvent.RENAMED)
            self.assertIsNone(self.presentation.rendered_document)
            self.presentation._render()
            self.assertEqual(renderer.render.call_count, 3)

    def test_hunk_positions_handle_multiple_removed_lines_and_deletions_at_eof(self) -> None:
        """Index-based hunk lookup keeps replacement and deletion targets identical."""

        for before, current, expected in (
                ("same\nold-a\nold-b\ntail\n", "same\nnew\ntail\n", [1]),
                ("same\nold-a\nold-b\ntail\n", "same\ntail\n", [1]),
                ("same\nold-a\nold-b\n", "same", [0])):
            with self.subTest(current=current):
                self.editor.buffer.set_text(current)
                self.presentation.base = before
                with patch.object(self.editor, "get_mapped", return_value=True), patch(
                        "slate.experimental_editor.run_async") as run:
                    self.presentation._refresh()
                    run.call_args.args[1](CommandResult((), 0, json.dumps([
                        asdict(row) for row in project_diff(before, current)]), ""))
                self.assertEqual(self.presentation.hunks, expected)
                self.assertEqual(self.presentation.text(), current)

    def test_css_replacement_saves_only_current_text_and_undo_is_shared(self) -> None:
        """Editing 12px to 14px changes the canonical source without saving old lines."""

        buffer = self.presentation.projection
        text = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True)
        offset = text.index("12px")
        buffer.delete(buffer.get_iter_at_offset(offset), buffer.get_iter_at_offset(offset + 2))
        buffer.insert(buffer.get_iter_at_offset(offset), "14")
        self.assertEqual(self.presentation.text(), "font-size: 14px;\n")
        self.assertTrue(self.editor.dirty)
        self.editor.undo()
        self.assertEqual(self.presentation.text(), "font-size: px;\n")
        self.editor.undo()
        self.assertEqual(self.presentation.text(), "font-size: 12px;\n")

    def test_repeated_typing_and_multiline_insertion_keep_mappings(self) -> None:
        """Native visual insertions remain editable before a worker result returns."""

        buffer = self.presentation.projection
        text = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True)
        offset = text.index("12px")
        for character in "abc":
            buffer.insert(buffer.get_iter_at_offset(offset), character)
            offset += 1
        self.assertEqual(self.presentation.text(), "font-size: abc12px;\n")
        buffer.insert(buffer.get_end_iter(), "a\nb\n")
        self.assertTrue(self.presentation.text().endswith("a\nb\n"))

    def test_deleted_rows_are_protected_and_crossing_selection_deletes_only_current(self) -> None:
        """Even programmatic deletions cannot copy historical rows into the source."""

        buffer = self.presentation.projection
        buffer.insert(buffer.get_start_iter(), "forbidden")
        self.assertNotIn("forbidden", self.presentation.text())
        buffer.delete(buffer.get_start_iter(), buffer.get_end_iter())
        self.assertEqual(self.presentation.text(), "")
        remaining = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True)
        self.assertIn("10px", remaining)
        self.editor.undo()
        self.assertEqual(self.presentation.text(), "font-size: 12px;\n")


    def test_one_native_replacement_has_one_canonical_undo(self) -> None:
        """A paste-style delete/insert action must undo as one user operation."""

        buffer = self.presentation.projection
        text = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True)
        start = text.index("12px")
        buffer.begin_user_action()
        buffer.delete(buffer.get_iter_at_offset(start), buffer.get_iter_at_offset(start + 2))
        buffer.insert(buffer.get_iter_at_offset(start), "14")
        buffer.end_user_action()
        self.assertEqual(self.presentation.text(), "font-size: 14px;\n")
        self.editor.undo()
        self.assertEqual(self.presentation.text(), "font-size: 12px;\n")
        self.assertFalse(self.editor.buffer.can_undo())


    def test_native_clipboard_replaces_mixed_history_and_current_selection_with_one_undo(self) -> None:
        """Real GTK paste replaces only selected current text, preserving history and source undo."""

        for mode in ("unified", "compact"):
            with self.subTest(mode=mode):
                self.editor.buffer.begin_not_undoable_action()
                self.editor.buffer.set_text("keep\nnew\nend\n")
                self.editor.buffer.end_not_undoable_action()
                self.editor.buffer.set_modified(False)
                self.presentation.base = "keep\nold\nend\n"
                self.presentation.rows = project_diff(self.presentation.base, self.presentation.text())
                self.presentation.rows_serial = self.presentation.serial
                self.presentation.rows_base_serial = self.presentation.base_serial
                self.presentation.set_mode(mode)
                self.presentation._replace_spans()
                self.presentation._render_projection()
                buffer = self.presentation.projection
                buffer.select_range(buffer.get_start_iter(), buffer.get_end_iter())
                clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
                clipboard.set_text("replacement\n", -1)
                completed = []

                def pasted(_buffer, _clipboard) -> None:
                    """Record completion of GTK's actual asynchronous clipboard transfer."""

                    completed.append(True)

                signal = buffer.connect("paste-done", pasted)
                try:
                    buffer.paste_clipboard(clipboard, None, True)
                    deadline = time.monotonic() + 3
                    context = GLib.MainContext.default()
                    while not completed and time.monotonic() < deadline:
                        while context.pending():
                            context.iteration(False)
                        time.sleep(0.002)
                    self.assertTrue(completed)
                    self.assertEqual(self.presentation.text(), "replacement\n")
                    visual = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True)
                    self.assertIn("old\n", visual)
                    self.editor.undo()
                    self.assertEqual(self.presentation.text(), "keep\nnew\nend\n")
                    self.assertFalse(self.editor.buffer.can_undo())
                finally:
                    buffer.disconnect(signal)
                    clipboard.clear()

    def test_fragmented_worker_result_preserves_previous_projection_and_recovers_after_edit(self) -> None:
        """A rejected projection never publishes valid serials or destroys the old marks and text."""

        before = "".join(f"line {index}\n" for index in range(1800))
        current = "".join(f"changed {index}\n" if index % 2 else f"line {index}\n" for index in range(1800))
        self.editor.buffer.set_text(current)
        self.presentation.base = before
        old_rows = self.presentation.rows
        old_serial = self.presentation.rows_serial
        old_spans = list(self.presentation.spans)
        buffer = self.presentation.projection
        previous_visual = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True)
        rows = project_diff(before, current)
        with patch.object(self.editor, "get_mapped", return_value=True), patch.object(
                self.presentation, "schedule"), patch("slate.experimental_editor.run_async") as run:
            self.presentation._refresh()
            run.call_args.args[1](CommandResult((), 0, json.dumps([asdict(row) for row in rows]), ""))
            self.assertIs(self.presentation.rows, old_rows)
            self.assertEqual(self.presentation.rows_serial, old_serial)
            self.assertNotEqual(self.presentation.rows_serial, self.presentation.serial)
            self.assertEqual(self.presentation.spans, old_spans)
            self.assertTrue(all(not span.start.get_deleted() for span in old_spans if span.start is not None))
            self.assertEqual(buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True), previous_visual)
            self.assertFalse(self.presentation.mode_buttons["unified"].get_sensitive())
            self.assertIn("fragmented", self.presentation.notice.get_text())
            self.presentation.set_mode("code")
            self.presentation.set_mode("unified")
            self.assertEqual(self.presentation.mode, "code")
            self.assertEqual(self.presentation.text(), current)
            self.editor.buffer.set_text("new\n")
            self.presentation.base = "old\n"
            self.presentation.base_serial += 1
            self.presentation.set_mode("unified")
            self.presentation._refresh()
            run.call_args.args[1](CommandResult((), 0, json.dumps([
                asdict(row) for row in project_diff("old\n", "new\n")]), ""))
            self.assertEqual(self.presentation.rows_serial, self.presentation.serial)
            self.assertTrue(self.presentation.mode_buttons["unified"].get_sensitive())
            self.assertEqual(self.presentation.text(), "new\n")

    def test_cached_projection_error_during_mode_switch_is_handled_without_losing_marks(self) -> None:
        """A cached full diff may fail compact validation without breaking the working full view."""

        previous_spans = list(self.presentation.spans)
        with patch.object(self.presentation, "_replace_spans", side_effect=ValueError("Diff too fragmented")):
            self.presentation.set_mode("compact")
        self.assertEqual(self.presentation.mode, "unified")
        self.assertEqual(self.presentation.spans, previous_spans)
        self.assertFalse(self.presentation.mode_buttons["compact"].get_sensitive())
        self.assertTrue(self.presentation.mode_buttons["unified"].get_sensitive())
        self.presentation.set_mode("code")
        self.assertEqual(self.presentation.mode, "code")

    def test_large_equal_block_uses_a_constant_number_of_marks(self) -> None:
        """An unchanged large file is represented by one span rather than a mark per line."""

        self.editor.buffer.set_text("line\n" * 10000)
        self.presentation.rows = project_diff(self.presentation.text(), self.presentation.text())
        self.presentation._replace_spans()
        self.assertEqual(len(self.presentation.spans), 1)
        self.presentation._render_projection()
        self.assertEqual(self.presentation.text(), "line\n" * 10000)

    def test_default_choice_happens_once_and_respects_markdown(self) -> None:
        """Choosing Rendered for a clean document does not later switch on dirty changes."""

        self.editor.relative_path = "readme.md"
        self.presentation.automatic = True
        self.presentation.initial_chosen = False
        self.presentation.base_identity = (None, "readme.md", None, None)
        self.presentation.base = self.presentation.text()
        with patch.object(self.presentation, "_show_mode"):
            self.presentation._choose_initial(False)
            self.assertEqual(self.presentation.mode, "rendered")
            self.editor.buffer.insert(self.editor.buffer.get_end_iter(), "changed")
            self.presentation._document_ready(self.editor.buffer)
            self.assertEqual(self.presentation.mode, "rendered")

    def test_actual_save_uses_only_the_canonical_css(self) -> None:
        """The normal Gio save path never receives synthetic historical lines."""

        path = Path(self.temporary.name) / "style.css"
        path.write_text("font-size: 12px;\n")
        buffer = self.presentation.projection
        text = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True)
        start = text.index("12px")
        buffer.delete(buffer.get_iter_at_offset(start), buffer.get_iter_at_offset(start + 2))
        buffer.insert(buffer.get_iter_at_offset(start), "14")
        completed = []
        loop = GLib.MainLoop()

        def saved(success: bool) -> None:
            """Complete the test after the actual asynchronous save finishes."""

            completed.append(success)
            loop.quit()

        def expired() -> bool:
            """Bound the isolated save verification."""

            loop.quit()
            return GLib.SOURCE_REMOVE

        timeout = GLib.timeout_add(3000, expired)
        try:
            self.editor.save(saved)
            if not completed:
                loop.run()
            self.assertEqual(completed, [True])
            self.assertEqual(path.read_text(), "font-size: 14px;\n")
        finally:
            GLib.source_remove(timeout)


    def test_outdated_baseline_does_not_overwrite_the_current_request(self) -> None:
        """Changing repository context invalidates both read callbacks and their timeout."""

        from slate.processes import CommandResult
        scm = SimpleNamespace(root=self.temporary.name, kind="hg", environment={})
        with patch("slate.experimental_editor.run_async") as run:
            self.presentation.configure_base(scm, "first.css", "modified")
            old_callback = run.call_args.args[1]
            self.presentation.configure_base(scm, "second.css", "modified")
            new_callback = run.call_args.args[1]
            old_callback(CommandResult((), 0, "old baseline", ""))
            self.assertIsNone(self.presentation.base)
            new_callback(CommandResult((), 0, "current baseline", ""))
            self.assertEqual(self.presentation.base, "current baseline")

    def test_historical_document_cannot_be_saved_or_recreated(self) -> None:
        """Viewing a deleted file never creates a replacement on disk."""

        from slate.editor import EditorWorkspace
        workspace = EditorWorkspace(Gtk.Box(), 11, MagicMock(), MagicMock(), MagicMock(), MagicMock())
        document = workspace.open_historical("project", self.temporary.name, "deleted.css")
        self.assertFalse(document.view.get_editable())
        self.assertEqual(workspace.editors, {})
        document.buffer.set_text("not a working copy")
        saved = []
        document.save(saved.append)
        self.assertEqual(saved, [False])
        self.assertFalse((Path(self.temporary.name) / "deleted.css").exists())
        workspace.shutdown()
        workspace.destroy()


    def test_side_search_uses_the_focused_version(self) -> None:
        """Find in the immutable baseline does not redirect to the editable source."""

        self.presentation.set_mode("side")
        self.presentation._side_focus(self.presentation.old_view, None)
        self.assertTrue(self.presentation.show_search())
        self.assertTrue(self.presentation.old_search.get_reveal_child())
        self.presentation._side_focus(self.editor.view, None)
        self.assertFalse(self.presentation.show_search())

    def test_append_after_deleted_tail_and_after_fully_deleted_file(self) -> None:
        """Historical tail rows must not remove the canonical EOF editing position."""

        for current in ("a\n", ""):
            with self.subTest(current=current):
                self.editor.buffer.set_text(current)
                self.presentation.rows = project_diff("a\nb\n", current)
                self.presentation._replace_spans()
                self.presentation._render_projection()
                self.presentation.projection.insert(self.presentation.projection.get_end_iter(), "appended\n")
                self.assertEqual(self.presentation.text(), current + "appended\n")

    def test_icon_buttons_are_exclusive_and_keep_unavailable_modes_disabled(self) -> None:
        """Each visible icon directly selects a mode and the active one cannot switch off."""

        buttons = self.presentation.mode_buttons
        self.assertEqual(len(buttons), 5)
        self.assertTrue(buttons["unified"].get_active())
        self.assertFalse(buttons["rendered"].get_sensitive())
        buttons["side"].clicked()
        self.assertEqual(self.presentation.mode, "side")
        self.assertEqual(sum(button.get_active() for button in buttons.values()), 1)
        buttons["side"].clicked()
        self.assertTrue(buttons["side"].get_active())
        self.assertEqual(self.presentation.mode, "side")
        for button in buttons.values():
            content = button.get_child()
            self.assertEqual(content.get_spacing(), 4)
            icon, label = content.get_children()
            self.assertIsInstance(icon, Gtk.Image)
            self.assertEqual(label.get_text(), button.get_tooltip_text())
        self.presentation.base = None
        self.presentation._sync_mode_buttons()
        self.assertFalse(buttons["unified"].get_sensitive())
        self.assertFalse(buttons["side"].get_sensitive())
        self.assertTrue(buttons["code"].get_sensitive())

    def test_source_edit_in_side_by_side_and_defaults_are_not_reselected(self) -> None:
        """Editing never changes a mode already selected for the open document."""

        self.presentation.set_mode("side")
        self.editor.buffer.insert(self.editor.buffer.get_end_iter(), "/* test */")
        self.presentation._document_ready(self.editor.buffer)
        self.assertEqual(self.presentation.mode, "side")
        self.assertFalse(self.presentation.old_view.get_editable())
        self.assertTrue(self.editor.view.get_editable())

    def test_projection_retains_cursor_and_selection_across_recalculation(self) -> None:
        """A new diff layout restores positions using canonical offsets."""

        buffer = self.presentation.projection
        offset = len(self.presentation.base) + 11
        buffer.select_range(buffer.get_iter_at_offset(offset + 2), buffer.get_iter_at_offset(offset))
        self.presentation.rows = project_diff(self.presentation.base, self.presentation.text())
        self.presentation._replace_spans()
        self.presentation._render_projection()
        bounds = buffer.get_selection_bounds()
        self.assertTrue(bounds)
        self.assertEqual(buffer.get_text(*bounds, True), "12")
