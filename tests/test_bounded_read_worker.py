"""Verify bounded project reads never follow a raced-in external symlink."""

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from slate.bounded_read_worker import _read_project_file


class BoundedReadWorkerTest(unittest.TestCase):
    """Exercise real no-follow descriptor reads against temporary filesystem races."""

    def test_parent_swap_before_final_open_keeps_original_directory(self):
        """A replaced parent pathname cannot redirect the already pinned descriptor."""

        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            base = Path(directory)
            root = base / "project"
            folder = root / "sub"
            folder.mkdir(parents=True)
            outside = base / "outside"
            outside.mkdir()
            path = folder / "file.txt"
            path.write_bytes(b"inside")
            (outside / "file.txt").write_bytes(b"external secret")
            original = os.open

            def swapped(name, flags, **kwargs):
                """Move the verified parent just before opening its child."""

                if name == "file.txt":
                    folder.rename(root / "original-sub")
                    folder.symlink_to(outside, target_is_directory=True)
                return original(name, flags, **kwargs)

            with patch("slate.bounded_read_worker.os.open", swapped):
                self.assertEqual(_read_project_file(str(root), str(path), 100), b"inside")

    def test_final_and_parent_symlinks_are_rejected(self):
        """A link present before opening either component is never followed."""

        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            root = Path(directory)
            (root / "original").mkdir()
            (root / "original" / "file.txt").write_bytes(b"keep")
            (root / "link").symlink_to("original", target_is_directory=True)
            (root / "file-link").symlink_to("original/file.txt")
            for path in (root / "link" / "file.txt", root / "file-link"):
                with self.subTest(path=path), self.assertRaises(OSError):
                    _read_project_file(str(root), str(path), 100)

    def test_regular_file_bounds_fifo_and_growth(self):
        """Exact limits work while FIFO streams and growth beyond the byte budget fail."""

        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            root = Path(directory)
            path = root / "file.txt"
            for data in (b"", b"abcdefgh"):
                path.write_bytes(data)
                self.assertEqual(_read_project_file(str(root), str(path), 8), data)
            fifo = root / "pipe"
            os.mkfifo(fifo)
            with self.assertRaisesRegex(ValueError, "regular"):
                _read_project_file(str(root), str(fifo), 8)
            original = os.read

            def grew(fd, count):
                """Grow the file after fstat but before the first bounded read."""

                path.write_bytes(b"x" * 1000)
                return original(fd, count)

            with patch("slate.bounded_read_worker.os.read", grew), self.assertRaisesRegex(ValueError, "5 MiB"):
                _read_project_file(str(root), str(path), 8)
