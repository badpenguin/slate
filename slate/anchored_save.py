"""Atomic Gio saves anchored to directory descriptors on Linux."""

import os
from pathlib import Path
import stat

from gi.repository import Gio, GLib


class AnchoredFileSave:
    """Keep the verified parent directory alive throughout an asynchronous save."""

    def __init__(self, root, path, original_file, data, etag, cancellable, callback, saved_text):
        """Open a no-follow directory chain and start a replacement within that directory."""

        self.original_file = original_file
        self.data = data
        self.cancellable = cancellable
        self.callback = callback
        self.saved_text = saved_text
        self.parent_fd = None
        self.stream = None
        self.error = None
        self.etag = None
        self.mode = None
        # 2026-11-04: checking a pathname before asynchronous I/O leaves a
        # race. /proc/self/fd pins the parent object; O_NOFOLLOW rejects links
        # along the chain and REPLACE_DESTINATION never follows the final link.
        try:
            target = Path(path)
            target.relative_to(Path(root))
            self.parent_fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
            for component in target.parent.parts[1:]:
                next_fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                  dir_fd=self.parent_fd)
                os.close(self.parent_fd)
                self.parent_fd = next_fd
            try:
                info = os.stat(target.name, dir_fd=self.parent_fd, follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode):
                    raise OSError("The file target is no longer a regular file")
                self.mode = stat.S_IMODE(info.st_mode)
            except FileNotFoundError:
                pass
            anchored = Gio.File.new_for_path(f"/proc/self/fd/{self.parent_fd}/{target.name}")
            anchored.replace_async(etag, False, Gio.FileCreateFlags.REPLACE_DESTINATION,
                                   GLib.PRIORITY_DEFAULT, cancellable, self._opened)
        except (GLib.Error, OSError, ValueError) as error:
            self.error = (error if isinstance(error, GLib.Error) else
                          GLib.Error.new_literal(Gio.io_error_quark(), str(error), Gio.IOErrorEnum.FAILED))
            GLib.idle_add(self._deliver)

    def _opened(self, file, result):
        """Preserve Unix permissions on the temporary output before writing its contents."""

        try:
            self.stream = file.replace_finish(result)
            # 2026-08-31: atomic replacement must preserve executable scripts.
            # 2026-11-04: set the mode on the open temporary descriptor before
            # publication, avoiding a chmod race against the destination path.
            if self.mode is not None:
                os.fchmod(Gio.FileDescriptorBased.get_fd(self.stream), self.mode)
            if self.data:
                self.stream.write_all_async(self.data, GLib.PRIORITY_DEFAULT,
                                            self.cancellable, self._written)
            else:
                self._close(None)
        except (GLib.Error, OSError) as error:
            self._close(error)

    def _written(self, stream, result):
        """Close only after the complete submitted snapshot has been written."""

        try:
            stream.write_all_finish(result)
            self._close(None)
        except GLib.Error as error:
            self._close(error)

    def _close(self, error):
        """Commit successful output or abort a failed replacement through a cancelled close."""

        if isinstance(error, OSError):
            error = GLib.Error.new_literal(Gio.io_error_quark(), str(error), Gio.IOErrorEnum.FAILED)
        self.error = error
        if self.stream is None:
            self._deliver()
            return
        token = self.cancellable
        if error is not None:
            token = Gio.Cancellable()
            token.cancel()
        self.stream.close_async(GLib.PRIORITY_DEFAULT, token, self._closed)

    def _closed(self, stream, result):
        """Record the committed etag and release the pinned directory on every result."""

        try:
            stream.close_finish(result)
            if self.error is None:
                self.etag = stream.get_etag()
        except GLib.Error as error:
            if self.error is None:
                self.error = error
        self._deliver()

    def _deliver(self):
        """Release descriptors and invoke the editor's existing save-completion contract."""

        if self.parent_fd is not None:
            os.close(self.parent_fd)
            self.parent_fd = None
        self.callback(self, None, self.saved_text)
        return GLib.SOURCE_REMOVE

    def replace_contents_finish(self, _result):
        """Expose a Gio-compatible completion result to the canonical editor callback."""

        if self.error is not None:
            raise self.error
        return True, self.etag

    def equal(self, file):
        """Retain the original logical identity for rename checks after completion."""

        return self.original_file.equal(file)
