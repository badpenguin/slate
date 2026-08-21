"""Tests for bounded asynchronous project-content search."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import gi

gi.require_version("Gdk", "3.0")
gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, GLib, Gtk

from slate.search import (
    ProjectSearch,
    SearchCommand,
    SearchCompletion,
    SearchResult,
    build_search_argv,
)


class SearchCommandTest(unittest.TestCase):
    """Verify ripgrep arguments, JSON parsing and global result bounds."""

    def test_argv_uses_literal_smart_case_and_explicit_exclusions(self) -> None:
        """The query remains one argv value and ignored heavy trees stay excluded."""

        argv = build_search_argv("value $(unsafe)", {"vendor", "node_modules"})
        self.assertEqual(argv[:5], ["rg", "--json", "--fixed-strings", "--smart-case", "--max-filesize"])
        self.assertIn(("--glob", "!**/node_modules/**"), tuple(zip(argv, argv[1:])))
        self.assertIn(("--glob", "!**/vendor/**"), tuple(zip(argv, argv[1:])))
        self.assertEqual(argv[-3:], ["--", "value $(unsafe)", "."])

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
        self.edited_internal: list[str] = []
        self.edited_external: list[str] = []
        self.melded: list[str] = []
        self.close_count = 0
        self.search = ProjectSearch(
            self._ignore_close,
            self.viewed.append,
            self.edited_internal.append,
            self.edited_external.append,
            self._can_meld,
            self.melded.append,
        )

    def _ignore_close(self) -> None:
        """Record search dismissal without changing widget visibility."""

        self.close_count += 1

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
            self.assertTrue(press(Gdk.KEY_m))
            self.assertTrue(press(Gdk.KEY_d))
        self.assertEqual(self.viewed, ["src/app.py"])
        self.assertEqual(self.edited_internal, ["src/app.py"])
        self.assertEqual(self.edited_external, ["src/app.py"])
        self.assertEqual(self.melded, ["src/app.py"])
        self.assertEqual(self.close_count, 1)

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
