"""Shared fixtures for repository-operation dialog tests."""

from __future__ import annotations

import tempfile
import unittest
from unittest.mock import MagicMock, patch

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # noqa: E402

from slate.processes import CommandResult
from slate.scm.base import SCM


class RepositoryDialogFixture:
    """Provide one intercepted command queue and disposable modal lifecycle."""

    @classmethod
    def setUpClass(cls) -> None:
        """Require the GTK display used by integration-style widget tests."""

        initialized, _arguments = Gtk.init_check(None)
        if not initialized:
            raise unittest.SkipTest("display GTK non disponibile")

    def setUp(self) -> None:
        """Intercept subprocesses and create shared watcher/dialog test state."""

        self.temporary = tempfile.TemporaryDirectory()
        self.calls: list[tuple[list[str], object, dict]] = []
        self.command = MagicMock()
        self.run_patch = patch(
            "slate.repository_dialog.run_async", self._record_command
        )
        self.run_patch.start()
        self.watcher = MagicMock()
        self.on_closed = MagicMock()
        self.dialog: Gtk.Dialog | None = None

    def tearDown(self) -> None:
        """Destroy any remaining modal and release patches and temporary paths."""

        if self.dialog is not None:
            self.dialog.destroy()
        self.run_patch.stop()
        self.temporary.cleanup()

    def _record_command(self, argv, callback, **kwargs):
        """Retain one asynchronous callback for deterministic completion."""

        self.calls.append((list(argv), callback, kwargs))
        return self.command

    def _open(self, dialog_type, scm: SCM):
        """Build one modal and grant its watcher ownership boundary."""

        dialog = dialog_type(
            None,
            scm,
            self.watcher,
            self.on_closed,
        )
        self.dialog = dialog
        dialog.start()
        paused_callback = self.watcher.pause_after_current.call_args.args[0]
        paused_callback()
        return dialog

    def _complete(
        self,
        index: int,
        stdout: str = "",
        stderr: str = "",
        returncode: int = 0,
    ) -> None:
        """Complete one intercepted command with controlled process output."""

        argv, callback, _kwargs = self.calls[index]
        callback(CommandResult(tuple(argv), returncode, stdout, stderr))
