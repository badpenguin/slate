"""Asynchronous creation, rename, inspection and deletion of project entries."""

from __future__ import annotations

from dataclasses import dataclass
from functools import partial
from typing import Callable

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib  # noqa: E402


OperationCallback = Callable[[str | None], None]
InspectionCallback = Callable[["DirectoryInspection | None", str | None], None]


@dataclass(frozen=True)
class DirectoryInspection:
    """Describe direct children without following symbolic links."""

    child_names: tuple[str, ...]
    contains_directory: bool

    @property
    def empty(self) -> bool:
        """Return whether the directory has no direct children."""

        return not self.child_names


class ProjectFileOperations:
    """Run bounded project file operations without blocking GTK's main loop."""

    ATTRIBUTES = ",".join(
        (
            Gio.FILE_ATTRIBUTE_STANDARD_NAME,
            Gio.FILE_ATTRIBUTE_STANDARD_TYPE,
            Gio.FILE_ATTRIBUTE_STANDARD_IS_SYMLINK,
        )
    )

    def create_file(self, path: str, callback: OperationCallback) -> None:
        """Create one empty file without replacing an existing entry."""

        Gio.File.new_for_path(path).create_async(
            Gio.FileCreateFlags.NONE,
            GLib.PRIORITY_DEFAULT,
            None,
            self._on_file_created,
            callback,
        )

    def _on_file_created(
        self,
        source: Gio.File,
        result: Gio.AsyncResult,
        callback: OperationCallback,
    ) -> None:
        """Close a newly created empty stream before reporting success."""

        try:
            stream = source.create_finish(result)
        except GLib.Error as error:
            callback(str(error))
            return
        stream.close_async(
            GLib.PRIORITY_DEFAULT,
            None,
            self._on_created_file_closed,
            callback,
        )

    def _on_created_file_closed(
        self,
        stream: Gio.OutputStream,
        result: Gio.AsyncResult,
        callback: OperationCallback,
    ) -> None:
        """Report the result of flushing and closing a new empty file."""

        try:
            stream.close_finish(result)
        except GLib.Error as error:
            callback(str(error))
            return
        callback(None)

    def create_directory(self, path: str, callback: OperationCallback) -> None:
        """Create one directory without creating missing parent directories."""

        Gio.File.new_for_path(path).make_directory_async(
            GLib.PRIORITY_DEFAULT,
            None,
            self._on_directory_created,
            callback,
        )

    def _on_directory_created(
        self,
        source: Gio.File,
        result: Gio.AsyncResult,
        callback: OperationCallback,
    ) -> None:
        """Report completion of an asynchronous directory creation."""

        try:
            source.make_directory_finish(result)
        except GLib.Error as error:
            callback(str(error))
            return
        callback(None)

    def rename_entry(
        self, source_path: str, destination_path: str, callback: OperationCallback
    ) -> None:
        """Rename one file, link or directory without replacing its destination."""

        # 2026-08-16: Gio mantiene l'operazione asincrona sul main loop e NONE
        # rifiuta collisioni invece di sovrascrivere implicitamente altri dati.
        source = Gio.File.new_for_path(source_path)
        source.move_async(
            Gio.File.new_for_path(destination_path),
            Gio.FileCopyFlags.NONE,
            GLib.PRIORITY_DEFAULT,
            None,
            None,
            None,
            self._on_entry_renamed,
            callback,
        )

    def _on_entry_renamed(
        self,
        source: Gio.File,
        result: Gio.AsyncResult,
        callback: OperationCallback,
    ) -> None:
        """Report completion of one asynchronous filesystem rename."""

        try:
            source.move_finish(result)
        except GLib.Error as error:
            callback(str(error))
            return
        callback(None)

    def inspect_directory(self, path: str, callback: InspectionCallback) -> None:
        """Enumerate direct children without following links or descending."""

        directory = Gio.File.new_for_path(path)
        directory.enumerate_children_async(
            self.ATTRIBUTES,
            Gio.FileQueryInfoFlags.NOFOLLOW_SYMLINKS,
            GLib.PRIORITY_DEFAULT,
            None,
            self._on_inspector_ready,
            callback,
        )

    def _on_inspector_ready(
        self,
        source: Gio.File,
        result: Gio.AsyncResult,
        callback: InspectionCallback,
    ) -> None:
        """Start paged reads after opening a directory enumerator."""

        try:
            enumerator = source.enumerate_children_finish(result)
        except GLib.Error as error:
            callback(None, str(error))
            return
        enumerator.next_files_async(
            100,
            GLib.PRIORITY_DEFAULT,
            None,
            self._on_inspection_page,
            (callback, []),
        )

    def _on_inspection_page(
        self,
        enumerator: Gio.FileEnumerator,
        result: Gio.AsyncResult,
        request: tuple[InspectionCallback, list[Gio.FileInfo]],
    ) -> None:
        """Collect bounded pages and publish one complete direct-child snapshot."""

        callback, entries = request
        try:
            page = enumerator.next_files_finish(result)
        except GLib.Error as error:
            callback(None, str(error))
            return
        if page:
            entries.extend(page)
            enumerator.next_files_async(
                100,
                GLib.PRIORITY_DEFAULT,
                None,
                self._on_inspection_page,
                request,
            )
            return
        enumerator.close_async(
            GLib.PRIORITY_DEFAULT, None, self._on_inspector_closed, None
        )
        callback(
            DirectoryInspection(
                tuple(info.get_name() for info in entries),
                any(
                    info.get_file_type() == Gio.FileType.DIRECTORY
                    and not info.get_is_symlink()
                    for info in entries
                ),
            ),
            None,
        )

    def _on_inspector_closed(
        self,
        enumerator: Gio.FileEnumerator,
        result: Gio.AsyncResult,
        _data: object,
    ) -> None:
        """Finish an enumerator close while ignoring cleanup-only failures."""

        try:
            enumerator.close_finish(result)
        except GLib.Error:
            pass

    def delete_entry(self, path: str, callback: OperationCallback) -> None:
        """Delete one file, link or empty directory asynchronously."""

        Gio.File.new_for_path(path).delete_async(
            GLib.PRIORITY_DEFAULT,
            None,
            self._on_entry_deleted,
            callback,
        )

    def _on_entry_deleted(
        self,
        source: Gio.File,
        result: Gio.AsyncResult,
        callback: OperationCallback,
    ) -> None:
        """Report completion of one asynchronous entry deletion."""

        try:
            source.delete_finish(result)
        except GLib.Error as error:
            callback(str(error))
            return
        callback(None)

    def delete_flat_directory(self, path: str, callback: OperationCallback) -> None:
        """Delete a directory only after proving it has no real subdirectories."""

        self.inspect_directory(
            path,
            partial(self._on_flat_directory_inspected, path, callback),
        )

    def _on_flat_directory_inspected(
        self,
        path: str,
        callback: OperationCallback,
        inspection: DirectoryInspection | None,
        error: str | None,
    ) -> None:
        """Reject subdirectories or begin deleting collected non-directory children."""

        if error is not None or inspection is None:
            callback(error or "Unable to inspect the directory.")
            return
        if inspection.contains_directory:
            callback("The directory contains other directories and cannot be deleted.")
            return
        parent = Gio.File.new_for_path(path)
        self._delete_next_child(parent, list(inspection.child_names), callback)

    def _delete_next_child(
        self,
        parent: Gio.File,
        names: list[str],
        callback: OperationCallback,
    ) -> None:
        """Delete direct non-directory children sequentially, then their parent."""

        if not names:
            parent.delete_async(
                GLib.PRIORITY_DEFAULT,
                None,
                self._on_entry_deleted,
                callback,
            )
            return
        child = parent.get_child(names.pop())
        child.delete_async(
            GLib.PRIORITY_DEFAULT,
            None,
            self._on_flat_child_deleted,
            (parent, names, callback),
        )

    def _on_flat_child_deleted(
        self,
        child: Gio.File,
        result: Gio.AsyncResult,
        request: tuple[Gio.File, list[str], OperationCallback],
    ) -> None:
        """Continue flat deletion only after one child was removed successfully."""

        parent, names, callback = request
        try:
            child.delete_finish(result)
        except GLib.Error as error:
            callback(str(error))
            return
        self._delete_next_child(parent, names, callback)
