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

from .processes import AsyncCommand, CommandResult, run_async
from .search import source_symbol_at_cursor


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
        on_definition: Callable[[str, str, str], None] | None = None,
        *,
        historical: bool = False,
    ) -> None:
        """Build one editor and begin loading its working-copy file."""

        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.historical = historical
        self.previewing = False
        self.on_edit = None
        self.project_name = project_name
        self.root = str(Path(root).resolve())
        self.relative_path = relative_path
        self.on_state = on_state
        self.on_removed = on_removed
        self.on_definition = on_definition
        self.path = self._safe_path()
        self.file = Gio.File.new_for_path(self.path) if self.path else None
        self.etag: str | None = None
        self.loading = False
        self.saving = False
        self.path_changing = False
        self.force_next_save = False
        self.external_conflict = False
        self.deleted_conflict = False
        self.attention = False
        self.monitor: Gio.FileMonitor | None = None
        self.monitor_source_id: int | None = None
        self.load_cancellable: Gio.Cancellable | None = None
        self.save_cancellable: Gio.Cancellable | None = None
        self.save_callbacks: list[Callable[[bool], None]] = []
        self.saved_snapshot: str | None = None
        self.lint_command: AsyncCommand | None = None
        self.lint_serial = 0
        self.lint_dialog: Gtk.MessageDialog | None = None
        self.mute_monitor_until = 0
        self.initial_load_id: int | None = None
        self.pending_line: int | None = None
        self.line_navigation_id: int | None = None

        self._build_info_bar()
        self._build_toolbar()
        self._build_source_view(font_size)
        self._build_search_bar()
        # 2026-08-16: l'idle lascia al workspace il tempo di registrare il child
        # prima che un path non valido possa richiederne la rimozione.
        # 2026-10-30: removed-file presentations never materialize a file or
        # monitor; their immutable repository text is loaded by the derived view.
        if historical:
            self.view.set_editable(False)
        else:
            self.initial_load_id = GLib.idle_add(self._start_initial_load)

    @property
    def reference(self) -> EditorRef:
        """Return the stable project-relative identity used by the workspace."""

        return self.project_name, self.relative_path

    @property
    def dirty(self) -> bool:
        """Return whether the source buffer contains unsaved edits."""

        # 2026-10-30: dopo una scrittura concorrente il save-point nativo
        # può indicare una versione precedente a quella realmente sul disco.
        if self.saved_snapshot is not None:
            text = self.buffer.get_text(self.buffer.get_start_iter(), self.buffer.get_end_iter(), True)
            return text != self.saved_snapshot
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
            resolved = candidate.resolve()
            resolved.relative_to(root)
        except (OSError, RuntimeError, ValueError):
            return None
        # 2026-10-12: Gio.replace_contents_async sostituirebbe il link stesso;
        # usare il target fisico mantiene il link intatto quando si salva.
        return str(resolved)

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

        toolbar_container = Gtk.Overlay()
        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        toolbar.get_style_context().add_class("editor-toolbar")
        toolbar_container.add(toolbar)
        self.toolbar_container = toolbar_container
        self.toolbar = toolbar
        self.path_label = Gtk.Label(label=self.relative_path)
        self.path_label.set_xalign(0)
        self.path_label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        self.path_label.set_tooltip_text(f"{self.project_name} / {self.relative_path}")
        toolbar.pack_start(self.path_label, True, True, 0)
        # 2026-10-30: selection opens a read-only transient document; only an
        # explicit edit action promotes it to the persistent project sidebar.
        self.edit_button = self._tool_button("document-edit-symbolic", "Edit this file (E)", self._on_edit_clicked)
        self.edit_button.set_no_show_all(True)
        toolbar.pack_start(self.edit_button, False, False, 0)
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
        self.pack_start(toolbar_container, False, False, 0)

    def _on_edit_clicked(self, _button) -> None:
        """Promote this transient document through its owning workspace."""

        if self.on_edit is not None:
            self.on_edit(self)
            if self.experimental is not None:
                self.experimental.focus_editor()

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
        self.buffer.connect_after("undo", self._on_history_restored)
        self.buffer.connect_after("redo", self._on_history_restored)
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
        self.view.connect("button-press-event", self._on_view_button_press)
        self.view.connect("populate-popup", self._on_populate_popup)
        self.font_provider = Gtk.CssProvider()
        self.view.get_style_context().add_provider(
            self.font_provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
        self.set_font_size(font_size)
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroller.add(self.view)
        self.source_scroller = scroller
        self.experimental = None
        self.pack_start(scroller, True, True, 0)

    def _on_view_button_press(
        self, view: GtkSource.View, event: Gdk.EventButton
    ) -> bool:
        """Select one identifier on a text-area double click."""

        if event.button != 1 or event.type != Gdk.EventType.DOUBLE_BUTTON_PRESS:
            return False
        window_type = view.get_window_type(event.window)
        if window_type != Gtk.TextWindowType.TEXT:
            return False
        x, y = view.window_to_buffer_coords(window_type, int(event.x), int(event.y))
        _inside_text, text_iter = view.get_iter_at_location(x, y)
        return self._select_identifier_at(text_iter)

    def _select_identifier_at(self, text_iter: Gtk.TextIter) -> bool:
        """Select a source identifier, including underscores and PHP sigils."""

        character = text_iter.get_char()
        if not (character.isalnum() or character in {"_", "$"}):
            return False
        # 2026-10-12: GTK può interpretare il doppio clic come selezione di
        # riga; i confini dell'identificatore sono espliciti solo nell'area testo.
        start = text_iter.copy()
        while start.backward_char():
            character = start.get_char()
            if not (character.isalnum() or character in {"_", "$"}):
                start.forward_char()
                break
        end = text_iter.copy()
        while not end.is_end():
            character = end.get_char()
            if not (character.isalnum() or character in {"_", "$"}):
                break
            end.forward_char()
        self.buffer.select_range(start, end)
        return True

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
        # 2026-10-30: rifiutare i file già troppo grandi prima di leggerli
        # interamente; mantenere anche il controllo finale contro crescite tardive.
        self.file.query_info_async(
            "standard::size", Gio.FileQueryInfoFlags.NONE, GLib.PRIORITY_DEFAULT,
            self.load_cancellable, self._on_load_info, (reason, self.load_cancellable),
        )

    def _on_load_info(self, source: Gio.File, result: Gio.AsyncResult, request) -> None:
        """Check file size asynchronously before starting the full text load."""

        reason, cancellable = request
        if cancellable is not self.load_cancellable:
            return
        try:
            info = source.query_info_finish(result)
        except GLib.Error as error:
            self.load_cancellable = None
            self._load_failed(reason, error)
            return
        if info.get_size() > self.MAX_TEXT_BYTES:
            self.load_cancellable = None
            self._reject_loaded_file(reason, "File over 5 MiB: internal editing is unavailable.")
            return
        source.load_contents_async(cancellable, self._on_loaded, request)

    def _load_failed(self, reason: str, error: GLib.Error) -> None:
        """Report a failed metadata or content read using the same conflict rules."""

        self.loading = False
        if error.matches(Gio.io_error_quark(), Gio.IOErrorEnum.CANCELLED):
            return
        if error.matches(Gio.io_error_quark(), Gio.IOErrorEnum.NOT_FOUND):
            if reason == "initial":
                self.on_removed(self, f"File no longer exists: {self.relative_path}")
            else:
                self._show_deleted_conflict()
            return
        message = f"Unable to read the file: {error}"
        if reason == "initial":
            self.on_removed(self, message)
        else:
            self._show_notice(message, Gtk.MessageType.ERROR)

    def _on_loaded(
        self, source: Gio.File, result: Gio.AsyncResult, request
    ) -> None:
        """Install only the contents belonging to the current asynchronous load."""

        # 2026-10-30: il controllo d'identità copre entrambe le fasi della
        # lettura; un callback annullato non può cancellare il token più recente.
        reason, cancellable = request
        try:
            _success, contents, etag = source.load_contents_finish(result)
        except GLib.Error as error:
            if cancellable is not self.load_cancellable:
                return
            self.load_cancellable = None
            self._load_failed(reason, error)
            return
        if cancellable is not self.load_cancellable:
            return
        self.load_cancellable = None
        if reason == "external" and self.etag is not None and etag == self.etag:
            # 2026-08-23: gli eventi prodotti dal salvataggio interno vengono
            # differiti oltre il mute; l'etag evita che il loro controllo
            # azzeri inutilmente cronologia, selezione e scroll del buffer.
            self.loading = False
            self._ensure_monitor()
            return
        if reason == "external" and self.dirty:
            # 2026-08-23: l'utente può iniziare a digitare mentre la lettura
            # asincrona è attiva; il contenuto locale deve allora diventare un
            # conflitto esplicito, mai essere sostituito dal callback tardivo.
            self.loading = False
            self.external_conflict = True
            self.deleted_conflict = False
            self.attention = True
            self._show_conflict(
                "The file changed on disk while it contains local changes.",
                (("Use version on disk", self.RESPONSE_DISK), ("Keep my version", self.RESPONSE_MINE)),
            )
            self.on_state(self)
            return
        data = bytes(contents)
        if len(data) > self.MAX_TEXT_BYTES:
            self._reject_loaded_file(
                reason, "File over 5 MiB: internal editing is unavailable."
            )
            return
        if b"\0" in data:
            self._reject_loaded_file(
                reason, "Binary file: internal editing is unavailable."
            )
            return
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            self._reject_loaded_file(
                reason, "The file is not UTF-8: internal editing is unavailable."
            )
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
        self.saved_snapshot = None
        self.buffer.set_modified(False)
        self.etag = etag
        self.loading = False
        self.external_conflict = False
        self.deleted_conflict = False
        self.force_next_save = False
        self.view.set_editable(not self.previewing)
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
        if self.experimental is not None:
            self.experimental._document_ready(self.buffer)
        self.on_state(self)
        if self.pending_line is not None:
            line = self.pending_line
            self.pending_line = None
            self.goto_line_number(line, focus=False)

    def _reject_loaded_file(self, reason: str, message: str) -> None:
        """Remove an unsupported new tab or retain an existing editor with notice."""

        self.loading = False
        self.view.set_editable(False)
        # 2026-09-16: il caricamento iniziale fallito non deve lasciare una
        # scheda vuota persistita; un reload esterno invece conserva il buffer.
        if reason == "initial":
            self.on_removed(self, message)
        else:
            self._show_notice(message, Gtk.MessageType.ERROR)

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
        """Monitor the parent directory so atomic file replacements stay visible."""

        if self.monitor is not None or self.file is None:
            return
        parent = self.file.get_parent()
        if parent is None:
            self._show_notice(
                "File monitor unavailable: missing parent directory.",
                Gtk.MessageType.WARNING,
            )
            return
        try:
            # 2026-09-29: gli agenti sostituiscono spesso il file con rename
            # atomici. Il monitor della directory conserva la propria identità
            # mentre cambiano gli inode e permette di osservare ogni sostituzione.
            self.monitor = parent.monitor_directory(
                Gio.FileMonitorFlags.WATCH_MOVES, None
            )
            self.monitor.connect("changed", self._on_file_changed)
        except GLib.Error as error:
            self._show_notice(f"File monitor unavailable: {error}", Gtk.MessageType.WARNING)

    def relocate(self, relative_path: str) -> None:
        """Retarget this buffer after its file or an ancestor was renamed."""

        self.lint_serial += 1
        if self.lint_command is not None:
            self.lint_command.cancel()
            self.lint_command = None
        if self.lint_dialog is not None:
            self.lint_dialog.destroy()
            self.lint_dialog = None
        if self.monitor_source_id is not None:
            GLib.source_remove(self.monitor_source_id)
            self.monitor_source_id = None
        if self.monitor is not None:
            self.monitor.cancel()
            self.monitor = None
        self.relative_path = relative_path
        self.path = self._safe_path()
        self.file = Gio.File.new_for_path(self.path) if self.path else None
        if self.loading:
            self._begin_load("initial" if self.etag is None else "external")
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

        if self.file is None or not (
            self.file.equal(_file)
            or (_other is not None and self.file.equal(_other))
        ):
            return
        if event_type not in {
            Gio.FileMonitorEvent.CHANGED,
            Gio.FileMonitorEvent.CHANGES_DONE_HINT,
            Gio.FileMonitorEvent.CREATED,
            Gio.FileMonitorEvent.DELETED,
            Gio.FileMonitorEvent.MOVED,
            Gio.FileMonitorEvent.MOVED_IN,
            Gio.FileMonitorEvent.MOVED_OUT,
            Gio.FileMonitorEvent.RENAMED,
        }:
            return
        if self.monitor_source_id is not None:
            GLib.source_remove(self.monitor_source_id)
        now = GLib.get_monotonic_time() // 1000
        delay = max(
            self.MONITOR_DEBOUNCE_MS,
            self.mute_monitor_until - now,
        )
        self.monitor_source_id = GLib.timeout_add(
            delay, self._handle_external_change
        )

    def _handle_external_change(self) -> bool:
        """Reload existing files or retain deleted buffers for an explicit choice."""

        self.monitor_source_id = None
        now = GLib.get_monotonic_time() // 1000
        if self.saving or now < self.mute_monitor_until:
            # Un salvataggio asincrono può essere ancora attivo quando scade il
            # primo debounce; conserviamo l'evento finché l'etag è definitivo.
            delay = max(
                self.MONITOR_DEBOUNCE_MS,
                self.mute_monitor_until - now,
            )
            self.monitor_source_id = GLib.timeout_add(
                delay, self._handle_external_change
            )
            return GLib.SOURCE_REMOVE
        exists = bool(self.path and Path(self.path).exists())
        if not exists:
            self._show_deleted_conflict()
            return GLib.SOURCE_REMOVE
        if self.dirty:
            self._ensure_monitor()
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

    def _show_deleted_conflict(self) -> None:
        """Keep a deleted file open and offer recovery without losing its buffer."""

        # 2026-08-26: gli agenti sostituiscono spesso un file con delete+create;
        # la scheda resta viva e monitora lo stesso path, così una ricomparsa
        # può seguire il normale reload esterno invece di perdere il documento.
        self._ensure_monitor()
        self.external_conflict = True
        self.deleted_conflict = True
        self.attention = True
        self._show_conflict(
            "The file was deleted on disk. It may be replaced shortly.",
            (
                ("Recreate from my buffer", self.RESPONSE_RECREATE),
                ("Close tab", self.RESPONSE_DISCARD),
            ),
        )
        self.on_state(self)

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
            if response == self.RESPONSE_RECREATE and not self.dirty:
                # Anche un buffer pulito conserva l'ultima versione valida; la
                # scelta esplicita lo rende salvabile sul path ormai assente.
                self.buffer.set_modified(True)
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
        if self.historical or self.previewing:
            self._finish_save(False)
            return
        if self.path_changing:
            self._show_notice("A rename is in progress; save again when it finishes.", Gtk.MessageType.WARNING)
            self._finish_save(False)
            return
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
        # 2026-08-31: il replace atomico standard conserva la modalità Unix del
        # file; REPLACE_DESTINATION la ricreava invece con 0664, rimuovendo +x
        # da script come deploy.sh dopo ogni salvataggio nell'editor interno.
        create_flags = Gio.FileCreateFlags.NONE
        if data:
            self.file.replace_contents_bytes_async(
                GLib.Bytes.new(data),
                expected_etag,
                False,
                create_flags,
                self.save_cancellable,
                self._on_saved,
                text,
            )
        else:
            # 2026-08-19: the Python-bytes overload handles an empty payload;
            # GLib.Bytes exposes it as NULL and Gio rejects it before callback.
            self.file.replace_contents_async(
                data,
                expected_etag,
                False,
                create_flags,
                self.save_cancellable,
                self._on_saved,
                text,
            )

    def _on_saved(
        self, source: Gio.File, result: Gio.AsyncResult, saved_text: str
    ) -> None:
        """Confirm the written snapshot without clearing newer unsaved changes."""

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
        # 2026-10-30: una scrittura avviata prima di un cambio di identità non
        # prova che il nuovo path contenga il buffer. Proteggere anche chiamanti
        # esterni al coordinamento delle rinomine della workspace.
        if self.file is None or not source.equal(self.file):
            self.saved_snapshot = None
            self.buffer.set_modified(True)
            self.external_conflict = True
            self.attention = True
            self._show_conflict(
                "The file moved while a save was completing. Check the version at the new path.",
                (("Use version on disk", self.RESPONSE_DISK), ("Keep my version", self.RESPONSE_MINE)),
            )
            self.on_state(self)
            self._finish_save(False)
            return
        self.etag = etag
        self.force_next_save = False
        self.external_conflict = False
        self.deleted_conflict = False
        self.attention = False
        self.mute_monitor_until = GLib.get_monotonic_time() // 1000 + 500
        # 2026-10-30: Gio scrive lo snapshot acquisito da save(), non il buffer
        # attuale. Una chiusura in attesa deve fermarsi se nel frattempo è cambiato.
        current_text = self.buffer.get_text(self.buffer.get_start_iter(), self.buffer.get_end_iter(), True)
        unchanged = current_text == saved_text
        self.saved_snapshot = None if unchanged else saved_text
        self.buffer.set_modified(not unchanged)
        self._ensure_monitor()
        self._hide_notice()
        self.on_state(self)
        self._start_php_lint()
        self._finish_save(unchanged)

    def _start_php_lint(self) -> None:
        """Check a newly saved PHP file without blocking the GTK main loop."""

        if self.path is None or Path(self.path).suffix.lower() != ".php":
            return
        # 2026-10-12: il lint parte solo dopo una scrittura riuscita; annullare
        # quello precedente evita popup relativi a contenuti ormai superati.
        self.lint_serial += 1
        if self.lint_command is not None:
            self.lint_command.cancel()
        if self.lint_dialog is not None:
            self.lint_dialog.destroy()
            self.lint_dialog = None
        self.lint_command = run_async(
            ["php", "-l", self.path],
            partial(self._on_php_lint_finished, self.lint_serial),
            cwd=self.root,
        )

    def _on_php_lint_finished(self, serial: int, result: CommandResult) -> None:
        """Show one syntax-error popup for the latest saved PHP version."""

        if serial != self.lint_serial:
            return
        self.lint_command = None
        if result.ok:
            return
        detail = "\n".join(
            part.strip() for part in (result.stderr, result.stdout) if part.strip()
        )
        if not detail:
            detail = str(result.error) if result.error else f"Exit code {result.returncode}"
        top_level = self.get_toplevel()
        parent = top_level if isinstance(top_level, Gtk.Window) else None
        dialog = Gtk.MessageDialog(
            transient_for=parent,
            modal=False,
            message_type=Gtk.MessageType.ERROR,
            buttons=Gtk.ButtonsType.CLOSE,
            text=f"⛔ PHP syntax error: {self.relative_path}",
        )
        # 2026-10-12: l'icona d'errore del tema rende riconoscibile il problema
        # anche se il carattere di divieto non è reso a colori dal sistema.
        dialog.set_image(
            Gtk.Image.new_from_icon_name("dialog-error", Gtk.IconSize.DIALOG)
        )
        dialog.format_secondary_text(detail)
        dialog.connect("response", self._on_php_lint_dialog_response)
        self.lint_dialog = dialog
        dialog.show_all()

    def _on_php_lint_dialog_response(
        self, dialog: Gtk.MessageDialog, _response: Gtk.ResponseType
    ) -> None:
        """Dismiss a completed PHP syntax-check popup."""

        if self.lint_dialog is dialog:
            self.lint_dialog = None
        dialog.destroy()

    def _finish_save(self, success: bool) -> None:
        """Resolve and clear every callback waiting on the current save."""

        self.save_button.set_sensitive(
            self.dirty and self.view.get_editable() and not self.saving and not self.path_changing
        )
        callbacks = tuple(self.save_callbacks)
        self.save_callbacks.clear()
        for callback in callbacks:
            callback(success)

    def show_search(self) -> None:
        """Reveal the search entry and seed it from the current selection."""

        if self.experimental is not None and self.experimental.enabled and self.experimental.show_search():
            return

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
        bounds = self.buffer.get_selection_bounds()
        if bounds:
            cursor = bounds[0] if backwards else bounds[1]
        else:
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
        self.goto_line_number(int(value))

    def goto_line_number(self, line_number: int, *, focus: bool = True) -> None:
        """Move to a one-based line now or immediately after initial loading."""

        # 2026-09-09: l'apertura registra il documento prima di avviare la
        # lettura nell'idle; conservare la riga anche in quella breve finestra
        # evita che il caricamento successivo riporti Ctrl+B alla prima riga.
        if self.loading or self.initial_load_id is not None:
            self.pending_line = max(1, line_number)
            return
        line = min(max(1, line_number), self.buffer.get_line_count()) - 1
        cursor = self.buffer.get_iter_at_line(line)
        self.buffer.place_cursor(cursor)
        self.view.scroll_to_iter(cursor, 0.15, True, 0.0, 0.5)
        # 2026-10-13: un caricamento tardivo posiziona il cursore senza rubare
        # il focus alla sidebar o alla preview aperta nel frattempo.
        if self.experimental is not None and self.experimental.enabled and self.experimental.mode in {"unified", "compact"}:
            self.experimental.align_line(line, focus)
        elif focus:
            self.view.grab_focus()
        if self.line_navigation_id is not None:
            GLib.source_remove(self.line_navigation_id)
        # 2026-09-12: uno stack appena rimappato può ignorare lo scroll prima
        # dell'allocazione; confermarlo nel prossimo idle mantiene visibile la riga.
        self.line_navigation_id = GLib.idle_add(
            self._finish_line_navigation, line_number
        )

    def _finish_line_navigation(self, line_number: int) -> bool:
        """Confirm cursor and scroll after GTK has mapped the activated editor."""

        self.line_navigation_id = None
        line = min(max(1, line_number), self.buffer.get_line_count()) - 1
        cursor = self.buffer.get_iter_at_line(line)
        self.buffer.place_cursor(cursor)
        self.view.scroll_to_iter(cursor, 0.15, True, 0.0, 0.5)
        # Il secondo passaggio conferma soltanto lo scroll: il focus potrebbe
        # essere già passato a un'altra vista dopo la richiesta iniziale.
        return GLib.SOURCE_REMOVE

    def go_to_definition(self) -> bool:
        """Request a project definition search for the symbol at the cursor."""

        symbol = source_symbol_at_cursor(self.buffer)
        if symbol is None:
            return False
        if self.on_definition is None:
            return False
        self.on_definition(self.root, self.relative_path, symbol)
        return True

    def _on_populate_popup(self, _view: Gtk.TextView, menu: Gtk.Menu) -> None:
        """Append Go to Definition to the editor's existing context menu."""

        separator = Gtk.SeparatorMenuItem()
        menu.append(separator)
        item = Gtk.MenuItem(label="Go to Definition")
        item.set_sensitive(source_symbol_at_cursor(self.buffer) is not None)
        item.connect("activate", self._on_definition_activate)
        menu.append(item)
        separator.show()
        item.show()

    def _on_definition_activate(self, _item: Gtk.MenuItem) -> None:
        """Run contextual Go to Definition for the editor cursor."""

        self.go_to_definition()

    def undo(self) -> None:
        """Undo one GtkSource buffer operation when available."""

        if self.buffer.can_undo():
            self.buffer.undo()

    def redo(self) -> None:
        """Redo one GtkSource buffer operation when available."""

        if self.buffer.can_redo():
            self.buffer.redo()

    def mark_activated(self, *, focus: bool = True) -> None:
        """Clear informational attention while preserving unresolved conflicts."""

        if not self.external_conflict:
            self.attention = False
            self.on_state(self)
        if focus:
            if self.experimental is not None and self.experimental.enabled:
                self.experimental.focus_view()
            else:
                self.view.grab_focus()

    def close(self) -> None:
        """Cancel asynchronous operations and release the retained file monitor."""

        if self.experimental is not None:
            self.experimental.shutdown()
        self.lint_serial += 1
        if self.lint_command is not None:
            self.lint_command.cancel()
            self.lint_command = None
        if self.lint_dialog is not None:
            self.lint_dialog.destroy()
            self.lint_dialog = None
        if self.initial_load_id is not None:
            GLib.source_remove(self.initial_load_id)
            self.initial_load_id = None
        if self.line_navigation_id is not None:
            GLib.source_remove(self.line_navigation_id)
            self.line_navigation_id = None
        if self.monitor_source_id is not None:
            GLib.source_remove(self.monitor_source_id)
            self.monitor_source_id = None
        if self.load_cancellable is not None:
            self.load_cancellable.cancel()
            self.load_cancellable = None
        if self.save_cancellable is not None:
            self.save_cancellable.cancel()
        if self.monitor is not None:
            self.monitor.cancel()
            self.monitor = None

    def _on_modified_changed(self, _buffer: GtkSource.Buffer) -> None:
        """Publish dirty-state changes to the corresponding project-tree row."""

        self.save_button.set_sensitive(
            self.dirty and self.view.get_editable() and not self.loading and not self.saving and not self.path_changing
        )
        if not self.loading:
            self.on_state(self)

    def _on_history_restored(self, buffer: GtkSource.Buffer) -> None:
        """Reconcile undo and redo with the snapshot actually written during a concurrent save."""

        if self.saved_snapshot is None:
            return
        dirty = self.dirty
        if not dirty:
            # 2026-10-30: raggiunta la versione su disco, set_modified(False)
            # riallinea il save-point nativo; da qui basta di nuovo GtkSource.
            self.saved_snapshot = None
        buffer.set_modified(dirty)
        self._on_modified_changed(buffer)

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
        on_definition: Callable[[str, str, str], None] | None = None,
    ) -> None:
        """Build a central stack whose navigation is owned by the project tree."""

        super().__init__()
        self.set_transition_type(Gtk.StackTransitionType.NONE)
        self.font_size = font_size
        self.on_persist = on_persist
        self.on_error = on_error
        self.on_collection_changed = on_collection_changed
        self.on_state_changed = on_state_changed
        self.on_definition = on_definition
        # 2026-08-17: le identità persistite restano separate dai widget per
        # evitare GtkSourceView, letture e monitor finché l'utente non le seleziona.
        self.editors: dict[EditorRef, EditorEntry] = {}
        self.active_ref: EditorRef | None = None
        self.restoring = False
        self.next_child_id = 1
        self.historical_document = None
        self.preview_entry = None
        self.renaming_paths: set[tuple[str, str]] = set()
        # 2026-08-16: il terminal stack resta un unico child per non ricreare o
        # spostare i Vte.Terminal; gli editor sono sibling selezionati dall'albero.
        self.add_named(terminal_page, "__terminals__")
        terminal_page.show()
        self.terminal_page = terminal_page

    def open_file(
        self, project_name: str, root: str, relative_path: str, *, focus: bool = True
    ) -> EditorDocument:
        """Create or activate the editor for one physical project file."""

        preview = self.preview_entry
        if (preview is not None and preview.project_name == project_name
                and (Path(preview.root) / preview.relative_path).resolve() == (Path(root) / relative_path).resolve()):
            editor = preview.document
            self.promote(editor)
            self.set_visible_child(editor)
            editor.mark_activated(focus=focus)
            return editor

        key = self.find_open_reference(project_name, root, relative_path)
        if key is None:
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
        editor.mark_activated(focus=focus)
        self.active_ref = key
        self._persist()
        if created:
            self.on_collection_changed()
        return editor

    def open_preview(self, project_name: str, root: str, relative_path: str) -> EditorDocument:
        """Reuse a pinned document or show one non-persistent read-only selection."""

        existing = self.find_open_reference(project_name, root, relative_path)
        if existing is not None:
            return self.open_file(project_name, root, relative_path, focus=False)
        entry = self.preview_entry
        same = (entry is not None and entry.project_name == project_name
                and (Path(entry.root) / entry.relative_path).resolve() == (Path(root) / relative_path).resolve())
        if not same:
            self._close_preview_document()
            entry = EditorEntry(project_name, root, relative_path)
            self.preview_entry = entry
        editor = self._materialize(entry)
        editor.previewing = True
        editor.on_edit = self.promote
        editor.view.set_editable(False)
        editor.edit_button.show()
        self.set_visible_child(editor)
        self.active_ref = editor.reference
        return editor

    def promote(self, editor: EditorDocument) -> None:
        """Pin a transient selection without reloading its buffer or changing its mode."""

        if self.preview_entry is None or self.preview_entry.document is not editor:
            return
        entry = self.preview_entry
        self.preview_entry = None
        self.editors[editor.reference] = entry
        editor.previewing = False
        editor.view.set_editable(not editor.loading and not editor.historical)
        editor.edit_button.hide()
        self.active_ref = editor.reference
        self._persist()
        self.on_collection_changed()

    def _close_preview_document(self) -> None:
        """Release a read-only transient buffer without prompting or altering pinned tabs."""

        entry = self.preview_entry
        self.preview_entry = None
        if entry is not None and entry.document is not None:
            document = entry.document
            if document.get_parent() is self:
                self.remove(document)
            document.close()
            document.destroy()

    def open_historical(self, project_name: str, root: str, relative_path: str) -> EditorDocument:
        """Show a temporary read-only removed file without persisting an editor tab."""

        if self.historical_document is not None:
            previous = self.historical_document
            previous.close()
            self.remove(previous)
            previous.destroy()
        editor = EditorDocument(project_name, root, relative_path, self.font_size,
                                self._on_editor_state, self._on_editor_removed,
                                self.on_definition, historical=True)
        self.historical_document = editor
        self.add_named(editor, "__historical__")
        # 2026-10-30: la presentazione viene collegata dal chiamante; non
        # mappare prima il sorgente grezzo per poi sostituirlo con il diff.
        editor.source_scroller.set_no_show_all(True)
        editor.source_scroller.hide()
        editor.show_all()
        editor.info_bar.hide()
        self.set_visible_child(editor)
        return editor

    def find_open_reference(
        self, project_name: str, root: str, relative_path: str
    ) -> EditorRef | None:
        """Find an existing tab reached through a file or directory symlink."""

        project_root = Path(root).resolve()
        try:
            target = (project_root / relative_path).resolve(strict=True)
            target.relative_to(project_root)
        except (OSError, RuntimeError, ValueError):
            return None
        # 2026-10-12: due alias interni dello stesso file condividono il buffer;
        # mantenere il riferimento originale evita bozze e salvataggi divergenti.
        for reference, entry in self.editors.items():
            if entry.project_name != project_name or Path(entry.root).resolve() != project_root:
                continue
            try:
                existing = (project_root / entry.relative_path).resolve(strict=True)
                existing.relative_to(project_root)
            except (OSError, RuntimeError, ValueError):
                continue
            if existing == target:
                return reference
        return None

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
            self.on_definition,
        )
        entry.document = editor
        editor.path_changing = any(
            name == entry.project_name and (entry.relative_path == path or entry.relative_path.startswith(path + "/"))
            for name, path in self.renaming_paths)
        child_name = f"editor-{self.next_child_id}"
        self.next_child_id += 1
        self.add_named(editor, child_name)
        # 2026-10-30: la presentazione viene collegata dal chiamante; non
        # mappare prima il sorgente grezzo per poi sostituirlo con il diff.
        editor.source_scroller.set_no_show_all(True)
        editor.source_scroller.hide()
        editor.show_all()
        editor.info_bar.hide()
        return editor

    def _on_editor_state(self, editor: EditorDocument) -> None:
        """Forward editor state so its project-tree row can be refreshed."""

        self.on_state_changed(editor)

    def _on_editor_removed(self, editor: EditorDocument, message: str) -> None:
        """Close a missing editor row and report why it disappeared."""

        if self.preview_entry is not None and self.preview_entry.document is editor:
            self._close_preview_document()
        else:
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

    def set_rename_pending(self, project_name: str, relative_path: str, pending: bool) -> bool:
        """Lock affected editor saves for a rename, rejecting overlap with an active write."""

        entries = list(self.editors.values())
        if self.preview_entry is not None:
            entries.append(self.preview_entry)
        if pending:
            for entry in entries:
                if (entry.project_name == project_name
                        and (entry.relative_path == relative_path or entry.relative_path.startswith(relative_path + "/"))
                        and entry.document is not None and entry.document.saving):
                    return False
            if any(name == project_name and (path == relative_path or path.startswith(relative_path + "/")
                                             or relative_path.startswith(path + "/"))
                   for name, path in self.renaming_paths):
                return False
            self.renaming_paths.add((project_name, relative_path))
        else:
            self.renaming_paths.discard((project_name, relative_path))
        # 2026-10-30: il lock copre anche directory e nuovi documenti aperti
        # durante il move asincrono, senza disabilitare la digitazione nel buffer.
        for entry in entries:
            if entry.document is not None:
                locked = any(
                    name == entry.project_name and (entry.relative_path == path or entry.relative_path.startswith(path + "/"))
                    for name, path in self.renaming_paths)
                if entry.document.path_changing != locked:
                    entry.document.path_changing = locked
                    entry.document._on_modified_changed(entry.document.buffer)
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
        preview = self.preview_entry
        if (preview is not None and preview.project_name == project_name
                and (preview.relative_path == old_path or preview.relative_path.startswith(prefix))):
            old_reference = (preview.project_name, preview.relative_path)
            preview.relative_path = new_path + preview.relative_path[len(old_path):]
            if preview.document is not None:
                preview.document.relocate(preview.relative_path)
            if self.active_ref == old_reference:
                self.active_ref = (preview.project_name, preview.relative_path)
            # 2026-10-30: aggiornare il documento temporaneo senza promuoverlo
            # né pubblicare nuove righe o metadati persistenti nei Projects.
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
        if editor.experimental is not None and editor.experimental.enabled:
            if editor.experimental.handle_key(event):
                return True
        keyval = Gdk.keyval_to_lower(event.keyval)
        if keyval == Gdk.KEY_s:
            editor.save()
        elif keyval == Gdk.KEY_b:
            return editor.go_to_definition()
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

        if self.preview_entry is not None and self.preview_entry.document is editor:
            self._close_preview_document()
            self.show_terminal()
            return True
        if editor is self.historical_document:
            editor.close()
            self.remove(editor)
            editor.destroy()
            self.historical_document = None
            self.show_terminal()
            return True
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

        if success and not editor.dirty:
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
        *,
        project_name: str | None = None,
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
            self._save_many(dirty, callback, project_name=project_name)
        else:
            callback(False)

    def request_close_project(
        self, project_name: str, callback: Callable[[bool], None]
    ) -> None:
        """Protect dirty project editors before project removal."""

        if self.preview_entry is not None and self.preview_entry.project_name == project_name:
            self._close_preview_document()
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

        self._confirm_dirty(dirty, resolved, project_name=project_name)

    def _save_many(
        self,
        editors: list[EditorDocument],
        callback: Callable[[bool], None],
        *,
        project_name: str | None = None,
    ) -> None:
        """Save dirty buffers and recheck the entire closing scope before approving closure."""

        pending = set(editors)
        failed = False

        def saved(editor: EditorDocument, success: bool) -> None:
            """Collect one save result and complete after the final editor."""

            nonlocal failed
            failed = failed or not success
            pending.discard(editor)
            if not pending:
                # 2026-10-30: durante l'ultima scrittura si può modificare un
                # documento già salvato, o uno prima pulito. Ricontrollare anche
                # gli editor aperti nel frattempo, nel solo ambito da chiudere.
                current = [entry.document for entry in self.editors.values()
                           if entry.document is not None
                           and (project_name is None or entry.project_name == project_name)]
                callback(not failed and not any(document.dirty for document in [*editors, *current]))

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
        entries = list(self.editors.values())
        if self.preview_entry is not None:
            entries.append(self.preview_entry)
        for entry in entries:
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
        aliases: dict[EditorRef, EditorRef] = {}
        self.restoring = True
        try:
            for item in state.get("tabs", []):
                project_name = item.get("project", "")
                relative_path = item.get("path", "")
                root = roots.get(project_name)
                if root:
                    reference = (project_name, relative_path)
                    existing = self.find_open_reference(
                        project_name, root, relative_path
                    )
                    if existing is not None:
                        # 2026-10-12: le configurazioni precedenti possono avere
                        # due schede per alias dello stesso file; ripristinarne una.
                        aliases[reference] = existing
                        continue
                    self.editors[reference] = EditorEntry(
                        project_name, root, relative_path
                    )
            active = state.get("active_tab")
            if isinstance(active, dict):
                reference = (active.get("project", ""), active.get("path", ""))
                reference = aliases.get(reference, reference)
                self.active_ref = reference if reference in self.editors else None
            self.terminal_page.show()
            self.set_visible_child(self.terminal_page)
        finally:
            self.restoring = False
        self._persist()

    def shutdown(self) -> None:
        """Release every editor monitor during final window destruction."""

        self._close_preview_document()
        if self.historical_document is not None:
            self.historical_document.close()
        for entry in tuple(self.editors.values()):
            if entry.document is not None:
                entry.document.close()
