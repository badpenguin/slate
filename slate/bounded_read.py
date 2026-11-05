"""Bounded asynchronous file reads for textual previews."""

from gi.repository import Gio, GLib


class BoundedFileRead:
    """Check file size and read at most the byte limit plus one overflow byte."""

    def __init__(self, file, limit, cancellable, callback):
        """Start metadata inspection before opening the file stream."""

        # 2026-11-03: checking metadata alone cannot bound a file that grows
        # during the read. Every chunk also respects the remaining byte budget.
        self.limit = limit
        self.cancellable = cancellable
        self.callback = callback
        self.stream = None
        self.data = bytearray()
        self.error = None
        file.query_info_async("standard::size,standard::type", Gio.FileQueryInfoFlags.NONE,
                              GLib.PRIORITY_DEFAULT, cancellable, self._inspected)

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
