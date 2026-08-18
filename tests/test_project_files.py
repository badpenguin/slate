"""Integration tests for asynchronous project filesystem operations."""

import tempfile
import time
import unittest
from pathlib import Path

from gi.repository import GLib

from slate.project_files import DirectoryInspection, ProjectFileOperations


class ProjectFileOperationsTest(unittest.TestCase):
    """Verify asynchronous project operations against a real filesystem."""

    def setUp(self) -> None:
        """Create an isolated root and reset asynchronous callback results."""

        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.operations = ProjectFileOperations()
        self.operation_result: str | None | object = self
        self.inspection_result: DirectoryInspection | None = None
        self.inspection_error: str | None | object = self

    def tearDown(self) -> None:
        """Release the isolated filesystem after pending callbacks complete."""

        self.temporary.cleanup()

    def _record_operation(self, error: str | None) -> None:
        """Record completion of a create or delete operation."""

        self.operation_result = error

    def _record_inspection(
        self, inspection: DirectoryInspection | None, error: str | None
    ) -> None:
        """Record one asynchronous direct-child inspection."""

        self.inspection_result = inspection
        self.inspection_error = error

    def _wait_for_operation(self) -> None:
        """Drive GLib until an operation callback publishes its result."""

        deadline = time.monotonic() + 2
        context = GLib.MainContext.default()
        while self.operation_result is self and time.monotonic() < deadline:
            context.iteration(True)
        self.assertIsNot(self.operation_result, self)

    def _wait_for_inspection(self) -> None:
        """Drive GLib until a directory-inspection callback completes."""

        deadline = time.monotonic() + 2
        context = GLib.MainContext.default()
        while self.inspection_error is self and time.monotonic() < deadline:
            context.iteration(True)
        self.assertIsNot(self.inspection_error, self)

    def test_create_file_and_directory_never_replace_existing_entries(self) -> None:
        """Creation produces empty entries and reports collisions without overwrite."""

        file_path = self.root / "new.txt"
        self.operations.create_file(str(file_path), self._record_operation)
        self._wait_for_operation()
        self.assertIsNone(self.operation_result)
        self.assertEqual(file_path.read_bytes(), b"")

        self.operation_result = self
        directory_path = self.root / "folder"
        self.operations.create_directory(
            str(directory_path), self._record_operation
        )
        self._wait_for_operation()
        self.assertIsNone(self.operation_result)
        self.assertTrue(directory_path.is_dir())

        self.operation_result = self
        self.operations.create_file(str(file_path), self._record_operation)
        self._wait_for_operation()
        self.assertIsInstance(self.operation_result, str)

    def test_rename_moves_entries_without_replacing_a_collision(self) -> None:
        """Rename files and directories while preserving an existing destination."""

        source = self.root / "old.txt"
        source.write_text("content", encoding="utf-8")
        destination = self.root / "new.txt"
        self.operations.rename_entry(
            str(source), str(destination), self._record_operation
        )
        self._wait_for_operation()
        self.assertIsNone(self.operation_result)
        self.assertFalse(source.exists())
        self.assertEqual(destination.read_text(encoding="utf-8"), "content")

        source_directory = self.root / "old-dir"
        source_directory.mkdir()
        (source_directory / "child.txt").write_text("child", encoding="utf-8")
        destination_directory = self.root / "new-dir"
        self.operation_result = self
        self.operations.rename_entry(
            str(source_directory),
            str(destination_directory),
            self._record_operation,
        )
        self._wait_for_operation()
        self.assertIsNone(self.operation_result)
        self.assertFalse(source_directory.exists())
        self.assertEqual(
            (destination_directory / "child.txt").read_text(encoding="utf-8"),
            "child",
        )

        collision = self.root / "collision.txt"
        collision.write_text("keep", encoding="utf-8")
        self.operation_result = self
        self.operations.rename_entry(
            str(destination), str(collision), self._record_operation
        )
        self._wait_for_operation()
        self.assertIsInstance(self.operation_result, str)
        self.assertTrue(destination.exists())
        self.assertEqual(collision.read_text(encoding="utf-8"), "keep")

    def test_flat_directory_deletes_files_and_links_without_following_links(self) -> None:
        """Flat deletion removes direct entries but preserves symlink destinations."""

        outside = self.root / "outside"
        outside.mkdir()
        (outside / "keep.txt").write_text("keep", encoding="utf-8")
        target = self.root / "flat"
        target.mkdir()
        (target / "file.txt").write_text("delete", encoding="utf-8")
        (target / "link").symlink_to(outside, target_is_directory=True)

        self.operations.delete_flat_directory(
            str(target), self._record_operation
        )
        self._wait_for_operation()
        self.assertIsNone(self.operation_result)
        self.assertFalse(target.exists())
        self.assertEqual((outside / "keep.txt").read_text(encoding="utf-8"), "keep")

    def test_real_subdirectory_rejects_deletion_before_any_child_is_removed(self) -> None:
        """A real child directory blocks deletion and preserves sibling files."""

        target = self.root / "nested"
        target.mkdir()
        (target / "file.txt").write_text("keep", encoding="utf-8")
        (target / "child").mkdir()
        self.operations.inspect_directory(str(target), self._record_inspection)
        self._wait_for_inspection()
        self.assertIsNone(self.inspection_error)
        self.assertTrue(self.inspection_result.contains_directory)

        self.operations.delete_flat_directory(
            str(target), self._record_operation
        )
        self._wait_for_operation()
        self.assertIsInstance(self.operation_result, str)
        self.assertTrue((target / "file.txt").exists())
        self.assertTrue((target / "child").is_dir())


if __name__ == "__main__":
    unittest.main()
