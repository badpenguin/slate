"""Exercise descriptor-anchored flat deletion using temporary files only."""

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from slate.flat_delete_worker import _delete_flat_directory


class FlatDeleteWorkerTest(unittest.TestCase):
    """Verify directory swaps cannot redirect a confirmed flat deletion."""

    def test_directory_swap_immediately_before_unlink_keeps_external_file(self):
        """Even a swap after the identity check cannot redirect unlink through a link."""

        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            root = Path(directory)
            target = root / "project"
            outside = root / "outside"
            target.mkdir()
            outside.mkdir()
            (target / "victim.txt").write_text("original")
            (outside / "victim.txt").write_text("external")
            original = os.unlink

            def swapped(name, *, dir_fd=None):
                """Replace the pathname only after the worker has pinned its directory."""

                target.rename(root / "moved")
                target.symlink_to(outside, target_is_directory=True)
                original(name, dir_fd=dir_fd)

            with patch("slate.flat_delete_worker.os.unlink", swapped):
                with self.assertRaisesRegex(ValueError, "changed"):
                    _delete_flat_directory(str(target))
            self.assertEqual((outside / "victim.txt").read_text(), "external")
            self.assertTrue(target.is_symlink())

    def test_symlink_in_directory_chain_is_rejected(self):
        """Both a final directory link and a linked ancestor fail before deleting contents."""

        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            root = Path(directory)
            outside = root / "outside"
            outside.mkdir()
            (outside / "file.txt").write_text("keep")
            (outside / "child").mkdir()
            (outside / "child" / "file.txt").write_text("keep child")
            link = root / "link"
            link.symlink_to(outside, target_is_directory=True)
            for target in (link, link / "child"):
                with self.subTest(path=target), self.assertRaises(OSError):
                    _delete_flat_directory(str(target))
            self.assertEqual((outside / "file.txt").read_text(), "keep")
            self.assertEqual((outside / "child" / "file.txt").read_text(), "keep child")

    def test_subdirectory_rejects_all_deletion_and_child_links_are_unlinked(self):
        """Real subdirectories preserve siblings; child links never expose their target."""

        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            root = Path(directory)
            target = root / "target"
            target.mkdir()
            (target / "keep.txt").write_text("keep")
            (target / "child").mkdir()
            with self.assertRaisesRegex(ValueError, "other directories"):
                _delete_flat_directory(str(target))
            self.assertTrue((target / "keep.txt").exists())
            (target / "child").rmdir()
            outside = root / "outside.txt"
            outside.write_text("external")
            (target / "link").symlink_to(outside)
            _delete_flat_directory(str(target))
            self.assertFalse(target.exists())
            self.assertEqual(outside.read_text(), "external")
