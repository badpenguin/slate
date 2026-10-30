"""Verify source and history stay separate in editable diff projections."""

import unittest

from slate.editor_diff_model import editable_ranges, project_diff


class EditorDiffModelTest(unittest.TestCase):
    """Exercise replacements, Unicode, line endings and protected historical rows."""

    def test_css_edit_preserves_only_current_source(self) -> None:
        """Changing 12px to 14px must not save the historical CSS declaration."""

        before = "font-size: 10px;\n"
        current = "font-size: 12px;\n"
        rows = project_diff(before, current)
        offset = len(before) + current.index("12")
        ranges = editable_ranges(rows, current, offset, offset + 2)
        self.assertEqual(ranges, [(11, 13)])
        start, end = ranges[0]
        self.assertEqual(current[:start] + "14" + current[end:], "font-size: 14px;\n")

    def test_selection_across_removed_rows_maps_only_current_text(self) -> None:
        """Historical rows are excluded even when a selection crosses them."""

        current = "a\nnew\nz\n"
        rows = project_diff("a\nold\nz\n", current)
        self.assertEqual(editable_ranges(rows, current, 0, 100), [(0, len(current))])
        self.assertEqual(editable_ranges(rows, current, 2, 6), [])

    def test_unicode_crlf_and_missing_final_newline_survive(self) -> None:
        """Projection never normalizes the source's line endings or characters."""

        current = "è\r\nlast"
        rows = project_diff("old\r\nlast", current)
        self.assertEqual("".join(row.text for row in rows if row.new_line is not None), current)

    def test_new_and_deleted_files(self) -> None:
        """An empty baseline supports new files and an empty source keeps history readonly."""

        self.assertEqual(project_diff("", "new")[0].kind, "added")
        rows = project_diff("old", "")
        self.assertEqual(rows[0].kind, "removed")
        self.assertEqual(editable_ranges(rows, "", 0, 3), [])
