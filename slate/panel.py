"""Polished SCM status panel with stable incremental row updates."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, GdkPixbuf, GLib, Gtk, Pango  # noqa: E402

from .scm.base import FileStatus, RepositoryRef, RepositorySyncStatus


@dataclass
class _SCMTreeState:
    """Retain one project's multi-repository GTK model between switches."""

    store: Gtk.TreeStore
    filtered_store: Gtk.TreeModelFilter
    repository_iters: dict[RepositoryRef, Gtk.TreeIter]
    group_iters: dict[tuple[RepositoryRef, str], Gtk.TreeIter]
    status_by_path: dict[str, FileStatus]
    iter_by_path: dict[str, Gtk.TreeIter]
    snapshots: dict[RepositoryRef, tuple[FileStatus, ...]]
    branches: dict[RepositoryRef, str]
    sync_statuses: dict[RepositoryRef, RepositorySyncStatus]
    expanded_rows: set[str] = field(default_factory=set)
    selected_keys: set[str] = field(default_factory=set)
    scroll_value: float = 0.0


class _CommitMessageView(Gtk.TextView):
    """Keep commit text wrapping inside the width assigned by its panel."""

    def do_get_preferred_width(self) -> tuple[int, int]:
        """Avoid promoting the allocated text width to a panel minimum width."""

        # 2026-08-18: GtkTextView can retain its current allocation as its
        # minimum width after layout; a neutral request lets the surrounding
        # panel remain authoritative and makes WORD_CHAR wrap at its edge.
        return 0, 0


