"""Bounded asynchronous file reads for textual previews."""

import base64
from pathlib import Path
import sys

from gi.repository import Gio, GLib

from .processes import run_async


class BoundedFileRead:
    """Check file size and read at most the byte limit plus one overflow byte."""

    def __init__(self, file, limit, cancellable, callback, *, root=None):
        """Start metadata inspection before opening the file stream."""

        # 2026-11-03: checking metadata alone cannot bound a file that grows
        # during the read. Every chunk also respects the remaining byte budget.
        self.limit = limit
        self.cancellable = cancellable
        self.callback = callback
        self.stream = None
        self.data = bytearray()
        self.error = None
        self.command = None
        self.cancel_handler = None
        if root is not None:
            # 2026-11-06: la ricerca non può seguire link sostituiti dopo la
            # validazione; il worker legge attraverso descrittori no-follow.
            self.command = run_async(
                [sys.executable, "-m", "slate.bounded_read_worker", str(root), file.get_path(), str(limit)],
                self._worker_completed, cwd=str(Path(__file__).resolve().parent.parent),
            )
            self.cancel_handler = cancellable.connect(self._cancel_worker, None)
            return
        file.query_info_async("standard::size,standard::type", Gio.FileQueryInfoFlags.NONE,
                              GLib.PRIORITY_DEFAULT, cancellable, self._inspected)

    def _cancel_worker(self):
        """Stop a project-bound read when its owning search is superseded."""

        if self.command is not None:
            self.command.cancel()

    def _worker_completed(self, result):
        """Decode bounded binary contents or deliver an explicit cancellation/error."""

        self.command = None
        if self.cancel_handler:
            self.cancellable.disconnect(self.cancel_handler)
            self.cancel_handler = None
        if self.cancellable.is_cancelled():
            self.callback(None, GLib.Error.new_literal(Gio.io_error_quark(), "Cancelled", Gio.IOErrorEnum.CANCELLED))
        elif not result.ok:
            self.callback(None, ValueError(str(result.error or result.stderr.strip() or "Unable to read the file")))
        else:
            try:
                data = base64.b64decode(result.stdout, validate=True)
                if len(data) > self.limit:
                    raise ValueError("Preview byte limit exceeded")
            except ValueError as error:
                self.callback(None, error)
                return
            self.callback(data, None)

    def _inspected(self, file, result):
        """Reject oversized or nonregular files before requesting their contents."""

        try:
            info = file.query_info_finish(result)
            if info.get_size() > self.limit:
                raise ValueError("File over 5 MiB: preview is unavailable")
            if info.get_file_type() != Gio.FileType.REGULAR:
                raise ValueError("Preview requires a regular file")
            file.read_async(GLib.PRIORITY_DEFAULT, self.cancellable, self._opened)
        except (GLib.Error, ValueError) as error:
            self._finish(error)

    def _opened(self, file, result):
        """Retain the stream and start the first bounded chunk."""

        try:
            self.stream = file.read_finish(result)
            self._read_next()
        except GLib.Error as error:
            self._finish(error)

    def _read_next(self):
        """Read no more than 64 KiB or the remaining overflow-detection budget."""

        self.stream.read_bytes_async(min(65536, self.limit + 1 - len(self.data)),
                                     GLib.PRIORITY_DEFAULT, self.cancellable, self._read)

    def _read(self, stream, result):
        """Accumulate a bounded chunk or finish on EOF, cancellation or overflow."""

        try:
            chunk = stream.read_bytes_finish(result).get_data()
            self.data.extend(chunk)
            if len(self.data) > self.limit:
                raise ValueError("File over 5 MiB: preview is unavailable")
            if chunk:
                self._read_next()
            else:
                self._finish(None)
        except (GLib.Error, ValueError) as error:
            self._finish(error)

    def _finish(self, error):
        """Close an opened stream asynchronously before delivering the result."""

        self.error = error
        if self.stream is None:
            self.callback(None, error)
        else:
            self.stream.close_async(GLib.PRIORITY_DEFAULT, None, self._closed)

    def _closed(self, stream, result):
        """Consume stream closure and deliver either bounded bytes or an error."""

        try:
            stream.close_finish(result)
        except GLib.Error as error:
            if self.error is None:
                self.error = error
        self.callback(bytes(self.data) if self.error is None else None, self.error)
