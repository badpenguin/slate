"""Verify preview byte limits and asynchronous stream cleanup without GTK windows."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from gi.repository import Gio, GLib

from slate.bounded_read import BoundedFileRead


class BoundedReadTest(unittest.TestCase):
    """Exercise real Gio reads, including growth after the metadata check."""

    def _run_read(self, path, limit, cancellable=None):
        """Read one private file while dispatching a bounded test main loop."""

        results = []
        loop = GLib.MainLoop()

        def completed(data, error):
            """Record the completed read after stream cleanup."""

            results.append((data, error))
            loop.quit()

        def expired():
            """Stop the test loop if a callback never arrives."""

            loop.quit()
            return GLib.SOURCE_REMOVE

        timeout = GLib.timeout_add(2000, expired)
        try:
            reader = BoundedFileRead(Gio.File.new_for_path(str(path)), limit,
                                     cancellable or Gio.Cancellable(), completed)
            loop.run()
            self.assertEqual(len(results), 1)
            if reader.stream is not None:
                self.assertTrue(reader.stream.is_closed())
            return reader, results[0]
        finally:
            GLib.source_remove(timeout)

    def test_exact_limit_and_empty_file_are_read_successfully(self):
        """A boundary-sized file and an empty file both terminate at EOF."""

        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            path = Path(directory) / "text.txt"
            for data in (b"", b"abcdefgh"):
                path.write_bytes(data)
                _reader, result = self._run_read(path, 8)
                self.assertEqual(result, (data, None))

    def test_oversized_metadata_prevents_opening_the_stream(self):
        """A known oversized file is rejected before any contents are read."""

        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            path = Path(directory) / "large.txt"
            path.write_bytes(b"too large")
            with patch("gi.repository.Gio.File.read_async") as opened:
                _reader, (data, error) = self._run_read(path, 8)
                opened.assert_not_called()
            self.assertIsNone(data)
            self.assertIsInstance(error, ValueError)

    def test_file_growth_after_metadata_stays_bounded(self):
        """Growth between metadata and open never accumulates beyond limit plus one."""

        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            path = Path(directory) / "growing.txt"
            path.write_bytes(b"small")
            original = BoundedFileRead._opened

            def grew(reader, file, result):
                """Grow the file after its metadata check, before the first chunk."""

                path.write_bytes(b"x" * 100000)
                original(reader, file, result)

            with patch.object(BoundedFileRead, "_opened", grew):
                reader, (data, error) = self._run_read(path, 8)
            self.assertIsNone(data)
            self.assertIsInstance(error, ValueError)
            self.assertEqual(len(reader.data), 9)

    def test_cancellation_after_open_closes_the_stream(self):
        """A superseded request cancels chunk reads and still releases its stream."""

        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            path = Path(directory) / "text.txt"
            path.write_bytes(b"text")
            original = BoundedFileRead._read_next

            def cancelled(reader):
                """Cancel the request after open and before the first read."""

                reader.cancellable.cancel()
                original(reader)

            with patch.object(BoundedFileRead, "_read_next", cancelled):
                _reader, (data, error) = self._run_read(path, 8)
            self.assertIsNone(data)
            self.assertTrue(error.matches(Gio.io_error_quark(), Gio.IOErrorEnum.CANCELLED))
