"""Tests for bounded asynchronous project-content search."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import gi

gi.require_version("Gdk", "3.0")
gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, GLib, Gtk

from slate.search import (
    DefinitionSearch,
    ProjectSearch,
    SearchCommand,
    SearchCompletion,
    SearchResult,
    build_definition_argv,
    build_search_argv,
    source_symbol_at_cursor,
)


class SearchCommandTest(unittest.TestCase):
    """Verify ripgrep arguments, JSON parsing and global result bounds."""

    def test_selected_php_and_css_symbols_match_the_cursor_token(self) -> None:
        """Selecting PHP sigils or CSS prefixes preserves the definition-search identity."""

        buffer = Gtk.TextBuffer()
        for text, expected in (("$config", "config"), ("--theme-color", "theme-color"), ("deploy", "deploy")):
            with self.subTest(text=text):
                buffer.set_text(text)
                buffer.select_range(buffer.get_start_iter(), buffer.get_end_iter())
                self.assertEqual(source_symbol_at_cursor(buffer), expected)
                buffer.place_cursor(buffer.get_iter_at_offset(3))
                self.assertEqual(source_symbol_at_cursor(buffer), expected)
        for text in ("$", "$config + other", "--"):
            buffer.set_text(text)
            buffer.select_range(buffer.get_start_iter(), buffer.get_end_iter())
            self.assertIsNone(source_symbol_at_cursor(buffer))

    def test_argv_uses_literal_smart_case_and_explicit_exclusions(self) -> None:
        """The query remains one argv value and ignored heavy trees stay excluded."""

        argv = build_search_argv("value $(unsafe)", {"vendor", "node_modules"})
        self.assertEqual(argv[:5], ["rg", "--json", "--fixed-strings", "--smart-case", "--max-filesize"])
        self.assertIn(("--glob", "!**/node_modules/**"), tuple(zip(argv, argv[1:])))
        self.assertIn(("--glob", "!**/vendor/**"), tuple(zip(argv, argv[1:])))
        self.assertEqual(argv[-3:], ["--", "value $(unsafe)", "."])

    def test_definition_argv_searches_declarations_without_shell_interpolation(self) -> None:
        """Definition lookup passes one escaped PCRE pattern directly to ripgrep."""

        argv = build_definition_argv("render$value", {"vendor"})
        pattern = argv[argv.index("--regexp") + 1]
        self.assertIn(r"render\$value", pattern)
        self.assertIn(("--glob", "!**/vendor/**"), tuple(zip(argv, argv[1:])))
        self.assertEqual(argv[-2:], ["--", "."])

    def test_symbol_at_cursor_accepts_selection_and_word_boundaries(self) -> None:
        """A source buffer yields either its selected or cursor-adjacent identifier."""

        buffer = Gtk.TextBuffer()
        buffer.set_text("call renderWidget now")
        cursor = buffer.get_iter_at_offset(11)
        buffer.place_cursor(cursor)
        self.assertEqual(source_symbol_at_cursor(buffer), "renderWidget")
        start = buffer.get_iter_at_offset(5)
        end = buffer.get_iter_at_offset(17)
        buffer.select_range(start, end)
        self.assertEqual(source_symbol_at_cursor(buffer), "renderWidget")

    def test_json_match_normalizes_path_and_unicode_byte_offsets(self) -> None:
        """UTF-8 byte spans become GtkTextBuffer-compatible character spans."""

        record = {
            "type": "match",
            "data": {
                "path": {"text": "./src/example.py"},
                "lines": {"text": "città utile\n"},
                "line_number": 7,
                "submatches": [{"start": 6, "end": 11}],
            },
        }
        result = SearchCommand._parse_match(json.dumps(record))
        self.assertEqual(
            result,
            SearchResult("src/example.py", 7, "città utile", ((5, 10),)),
        )

    def test_dense_line_has_bounded_spans_with_correct_unicode_offsets(self) -> None:
        """Thousands of Unicode matches retain a bounded number of exact source coordinates."""

        text = "éneedle " * 10000
        byte_width = len("éneedle ".encode("utf-8"))
        record = {"type": "match", "data": {
            "path": {"text": "dense.txt"}, "lines": {"text": text}, "line_number": 1,
            "submatches": [{"start": index * byte_width + 2, "end": index * byte_width + 8}
                           for index in range(10000)],
        }}
        result = SearchCommand._parse_match(json.dumps(record))
        self.assertEqual(len(result.spans), SearchCommand.MATCH_SPAN_LIMIT)
        self.assertLessEqual(len(result.text), 502)
        self.assertFalse(result.complete_line)
        for index, (left, right) in enumerate(result.spans):
            self.assertEqual((left, right), (index * 8 + 1, index * 8 + 7))
            self.assertEqual(text[left:right], "needle")

    def test_unicode_conversion_handles_overlapping_and_unordered_spans(self) -> None:
        """Incremental decoding preserves source offsets independently of span ordering."""

        record = {"type": "match", "data": {
            "path": {"text": "text.txt"}, "lines": {"text": "éabcé\n"}, "line_number": 1,
            "submatches": [{"start": 3, "end": 7}, {"start": 0, "end": 4}, {"start": 2, "end": 5}],
        }}
        result = SearchCommand._parse_match(json.dumps(record))
        self.assertEqual(result.spans, ((2, 5), (0, 3), (1, 4)))

    def test_search_preserves_literal_backslashes_in_linux_filenames(self) -> None:
        """A match for a backslash filename cannot resolve to a different nested file."""

        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            root = Path(directory)
            (root / "a\\b.txt").write_text("literal match")
            (root / "a").mkdir()
            (root / "a" / "b.txt").write_text("different file")
            record = {"type": "match", "data": {
                "path": {"text": "./a\\b.txt"}, "lines": {"text": "literal match"}, "line_number": 1,
                "submatches": [{"start": 0, "end": 7}],
            }}
            result = SearchCommand._parse_match(json.dumps(record))
            self.assertEqual(result.path, "a\\b.txt")
            self.assertEqual((root / result.path).read_text(), "literal match")

    def test_json_match_rejects_traversal(self) -> None:
        """A tool result can never introduce a path outside the project."""

        record = {
            "type": "match",
            "data": {
                "path": {"text": "../outside.txt"},
                "lines": {"text": "needle\n"},
                "line_number": 1,
                "submatches": [{"start": 0, "end": 6}],
            },
        }
        self.assertIsNone(SearchCommand._parse_match(json.dumps(record)))

    def test_real_search_stops_at_global_limit(self) -> None:
        """Streaming force-exits ripgrep after the configured number of rows."""

        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "many.txt").write_text(
                "".join(f"needle {index}\n" for index in range(140)),
                encoding="utf-8",
            )
            loop = GLib.MainLoop()
            results: list[SearchResult] = []
            completions: list[SearchCompletion] = []

            def completed(completion: SearchCompletion) -> None:
                """Retain completion and release the isolated GLib loop."""

                completions.append(completion)
                loop.quit()

            command = SearchCommand(
                directory,
                "needle",
                set(),
                100,
                results.append,
                completed,
            )
            self.assertIsNotNone(command)
            loop.run()
        self.assertEqual(len(results), 100)
        self.assertEqual(len(completions), 1)
        self.assertTrue(completions[0].truncated)
        self.assertFalse(completions[0].error)

    def test_definition_search_returns_declarations_not_plain_calls(self) -> None:
        """The heuristic resolver excludes ordinary symbol references."""

        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "app.php").write_text(
                "function deploy() {}\ndeploy();\n",
                encoding="utf-8",
            )
            loop = GLib.MainLoop()
            observed: list[tuple[SearchResult, ...]] = []

            def completed(
                _symbol: str,
                results: tuple[SearchResult, ...],
                _completion: SearchCompletion,
            ) -> None:
                """Retain definition candidates and stop the isolated loop."""

                observed.append(results)
                loop.quit()

            DefinitionSearch(directory, "deploy", completed)
            loop.run()

        self.assertEqual(len(observed[0]), 1)
        self.assertEqual(observed[0][0].line, 1)


class ProjectSearchTest(unittest.TestCase):
    """Verify query threshold, selection and contextual keyboard actions."""

    @classmethod
    def setUpClass(cls) -> None:
        """Require the display server shared by the GTK test session."""

        initialized, _arguments = Gtk.init_check(None)
        if not initialized:
            raise unittest.SkipTest("display GTK non disponibile")

    def setUp(self) -> None:
        """Create an inert search surface with observable action callbacks."""

        self.viewed: list[str] = []
        self.edited_internal: list[tuple[str, int | None]] = []
        self.melded: list[str] = []
        self.close_count = 0
        self.search = ProjectSearch(
            self._ignore_close,
            self.viewed.append,
            self._edit_internal,

            self._can_meld,
            self.melded.append,
        )

    def _ignore_close(self) -> None:
        """Record search dismissal without changing widget visibility."""

        self.close_count += 1

    def _edit_internal(self, path: str, line: int | None) -> None:
        """Record the file and exact line requested from a search result."""

        self.edited_internal.append((path, line))

    @staticmethod
    def _can_meld(path: str) -> bool:
        """Expose Meld only for the representative modified path."""

        return path == "src/app.py"

    def test_search_starts_only_after_four_characters(self) -> None:
        """Short queries clear state while four characters queue one debounce."""

        self.search.root = "/tmp/project"
        with patch("slate.search.GLib.timeout_add", return_value=73) as timeout:
            self.search.entry.set_text("abc")
            self.search._on_query_changed(self.search.entry)
            timeout.assert_not_called()
            self.search.entry.set_text("abcd")
            self.search._on_query_changed(self.search.entry)
        timeout.assert_called_once_with(
            self.search.DEBOUNCE_MS,
            self.search._start_search,
            self.search.request_serial,
            "abcd",
        )
        self.search.debounce_id = None

    def test_definition_fallback_searches_short_text_immediately(self) -> None:
        """An explicit fallback bypasses debounce and the free-typing threshold."""

        with patch.object(self.search, "_start_search") as start:
            self.search.search_for("project", "/tmp/project", "go")

        self.assertEqual(self.search.project_name, "project")
        self.assertEqual(self.search.root, "/tmp/project")
        self.assertEqual(self.search.entry.get_text(), "go")
        self.assertEqual(self.search.status_label.get_text(), "Searching…")
        start.assert_called_once_with(self.search.request_serial, "go")

    def test_primary_selection_seeds_one_current_blank_search(self) -> None:
        """A concise external PRIMARY selection becomes the live project query."""

        self.search.root = "/tmp/project"
        self.search.selection_request_serial = 4
        self.search._on_primary_selection_received(None, "  selected text  ", 4)
        self.assertEqual(self.search.entry.get_text(), "selected text")

    def test_primary_selection_rejects_multiline_long_and_stale_text(self) -> None:
        """Non-search selections and late callbacks never replace query input."""

        self.search.root = "/tmp/project"
        self.search.selection_request_serial = 8
        self.search._on_primary_selection_received(None, "first\nsecond", 8)
        self.search._on_primary_selection_received(None, "x" * 61, 8)
        self.search._on_primary_selection_received(None, "stale", 7)
        self.assertEqual(self.search.entry.get_text(), "")

        self.search.entry.set_text("typed manually")
        self.search._on_primary_selection_received(None, "external", 8)
        self.assertEqual(self.search.entry.get_text(), "typed manually")

    def test_result_shortcuts_dispatch_view_editor_and_meld(self) -> None:
        """V, E, M and D act on the focused row with the shared editor mapping."""

        result = SearchResult("src/app.py", 4, "needle", ((0, 6),))
        tree_iter = self.search.store.append(
            (result.text, f"{result.path}:{result.line}", result)
        )
        self.search.tree.get_selection().select_iter(tree_iter)

        def press(keyval: int) -> bool:
            """Dispatch one unmodified key to the result tree."""

            event = SimpleNamespace(keyval=keyval, state=Gdk.ModifierType(0))
            return self.search._on_tree_key_press(self.search.tree, event)

        with patch.object(self.search, "_load_preview"):
            self.assertTrue(press(Gdk.KEY_v))
            self.assertTrue(press(Gdk.KEY_e))
            self.assertFalse(press(Gdk.KEY_m))
            self.assertTrue(press(Gdk.KEY_d))
        self.assertEqual(self.viewed, ["src/app.py"])
        self.assertEqual(self.edited_internal, [("src/app.py", 4)])
        self.assertEqual(self.melded, ["src/app.py"])
        self.assertEqual(self.close_count, 1)

    def test_result_replaced_with_circular_symlink_reports_preview_error(self) -> None:
        """A filesystem change after searching cannot raise from the selection callback."""

        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            path = Path(directory) / "found.txt"
            path.write_text("needle")
            result = SearchResult("found.txt", 1, "needle", ((0, 6),))
            path.unlink()
            path.symlink_to("found.txt")
            self.search.root = directory
            with patch.object(self.search, "_set_preview_message") as message, patch(
                    "slate.search.BoundedFileRead") as read:
                self.search._load_preview(result)
                message.assert_called_once()
                read.assert_not_called()

    def test_stale_bounded_preview_cannot_replace_new_selection(self) -> None:
        """A late read or error leaves the newer preview and cancellation token untouched."""

        old = SearchResult("old.txt", 1, "old", ())
        current = SearchResult("new.txt", 1, "new", ())
        self.search.request_serial = 20
        self.search.preview_result = current
        token = object()
        self.search.preview_cancellable = token
        with patch.object(self.search, "_render_preview") as render, patch.object(
                self.search, "_set_preview_text") as message:
            self.search._on_preview_loaded((19, old), b"stale", None)
            self.search._on_preview_loaded((19, old), None, ValueError("stale error"))
            render.assert_not_called()
            message.assert_not_called()
        self.assertIs(self.search.preview_cancellable, token)
        self.search.preview_cancellable = None

    def test_cleared_query_disables_previous_preview_shortcuts(self) -> None:
        """An empty search preview cannot open the previous result with E, V or D."""

        self.search.root = "/tmp/project"
        self.search.preview_result = SearchResult(
            "src/app.py", 7, "example", ((0, 3),)
        )
        self.search.preview_buffer.set_text("example")
        self.search._on_query_changed(self.search.entry)

        self.assertIsNone(self.search.preview_result)
        self.assertEqual(len(self.search.store), 0)
        self.assertEqual(self.search.preview_buffer.get_char_count(), 0)
        for keyval in (Gdk.KEY_e, Gdk.KEY_v, Gdk.KEY_d):
            event = SimpleNamespace(keyval=keyval, state=Gdk.ModifierType(0))
            self.assertFalse(
                self.search._on_preview_key_press(self.search.preview_view, event)
            )
        self.assertEqual(self.edited_internal, [])
        self.assertEqual(self.viewed, [])
        self.assertEqual(self.melded, [])
        self.assertEqual(self.close_count, 0)

    def test_deselected_preview_ignores_pending_read(self) -> None:
        """A read finishing after deselection cannot restore the cleared preview."""

        result = SearchResult("src/app.py", 7, "example", ((0, 3),))
        self.search.preview_result = result
        request = (self.search.request_serial, result)
        self.search._on_selection_changed(self.search.tree.get_selection())

        with patch.object(self.search, "_render_preview") as render:
            self.search._on_preview_loaded(request, b"example", None)
        render.assert_not_called()
        self.assertIsNone(self.search.preview_result)
        self.assertEqual(self.search.preview_buffer.get_char_count(), 0)

    def test_preview_shortcuts_target_the_rendered_search_result(self) -> None:
        """Preview focus dispatches V, E, M and D without consulting tree focus."""

        result = SearchResult("src/app.py", 4, "needle", ((0, 6),))
        self.search.preview_result = result

        def press(keyval: int) -> bool:
            """Dispatch one unmodified key to the search preview."""

            event = SimpleNamespace(keyval=keyval, state=Gdk.ModifierType(0))
            return self.search._on_preview_key_press(
                self.search.preview_view, event
            )

        self.assertTrue(press(Gdk.KEY_v))
        self.assertTrue(press(Gdk.KEY_e))
        self.assertFalse(press(Gdk.KEY_m))
        self.assertTrue(press(Gdk.KEY_d))
        self.assertEqual(self.viewed, ["src/app.py"])
        self.assertEqual(self.edited_internal, [("src/app.py", 4)])
        self.assertEqual(self.melded, ["src/app.py"])

    def test_ctrl_b_in_search_preview_resolves_symbol_in_rendered_file(self) -> None:
        """The focused search preview forwards its symbol and source identity."""

        requested = MagicMock()
        result = SearchResult("src/app.py", 4, "deploy();", ((0, 6),))
        self.search.on_definition = requested
        self.search.root = "/tmp/project"
        self.search.preview_result = result
        self.search.preview_buffer.set_text("function deploy() {}\n")
        self.search.preview_buffer.place_cursor(
            self.search.preview_buffer.get_iter_at_offset(12)
        )
        event = SimpleNamespace(
            keyval=Gdk.KEY_b,
            state=Gdk.ModifierType.CONTROL_MASK,
        )

        self.assertTrue(
            self.search._on_preview_key_press(self.search.preview_view, event)
        )
        requested.assert_called_once_with(
            "/tmp/project", "src/app.py", "deploy"
        )

    def test_search_preview_menu_does_not_grab_global_shortcuts(self) -> None:
        """The mouse context menu leaves keyboard and XF86 shortcuts available."""

        menu = Gtk.Menu()
        self.search._on_preview_populate_popup(self.search.preview_view, menu)
        self.assertFalse(menu.get_take_focus())

    def test_search_preview_ctrl_f_uses_local_buffer_search(self) -> None:
        """Ctrl+F in a rendered result searches its preview rather than the project."""

        self.search.preview_result = SearchResult(
            "src/app.py", 1, "needle", ((0, 6),)
        )
        self.search.preview_buffer.set_text("needle elsewhere needle\n")
        event = SimpleNamespace(
            keyval=Gdk.KEY_f,
            state=Gdk.ModifierType.CONTROL_MASK,
        )
        self.assertTrue(
            self.search._on_preview_key_press(self.search.preview_view, event)
        )
        self.assertTrue(self.search.preview_search.get_reveal_child())

    def test_result_columns_show_match_left_and_file_location_right(self) -> None:
        """The result hierarchy keeps source text before the compact file location."""

        result = SearchResult("src/app.py", 17, "\t  matched source", ((3, 10),))
        with patch.object(self.search, "_load_preview"):
            self.search._on_search_result(result)
        self.assertEqual(
            [column.get_title() for column in self.search.tree.get_columns()],
            ["Match", "File"],
        )
        tree_iter = self.search.store.get_iter_first()
        self.assertEqual(
            self.search.store.get_value(tree_iter, self.search.COL_TEXT),
            "matched source",
        )
        self.assertEqual(result.text, "\t  matched source")
        self.assertEqual(
            self.search.store.get_value(tree_iter, self.search.COL_LOCATION),
            "src/app.py:17",
        )

    def test_result_columns_start_at_seventy_thirty_and_preserve_ratio(self) -> None:
        """Result allocation starts balanced and scales a manual divider choice."""

        allocation = SimpleNamespace(width=1000)
        self.search._on_results_size_allocate(self.search.tree, allocation)
        self.assertEqual(self.search.match_column.get_fixed_width(), 700)
        self.assertEqual(self.search.file_column.get_fixed_width(), 300)
        self.assertTrue(self.search.match_column.get_resizable())
        self.assertTrue(self.search.file_column.get_resizable())

        self.search.match_column.set_fixed_width(600)
        self.search.file_column.set_fixed_width(400)
        self.search._on_results_size_allocate(
            self.search.tree,
            SimpleNamespace(width=1200),
        )
        self.assertEqual(self.search.match_column.get_fixed_width(), 720)
        self.assertEqual(self.search.file_column.get_fixed_width(), 480)

    def test_clean_result_consumes_meld_shortcut_without_launching(self) -> None:
        """D remains inert for a result without a cached tracked patch."""

        result = SearchResult("clean.txt", 1, "needle", ((0, 6),))
        tree_iter = self.search.store.append(
            (result.text, f"{result.path}:{result.line}", result)
        )
        with patch.object(self.search, "_load_preview"):
            self.search.tree.get_selection().select_iter(tree_iter)
        event = SimpleNamespace(
            keyval=Gdk.KEY_d,
            state=Gdk.ModifierType(0),
        )
        self.assertTrue(self.search._on_tree_key_press(self.search.tree, event))
        self.assertEqual(self.melded, [])


if __name__ == "__main__":
    unittest.main()
