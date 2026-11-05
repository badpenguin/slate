"""Exercise atomic directory-anchored writes using only temporary Linux files."""

from pathlib import Path
import shutil
import stat
import tempfile
import unittest
from unittest.mock import patch

from gi.repository import Gio, GLib

from slate.anchored_save import AnchoredFileSave


class AnchoredSaveTest(unittest.TestCase):
    """Verify race protection, permissions and cancellation with real Gio operations."""

    def _save(self, root, path, data, cancellable=None):
        """Dispatch one isolated save and collect its completion after releasing descriptors."""

        loop = GLib.MainLoop()
        results = []

        def completed(source, result, text):
            """Record either a committed etag or the precise Gio error."""

            try:
                results.append(source.replace_contents_finish(result))
            except GLib.Error as error:
                results.append(error)
            loop.quit()

        def expired():
            """Stop a stalled test without leaving an unbounded wait."""

            loop.quit()
            return GLib.SOURCE_REMOVE

        timeout = GLib.timeout_add(2000, expired)
        try:
            save = AnchoredFileSave(str(root), str(path), Gio.File.new_for_path(str(path)),
                                    data, None, cancellable or Gio.Cancellable(), completed, "snapshot")
            loop.run()
            self.assertEqual(len(results), 1)
            self.assertIsNone(save.parent_fd)
            return results[0]
        finally:
            GLib.source_remove(timeout)

    def test_empty_and_nonempty_saves_preserve_executable_mode(self):
        """The new atomic output has its original Unix permissions when published."""

        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            root = Path(directory)
            path = root / "script.sh"
            path.write_text("old")
            path.chmod(0o751)
            for data in (b"new", b""):
                result = self._save(root, path, data)
                self.assertIsInstance(result, tuple)
                self.assertEqual(path.read_bytes(), data)
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o751)

    def test_parent_switch_after_validation_never_redirects_the_write(self):
        """A replaced pathname cannot redirect a save away from its pinned parent object."""

        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            base = Path(directory)
            root = base / "project"
            parent = root / "sub"
            parent.mkdir(parents=True)
            outside = base / "outside"
            outside.mkdir()
            path = parent / "file.txt"
            path.write_text("original")
            shutil.copy2(path, outside / "file.txt")
            original = Gio.File.replace_async

            def switched(file, *args):
                """Replace the pathname after the descriptor chain has been verified."""

                parent.rename(root / "old-sub")
                parent.symlink_to(outside, target_is_directory=True)
                original(file, *args)

            with patch.object(Gio.File, "replace_async", switched):
                result = self._save(root, path, b"new snapshot")
            self.assertIsInstance(result, tuple)
            self.assertEqual((outside / "file.txt").read_text(), "original")
            self.assertEqual((root / "old-sub" / "file.txt").read_bytes(), b"new snapshot")

    def test_final_symlink_inserted_after_validation_is_not_followed(self):
        """Even a forced overwrite replaces a raced-in symlink instead of its external target."""

        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            base = Path(directory)
            root = base / "project"
            root.mkdir()
            path = root / "file.txt"
            path.write_text("original")
            outside = base / "outside.txt"
            outside.write_text("outside")
            original = Gio.File.replace_async

            def switched(file, *args):
                """Insert a final symlink immediately before Gio opens the replacement."""

                path.unlink()
                path.symlink_to(outside)
                original(file, *args)

            with patch.object(Gio.File, "replace_async", switched):
                result = self._save(root, path, b"new snapshot")
            self.assertIsInstance(result, tuple)
            self.assertEqual(outside.read_text(), "outside")
            self.assertFalse(path.is_symlink())
            self.assertEqual(path.read_bytes(), b"new snapshot")

    def test_cancellation_before_commit_keeps_original_and_removes_temporary_output(self):
        """Cancelling after writing the temporary stream cannot publish its contents."""

        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            root = Path(directory)
            path = root / "file.txt"
            path.write_text("original")
            original = AnchoredFileSave._written

            def cancelled(save, stream, result):
                """Cancel between writing the snapshot and atomically committing it."""

                save.cancellable.cancel()
                original(save, stream, result)

            with patch.object(AnchoredFileSave, "_written", cancelled):
                result = self._save(root, path, b"new snapshot")
            self.assertIsInstance(result, GLib.Error)
            self.assertEqual(path.read_text(), "original")
            self.assertEqual(sorted(item.name for item in root.iterdir()), ["file.txt"])
