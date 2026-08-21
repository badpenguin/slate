"""Minimal asynchronous project file browser for the third column."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Callable

import gi

gi.require_version("Gio", "2.0")
gi.require_version("Gdk", "3.0")
gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, Gio, GLib, Gtk, Pango  # noqa: E402


@dataclass
class _ProjectTreeState:
    """Retain one project's loaded file tree while another project is visible."""

    root: str
    store: Gtk.TreeStore
    expanded_paths: set[str]
    dir_iters: dict[str, Gtk.TreeIter]
    loaded_paths: set[str]
    dirty: bool
    ignored: set[str]
    show_hidden: bool
    show_excluded: bool
    repo_watched: bool
    pending_cursor_path: str | None


class ProjectFileManager(Gtk.Box):
    """Browse project files and forward their available filesystem actions."""

    ATTRIBUTES = ",".join(
        (
            Gio.FILE_ATTRIBUTE_STANDARD_NAME,
            Gio.FILE_ATTRIBUTE_STANDARD_DISPLAY_NAME,
            Gio.FILE_ATTRIBUTE_STANDARD_TYPE,
            Gio.FILE_ATTRIBUTE_STANDARD_IS_HIDDEN,
            Gio.FILE_ATTRIBUTE_STANDARD_IS_SYMLINK,
            Gio.FILE_ATTRIBUTE_STANDARD_ICON,
        )
    )
    HARD_EXCLUDED = {
        "node_modules",
        "vendor",
        "dist",
        "build",
        ".venv",
        "__pycache__",
        ".cache",
    }
    VCS_METADATA = {".git", ".hg", ".svn"}
    REFRESH_DEBOUNCE_MS = 200

    COL_NAME = 0
    COL_PATH = 1
    COL_DIRECTORY = 2
    COL_SYMLINK = 3
    COL_LOADED = 4
    COL_EXCLUDED = 5
    COL_PLACEHOLDER = 6
    COL_ICON = 7
    COL_BLOCKED = 8

    def __init__(
        self,
        on_preview: Callable[[str | None], None],
        on_view: Callable[[str], None],
        on_edit_internal: Callable[[str], None],
        on_edit_external: Callable[[str], None],
        on_new_file: Callable[[str], None],
        on_new_directory: Callable[[str], None],
        on_rename: Callable[[str], None],
        on_open_terminal: Callable[[str], None],
        on_delete: Callable[[str], None],
        on_preferences: Callable[[dict[str, object]], None],
    ) -> None:
        """Build the toolbar, lazy tree and action dispatchers."""

        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.get_style_context().add_class("file-manager")
        self.on_preview = on_preview
        self.on_view = on_view
        self.on_edit_internal = on_edit_internal
        self.on_edit_external = on_edit_external
        self.on_new_file = on_new_file
        self.on_new_directory = on_new_directory
        self.on_rename = on_rename
        self.on_open_terminal = on_open_terminal
        self.on_delete = on_delete
        self.on_preferences = on_preferences
        self.project_name: str | None = None
        self.root: str | None = None
        self.repo_watched = False
        self.active = False
        self.dirty = False
        self.show_hidden = False
        self.show_excluded = False
        self.ignored: set[str] = set()
        self.expanded_paths: set[str] = set()
        self.dir_iters: dict[str, Gtk.TreeIter] = {}
        self.loading_paths: set[str] = set()
        self.loaded_paths: set[str] = set()
        self.monitors: dict[str, Gio.FileMonitor] = {}
        self.cancellable = Gio.Cancellable()
        self.request_serial = 0
        self.refresh_id: int | None = None
        self.updating_controls = False
        self.restoring_expansion = False
        self.rebuilding_model = False
        self.expand_all_requested = False
        self.expand_all_complete = False
        self.context_relative: str | None = None
        self.context_directory = False
        self.context_blocked = False
        self.pending_cursor_path: str | None = None
        self.project_states: dict[str, _ProjectTreeState] = {}

        self._build_error_bar()
        self._build_toolbar()
        self._build_tree()
        self._set_available(False)

    def _build_error_bar(self) -> None:
        """Create a dismissible error area for file operations and enumeration."""

        self.error_bar = Gtk.InfoBar()
        self.error_bar.set_message_type(Gtk.MessageType.ERROR)
        self.error_bar.set_show_close_button(True)
        self.error_bar.set_no_show_all(True)
        self.error_bar.connect("response", self._on_error_response)
        self.error_label = Gtk.Label()
        self.error_label.set_xalign(0)
        self.error_label.set_line_wrap(True)
        self.error_bar.get_content_area().add(self.error_label)
        self.pack_start(self.error_bar, False, False, 0)

    def show_error(self, message: str) -> None:
        """Display a file-manager error without switching back to SCM."""

        self.error_label.set_text(message)
        self.error_bar.set_no_show_all(False)
        self.error_bar.show_all()
        self.error_bar.set_no_show_all(True)

    def clear_error(self) -> None:
        """Dismiss the current file-manager error."""

        self.error_label.set_text("")
        self.error_bar.hide()

    def _on_error_response(
        self, _bar: Gtk.InfoBar, _response: Gtk.ResponseType
    ) -> None:
        """Hide a file-manager error after its close control is used."""

        self.clear_error()

    def _build_toolbar(self) -> None:
        """Create expansion and visibility controls above the file tree."""

        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        toolbar.get_style_context().add_class("file-manager-toolbar")
        self.new_file_button = self._toolbar_button(
            "+ File", "document-new", self._on_new_file_clicked
        )
        self.new_directory_button = self._toolbar_button(
            "+ Folder", "folder-new", self._on_new_directory_clicked
        )
        self.expand_button = Gtk.Button()
        expand_content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self.expand_icon = Gtk.Image.new_from_icon_name(
            "go-down", Gtk.IconSize.BUTTON
        )
        self.expand_label = Gtk.Label(label="Expand")
        expand_content.pack_start(self.expand_icon, False, False, 0)
        expand_content.pack_start(self.expand_label, False, False, 0)
        self.expand_button.add(expand_content)
        expand_content.show_all()
        self.expand_button.set_size_request(100, -1)
        self._set_expand_action(False)
        self.expand_button.connect("clicked", self._on_expand_all_clicked)
        self.hidden_check = Gtk.CheckButton(label="Hidden")
        self.hidden_check.set_tooltip_text("Show files and folders hidden by the system")
        self.hidden_check.connect("toggled", self._on_filter_toggled)
        self.excluded_check = Gtk.CheckButton(label="Excluded")
        self.excluded_check.set_tooltip_text("Show ignored files and large folders")
        self.excluded_check.connect("toggled", self._on_filter_toggled)
        # 2026-08-16: creazione e visibilità condividono la singola riga
        # concordata, senza spostare i controlli esistenti su una seconda riga.
        toolbar.pack_start(self.new_file_button, False, False, 0)
        toolbar.pack_start(self.new_directory_button, False, False, 0)
        toolbar.pack_start(self.expand_button, False, False, 0)
        toolbar.pack_start(self.hidden_check, False, False, 0)
        toolbar.pack_start(self.excluded_check, False, False, 0)
        self.pack_start(toolbar, False, False, 0)

    def _set_expand_action(self, expanded: bool) -> None:
        """Update the global tree action with directional icon and label."""

        # 2026-08-16: frecce verticali descrivono espansione e compressione
        # dell'albero senza ricorrere agli ambigui simboli più e meno.
        label = "Collapse" if expanded else "Expand"
        icon_name = "go-up" if expanded else "go-down"
        self.expand_label.set_text(label)
        self.expand_icon.set_from_icon_name(icon_name, Gtk.IconSize.BUTTON)
        self.expand_button.set_tooltip_text(f"{label} all folders")
        self.expand_button.get_accessible().set_name(label)

    def _toolbar_button(
        self,
        label: str,
        icon_name: str,
        callback: Callable[[Gtk.Button], None],
    ) -> Gtk.Button:
        """Create a toolbar action with a themed icon and four-pixel label gap."""

        button = Gtk.Button()
        content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        content.pack_start(
            Gtk.Image.new_from_icon_name(icon_name, Gtk.IconSize.BUTTON),
            False,
            False,
            0,
        )
        content.pack_start(Gtk.Label(label=label), False, False, 0)
        button.add(content)
        button.get_accessible().set_name(label)
        button.connect("clicked", callback)
        return button

    def _build_tree(self) -> None:
        """Create the lazy directory TreeStore and keyboard interactions."""

        self.store = self._new_store()
        self.tree = Gtk.TreeView(model=self.store)
        self.tree.set_headers_visible(False)
        # 2026-08-16: il browser usa comandi a tasto singolo; la ricerca nativa
        # non deve aprirsi per i caratteri che non hanno ancora una scorciatoia.
        self.tree.set_enable_search(False)
        self.tree.set_tooltip_column(self.COL_PATH)
        self.tree.get_selection().set_mode(Gtk.SelectionMode.SINGLE)
        self.tree.connect("row-expanded", self._on_row_expanded)
        self.tree.connect("row-collapsed", self._on_row_collapsed)
        self.tree.connect("cursor-changed", self._on_cursor_changed)
        self.tree.connect("key-press-event", self._on_key_press)
        self.tree.connect("button-press-event", self._on_button_press)
        self.tree.connect("popup-menu", self._on_popup_menu)

        icon = Gtk.CellRendererPixbuf()
        self.text_renderer = Gtk.CellRendererText()
        self.text_renderer.set_property("ellipsize", Pango.EllipsizeMode.END)
        # 2026-08-16: le icone da 24 px mantengono leggibile l'albero, mentre la
        # dimensione del testo è ora controllata dalle impostazioni globali.
        icon.set_property("stock-size", Gtk.IconSize.LARGE_TOOLBAR)
        icon.set_property("ypad", 2)
        self.text_renderer.set_property("ypad", 2)
        column = Gtk.TreeViewColumn("File")
        column.pack_start(icon, False)
        column.pack_start(self.text_renderer, True)
        # 2026-08-16: GIO restituisce icone MIME non-symbolic come prima scelta;
        # il renderer conserva così colori ed emblemi definiti dal tema desktop.
        column.add_attribute(icon, "gicon", self.COL_ICON)
        column.add_attribute(self.text_renderer, "text", self.COL_NAME)
        column.set_spacing(4)
        column.set_expand(True)
        self.tree.append_column(column)

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.add(self.tree)
        self.content_stack = Gtk.Stack()
        self.empty_label = Gtk.Label(label="No active project")
        self.content_stack.add_named(self.empty_label, "empty")
        self.content_stack.add_named(scroller, "tree")
        self.pack_start(self.content_stack, True, True, 0)

    @staticmethod
    def _new_store() -> Gtk.TreeStore:
        """Create one model with the fixed project-file column schema."""

        return Gtk.TreeStore(
            str, str, bool, bool, bool, bool, bool, Gio.Icon, bool
        )

    def set_font_size(self, points: int) -> None:
        """Set the project-file tree text size in typographic points."""

        self.text_renderer.set_property("size-points", float(points))
        # Il renderer non invalida da solo le righe già disegnate: ricalcolare
        # subito geometria e contenuto evita aggiornamenti parziali allo scroll.
        self.tree.queue_resize()
        self.tree.queue_draw()

    def set_project(
        self,
        project_name: str,
        root: str,
        preferences: dict[str, object],
        ignored: set[str],
        repo_watched: bool,
    ) -> None:
        """Select a project and reuse its cached tree when still coherent."""

        resolved_root = str(Path(root).resolve())
        self._stash_project_state()
        self.project_name = project_name
        self.root = resolved_root
        self.repo_watched = repo_watched
        requested_ignored = {path.rstrip("/") for path in ignored if path}
        requested_hidden = bool(preferences.get("show_hidden", False))
        requested_excluded = bool(preferences.get("show_excluded", False))
        raw_expanded = preferences.get("expanded_paths", [])
        requested_expanded = (
            {str(path) for path in raw_expanded}
            if isinstance(raw_expanded, list)
            else set()
        )
        cached = self.project_states.get(project_name)
        if cached is None or cached.root != resolved_root:
            self.store = self._new_store()
            self.dir_iters = {}
            self.loaded_paths = set()
            self.dirty = True
            self.pending_cursor_path = None
        else:
            self.store = cached.store
            self.dir_iters = cached.dir_iters
            self.loaded_paths = cached.loaded_paths
            self.dirty = cached.dirty or (
                cached.ignored != requested_ignored
                or cached.show_hidden != requested_hidden
                or cached.show_excluded != requested_excluded
                or cached.repo_watched != repo_watched
            )
            self.pending_cursor_path = cached.pending_cursor_path
        self.tree.set_model(self.store)
        self.loading_paths = set()
        self.ignored = requested_ignored
        self.show_hidden = requested_hidden
        self.show_excluded = requested_excluded
        self.expanded_paths = requested_expanded
        self.updating_controls = True
        self.hidden_check.set_active(self.show_hidden)
        self.excluded_check.set_active(self.show_excluded)
        self.updating_controls = False
        self._set_available(True)
        if self.active:
            if self.dirty:
                self.refresh()
            else:
                self._restore_cached_expansion()

    def _stash_project_state(self) -> None:
        """Save the current model by project without destroying its loaded rows."""

        if self.project_name is None or self.root is None:
            return
        incomplete = bool(self.loading_paths)
        self._cancel_requests()
        self._clear_monitors()
        self.loading_paths.clear()
        # 2026-08-17: repository watcher persistenti rendono riusabile il modello;
        # un browser senza watcher viene invece ricaricato per non mostrare dati stantii.
        self.project_states[self.project_name] = _ProjectTreeState(
            self.root,
            self.store,
            set(self.expanded_paths),
            dict(self.dir_iters),
            set(self.loaded_paths),
            self.dirty or incomplete or not self.repo_watched,
            set(self.ignored),
            self.show_hidden,
            self.show_excluded,
            self.repo_watched,
            self.pending_cursor_path,
        )

    def _restore_cached_expansion(self) -> None:
        """Restore expanded rows after attaching a cached model to the TreeView."""

        self.restoring_expansion = True
        try:
            ordered_paths = [
                (path.count("/"), path) for path in self.expanded_paths
            ]
            ordered_paths.sort()
            for _depth, relative in ordered_paths:
                tree_iter = self.dir_iters.get(relative)
                if tree_iter is not None:
                    self.tree.expand_row(self.store.get_path(tree_iter), False)
        finally:
            self.restoring_expansion = False

    def clear_project(self) -> None:
        """Release project resources and show the inactive empty state."""

        self.project_name = None
        self.root = None
        self.ignored.clear()
        self.expanded_paths.clear()
        self._cancel_requests()
        self._clear_monitors()
        self._clear_model()
        self.project_states.clear()
        self._set_available(False)

    def forget_project(self, project_name: str) -> None:
        """Drop cached rows belonging to a project removed from configuration."""

        self.project_states.pop(project_name, None)

    def set_active(self, active: bool) -> None:
        """Refresh deferred changes when the file page becomes visible."""

        self.active = active
        if active:
            # 2026-08-16: Gtk.Widget.show_all() può riportare lo Stack interno
            # al primo child; riattivare esplicitamente tree evita il falso
            # stato “Nessun progetto attivo” con una root già configurata.
            self._set_available(self.root is not None)
            if self.dirty:
                self.refresh()
            else:
                self._restore_cached_expansion()

    def update_ignored(self, ignored: set[str]) -> None:
        """Replace VCS ignore data and rebuild when excluded visibility depends on it."""

        normalized = {path.rstrip("/") for path in ignored if path}
        if normalized == self.ignored:
            return
        self.ignored = normalized
        if self.active:
            self.refresh()
        else:
            self.dirty = True

    def project_ignored_changed(
        self, project_name: str, ignored: set[str]
    ) -> None:
        """Update ignore data only on the matching visible or cached tree."""

        normalized = {path.rstrip("/") for path in ignored if path}
        if project_name == self.project_name:
            self.update_ignored(normalized)
            return
        cached = self.project_states.get(project_name)
        if cached is not None and cached.ignored != normalized:
            cached.ignored = normalized
            cached.dirty = True

    def project_filesystem_changed(
        self, project_name: str, relative_path: str
    ) -> None:
        """Dirty the matching visible or cached project after a watcher event."""

        if project_name == self.project_name:
            self.filesystem_changed(relative_path)
            return
        cached = self.project_states.get(project_name)
        if cached is not None:
            cached.dirty = True

    def filesystem_changed(self, _relative_path: str) -> None:
        """Debounce one watcher notification into a stable tree refresh."""

        if not self.active:
            self.dirty = True
            return
        if self.refresh_id is not None:
            GLib.source_remove(self.refresh_id)
        self.refresh_id = GLib.timeout_add(
            self.REFRESH_DEBOUNCE_MS, self._on_refresh_timeout
        )

    def refresh(self) -> None:
        """Reload the visible hierarchy while restoring expanded paths."""

        if self.root is None:
            return
        if not self.active:
            self.dirty = True
            return
        self.dirty = False
        self._rebuild()

    def relocate_path(self, old_path: str, new_path: str) -> None:
        """Preserve expansion and cursor state across a successful rename."""

        old_prefix = f"{old_path}/"
        self.expanded_paths = {
            f"{new_path}{path[len(old_path):]}"
            if path == old_path or path.startswith(old_prefix)
            else path
            for path in self.expanded_paths
        }
        self.pending_cursor_path = new_path
        # 2026-08-16: directory espanse e focus seguono l'identità rinominata,
        # invece di farla apparire collassata o perdere il punto di lavoro.
        self._save_preferences()

    def reveal_path(self, relative_path: str) -> bool:
        """Reveal a safe project path or focus its nearest visible directory."""

        raw_parts = relative_path.split("/")
        if (
            self.root is None
            or not relative_path
            or os.path.isabs(relative_path)
            or ".." in raw_parts
            or "\0" in relative_path
        ):
            return False
        normalized = os.path.normpath(relative_path)
        if normalized in {"", "."} or ".." in normalized.split("/"):
            return False

        # 2026-08-20: la navigazione prepara soltanto la catena degli antenati;
        # l'enumerazione GIO esistente la materializza in modo lazy e asincrono.
        parts = normalized.split("/")
        ancestors = {
            "/".join(parts[:depth]) for depth in range(1, len(parts))
        }
        self.expanded_paths.update(ancestors)
        self.pending_cursor_path = normalized
        if self.active:
            if self.dirty:
                self.refresh()
            else:
                self._restore_cached_expansion()

        target_iter = self._tree_iter_for_path(normalized)
        if target_iter is not None:
            self._focus_tree_iter(target_iter)
            self.pending_cursor_path = None
        else:
            # Mostrare subito l'antenato già caricato evita una navigazione
            # apparentemente inerte mentre i livelli successivi vengono letti.
            loaded_fallback: str | None = None
            for ancestor in sorted(ancestors, reverse=True):
                ancestor_iter = self.dir_iters.get(ancestor)
                if ancestor_iter is not None:
                    self._focus_tree_iter(ancestor_iter)
                    if ancestor in self.loaded_paths:
                        loaded_fallback = ancestor
                    break
            if loaded_fallback is None and "" in self.loaded_paths:
                loaded_fallback = ""
            if loaded_fallback is not None:
                self._resolve_pending_reveal(loaded_fallback)
        self._save_preferences()
        return True

    def _tree_iter_for_path(self, relative_path: str) -> Gtk.TreeIter | None:
        """Find one currently loaded real row by its project-relative path."""

        def _walk(parent: Gtk.TreeIter | None) -> Gtk.TreeIter | None:
            """Depth-first search loaded rows without triggering enumeration."""

            tree_iter = self.store.iter_children(parent)
            while tree_iter is not None:
                if (
                    not self.store.get_value(tree_iter, self.COL_PLACEHOLDER)
                    and self.store.get_value(tree_iter, self.COL_PATH)
                    == relative_path
                ):
                    return tree_iter
                found = _walk(tree_iter)
                if found is not None:
                    return found
                tree_iter = self.store.iter_next(tree_iter)
            return None

        return _walk(None)

    def _focus_tree_iter(self, tree_iter: Gtk.TreeIter) -> None:
        """Select and scroll to one already loaded File-manager row."""

        row_path = self.store.get_path(tree_iter)
        self.tree.set_cursor(row_path)
        self.tree.scroll_to_cell(row_path, None, False, 0.0, 0.0)

    def _set_available(self, available: bool) -> None:
        """Enable controls and select the appropriate content page."""

        self.expand_button.set_sensitive(available)
        self.new_file_button.set_sensitive(available)
        self.new_directory_button.set_sensitive(available)
        self.hidden_check.set_sensitive(available)
        self.excluded_check.set_sensitive(available)
        # 2026-08-16: Gtk.Stack seleziona soltanto child già visibili; farli
        # conoscere prima evita che visible-child-name rimanga silenziosamente None.
        self.content_stack.show_all()
        self.content_stack.set_visible_child_name("tree" if available else "empty")

    def _rebuild(self) -> None:
        """Cancel stale enumeration and lazily reload the project root."""

        self._cancel_requests()
        self._clear_monitors()
        self._clear_model()
        self.dir_iters.clear()
        self.loading_paths.clear()
        self.loaded_paths.clear()
        self.expand_all_requested = False
        self.expand_all_complete = False
        self._set_expand_action(False)
        self.cancellable = Gio.Cancellable()
        self.request_serial += 1
        if self.root is not None:
            if not self.repo_watched:
                self._monitor_directory("", excluded=False)
            self._load_directory("")

    def _clear_model(self) -> None:
        """Clear programmatic rows without treating them as user collapses."""

        # 2026-08-16: Gtk.TreeStore.clear() può emettere row-collapsed nella
        # vista realizzata; un refresh o cambio filtro non deve perdere i rami.
        self.rebuilding_model = True
        try:
            self.store.clear()
        finally:
            self.rebuilding_model = False

    def _cancel_requests(self) -> None:
        """Invalidate pending enumeration and refresh callbacks."""

        self.request_serial += 1
        self.cancellable.cancel()
        if self.refresh_id is not None:
            GLib.source_remove(self.refresh_id)
            self.refresh_id = None

    def _load_directory(self, relative: str) -> None:
        """Start asynchronous enumeration for one unloaded relative directory."""

        if self.root is None or relative in self.loading_paths or relative in self.loaded_paths:
            return
        self.loading_paths.add(relative)
        directory = Gio.File.new_for_path(os.path.join(self.root, relative))
        directory.enumerate_children_async(
            self.ATTRIBUTES,
            Gio.FileQueryInfoFlags.NOFOLLOW_SYMLINKS,
            GLib.PRIORITY_DEFAULT,
            self.cancellable,
            self._on_enumerator_ready,
            (self.request_serial, relative),
        )

    def _on_enumerator_ready(
        self,
        source: Gio.File,
        result: Gio.AsyncResult,
        request: tuple[int, str],
    ) -> None:
        """Begin paged reads after GIO opens one directory enumerator."""

        serial, relative = request
        if serial != self.request_serial:
            return
        try:
            enumerator = source.enumerate_children_finish(result)
        except GLib.Error as error:
            self._finish_directory(relative, [], str(error))
            return
        enumerator.next_files_async(
            100,
            GLib.PRIORITY_DEFAULT,
            self.cancellable,
            self._on_entries_ready,
            (serial, relative, []),
        )

    def _on_entries_ready(
        self,
        enumerator: Gio.FileEnumerator,
        result: Gio.AsyncResult,
        request: tuple[int, str, list[Gio.FileInfo]],
    ) -> None:
        """Collect bounded pages and publish a directory only when complete."""

        serial, relative, entries = request
        if serial != self.request_serial:
            return
        try:
            page = enumerator.next_files_finish(result)
        except GLib.Error as error:
            self._finish_directory(relative, [], str(error))
            return
        if page:
            entries.extend(page)
            enumerator.next_files_async(
                100,
                GLib.PRIORITY_DEFAULT,
                self.cancellable,
                self._on_entries_ready,
                (serial, relative, entries),
            )
            return
        enumerator.close_async(
            GLib.PRIORITY_DEFAULT, None, self._on_enumerator_closed, None
        )
        self._finish_directory(relative, entries)

    def _on_enumerator_closed(
        self,
        enumerator: Gio.FileEnumerator,
        result: Gio.AsyncResult,
        _data: object,
    ) -> None:
        """Finish an asynchronous enumerator close without surfacing stale errors."""

        try:
            enumerator.close_finish(result)
        except GLib.Error:
            pass

    def _finish_directory(
        self,
        relative: str,
        entries: list[Gio.FileInfo],
        error: str | None = None,
    ) -> None:
        """Replace a placeholder with sorted, filtered directory children."""

        self.loading_paths.discard(relative)
        self.loaded_paths.add(relative)
        parent = self.dir_iters.get(relative) if relative else None
        if relative and parent is None:
            return
        placeholder = None
        if parent is not None:
            self.store.set_value(parent, self.COL_LOADED, True)
            child = self.store.iter_children(parent)
            while child:
                if self.store.get_value(child, self.COL_PLACEHOLDER):
                    placeholder = child
                    break
                child = self.store.iter_next(child)
        if error:
            self.store.append(
                parent,
                [
                    f"Unable to read: {error}",
                    "",
                    False,
                    False,
                    True,
                    False,
                    True,
                    Gio.ThemedIcon.new("dialog-error"),
                    True,
                ],
            )
            if placeholder is not None:
                self.store.remove(placeholder)
            self._resolve_pending_reveal(relative)
            self._continue_expand_all()
            return
        visible = [info for info in entries if self._entry_visible(relative, info)]
        visible.sort(key=partial(self._entry_sort_key, relative))
        # 2026-08-16: i figli reali entrano prima di togliere il placeholder;
        # evitando un nodo temporaneamente vuoto GTK non richiude l'expander.
        for info in visible:
            self._append_entry(parent, relative, info)
        if placeholder is not None:
            self.store.remove(placeholder)
        self._resolve_pending_reveal(relative)
        self._restore_child_expansion(relative)
        self._continue_expand_all()

    def _resolve_pending_reveal(self, loaded_directory: str) -> None:
        """Fall back when a requested next path component is filtered or absent."""

        target = self.pending_cursor_path
        prefix = f"{loaded_directory}/" if loaded_directory else ""
        if target is None or not target.startswith(prefix):
            return
        remainder = target[len(prefix):]
        if not remainder:
            return
        next_path = f"{prefix}{remainder.split('/', 1)[0]}"
        if self._tree_iter_for_path(next_path) is not None:
            return
        if loaded_directory:
            fallback = self.dir_iters.get(loaded_directory)
            if fallback is not None:
                self._focus_tree_iter(fallback)
        # La root non ha una propria riga: in quel caso la scheda Files già
        # rappresenta correttamente la directory di fallback del progetto.
        self.pending_cursor_path = None

    def _entry_sort_key(
        self, parent: str, info: Gio.FileInfo
    ) -> tuple[bool, str]:
        """Sort directories before files and names without case sensitivity."""

        name = info.get_name()
        relative = f"{parent}/{name}" if parent else name
        is_directory, _is_symlink, _blocked = self._classify_entry(relative, info)
        return (
            not is_directory,
            info.get_display_name().casefold(),
        )

    def _classify_entry(
        self, relative: str, info: Gio.FileInfo
    ) -> tuple[bool, bool, bool]:
        """Classify links without allowing broken, external or cyclic traversal."""

        is_symlink = info.get_is_symlink()
        if not is_symlink:
            return info.get_file_type() == Gio.FileType.DIRECTORY, False, False
        if self.root is None:
            return False, True, True
        candidate = os.path.join(self.root, relative)
        if not os.path.exists(candidate):
            return False, True, True
        target = os.path.realpath(candidate)
        parent_target = os.path.realpath(os.path.dirname(candidate))
        try:
            inside_root = os.path.commonpath((self.root, target)) == self.root
            target_contains_parent = (
                os.path.commonpath((parent_target, target)) == target
            )
        except ValueError:
            inside_root = False
            target_contains_parent = True
        # 2026-08-17: only links to safe descendants can become expandable.
        # External, broken and ancestor links remain visible but inert, avoiding
        # both workspace escape and recursive cycles during "Espandi".
        blocked = not inside_root or target_contains_parent
        return os.path.isdir(candidate) and not blocked, True, blocked

    def _entry_visible(self, parent: str, info: Gio.FileInfo) -> bool:
        """Apply metadata, hidden and excluded visibility rules to one entry."""

        name = info.get_name()
        relative = f"{parent}/{name}" if parent else name
        if any(part in self.VCS_METADATA for part in relative.split("/")):
            return False
        if info.get_is_hidden() and not self.show_hidden:
            return False
        return self.show_excluded or not self._is_excluded(relative)

    def _is_excluded(self, relative: str) -> bool:
        """Classify VCS-ignored and performance-blacklisted paths."""

        parts = relative.split("/")
        if any(part in self.HARD_EXCLUDED for part in parts):
            return True
        return any(
            relative == ignored or relative.startswith(f"{ignored}/")
            for ignored in self.ignored
        )

    def _append_entry(
        self,
        parent: Gtk.TreeIter | None,
        parent_path: str,
        info: Gio.FileInfo,
    ) -> None:
        """Append one safe filesystem entry and a lazy directory placeholder."""

        name = info.get_name()
        relative = f"{parent_path}/{name}" if parent_path else name
        is_directory, is_symlink, blocked = self._classify_entry(relative, info)
        excluded = self._is_excluded(relative)
        broken = is_symlink and not os.path.exists(
            os.path.join(self.root or "", relative)
        )
        if broken:
            icon = Gio.ThemedIcon.new("dialog-error")
        elif is_symlink and is_directory:
            icon = Gio.EmblemedIcon.new(
                Gio.ThemedIcon.new("folder"),
                Gio.Emblem.new(Gio.ThemedIcon.new("emblem-symbolic-link")),
            )
        elif is_symlink and not blocked:
            target = os.path.realpath(os.path.join(self.root or "", relative))
            content_type, _uncertain = Gio.content_type_guess(target, None)
            icon = Gio.EmblemedIcon.new(
                Gio.content_type_get_icon(content_type),
                Gio.Emblem.new(Gio.ThemedIcon.new("emblem-symbolic-link")),
            )
        else:
            icon = info.get_icon() or Gio.ThemedIcon.new(
                "folder" if is_directory else "text-x-generic"
            )
        tree_iter = self.store.append(
            parent,
            [
                info.get_display_name(),
                relative,
                is_directory,
                is_symlink,
                not is_directory,
                excluded,
                False,
                icon,
                blocked,
            ],
        )
        if relative == self.pending_cursor_path:
            row_path = self.store.get_path(tree_iter)
            self.tree.set_cursor(row_path)
            self.tree.scroll_to_cell(row_path, None, False, 0.0, 0.0)
            self.pending_cursor_path = None
        if is_directory:
            self.dir_iters[relative] = tree_iter
            self.store.append(
                tree_iter,
                [
                    "Loading…",
                    "",
                    False,
                    False,
                    True,
                    excluded,
                    True,
                    Gio.ThemedIcon.new("process-working"),
                    False,
                ],
            )

    def _restore_child_expansion(self, parent_path: str) -> None:
        """Expand persisted direct children after their parent finishes loading."""

        prefix_depth = 0 if not parent_path else parent_path.count("/") + 1
        targets = [
            path
            for path in self.expanded_paths
            if path.count("/") == prefix_depth
            and (not parent_path or path.startswith(f"{parent_path}/"))
        ]
        self.restoring_expansion = True
        try:
            for path in sorted(targets):
                tree_iter = self.dir_iters.get(path)
                if tree_iter is not None:
                    self.tree.expand_row(self.store.get_path(tree_iter), False)
        finally:
            self.restoring_expansion = False

    def _on_row_expanded(
        self, _tree: Gtk.TreeView, tree_iter: Gtk.TreeIter, _path: Gtk.TreePath
    ) -> None:
        """Load an expanded directory and persist its relative path."""

        relative = self.store.get_value(tree_iter, self.COL_PATH)
        if not relative:
            return
        self.expanded_paths.add(relative)
        excluded = self.store.get_value(tree_iter, self.COL_EXCLUDED)
        if not self.repo_watched or excluded:
            self._monitor_directory(relative, excluded)
        self._load_directory(relative)
        if not self.restoring_expansion:
            self._save_preferences()

    def _on_row_collapsed(
        self, _tree: Gtk.TreeView, tree_iter: Gtk.TreeIter, _path: Gtk.TreePath
    ) -> None:
        """Forget collapsed descendants and release supplemental monitors."""

        if self.rebuilding_model:
            return
        relative = self.store.get_value(tree_iter, self.COL_PATH)
        if not relative:
            return
        self.expanded_paths = {
            path
            for path in self.expanded_paths
            if path != relative and not path.startswith(f"{relative}/")
        }
        self._remove_monitors_below(relative)
        self.expand_all_complete = False
        if not self.expand_all_requested:
            self._set_expand_action(False)
        if not self.restoring_expansion:
            self._save_preferences()

    def _on_cursor_changed(self, _tree: Gtk.TreeView) -> None:
        """Preview the focused file and clear preview for directories."""

        relative = self._focused_file()
        self.on_preview(relative)

    def _focused_file(self) -> str | None:
        """Return the focused non-placeholder file path."""

        entry = self._focused_entry()
        if entry is None or entry[1] or entry[2]:
            return None
        return entry[0]

    def _focused_entry(self) -> tuple[str, bool, bool] | None:
        """Return the focused real entry path, directory and blocked flags."""

        path, _column = self.tree.get_cursor()
        if path is None:
            return None
        tree_iter = self.store.get_iter(path)
        if self.store.get_value(tree_iter, self.COL_PLACEHOLDER):
            return None
        relative = self.store.get_value(tree_iter, self.COL_PATH)
        if not relative:
            return None
        return (
            relative,
            self.store.get_value(tree_iter, self.COL_DIRECTORY),
            self.store.get_value(tree_iter, self.COL_BLOCKED),
        )

    def _creation_parent(self) -> str:
        """Choose root, focused directory or focused file parent for creation."""

        entry = self._focused_entry()
        if entry is None:
            return ""
        relative, directory, blocked = entry
        if blocked:
            return ""
        return relative if directory else os.path.dirname(relative)

    def _on_key_press(self, _tree: Gtk.TreeView, event: Gdk.EventKey) -> bool:
        """Dispatch file shortcuts and Delete for the focused filesystem entry."""

        if event.state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.MOD1_MASK):
            return False
        entry = self._focused_entry()
        if entry is None:
            return False
        relative, directory, blocked = entry
        if blocked:
            keyval = Gdk.keyval_to_lower(event.keyval)
            return keyval in {
                Gdk.KEY_v,
                Gdk.KEY_m,
                Gdk.KEY_e,
                Gdk.KEY_r,
                Gdk.KEY_t,
                Gdk.KEY_Delete,
                Gdk.KEY_KP_Delete,
            }
        keyval = Gdk.keyval_to_lower(event.keyval)
        if keyval == Gdk.KEY_v and not directory:
            self.on_view(relative)
            return True
        if keyval == Gdk.KEY_m and not directory:
            self.on_edit_internal(relative)
            return True
        if keyval == Gdk.KEY_e and not directory:
            self.on_edit_external(relative)
            return True
        if keyval == Gdk.KEY_r:
            self.on_rename(relative)
            return True
        if keyval == Gdk.KEY_t and directory:
            self.on_open_terminal(relative)
            return True
        if event.keyval in (Gdk.KEY_Delete, Gdk.KEY_KP_Delete):
            self.on_delete(relative)
            return True
        return False

    def _on_button_press(self, tree: Gtk.TreeView, event: Gdk.EventButton) -> bool:
        """Toggle folders or open contextual actions for entries and root space."""

        if event.button not in (1, 3):
            return False
        hit = tree.get_path_at_pos(int(event.x), int(event.y))
        if not hit:
            if event.button == 3:
                tree.get_selection().unselect_all()
                self.context_relative = None
                self.context_directory = False
                self.context_blocked = False
                return self._show_entry_menu(event)
            return False
        tree.set_cursor(hit[0])
        if event.button == 1:
            tree_iter = self.store.get_iter(hit[0])
            if not self.store.get_value(tree_iter, self.COL_DIRECTORY):
                return False
            # Il click viene consumato dal toggle personalizzato, quindi il focus
            # va assegnato esplicitamente affinché Canc raggiunga l'albero.
            tree.grab_focus()
            # 2026-08-16: consumare il clic della cartella evita che l'expander
            # nativo e il toggle sulla label agiscano entrambi sullo stesso nodo.
            if tree.row_expanded(hit[0]):
                tree.collapse_row(hit[0])
            else:
                tree.expand_row(hit[0], False)
            return True
        entry = self._focused_entry()
        if entry is None:
            return False
        self.context_relative, self.context_directory, self.context_blocked = entry
        if self.context_blocked:
            return True
        return self._show_entry_menu(event)

    def _on_popup_menu(self, _tree: Gtk.TreeView) -> bool:
        """Open contextual entry actions from Menu or Shift+F10."""

        entry = self._focused_entry()
        if entry is None:
            return False
        self.context_relative, self.context_directory, self.context_blocked = entry
        if self.context_blocked:
            return True
        return self._show_entry_menu(None)

    def _show_entry_menu(self, event: Gdk.EventButton | None) -> bool:
        """Present creation plus actions appropriate to the contextual entry."""

        if self.context_blocked:
            return True
        menu = Gtk.Menu()
        if self.context_relative is not None and self.context_directory:
            terminal_item = self._menu_item(
                "Open terminal here", "utilities-terminal", Gdk.KEY_t
            )
            terminal_item.connect("activate", self._on_context_open_terminal)
            menu.append(terminal_item)
            menu.append(Gtk.SeparatorMenuItem())
        if self.context_relative is not None and not self.context_directory:
            for label, icon, keyval, callback in (
                ("View", "document-open", Gdk.KEY_v, self._on_context_view),
                (
                    "Edit in SLATE",
                    "accessories-text-editor",
                    Gdk.KEY_m,
                    self._on_context_edit_internal,
                ),
                ("Edit in gVim", "gvim", Gdk.KEY_e, self._on_context_edit_external),
            ):
                item = self._menu_item(label, icon, keyval)
                item.connect("activate", callback)
                menu.append(item)
            menu.append(Gtk.SeparatorMenuItem())
        for label, icon, callback in (
            ("+ File", "document-new", self._on_context_new_file),
            ("+ Folder", "folder-new", self._on_context_new_directory),
        ):
            item = self._menu_item(label, icon, None)
            item.connect("activate", callback)
            menu.append(item)
        if self.context_relative is not None:
            menu.append(Gtk.SeparatorMenuItem())
            rename_item = self._menu_item("Rename", "edit-rename", Gdk.KEY_r)
            rename_item.connect("activate", self._on_context_rename)
            menu.append(rename_item)
            item = self._menu_item("Delete", "edit-delete", Gdk.KEY_Delete)
            item.connect("activate", self._on_context_delete)
            menu.append(item)
        menu.show_all()
        if event is not None:
            menu.popup_at_pointer(event)
        else:
            menu.popup_at_widget(self.tree, Gdk.Gravity.CENTER, Gdk.Gravity.CENTER, None)
        return True

    @staticmethod
    def _menu_item(
        label: str, icon_name: str, keyval: int | None
    ) -> Gtk.MenuItem:
        """Create a contextual item with icon and single-key hint."""

        item = Gtk.MenuItem()
        content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        icon = Gtk.Image.new_from_icon_name(icon_name, Gtk.IconSize.MENU)
        text = Gtk.AccelLabel(label=label)
        text.set_xalign(0)
        text.set_accel_widget(item)
        if keyval is not None:
            text.set_accel(keyval, Gdk.ModifierType(0))
        content.pack_start(icon, False, False, 0)
        content.pack_start(text, True, True, 0)
        item.add(content)
        return item

    def _on_context_view(self, _item: Gtk.MenuItem) -> None:
        """Forward contextual viewing for the focused file."""

        relative = self.context_relative
        if relative:
            self.on_view(relative)

    def _on_context_edit_internal(self, _item: Gtk.MenuItem) -> None:
        """Forward contextual editing to a SLATE editor tab."""

        relative = self.context_relative
        if relative:
            self.on_edit_internal(relative)

    def _on_context_edit_external(self, _item: Gtk.MenuItem) -> None:
        """Forward contextual editing to a separate gVim window."""

        relative = self.context_relative
        if relative:
            self.on_edit_external(relative)

    def _on_context_delete(self, _item: Gtk.MenuItem) -> None:
        """Forward confirmed deletion for the contextual file or directory."""

        relative = self.context_relative
        if relative:
            self.on_delete(relative)

    def _on_context_rename(self, _item: Gtk.MenuItem) -> None:
        """Forward rename for the contextual file or directory."""

        relative = self.context_relative
        if relative:
            self.on_rename(relative)

    def _on_context_open_terminal(self, _item: Gtk.MenuItem) -> None:
        """Request a new project terminal rooted in the contextual directory."""

        relative = self.context_relative
        if relative and self.context_directory:
            self.on_open_terminal(relative)

    def _on_context_new_file(self, _item: Gtk.MenuItem) -> None:
        """Request a new file in the contextual root, directory or file parent."""

        self.on_new_file(self._context_creation_parent())

    def _on_context_new_directory(self, _item: Gtk.MenuItem) -> None:
        """Request a new directory in the contextual target parent."""

        self.on_new_directory(self._context_creation_parent())

    def _context_creation_parent(self) -> str:
        """Return the parent represented by the current contextual target."""

        if self.context_relative is None:
            return ""
        if self.context_directory:
            return self.context_relative
        return os.path.dirname(self.context_relative)

    def _on_new_file_clicked(self, _button: Gtk.Button) -> None:
        """Request an empty file from the current toolbar target."""

        self.on_new_file(self._creation_parent())

    def _on_new_directory_clicked(self, _button: Gtk.Button) -> None:
        """Request a directory from the current toolbar target."""

        self.on_new_directory(self._creation_parent())

    def _on_expand_all_clicked(self, _button: Gtk.Button) -> None:
        """Start recursive expansion or collapse the complete visible hierarchy."""

        if self.expand_all_requested or self.expand_all_complete:
            self.expand_all_requested = False
            self.expand_all_complete = False
            self.expanded_paths.clear()
            self._save_preferences()
            self._rebuild()
            return
        self.expand_all_requested = True
        self._set_expand_action(True)
        self._continue_expand_all()

    def _continue_expand_all(self) -> None:
        """Expand every loaded non-excluded directory until recursion completes."""

        if not self.expand_all_requested:
            return
        pending = False
        for relative, tree_iter in tuple(self.dir_iters.items()):
            if self.store.get_value(tree_iter, self.COL_EXCLUDED):
                continue
            path = self.store.get_path(tree_iter)
            if not self.tree.row_expanded(path):
                self.tree.expand_row(path, False)
            if relative not in self.loaded_paths:
                pending = True
        if pending or self.loading_paths:
            return
        self.expand_all_requested = False
        self.expand_all_complete = True
        self._set_expand_action(True)
        self._save_preferences()

    def _on_filter_toggled(self, _button: Gtk.CheckButton) -> None:
        """Persist visibility filters and rebuild the visible hierarchy."""

        if self.updating_controls:
            return
        self.show_hidden = self.hidden_check.get_active()
        self.show_excluded = self.excluded_check.get_active()
        self._save_preferences()
        self.on_preview(None)
        self._rebuild()

    def _save_preferences(self) -> None:
        """Publish the current per-project browser preferences."""

        if self.project_name is None:
            return
        self.on_preferences(
            {
                "show_hidden": self.show_hidden,
                "show_excluded": self.show_excluded,
                "expanded_paths": sorted(self.expanded_paths),
            }
        )

    def _monitor_directory(self, relative: str, excluded: bool) -> None:
        """Monitor only directories not already covered by the repository watcher."""

        if self.root is None or relative in self.monitors:
            return
        if self.repo_watched and not excluded:
            return
        try:
            monitor = Gio.File.new_for_path(os.path.join(self.root, relative)).monitor_directory(
                Gio.FileMonitorFlags.WATCH_MOVES, None
            )
        except GLib.Error:
            return
        monitor.connect("changed", self._on_supplemental_change)
        self.monitors[relative] = monitor

    def _on_supplemental_change(
        self,
        _monitor: Gio.FileMonitor,
        file: Gio.File,
        _other: Gio.File | None,
        _event_type: Gio.FileMonitorEvent,
    ) -> None:
        """Forward supplemental directory events through the common debounce."""

        path = file.get_path()
        if path and self.root:
            self.filesystem_changed(os.path.relpath(path, self.root).replace(os.sep, "/"))

    def _remove_monitors_below(self, relative: str) -> None:
        """Cancel supplemental monitors owned by one collapsed subtree."""

        for path, monitor in tuple(self.monitors.items()):
            if path == relative or path.startswith(f"{relative}/"):
                monitor.cancel()
                self.monitors.pop(path, None)

    def _clear_monitors(self) -> None:
        """Cancel every supplemental monitor before rebuilding or changing project."""

        for monitor in self.monitors.values():
            monitor.cancel()
        self.monitors.clear()

    def _on_refresh_timeout(self) -> bool:
        """Execute one coalesced filesystem refresh."""

        self.refresh_id = None
        self.refresh()
        return GLib.SOURCE_REMOVE
