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

from slate.editor import EditorDocument, EditorWorkspace
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
        self.editor.view.set_editable(True)
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

    def test_goto_changed_line_accepts_typing_and_preserves_undo_in_each_mode(self) -> None:
        """Navigate to a changed line and edit the live version through native GTK actions."""

        before = "".join(f"row {number}: width = 10;\n" for number in range(1, 31))
        current = before.replace("row 11: width = 10;", "row 11: width = 14;")
        expected = current.replace("row 11: width = 14;", "/* caffè */ row 11: width = 14;")
        # 2026-11-07: verificare lo stesso gesto nelle quattro viste distingue
        # gli errori di proiezione dai problemi del buffer e della sua history.
        for mode in ("compact", "unified", "side", "code"):
            with self.subTest(mode=mode):
                self.editor.buffer.begin_not_undoable_action()
                self.editor.buffer.set_text(current)
                self.editor.buffer.end_not_undoable_action()
                self.editor.buffer.set_modified(False)
                self.presentation.base = before
                self.presentation.old_buffer.set_text(before)
                self.presentation.rows = project_diff(before, current)
                self.presentation.rows_serial = self.presentation.serial
                self.presentation.rows_base_serial = self.presentation.base_serial
                self.presentation.set_mode(mode)
                if mode in {"compact", "unified"}:
                    self.presentation._replace_spans()
                    self.presentation._render_projection()
                self.editor.goto_line_number(11, focus=False)
                context = GLib.MainContext.default()
                while context.pending():
                    context.iteration(False)
                view = self.presentation.projection_view if mode in {"compact", "unified"} else self.editor.view
                view.emit("insert-at-cursor", "/* caffè */ ")
                self.assertEqual(self.presentation.text(), expected)
                self.editor.undo()
                self.assertEqual(self.presentation.text(), current)
                self.editor.redo()
                self.assertEqual(self.presentation.text(), expected)

    def test_goto_eof_after_trailing_deletion_accepts_typing_and_undo(self) -> None:
        """Navigate past deleted historical tail rows to the live EOF, even for an empty file."""

        for current in ("keep\n", ""):
            for mode in ("compact", "unified", "side", "code"):
                with self.subTest(current=current, mode=mode):
                    self.editor.buffer.begin_not_undoable_action()
                    self.editor.buffer.set_text(current)
                    self.editor.buffer.end_not_undoable_action()
                    self.editor.buffer.set_modified(False)
                    self.presentation.base = "keep\nremoved\n"
                    self.presentation.old_buffer.set_text(self.presentation.base)
                    self.presentation.rows = project_diff(self.presentation.base, current)
                    self.presentation.rows_serial = self.presentation.serial
                    self.presentation.rows_base_serial = self.presentation.base_serial
                    self.presentation.set_mode(mode)
                    if mode in {"compact", "unified"}:
                        self.presentation._replace_spans()
                        self.presentation._render_projection()
                    self.editor.goto_line_number(self.editor.buffer.get_line_count(), focus=False)
                    context = GLib.MainContext.default()
                    while context.pending():
                        context.iteration(False)
                    view = self.presentation.projection_view if mode in {"compact", "unified"} else self.editor.view
                    view.emit("insert-at-cursor", "new last line\n")
                    self.assertEqual(self.presentation.text(), current + "new last line\n")
                    self.editor.undo()
                    self.assertEqual(self.presentation.text(), current)
                    self.editor.redo()
                    self.assertEqual(self.presentation.text(), current + "new last line\n")

    def test_goto_hidden_compact_line_expands_only_its_gap_and_edits_there(self) -> None:
        """Navigation reveals the requested source line and typing uses that exact position."""

        before = "".join(f"line {number}\n" for number in range(1, 101))
        current = before.replace("line 50\n", "changed 50\n")
        self.editor.buffer.set_text(current)
        self.presentation.base = before
        self.presentation.old_buffer.set_text(before)
        self.presentation.rows = project_diff(before, current)
        self.presentation.rows_serial = self.presentation.serial
        self.presentation.rows_base_serial = self.presentation.base_serial
        self.presentation.set_mode("compact")
        self.presentation._replace_spans()
        self.presentation._render_projection()

        for number in (10, 90):
            with self.subTest(line=number):
                self.editor.goto_line_number(number, focus=False)
                source_offset = self.editor.buffer.get_iter_at_line(number - 1).get_offset()
                cursor = self.presentation.projection.get_iter_at_mark(
                    self.presentation.projection.get_insert()
                )
                self.assertEqual(self.presentation._source_offset(cursor.get_offset()), source_offset)
                self.assertEqual(len(self.presentation.expanded_gaps), 1 if number == 10 else 2)
                self.assertEqual(len(self.presentation.expanded_gap_marks), 1 if number == 10 else 2)

        self.editor.goto_line_number(10, focus=False)
        self.assertEqual(len(self.presentation.expanded_gaps), 2)
        self.presentation.projection.insert_at_cursor("INSERTED ")
        self.assertEqual(self.presentation.text(), current.replace("line 10\n", "INSERTED line 10\n"))
        self.editor.undo()
        self.assertEqual(self.presentation.text(), current)

    def test_goto_during_compact_refresh_retains_target_unless_user_moves(self) -> None:
        """Deferred navigation reveals its target and respects later cursor moves or typing."""

        for intervention in ("none", "cursor", "typing"):
            with self.subTest(intervention=intervention):
                p = self.presentation
                before = "".join(f"line {number}\n" for number in range(1, 101))
                current = before.replace("line 50\n", "changed 50\n")
                self.editor.buffer.set_text(current)
                p.base = before
                p.old_buffer.set_text(before)
                p.rows = project_diff(before, current)
                p.rows_serial = p.serial
                p.rows_base_serial = p.base_serial
                p.expanded_gaps.clear()
                for first, last in p.expanded_gap_marks:
                    self.editor.buffer.delete_mark(first)
                    self.editor.buffer.delete_mark(last)
                p.expanded_gap_marks.clear()
                p.set_mode("compact")
                p._replace_spans()
                p._render_projection()
                added = next(span for span in p.spans if span.kind == "added" and span.start is not None)
                p.projection.insert(p.projection.get_iter_at_mark(added.visual_start), "X")
                self.editor.goto_line_number(10, focus=False)
                self.assertIsNotNone(p.pending_navigation)
                if intervention != "none":
                    moved = p.projection.get_iter_at_mark(added.visual_end)
                    moved.backward_chars(2)
                    self.assertNotEqual(moved.get_offset(), p.pending_navigation[1])
                    p.projection.place_cursor(moved)
                    if intervention == "typing":
                        p.projection.insert_at_cursor("Y")
                    intended = p._canonical_cursor()
                else:
                    intended = self.editor.buffer.get_iter_at_line(9).get_offset()
                with patch.object(self.editor, "get_mapped", return_value=True), patch(
                        "slate.experimental_editor.run_async", return_value=MagicMock()) as run:
                    p._refresh()
                rows = project_diff(before, p.text())
                run.call_args.args[1](CommandResult((), 0, json.dumps([asdict(row) for row in rows]), ""))
                self.assertIsNone(p.pending_navigation)
                self.assertEqual(p._canonical_cursor(), intended)
                if intervention == "none":
                    p.projection.insert_at_cursor("INSERTED ")
                    self.assertIn("INSERTED line 10\n", p.text())

    def test_historical_document_initializes_syntax_for_visible_modes(self) -> None:
        """A deleted CSS file has a language without requiring a working-copy load."""

        workspace = EditorWorkspace(Gtk.Box(), 11, MagicMock(), MagicMock(), MagicMock(), MagicMock())
        try:
            document = workspace.open_historical("project", self.temporary.name, "deleted.css")
            p = ExperimentalEditor(document)
            document.experimental = p
            p.attach()
            p.base = "body { color: red; }\n"
            p.old_buffer.set_text(p.base)
            for mode in ("code", "side", "unified", "compact"):
                with self.subTest(mode=mode):
                    p.set_mode(mode)
                    self.assertEqual(p.old_buffer.get_language().get_id(), "css")
                    self.assertEqual(p.old_buffer.get_highlight_syntax(), mode != "compact")
        finally:
            workspace.shutdown()
            workspace.destroy()

    def test_stale_gap_expansion_is_applied_after_worker_refresh(self) -> None:
        """Deleting a visible line then expanding a stale gap waits for valid coordinates."""

        p = self.presentation
        before = "".join(f"line {number}\n" for number in range(1, 101))
        current = before.replace("line 50\n", "changed 50\n")
        self.editor.buffer.set_text(current)
        p.base = before
        p.old_buffer.set_text(before)
        p.rows = project_diff(before, current)
        p.rows_serial = p.serial
        p.rows_base_serial = p.base_serial
        p.set_mode("compact")
        p._replace_spans()
        p._render_projection()
        added = next(span for span in p.spans if span.kind == "added" and span.start is not None)
        p.projection.delete(p.projection.get_iter_at_mark(added.visual_start),
                            p.projection.get_iter_at_mark(added.visual_end))
        gap = [span for span in p.spans if span.gap][-1]
        with patch.object(p, "_replace_spans", wraps=p._replace_spans) as replace:
            self.assertTrue(p.expand_gap_at(p.projection.get_iter_at_mark(gap.visual_start).get_offset()))
            replace.assert_not_called()
        self.assertTrue(p.expanded_gap_marks)
        with patch.object(self.editor, "get_mapped", return_value=True), patch(
                "slate.experimental_editor.run_async", return_value=MagicMock()) as run:
            p._refresh()
        rows = project_diff(before, p.text())
        run.call_args.args[1](CommandResult((), 0, json.dumps([asdict(row) for row in rows]), ""))
        self.assertEqual(p.rows_serial, p.serial)
        self.assertTrue(p.expanded_gaps)
        self.assertIn("line 99\n", p.projection.get_text(p.projection.get_start_iter(), p.projection.get_end_iter(), True))
        self.assertEqual(p.text(), current.replace("changed 50\n", ""))

    def test_historical_goto_line_uses_visible_baseline_in_every_source_mode(self) -> None:
        """Deleted-file navigation uses its baseline in Code, Side, Unified and Compact."""

        workspace = EditorWorkspace(Gtk.Box(), 11, MagicMock(), MagicMock(), MagicMock(), MagicMock())
        try:
            document = workspace.open_historical("project", self.temporary.name, "deleted.txt")
            p = ExperimentalEditor(document)
            document.experimental = p
            p.base = "".join(f"line {number}\n" for number in range(1, 101))
            p.old_buffer.set_text(p.base)
            p.rows = project_diff(p.base, "")
            p.rows_serial = p.serial
            p.rows_base_serial = p.base_serial
            p.hunks = [0]
            p.attach()
            for mode in ("code", "side", "unified", "compact"):
                with self.subTest(mode=mode):
                    p.set_mode(mode)
                    document.goto_line_number(50, focus=False)
                    # Flush first-change scrolling too: its deferred callback
                    # must respect later navigation in the historical buffer.
                    context = GLib.MainContext.default()
                    while context.pending():
                        context.iteration(False)
                    cursor = p.old_buffer.get_iter_at_mark(p.old_buffer.get_insert())
                    self.assertEqual(cursor.get_line(), 49)
                    if mode in {"unified", "compact"}:
                        cursor = p.projection.get_iter_at_mark(p.projection.get_insert())
                        self.assertEqual(cursor.get_line(), 49)
            with patch("slate.editor.Gtk.Dialog") as dialog, patch("slate.editor.Gtk.Entry") as entry:
                dialog.return_value.run.return_value = Gtk.ResponseType.CANCEL
                document.goto_line()
                entry.return_value.set_placeholder_text.assert_called_once_with("1–101")
        finally:
            workspace.shutdown()
            workspace.destroy()

    def test_side_navigation_uses_left_cursor_and_accounts_for_inserted_lines(self) -> None:
        """Previous/Next use the selected baseline location rather than the hidden live caret."""

        for prefix in ("", "extra one\nextra two\n"):
            with self.subTest(prefix=prefix):
                before = "".join(f"line {number}\n" for number in range(1, 101))
                current = prefix + before
                for number in (10, 30, 70):
                    current = current.replace(f"line {number}\n", f"changed {number}\n")
                p = self.presentation
                self.editor.buffer.set_text(current)
                p.base = before
                p.old_buffer.set_text(before)
                p.rows = project_diff(before, current)
                p.rows_serial = p.serial
                p.rows_base_serial = p.base_serial
                shift = prefix.count("\n")
                p.hunks = ([0] if prefix else []) + [9 + shift, 29 + shift, 69 + shift]
                p.set_mode("side")
                for old_line, delta, expected in ((69, -1, 29 + shift), (29, 1, 69 + shift)):
                    with self.subTest(old_line=old_line, delta=delta):
                        self.editor.buffer.place_cursor(self.editor.buffer.get_start_iter())
                        p.old_buffer.place_cursor(p.old_buffer.get_iter_at_line(old_line))
                        p._side_focus(p.old_view, None)
                        if delta < 0:
                            p._previous(None)
                        else:
                            p._next(None)
                        cursor = self.editor.buffer.get_iter_at_mark(self.editor.buffer.get_insert())
                        self.assertEqual(cursor.get_line(), expected)
                self.assertEqual(p.text(), current)

    def test_cut_rejects_historical_only_selection_without_changing_clipboard(self) -> None:
        """Read-only historical selections disable Cut and cannot clear a previous clipboard value."""

        p = self.presentation
        removed = next(span for span in p.spans if span.kind == "removed")
        p.projection.select_range(p.projection.get_iter_at_mark(removed.visual_start),
                                 p.projection.get_iter_at_mark(removed.visual_end))
        menu = Gtk.Menu()
        try:
            p._projection_menu(p.projection_view, menu)
            cut = next(item for item in menu.get_children() if item.get_label() == "Cut")
            self.assertFalse(cut.get_sensitive())
            original = p.text()
            with patch.object(p.projection_view, "get_clipboard") as clipboard:
                p.projection_view.emit("cut-clipboard")
            clipboard.assert_not_called()
            self.assertEqual(p.text(), original)
        finally:
            menu.destroy()

    def test_cut_of_mixed_selection_copies_and_removes_only_live_text(self) -> None:
        """A mixed diff selection remains cuttable and has one canonical undo operation."""

        p = self.presentation
        original = p.text()
        p.projection.select_range(p.projection.get_start_iter(), p.projection.get_end_iter())
        menu = Gtk.Menu()
        try:
            p._projection_menu(p.projection_view, menu)
            cut = next(item for item in menu.get_children() if item.get_label() == "Cut")
            self.assertTrue(cut.get_sensitive())
            with patch.object(p.projection_view, "get_clipboard") as clipboard:
                cut.activate()
                clipboard.return_value.set_text.assert_called_once_with(original, -1)
            self.assertEqual(p.text(), "")
            self.editor.undo()
            self.assertEqual(p.text(), original)
        finally:
            menu.destroy()

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


    def test_minified_editor_selects_code_before_its_first_presentation(self) -> None:
        """Opening a minified file does not first select a diff or its loading screen."""

        self.editor.relative_path = "bundle.min.js"
        presentation = ExperimentalEditor(self.editor)
        try:
            self.assertEqual(presentation.mode, "code")
            self.assertTrue(presentation.initial_chosen)
            for mode in ("compact", "unified", "side"):
                self.assertIsNotNone(presentation._projection_error(mode))
        finally:
            presentation.shutdown()
            presentation.destroy()

    def test_minified_files_skip_repository_reads_and_all_diff_workers(self) -> None:
        """Explicitly minified filenames stay editable while all diff modes are unavailable."""

        for name in ("bundle.min.js", "style.MIN.css", "library.min.js.map"):
            with self.subTest(name=name), patch("slate.experimental_editor.run_async") as run:
                self.editor.relative_path = name
                self.presentation.base_identity = None
                scm = SimpleNamespace(root=self.temporary.name, kind="git", environment={})
                self.presentation.configure_base(scm, name, "modified")
                self.assertEqual(self.presentation.mode, "code")
                self.assertEqual(self.presentation.stack.get_visible_child_name(), "source")
                self.assertTrue(self.presentation.notice.get_visible())
                self.assertIn("minified", self.presentation.notice.get_text())
                self.assertTrue(self.editor.view.get_editable())
                for mode in ("compact", "unified", "side"):
                    self.assertFalse(self.presentation.mode_buttons[mode].get_sensitive())
                    self.presentation.set_mode(mode)
                    self.assertEqual(self.presentation.mode, "code")
                with patch.object(self.editor, "get_mapped", return_value=True):
                    self.editor.buffer.insert_at_cursor("edited")
                    self.presentation.schedule()
                    self.presentation._refresh()
                run.assert_not_called()

    def test_minified_rename_cancels_inflight_diff_and_ignores_late_result(self) -> None:
        """Renaming an open diff to .min. stops expensive work and preserves normal files."""

        self.presentation.rows_serial = -1
        with patch.object(self.editor, "get_mapped", return_value=True), patch(
                "slate.experimental_editor.run_async") as run:
            self.presentation._refresh()
            callback = run.call_args.args[1]
            command = self.presentation.command
            self.editor.relative_path = "style.min.css"
            self.presentation.configure_base(SimpleNamespace(root=self.temporary.name, kind="hg", environment={}),
                                             "style.min.css", "modified")
            command.cancel.assert_called_once_with()
            callback(CommandResult((), 0, "[]", ""))
            self.assertEqual(self.presentation.mode, "code")
            self.assertIsNone(self.presentation.command)
            self.assertEqual(run.call_count, 1)
            self.editor.relative_path = "directory.min.assets/style.css"
            self.presentation.configure_base(SimpleNamespace(root=self.temporary.name, kind="hg", environment={}),
                                             self.editor.relative_path, "modified")
            self.assertEqual(run.call_count, 2)
            self.assertEqual(run.call_args.args[0][:2], ["hg", "cat"])

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

    def test_failed_repository_read_can_be_retried_on_reopen(self) -> None:
        """A failed or timed-out read never caches an unavailable baseline."""

        scm = SimpleNamespace(root=self.temporary.name, kind="hg", environment={})
        for timeout in (False, True):
            with self.subTest(timeout=timeout), patch("slate.experimental_editor.run_async") as run:
                self.presentation.base_identity = None
                self.presentation.configure_base(scm, "style.css", "modified")
                callback = run.call_args.args[1]
                if timeout:
                    GLib.source_remove(self.presentation.base_timeout)
                    self.presentation._base_timed_out()
                else:
                    callback(CommandResult((), 1, "", "temporary error"))
                self.presentation.configure_base(scm, "style.css", "modified")
                self.assertEqual(run.call_count, 2)
                if timeout:
                    callback(CommandResult((), 0, "stale", ""))
                    self.assertIsNone(self.presentation.base)
                run.call_args.args[1](CommandResult((), 0, "recovered", ""))
                self.assertEqual(self.presentation.base, "recovered")

    def test_diff_timeout_blocks_identical_work_until_either_version_changes(self) -> None:
        """Remapping a timed-out diff does not restart its worker for unchanged texts."""

        self.presentation.command = MagicMock()
        command = self.presentation.command
        self.presentation._diff_timed_out()
        command.cancel.assert_called_once_with()
        for mode in ("compact", "unified", "side"):
            self.assertIsNotNone(self.presentation._projection_error(mode))
            self.assertFalse(self.presentation.mode_buttons[mode].get_sensitive())
        with patch.object(self.editor, "get_mapped", return_value=True), patch(
                "slate.experimental_editor.GLib.timeout_add") as timer, patch(
                "slate.experimental_editor.GLib.idle_add") as idle:
            self.presentation.schedule()
            timer.assert_not_called()
            idle.assert_not_called()
        self.presentation.serial += 1
        self.assertIsNone(self.presentation._projection_error("unified"))
        self.presentation.serial -= 1
        self.presentation.base_serial += 1
        self.assertIsNone(self.presentation._projection_error("unified"))

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


    def test_definition_shortcut_uses_the_focused_side_without_fallback(self) -> None:
        """Ctrl+B follows the left or right symbol and never falls back from an empty old side."""

        self.presentation.set_mode("side")
        self.editor.buffer.set_text("right_symbol = 1\n")
        self.editor.buffer.place_cursor(self.editor.buffer.get_iter_at_offset(3))
        self.editor.on_definition = MagicMock()
        owner = SimpleNamespace(current_editor=MagicMock(return_value=self.editor))
        event = SimpleNamespace(keyval=Gdk.KEY_b, state=Gdk.ModifierType.CONTROL_MASK)
        for view, old_text, expected in ((self.presentation.old_view, "left_symbol = 1\n", "left_symbol"),
                                          (self.editor.view, "left_symbol = 1\n", "right_symbol"),
                                          (self.presentation.old_view, "   ", None)):
            with self.subTest(expected=expected):
                self.presentation.old_buffer.set_text(old_text)
                self.presentation.old_buffer.place_cursor(self.presentation.old_buffer.get_iter_at_offset(2))
                self.presentation._side_focus(view, None)
                self.editor.on_definition.reset_mock()
                self.assertTrue(EditorWorkspace.handle_key(owner, event))
                if expected is None:
                    self.editor.on_definition.assert_not_called()
                else:
                    self.assertEqual(self.editor.on_definition.call_args.args[2], expected)

    def test_unified_definition_uses_the_symbol_in_the_removed_row(self) -> None:
        """A removed line's identifier cannot be replaced with one from the hidden live cursor."""

        self.editor.buffer.set_text("current_symbol = 1\n")
        self.presentation.base = "removed_symbol = 1\n"
        self.presentation.old_buffer.set_text(self.presentation.base)
        self.presentation.rows = project_diff(self.presentation.base, self.presentation.text())
        self.presentation._replace_spans()
        self.presentation._render_projection()
        visual = self.presentation.projection.get_text(self.presentation.projection.get_start_iter(),
                                                       self.presentation.projection.get_end_iter(), True)
        self.presentation.projection.place_cursor(
            self.presentation.projection.get_iter_at_offset(visual.index("removed_symbol") + 3))
        self.editor.buffer.place_cursor(self.editor.buffer.get_iter_at_offset(3))
        self.editor.on_definition = MagicMock()
        event = SimpleNamespace(keyval=Gdk.KEY_b, state=Gdk.ModifierType.CONTROL_MASK)
        self.assertTrue(self.presentation.handle_key(event))
        self.assertEqual(self.editor.on_definition.call_args.args[2], "removed_symbol")
        self.assertEqual(self.editor.buffer.get_iter_at_mark(self.editor.buffer.get_insert()).get_offset(), 3)

    def test_f3_uses_visible_diff_search_after_returning_to_text(self) -> None:
        """F3 and Shift+F3 navigate the visible diff buffer without a control modifier."""

        self.editor.buffer.set_text("needle needle\n")
        self.presentation.base = "needle needle old\n"
        self.presentation.old_buffer.set_text(self.presentation.base)
        self.presentation.rows = project_diff(self.presentation.base, self.presentation.text())
        self.presentation.rows_serial = self.presentation.serial
        self.presentation.rows_base_serial = self.presentation.base_serial
        owner = SimpleNamespace(current_editor=MagicMock(return_value=self.editor))
        for mode, side in (("unified", "current"), ("compact", "current"),
                           ("side", "old"), ("side", "current")):
            with self.subTest(mode=mode, side=side):
                self.presentation.set_mode(mode)
                if mode in {"unified", "compact"}:
                    self.presentation._replace_spans()
                    self.presentation._render_projection()
                    search = self.presentation.projection_search
                    search.settings.set_search_text("needle")
                    buffer = search.buffer
                    buffer.place_cursor(buffer.get_start_iter())
                    search.search()
                elif side == "old":
                    self.presentation._side_focus(self.presentation.old_view, None)
                    search = self.presentation.old_search
                    search.settings.set_search_text("needle")
                    buffer = search.buffer
                    buffer.place_cursor(buffer.get_start_iter())
                    search.search()
                else:
                    self.presentation._side_focus(self.editor.view, None)
                    self.editor.search_settings.set_search_text("needle")
                    buffer = self.editor.buffer
                    buffer.place_cursor(buffer.get_start_iter())
                    self.editor.search()
                for state, expected in ((Gdk.ModifierType(0), 7), (Gdk.ModifierType.SHIFT_MASK, 0)):
                    event = SimpleNamespace(keyval=Gdk.KEY_F3, state=state)
                    self.assertTrue(EditorWorkspace.handle_key(owner, event))
                    self.assertEqual(buffer.get_selection_bounds()[0].get_offset(), expected)
                self.assertEqual(self.presentation.mode, mode)

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
