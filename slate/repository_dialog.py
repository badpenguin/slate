"""Shared asynchronous shell for explicit repository-operation dialogs."""

from __future__ import annotations

import re
from typing import Callable, Mapping, Sequence

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, Gtk  # noqa: E402

from .processes import AsyncCommand, CommandResult, run_async
from .scm.base import SCM
from .watcher import RepoWatcher


# 2026-08-18: un solo proprietario del ciclo asincrono evita divergenze tra
# Aggiorna e le altre azioni su cancellazione, credenziali e rilascio watcher.
class RepositoryOperationDialog(Gtk.Dialog):
    """Own the common UI and asynchronous lifecycle of a repository operation."""

    def __init__(
        self,
        parent: Gtk.Window,
        title: str,
        scm: SCM,
        watcher: RepoWatcher,
        on_closed: Callable[[], None],
        *,
        action_label: str | None = None,
        cancellation_title: str,
        allow_idle_close: bool,
    ) -> None:
        """Build one modal shell and retain its single-repository dependencies."""

        super().__init__(
            title=title,
            transient_for=parent,
            modal=True,
            destroy_with_parent=True,
        )
        self.set_default_size(540, -1)
        self.scm = scm
        self.watcher = watcher
        self.on_closed = on_closed
        self.cancellation_title = cancellation_title
        self.allow_idle_close = allow_idle_close
        self.command: AsyncCommand | None = None
        self.command_cancellable = False
        self.watcher_acquired = False
        self.watcher_released = False
        self.finished = False
        self.cancel_requested = False
        self.ready_to_submit = False
        self.form_widgets: list[Gtk.Widget] = []

        self.cancel_button = self.add_button("Cancel", Gtk.ResponseType.CANCEL)
        self.action_button = (
            self.add_button(action_label, Gtk.ResponseType.OK)
            if action_label is not None
            else None
        )
        self.close_button = self.add_button("Close", Gtk.ResponseType.CLOSE)
        self.cancel_button.set_sensitive(allow_idle_close)
        if self.action_button is not None:
            self.action_button.set_sensitive(False)
            self.close_button.set_no_show_all(True)
        else:
            self.close_button.set_sensitive(False)
        self.connect("response", self._on_response)
        self.connect("delete-event", self._on_delete_event)

        self.content = self.get_content_area()
        self.content.set_border_width(18)
        self.content.set_spacing(12)
        identity = Gtk.Label(label=f"{scm.display_name}  ·  {scm.root}", xalign=0)
        identity.set_selectable(True)
        self.content.pack_start(identity, False, False, 0)

        progress = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.spinner = Gtk.Spinner()
        self.phase_label = Gtk.Label(xalign=0)
        self.phase_label.set_line_wrap(True)
        progress.pack_start(self.spinner, False, False, 0)
        progress.pack_start(self.phase_label, True, True, 0)
        self.content.pack_start(progress, False, False, 0)
        self.detail_label = Gtk.Label(xalign=0)
        self.detail_label.set_line_wrap(True)
        self.detail_label.set_selectable(True)
        self.content.pack_start(self.detail_label, False, False, 0)

    def start(self) -> None:
        """Acquire the watcher boundary before inspecting repository state."""

        self.spinner.start()
        self._set_progress("Waiting for the local check…")
        self.watcher.pause_after_current(self._on_watcher_paused)

    def _on_watcher_paused(self) -> None:
        """Begin the specialized workflow after automatic commands become quiet."""

        self.watcher_acquired = True
        self._begin()

    def _begin(self) -> None:
        """Start the operation-specific workflow after watcher acquisition."""

        raise NotImplementedError

    def _run_command(
        self,
        argv: Sequence[str],
        callback: Callable[[CommandResult], None],
        phase: str,
        *,
        cancellable: bool = False,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        """Run one serialized command and expose cancellation only when safe."""

        self._set_progress(phase)
        if self.action_button is not None:
            self.action_button.set_sensitive(False)
        self.command_cancellable = cancellable
        self.cancel_button.set_sensitive(cancellable)
        self.command = run_async(
            argv,
            callback,
            cwd=self.scm.root,
            env=environment or self.scm.environment,
        )

    def _prepare_result(self) -> bool:
        """Release command state and finish a requested cancellation once."""

        self.command = None
        self.command_cancellable = False
        self.cancel_button.set_sensitive(self.allow_idle_close)
        if not self.cancel_requested:
            return True
        self._finish(
            self.cancellation_title,
            "The remote command was interrupted.",
        )
        return False

    def _set_progress(self, phase: str, detail: str = "") -> None:
        """Update the stable phase and detail labels in place."""

        self.phase_label.set_text(phase)
        self.detail_label.set_text(detail)

    def _finish(self, title: str, detail: str) -> None:
        """Present one terminal result and expose only explicit closure."""

        self.finished = True
        self.command = None
        self.command_cancellable = False
        self.spinner.stop()
        self._set_progress(title, detail)
        self.cancel_button.set_sensitive(False)
        if self.action_button is not None:
            self.cancel_button.hide()
            self.action_button.hide()
            for widget in self.form_widgets:
                widget.set_sensitive(False)
            self.close_button.show()
        else:
            self.close_button.set_sensitive(True)
        self.close_button.grab_focus()

    def _finish_error(self, title: str, result: CommandResult) -> None:
        """Present a sanitized command failure without interpreting display text."""

        detail = result.stderr.strip()
        if not detail and result.error is not None:
            detail = str(result.error)
        self._finish(title, self._redact_credentials(detail or "Command failed."))

    @staticmethod
    def _redact_credentials(message: str) -> str:
        """Remove passwords embedded in HTTP-style repository URLs."""

        return re.sub(r"(https?://[^\s/:]+:)[^@\s]+@", r"\1…@", message)

    def _on_response(self, _dialog: Gtk.Dialog, response: int) -> None:
        """Route submission, safe cancellation and completed closure."""

        if response == Gtk.ResponseType.OK and self.ready_to_submit:
            self._submit()
        elif response == Gtk.ResponseType.CANCEL:
            if self.command is not None and self.command_cancellable:
                self.cancel_requested = True
                self.cancel_button.set_sensitive(False)
                self.phase_label.set_text("Cancelling…")
                self.command.cancel()
            elif self.command is None and self.allow_idle_close:
                self._close()
        elif response == Gtk.ResponseType.CLOSE and self.finished:
            self._close()

    def _submit(self) -> None:
        """Execute the operation represented by the dialog's form values."""

        raise NotImplementedError

    def _on_delete_event(self, _dialog: Gtk.Dialog, _event: Gdk.Event) -> bool:
        """Close only in a state allowed by the specialized dialog contract."""

        if self.command is None and (self.finished or self.allow_idle_close):
            self._close()
        return True

    def _close(self) -> None:
        """Resume one coherent watcher refresh and destroy this modal."""

        if self.watcher_acquired and not self.watcher_released:
            self.watcher_released = True
            self.watcher.resume_with_full_refresh(refresh_branch=True)
        self.destroy()
        self.on_closed()
