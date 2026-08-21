"""Asynchronous project-content search and read-only contextual previews."""

from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import gi

gi.require_version("Gdk", "3.0")
gi.require_version("Gio", "2.0")
gi.require_version("Gtk", "3.0")
gi.require_version("GtkSource", "4")
from gi.repository import Gdk, Gio, GLib, Gtk, GtkSource, Pango  # noqa: E402


@dataclass(frozen=True)
class SearchResult:
    """Describe one matching source line returned by ripgrep."""

    path: str
    line: int
    text: str
    spans: tuple[tuple[int, int], ...]
    complete_line: bool = True


@dataclass(frozen=True)
class SearchCompletion:
    """Describe the terminal state of one asynchronous search request."""

    returncode: int
    truncated: bool = False
    error: str = ""


def build_search_argv(query: str, excluded: set[str]) -> list[str]:
    """Build a literal smart-case ripgrep command for one project root."""

    argv = [
        "rg",
        "--json",
        "--fixed-strings",
        "--smart-case",
        "--max-filesize",
        "5M",
    ]
    for directory in sorted(excluded):
        argv.extend(("--glob", f"!**/{directory}/**"))
    argv.extend(("--", query, "."))
    return argv


class SearchCommand:
    """Stream bounded ripgrep JSON without blocking the GTK main loop."""

    def __init__(
        self,
        root: str,
        query: str,
        excluded: set[str],
        limit: int,
        on_result: Callable[[SearchResult], None],
        on_complete: Callable[[SearchCompletion], None],
    ) -> None:
        """Start one cancellable search and deliver results incrementally."""

        self.root = str(Path(root).resolve())
        self.argv = tuple(build_search_argv(query, excluded))
        self.limit = limit
        self.on_result = on_result
        self.on_complete = on_complete
        self.cancellable = Gio.Cancellable()
        self.process: Gio.Subprocess | None = None
        self.stream: Gio.DataInputStream | None = None
        self.cancelled = False
        self.truncated = False
        self.result_count = 0
        self.stdout_done = False
        self.process_done = False
        self.returncode = -1
        self.diagnostics: list[str] = []
        self.completed = False

        # 2026-08-20: lo streaming permette di terminare al limite globale
        # prima che un output enorme venga accumulato interamente in memoria.
        launcher = Gio.SubprocessLauncher.new(
            Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_MERGE
        )
        launcher.set_cwd(self.root)
        try:
            self.process = launcher.spawnv(self.argv)
        except Exception as error:  # GLib.Error is not the sole startup failure.
            GLib.idle_add(self._deliver_start_error, str(error))
            return
        output = self.process.get_stdout_pipe()
        self.stream = Gio.DataInputStream.new(output)
        self.process.wait_async(self.cancellable, self._on_process_waited)
        self._read_next_line()

    def cancel(self) -> None:
        """Cancel communication and force-exit a superseded ripgrep process."""

        if self.cancelled or self.completed:
            return
        self.cancelled = True
        self.cancellable.cancel()
        if self.process is not None:
            self.process.force_exit()

    def _read_next_line(self) -> None:
        """Queue the next UTF-8 JSON record from ripgrep's merged output."""

        if self.cancelled or self.stream is None:
            return
        self.stream.read_line_async(
            GLib.PRIORITY_DEFAULT,
            self.cancellable,
            self._on_line_read,
        )

    def _on_line_read(
        self, stream: Gio.DataInputStream, result: Gio.AsyncResult
    ) -> None:
        """Parse one result line and stop ripgrep at the global UI limit."""

        if self.cancelled:
            return
        try:
            line, _length = stream.read_line_finish_utf8(result)
        except GLib.Error as error:
            if not error.matches(Gio.io_error_quark(), Gio.IOErrorEnum.CANCELLED):
                self.diagnostics.append(str(error))
            self.stdout_done = True
            self._finish_if_ready()
            return
        if line is None:
            self.stdout_done = True
            self._finish_if_ready()
            return
        parsed = self._parse_match(line)
        if parsed is not None and self.result_count < self.limit:
            self.result_count += 1
            self.on_result(parsed)
            if self.result_count >= self.limit:
                # 2026-08-20: interrompere alla centesima riga limita memoria e
                # widget creati senza attendere la scansione completa del progetto.
                self.truncated = True
                if self.process is not None:
                    self.process.force_exit()
        elif not line.startswith("{"):
            self.diagnostics.append(line)
        self._read_next_line()

    def _on_process_waited(
        self, process: Gio.Subprocess, result: Gio.AsyncResult
    ) -> None:
        """Record ripgrep termination and wait for the remaining pipe data."""

        if self.cancelled:
            return
        try:
            process.wait_finish(result)
            self.returncode = (
                process.get_exit_status() if process.get_if_exited() else -1
            )
        except GLib.Error as error:
            if not error.matches(Gio.io_error_quark(), Gio.IOErrorEnum.CANCELLED):
                self.diagnostics.append(str(error))
        self.process_done = True
        self._finish_if_ready()

    def _finish_if_ready(self) -> None:
        """Publish completion once both the child and output stream are done."""

        if (
            self.cancelled
            or self.completed
            or not self.stdout_done
            or not self.process_done
        ):
            return
        self.completed = True
        error = ""
        if not self.truncated and self.returncode not in (0, 1):
            error = "\n".join(self.diagnostics).strip()
            if not error:
                error = f"ripgrep exited with code {self.returncode}."
        self.on_complete(
            SearchCompletion(self.returncode, self.truncated, error)
        )

    def _deliver_start_error(self, message: str) -> bool:
        """Report a spawn failure after the owner has retained this command."""

        if not self.cancelled:
            self.completed = True
            self.on_complete(SearchCompletion(-1, error=message))
        return GLib.SOURCE_REMOVE

    @staticmethod
    def _parse_match(line: str) -> SearchResult | None:
        """Convert one ripgrep JSON match event into a safe relative result."""

        try:
            record = json.loads(line)
        except (TypeError, ValueError):
            return None
        if record.get("type") != "match" or not isinstance(record.get("data"), dict):
            return None
        data = record["data"]
        path = SearchCommand._json_text(data.get("path"))
        text = SearchCommand._json_text(data.get("lines"))
        line_number = data.get("line_number")
        if (
            path is None
            or text is None
            or not isinstance(line_number, int)
            or line_number < 1
        ):
            return None
        normalized = path.replace("\\", "/")
        while normalized.startswith("./"):
            normalized = normalized[2:]
        if (
            not normalized
            or normalized.startswith("/")
            or ".." in normalized.split("/")
            or "\0" in normalized
        ):
            return None
        line_text = text.rstrip("\r\n")
        spans: list[tuple[int, int]] = []
        encoded = text.encode("utf-8")
        for submatch in data.get("submatches", []):
            if not isinstance(submatch, dict):
                continue
            start = submatch.get("start")
            end = submatch.get("end")
            if (
                not isinstance(start, int)
                or not isinstance(end, int)
                or not 0 <= start <= end <= len(encoded)
            ):
                continue
            # 2026-08-20: ripgrep usa offset in byte UTF-8 mentre GtkTextBuffer
            # usa caratteri; la conversione evita highlight spostati con Unicode.
            char_start = len(encoded[:start].decode("utf-8", "replace"))
            char_end = len(encoded[:end].decode("utf-8", "replace"))
            spans.append((char_start, min(char_end, len(line_text))))
        display_text, complete_line = SearchCommand._bounded_line_text(
            line_text, spans
        )
        return SearchResult(
            normalized,
            line_number,
            display_text,
            tuple(spans),
            complete_line,
        )

    @staticmethod
    def _bounded_line_text(
        text: str, spans: list[tuple[int, int]], maximum: int = 500
    ) -> tuple[str, bool]:
        """Bound an exceptional source line while keeping its first match visible."""

        if len(text) <= maximum:
            return text, True
        # 2026-08-20: conservare al massimo 500 caratteri attorno al primo
        # match impedisce a poche righe gigantesche di saturare il modello GTK.
        anchor = spans[0][0] if spans else 0
        start = max(0, min(anchor - maximum // 3, len(text) - maximum))
        end = min(len(text), start + maximum)
        prefix = "…" if start else ""
        suffix = "…" if end < len(text) else ""
        return f"{prefix}{text[start:end]}{suffix}", False

    @staticmethod
    def _json_text(value: object) -> str | None:
        """Decode ripgrep's text-or-base64 JSON representation."""

        if not isinstance(value, dict):
            return None
        text = value.get("text")
        if isinstance(text, str):
            return text
        encoded = value.get("bytes")
        if not isinstance(encoded, str):
            return None
        try:
            return base64.b64decode(encoded).decode("utf-8", "replace")
        except (ValueError, UnicodeError):
            return None


class ProjectSearch(Gtk.Box):
    """Display project-wide matches and a non-editable source preview."""

    MIN_QUERY_LENGTH = 4
    DEBOUNCE_MS = 200
    RESULT_LIMIT = 100
    MAX_TEXT_BYTES = 5 * 1024 * 1024
    MAX_SELECTION_LENGTH = 60
    DEFAULT_FILE_COLUMN_FRACTION = 0.30
    COL_TEXT = 0
    COL_LOCATION = 1
    COL_RESULT = 2

    def __init__(
        self,
        on_close: Callable[[], None],
        on_view: Callable[[str], None],
        on_edit_external: Callable[[str], None],
        can_open_meld: Callable[[str], bool],
        on_open_meld: Callable[[str], None],
    ) -> None:
        """Build the three-row search surface and contextual action dispatchers."""

        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.on_close = on_close
        self.on_view = on_view
        self.on_edit_external = on_edit_external
        self.can_open_meld = can_open_meld
        self.on_open_meld = on_open_meld
        self.project_name: str | None = None
        self.root: str | None = None
        self.debounce_id: int | None = None
        self.command: SearchCommand | None = None
        self.request_serial = 0
        self.preview_cancellable: Gio.Cancellable | None = None
        self.preview_result: SearchResult | None = None
        self.context_result: SearchResult | None = None
        self.selection_request_serial = 0
        self.result_columns_width: int | None = None
        self.set_no_show_all(True)
        self.set_hexpand(True)
        self.set_vexpand(True)
        self.get_style_context().add_class("project-search")

        self._build_search_bar()
        self._build_results_and_preview()

    def _build_search_bar(self) -> None:
        """Create the query, live status and close controls in the first row."""

        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        toolbar.get_style_context().add_class("project-search-toolbar")
        self.entry = Gtk.SearchEntry()
        self.entry.set_placeholder_text("Search project contents…")
        self.entry.connect("search-changed", self._on_query_changed)
        toolbar.pack_start(self.entry, True, True, 0)
        self.status_label = Gtk.Label(label="Enter at least 4 characters")
        self.status_label.set_xalign(1)
        self.status_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.status_label.set_max_width_chars(60)
        self.status_label.get_style_context().add_class("project-search-status")
        toolbar.pack_start(self.status_label, False, False, 0)
        close_button = Gtk.Button()
        close_button.set_relief(Gtk.ReliefStyle.NONE)
        close_button.set_image(
            Gtk.Image.new_from_icon_name("window-close-symbolic", Gtk.IconSize.BUTTON)
        )
        close_button.set_tooltip_text("Close search (Esc)")
        close_button.get_accessible().set_name("Close search")
        close_button.connect("clicked", self._on_close_clicked)
        toolbar.pack_end(close_button, False, False, 0)
        self.pack_start(toolbar, False, False, 0)

    def _build_results_and_preview(self) -> None:
        """Create the result list and syntax-aware read-only preview rows."""

        self.store = Gtk.ListStore(str, str, object)
        self.tree = Gtk.TreeView(model=self.store)
        self.tree.set_enable_search(False)
        self.tree.set_headers_visible(True)
        self.tree.get_selection().set_mode(Gtk.SelectionMode.SINGLE)
        self.tree.get_selection().connect("changed", self._on_selection_changed)
        self.tree.connect("button-press-event", self._on_tree_button_press)
        self.tree.connect("popup-menu", self._on_tree_popup_menu)
        self.tree.connect("key-press-event", self._on_tree_key_press)
        self.tree.connect("size-allocate", self._on_results_size_allocate)
        for title, column in (
            ("Match", self.COL_TEXT),
            ("File", self.COL_LOCATION),
        ):
            renderer = Gtk.CellRendererText()
            renderer.set_property("ellipsize", Pango.EllipsizeMode.END)
            if column == self.COL_LOCATION:
                renderer.set_property("xalign", 1.0)
            view_column = Gtk.TreeViewColumn(title, renderer, text=column)
            view_column.set_sizing(Gtk.TreeViewColumnSizing.FIXED)
            view_column.set_expand(False)
            view_column.set_resizable(True)
            if column == self.COL_LOCATION:
                view_column.set_alignment(1.0)
                self.file_column = view_column
            else:
                self.match_column = view_column
            self.tree.append_column(view_column)
        results_scroller = Gtk.ScrolledWindow()
        results_scroller.set_policy(
            Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC
        )
        results_scroller.add(self.tree)

        preview_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.preview_title = Gtk.Label(label="Select a result to preview it")
        self.preview_title.set_xalign(0)
        self.preview_title.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        self.preview_title.get_style_context().add_class("project-search-preview-title")
        preview_box.pack_start(self.preview_title, False, False, 0)
        self.preview_buffer = GtkSource.Buffer()
        self.preview_line_tag = self.preview_buffer.create_tag("search-result-line")
        self.preview_match_tag = self.preview_buffer.create_tag("search-result-match")
        self._configure_preview_tags()
        self.preview_view = GtkSource.View.new_with_buffer(self.preview_buffer)
        self.preview_view.set_editable(False)
        self.preview_view.set_cursor_visible(False)
        self.preview_view.set_monospace(True)
        self.preview_view.set_show_line_numbers(True)
        self.preview_view.set_highlight_current_line(False)
        self.preview_view.set_wrap_mode(Gtk.WrapMode.NONE)
        preview_scroller = Gtk.ScrolledWindow()
        preview_scroller.set_policy(
            Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC
        )
        preview_scroller.add(self.preview_view)
        preview_box.pack_start(preview_scroller, True, True, 0)

        split = Gtk.Paned.new(Gtk.Orientation.VERTICAL)
        split.pack1(results_scroller, resize=True, shrink=False)
        split.pack2(preview_box, resize=True, shrink=False)
        split.set_position(320)
        self.pack_start(split, True, True, 0)

    def _on_results_size_allocate(
        self, _tree: Gtk.TreeView, allocation: Gdk.Rectangle
    ) -> None:
        """Size result columns initially at 70/30 and preserve manual ratios."""

        available = int(allocation.width)
        if available < 2 or available == self.result_columns_width:
            return
        file_fraction = self.DEFAULT_FILE_COLUMN_FRACTION
        if self.result_columns_width is not None:
            match_width = self.match_column.get_fixed_width()
            file_width = self.file_column.get_fixed_width()
            previous_total = match_width + file_width
            if previous_total > 0:
                file_fraction = file_width / previous_total
        # 2026-08-21: le larghezze FIXED impediscono ai risultati brevi di
        # richiudere File; al resize si conserva l'eventuale divisore manuale.
        file_width = max(1, min(available - 1, round(available * file_fraction)))
        self.file_column.set_fixed_width(file_width)
        self.match_column.set_fixed_width(available - file_width)
        self.result_columns_width = available

    def _configure_preview_tags(self) -> None:
        """Derive result emphasis from the active GTK selection colors."""

        context = self.get_style_context()
        found_bg, background = context.lookup_color("theme_selected_bg_color")
        found_fg, foreground = context.lookup_color("theme_selected_fg_color")
        if found_bg:
            line_background = Gdk.RGBA()
            line_background.red = background.red
            line_background.green = background.green
            line_background.blue = background.blue
            line_background.alpha = 0.18
            self.preview_line_tag.set_property(
                "paragraph-background-rgba", line_background
            )
            self.preview_match_tag.set_property("background-rgba", background)
        if found_fg:
            self.preview_match_tag.set_property("foreground-rgba", foreground)

    def open(self, project_name: str, root: str) -> None:
        """Reset, focus and asynchronously seed search from PRIMARY selection."""

        self.cancel()
        self.selection_request_serial += 1
        selection_request_serial = self.selection_request_serial
        self.project_name = project_name
        self.root = str(Path(root).resolve())
        self.entry.set_text("")
        self.store.clear()
        self._set_preview_message("Select a result to preview it")
        self.status_label.set_text("Enter at least 4 characters")
        self.set_no_show_all(False)
        self.show_all()
        self.set_no_show_all(True)
        self.entry.grab_focus()
        # 2026-08-20: PRIMARY permette a Ctrl+Shift+F di riusare la selezione
        # presente in qualunque applicazione Linux senza bloccare il main loop.
        clipboard = Gtk.Clipboard.get(Gdk.SELECTION_PRIMARY)
        clipboard.request_text(
            self._on_primary_selection_received,
            selection_request_serial,
        )

    def _on_primary_selection_received(
        self,
        _clipboard: Gtk.Clipboard,
        text: str | None,
        selection_request_serial: int,
    ) -> None:
        """Seed only a current blank search with a concise single-line selection."""

        if (
            selection_request_serial != self.selection_request_serial
            or self.root is None
            or self.entry.get_text()
        ):
            return
        selection = (text or "").strip()
        if (
            not selection
            or "\n" in selection
            or "\r" in selection
            or len(selection) > self.MAX_SELECTION_LENGTH
        ):
            return
        self.entry.set_text(selection)
        self.entry.set_position(-1)

    def dismiss(self) -> None:
        """Cancel all search work and hide the ephemeral overlay state."""

        self.cancel()
        self.selection_request_serial += 1
        self.project_name = None
        self.root = None
        self.context_result = None
        self.hide()

    def cancel(self) -> None:
        """Cancel pending debounce, ripgrep and preview reads."""

        self.request_serial += 1
        if self.debounce_id is not None:
            GLib.source_remove(self.debounce_id)
            self.debounce_id = None
        if self.command is not None:
            self.command.cancel()
            self.command = None
        if self.preview_cancellable is not None:
            self.preview_cancellable.cancel()
            self.preview_cancellable = None

    def focus_query(self) -> None:
        """Return keyboard focus to the live query field."""

        self.entry.grab_focus()
        self.entry.select_region(0, -1)

    def _on_query_changed(self, entry: Gtk.SearchEntry) -> None:
        """Debounce a sufficiently long query and invalidate older results."""

        self.cancel()
        self.store.clear()
        self._set_preview_message("Select a result to preview it")
        query = entry.get_text()
        if len(query) < self.MIN_QUERY_LENGTH or self.root is None:
            self.status_label.set_text("Enter at least 4 characters")
            return
        self.status_label.set_text("Waiting…")
        request_serial = self.request_serial
        self.debounce_id = GLib.timeout_add(
            self.DEBOUNCE_MS,
            self._start_search,
            request_serial,
            query,
        )

    def _start_search(self, request_serial: int, query: str) -> bool:
        """Start ripgrep only if the debounced query is still current."""

        self.debounce_id = None
        if request_serial != self.request_serial or self.root is None:
            return GLib.SOURCE_REMOVE
        self.status_label.set_text("Searching…")
        excluded = {
            "node_modules",
            "vendor",
            "dist",
            "build",
            ".venv",
            "__pycache__",
            ".cache",
            ".git",
            ".hg",
            ".svn",
        }
        self.command = SearchCommand(
            self.root,
            query,
            excluded,
            self.RESULT_LIMIT,
            self._on_search_result,
            self._on_search_complete,
        )
        return GLib.SOURCE_REMOVE

    def _on_search_result(self, result: SearchResult) -> None:
        """Append one safe match and preview the first result automatically."""

        row = self.store.append(
            (result.text.lstrip(), f"{result.path}:{result.line}", result)
        )
        if len(self.store) == 1:
            self.tree.get_selection().select_iter(row)
            self.tree.set_cursor(self.store.get_path(row))
        self.status_label.set_text(f"{len(self.store)} results…")

    def _on_search_complete(self, completion: SearchCompletion) -> None:
        """Expose a completed, truncated or failed search without a dialog."""

        self.command = None
        count = len(self.store)
        if completion.error:
            self.status_label.set_text(f"Search failed: {completion.error}")
        elif completion.truncated:
            self.status_label.set_text(
                f"First {self.RESULT_LIMIT} results; refine the query"
            )
        elif count:
            self.status_label.set_text(f"{count} results")
        else:
            self.status_label.set_text("No results")

    def _focused_result(self) -> SearchResult | None:
        """Return the single result currently targeted by the list cursor."""

        model, tree_iter = self.tree.get_selection().get_selected()
        if tree_iter is None:
            return None
        result = model.get_value(tree_iter, self.COL_RESULT)
        return result if isinstance(result, SearchResult) else None

    def _on_selection_changed(self, _selection: Gtk.TreeSelection) -> None:
        """Load and center the file context for the newly selected match."""

        result = self._focused_result()
        if result is None:
            self._set_preview_message("Select a result to preview it")
            return
        self._load_preview(result)

    def _safe_result_path(self, relative_path: str) -> str | None:
        """Resolve one result without permitting traversal or external links."""

        if self.root is None or os.path.isabs(relative_path):
            return None
        root = Path(self.root).resolve()
        candidate = (root / relative_path).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            return None
        return str(candidate)

    def _load_preview(self, result: SearchResult) -> None:
        """Read the selected current file asynchronously through Gio."""

        if self.preview_cancellable is not None:
            self.preview_cancellable.cancel()
        self.request_serial += 1
        request_serial = self.request_serial
        self.preview_result = result
        path = self._safe_result_path(result.path)
        if path is None:
            self._set_preview_message("Preview rejected: the path leaves the project")
            return
        self.preview_title.set_text(f"{result.path}:{result.line}")
        self.preview_title.set_tooltip_text(f"{result.path}:{result.line}")
        self._set_preview_text("Loading preview…")
        self.preview_cancellable = Gio.Cancellable()
        Gio.File.new_for_path(path).load_contents_async(
            self.preview_cancellable,
            self._on_preview_loaded,
            (request_serial, result),
        )

    def _on_preview_loaded(
        self,
        source: Gio.File,
        result: Gio.AsyncResult,
        request: tuple[int, SearchResult],
    ) -> None:
        """Validate and render one completed result preview."""

        request_serial, search_result = request
        if (
            request_serial != self.request_serial
            or search_result != self.preview_result
        ):
            return
        self.preview_cancellable = None
        try:
            _success, contents, _etag = source.load_contents_finish(result)
        except GLib.Error as error:
            if not error.matches(Gio.io_error_quark(), Gio.IOErrorEnum.CANCELLED):
                self._set_preview_text(f"Unable to read the file:\n{error}")
            return
        data = bytes(contents)
        if len(data) > self.MAX_TEXT_BYTES:
            self._set_preview_text("File over 5 MiB: preview is unavailable")
            return
        if b"\0" in data:
            self._set_preview_text("Binary file: preview is unavailable")
            return
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            self._set_preview_text("The file is not UTF-8: preview is unavailable")
            return
        self._render_preview(text, search_result, request_serial)

    def _render_preview(
        self, text: str, result: SearchResult, request_serial: int
    ) -> None:
        """Install source text, emphasize the match and schedule centering."""

        language = GtkSource.LanguageManager.get_default().guess_language(
            result.path, None
        )
        self.preview_buffer.set_language(language)
        self.preview_buffer.set_highlight_syntax(language is not None)
        settings = Gtk.Settings.get_default()
        theme = str(settings.get_property("gtk-theme-name") if settings else "")
        scheme_name = "oblivion" if "dark" in theme.lower() else "classic"
        scheme = GtkSource.StyleSchemeManager.get_default().get_scheme(scheme_name)
        if scheme is not None:
            self.preview_buffer.set_style_scheme(scheme)
        self.preview_buffer.set_text(text)
        line_index = min(
            result.line - 1, max(0, self.preview_buffer.get_line_count() - 1)
        )
        start = self.preview_buffer.get_iter_at_line(line_index)
        end = start.copy()
        end.forward_to_line_end()
        self.preview_buffer.apply_tag(self.preview_line_tag, start, end)
        actual_line = self.preview_buffer.get_text(start, end, True)
        if not result.complete_line or actual_line == result.text:
            for span_start, span_end in result.spans:
                match_start = self.preview_buffer.get_iter_at_line_offset(
                    line_index, min(span_start, len(actual_line))
                )
                match_end = self.preview_buffer.get_iter_at_line_offset(
                    line_index, min(span_end, len(actual_line))
                )
                self.preview_buffer.apply_tag(
                    self.preview_match_tag, match_start, match_end
                )
        elif result.complete_line:
            self.preview_title.set_text(
                f"{result.path}:{result.line} — file changed since search"
            )
        GLib.idle_add(self._center_preview_line, request_serial, line_index)

    def _center_preview_line(self, request_serial: int, line_index: int) -> bool:
        """Center the selected source line after GTK has allocated the view."""

        if request_serial != self.request_serial:
            return GLib.SOURCE_REMOVE
        target = self.preview_buffer.get_iter_at_line(line_index)
        self.preview_view.scroll_to_iter(target, 0.1, True, 0.0, 0.5)
        return GLib.SOURCE_REMOVE

    def _set_preview_message(self, message: str) -> None:
        """Reset preview title and content to a read-only informational state."""

        self.preview_title.set_text(message)
        self.preview_title.set_tooltip_text(message)
        self._set_preview_text("")

    def _set_preview_text(self, text: str) -> None:
        """Show plain preview text without language-specific highlighting."""

        self.preview_buffer.set_language(None)
        self.preview_buffer.set_highlight_syntax(False)
        self.preview_buffer.set_text(text)

    def _on_tree_button_press(
        self, tree: Gtk.TreeView, event: Gdk.EventButton
    ) -> bool:
        """Select the right-clicked result before opening contextual actions."""

        if event.button != 3:
            return False
        hit = tree.get_path_at_pos(int(event.x), int(event.y))
        if hit is None:
            return False
        tree.set_cursor(hit[0])
        self.context_result = self._focused_result()
        return self._show_result_menu(event)

    def _on_tree_popup_menu(self, _tree: Gtk.TreeView) -> bool:
        """Open result actions from Menu or Shift+F10."""

        self.context_result = self._focused_result()
        return self._show_result_menu(None)

    def _show_result_menu(self, event: Gdk.EventButton | None) -> bool:
        """Present View, external editor and path-limited Meld actions."""

        if self.context_result is None:
            return False
        menu = Gtk.Menu()
        for label, icon, keyval, callback in (
            ("View", "document-open", Gdk.KEY_v, self._on_context_view),
            (
                "Edit in external editor",
                "accessories-text-editor",
                Gdk.KEY_e,
                self._on_context_edit,
            ),
            ("Open in Meld", "document-open", Gdk.KEY_d, self._on_context_meld),
        ):
            item = self._menu_item(label, icon, keyval)
            item.connect("activate", callback)
            if keyval == Gdk.KEY_d:
                item.set_sensitive(self.can_open_meld(self.context_result.path))
            menu.append(item)
        menu.show_all()
        if event is not None:
            menu.popup_at_pointer(event)
        else:
            menu.popup_at_widget(
                self.tree, Gdk.Gravity.CENTER, Gdk.Gravity.CENTER, None
            )
        return True

    @staticmethod
    def _menu_item(label: str, icon_name: str, keyval: int) -> Gtk.MenuItem:
        """Create a contextual result action with icon and single-key hint."""

        item = Gtk.MenuItem()
        content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        icon = Gtk.Image.new_from_icon_name(icon_name, Gtk.IconSize.MENU)
        text = Gtk.AccelLabel(label=label)
        text.set_xalign(0)
        text.set_accel_widget(item)
        text.set_accel(keyval, Gdk.ModifierType(0))
        content.pack_start(icon, False, False, 0)
        content.pack_start(text, True, True, 0)
        item.add(content)
        return item

    def _on_tree_key_press(
        self, _tree: Gtk.TreeView, event: Gdk.EventKey
    ) -> bool:
        """Dispatch V, E and D only for the explicitly focused result."""

        if event.state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.MOD1_MASK):
            return False
        result = self._focused_result()
        if result is None:
            return False
        keyval = Gdk.keyval_to_lower(event.keyval)
        if keyval == Gdk.KEY_v:
            self.on_view(result.path)
            return True
        if keyval == Gdk.KEY_e:
            self.on_edit_external(result.path)
            return True
        if keyval == Gdk.KEY_d:
            if self.can_open_meld(result.path):
                self.on_open_meld(result.path)
            return True
        return False

    def _on_context_view(self, _item: Gtk.MenuItem) -> None:
        """Open the contextual result through the desktop association."""

        if self.context_result is not None:
            self.on_view(self.context_result.path)

    def _on_context_edit(self, _item: Gtk.MenuItem) -> None:
        """Open the contextual result with the configured external editor."""

        if self.context_result is not None:
            self.on_edit_external(self.context_result.path)

    def _on_context_meld(self, _item: Gtk.MenuItem) -> None:
        """Open Meld only when the contextual result owns a tracked patch."""

        if (
            self.context_result is not None
            and self.can_open_meld(self.context_result.path)
        ):
            self.on_open_meld(self.context_result.path)

    def _on_close_clicked(self, _button: Gtk.Button) -> None:
        """Forward explicit search dismissal to the owning window."""

        self.on_close()
