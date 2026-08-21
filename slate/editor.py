"""Persistent multi-project GtkSourceView editor for the central workspace."""

from __future__ import annotations

from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Callable

import gi

gi.require_version("Gdk", "3.0")
gi.require_version("Gio", "2.0")
gi.require_version("Gtk", "3.0")
gi.require_version("GtkSource", "4")
from gi.repository import Gdk, Gio, GLib, GObject, Gtk, GtkSource, Pango  # noqa: E402


EditorRef = tuple[str, str]


@dataclass
class EditorEntry:
    """Retain one persistent editor identity and its optional loaded widget."""

    project_name: str
    root: str
    relative_path: str
    document: EditorDocument | None = None


class EditorDocument(Gtk.Box):
    """Edit one safe UTF-8 project file and monitor external replacements."""

    MAX_TEXT_BYTES = 5 * 1024 * 1024
    MONITOR_DEBOUNCE_MS = 180
    RESPONSE_DISK = 1
    RESPONSE_MINE = 2
    RESPONSE_RECREATE = 3
    RESPONSE_DISCARD = 4

    def __init__(
        self,
        project_name: str,
        root: str,
        relative_path: str,
        font_size: int,
        on_state: Callable[["EditorDocument"], None],
        on_removed: Callable[["EditorDocument", str], None],
    ) -> None:
        """Build one editor and begin loading its working-copy file."""

        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.project_name = project_name
        self.root = str(Path(root).resolve())
        self.relative_path = relative_path
        self.on_state = on_state
        self.on_removed = on_removed
        self.path = self._safe_path()
        self.file = Gio.File.new_for_path(self.path) if self.path else None
        self.etag: str | None = None
        self.loading = False
        self.saving = False
        self.force_next_save = False
        self.external_conflict = False
        self.deleted_conflict = False
        self.attention = False
        self.monitor: Gio.FileMonitor | None = None
        self.monitor_source_id: int | None = None
        self.load_cancellable: Gio.Cancellable | None = None
        self.save_cancellable: Gio.Cancellable | None = None
        self.save_callbacks: list[Callable[[bool], None]] = []
        self.mute_monitor_until = 0
        self.initial_load_id: int | None = None

        self._build_info_bar()
        self._build_toolbar()
        self._build_source_view(font_size)
        self._build_search_bar()
        # 2026-08-16: l'idle lascia al workspace il tempo di registrare il child
        # prima che un path non valido possa richiederne la rimozione.
        self.initial_load_id = GLib.idle_add(self._start_initial_load)

    @property
    def reference(self) -> EditorRef:
        """Return the stable project-relative identity used by the workspace."""

        return self.project_name, self.relative_path

    @property
    def dirty(self) -> bool:
        """Return whether the source buffer contains unsaved edits."""

        return self.buffer.get_modified()

    def _safe_path(self) -> str | None:
        """Resolve the configured relative path without escaping its project."""

        if (
            not self.relative_path
            or Path(self.relative_path).is_absolute()
            or ".." in self.relative_path.replace("\\", "/").split("/")
        ):
            return None
        root = Path(self.root)
        candidate = root / self.relative_path
        try:
            candidate.resolve().relative_to(root)
        except (OSError, ValueError):
            return None
        return str(candidate)

    def _start_initial_load(self) -> bool:
        """Begin first loading after the constructed editor belongs to its stack."""

        self.initial_load_id = None
        self._begin_load("initial")
        return GLib.SOURCE_REMOVE

    def _build_info_bar(self) -> None:
        """Create the non-modal area used for reload and conflict messages."""

        self.info_bar = Gtk.InfoBar()
        self.info_bar.set_no_show_all(True)
        self.info_bar.set_revealed(False)
        self.info_bar.set_show_close_button(True)
        self.info_bar.connect("response", self._on_info_response)
        self.info_label = Gtk.Label()
        self.info_label.set_xalign(0)
        self.info_label.set_line_wrap(True)
        self.info_bar.get_content_area().add(self.info_label)
        self.pack_start(self.info_bar, False, False, 0)

    def _build_toolbar(self) -> None:
        """Create compact native controls for common editor operations."""

        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        toolbar.get_style_context().add_class("editor-toolbar")
        self.path_label = Gtk.Label(label=self.relative_path)
        self.path_label.set_xalign(0)
        self.path_label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        self.path_label.set_tooltip_text(f"{self.project_name} / {self.relative_path}")
        toolbar.pack_start(self.path_label, True, True, 0)
        # 2026-08-16: Salva rappresenta un'azione applicabile soltanto a un
        # buffer dirty; conservarlo visibile ma disabilitato evita falsi stati.
        self.save_button = self._tool_button(
            "document-save", "Save (Ctrl+S)", self._on_save_clicked
        )
        self.save_button.set_sensitive(False)
        toolbar.pack_start(self.save_button, False, False, 0)
        self.undo_button = self._tool_button(
            "edit-undo", "Undo (Ctrl+Z)", self._on_undo_clicked
        )
        self.redo_button = self._tool_button(
            "edit-redo", "Redo (Ctrl+Shift+Z / Ctrl+Y)", self._on_redo_clicked
        )
        self.undo_button.set_sensitive(False)
        self.redo_button.set_sensitive(False)
        toolbar.pack_start(self.undo_button, False, False, 0)
        toolbar.pack_start(self.redo_button, False, False, 0)
        for icon, tooltip, callback in (
            ("edit-find", "Find (Ctrl+F)", self._on_find_clicked),
            ("go-jump", "Go to line (Ctrl+G)", self._on_goto_clicked),
        ):
            toolbar.pack_start(self._tool_button(icon, tooltip, callback), False, False, 0)
        self.pack_start(toolbar, False, False, 0)

    @staticmethod
    def _tool_button(
        icon_name: str,
        tooltip: str,
        callback: Callable[[Gtk.Button], None],
    ) -> Gtk.Button:
        """Create one accessible icon-only editor toolbar button."""

        button = Gtk.Button()
        button.set_relief(Gtk.ReliefStyle.NONE)
        button.set_image(Gtk.Image.new_from_icon_name(icon_name, Gtk.IconSize.BUTTON))
        button.set_tooltip_text(tooltip)
        button.get_accessible().set_name(tooltip)
        button.connect("clicked", callback)
        return button

    def _build_source_view(self, font_size: int) -> None:
        """Create the editable GtkSource buffer and its scrolling view."""

        self.buffer = GtkSource.Buffer()
        self.buffer.set_highlight_syntax(True)
        self.buffer.set_max_undo_levels(500)
        self.buffer.connect("modified-changed", self._on_modified_changed)
        # 2026-08-16: GtkSource aggiorna l'undo manager dopo il segnale changed;
        # le proprietà dedicate notificano invece lo stato definitivo utilizzabile.
        self.buffer.connect("notify::can-undo", self._on_history_capability_changed)
        self.buffer.connect("notify::can-redo", self._on_history_capability_changed)
        self.view = GtkSource.View.new_with_buffer(self.buffer)
        self.view.set_monospace(True)
        self.view.set_show_line_numbers(True)
        self.view.set_highlight_current_line(True)
        self.view.set_auto_indent(True)
        self.view.set_tab_width(4)
        self.view.set_insert_spaces_instead_of_tabs(False)
        self.view.set_wrap_mode(Gtk.WrapMode.NONE)
        self.font_provider = Gtk.CssProvider()
        self.view.get_style_context().add_provider(
            self.font_provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
        self.set_font_size(font_size)
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroller.add(self.view)
        self.pack_start(scroller, True, True, 0)

    def _build_search_bar(self) -> None:
        """Create the built-in GtkSource search controls below the editor."""

        self.search_settings = GtkSource.SearchSettings()
        self.search_settings.set_wrap_around(True)
        self.search_context = GtkSource.SearchContext.new(
            self.buffer, self.search_settings
        )
        self.search_revealer = Gtk.Revealer()
        search_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        search_box.get_style_context().add_class("editor-search")
        self.search_entry = Gtk.SearchEntry()
        self.search_entry.set_placeholder_text("Find…")
        self.search_entry.connect("search-changed", self._on_search_changed)
        self.search_entry.connect("key-press-event", self._on_search_key_press)
        search_box.pack_start(self.search_entry, True, True, 0)
        for icon, tooltip, callback in (
            ("go-up", "Previous result (Shift+Enter)", self._on_search_previous_clicked),
            ("go-down", "Next result (Enter)", self._on_search_next_clicked),
            ("window-close-symbolic", "Close search (Esc)", self._on_search_close_clicked),
        ):
            search_box.pack_start(self._tool_button(icon, tooltip, callback), False, False, 0)
        self.search_revealer.add(search_box)
        self.pack_end(self.search_revealer, False, False, 0)

    def set_font_size(self, points: int) -> None:
        """Apply a monospace point size immediately to the source view."""

        # 2026-08-16: un provider locale evita override_font deprecato e non
        # altera terminale, preview o altri GtkTextView dell'applicazione.
        self.font_provider.load_from_data(
            f"textview {{ font-family: monospace; font-size: {points}pt; }}".encode()
        )
        self.view.queue_resize()
        self.view.queue_draw()

    def _begin_load(self, reason: str) -> None:
        """Load current disk contents asynchronously for opening or reloading."""

        if self.file is None:
            self.on_removed(self, f"Invalid editor path: {self.relative_path}")
            return
        if self.load_cancellable is not None:
            self.load_cancellable.cancel()
        self.loading = True
        self.save_button.set_sensitive(False)
        self.load_cancellable = Gio.Cancellable()
        self.file.load_contents_async(
            self.load_cancellable,
            self._on_loaded,
            reason,
        )

    def _on_loaded(
        self, source: Gio.File, result: Gio.AsyncResult, reason: str
    ) -> None:
        """Validate and install asynchronously loaded UTF-8 file contents."""

        self.load_cancellable = None
        try:
            _success, contents, etag = source.load_contents_finish(result)
        except GLib.Error as error:
            self.loading = False
            if error.matches(Gio.io_error_quark(), Gio.IOErrorEnum.CANCELLED):
                return
            if error.matches(Gio.io_error_quark(), Gio.IOErrorEnum.NOT_FOUND):
                self.on_removed(self, f"File no longer exists: {self.relative_path}")
                return
            self._show_notice(f"Unable to read the file: {error}", Gtk.MessageType.ERROR)
            return
        data = bytes(contents)
        if len(data) > self.MAX_TEXT_BYTES:
            self.loading = False
            self.view.set_editable(False)
            self._show_notice("File over 5 MiB: internal editing is unavailable.", Gtk.MessageType.ERROR)
            return
        if b"\0" in data:
            self.loading = False
            self.view.set_editable(False)
            self._show_notice("Binary file: internal editing is unavailable.", Gtk.MessageType.ERROR)
            return
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            self.loading = False
            self.view.set_editable(False)
            self._show_notice("The file is not UTF-8: use gVim to edit it.", Gtk.MessageType.ERROR)
            return
        cursor_line = self.buffer.get_iter_at_mark(self.buffer.get_insert()).get_line()
        # 2026-08-16: il contenuto letto dal disco è lo stato iniziale, non una
        # modifica dell'utente; escluderlo impedisce un falso Undo dopo l'apertura.
        self.buffer.begin_not_undoable_action()
        self.buffer.set_text(text)
        self.buffer.end_not_undoable_action()
        self._sync_history_actions()
        self._configure_language()
        target_line = min(cursor_line, max(0, self.buffer.get_line_count() - 1))
        cursor = self.buffer.get_iter_at_line(target_line)
        self.buffer.place_cursor(cursor)
        self.buffer.set_modified(False)
        self.etag = etag
        self.loading = False
        self.external_conflict = False
        self.deleted_conflict = False
        self.force_next_save = False
        self.view.set_editable(True)
        self._ensure_monitor()
        if reason == "external":
            # Un buffer pulito può seguire il disco senza richiedere attenzione:
            # soltanto le modifiche locali rendono necessaria una scelta.
            self.attention = False
            self._hide_notice()
        else:
            if reason == "conflict-disk":
                self.attention = False
            self._hide_notice()
        self.on_state(self)

    def _configure_language(self) -> None:
        """Select syntax and theme from the file name and current GTK theme."""

        language = GtkSource.LanguageManager.get_default().guess_language(
            self.relative_path, None
        )
        self.buffer.set_language(language)
        self.buffer.set_highlight_syntax(language is not None)
        settings = Gtk.Settings.get_default()
        theme = str(settings.get_property("gtk-theme-name") if settings else "")
        scheme_name = "oblivion" if "dark" in theme.lower() else "classic"
        scheme = GtkSource.StyleSchemeManager.get_default().get_scheme(scheme_name)
        if scheme is not None:
            self.buffer.set_style_scheme(scheme)

    def _ensure_monitor(self) -> None:
        """Create and retain exactly one Gio monitor for this open file."""

        if self.monitor is not None or self.file is None:
            return
        try:
            self.monitor = self.file.monitor_file(
                Gio.FileMonitorFlags.WATCH_MOVES, None
            )
            self.monitor.connect("changed", self._on_file_changed)
        except GLib.Error as error:
            self._show_notice(f"File monitor unavailable: {error}", Gtk.MessageType.WARNING)

    def relocate(self, relative_path: str) -> None:
        """Retarget this buffer after its file or an ancestor was renamed."""

        if self.monitor_source_id is not None:
            GLib.source_remove(self.monitor_source_id)
            self.monitor_source_id = None
        if self.monitor is not None:
            self.monitor.cancel()
            self.monitor = None
        self.relative_path = relative_path
        self.path = self._safe_path()
        self.file = Gio.File.new_for_path(self.path) if self.path else None
        self.path_label.set_text(relative_path)
        self.path_label.set_tooltip_text(f"{self.project_name} / {relative_path}")
        self._configure_language()
        # 2026-08-16: il buffer aperto rappresenta lo stesso file appena mosso;
        # si cambia monitor senza ricaricare e senza perdere modifiche non salvate.
        self.mute_monitor_until = GLib.get_monotonic_time() // 1000 + 500
        self._ensure_monitor()
        self.on_state(self)

    def _on_file_changed(
        self,
        _monitor: Gio.FileMonitor,
        _file: Gio.File,
        _other: Gio.File | None,
        event_type: Gio.FileMonitorEvent,
    ) -> None:
        """Debounce meaningful external writes, moves and deletions."""

        now = GLib.get_monotonic_time() // 1000
        if self.saving or now < self.mute_monitor_until:
            return
        if event_type not in {
            Gio.FileMonitorEvent.CHANGES_DONE_HINT,
            Gio.FileMonitorEvent.CREATED,
            Gio.FileMonitorEvent.DELETED,
            Gio.FileMonitorEvent.MOVED_IN,
            Gio.FileMonitorEvent.MOVED_OUT,
            Gio.FileMonitorEvent.RENAMED,
        }:
            return
        if self.monitor_source_id is not None:
            GLib.source_remove(self.monitor_source_id)
        self.monitor_source_id = GLib.timeout_add(
            self.MONITOR_DEBOUNCE_MS, self._handle_external_change
        )

    def _handle_external_change(self) -> bool:
        """Reload clean buffers or require a choice for dirty/deleted buffers."""

        self.monitor_source_id = None
        exists = bool(self.path and Path(self.path).exists())
        if not exists:
            if self.dirty:
                self.external_conflict = True
                self.deleted_conflict = True
                self.attention = True
                self._show_conflict(
                    "The file was deleted on disk.",
                    (("Recreate from my buffer", self.RESPONSE_RECREATE), ("Discard", self.RESPONSE_DISCARD)),
                )
                self.on_state(self)
            else:
                self.on_removed(self, f"File deleted: {self.relative_path}")
            return GLib.SOURCE_REMOVE
        if self.dirty:
            self.external_conflict = True
            self.deleted_conflict = False
            self.attention = True
            self._show_conflict(
                "The file changed on disk while it contains local changes.",
                (("Use version on disk", self.RESPONSE_DISK), ("Keep my version", self.RESPONSE_MINE)),
            )
            self.on_state(self)
        else:
            self._begin_load("external")
        return GLib.SOURCE_REMOVE

    def _show_conflict(
        self, message: str, actions: tuple[tuple[str, int], ...]
    ) -> None:
        """Present explicit non-modal choices for an external-file conflict."""

        action_area = self.info_bar.get_action_area()
        for child in tuple(action_area.get_children()):
            action_area.remove(child)
        for label, response in actions:
            self.info_bar.add_button(label, response)
        self._show_notice(message, Gtk.MessageType.WARNING)

    def _show_notice(self, message: str, message_type: Gtk.MessageType) -> None:
        """Show a non-modal editor message without changing the central view."""

        self.info_label.set_text(message)
        self.info_bar.set_message_type(message_type)
        self.info_bar.set_no_show_all(False)
        self.info_bar.set_revealed(True)
        self.info_bar.show_all()
        self.info_bar.set_no_show_all(True)

    def _hide_notice(self) -> None:
        """Hide the current editor notice and remove obsolete action buttons."""

        self.info_label.set_text("")
        action_area = self.info_bar.get_action_area()
        for child in tuple(action_area.get_children()):
            action_area.remove(child)
        # 2026-08-16: GtkInfoBar mantiene uno stato revealed distinto da
        # visible; azzerarli entrambi fa collassare anche l'altezza allocata.
        self.info_bar.set_revealed(False)
        self.info_bar.hide()

    def _on_info_response(
        self, _bar: Gtk.InfoBar, response: Gtk.ResponseType
    ) -> None:
        """Resolve conflict choices or dismiss an informational notice."""

        if response == self.RESPONSE_DISK:
            # 2026-08-16: la scelta esplicita conclude il conflitto; il reload
            # risolutivo non deve lasciare un secondo pannello informativo.
            self._begin_load("conflict-disk")
            return
        if response in {self.RESPONSE_MINE, self.RESPONSE_RECREATE}:
            self.external_conflict = False
            self.deleted_conflict = False
            self.force_next_save = True
            self.attention = False
            self._hide_notice()
            self.on_state(self)
            return
        if response == self.RESPONSE_DISCARD:
            self.on_removed(self, f"Tab closed: {self.relative_path}")
            return
        if not self.external_conflict:
            self._hide_notice()

    def save(self, callback: Callable[[bool], None] | None = None) -> None:
        """Atomically save the UTF-8 buffer, respecting external etag changes."""

        if callback is not None:
            self.save_callbacks.append(callback)
        if self.saving:
            return
        if not self.dirty:
            self._finish_save(True)
            return
        if self.file is None or not self.view.get_editable():
            self._finish_save(False)
            return
        if self.external_conflict and not self.force_next_save:
            self._show_notice(
                "Resolve the conflict with the version on disk first.",
                Gtk.MessageType.WARNING,
            )
            self._finish_save(False)
            return
        text = self.buffer.get_text(
            self.buffer.get_start_iter(), self.buffer.get_end_iter(), True
        )
        data = text.encode("utf-8")
        if len(data) > self.MAX_TEXT_BYTES:
            self._show_notice("The buffer exceeds 5 MiB and was not saved.", Gtk.MessageType.ERROR)
            self._finish_save(False)
            return
        self.saving = True
        self.save_button.set_sensitive(False)
        self.save_cancellable = Gio.Cancellable()
        expected_etag = None if self.force_next_save else self.etag
        if data:
            self.file.replace_contents_bytes_async(
                GLib.Bytes.new(data),
                expected_etag,
                False,
                Gio.FileCreateFlags.REPLACE_DESTINATION,
                self.save_cancellable,
                self._on_saved,
                None,
            )
        else:
            # 2026-08-19: the Python-bytes overload handles an empty payload;
            # GLib.Bytes exposes it as NULL and Gio rejects it before callback.
            self.file.replace_contents_async(
                data,
                expected_etag,
                False,
                Gio.FileCreateFlags.REPLACE_DESTINATION,
                self.save_cancellable,
                self._on_saved,
                None,
            )

    def _on_saved(
        self, source: Gio.File, result: Gio.AsyncResult, _data: object
    ) -> None:
        """Commit a successful save state or expose an external-write conflict."""

        self.save_cancellable = None
        try:
            _success, etag = source.replace_contents_finish(result)
        except GLib.Error as error:
            self.saving = False
            if error.matches(Gio.io_error_quark(), Gio.IOErrorEnum.CANCELLED):
                self._finish_save(False)
                return
            if error.matches(Gio.io_error_quark(), Gio.IOErrorEnum.WRONG_ETAG):
                self.external_conflict = True
                self.attention = True
                self._show_conflict(
                    "The file changed before it could be saved.",
                    (("Use version on disk", self.RESPONSE_DISK), ("Keep my version", self.RESPONSE_MINE)),
                )
                self.on_state(self)
            else:
                self._show_notice(f"Save failed: {error}", Gtk.MessageType.ERROR)
            self._finish_save(False)
            return
        self.saving = False
        self.etag = etag
        self.force_next_save = False
        self.external_conflict = False
        self.deleted_conflict = False
        self.attention = False
        self.mute_monitor_until = GLib.get_monotonic_time() // 1000 + 500
        self.buffer.set_modified(False)
        self._hide_notice()
        self.on_state(self)
        self._finish_save(True)

    def _finish_save(self, success: bool) -> None:
        """Resolve and clear every callback waiting on the current save."""

        self.save_button.set_sensitive(
            self.dirty and self.view.get_editable() and not self.saving
        )
        callbacks = tuple(self.save_callbacks)
        self.save_callbacks.clear()
        for callback in callbacks:
            callback(success)

    def show_search(self) -> None:
        """Reveal the search entry and seed it from the current selection."""

        bounds = self.buffer.get_selection_bounds()
        if bounds:
            self.search_entry.set_text(self.buffer.get_text(bounds[0], bounds[1], True))
        self.search_revealer.set_reveal_child(True)
        self.search_entry.grab_focus()
        self.search_entry.select_region(0, -1)

    def _on_search_changed(self, entry: Gtk.SearchEntry) -> None:
        """Update GtkSource search highlighting from the entry text."""

        self.search_settings.set_search_text(entry.get_text())

    def search(self, backwards: bool = False) -> None:
        """Select the next or previous wrapped GtkSource search match."""

        if not self.search_settings.get_search_text():
            return
        cursor = self.buffer.get_iter_at_mark(self.buffer.get_insert())
        match = (
            self.search_context.backward(cursor)
            if backwards
            else self.search_context.forward(cursor)
        )
        found, start, end, _wrapped = match
        if not found:
            boundary = self.buffer.get_end_iter() if backwards else self.buffer.get_start_iter()
            match = (
                self.search_context.backward(boundary)
                if backwards
                else self.search_context.forward(boundary)
            )
            found, start, end, _wrapped = match
        if found:
            self.buffer.select_range(start, end)
            self.view.scroll_to_iter(start, 0.15, False, 0.0, 0.0)

    def _on_search_key_press(
        self, _entry: Gtk.SearchEntry, event: Gdk.EventKey
    ) -> bool:
        """Navigate results with Enter or close search with Escape."""

        if event.keyval == Gdk.KEY_Escape:
            self.search_revealer.set_reveal_child(False)
            self.view.grab_focus()
            return True
        if event.keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            self.search(bool(event.state & Gdk.ModifierType.SHIFT_MASK))
            return True
        return False

    def goto_line(self) -> None:
        """Ask for a one-based line number and move the insertion cursor."""

        dialog = Gtk.Dialog(
            title="Go to line",
            transient_for=self.get_toplevel(),
            modal=True,
            destroy_with_parent=True,
        )
        dialog.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Go", Gtk.ResponseType.OK)
        entry = Gtk.Entry()
        entry.set_input_purpose(Gtk.InputPurpose.DIGITS)
        entry.set_activates_default(True)
        entry.set_placeholder_text(f"1–{self.buffer.get_line_count()}")
        dialog.get_content_area().pack_start(entry, False, False, 12)
        dialog.set_default_response(Gtk.ResponseType.OK)
        dialog.show_all()
        response = dialog.run()
        value = entry.get_text().strip()
        dialog.destroy()
        if response != Gtk.ResponseType.OK or not value.isdigit():
            return
        line = min(max(1, int(value)), self.buffer.get_line_count()) - 1
        cursor = self.buffer.get_iter_at_line(line)
        self.buffer.place_cursor(cursor)
        self.view.scroll_to_iter(cursor, 0.15, True, 0.0, 0.5)
        self.view.grab_focus()

    def undo(self) -> None:
        """Undo one GtkSource buffer operation when available."""

        if self.buffer.can_undo():
            self.buffer.undo()

    def redo(self) -> None:
        """Redo one GtkSource buffer operation when available."""

        if self.buffer.can_redo():
            self.buffer.redo()

    def mark_activated(self) -> None:
        """Clear informational attention while preserving unresolved conflicts."""

        if not self.external_conflict:
            self.attention = False
            self.on_state(self)
        self.view.grab_focus()

    def close(self) -> None:
        """Cancel asynchronous operations and release the retained file monitor."""

        if self.initial_load_id is not None:
            GLib.source_remove(self.initial_load_id)
            self.initial_load_id = None
        if self.monitor_source_id is not None:
            GLib.source_remove(self.monitor_source_id)
            self.monitor_source_id = None
        if self.load_cancellable is not None:
            self.load_cancellable.cancel()
        if self.save_cancellable is not None:
            self.save_cancellable.cancel()
        if self.monitor is not None:
            self.monitor.cancel()
            self.monitor = None

    def _on_modified_changed(self, _buffer: GtkSource.Buffer) -> None:
        """Publish dirty-state changes to the corresponding project-tree row."""

        self.save_button.set_sensitive(
            self.dirty and self.view.get_editable() and not self.loading and not self.saving
        )
        if not self.loading:
            self.on_state(self)

    def _on_history_capability_changed(
        self, _buffer: GtkSource.Buffer, _property: GObject.GParamSpec
    ) -> None:
        """Refresh Undo and Redo after GtkSource commits a history change."""

        self._sync_history_actions()

    def _sync_history_actions(self) -> None:
        """Enable history buttons only when their respective action is valid."""

        self.undo_button.set_sensitive(self.buffer.can_undo())
        self.redo_button.set_sensitive(self.buffer.can_redo())

    def _on_save_clicked(self, _button: Gtk.Button) -> None:
        """Save from the editor toolbar."""

        self.save()

    def _on_undo_clicked(self, _button: Gtk.Button) -> None:
        """Undo from the editor toolbar."""

        self.undo()

    def _on_redo_clicked(self, _button: Gtk.Button) -> None:
        """Redo from the editor toolbar."""

        self.redo()

    def _on_find_clicked(self, _button: Gtk.Button) -> None:
        """Open search from the editor toolbar."""

        self.show_search()

    def _on_goto_clicked(self, _button: Gtk.Button) -> None:
        """Open line navigation from the editor toolbar."""

        self.goto_line()

    def _on_search_previous_clicked(self, _button: Gtk.Button) -> None:
        """Select the previous search match."""

        self.search(True)

    def _on_search_next_clicked(self, _button: Gtk.Button) -> None:
        """Select the next search match."""

        self.search(False)

    def _on_search_close_clicked(self, _button: Gtk.Button) -> None:
        """Hide the search controls and restore editor focus."""

        self.search_revealer.set_reveal_child(False)
        self.view.grab_focus()


class EditorWorkspace(Gtk.Stack):
    """Keep terminal and editor widgets alive behind one tabless central view."""

    def __init__(
        self,
        terminal_page: Gtk.Widget,
        font_size: int,
        on_persist: Callable[[list[dict[str, str]], dict[str, str] | None], None],
        on_error: Callable[[str], None],
        on_collection_changed: Callable[[], None],
        on_state_changed: Callable[["EditorDocument"], None],
    ) -> None:
        """Build a central stack whose navigation is owned by the project tree."""

        super().__init__()
        self.set_transition_type(Gtk.StackTransitionType.NONE)
        self.font_size = font_size
        self.on_persist = on_persist
        self.on_error = on_error
        self.on_collection_changed = on_collection_changed
        self.on_state_changed = on_state_changed
        # 2026-08-17: le identità persistite restano separate dai widget per
        # evitare GtkSourceView, letture e monitor finché l'utente non le seleziona.
        self.editors: dict[EditorRef, EditorEntry] = {}
        self.active_ref: EditorRef | None = None
        self.restoring = False
        self.next_child_id = 1
        # 2026-08-16: il terminal stack resta un unico child per non ricreare o
        # spostare i Vte.Terminal; gli editor sono sibling selezionati dall'albero.
        self.add_named(terminal_page, "__terminals__")
        terminal_page.show()
        self.terminal_page = terminal_page

    def open_file(
        self, project_name: str, root: str, relative_path: str
    ) -> EditorDocument:
        """Create or activate one unique project-relative editor child."""

        key = (project_name, relative_path)
        entry = self.editors.get(key)
        created = entry is None
        if entry is None:
            entry = EditorEntry(project_name, root, relative_path)
            self.editors[key] = entry
        else:
            entry.root = root
        editor = self._materialize(entry)
        self.set_visible_child(editor)
        editor.mark_activated()
        self.active_ref = key
        self._persist()
        if created:
            self.on_collection_changed()
        return editor

    def _materialize(self, entry: EditorEntry) -> EditorDocument:
        """Create and register an editor widget only on its first activation."""

        if entry.document is not None:
            return entry.document
        editor = EditorDocument(
            entry.project_name,
            entry.root,
            entry.relative_path,
            self.font_size,
            self._on_editor_state,
            self._on_editor_removed,
        )
        entry.document = editor
        child_name = f"editor-{self.next_child_id}"
        self.next_child_id += 1
        self.add_named(editor, child_name)
        editor.show_all()
        editor.info_bar.hide()
        return editor

    def _on_editor_state(self, editor: EditorDocument) -> None:
        """Forward editor state so its project-tree row can be refreshed."""

        self.on_state_changed(editor)

    def _on_editor_removed(self, editor: EditorDocument, message: str) -> None:
        """Close a missing editor row and report why it disappeared."""

        self._remove_editor(editor.reference)
        self.on_error(message)

    def show_terminal(self) -> None:
        """Activate the nested terminal stack without closing editor buffers."""

        self.terminal_page.show()
        self.set_visible_child(self.terminal_page)
        self.active_ref = None
        self._persist()

    def show_inactive(self) -> None:
        """Show the central terminal page without persisting an active item."""

        self.terminal_page.show()
        self.set_visible_child(self.terminal_page)

    def show_editor(self, project_name: str, relative_path: str) -> bool:
        """Show one existing editor selected from its project-tree row."""

        key = (project_name, relative_path)
        entry = self.editors.get(key)
        if entry is None:
            return False
        editor = self._materialize(entry)
        self.set_visible_child(editor)
        editor.mark_activated()
        self.active_ref = key
        self._persist()
        return True

    def relocate_path(
        self, project_name: str, old_path: str, new_path: str
    ) -> None:
        """Rekey editors affected by a successful file or directory rename."""

        prefix = f"{old_path}/"
        affected = [
            (reference, entry)
            for reference, entry in self.editors.items()
            if entry.project_name == project_name
            and (
                entry.relative_path == old_path
                or entry.relative_path.startswith(prefix)
            )
        ]
        for old_reference, entry in affected:
            self.editors.pop(old_reference, None)
            suffix = entry.relative_path[len(old_path) :]
            entry.relative_path = f"{new_path}{suffix}"
            if entry.document is not None:
                entry.document.relocate(entry.relative_path)
            new_reference = (entry.project_name, entry.relative_path)
            self.editors[new_reference] = entry
            if self.active_ref == old_reference:
                self.active_ref = new_reference
        if affected:
            # 2026-08-16: config e albero progetti condividono le identità degli
            # editor; pubblicarle insieme evita riferimenti al vecchio percorso.
            self._persist()
            self.on_collection_changed()

    def current_editor(self) -> EditorDocument | None:
        """Return the visible editor child, excluding the terminal stack."""

        child = self.get_visible_child()
        return child if isinstance(child, EditorDocument) else None

    def handle_key(self, event: Gdk.EventKey) -> bool:
        """Handle editor shortcuts only while an editor child is active."""

        editor = self.current_editor()
        if editor is None or not event.state & Gdk.ModifierType.CONTROL_MASK:
            return False
        keyval = Gdk.keyval_to_lower(event.keyval)
        if keyval == Gdk.KEY_s:
            editor.save()
        elif keyval == Gdk.KEY_f:
            editor.show_search()
        elif keyval == Gdk.KEY_g:
            editor.goto_line()
        elif keyval == Gdk.KEY_w:
            self.request_close(editor)
        elif keyval == Gdk.KEY_z and event.state & Gdk.ModifierType.SHIFT_MASK:
            editor.redo()
        elif keyval == Gdk.KEY_z:
            editor.undo()
        elif keyval == Gdk.KEY_y:
            editor.redo()
        else:
            return False
        return True

    def request_close(self, editor: EditorDocument) -> bool:
        """Close one editor immediately or protect its unsaved buffer."""

        if not editor.dirty:
            self._remove_editor(editor.reference)
            return True
        dialog = Gtk.MessageDialog(
            transient_for=self.get_toplevel(),
            modal=True,
            message_type=Gtk.MessageType.WARNING,
            buttons=Gtk.ButtonsType.NONE,
            text=f"Save changes to {Path(editor.relative_path).name}?",
        )
        dialog.format_secondary_text(f"{editor.project_name} / {editor.relative_path}")
        cancel = dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dialog.add_button("Discard", Gtk.ResponseType.REJECT)
        dialog.add_button("Save", Gtk.ResponseType.ACCEPT)
        dialog.set_default_response(Gtk.ResponseType.CANCEL)
        dialog.set_focus(cancel)
        response = dialog.run()
        dialog.destroy()
        if response == Gtk.ResponseType.REJECT:
            self._remove_editor(editor.reference)
            return True
        if response == Gtk.ResponseType.ACCEPT:
            editor.save(partial(self._close_after_save, editor))
        return False

    def request_close_reference(self, reference: EditorRef) -> bool:
        """Close a registered editor without loading an untouched lazy entry."""

        entry = self.editors.get(reference)
        if entry is None:
            return False
        if entry.document is None:
            self._remove_editor(reference)
            return True
        return self.request_close(entry.document)

    def _close_after_save(self, editor: EditorDocument, success: bool) -> None:
        """Remove an editor row only after its asynchronous save succeeds."""

        if success:
            self._remove_editor(editor.reference)

    def request_close_all(self, callback: Callable[[bool], None]) -> None:
        """Resolve every dirty editor before application shutdown proceeds."""

        dirty = [
            entry.document
            for entry in self.editors.values()
            if entry.document is not None and entry.document.dirty
        ]
        self._confirm_dirty(dirty, callback)

    def _confirm_dirty(
        self,
        dirty: list[EditorDocument],
        callback: Callable[[bool], None],
    ) -> None:
        """Present one aggregate save/discard decision for dirty editors."""

        if not dirty:
            callback(True)
            return
        dialog = Gtk.MessageDialog(
            transient_for=self.get_toplevel(),
            modal=True,
            message_type=Gtk.MessageType.WARNING,
            buttons=Gtk.ButtonsType.NONE,
            text=f"{len(dirty)} files with unsaved changes",
        )
        dialog.format_secondary_text("\n".join(f"• {editor.project_name} / {editor.relative_path}" for editor in dirty))
        cancel = dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dialog.add_button("Discard all", Gtk.ResponseType.REJECT)
        dialog.add_button("Save all", Gtk.ResponseType.ACCEPT)
        dialog.set_default_response(Gtk.ResponseType.CANCEL)
        dialog.set_focus(cancel)
        response = dialog.run()
        dialog.destroy()
        if response == Gtk.ResponseType.REJECT:
            callback(True)
        elif response == Gtk.ResponseType.ACCEPT:
            self._save_many(dirty, callback)
        else:
            callback(False)

    def request_close_project(
        self, project_name: str, callback: Callable[[bool], None]
    ) -> None:
        """Protect dirty project editors before project removal."""

        entries = [
            entry
            for entry in self.editors.values()
            if entry.project_name == project_name
        ]
        dirty = [
            entry.document
            for entry in entries
            if entry.document is not None and entry.document.dirty
        ]
        if not dirty:
            for entry in tuple(entries):
                self._remove_editor((entry.project_name, entry.relative_path))
            callback(True)
            return

        def resolved(success: bool) -> None:
            """Remove project editors only after their dirty decision succeeds."""

            if success:
                for entry in tuple(entries):
                    self._remove_editor((entry.project_name, entry.relative_path))
            callback(success)

        self._confirm_dirty(dirty, resolved)

    def _save_many(
        self,
        editors: list[EditorDocument],
        callback: Callable[[bool], None],
    ) -> None:
        """Save multiple dirty buffers and report aggregate success once."""

        pending = set(editors)
        failed = False

        def saved(editor: EditorDocument, success: bool) -> None:
            """Collect one save result and complete after the final editor."""

            nonlocal failed
            failed = failed or not success
            pending.discard(editor)
            if not pending:
                callback(not failed)

        for editor in editors:
            editor.save(partial(saved, editor))

    def _remove_editor(self, reference: EditorRef) -> None:
        """Release one optional widget and remove its stack/config identity."""

        entry = self.editors.pop(reference, None)
        if entry is None:
            return
        editor = entry.document
        if editor is not None:
            if self.get_visible_child() is editor:
                self.set_visible_child(self.terminal_page)
            editor.close()
            self.remove(editor)
        if self.active_ref == reference:
            self.active_ref = None
        self._persist()
        self.on_collection_changed()

    def set_font_size(self, points: int) -> None:
        """Apply one editor font size to current and future documents."""

        self.font_size = points
        for entry in self.editors.values():
            if entry.document is not None:
                entry.document.set_font_size(points)

    def serialized_state(self) -> tuple[list[dict[str, str]], dict[str, str] | None]:
        """Return ordered editor references and the currently visible editor."""

        tabs = [
            {"project": entry.project_name, "path": entry.relative_path}
            for entry in self.editors.values()
        ]
        active = (
            {"project": self.active_ref[0], "path": self.active_ref[1]}
            if self.active_ref in self.editors
            else None
        )
        return tabs, active

    def _persist(self) -> None:
        """Publish open-editor state unless startup restoration is in progress."""

        if self.restoring:
            return
        tabs, active = self.serialized_state()
        self.on_persist(tabs, active)

    def restore(self, state: dict, projects: list[dict]) -> None:
        """Register configured editor rows without touching their project files."""

        roots = {project["name"]: project["path"] for project in projects}
        self.restoring = True
        try:
            for item in state.get("tabs", []):
                project_name = item.get("project", "")
                relative_path = item.get("path", "")
                root = roots.get(project_name)
                if root:
                    reference = (project_name, relative_path)
                    self.editors[reference] = EditorEntry(
                        project_name, root, relative_path
                    )
            active = state.get("active_tab")
            if isinstance(active, dict):
                reference = (active.get("project", ""), active.get("path", ""))
                self.active_ref = reference if reference in self.editors else None
            self.terminal_page.show()
            self.set_visible_child(self.terminal_page)
        finally:
            self.restoring = False
        self._persist()

    def shutdown(self) -> None:
        """Release every editor monitor during final window destruction."""

        for entry in tuple(self.editors.values()):
            if entry.document is not None:
                entry.document.close()