class SCMPanel(Gtk.Box):
    """Display normalized repository state and explicit local SCM actions."""

    GROUPS = (
        ("conflict", "Conflicts", "dialog-warning"),
        ("modified", "Modified", "accessories-text-editor"),
        ("moved", "Moved", "go-jump"),
        ("added", "Added", "list-add"),
        ("removed", "Removed", "list-remove"),
        ("untracked", "New", "document-new"),
    )
    ICONS = {state: icon for state, _title, icon in GROUPS}
    GROUP_ORDER = {state: index for index, (state, _title, _icon) in enumerate(GROUPS)}
    COL_TEXT = 0
    COL_STATE = 1
    COL_GROUP = 2
    COL_ICON = 3
    COL_CHECKED = 4
    COL_CHECKABLE = 5
    COL_REPOSITORY = 6
    COL_KIND = 7
    COL_PATH = 8

    def __init__(
        self,
        on_commit: Callable[[str, list[FileStatus]], None],
        on_diff: Callable[[RepositoryRef, Sequence[str]], None],
        on_external: Callable[[RepositoryRef], None],
        on_update: Callable[[RepositoryRef], None],
        on_repository_action: Callable[[str, RepositoryRef], None],
        on_scan: Callable[[], None],
        on_reset: Callable[[], None],
        on_exclude: Callable[[RepositoryRef], None],
        on_preview: Callable[[FileStatus | None], None],
        on_add: Callable[[list[FileStatus]], None],
        on_forget: Callable[[list[FileStatus]], None],
        on_revert: Callable[[list[FileStatus]], None],
        on_view: Callable[[FileStatus], None],
        on_edit_internal: Callable[[FileStatus], None],
        on_edit_external: Callable[[FileStatus], None],
        on_delete: Callable[[FileStatus], None],
    ) -> None:
        """Build repository header, state pages, commit editor and action bar."""

        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.get_style_context().add_class("scm-panel")
        self.on_commit = on_commit
        self.on_diff = on_diff
        self.on_external = on_external
        self.on_update = on_update
        self.on_repository_action = on_repository_action
        self.on_scan = on_scan
        self.on_reset = on_reset
        self.on_exclude = on_exclude
        self.on_preview = on_preview
        self.on_add = on_add
        self.on_forget = on_forget
        self.on_revert = on_revert
        self.on_view = on_view
        self.on_edit_internal = on_edit_internal
        self.on_edit_external = on_edit_external
        self.on_delete = on_delete
        self.status_by_path: dict[str, FileStatus] = {}
        self.iter_by_path: dict[str, Gtk.TreeIter] = {}
        self.checked_paths: set[str] = set()
        self.button_labels: dict[Gtk.Button, Gtk.Label] = {}
        self.button_icons: dict[Gtk.Button, Gtk.Image] = {}
        self.commit_busy = False
        self.updating_select_all = False
        self.reconciling_status = False
        self.expansion_restore_id: int | None = None
        self.context_status: FileStatus | None = None
        self.context_repository: RepositoryRef | None = None
        self.context_add_statuses: list[FileStatus] = []
        self.context_forget_statuses: list[FileStatus] = []
        self.context_checkbox_statuses: list[FileStatus] = []
        self.context_checkbox_checked = False
        self.context_selected_statuses: list[FileStatus] = []
        self.project_states: dict[str, _SCMTreeState] = {}
        self.current_project: str | None = None
        # 2026-08-17: bundled badges guarantee distinct colored repository
        # identities instead of falling back to a monochrome generic folder.
        self.repository_icons = {
            scm_type: GdkPixbuf.Pixbuf.new_from_file_at_scale(
                str(Path(__file__).with_name(f"scm-{scm_type}.svg")),
                16,
                16,
                True,
            )
            for scm_type in ("git", "hg")
        }

        self._build_header()
        self._build_error_bar()
        self._build_state_stack()
        self._build_tool_actions()
        self._build_commit_section()
        self._build_commit_action()
        self.set_supported(False)

    def _build_header(self) -> None:
        """Create repository controls for the shared select-all toolbar."""

        self.scan_button = self._action_button(
            "Scan", "view-refresh", self._on_scan_clicked
        )
        self.scan_button.set_tooltip_text("Find HG and Git repositories in the project")
        self.reset_button = self._action_button(
            "Reset", "edit-clear", self._on_reset_clicked
        )
        self.reset_button.set_tooltip_text(
            "Forget cache and exclusions, then scan again"
        )

    def _build_error_bar(self) -> None:
        """Create a dismissible non-modal area for repository errors."""

        self.error_bar = Gtk.InfoBar()
        self.error_bar.set_message_type(Gtk.MessageType.ERROR)
        self.error_bar.set_show_close_button(True)
        self.error_bar.connect("response", self._on_error_response)
        self.error_label = Gtk.Label()
        self.error_label.set_xalign(0)
        self.error_label.set_line_wrap(True)
        self.error_bar.get_content_area().add(self.error_label)
        self.error_bar.set_no_show_all(True)
        self.pack_start(self.error_bar, False, False, 0)

    def _build_state_stack(self) -> None:
        """Create unsupported, loading, clean and changed repository pages."""

        self.state_stack = Gtk.Stack()
        self.state_stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.state_stack.set_transition_duration(120)
        self.state_stack.add_named(
            self._empty_state(
                "folder-remote-symbolic",
                "No supported repository",
                "The terminal remains available, but no watcher is started.",
            ),
            "unsupported",
        )
        self.state_stack.add_named(
            self._loading_state(),
            "loading",
        )
        self.state_stack.add_named(
            self._empty_state(
                "emblem-ok-symbolic",
                "Clean working copy",
                "No changes to review.",
            ),
            "clean",
        )

        self._attach_status_model(self._new_status_model())
        self.tree = Gtk.TreeView(model=self.filtered_store)
        self.tree.set_headers_visible(False)
        # 2026-08-16: le azioni V/M/E/A/Spazio appartengono al pannello; anche
        # gli altri caratteri non devono attivare la ricerca incrementale GTK.
        self.tree.set_enable_search(False)
        self.tree.set_tooltip_column(self.COL_TEXT)
        self.tree.get_style_context().add_class("scm-tree")
        selection = self.tree.get_selection()
        # 2026-08-16: Ctrl/Shift selezionano sottoinsiemi di file nuovi per hg
        # add; Commit e Ripristina continuano a usare soltanto le checkbox.
        selection.set_mode(Gtk.SelectionMode.MULTIPLE)
        self.tree.connect("cursor-changed", self._on_tree_cursor_changed)
        self.tree.connect("row-activated", self._on_row_activated)
        self.tree.connect("row-expanded", self._on_revision_row_expanded)
        self.tree.connect("row-collapsed", self._on_revision_row_collapsed)
        self.tree.connect("map", self._on_revision_tree_mapped)
        self.tree.connect("button-press-event", self._on_tree_button)
        self.tree.connect("popup-menu", self._on_tree_popup_menu)
        self.tree.connect("key-press-event", self._on_tree_key_press)

        self.toggle_renderer = Gtk.CellRendererToggle()
        self.toggle_renderer.connect("toggled", self._on_status_toggled)
        icon_renderer = Gtk.CellRendererPixbuf()
        icon_renderer.set_property("xpad", 2)
        self.text_renderer = Gtk.CellRendererText()
        self.text_renderer.set_property("xpad", 2)
        self.text_renderer.set_property("ellipsize", Pango.EllipsizeMode.END)
        column = Gtk.TreeViewColumn("Revisions")
        # 2026-08-16: la checkbox resta nella colonna gerarchica originale; si
        # riduce soltanto l'expander via CSS per non alterare allineamento e hitbox.
        column.pack_start(self.toggle_renderer, False)
        column.pack_start(icon_renderer, False)
        column.pack_start(self.text_renderer, True)
        column.add_attribute(self.toggle_renderer, "active", self.COL_CHECKED)
        column.add_attribute(self.toggle_renderer, "visible", self.COL_CHECKABLE)
        column.set_cell_data_func(icon_renderer, self._render_status_icon)
        column.set_cell_data_func(self.text_renderer, self._render_status_text)
        column.set_expand(True)
        self.tree.append_column(column)
        self.tree.set_expander_column(column)

        self.select_all_bar = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=0
        )
        self.select_all_bar.set_no_show_all(True)
        self.select_all_bar.get_style_context().add_class("scm-select-all")
        self.select_all_check = Gtk.CheckButton(label="Select all")
        self.select_all_check.connect("toggled", self._on_select_all_toggled)
        self.select_all_bar.pack_start(self.select_all_check, False, False, 0)
        self.repository_actions = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=8
        )
        self.repository_actions.pack_start(self.scan_button, False, False, 0)
        self.repository_actions.pack_start(self.reset_button, False, False, 0)
        # 2026-08-17: selezione e manutenzione repository condividono l'unica
        # riga richiesta; il box finale tiene Scansiona/Reset allineati a destra.
        self.select_all_bar.pack_end(
            self.repository_actions, False, False, 0
        )
        self.tree_scroller = Gtk.ScrolledWindow()
        self.tree_scroller.set_policy(
            Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC
        )
        self.tree_scroller.add(self.tree)
        changes = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        changes.pack_start(self.select_all_bar, False, False, 0)
        changes.pack_start(self.tree_scroller, True, True, 0)
        self.state_stack.add_named(changes, "changes")
        # 2026-08-16: Gtk.Stack può selezionare soltanto child già marcati
        # visibili; prepararli qui evita uno stato transitorio senza pagina.
        self.state_stack.show_all()
        self.pack_start(self.state_stack, True, True, 0)

    def _new_status_model(self) -> _SCMTreeState:
        """Create one independent project model ready for repository roots."""

        store = Gtk.TreeStore(str, str, bool, str, bool, bool, object, str, str)
        store.set_default_sort_func(self._compare_status_rows, None)
        store.set_sort_column_id(
            Gtk.TREE_SORTABLE_DEFAULT_SORT_COLUMN_ID,
            Gtk.SortType.ASCENDING,
        )
        filtered_store = store.filter_new()
        filtered_store.set_visible_func(self._row_visible)
        return _SCMTreeState(filtered_store=filtered_store, store=store,
            repository_iters={}, group_iters={}, status_by_path={}, iter_by_path={},
            snapshots={}, branches={}, sync_statuses={})

    def _compare_status_rows(
        self,
        model: Gtk.TreeModel,
        first: Gtk.TreeIter,
        second: Gtk.TreeIter,
        _data: object,
    ) -> int:
        """Order repositories and files alphabetically while retaining group order."""

        def _row_key(tree_iter: Gtk.TreeIter) -> tuple[int, object, str]:
            """Build one deterministic sibling key from the row's semantic kind."""

            kind = model.get_value(tree_iter, self.COL_KIND)
            repository = model.get_value(tree_iter, self.COL_REPOSITORY)
            text = model.get_value(tree_iter, self.COL_TEXT)
            if kind == "repository":
                # 2026-08-17: discovery timing must not determine presentation;
                # root remains first, then repository paths sort naturally.
                path_key = "" if repository.path == "." else repository.path.casefold()
                return (0, path_key, f"{repository.path}\0{repository.scm_type}")
            if kind == "group":
                state = model.get_value(tree_iter, self.COL_STATE)
                return (1, self.GROUP_ORDER.get(state, len(self.GROUPS)), state)
            return (2, text.casefold(), text)

        first_key = _row_key(first)
        second_key = _row_key(second)
        return (first_key > second_key) - (first_key < second_key)

    def _attach_status_model(self, state: _SCMTreeState) -> None:
        """Make one cached repository model the target of panel operations."""

        self.store = state.store
        self.filtered_store = state.filtered_store
        self.repository_iters = state.repository_iters
        self.group_iters = state.group_iters
        self.status_by_path = state.status_by_path
        self.iter_by_path = state.iter_by_path
        self.current_state = state
        if hasattr(self, "tree"):
            previous_reconciling = self.reconciling_status
            self.reconciling_status = True
            try:
                self.tree.set_model(self.filtered_store)
                self._restore_tree_view_state()
            finally:
                self.reconciling_status = previous_reconciling
            self._schedule_revision_expansion_restore()

    def bind_project(self, project_name: str, supported: bool) -> None:
        """Switch repositories by attaching cached rows instead of rebuilding them."""

        if self.current_project is not None:
            self._capture_tree_view_state()
            self.project_states[self.current_project] = self.current_state
        state = self.project_states.get(project_name)
        if state is None:
            state = self._new_status_model()
            if supported:
                self.project_states[project_name] = state
        self._attach_status_model(state)
        self.current_project = project_name
        # 2026-08-17: migliaia di righe SCM restano associate al loro progetto;
        # lo switch cambia soltanto modello e non distrugge widget riga per riga.
        self.set_supported(supported)

    def _capture_tree_view_state(self) -> None:
        """Store row selection and scroll before changing project model."""

        if not hasattr(self, "tree"):
            return
        # 2026-08-17: expansion is updated only by real input-event callbacks;
        # sampling GTK here would turn an automatic layout collapse into state.
        self.current_state.selected_keys = {
            self._status_key(status) for status in self.selected_statuses()
        }
        self.current_state.scroll_value = (
            self.tree_scroller.get_vadjustment().get_value()
        )

    def _restore_tree_view_state(self) -> None:
        """Restore cached expansion, selection and scroll on the attached model."""

        for repository, source_iter in self.repository_iters.items():
            identity = self._repository_identity(repository)
            self._restore_expanded_iter(source_iter, f"repository:{identity}")
            for state, _title, _icon in self.GROUPS:
                group_iter = self.group_iters.get((repository, state))
                if group_iter is not None:
                    self._restore_expanded_iter(
                        group_iter, f"group:{identity}\0{state}"
                    )
        selection = self.tree.get_selection()
        selection.unselect_all()
        for key in self.current_state.selected_keys:
            source_iter = self.iter_by_path.get(key)
            if source_iter is None:
                continue
            filtered_path = self.filtered_store.convert_child_path_to_path(
                self.store.get_path(source_iter)
            )
            if filtered_path is not None:
                selection.select_path(filtered_path)
        adjustment = self.tree_scroller.get_vadjustment()
        maximum = max(adjustment.get_lower(), adjustment.get_upper() - adjustment.get_page_size())
        adjustment.set_value(min(maximum, self.current_state.scroll_value))

    def _restore_expanded_iter(self, source_iter: Gtk.TreeIter, identity: str) -> None:
        """Expand one cached source row when it remains visible after filtering."""

        if identity not in self.current_state.expanded_rows:
            return
        filtered_path = self.filtered_store.convert_child_path_to_path(
            self.store.get_path(source_iter)
        )
        if filtered_path is not None:
            self.tree.expand_row(filtered_path, False)

    def _row_identity(
        self, model: Gtk.TreeModel, tree_iter: Gtk.TreeIter
    ) -> str | None:
        """Return a stable identity for an expandable repository or group row."""

        repository = model.get_value(tree_iter, self.COL_REPOSITORY)
        kind = model.get_value(tree_iter, self.COL_KIND)
        identity = self._repository_identity(repository)
        if kind == "repository":
            return f"repository:{identity}"
        if kind == "group":
            return f"group:{identity}\0{model.get_value(tree_iter, self.COL_STATE)}"
        return None

    @staticmethod
    def _repository_identity(repository: RepositoryRef) -> str:
        """Return one stable identity for expansion and model caches."""

        # 2026-08-17: preserve the established HG identity while namespacing Git
        # rows that may legally use the same project-relative path.
        return (
            repository.path
            if repository.scm_type == "hg"
            else f"{repository.scm_type}\0{repository.path}"
        )

    def forget_project(self, project_name: str) -> None:
        """Release a cached SCM model after project removal."""

        self.project_states.pop(project_name, None)

    def set_repositories(
        self,
        repositories: Sequence[RepositoryRef],
        scanning: bool = False,
    ) -> None:
        """Reconcile persistent repository roots even when they contain no changes."""

        incoming = set(repositories)
        for repository in tuple(self.repository_iters):
            if repository not in incoming:
                self._remove_repository(repository)
        for repository in repositories:
            self._ensure_repository(repository)
        for repository in repositories:
            self._update_repository_label(repository)
        self.scan_button.set_sensitive(not scanning)
        self.reset_button.set_sensitive(not scanning)
        if repositories:
            self._update_summary()
        elif scanning:
            self.set_loading()
        else:
            self.set_supported(False)

    def _ensure_repository(self, repository: RepositoryRef) -> Gtk.TreeIter:
        """Create one repository node and its filtered status groups if absent."""

        existing = self.repository_iters.get(repository)
        if existing is not None:
            return existing
        # 2026-08-17: the repository is a persistent root rather than a status
        # group, therefore a clean working copy remains visible and actionable.
        label = self.current_project or repository.scm_type.upper()
        if repository.path != ".":
            label = repository.path
        repository_iter = self.store.append(
            None,
            [
                label,
                "",
                True,
                repository.scm_type,
                False,
                False,
                repository,
                "repository",
                "",
            ],
        )
        self.repository_iters[repository] = repository_iter
        # 2026-08-17: Revisioni nasce completamente espanso; le successive
        # chiusure manuali rimuovono queste identità e restano persistenti.
        identity = self._repository_identity(repository)
        self.current_state.expanded_rows.add(f"repository:{identity}")
        for state, title, icon in self.GROUPS:
            self.group_iters[(repository, state)] = self.store.append(
                repository_iter,
                [title, state, True, icon, False, False, repository, "group", ""],
            )
            self.current_state.expanded_rows.add(f"group:{identity}\0{state}")
        self.filtered_store.refilter()
        self._restore_repository_expansion(repository)
        return repository_iter

    def _remove_repository(self, repository: RepositoryRef) -> None:
        """Remove one repository root and every cached status owned by it."""

        for key, status in tuple(self.status_by_path.items()):
            if (
                status.repository == repository.path
                and status.scm_type == repository.scm_type
            ):
                self.status_by_path.pop(key, None)
                self.iter_by_path.pop(key, None)
                self.checked_paths.discard(key)
                self.current_state.selected_keys.discard(key)
        tree_iter = self.repository_iters.pop(repository, None)
        if tree_iter is not None:
            self.store.remove(tree_iter)
        for state, _title, _icon in self.GROUPS:
            self.group_iters.pop((repository, state), None)
        self.current_state.snapshots.pop(repository, None)
        self.current_state.branches.pop(repository, None)
        self.current_state.sync_statuses.pop(repository, None)
        repository_identity = self._repository_identity(repository)
        self.current_state.expanded_rows = {
            row_identity
            for row_identity in self.current_state.expanded_rows
            if row_identity != f"repository:{repository_identity}"
            and not row_identity.startswith(f"group:{repository_identity}\0")
        }
        self.filtered_store.refilter()

    def set_font_size(self, points: int) -> None:
        """Set the revision-tree text size in typographic points."""

        self.text_renderer.set_property("size-points", float(points))
        # Il renderer non invalida da solo le righe già disegnate: senza queste
        # richieste GTK applica il font soltanto durante ridisegni successivi.
        self.tree.queue_resize()
        self.tree.queue_draw()

    def _build_commit_section(self) -> None:
        """Create the labeled multiline commit-message editor."""

        self.commit_section = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=6
        )
        self.commit_section.set_no_show_all(True)
        self.commit_section.get_style_context().add_class("commit-section")
        label = Gtk.Label(label="COMMIT MESSAGE")
        label.set_xalign(0)
        label.get_style_context().add_class("section-title")
        self.commit_section.pack_start(label, False, False, 0)
        self.message = _CommitMessageView()
        self.message.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        # 2026-08-16: nel messaggio di commit Tab serve alla navigazione della
        # finestra; l'indentazione con tabulazioni non è un caso d'uso utile.
        self.message.set_accepts_tab(False)
        self.message.set_tooltip_text("Commit message")
        self.message.get_buffer().connect("changed", self._on_message_changed)
        self.message.connect("key-press-event", self._on_message_key_press)
        message_scroller = Gtk.ScrolledWindow()
        message_scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        message_scroller.set_size_request(-1, 88)
        message_scroller.get_style_context().add_class("commit-editor")
        message_scroller.add(self.message)
        self.commit_section.pack_start(message_scroller, False, True, 0)
        self.pack_start(self.commit_section, False, True, 0)

    def _build_tool_actions(self) -> None:
        """Place aggregate add and revert tools before the commit editor."""

        self.tool_actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.tool_actions.set_no_show_all(True)
        self.tool_actions.get_style_context().add_class("scm-actions")
        self.add_new_button = self._action_button(
            "Add new", "emblem-default-symbolic", self._on_add_new_clicked
        )
        self.revert_button = self._action_button(
            "Revert selected",
            "edit-undo-symbolic",
            self._on_revert_checked_clicked,
        )
        for button in (
            self.add_new_button,
            self.revert_button,
        ):
            button.set_no_show_all(True)
        self.tool_actions.pack_start(self.add_new_button, False, False, 0)
        self.tool_actions.pack_start(self.revert_button, False, False, 0)
        self.pack_start(self.tool_actions, False, False, 0)

    def _build_commit_action(self) -> None:
        """Place a duplicate master checkbox beside the final Commit action."""

        self.commit_actions = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=6
        )
        self.commit_actions.set_no_show_all(True)
        self.commit_actions.get_style_context().add_class("scm-actions")
        # 2026-08-16: duplicare il controllo vicino a Commit accorcia il flusso
        # operativo senza rimuovere quello utile durante la revisione in lista.
        self.commit_select_all_check = Gtk.CheckButton(label="Select all")
        self.commit_select_all_check.set_no_show_all(True)
        self.commit_select_all_check.connect("toggled", self._on_select_all_toggled)
        self.commit_button = self._action_button(
            "Commit", "document-save-symbolic", self._on_commit_clicked
        )
        self.commit_button.set_tooltip_text("Commit selected files (Ctrl+Enter)")
        self.commit_button.set_no_show_all(True)
        self.commit_shortcut_label = Gtk.Label(label="Ctrl+Enter")
        self.commit_shortcut_label.get_style_context().add_class("commit-shortcut")
        self.commit_actions.pack_start(
            self.commit_select_all_check, False, False, 0
        )
        self.commit_actions.pack_end(self.commit_button, False, False, 0)
        self.commit_actions.pack_end(self.commit_shortcut_label, False, False, 0)
        self.pack_start(self.commit_actions, False, False, 0)

    def _empty_state(self, icon_name: str, title: str, detail: str) -> Gtk.Widget:
        """Create a centered accessible state page with themed icon and copy."""

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_halign(Gtk.Align.CENTER)
        box.set_valign(Gtk.Align.CENTER)
        box.set_border_width(18)
        icon = Gtk.Image.new_from_icon_name(icon_name, Gtk.IconSize.DIALOG)
        title_label = Gtk.Label(label=title)
        title_label.get_style_context().add_class("empty-state-title")
        detail_label = Gtk.Label(label=detail)
        detail_label.set_line_wrap(True)
        detail_label.set_justify(Gtk.Justification.CENTER)
        detail_label.get_style_context().add_class("empty-state-detail")
        box.pack_start(icon, False, False, 0)
        box.pack_start(title_label, False, False, 0)
        box.pack_start(detail_label, False, False, 0)
        return box

    def _loading_state(self) -> Gtk.Widget:
        """Create the initial asynchronous repository-loading state."""

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_halign(Gtk.Align.CENTER)
        box.set_valign(Gtk.Align.CENTER)
        spinner = Gtk.Spinner()
        spinner.start()
        label = Gtk.Label(label="Reading repository status…")
        label.get_style_context().add_class("empty-state-detail")
        box.pack_start(spinner, False, False, 0)
        box.pack_start(label, False, False, 0)
        return box

    def _action_button(
        self, label: str, icon_name: str, callback: Callable[[Gtk.Button], None]
    ) -> Gtk.Button:
        """Create an icon-and-label button with the required four-pixel gap."""

        button = Gtk.Button()
        content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        icon = Gtk.Image.new_from_icon_name(icon_name, Gtk.IconSize.BUTTON)
        label_widget = Gtk.Label(label=label)
        content.pack_start(icon, False, False, 0)
        content.pack_start(label_widget, False, False, 0)
        button.add(content)
        button.get_accessible().set_name(label)
        self.button_labels[button] = label_widget
        self.button_icons[button] = icon
        button.connect("clicked", callback)
        return button

    def _set_action_label(self, button: Gtk.Button, text: str) -> None:
        """Update a custom spaced button label and its accessible name together."""

        self.button_labels[button].set_text(text)
        button.get_accessible().set_name(text)

    def set_supported(self, supported: bool) -> None:
        """Switch between no repositories and asynchronous discovery states."""

        self.clear_error()
        if supported:
            self.state_stack.set_visible_child_name("changes")
            self.tool_actions.show()
            self.add_new_button.hide()
            self.revert_button.hide()
            self.commit_actions.hide()
            self.commit_button.hide()
        else:
            self._clear_rows()
            self.state_stack.set_visible_child_name("unsupported")
            self.commit_section.hide()
            self.tool_actions.hide()
            self.commit_actions.hide()

    def set_loading(self) -> None:
        """Show discovery progress when no cached repository is available."""

        self.state_stack.set_visible_child_name("loading")
        self.commit_section.hide()
        self.add_new_button.hide()
        self.revert_button.hide()
        self.commit_actions.hide()
        self.commit_button.hide()
        self.tool_actions.hide()

    def update_status(
        self,
        statuses: list[FileStatus],
        branch: str,
        repository: RepositoryRef = RepositoryRef(".", "hg"),
    ) -> None:
        """Apply one repository snapshot inside the active project's tree."""

        self._ensure_repository(repository)
        qualified = [
            status
            if (
                status.repository == repository.path
                and status.scm_type == repository.scm_type
            )
            else FileStatus(
                status.path,
                status.state,
                status.staged,
                repository.path,
                status.source_path,
                repository.scm_type,
            )
            for status in statuses
        ]
        snapshot = tuple(qualified)
        if self.current_state.snapshots.get(repository) != snapshot:
            self.reconciling_status = True
            try:
                self._reconcile_status_rows(repository, qualified)
            finally:
                self.reconciling_status = False
            self.current_state.snapshots[repository] = snapshot
        self.current_state.branches[repository] = branch
        self._update_repository_label(repository)
        self._update_summary()
        self._schedule_revision_expansion_restore()

    def _update_summary(self) -> None:
        """Update aggregate labels and actions without hiding empty repositories."""

        new_count = sum(
            status.state == "untracked" for status in self.status_by_path.values()
        )
        self.state_stack.set_visible_child_name(
            "changes" if self.repository_iters else "unsupported"
        )
        self._show_conditional(self.commit_section)
        self._show_conditional(self.tool_actions)
        self._show_conditional(self.add_new_button)
        self._show_conditional(self.revert_button)
        self._show_conditional(self.commit_actions)
        self._show_conditional(self.commit_button)
        self._set_action_label(
            self.add_new_button,
            f"Add new ({new_count})" if new_count else "Add new",
        )
        self.add_new_button.set_sensitive(new_count > 0)
        self._sync_checked_actions()

    def _update_repository_label(self, repository: RepositoryRef) -> None:
        """Render repository path, branch and explicit remote state."""

        tree_iter = self.repository_iters.get(repository)
        if tree_iter is None:
            return
        if repository.path == ".":
            base = "[root]"
        else:
            base = repository.path
        branch = self.current_state.branches.get(repository, "")
        suffix = f" — {branch}" if branch else ""
        # 2026-08-19: the explicit result lives beside the cached GTK model so
        # project switches retain it without writing remote state to config.
        sync_status = self.current_state.sync_statuses.get(
            repository, RepositorySyncStatus()
        )
        if sync_status.state == "synced":
            remote = "remote: up to date"
        elif sync_status.state == "ahead":
            remote = f"remote: ahead {sync_status.ahead}"
        elif sync_status.state == "behind":
            remote = f"remote: behind {sync_status.behind}"
        elif sync_status.state == "diverged":
            remote = (
                f"remote: diverged · ahead {sync_status.ahead}, "
                f"behind {sync_status.behind}"
            )
        elif sync_status.state == "local":
            remote = "local repository"
        elif sync_status.state == "unconfigured":
            remote = "upstream not configured"
        elif sync_status.state == "detached":
            remote = "detached HEAD"
        elif sync_status.state == "access_required":
            remote = "remote access required"
        else:
            remote = "remote: not verified"
        self.store.set_value(
            tree_iter, self.COL_TEXT, f"{base}{suffix} — {remote}"
        )

    def set_remote_status(
        self,
        repository: RepositoryRef,
        status: RepositorySyncStatus,
    ) -> None:
        """Update one repository's explicit remote result without rebuilding rows."""

        self._ensure_repository(repository)
        self.current_state.sync_statuses[repository] = status
        self._update_repository_label(repository)

    def set_project_remote_status(
        self,
        project_name: str,
        repository: RepositoryRef,
        status: RepositorySyncStatus,
    ) -> None:
        """Update remote state in either the visible or an inactive cached model."""

        if project_name == self.current_project:
            self.set_remote_status(repository, status)
            return
        state = self.project_states.get(project_name)
        if state is not None:
            # 2026-08-19: inactive models have no visible row to redraw; the
            # normal project binding will render this invalidation on return.
            state.sync_statuses[repository] = status

    def _reconcile_status_rows(
        self, repository: RepositoryRef, statuses: list[FileStatus]
    ) -> None:
        """Diff one repository's rows while suppressing preview side effects."""

        incoming = {self._status_key(status): status for status in statuses}
        existing = {
            key
            for key, status in self.status_by_path.items()
            if status.repository == repository.path
            and status.scm_type == repository.scm_type
        }
        for key in existing - set(incoming):
            tree_iter = self.iter_by_path.pop(key)
            self.store.remove(tree_iter)
            self.status_by_path.pop(key, None)
            self.checked_paths.discard(key)
        for key, status in incoming.items():
            old = self.status_by_path.get(key)
            if old is None:
                self.iter_by_path[key] = self.store.append(
                    self.group_iters[(repository, status.state)],
                    [
                        self._status_label(status),
                        status.state,
                        False,
                        self.ICONS[status.state],
                        key in self.checked_paths,
                        status.state != "untracked",
                        repository,
                        "file",
                        status.path,
                    ],
                )
            elif old != status:
                tree_iter = self.iter_by_path[key]
                if old.state != status.state:
                    if status.state == "untracked":
                        self.checked_paths.discard(key)
                    self.store.remove(tree_iter)
                    tree_iter = self.store.append(
                        self.group_iters[(repository, status.state)],
                        [
                            self._status_label(status),
                            status.state,
                            False,
                            self.ICONS[status.state],
                            key in self.checked_paths,
                            status.state != "untracked",
                            repository,
                            "file",
                            status.path,
                        ],
                    )
                    self.iter_by_path[key] = tree_iter
                else:
                    self.store.set(
                        tree_iter,
                        self.COL_TEXT,
                        self._status_label(status),
                        self.COL_STATE,
                        status.state,
                        self.COL_ICON,
                        self.ICONS[status.state],
                        self.COL_PATH,
                        status.path,
                    )
            self.status_by_path[key] = status
        self._update_group_labels(repository)
        self.filtered_store.refilter()
        self._restore_repository_expansion(repository)

    @staticmethod
    def _status_key(status: FileStatus) -> str:
        """Build a collision-free key across paths, repositories and SCMs."""

        if status.scm_type == "hg":
            return (
                status.path
                if status.repository == "."
                else f"{status.repository}\0{status.path}"
            )
        return f"{status.scm_type}\0{status.repository}\0{status.path}"

    @staticmethod
    def _status_label(status: FileStatus) -> str:
        """Return the visible path label, including both endpoints of a move."""

        if status.state == "moved" and status.source_path:
            return f"{status.source_path} → {status.path}"
        return status.path

    def show_error(self, message: str) -> None:
        """Display a dismissible non-modal error without discarding model state."""

        self.error_label.set_text(message)
        self._show_conditional(self.error_bar)

    def clear_error(self) -> None:
        """Hide and clear the previous repository error."""

        self.error_label.set_text("")
        self.error_bar.hide()

    def selected_statuses(self) -> list[FileStatus]:
        """Return selected file rows while excluding filtered group headers."""

        model, paths = self.tree.get_selection().get_selected_rows()
        selected: list[FileStatus] = []
        for tree_path in paths:
            tree_iter = model.get_iter(tree_path)
            if model.get_value(tree_iter, self.COL_KIND) == "file":
                path = model.get_value(tree_iter, self.COL_PATH)
                repository = model.get_value(tree_iter, self.COL_REPOSITORY)
                key = self._status_key(self._status_from_row(path, repository))
                if key in self.status_by_path:
                    selected.append(self.status_by_path[key])
        return selected

    def checked_statuses(self) -> list[FileStatus]:
        """Return explicitly checked tracked files in the visible status order."""

        return [
            status
            for key, status in self.status_by_path.items()
            if key in self.checked_paths and status.state != "untracked"
        ]

    def message_text(self) -> str:
        """Return the trimmed commit message from the text buffer."""

        buffer = self.message.get_buffer()
        return buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True).strip()

    def clear_message(self) -> None:
        """Clear commit text when the active terminal or project changes."""

        self.message.get_buffer().set_text("")

    def set_commit_busy(self, busy: bool) -> None:
        """Prevent message changes from re-enabling a commit already in progress."""

        self.commit_busy = busy
        self._sync_checked_actions()

    def uncheck_statuses(self, statuses: Sequence[FileStatus]) -> None:
        """Clear only successful operation targets after a multi-repository commit."""

        for status in statuses:
            key = self._status_key(status)
            self.checked_paths.discard(key)
            tree_iter = self.iter_by_path.get(key)
            if tree_iter is not None:
                self.store.set_value(tree_iter, self.COL_CHECKED, False)
        self._sync_checked_actions()

    def _clear_rows(self) -> None:
        """Remove file rows incrementally while retaining repository roots."""

        previous_reconciling = self.reconciling_status
        self.reconciling_status = True
        try:
            for path, tree_iter in tuple(self.iter_by_path.items()):
                self.store.remove(tree_iter)
                self.iter_by_path.pop(path, None)
            self.status_by_path.clear()
            self.checked_paths.clear()
            for repository in self.repository_iters:
                self._update_group_labels(repository)
            self.filtered_store.refilter()
        finally:
            self.reconciling_status = previous_reconciling

    @staticmethod
    def _show_conditional(widget: Gtk.Widget) -> None:
        """Show a conditional widget and its children without changing future policy."""

        widget.set_no_show_all(False)
        widget.show_all()
        widget.set_no_show_all(True)

    def _update_group_labels(self, repository: RepositoryRef) -> None:
        """Update one repository's group counts before the model refilters."""

        for state, title, _icon in self.GROUPS:
            count = sum(
                1
                for item in self.status_by_path.values()
                if item.repository == repository.path
                and item.scm_type == repository.scm_type
                and item.state == state
            )
            self.store.set_value(
                self.group_iters[(repository, state)],
                self.COL_TEXT,
                f"{title}:  {count}",
            )

    def _restore_repository_expansion(self, repository: RepositoryRef) -> None:
        """Apply cached/default expansion to one repository's visible rows."""

        source_iter = self.repository_iters.get(repository)
        if source_iter is None:
            return
        identity = self._repository_identity(repository)
        self._restore_expanded_iter(source_iter, f"repository:{identity}")
        for state, _title, _icon in self.GROUPS:
            group_iter = self.group_iters.get((repository, state))
            if group_iter is not None:
                self._restore_expanded_iter(
                    group_iter, f"group:{identity}\0{state}"
                )

    def _schedule_revision_expansion_restore(self) -> None:
        """Reapply default expansion after GTK finishes filtering and layout."""

        if self.tree.get_mapped() and self.expansion_restore_id is None:
            self.expansion_restore_id = GLib.idle_add(
                self._restore_revision_expansion_idle
            )

    def _on_revision_tree_mapped(self, _tree: Gtk.TreeView) -> None:
        """Schedule startup expansion when the filtered tree first becomes visible."""

        self._schedule_revision_expansion_restore()

    def _restore_revision_expansion_idle(self) -> bool:
        """Expand default rows once they have stable filtered model paths."""

        self.expansion_restore_id = None
        previous_reconciling = self.reconciling_status
        self.reconciling_status = True
        try:
            for repository in self.repository_iters:
                self._restore_repository_expansion(repository)
        finally:
            self.reconciling_status = previous_reconciling
        return GLib.SOURCE_REMOVE

    def _row_visible(
        self, model: Gtk.TreeModel, tree_iter: Gtk.TreeIter, _data: object = None
    ) -> bool:
        """Keep repository roots visible while filtering empty status groups."""

        if model.get_value(tree_iter, self.COL_KIND) in {"repository", "file"}:
            return True
        return model.iter_has_child(tree_iter)

    def _render_status_text(
        self,
        _column: Gtk.TreeViewColumn,
        renderer: Gtk.CellRendererText,
        model: Gtk.TreeModel,
        tree_iter: Gtk.TreeIter,
        _data: object = None,
    ) -> None:
        """Differentiate group labels from paths without hardcoded colors."""

        kind = model.get_value(tree_iter, self.COL_KIND)
        repository = model.get_value(tree_iter, self.COL_REPOSITORY)
        repository_changed = kind == "repository" and any(
            status.repository == repository.path
            and status.scm_type == repository.scm_type
            for status in self.status_by_path.values()
        )
        emphasized = kind == "group" or repository_changed
        renderer.set_property("text", model.get_value(tree_iter, self.COL_TEXT))
        renderer.set_property(
            "weight", Pango.Weight.BOLD if emphasized else Pango.Weight.NORMAL
        )
        renderer.set_property("weight-set", True)

    def _render_status_icon(
        self,
        _column: Gtk.TreeViewColumn,
        renderer: Gtk.CellRendererPixbuf,
        model: Gtk.TreeModel,
        tree_iter: Gtk.TreeIter,
        _data: object = None,
    ) -> None:
        """Render colored SCM badges on roots and themed icons on child rows."""

        kind = model.get_value(tree_iter, self.COL_KIND)
        repository = model.get_value(tree_iter, self.COL_REPOSITORY)
        if kind == "repository":
            renderer.set_property("icon-name", None)
            renderer.set_property("pixbuf", self.repository_icons[repository.scm_type])
            return
        renderer.set_property("pixbuf", None)
        renderer.set_property("icon-name", model.get_value(tree_iter, self.COL_ICON))

    @staticmethod
    def _status_from_row(path: str, repository: RepositoryRef) -> FileStatus:
        """Build the typed identity used to look up one status model row."""

        return FileStatus(path, "", False, repository.path, None, repository.scm_type)

    def _sync_checked_actions(self) -> None:
        """Enable destructive actions only for explicitly checked tracked paths."""

        checked = self.checked_statuses()
        # 2026-08-16: visibilità e righe già distinguono un repository valido;
        # un secondo flag creava una sorgente di verità incoerente per le azioni.
        enabled = bool(checked) and bool(self.message_text()) and not self.commit_busy
        self.commit_button.set_sensitive(enabled)
        self.revert_button.set_sensitive(bool(checked))
        self._sync_select_all()

    def _on_message_changed(self, _buffer: Gtk.TextBuffer) -> None:
        """Re-evaluate the commit action whenever its required message changes."""

        self._sync_checked_actions()

    def _on_status_toggled(
        self, _renderer: Gtk.CellRendererToggle, path_text: str
    ) -> None:
        """Toggle one explicit operation checkbox without changing preview selection."""

        filtered_path = Gtk.TreePath.new_from_string(path_text)
        filtered_iter = self.filtered_store.get_iter(filtered_path)
        if not self.filtered_store.get_value(filtered_iter, self.COL_CHECKABLE):
            return
        path = self.filtered_store.get_value(filtered_iter, self.COL_PATH)
        repository = self.filtered_store.get_value(
            filtered_iter, self.COL_REPOSITORY
        )
        key = self._status_key(self._status_from_row(path, repository))
        checked = key not in self.checked_paths
        if checked:
            self.checked_paths.add(key)
        else:
            self.checked_paths.discard(key)
        source_iter = self.iter_by_path[key]
        self.store.set_value(source_iter, self.COL_CHECKED, checked)
        self._sync_checked_actions()

    def _sync_select_all(self) -> None:
        """Reflect tracked-file selection on both synchronized master controls."""

        tracked_paths = {
            path
            for path, status in self.status_by_path.items()
            if status.state != "untracked"
        }
        controls = (self.select_all_check, self.commit_select_all_check)
        self.updating_select_all = True
        try:
            if not tracked_paths:
                self._show_conditional(self.select_all_bar)
                for control in controls:
                    control.set_inconsistent(False)
                    control.set_active(False)
                    control.hide()
                return
            checked_count = len(tracked_paths & self.checked_paths)
            for control in controls:
                control.set_inconsistent(0 < checked_count < len(tracked_paths))
                control.set_active(checked_count == len(tracked_paths))
                self._show_conditional(control)
            self._show_conditional(self.select_all_bar)
        finally:
            self.updating_select_all = False

    def _on_select_all_toggled(self, button: Gtk.CheckButton) -> None:
        """Set every tracked file checkbox from the explicit master control."""

        if self.updating_select_all:
            return
        checked = button.get_active()
        for path, status in self.status_by_path.items():
            if status.state == "untracked":
                continue
            if checked:
                self.checked_paths.add(path)
            else:
                self.checked_paths.discard(path)
            self.store.set_value(
                self.iter_by_path[path], self.COL_CHECKED, checked
            )
        self._sync_checked_actions()
        if checked:
            # 2026-08-16: il clic termina dopo il segnale toggled e può
            # riprendersi il focus; rinviarlo prepara subito la scrittura.
            GLib.idle_add(self._focus_commit_message_after_select_all)

    def _focus_commit_message_after_select_all(self) -> bool:
        """Focus the commit message after GTK completes a master-checkbox click."""

        self.message.grab_focus()
        return GLib.SOURCE_REMOVE

    def _on_revision_row_expanded(
        self, tree: Gtk.TreeView, tree_iter: Gtk.TreeIter, _path: Gtk.TreePath
    ) -> None:
        """Persist one explicit repository/group expansion in the project cache."""

        if self.reconciling_status or Gtk.get_current_event() is None:
            return
        identity = self._row_identity(tree.get_model(), tree_iter)
        if identity is not None:
            self.current_state.expanded_rows.add(identity)

    def _on_revision_row_collapsed(
        self, tree: Gtk.TreeView, tree_iter: Gtk.TreeIter, _path: Gtk.TreePath
    ) -> None:
        """Persist one explicit repository/group collapse in the project cache."""

        # 2026-08-17: GtkTreeModelFilter and layout emit collapse signals too;
        # only a real mouse/key event may change the in-memory expansion policy.
        if self.reconciling_status or Gtk.get_current_event() is None:
            return
        identity = self._row_identity(tree.get_model(), tree_iter)
        if identity is not None:
            self.current_state.expanded_rows.discard(identity)

    def _on_tree_button(self, tree: Gtk.TreeView, event: Gdk.EventButton) -> bool:
        """Handle previews plus repository and file contextual menus."""

        hit = tree.get_path_at_pos(int(event.x), int(event.y))
        if event.button == 1:
            if not hit:
                self.on_preview(None)
                return False
            tree_iter = self.filtered_store.get_iter(hit[0])
            if self.filtered_store.get_value(tree_iter, self.COL_KIND) != "file":
                self.on_preview(None)
                return False
            path = self.filtered_store.get_value(tree_iter, self.COL_PATH)
            repository = self.filtered_store.get_value(
                tree_iter, self.COL_REPOSITORY
            )
            key = self._status_key(self._status_from_row(path, repository))
            self.on_preview(self.status_by_path.get(key))
            return False
        if event.button != 3 or not hit:
            if event.button == 3:
                self.on_preview(None)
            return False
        tree_path = hit[0]
        tree_iter = self.filtered_store.get_iter(tree_path)
        kind = self.filtered_store.get_value(tree_iter, self.COL_KIND)
        if kind == "repository":
            self.context_repository = self.filtered_store.get_value(
                tree_iter, self.COL_REPOSITORY
            )
            return self._show_repository_menu(event)
        if kind != "file":
            return False
        selection = tree.get_selection()
        # Il tasto destro su una riga già selezionata conserva il sottoinsieme
        # costruito con Ctrl/Shift; su una nuova riga crea una selezione singola.
        if not selection.path_is_selected(tree_path):
            selection.unselect_all()
            selection.select_path(tree_path)
        path = self.filtered_store.get_value(tree_iter, self.COL_PATH)
        repository = self.filtered_store.get_value(tree_iter, self.COL_REPOSITORY)
        key = self._status_key(self._status_from_row(path, repository))
        self.context_status = self.status_by_path.get(key)
        return self._show_file_menu(event)

    def _on_tree_popup_menu(self, _tree: Gtk.TreeView) -> bool:
        """Open repository or file actions from Menu or Shift+F10."""

        tree_path, _column = self.tree.get_cursor()
        if tree_path is not None:
            tree_iter = self.filtered_store.get_iter(tree_path)
            if self.filtered_store.get_value(tree_iter, self.COL_KIND) == "repository":
                self.context_repository = self.filtered_store.get_value(
                    tree_iter, self.COL_REPOSITORY
                )
                return self._show_repository_menu(None)
        if not self.selected_statuses():
            return False
        return self._show_file_menu(None)

    def _show_repository_menu(self, event: Gdk.EventButton | None) -> bool:
        """Show tools whose working directory is the contextual repository."""

        if self.context_repository is None:
            return False
        menu = Gtk.Menu()
        verify_item = self._menu_item("Verify…", "view-refresh", None)
        update_item = self._menu_item("Update…", "view-refresh", None)
        publish_item = self._menu_item("Publish…", "document-send", None)
        new_branch_item = self._menu_item("New branch…", "list-add", None)
        switch_branch_item = self._menu_item("Switch branch…", "go-jump", None)
        merge_branch_item = self._menu_item("Merge branch…", "insert-link", None)
        tag_item = self._menu_item("Assign tag…", "bookmark-new", None)
        # 2026-08-20: D indica Diff ed è l'unico tasto libero comune alle
        # azioni Meld; M resta riservato all'editor interno e Delete è distinto.
        meld_item = self._menu_item("Open in Meld", "document-open", Gdk.KEY_d)
        exclude_item = self._menu_item(
            "Exclude repository", "list-remove", None
        )
        verify_item.connect("activate", self._on_context_verify)
        update_item.connect("activate", self._on_context_update)
        publish_item.connect("activate", self._on_context_publish)
        new_branch_item.connect("activate", self._on_context_new_branch)
        switch_branch_item.connect("activate", self._on_context_switch_branch)
        merge_branch_item.connect("activate", self._on_context_merge_branch)
        tag_item.connect("activate", self._on_context_tag)
        meld_item.connect("activate", self._on_context_meld)
        exclude_item.connect("activate", self._on_context_exclude)
        menu.append(meld_item)
        if self.context_repository.scm_type == "hg":
            external_item = self._menu_item(
                "Open in TortoiseHg", "applications-system", None
            )
            external_item.connect("activate", self._on_context_external)
            menu.append(external_item)
        menu.append(verify_item)
        # 2026-08-18: repository mutations follow harmless inspection tools
        # and remain visually separated from repository-list administration.
        menu.append(Gtk.SeparatorMenuItem())
        menu.append(update_item)
        menu.append(publish_item)
        menu.append(new_branch_item)
        menu.append(switch_branch_item)
        menu.append(merge_branch_item)
        menu.append(tag_item)
        menu.append(Gtk.SeparatorMenuItem())
        menu.append(exclude_item)
        menu.show_all()
        if event is not None:
            menu.popup_at_pointer(event)
        else:
            menu.popup_at_widget(
                self.tree, Gdk.Gravity.CENTER, Gdk.Gravity.CENTER, None
            )
        return True

    def _show_file_menu(self, event: Gdk.EventButton | None) -> bool:
        """Show all safe contextual actions for the focused file row."""

        selected = self.selected_statuses()
        if not selected:
            return False
        if event is None:
            self.context_status = self._focused_file_status() or selected[0]
        elif self.context_status not in selected:
            self.context_status = self._focused_file_status() or selected[0]
        self.context_selected_statuses = list(selected)
        self.context_add_statuses = self.selected_untracked_statuses()
        self.context_forget_statuses = self.selected_added_statuses()
        self.context_checkbox_statuses = (
            [status for status in selected if status.state != "untracked"]
            if len(selected) > 1
            else []
        )
        if self.context_checkbox_statuses:
            anchor = (
                self.context_status
                if self.context_status in self.context_checkbox_statuses
                else self.context_checkbox_statuses[0]
            )
            self.context_checkbox_checked = (
                self._status_key(anchor) not in self.checked_paths
            )
        menu = self._build_file_menu()
        menu.show_all()
        if event is not None:
            menu.popup_at_pointer(event)
        else:
            menu.popup_at_widget(
                self.tree,
                Gdk.Gravity.CENTER,
                Gdk.Gravity.CENTER,
                None,
            )
        return True

    def _build_file_menu(self) -> Gtk.Menu:
        """Build generic file and source-control action groups for the context."""

        menu = Gtk.Menu()
        # 2026-08-17: una selezione multipla non deve presentare azioni che
        # colpirebbero silenziosamente soltanto il file contestuale.
        multiple = len(self.context_selected_statuses) > 1
        if not multiple:
            view_item = self._menu_item("View", "document-open", Gdk.KEY_v)
            internal_item = self._menu_item(
                "Edit in SLATE", "accessories-text-editor", Gdk.KEY_m
            )
            external_item = self._menu_item(
                "Edit in gVim", "gvim", Gdk.KEY_e
            )
            view_item.connect("activate", self._on_context_view)
            internal_item.connect("activate", self._on_context_edit_internal)
            external_item.connect("activate", self._on_context_edit_external)
            menu.append(view_item)
            menu.append(internal_item)
            menu.append(external_item)
            # 2026-08-20: il menu del file espone lo stesso percorso Meld già
            # usato dal doppio clic, soltanto quando esiste una patch tracciata.
            if (
                self.context_status is not None
                and self.context_status.state
                not in {"untracked", "added", "removed"}
            ):
                meld_item = self._menu_item(
                    "Open in Meld", "document-open", Gdk.KEY_d
                )
                meld_item.connect("activate", self._on_context_file_meld)
                menu.append(meld_item)
        # 2026-08-16: le operazioni SCM formano un gruppo distinto dalle
        # azioni generiche sul file condivise con il File manager.
        if (
            self.context_checkbox_statuses
            or self.context_add_statuses
            or self.context_forget_statuses
        ):
            if menu.get_children():
                menu.append(Gtk.SeparatorMenuItem())
        if self.context_checkbox_statuses:
            checkbox_count = len(self.context_checkbox_statuses)
            checkbox_label = (
                f"Check ({checkbox_count})"
                if self.context_checkbox_checked
                else f"Uncheck ({checkbox_count})"
            )
            checkbox_item = self._menu_item(
                checkbox_label, "emblem-ok", Gdk.KEY_space
            )
            checkbox_item.connect("activate", self._on_context_toggle_checkboxes)
            menu.append(checkbox_item)
        if self.context_add_statuses:
            add_label = (
                "Add"
                if len(self.context_add_statuses) == 1
                else f"Add ({len(self.context_add_statuses)})"
            )
            add_item = self._menu_item(add_label, "list-add", Gdk.KEY_a)
            add_item.connect("activate", self._on_context_add)
            menu.append(add_item)
        if self.context_forget_statuses:
            forget_item = self._menu_item("Undo add", "list-remove", None)
            forget_item.connect("activate", self._on_context_forget)
            menu.append(forget_item)
        if not multiple:
            delete_item = self._menu_item("Delete", "edit-delete", Gdk.KEY_Delete)
            delete_item.connect("activate", self._on_context_delete)
            menu.append(Gtk.SeparatorMenuItem())
            menu.append(delete_item)
        return menu

    @staticmethod
    def _menu_item(
        label: str, icon_name: str, keyval: int | None
    ) -> Gtk.MenuItem:
        """Create a contextual item with a visible themed icon and shortcut hint."""

        # 2026-08-16: un contenuto esplicito evita Gtk.ImageMenuItem, deprecato
        # in GTK3, mantenendo icona e suggerimento tastiera sempre visibili.
        item = Gtk.MenuItem()
        content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        icon = Gtk.Image.new_from_icon_name(icon_name, Gtk.IconSize.MENU)
        accel_label = Gtk.AccelLabel(label=label)
        accel_label.set_xalign(0)
        accel_label.set_accel_widget(item)
        if keyval is not None:
            accel_label.set_accel(keyval, Gdk.ModifierType(0))
        content.pack_start(icon, False, False, 0)
        content.pack_start(accel_label, True, True, 0)
        item.add(content)
        return item

    def _focused_file_status(self) -> FileStatus | None:
        """Return the TreeView cursor file independently from multi-selection."""

        tree_path, _column = self.tree.get_cursor()
        if tree_path is not None:
            tree_iter = self.filtered_store.get_iter(tree_path)
            if self.filtered_store.get_value(tree_iter, self.COL_KIND) == "file":
                path = self.filtered_store.get_value(tree_iter, self.COL_PATH)
                repository = self.filtered_store.get_value(
                    tree_iter, self.COL_REPOSITORY
                )
                key = self._status_key(self._status_from_row(path, repository))
                return self.status_by_path.get(key)
        selected = self.selected_statuses()
        return selected[0] if selected else None

    def selected_untracked_statuses(self) -> list[FileStatus]:
        """Return only new files from the current Ctrl/Shift row selection."""

        return [
            status
            for status in self.selected_statuses()
            if status.state == "untracked"
        ]

    def selected_added_statuses(self) -> list[FileStatus]:
        """Return only added files from the current contextual row selection."""

        return [
            status
            for status in self.selected_statuses()
            if status.state == "added"
        ]

    def _on_tree_cursor_changed(self, _tree: Gtk.TreeView) -> None:
        """Keep preview synchronized with the current mouse or arrow-key cursor."""

        if self.reconciling_status:
            return
        self.on_preview(self._focused_file_status())

    def _on_tree_key_press(self, _tree: Gtk.TreeView, event: Gdk.EventKey) -> bool:
        """Dispatch single-key file actions and checkbox toggling from the tree."""

        if event.state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.MOD1_MASK):
            return False
        keyval = Gdk.keyval_to_lower(event.keyval)
        tree_path, _column = self.tree.get_cursor()
        # 2026-08-20: D è contestuale: sulla radice confronta il repository,
        # sulla riga file limita Meld ai path della singola patch tracciata.
        if tree_path is not None and keyval == Gdk.KEY_d:
            tree_iter = self.filtered_store.get_iter(tree_path)
            kind = self.filtered_store.get_value(tree_iter, self.COL_KIND)
            repository = self.filtered_store.get_value(
                tree_iter, self.COL_REPOSITORY
            )
            if kind == "repository":
                self.on_diff(repository, ())
                return True
        status = self._focused_file_status()
        if status is None:
            return False
        multiple = len(self.selected_statuses()) > 1
        if keyval == Gdk.KEY_space and status.state != "untracked":
            focused_key = self._status_key(status)
            checked = focused_key not in self.checked_paths
            selected = [
                selected_status
                for selected_status in self.selected_statuses()
                if selected_status.state != "untracked"
            ]
            selected_keys = {
                self._status_key(selected_status) for selected_status in selected
            }
            targets = selected if focused_key in selected_keys else [status]
            self._set_operation_checkboxes(targets, checked)
            return True
        if keyval == Gdk.KEY_v:
            if multiple:
                return True
            self.on_view(status)
            return True
        if keyval == Gdk.KEY_m:
            if multiple:
                return True
            self.on_edit_internal(status)
            return True
        if keyval == Gdk.KEY_e:
            if multiple:
                return True
            self.on_edit_external(status)
            return True
        if keyval == Gdk.KEY_d:
            if multiple:
                return True
            if status.state not in {"untracked", "added", "removed"}:
                self.on_diff(
                    RepositoryRef(status.repository, status.scm_type),
                    status.operation_paths(),
                )
            return True
        if keyval == Gdk.KEY_a:
            selected_new = self.selected_untracked_statuses()
            if selected_new:
                self.on_add(selected_new)
                return True
        if event.keyval in (Gdk.KEY_Delete, Gdk.KEY_KP_Delete):
            if multiple:
                return True
            self.on_delete(status)
            return True
        return False

    def _on_context_add(self, _item: Gtk.MenuItem) -> None:
        """Forward all contextually selected untracked files for addition."""

        if self.context_add_statuses:
            self.on_add(list(self.context_add_statuses))

    def _on_context_forget(self, _item: Gtk.MenuItem) -> None:
        """Return contextually selected added files to the untracked state."""

        if self.context_forget_statuses:
            self.on_forget(list(self.context_forget_statuses))

    def _on_context_toggle_checkboxes(self, _item: Gtk.MenuItem) -> None:
        """Apply the contextual multi-selection state to its tracked checkboxes."""

        if self.context_checkbox_statuses:
            self._set_operation_checkboxes(
                self.context_checkbox_statuses, self.context_checkbox_checked
            )

    def _set_operation_checkboxes(
        self, statuses: Sequence[FileStatus], checked: bool
    ) -> None:
        """Set tracked operation checkboxes uniformly and refresh their actions."""

        # 2026-08-17: tastiera e menu contestuale devono applicare la stessa
        # decisione atomica alla selezione, senza invertire separatamente gli
        # stati misti né coinvolgere righe prive di checkbox.
        for status in statuses:
            if status.state == "untracked":
                continue
            key = self._status_key(status)
            if checked:
                self.checked_paths.add(key)
            else:
                self.checked_paths.discard(key)
            self.store.set_value(self.iter_by_path[key], self.COL_CHECKED, checked)
        self._sync_checked_actions()

    def _on_context_view(self, _item: Gtk.MenuItem) -> None:
        """Open the contextual file with the desktop's default viewer."""

        if self.context_status:
            self.on_view(self.context_status)

    def _on_context_edit_internal(self, _item: Gtk.MenuItem) -> None:
        """Open the contextual working-copy file in a SLATE editor tab."""

        if self.context_status:
            self.on_edit_internal(self.context_status)

    def _on_context_edit_external(self, _item: Gtk.MenuItem) -> None:
        """Open the contextual file in a separate gVim window."""

        if self.context_status:
            self.on_edit_external(self.context_status)

    def _on_context_delete(self, _item: Gtk.MenuItem) -> None:
        """Request confirmed deletion of the contextual working-copy file."""

        if self.context_status:
            self.on_delete(self.context_status)

    def _on_context_meld(self, _item: Gtk.MenuItem) -> None:
        """Launch Meld rooted at the explicitly clicked repository."""

        if self.context_repository is not None:
            self.on_diff(self.context_repository, ())

    def _on_context_file_meld(self, _item: Gtk.MenuItem) -> None:
        """Launch Meld for the explicitly targeted tracked file patch."""

        if (
            self.context_status is not None
            and self.context_status.state not in {"untracked", "added", "removed"}
        ):
            status = self.context_status
            self.on_diff(
                RepositoryRef(status.repository, status.scm_type),
                status.operation_paths(),
            )

    def _on_context_external(self, _item: Gtk.MenuItem) -> None:
        """Launch TortoiseHg rooted at the explicitly clicked repository."""

        if self.context_repository is not None:
            self.on_external(self.context_repository)

    def _on_context_verify(self, _item: Gtk.MenuItem) -> None:
        """Schedule explicit remote verification for the contextual repository."""

        self._schedule_repository_action("verify")

    def _on_context_update(self, _item: Gtk.MenuItem) -> None:
        """Schedule the repository update modal after menu deactivation."""

        if self.context_repository is not None:
            repository = self.context_repository
            GLib.idle_add(self._update_context_repository, repository)

    def _update_context_repository(self, repository: RepositoryRef) -> bool:
        """Open Update only after GTK has finished dispatching the context menu."""

        # 2026-08-17: deferring modal creation avoids nesting it in GTK's menu
        # activation stack, the same boundary needed by repository exclusion.
        self.on_update(repository)
        return GLib.SOURCE_REMOVE

    def _on_context_publish(self, _item: Gtk.MenuItem) -> None:
        """Schedule the simple Publish modal for the contextual repository."""

        self._schedule_repository_action("publish")

    def _on_context_new_branch(self, _item: Gtk.MenuItem) -> None:
        """Schedule the New branch modal for the contextual repository."""

        self._schedule_repository_action("new_branch")

    def _on_context_switch_branch(self, _item: Gtk.MenuItem) -> None:
        """Schedule the Switch branch modal for the contextual repository."""

        self._schedule_repository_action("switch_branch")

    def _on_context_merge_branch(self, _item: Gtk.MenuItem) -> None:
        """Schedule the local Merge branch modal for the contextual repository."""

        self._schedule_repository_action("merge_branch")

    def _on_context_tag(self, _item: Gtk.MenuItem) -> None:
        """Schedule the Assign tag modal for the contextual repository."""

        self._schedule_repository_action("tag")

    def _schedule_repository_action(self, action: str) -> None:
        """Defer one repository action until GTK has closed its context menu."""

        if self.context_repository is not None:
            GLib.idle_add(
                self._activate_repository_action,
                action,
                self.context_repository,
            )

    def _activate_repository_action(
        self, action: str, repository: RepositoryRef
    ) -> bool:
        """Forward one explicit repository action after menu deactivation."""

        self.on_repository_action(action, repository)
        return GLib.SOURCE_REMOVE

    def _on_context_exclude(self, _item: Gtk.MenuItem) -> None:
        """Schedule exclusion after GTK finishes dispatching menu activation."""

        if self.context_repository is not None:
            repository = self.context_repository
            GLib.idle_add(self._exclude_context_repository, repository)

    def _exclude_context_repository(self, repository: RepositoryRef) -> bool:
        """Exclude a repository once its contextual menu is no longer active."""

        self.on_exclude(repository)
        return GLib.SOURCE_REMOVE

    def _on_add_new_clicked(self, _button: Gtk.Button) -> None:
        """Request confirmed addition of every currently untracked path."""

        self.on_add(
            [
                status
                for status in self.status_by_path.values()
                if status.state == "untracked"
            ]
        )

    def _on_revert_checked_clicked(self, _button: Gtk.Button) -> None:
        """Forward only explicitly checked tracked files to confirmed revert."""

        self.on_revert(self.checked_statuses())

    def _on_error_response(
        self, _bar: Gtk.InfoBar, _response: Gtk.ResponseType
    ) -> None:
        """Allow the user to dismiss a previously reported repository error."""

        self.clear_error()

    def _on_commit_clicked(self, _button: Gtk.Button) -> None:
        """Forward an explicit commit request to the owning window."""

        self.on_commit(self.message_text(), self.checked_statuses())

    def _on_message_key_press(
        self, _message: Gtk.TextView, event: Gdk.EventKey
    ) -> bool:
        """Submit an enabled commit when Ctrl+Enter is pressed in its message."""

        # 2026-08-18: la scorciatoia passa dalla stessa azione del pulsante,
        # mantenendo unica la validazione di messaggio, checkbox e stato busy.
        modifiers = event.state & Gtk.accelerator_get_default_mod_mask()
        enter_keys = (Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_ISO_Enter)
        if event.keyval not in enter_keys or modifiers != Gdk.ModifierType.CONTROL_MASK:
            return False
        if self.commit_button.get_sensitive():
            self._on_commit_clicked(self.commit_button)
        return True

    def _on_scan_clicked(self, _button: Gtk.Button) -> None:
        """Request one repository discovery pass for the active project."""

        self.on_scan()

    def _on_reset_clicked(self, _button: Gtk.Button) -> None:
        """Reset repository discovery preferences for the active project."""

        self.on_reset()

    def _on_row_activated(
        self, _tree: Gtk.TreeView, path: Gtk.TreePath, _column: Gtk.TreeViewColumn
    ) -> None:
        """Open Meld for tracked files and retain the internal view for new ones."""

        tree_iter = self.filtered_store.get_iter(path)
        if self.filtered_store.get_value(tree_iter, self.COL_KIND) == "file":
            file_path = self.filtered_store.get_value(tree_iter, self.COL_PATH)
            repository = self.filtered_store.get_value(
                tree_iter, self.COL_REPOSITORY
            )
            key = self._status_key(self._status_from_row(file_path, repository))
            status = self.status_by_path.get(key)
            if status and status.state in {"untracked", "added", "removed"}:
                self.on_preview(status)
            elif status:
                self.on_diff(
                    RepositoryRef(status.repository, status.scm_type),
                    status.operation_paths(),
                )
