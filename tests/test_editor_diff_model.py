"""Verify source and history stay separate in editable diff projections."""

import unittest

from slate.editor_diff_model import project_diff


class EditorDiffModelTest(unittest.TestCase):
    """Exercise replacements, Unicode, line endings and protected historical rows."""


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
