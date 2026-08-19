"""Read-only overlay previews for SCM diffs and complete source files."""

from __future__ import annotations

from functools import partial
from pathlib import Path
from typing import Callable

import gi

gi.require_version("Gio", "2.0")
gi.require_version("Gtk", "3.0")
gi.require_version("GtkSource", "4")
from gi.repository import Gio, GLib, Gtk, GtkSource, Pango  # noqa: E402

from .processes import AsyncCommand, CommandResult, run_async
from .scm.base import FileStatus, SCM


class FilePreview(Gtk.Box):
    """Render one selected path without allowing edits to the working copy."""

    MAX_TEXT_BYTES = 5 * 1024 * 1024

    def __init__(self, on_close: Callable[[], None]) -> None:
        """Build a header and GtkSourceView suitable for diff and source content."""

        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.on_close = on_close
        self.current_path: str | None = None
        self.command: AsyncCommand | None = None
        self.file_cancellable: Gio.Cancellable | None = None
        self.request_serial = 0
        self.get_style_context().add_class("file-preview")
        self.set_no_show_all(True)

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        header.get_style_context().add_class("preview-header")
        self.title = Gtk.Label()
        self.title.set_xalign(0)
        self.title.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        self.title.get_style_context().add_class("preview-title")
        close_button = Gtk.Button()
        close_button.set_relief(Gtk.ReliefStyle.NONE)
        close_button.set_image(
            Gtk.Image.new_from_icon_name("window-close-symbolic", Gtk.IconSize.BUTTON)
        )
        close_button.set_tooltip_text("Close preview (Esc)")
        close_button.get_accessible().set_name("Close preview")
        close_button.connect("clicked", self._on_close_clicked)
        header.pack_start(self.title, True, True, 0)
        header.pack_end(close_button, False, False, 0)
        self.pack_start(header, False, False, 0)

        # 2026-08-17: mantenere stabile il GtkTextBTree evita che callback di
        # layout già accodati conservino righe appartenenti a un buffer sostituito.
        self.buffer = GtkSource.Buffer()
        self.diff_tags = {
            "added": self.buffer.create_tag("diff-added", foreground="#26a269"),
            "removed": self.buffer.create_tag("diff-removed", foreground="#e01b24"),
            "hunk": self.buffer.create_tag(
                "diff-hunk", foreground="#1c71d8", weight=Pango.Weight.BOLD
            ),
            "header": self.buffer.create_tag(
                "diff-header", weight=Pango.Weight.BOLD
            ),
            "note": self.buffer.create_tag("diff-note", style=Pango.Style.ITALIC),
        }
        self.view = GtkSource.View.new_with_buffer(self.buffer)
        self.view.set_editable(False)
        self.view.set_cursor_visible(False)
        self.view.set_monospace(True)
        self.view.set_show_line_numbers(True)
        self.view.set_highlight_current_line(False)
        self.view.set_tab_width(4)
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroller.add(self.view)
        self.pack_start(scroller, True, True, 0)
        self._set_plain_text("Select a file to preview it.")

    def show_status(
        self, root: str, scm: SCM, status: FileStatus
    ) -> None:
        """Choose complete-file or diff presentation from normalized SCM state."""

        self.cancel()
        request_serial = self.request_serial
        self.current_path = status.path
        title = (
            f"{status.source_path} → {status.path}"
            if status.state == "moved" and status.source_path
            else status.path
        )
        self.title.set_text(title)
        self.title.set_tooltip_text(title)
        self._set_plain_text("Loading preview…")
        if status.state == "moved" and status.source_path:
            # 2026-08-17: rename-aware output is required because a normal patch
            # can render a move as unrelated add/remove blocks.
            self.command = run_async(
                scm.preview_move_diff_argv(status.source_path, status.path),
                partial(self._on_diff_loaded, request_serial, status.path),
                cwd=scm.root,
                env=scm.environment,
            )
            return
        if status.state in {"untracked", "added"}:
            self._load_working_file(root, status.path, request_serial)
            return
        if status.state == "removed":
            self.command = run_async(
                scm.base_argv(status.path),
                partial(self._on_base_loaded, request_serial, status.path),
                cwd=scm.root,
                env=scm.environment,
            )
            return
        self.command = run_async(
            scm.preview_diff_argv(status.path),
            partial(self._on_diff_loaded, request_serial, status.path),
            cwd=scm.root,
            env=scm.environment,
        )

    def show_file(self, root: str, relative_path: str) -> None:
        """Preview a normal project file independently from source-control state."""

        self.cancel()
        request_serial = self.request_serial
        self.current_path = relative_path
        self.title.set_text(relative_path)
        self.title.set_tooltip_text(relative_path)
        self._set_plain_text("Loading preview…")
        # 2026-08-16: la vista File usa lo stesso lettore sicuro e gli stessi
        # limiti della preview SCM, senza creare un secondo renderer di sorgenti.
        self._load_working_file(root, relative_path, request_serial)

    def cancel(self) -> None:
        """Cancel asynchronous work belonging to a superseded or closed preview."""

        # 2026-08-16: il seriale invalida anche callback già accodati nel main
        # loop, che una cancellazione Gio da sola non può ritirare.
        self.request_serial += 1
        if self.command is not None:
            self.command.cancel()
            self.command = None
        if self.file_cancellable is not None:
            self.file_cancellable.cancel()
            self.file_cancellable = None

    def _load_working_file(
        self, root: str, relative_path: str, request_serial: int
    ) -> None:
        """Read one safe current working-copy path asynchronously through Gio."""

        root_path = Path(root).resolve()
        candidate = (root_path / relative_path).resolve()
        try:
            candidate.relative_to(root_path)
        except ValueError:
            self._set_plain_text("Preview rejected: the path leaves the project.")
            return
        try:
            size = candidate.stat().st_size
        except OSError as error:
            self._set_plain_text(f"Unable to read the file:\n{error}")
            return
        if size > self.MAX_TEXT_BYTES:
            self._set_plain_text(
                f"File too large to preview ({size / 1024 / 1024:.1f} MiB)."
            )
            return
        self.file_cancellable = Gio.Cancellable()
        Gio.File.new_for_path(str(candidate)).load_contents_async(
            self.file_cancellable,
            self._on_file_loaded,
            (request_serial, relative_path),
        )

    def _on_file_loaded(
        self,
        source: Gio.File,
        result: Gio.AsyncResult,
        request: tuple[int, str],
    ) -> None:
        """Decode and syntax-highlight a completed untracked-file read."""

        request_serial, relative_path = request
        if request_serial != self.request_serial or relative_path != self.current_path:
            return
        self.file_cancellable = None
        try:
            _success, contents, _etag = source.load_contents_finish(result)
        except GLib.Error as error:
            if not error.matches(Gio.io_error_quark(), Gio.IOErrorEnum.CANCELLED):
                self._set_plain_text(f"Unable to read the file:\n{error}")
            return
        data = bytes(contents)
        if len(data) > self.MAX_TEXT_BYTES:
            self._set_plain_text(
                f"File too large to preview ({len(data) / 1024 / 1024:.1f} MiB)."
            )
            return
        if b"\0" in data:
            self._set_plain_text("Binary file: text preview is unavailable.")
            return
        self._set_source_text(data.decode("utf-8", "replace"), relative_path)

    def _on_base_loaded(
        self, request_serial: int, relative_path: str, result: CommandResult
    ) -> None:
        """Render the complete parent-revision content of a removed file."""

        if request_serial != self.request_serial or relative_path != self.current_path:
            return
        self.command = None
        if not result.ok:
            self._set_plain_text(
                result.stderr.strip() or "Unable to read the base revision."
            )
            return
        encoded = result.stdout.encode("utf-8", "replace")
        if len(encoded) > self.MAX_TEXT_BYTES:
            self._set_plain_text("Base file too large for the internal preview.")
            return
        if "\0" in result.stdout:
            self._set_plain_text("Binary file: text preview is unavailable.")
            return
        self._set_source_text(result.stdout, relative_path)

    def _on_diff_loaded(
        self, request_serial: int, relative_path: str, result: CommandResult
    ) -> None:
        """Render a completed SCM diff only if it is still current."""

        if request_serial != self.request_serial or relative_path != self.current_path:
            return
        self.command = None
        if not result.ok:
            self._set_plain_text(
                result.stderr.strip() or "Unable to generate the diff."
            )
            return
        if len(result.stdout.encode("utf-8", "replace")) > self.MAX_TEXT_BYTES:
            self._set_plain_text("Diff too large for the internal preview.")
            return
        self._set_diff_text(result.stdout or "No textual differences available.\n")

    def _set_source_text(self, text: str, path: str) -> None:
        """Display an entire text file with language-aware highlighting."""

        language = GtkSource.LanguageManager.get_default().guess_language(path, None)
        self.buffer.set_language(language)
        self.buffer.set_highlight_syntax(language is not None)
        scheme_name = self._preferred_scheme_name()
        scheme = GtkSource.StyleSchemeManager.get_default().get_scheme(scheme_name)
        if scheme is not None:
            self.buffer.set_style_scheme(scheme)
        self.buffer.set_text(text)

    def _set_diff_text(self, text: str) -> None:
        """Display a unified diff with semantic colors based on line prefixes."""

        self.buffer.set_language(None)
        self.buffer.set_highlight_syntax(False)
        self.buffer.set_text(text)
        offset = 0
        for line in text.splitlines(keepends=True):
            # 2026-08-17: impostare il testo una sola volta riduce le
            # invalidazioni del layout; gli offset Unicode delimitano poi le righe.
            start = self.buffer.get_iter_at_offset(offset)
            offset += len(line)
            end = self.buffer.get_iter_at_offset(offset)
            tag = None
            if line.startswith("@@"):
                tag = self.diff_tags["hunk"]
            elif line.startswith(("diff ", "--- ", "+++ ")):
                tag = self.diff_tags["header"]
            elif line.startswith("+"):
                tag = self.diff_tags["added"]
            elif line.startswith("-"):
                tag = self.diff_tags["removed"]
            elif line.startswith("\\ No newline"):
                tag = self.diff_tags["note"]
            if tag is not None:
                self.buffer.apply_tag(tag, start, end)

    def _set_plain_text(self, text: str) -> None:
        """Display an informational or error message without syntax processing."""

        self.buffer.set_language(None)
        self.buffer.set_highlight_syntax(False)
        self.buffer.set_text(text)

    @staticmethod
    def _preferred_scheme_name() -> str:
        """Choose a bundled GtkSource scheme that follows light/dark GTK themes."""

        settings = Gtk.Settings.get_default()
        theme_name = str(settings.get_property("gtk-theme-name") if settings else "")
        return "oblivion" if "dark" in theme_name.lower() else "classic"

    def _on_close_clicked(self, _button: Gtk.Button) -> None:
        """Forward an explicit close action to the overlay owner."""

        self.on_close()
