"""Resolve renderer resources through asynchronous Gio metadata requests."""

import os
from pathlib import Path

from gi.repository import Gio, GLib


class ResourcePath:
    """Validate every path component without blocking GTK on filesystem metadata."""

    def __init__(self, root, relative, cancellable, callback):
        """Start a bounded symlink walk confined to the canonical project root."""

        # 2026-11-03: resolve()/is_file() can block on slow mounts; inspect
        # each component asynchronously while retaining canonical containment.
        self.root = root
        self.remaining = list(Path(relative).parts)
        self.current = root
        self.links = 0
        self.cancellable = cancellable
        self.callback = callback
        self._next()

    def _next(self):
        """Request metadata for the next component without following symlinks."""

        if not self.remaining or self.cancellable.is_cancelled():
            self.callback(None)
            return
        self.target = self.current / self.remaining.pop(0)
        Gio.File.new_for_path(str(self.target)).query_info_async(
            "standard::type,standard::symlink-target", Gio.FileQueryInfoFlags.NOFOLLOW_SYMLINKS,
            GLib.PRIORITY_DEFAULT, self.cancellable, self._inspected,
        )

    def _inspected(self, source, result):
        """Follow bounded internal links or return the validated regular file."""

        try:
            info = source.query_info_finish(result)
            kind = info.get_file_type()
            if kind == Gio.FileType.SYMBOLIC_LINK:
                self.links += 1
                if self.links > 40:
                    raise ValueError("Too many symbolic links")
                target = Path(os.path.normpath(self.current / info.get_symlink_target()))
                relative = target.relative_to(self.root)
                self.remaining = list(relative.parts) + self.remaining
                self.current = self.root
                self._next()
            elif self.remaining and kind == Gio.FileType.DIRECTORY:
                self.current = self.target
                self._next()
            elif not self.remaining and kind == Gio.FileType.REGULAR:
                self.callback(self.target)
            else:
                self.callback(None)
        except (GLib.Error, ValueError, OSError):
            self.callback(None)
