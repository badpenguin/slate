"""Main three-column GTK window and application signal wiring."""

from __future__ import annotations

from functools import partial
import os
from pathlib import Path
import re
import shlex
import sys
from typing import Callable, Sequence

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, GdkPixbuf, Gio, GLib, GObject, Gtk, Pango  # noqa: E402

from .browser import BrowserManager, BrowserPage
from .config import ConfigStore, new_project_config
from .editor import EditorDocument, EditorWorkspace
from .file_manager import ProjectFileManager
from .panel import SCMPanel
from .preview import FilePreview
from .processes import CommandResult, run_async, spawn_detached
from .project_files import DirectoryInspection, ProjectFileOperations
from .repository_actions import (
    RepositoryCreateBranchDialog,
    RepositoryMergeBranchDialog,
    RepositoryPublishDialog,
    RepositorySwitchBranchDialog,
    RepositoryTagDialog,
)
from .repository_discovery import RepositoryDiscovery
from .repository_update import RepositoryUpdateDialog
from .repository_verify import RepositoryVerifyDialog, UnattendedRepositoryVerifier
from .scm.base import FileStatus, RepositoryRef, RepositorySyncStatus, SCM
from .scm.detect import is_normal_repository
from .scm.git import GitSCM
from .scm.hg import MercurialSCM
from .settings import SettingsDialog
from .terminals import (
    OrphanSession,
    PaneInfo,
    TerminalManager,
    session_name,
    slug,
    terminal_key,
)
from .watcher import RepoWatcher


_SidebarIdentity = tuple[str, str, str]


def _moved_sequence(
    values: Sequence[object], source: object, target: object, before: bool
) -> list[object] | None:
    """Return an effectively reordered sequence or None for an invalid/no-op move."""

    original = list(values)
    if source == target or source not in original or target not in original:
        return None
    reordered = list(original)
    # La sorgente va rimossa prima di calcolare l'indice finale, altrimenti uno
    # spostamento verso il basso risulta sfalsato di una posizione.
    reordered.remove(source)
    target_index = reordered.index(target)
    reordered.insert(target_index if before else target_index + 1, source)
    return reordered if reordered != original else None


class SlateWindow(Gtk.ApplicationWindow):
    """Coordinate projects, persistent terminals and local SCM status panels."""

    COL_TEXT = 0
    COL_KIND = 1
    COL_PROJECT = 2
    COL_ITEM = 3
    COL_ACTIVITY = 4
    COL_TOOLTIP = 5
    COL_ATTENTION = 6
    PROJECT_DRAG_TARGET = "application/x-slate-project-sidebar-row"
    PROJECT_DRAG_PAYLOAD = b"slate-project-sidebar-row"
    PROJECT_DRAG_ACTIONS = Gdk.DragAction.COPY | Gdk.DragAction.MOVE
    BROWSER_BELL_DEBOUNCE_MS = 250

    def __init__(
        self,
        application: Gtk.Application,
        config: ConfigStore,
    ) -> None:
        """Build the workbench window for one application and configuration."""

        debug_suffix = " — AGENT DEBUG" if os.environ.get("SLATE_AGENT_DEBUG") else ""
        super().__init__(application=application, title=f"SLATE{debug_suffix}")
        # 2026-08-16: un file incluso nel pacchetto assegna un'identità stabile
        # alla finestra anche quando SLATE parte dal checkout e non da un .desktop.
        try:
            self.set_icon_from_file(str(Path(__file__).with_name("slate.svg")))
        except GLib.Error as error:
            print(f"Failed to load SLATE icon: {error}", file=sys.stderr)
        self.config = config
        # 2026-08-17: typed references keep mixed HG/Git runtime objects
        # isolated without changing the lazy per-project lifetime architecture.
        self.watchers: dict[tuple[str, RepositoryRef], RepoWatcher] = {}
        self.scm_by_repository: dict[tuple[str, RepositoryRef], SCM] = {}
        self.snapshots: dict[
            tuple[str, RepositoryRef], tuple[list[FileStatus], str]
        ] = {}
        # 2026-08-18: conserviamo il valore già ricevuto dal tab Revisioni per
        # ripristinarne il riflesso sulla riga progetto dopo i rebuild UI.
        self.revision_counts: dict[str, int] = {}
        self.repositories_by_project: dict[str, set[RepositoryRef]] = {}
        self.discovery_by_project: dict[str, RepositoryDiscovery] = {}
        self.scanned_projects: set[str] = set()
        self.ignored_by_repository: dict[tuple[str, RepositoryRef], set[str]] = {}
        self.active_project_name: str | None = None
        self.active_terminal_name: str | None = None
        self.active_editor_ref: tuple[str, str] | None = None
        self.restoring_tree = False
        self.restoring_selection = False
        self.startup_inactive = True
        self.project_drag_candidate: tuple[
            _SidebarIdentity, Gtk.TreePath, int, int
        ] | None = None
        self.project_drag_source: _SidebarIdentity | None = None
        self.closing = False
        self.repository_dialog: Gtk.Dialog | None = None
        self.unattended_verifier: UnattendedRepositoryVerifier | None = None
        self.unattended_verifier_key: tuple[str, RepositoryRef] | None = None
        self.unattended_verification_queue: list[tuple[str, RepositoryRef]] = []
        self.verified_projects: set[str] = set()
        self._terminate_started_at = 0
        self.attention_terminals: set[Gtk.Widget] = set()
        self.browser_bell_timeout_id: int | None = None
        self.browser_bell_project_name: str | None = None
        self.project_file_operations = ProjectFileOperations()
        # 2026-08-16: un asset SLATE dedicato mantiene la campanella colorata e
        # leggibile anche quando il tema offre soltanto icone symbolic monocrome.
        self.attention_icon = GdkPixbuf.Pixbuf.new_from_file_at_scale(
            str(Path(__file__).with_name("bell.svg")), 16, 16, True
        )
        # 2026-08-19: the sidebar reuses the HeaderBar's bundled Incognito
        # artwork so the same browser mode never has two unrelated icons.
        self.incognito_icon = GdkPixbuf.Pixbuf.new_from_file_at_scale(
            str(Path(__file__).with_name("incognito.svg")), 16, 16, True
        )

        geometry = self.config.data["window"]
        self.set_default_size(geometry["width"], geometry["height"])
        if geometry["maximized"]:
            self.maximize()
        self.connect("delete-event", self._on_delete_event)
        self.connect("key-press-event", self._on_key_press)
        self.connect_after("button-press-event", self._on_window_button_press)
        self.set_titlebar(self._build_headerbar())

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.add(root)

        self.outer_paned = Gtk.Paned.new(Gtk.Orientation.HORIZONTAL)
        self.inner_paned = Gtk.Paned.new(Gtk.Orientation.HORIZONTAL)
        # 2026-08-16: un overlay unico consente alla preview di coprire sidebar
        # e terminale senza nascondere né ricostruire il pannello Revisioni.
        self.content_overlay = Gtk.Overlay()
        self.content_overlay.add(self.outer_paned)
        root.pack_start(self.content_overlay, True, True, 0)

        project_column = self._build_project_tree()
        self.terminal_stack = Gtk.Stack()
        self.terminal_stack.set_transition_type(Gtk.StackTransitionType.NONE)
        self.inactive_terminal = Gtk.Box()
        self.terminal_stack.add_named(self.inactive_terminal, "__inactive__")
        self.empty_terminal = self._build_empty_terminal()
        self.terminal_stack.add_named(self.empty_terminal, "__empty__")
        self.panel = SCMPanel(
            self._commit,
            self._open_diff,
            self._open_external,
            self._update_repository,
            self._open_repository_action,
            self._scan_active_project,
            self._reset_active_project_repositories,
            self._exclude_repository,
            self._preview_file,
            self._add_statuses,
            self._forget_statuses,
            self._revert_statuses,
            self._view_status,
            self._edit_status_internal,
            self._edit_status_external,
            self._delete_status,
        )
        self.file_manager = ProjectFileManager(
            self._preview_project_file,
            self._view_project_file,
            self._edit_project_file_internal,
            self._edit_project_file_external,
            self._new_project_file,
            self._new_project_directory,
            self._rename_project_file,
            self._open_terminal_in_project_directory,
            self._delete_project_file,
            self._save_file_manager_preferences,
        )
        settings = self.config.data["settings"]
        self.panel.set_font_size(settings["revisions"]["font_size"])
        self.file_manager.set_font_size(settings["files"]["font_size"])
        # 2026-08-16: lo stack centrale non espone tab; l'albero dei progetti è
        # l'unico selettore sia per i terminali sia per i pochi file di lavoro.
        self.editor_workspace = EditorWorkspace(
            self.terminal_stack,
            settings["editor"]["font_size"],
            self._persist_editor_state,
            self._show_file_error,
            self._on_editor_collection_changed,
            self._on_editor_state_changed,
        )
        # 2026-08-17: il manager browser condivide soltanto lo stack centrale;
        # contesto WebKit e pagine restano lazy fino al click esplicito.
        self.browser_manager = BrowserManager(
            self.editor_workspace,
            self._on_browser_collection_changed,
            self._on_browser_state_changed,
            self._show_error,
        )
        # 2026-08-16: Revisioni e File condividono la terza colonna; la preview
        # conserva così tutta l'area delle prime due colonne senza comprimere UI.
        self.right_notebook = Gtk.Notebook()
        self.right_notebook.set_tab_pos(Gtk.PositionType.TOP)
        self.right_notebook.set_scrollable(False)
        self.changes_tab = Gtk.Label(label="Changes")
        files_tab = Gtk.Label(label="Files")
        # 2026-08-16: margini sulle label aumentano l'area cliccabile verticale
        # conservando bordi, stati e rendering nativi del GtkNotebook.
        for tab in (self.changes_tab, files_tab):
            tab.set_margin_top(6)
            tab.set_margin_bottom(6)
        self.right_notebook.append_page(self.panel, self.changes_tab)
        self.right_notebook.append_page(self.file_manager, files_tab)
        # 2026-08-16: tab-expand distribuisce la larghezza della terza colonna
        # fra le due linguette native, evitando etichette minuscole a sinistra.
        for page in (self.panel, self.file_manager):
            self.right_notebook.child_set_property(page, "tab-expand", True)
            self.right_notebook.child_set_property(page, "tab-fill", True)
        self.right_notebook.connect("switch-page", self._on_right_page_changed)
        right_column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        right_column.get_style_context().add_class("right-column")
        right_column.pack_start(self.right_notebook, True, True, 0)

        self.outer_paned.pack1(project_column, resize=False, shrink=False)
        self.outer_paned.pack2(self.inner_paned, resize=True, shrink=False)
        self.inner_paned.pack1(self.editor_workspace, resize=True, shrink=False)
        self.inner_paned.pack2(right_column, resize=False, shrink=False)
        self.outer_paned.set_position(self.config.data["pane_positions"][0])
        self.inner_paned.set_position(self.config.data["pane_positions"][1])
        self.preview = FilePreview(self._close_preview)
        self.preview.set_halign(Gtk.Align.START)
        self.preview.set_valign(Gtk.Align.FILL)
        self.content_overlay.add_overlay(self.preview)
        self.outer_paned.connect("notify::position", self._update_preview_geometry)
        self.inner_paned.connect("notify::position", self._update_preview_geometry)
        self.content_overlay.connect("size-allocate", self._update_preview_geometry)

        self.terminals = TerminalManager(
            self.terminal_stack,
            self._show_error,
            self._update_activity,
            self._on_terminal_exited,
            self._update_terminal_attention,
            self._queue_browser_bell_reload,
            bool(settings["terminal"]["status_bar"]),
        )
        self.connect("notify::is-active", self._on_window_activity_changed)
        self.editor_workspace.restore(
            self.config.data["editor"], self.config.data["projects"]
        )
        # 2026-08-17: il restore registra soltanto metadati e righe; contesto,
        # WebView e richieste restano lazy fino alla selezione esplicita.
        self.browser_manager.restore(self.config.data["projects"])
        self._populate_projects()
        # 2026-08-17: startup remains neutral and repository watchers are lazy;
        # only an explicit project selection may activate terminals or run hg.
        self._show_inactive_workspace()
        self.show_all()
        # 2026-08-17: GTK may assign a cursor while mapping the populated tree;
        # keep startup selection blocked until those queued events are drained.
        GLib.idle_add(self._finish_inactive_startup)
        self.preview.hide()
        self.empty_label.set_visible(not bool(self.config.data["projects"]))
        if self.config.error:
            GLib.idle_add(self._show_error, self.config.error)

    def _build_headerbar(self) -> Gtk.HeaderBar:
        """Create the native title bar with frequent and overflow actions."""

        header = Gtk.HeaderBar()
        header.set_title(
            "SLATE — AGENT DEBUG" if os.environ.get("SLATE_AGENT_DEBUG") else "SLATE"
        )
        header.set_show_close_button(True)
        header.get_style_context().add_class("slate-header")
        self.headerbar = header

        add_project = self._header_action_button(
            "New Project", "folder-new", self._on_add_project
        )
        add_terminal = self._header_action_button(
            "New Terminal", "utilities-terminal", self._on_add_terminal
        )
        add_command = self._header_action_button(
            "Execute", "system-run", self._on_add_command
        )
        resume_codex = self._header_action_button(
            "Codex",
            "system-run",
            self._on_resume_codex,
            icon_path=Path(__file__).with_name("codex.svg"),
        )
        add_browser = self._header_action_button(
            "Open URL", "web-browser", self._on_add_browser
        )
        add_private_browser = self._header_action_button(
            "Incognito",
            "user-invisible",
            self._on_add_private_browser,
            icon_path=Path(__file__).with_name("incognito.svg"),
        )
        add_terminal.set_sensitive(False)
        add_command.set_sensitive(False)
        resume_codex.set_sensitive(False)
        add_browser.set_sensitive(False)
        add_private_browser.set_sensitive(False)
        self.add_terminal_button = add_terminal
        self.add_command_button = add_command
        self.resume_codex_button = resume_codex
        self.add_browser_button = add_browser
        self.add_private_browser_button = add_private_browser
        header.pack_start(add_project)
        # 2026-08-18: the separators expose the three action groups requested by
        # the user without adding another toolbar container or changing behavior.
        header.pack_start(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        header.pack_start(add_terminal)
        header.pack_start(add_command)
        header.pack_start(resume_codex)
        header.pack_start(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        header.pack_start(add_browser)
        header.pack_start(add_private_browser)

        overflow = Gtk.MenuButton()
        overflow.set_image(
            Gtk.Image.new_from_icon_name("open-menu-symbolic", Gtk.IconSize.BUTTON)
        )
        overflow.set_tooltip_text("More actions")
        overflow.get_accessible().set_name("More actions")
        menu = Gtk.Menu()
        settings_item = Gtk.MenuItem(label="Settings")
        settings_item.connect("activate", self._on_settings)
        orphans = Gtk.MenuItem(label="Orphan Sessions")
        orphans.connect("activate", self._on_orphans)
        menu.append(settings_item)
        menu.append(Gtk.SeparatorMenuItem())
        menu.append(orphans)
        menu.show_all()
        overflow.set_popup(menu)
        header.pack_end(overflow)
        return header

    def _on_settings(self, _item: Gtk.MenuItem) -> None:
        """Open the modal settings dialog over the current SLATE window."""

        dialog = SettingsDialog(
            self,
            self.config.data["settings"],
            self._update_font_setting,
            self._update_tmux_status_setting,
        )
        dialog.show_all()

    def _update_font_setting(self, section: str, font_size: int) -> None:
        """Apply and persist one supported list-font setting immediately."""

        # 2026-08-16: la mappa esplicita impedisce a una categoria UI inattesa
        # di creare chiavi arbitrarie nella configurazione unica di SLATE.
        targets = {
            "revisions": self.panel,
            "files": self.file_manager,
            "editor": self.editor_workspace,
        }
        target = targets.get(section)
        if target is None or not 8 <= font_size <= 32:
            return
        self.config.data["settings"][section]["font_size"] = font_size
        target.set_font_size(font_size)
        self.config.save()

    def _update_tmux_status_setting(self, enabled: bool) -> None:
        """Persist and immediately apply the dedicated tmux status visibility."""

        self.config.data["settings"]["terminal"]["status_bar"] = enabled
        self.terminals.set_status_bar_enabled(enabled)
        self.config.save()

    def _persist_editor_state(
        self,
        tabs: list[dict[str, str]],
        active_tab: dict[str, str] | None,
    ) -> None:
        """Persist open editor identities and the visible editor in one config."""

        state = {"tabs": tabs, "active_tab": active_tab}
        if self.config.data.get("editor") == state:
            return
        self.config.data["editor"] = state
        self.config.save()

    def _on_editor_collection_changed(self) -> None:
        """Rebuild sidebar rows after an editor is opened or closed."""

        if not hasattr(self, "project_store") or not hasattr(self, "terminals"):
            return
        visible_editor = self.editor_workspace.current_editor()
        active_ref = (
            visible_editor.reference
            if visible_editor is not None
            else self.active_editor_ref
        )
        previous_orders = {
            project["name"]: tuple(
                (item.get("kind", ""), item.get("value", ""))
                for item in project.setdefault("item_order", [])
            )
            for project in self.config.data["projects"]
        }
        self._populate_projects()
        current_orders = {
            project["name"]: tuple(
                (item.get("kind", ""), item.get("value", ""))
                for item in project["item_order"]
            )
            for project in self.config.data["projects"]
        }
        if current_orders != previous_orders:
            self.config.save()
        if active_ref and active_ref in self.editor_workspace.editors:
            self._select_tree_row(active_ref[0], active_ref[1], "editor")
            return
        self.active_editor_ref = None
        project = self.config.find_project(self.active_project_name or "")
        if project is not None:
            terminal_name_value = project.get("last_terminal") or (
                project["terminals"][0] if project["terminals"] else ""
            )
            self._select_tree_row(project["name"], terminal_name_value, "terminal")

    def _on_editor_state_changed(self, editor: EditorDocument) -> None:
        """Mirror dirty and external-attention state on one editor tree row."""

        project_iter = self.project_store.get_iter_first()
        while project_iter:
            child = self.project_store.iter_children(project_iter)
            while child:
                if (
                    self.project_store.get_value(child, self.COL_KIND) == "editor"
                    and self.project_store.get_value(child, self.COL_PROJECT)
                    == editor.project_name
                    and self.project_store.get_value(child, self.COL_ITEM)
                    == editor.relative_path
                ):
                    self.project_store.set(
                        child,
                        self.COL_ACTIVITY,
                        editor.dirty,
                        self.COL_ATTENTION,
                        editor.attention,
                    )
                    return
                child = self.project_store.iter_next(child)
            project_iter = self.project_store.iter_next(project_iter)

    def _on_browser_collection_changed(self) -> None:
        """Persist browser-tab metadata and rebuild all browser rows."""

        visible = self.browser_manager.current_page()
        active_ref = visible.reference if visible is not None else None
        changed = False
        for project in self.config.data["projects"]:
            browsers = self.browser_manager.serialized_project(project["name"])
            if project.get("browsers", []) != browsers:
                project["browsers"] = browsers
                changed = True
            browser_ids = {browser["id"] for browser in browsers}
            old_order = project.setdefault("item_order", [])
            filtered_order = [
                item
                for item in old_order
                if item.get("kind") != "browser"
                or item.get("value") in browser_ids
            ]
            ordered_ids = {
                item.get("value")
                for item in filtered_order
                if item.get("kind") == "browser"
            }
            for browser in browsers:
                if browser["id"] not in ordered_ids:
                    filtered_order.append(
                        {"kind": "browser", "value": browser["id"]}
                    )
            if old_order != filtered_order:
                project["item_order"] = filtered_order
                changed = True
        if changed:
            self.config.save()
        self._populate_projects()
        if active_ref is not None and active_ref in self.browser_manager.pages:
            self._select_tree_row(active_ref[0], active_ref[1], "browser")
            return
        project = self.config.find_project(self.active_project_name or "")
        if project is not None:
            terminal_name_value = project.get("last_terminal") or (
                project["terminals"][0] if project["terminals"] else ""
            )
            self._select_tree_row(project["name"], terminal_name_value, "terminal")

    def _on_browser_state_changed(self, page: BrowserPage) -> None:
        """Mirror page identity and persist its latest navigation metadata."""

        tree_iter = self._find_sidebar_iter(
            ("browser", page.project_name, page.identifier)
        )
        if tree_iter is not None:
            self.project_store.set(
                tree_iter,
                self.COL_TEXT,
                page.entry.display_title,
                self.COL_TOOLTIP,
                GLib.markup_escape_text(page.entry.display_title),
            )
        project = self.config.find_project(page.project_name)
        if project is not None:
            browsers = self.browser_manager.serialized_project(page.project_name)
            if project.get("browsers", []) != browsers:
                project["browsers"] = browsers
                self.config.save()

    def _on_right_page_changed(
        self, _notebook: Gtk.Notebook, _page: Gtk.Widget, page_number: int
    ) -> None:
        """Activate file loading only while its third-column page is visible."""

        files_visible = page_number == 1
        self._close_preview()
        if files_visible:
            project = self.config.find_project(self.active_project_name or "")
            if project is not None:
                self._configure_file_manager(project)
            else:
                self.file_manager.clear_project()
        self.file_manager.set_active(files_visible)

    def _configure_file_manager(self, project: dict) -> None:
        """Bind the file page to one project unless it is already current."""

        root = str(Path(project["path"]).resolve())
        if (
            self.file_manager.project_name == project["name"]
            and self.file_manager.root == root
        ):
            return
        preferences = project.setdefault(
            "file_manager",
            {
                "show_hidden": False,
                "show_excluded": False,
                "expanded_paths": [],
            },
        )
        # 2026-08-16: il binding viene verificato anche aprendo direttamente la
        # tab File, così una selezione GTK non può lasciare la pagina senza root.
        self.file_manager.set_project(
            project["name"],
            root,
            preferences,
            self._project_ignored_paths(project["name"]),
            # 2026-08-17: nested repository watchers do not cover files placed
            # directly in the workspace; only a root watcher can replace the
            # File manager's own lazy monitors without leaving a stale tree.
            any(
                repository.path == "."
                for repository in self.repositories_by_project.get(
                    project["name"], set()
                )
            ),
        )

    def _save_file_manager_preferences(self, preferences: dict[str, object]) -> None:
        """Persist file-browser preferences inside the active project entry."""

        project = self.config.find_project(self.active_project_name or "")
        if project is None:
            return
        # 2026-08-16: filtri ed espansione appartengono al progetto, non alla
        # sessione terminale, e restano nel solo file config previsto da SLATE.
        project["file_manager"] = preferences
        self.config.save()

    def _update_terminal_attention(
        self, terminal: Gtk.Widget, requires_attention: bool
    ) -> None:
        """Show a bell on each ringing terminal row until it regains focus."""

        if requires_attention:
            self.attention_terminals.add(terminal)
        else:
            self.attention_terminals.discard(terminal)
        # 2026-08-16: l'avviso appartiene alla sessione che ha emesso BEL; una
        # campanella per riga permette di capire subito quale terminale è idle.
        project_iter = self.project_store.get_iter_first()
        while project_iter:
            terminal_iter = self.project_store.iter_children(project_iter)
            while terminal_iter:
                if (
                    self.project_store.get_value(terminal_iter, self.COL_KIND)
                    != "terminal"
                ):
                    terminal_iter = self.project_store.iter_next(terminal_iter)
                    continue
                project_name = self.project_store.get_value(
                    terminal_iter, self.COL_PROJECT
                )
                terminal_name_value = self.project_store.get_value(
                    terminal_iter, self.COL_ITEM
                )
                candidate = self.terminals.terminals.get(
                    terminal_key(project_name, terminal_name_value)
                )
                attention = candidate in self.attention_terminals
                if (
                    self.project_store.get_value(
                        terminal_iter, self.COL_ATTENTION
                    )
                    != attention
                ):
                    self.project_store.set_value(
                        terminal_iter, self.COL_ATTENTION, attention
                    )
                terminal_iter = self.project_store.iter_next(terminal_iter)
            project_iter = self.project_store.iter_next(project_iter)

    def _queue_browser_bell_reload(
        self, project_name: str, _terminal_name: str
    ) -> None:
        """Debounce a project's terminal BELL before activating its browser target."""

        self.browser_bell_project_name = project_name
        if self.browser_bell_timeout_id is not None:
            GLib.source_remove(self.browser_bell_timeout_id)
        # 2026-08-18: un solo debounce globale evita raffiche di reload e fa
        # prevalere l'ultimo progetto quando più terminali suonano insieme.
        self.browser_bell_timeout_id = GLib.timeout_add(
            self.BROWSER_BELL_DEBOUNCE_MS,
            self._finish_browser_bell_reload,
        )

    def _finish_browser_bell_reload(self) -> bool:
        """Show, hard-reload and present the runtime browser selected for BELL."""

        self.browser_bell_timeout_id = None
        project_name = self.browser_bell_project_name
        self.browser_bell_project_name = None
        if project_name is None:
            return GLib.SOURCE_REMOVE
        target = self.browser_manager.reload_on_bell_target(project_name)
        if target is None or target.page is None:
            return GLib.SOURCE_REMOVE
        if not self._select_tree_row(
            target.project_name, target.identifier, "browser"
        ):
            return GLib.SOURCE_REMOVE
        target.page.reload_bypass_cache()
        self.present()
        return GLib.SOURCE_REMOVE

    def _header_action_button(
        self,
        label: str,
        icon_name: str,
        callback: Callable[[Gtk.Widget], None],
        *,
        icon_path: Path | None = None,
    ) -> Gtk.Button:
        """Create an accessible HeaderBar action with a themed or bundled icon."""

        button = Gtk.Button()
        content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        # 2026-08-18: normalize bundled artwork to GTK's button-icon dimensions
        # so official assets do not alter the HeaderBar height.
        icon = (
            Gtk.Image.new_from_pixbuf(
                GdkPixbuf.Pixbuf.new_from_file_at_scale(
                    str(icon_path), 16, 16, True
                )
            )
            if icon_path is not None
            else Gtk.Image.new_from_icon_name(icon_name, Gtk.IconSize.BUTTON)
        )
        content.pack_start(
            icon,
            False,
            False,
            0,
        )
        content.pack_start(Gtk.Label(label=label), False, False, 0)
        button.add(content)
        button.set_tooltip_text(label)
        button.get_accessible().set_name(label)
        button.connect("clicked", callback)
        return button

    def _build_project_tree(self) -> Gtk.Widget:
        """Create the fixed two-level project/terminal tree."""

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.get_style_context().add_class("project-sidebar")
        box.set_size_request(190, -1)
        sidebar_title = Gtk.Label(label="PROJECTS")
        sidebar_title.set_xalign(0)
        sidebar_title.get_style_context().add_class("sidebar-title")
        box.pack_start(sidebar_title, False, False, 0)
        self.empty_label = Gtk.Label(label="Add a project to get started")
        self.empty_label.set_line_wrap(True)
        self.empty_label.get_style_context().add_class("sidebar-empty")
        box.pack_start(self.empty_label, False, False, 8)
        self.project_store = Gtk.TreeStore(str, str, str, str, bool, str, bool)
        self.project_tree = Gtk.TreeView(model=self.project_store)
        self.project_tree.set_headers_visible(False)
        # 2026-08-16: la ricerca incrementale GTK intercetta i tasti non gestiti
        # e confligge con le scorciatoie della sidebar, quindi resta disabilitata.
        self.project_tree.set_enable_search(False)
        # 2026-08-18: GtkTreeView interpreta la colonna tooltip come markup
        # Pango; ogni valore dinamico viene quindi salvato già escaped.
        self.project_tree.set_tooltip_column(self.COL_TOOLTIP)
        self.project_tree.get_style_context().add_class("project-tree")
        self.project_tree.set_level_indentation(0)

        found_base, base_color = self.project_tree.get_style_context().lookup_color(
            "theme_base_color"
        )
        found_foreground, foreground_color = (
            self.project_tree.get_style_context().lookup_color("theme_fg_color")
        )
        self.project_row_background = Gdk.RGBA()
        if found_base and found_foreground:
            self.project_row_background.red = (
                base_color.red * 0.92 + foreground_color.red * 0.08
            )
            self.project_row_background.green = (
                base_color.green * 0.92 + foreground_color.green * 0.08
            )
            self.project_row_background.blue = (
                base_color.blue * 0.92 + foreground_color.blue * 0.08
            )
            self.project_row_background.alpha = 1.0
        else:
            self.project_row_background.parse("#e8e8e8")

        icon_renderer = Gtk.CellRendererPixbuf()
        self.name_renderer = Gtk.CellRendererText()
        self.name_renderer.set_property("ellipsize", Pango.EllipsizeMode.END)
        activity_renderer = Gtk.CellRendererPixbuf()
        attention_renderer = Gtk.CellRendererPixbuf()
        expander_renderer = Gtk.CellRendererPixbuf()
        expander_renderer.set_property("stock-size", Gtk.IconSize.BUTTON)
        self.project_expander_column = Gtk.TreeViewColumn()
        self.project_expander_column.set_sizing(Gtk.TreeViewColumnSizing.FIXED)
        self.project_expander_column.set_fixed_width(18)
        self.project_expander_column.pack_start(expander_renderer, False)
        self.project_expander_column.set_cell_data_func(
            expander_renderer, self._render_expander_cell
        )
        column = Gtk.TreeViewColumn("Project")
        column.pack_start(icon_renderer, False)
        column.pack_start(self.name_renderer, True)
        column.pack_end(activity_renderer, False)
        column.pack_end(attention_renderer, False)
        column.set_cell_data_func(icon_renderer, self._render_tree_icon)
        column.set_cell_data_func(self.name_renderer, self._render_tree_name)
        column.set_cell_data_func(activity_renderer, self._render_activity_icon)
        column.set_cell_data_func(attention_renderer, self._render_attention_icon)
        column.set_expand(True)
        self.project_tree.append_column(self.project_expander_column)
        self.project_tree.append_column(column)
        # 2026-08-17: l'indicatore personalizzato vive dentro la cella colorata;
        # quello nativo resterebbe fuori dallo sfondo e sarebbe troppo piccolo.
        self.project_tree.set_show_expanders(False)
        self.project_tree.set_expander_column(self.project_expander_column)
        # 2026-08-17: il DnD viene avviato manualmente per imporre Ctrl e per
        # impedire a GtkTreeView di spostare righe fra livelli o progetti. GTK
        # associa Ctrl a COPY: va dichiarata anche quell'azione di protocollo,
        # sebbene SLATE applichi sempre e soltanto un riordino in-place.
        drag_target = Gtk.TargetEntry.new(
            self.PROJECT_DRAG_TARGET, Gtk.TargetFlags.SAME_WIDGET, 0
        )
        self.project_drag_targets = Gtk.TargetList.new([drag_target])
        self.project_tree.drag_dest_set(
            Gtk.DestDefaults(0), [drag_target], self.PROJECT_DRAG_ACTIONS
        )
        self.project_tree.add_events(
            Gdk.EventMask.BUTTON1_MOTION_MASK | Gdk.EventMask.BUTTON_RELEASE_MASK
        )
        self.project_tree.get_selection().connect("changed", self._on_tree_selection)
        self.project_tree.connect("row-expanded", self._on_row_expanded)
        self.project_tree.connect("row-collapsed", self._on_row_collapsed)
        self.project_tree.connect("button-press-event", self._on_tree_button)
        self.project_tree.connect("button-release-event", self._on_tree_button_release)
        self.project_tree.connect("motion-notify-event", self._on_tree_motion)
        self.project_tree.connect("drag-data-get", self._on_tree_drag_data_get)
        self.project_tree.connect("drag-motion", self._on_tree_drag_motion)
        self.project_tree.connect("drag-drop", self._on_tree_drag_drop)
        self.project_tree.connect(
            "drag-data-received", self._on_tree_drag_data_received
        )
        self.project_tree.connect("drag-leave", self._on_tree_drag_leave)
        self.project_tree.connect("drag-end", self._on_tree_drag_end)
        self.project_tree.connect("popup-menu", self._on_tree_popup_menu)
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.add(self.project_tree)
        box.pack_start(scroller, True, True, 0)
        return box

    def _build_empty_terminal(self) -> Gtk.Widget:
        """Create terminal placeholder with an explicit creation action."""

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_halign(Gtk.Align.CENTER)
        box.set_valign(Gtk.Align.CENTER)
        label = Gtk.Label(label="No terminals for this project")
        button = Gtk.Button(label="Open Terminal")
        button.connect("clicked", self._on_add_terminal)
        box.pack_start(label, False, False, 0)
        box.pack_start(button, False, False, 0)
        return box

    def _ordered_project_items(self, project: dict) -> list[tuple[str, str]]:
        """Return all project children in their shared persistent order."""

        editors = {
            entry.relative_path
            for entry in self.editor_workspace.editors.values()
            if entry.project_name == project["name"]
        }
        available = {("terminal", name) for name in project["terminals"]}
        available.update(("editor", path) for path in editors)
        browser_entries = getattr(
            getattr(self, "browser_manager", None), "pages", {}
        ).values()
        browsers = [
            entry
            for entry in browser_entries
            if entry.project_name == project["name"]
        ]
        available.update(
            ("browser", entry.identifier) for entry in browsers
        )
        ordered: list[tuple[str, str]] = []
        for item in project.setdefault("item_order", []):
            reference = (item.get("kind", ""), item.get("value", ""))
            if reference in available and reference not in ordered:
                ordered.append(reference)
        # 2026-08-16: le voci prive di ordine provengono da configurazioni
        # precedenti; vengono accodate una volta senza separarle nuovamente per tipo.
        missing_candidates = [
            ("terminal", name) for name in project["terminals"]
        ]
        missing_candidates.extend(
            ("editor", entry.relative_path)
            for entry in self.editor_workspace.editors.values()
            if entry.project_name == project["name"]
        )
        missing_candidates.extend(
            ("browser", entry.identifier) for entry in browsers
        )
        for reference in missing_candidates:
            if reference not in ordered:
                ordered.append(reference)
        project["item_order"] = [
            {"kind": kind, "value": value} for kind, value in ordered
        ]
        return ordered

    @staticmethod
    def _append_project_item(project: dict, kind: str, value: str) -> bool:
        """Append one new project child unless its stable identity already exists."""

        order = project.setdefault("item_order", [])
        if any(
            item.get("kind") == kind and item.get("value") == value
            for item in order
        ):
            return False
        order.append({"kind": kind, "value": value})
        return True

    def _populate_projects(self) -> None:
        """Rebuild project children for terminals and persistent work files."""

        self.restoring_tree = True
        self.project_store.clear()
        expanded = set(self.config.data["expanded_projects"])
        for project in self.config.data["projects"]:
            revision_count = getattr(self, "revision_counts", {}).get(
                project["name"], 0
            )
            parent = self.project_store.append(
                None,
                [
                    f"{project['name']} ({revision_count})"
                    if revision_count
                    else project["name"],
                    "project",
                    project["name"],
                    "",
                    False,
                    GLib.markup_escape_text(project["name"]),
                    False,
                ],
            )
            for kind, value in self._ordered_project_items(project):
                if kind == "terminal":
                    terminal = self.terminals.terminals.get(
                        terminal_key(project["name"], value)
                    )
                    self.project_store.append(
                        parent,
                        [
                            value,
                            kind,
                            project["name"],
                            value,
                            False,
                            GLib.markup_escape_text(value),
                            terminal in self.attention_terminals,
                        ],
                    )
                    continue
                if kind == "browser":
                    entry = self.browser_manager.pages[(project["name"], value)]
                    self.project_store.append(
                        parent,
                        [
                            entry.display_title,
                            kind,
                            project["name"],
                            value,
                            False,
                            GLib.markup_escape_text(entry.display_title),
                            False,
                        ],
                    )
                    continue
                entry = self.editor_workspace.editors[(project["name"], value)]
                editor = entry.document
                self.project_store.append(
                    parent,
                    [
                        Path(value).name,
                        kind,
                        project["name"],
                        value,
                        editor.dirty if editor is not None else False,
                        GLib.markup_escape_text(value),
                        editor.attention if editor is not None else False,
                    ],
                )
            if project["name"] in expanded:
                self.project_tree.expand_row(self.project_store.get_path(parent), False)
        self.restoring_tree = False

    def _ensure_project_repositories(self, project: dict) -> list[RepositoryRef]:
        """Attach cached repositories immediately and start first-use discovery."""

        project_name = project["name"]
        preferences = project.setdefault(
            "repositories", {"known": [], "excluded": []}
        )
        repositories = self.repositories_by_project.setdefault(project_name, set())
        excluded = {
            RepositoryRef(item["path"], item["type"])
            for item in preferences.get("excluded", [])
        }
        changed = False
        for item in tuple(preferences.get("known", [])):
            repository = RepositoryRef(item.get("path", ""), item.get("type", ""))
            root = self._repository_root(project, repository.path)
            if (
                repository.scm_type in {"hg", "git"}
                and repository not in excluded
                and root is not None
                and is_normal_repository(root, repository.scm_type)
            ):
                self._attach_repository(project, repository)
            else:
                preferences["known"].remove(item)
                changed = True
        if changed:
            self.config.save()
        if (
            project_name not in self.discovery_by_project
            and project_name not in self.scanned_projects
        ):
            self._scan_project_repositories(project)
        return SlateWindow._sorted_repositories(repositories)

    def _attach_repository(self, project: dict, repository: RepositoryRef) -> None:
        """Create the persistent SCM adapter and watcher for one discovered root."""

        project_name = project["name"]
        key = (project_name, repository)
        root = self._repository_root(project, repository.path)
        if root is None or key in self.scm_by_repository:
            return
        scm: SCM = GitSCM(root) if repository.scm_type == "git" else MercurialSCM(root)
        self.scm_by_repository[key] = scm
        self.repositories_by_project.setdefault(project_name, set()).add(repository)

        def status_changed(
            statuses: list[FileStatus],
            branch: str,
            repo_key: tuple[str, RepositoryRef] = key,
        ) -> None:
            """Publish a repository-qualified snapshot to the visible project."""

            qualified = [
                FileStatus(
                    status.path,
                    status.state,
                    status.staged,
                    repo_key[1].path,
                    status.source_path,
                    repo_key[1].scm_type,
                )
                for status in statuses
                if self._status_owned_by_repository(
                    repo_key[0], repo_key[1], status.path
                )
            ]
            self.snapshots[repo_key] = (qualified, branch)
            if repo_key[0] == self.active_project_name:
                self.panel.update_status(qualified, branch, repo_key[1])
                self._update_active_revision_count()
                self.panel.clear_error()
                self._refresh_visible_preview(qualified, repo_key[1])

        def watcher_error(
            message: str, repo_key: tuple[str, RepositoryRef] = key
        ) -> None:
            """Show errors only for repositories in the visible project."""

            if repo_key[0] == self.active_project_name:
                self.panel.show_error(f"{repo_key[1]}: {message}")

        def file_changed(
            relative: str, repo_key: tuple[str, RepositoryRef] = key
        ) -> None:
            """Translate repository paths to the encompassing workspace path."""

            workspace_path = self._workspace_repository_path(repo_key[1].path, relative)
            self.file_manager.project_filesystem_changed(repo_key[0], workspace_path)

        def ignored_changed(
            ignored: set[str], repo_key: tuple[str, RepositoryRef] = key
        ) -> None:
            """Aggregate repository-relative ignore rules for the File manager."""

            self.ignored_by_repository[repo_key] = {
                self._workspace_repository_path(repo_key[1].path, path)
                for path in ignored
            }
            self.file_manager.project_ignored_changed(
                repo_key[0], self._project_ignored_paths(repo_key[0])
            )

        def history_changed(
            repo_key: tuple[str, RepositoryRef] = key,
        ) -> None:
            """Invalidate an explicit remote result after detected history changes."""

            self.panel.set_project_remote_status(
                repo_key[0], repo_key[1], RepositorySyncStatus()
            )

        # 2026-08-17: each working copy owns a watcher, while cached instances
        # survive project switches to avoid rebuilding large monitor trees.
        watcher = RepoWatcher(
            root,
            scm,
            status_changed,
            watcher_error,
            file_changed,
            ignored_changed,
            history_changed,
        )
        self.watchers[key] = watcher
        self._update_repository_boundaries(project_name)
        watcher.set_active(project_name == self.active_project_name)

    def _scan_project_repositories(
        self, project: dict, refresh_existing: bool = False
    ) -> None:
        """Discover repositories and optionally rescan existing watcher metadata."""

        project_name = project["name"]
        existing_repositories = set(
            self.repositories_by_project.get(project_name, set())
        )
        previous = self.discovery_by_project.pop(project_name, None)
        if previous is not None:
            previous.cancel()
        preferences = project.setdefault(
            "repositories", {"known": [], "excluded": []}
        )
        cache_changed = False
        for item in tuple(preferences.get("known", [])):
            repository = RepositoryRef(item.get("path", ""), item.get("type", ""))
            root = self._repository_root(project, repository.path)
            if (
                root is None
                or repository.scm_type not in {"hg", "git"}
                or not is_normal_repository(root, repository.scm_type)
            ):
                preferences["known"].remove(item)
                self._remove_repository_runtime(project_name, repository)
                cache_changed = True
        if cache_changed:
            self.config.save()

        def repository_found(repository: RepositoryRef) -> None:
            """Persist and attach a newly discovered supported working copy."""

            known = preferences.setdefault("known", [])
            if not any(
                item.get("path") == repository.path
                and item.get("type") == repository.scm_type
                for item in known
            ):
                known.append(
                    {"path": repository.path, "type": repository.scm_type}
                )
                self.config.save()
            self._attach_repository(project, repository)
            if project_name == self.active_project_name:
                repositories = self.repositories_by_project.get(project_name, set())
                self.panel.set_repositories(self._sorted_repositories(repositories))

        def scan_complete(error: str | None) -> None:
            """Finish discovery without hiding clean repository nodes."""

            self.discovery_by_project.pop(project_name, None)
            if refresh_existing:
                # 2026-08-17: Scansiona is the sole routine UI action that
                # deliberately rereads branch and complete repository state.
                for repository in existing_repositories:
                    watcher = self.watchers.get((project_name, repository))
                    if watcher is not None:
                        watcher.request_scan()
            if project_name != self.active_project_name:
                return
            repositories = self._sorted_repositories(
                self.repositories_by_project.get(project_name, set())
            )
            self.panel.set_repositories(repositories, False)
            if error:
                self.panel.show_error(f"Repository scan failed: {error}")
            self._queue_unattended_project_verification(
                project_name,
                force=refresh_existing,
            )

        discovery = RepositoryDiscovery(
            project["path"],
            {
                RepositoryRef(item["path"], item["type"])
                for item in preferences.get("excluded", [])
            },
            repository_found,
            scan_complete,
        )
        self.discovery_by_project[project_name] = discovery
        self.scanned_projects.add(project_name)
        if project_name == self.active_project_name:
            self.panel.set_repositories(
                self._sorted_repositories(
                    self.repositories_by_project.get(project_name, set())
                ),
                True,
            )
        discovery.start()

    def _scan_active_project(self) -> None:
        """Run manual repository discovery for the currently visible project."""

        project = self.config.find_project(self.active_project_name or "")
        if project is not None:
            self._scan_project_repositories(project, refresh_existing=True)

    def _queue_unattended_project_verification(
        self, project_name: str, *, force: bool = False
    ) -> None:
        """Queue one lazy sequential remote check for an initialized project."""

        if (
            project_name != self.active_project_name
            or project_name in self.discovery_by_project
            or (not force and project_name in self.verified_projects)
        ):
            return
        self.verified_projects.add(project_name)
        queued = set(self.unattended_verification_queue)
        current = self.unattended_verifier_key
        for repository in self._sorted_repositories(
            self.repositories_by_project.get(project_name, set())
        ):
            key = (project_name, repository)
            # 2026-08-19: a manual Scan made during a running initial check must
            # still schedule one fresh comparison after that in-flight result.
            if key in queued or (key == current and not force):
                continue
            self.unattended_verification_queue.append(key)
            queued.add(key)
        self._start_next_unattended_verification()

    def _start_next_unattended_verification(self) -> None:
        """Start the next queued check while keeping remote traffic single-flight."""

        if self.unattended_verifier is not None:
            return
        while self.unattended_verification_queue:
            key = self.unattended_verification_queue.pop(0)
            project_name, repository = key
            if project_name != self.active_project_name:
                self.verified_projects.discard(project_name)
                continue
            scm = self.scm_by_repository.get(key)
            watcher = self.watchers.get(key)
            if scm is None or watcher is None:
                continue

            def verified(
                status: RepositorySyncStatus,
                repo_key: tuple[str, RepositoryRef] = key,
            ) -> None:
                """Publish a headless result to the owning cached panel model."""

                self.panel.set_project_remote_status(
                    repo_key[0], repo_key[1], status
                )

            def closed(repo_key: tuple[str, RepositoryRef] = key) -> None:
                """Release the completed task and continue the global queue."""

                self._on_unattended_verification_closed(repo_key)

            self.unattended_verifier_key = key
            self.unattended_verifier = UnattendedRepositoryVerifier(
                scm,
                watcher,
                verified,
                closed,
            )
            self.unattended_verifier.start()
            return

    def _on_unattended_verification_closed(
        self, key: tuple[str, RepositoryRef]
    ) -> None:
        """Forget one headless task and launch the next queued repository."""

        if self.unattended_verifier_key != key:
            return
        self.unattended_verifier = None
        self.unattended_verifier_key = None
        self._start_next_unattended_verification()

    def _stop_unattended_verification(self) -> None:
        """Cancel background verification before an explicit repository modal."""

        self.unattended_verification_queue.clear()
        verifier = self.unattended_verifier
        if verifier is not None:
            verifier.close()

    def _remove_unattended_repository_verification(
        self, key: tuple[str, RepositoryRef]
    ) -> None:
        """Drop queued or active verification for a repository being removed."""

        self.unattended_verification_queue = [
            queued for queued in self.unattended_verification_queue if queued != key
        ]
        self.verified_projects.discard(key[0])
        if self.unattended_verifier_key == key and self.unattended_verifier is not None:
            self.unattended_verifier.close()

    def _reset_active_project_repositories(self) -> None:
        """Clear repository cache and exclusions, then rediscover from disk."""

        project = self.config.find_project(self.active_project_name or "")
        if project is None:
            return
        self._remove_project_repository_runtime(project["name"])
        self.scanned_projects.discard(project["name"])
        project["repositories"] = {"known": [], "excluded": []}
        self.config.save()
        self.panel.set_repositories([], True)
        self._scan_project_repositories(project)

    def _exclude_repository(self, repository: RepositoryRef) -> None:
        """Exclude an explicit repository node without touching its files."""

        project = self.config.find_project(self.active_project_name or "")
        if project is None:
            return
        preferences = project.setdefault(
            "repositories", {"known": [], "excluded": []}
        )
        serialized = {"path": repository.path, "type": repository.scm_type}
        if serialized not in preferences["excluded"]:
            preferences["excluded"].append(serialized)
        preferences["known"] = [
            item
            for item in preferences["known"]
            if not (
                item.get("path") == repository.path
                and item.get("type") == repository.scm_type
            )
        ]
        self._remove_repository_runtime(project["name"], repository)
        self.config.save()
        self.panel.set_repositories(
            self._sorted_repositories(
                self.repositories_by_project.get(project["name"], set())
            )
        )
        self._update_active_revision_count()
        if project["name"] in self.discovery_by_project:
            self._scan_project_repositories(project)

    def _remove_repository_runtime(
        self, project_name: str, repository: RepositoryRef
    ) -> None:
        """Close and forget runtime objects belonging to one repository."""

        key = (project_name, repository)
        self._remove_unattended_repository_verification(key)
        watcher = self.watchers.pop(key, None)
        if watcher is not None:
            watcher.close()
        self.scm_by_repository.pop(key, None)
        self.snapshots.pop(key, None)
        self.ignored_by_repository.pop(key, None)
        self.repositories_by_project.setdefault(project_name, set()).discard(repository)
        self._update_repository_boundaries(project_name)
        self._refresh_project_watchers(project_name)

    def _remove_project_repository_runtime(self, project_name: str) -> None:
        """Close discovery and every repository object owned by one project."""

        discovery = self.discovery_by_project.pop(project_name, None)
        if discovery is not None:
            discovery.cancel()
        for name, repository in tuple(self.watchers):
            if name == project_name:
                self._remove_repository_runtime(name, repository)
        self.repositories_by_project.pop(project_name, None)
        self.scanned_projects.discard(project_name)

    def _repository_root(self, project: dict, repository: str) -> str | None:
        """Resolve a cached repository path without allowing workspace escape."""

        root = Path(project["path"]).resolve()
        candidate = root if repository == "." else (root / repository).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            return None
        return str(candidate)

    @staticmethod
    def _workspace_repository_path(repository: str, relative: str) -> str:
        """Prefix a repository path for File-manager operations in the workspace."""

        return relative if repository == "." else f"{repository}/{relative}"

    def _project_ignored_paths(self, project_name: str) -> set[str]:
        """Return all repository ignore paths translated to one workspace."""

        combined: set[str] = set()
        for (name, _repository), ignored in self.ignored_by_repository.items():
            if name == project_name:
                combined.update(ignored)
        return combined

    def _update_repository_boundaries(self, project_name: str) -> None:
        """Tell ancestor watchers which nested working copies they must ignore."""

        repositories = self.repositories_by_project.get(project_name, set())
        for (name, repository), watcher in self.watchers.items():
            if name != project_name:
                continue
            if repository.path == ".":
                nested = {
                    candidate.path
                    for candidate in repositories
                    if candidate.path != "."
                }
            else:
                prefix = f"{repository.path}/"
                nested = {
                    candidate.path[len(prefix):]
                    for candidate in repositories
                    if candidate.path.startswith(prefix)
                }
            watcher.set_nested_repositories(nested)

    def _status_owned_by_repository(
        self, project_name: str, repository: RepositoryRef, path: str
    ) -> bool:
        """Reject ancestor status rows that belong to a discovered child repository."""

        repositories = self.repositories_by_project.get(project_name, set())
        for candidate in repositories:
            if candidate == repository:
                continue
            if repository.path == ".":
                child = candidate.path
            else:
                prefix = f"{repository.path}/"
                if not candidate.path.startswith(prefix):
                    continue
                child = candidate.path[len(prefix):]
            if path == child or path.startswith(f"{child}/"):
                return False
        return True

    def _update_active_revision_count(self) -> None:
        """Publish the aggregate visible status count for the active project."""

        project_name = self.active_project_name or ""
        count = sum(
            len(statuses)
            for (name, _repository), (statuses, _branch) in self.snapshots.items()
            if name == project_name
        )
        self._set_revision_count(count)

    @staticmethod
    def _sorted_repositories(
        repositories: Sequence[RepositoryRef] | set[RepositoryRef],
    ) -> list[RepositoryRef]:
        """Return root-first deterministic repository order across SCM types."""

        return sorted(repositories, key=SlateWindow._repository_sort_key)

    @staticmethod
    def _repository_sort_key(repository: RepositoryRef) -> tuple[bool, str, str]:
        """Build the root-first alphabetical key for one repository reference."""

        return (
            repository.path != ".",
            repository.path.casefold(),
            repository.scm_type,
        )

    def _restore_active_terminal(self) -> None:
        """Restore the active editor or terminal, then use the first project."""

        self.restoring_selection = True
        active_editor = self.config.data.get("editor", {}).get("active_tab")
        if isinstance(active_editor, dict) and self._select_tree_row(
            active_editor.get("project", ""),
            active_editor.get("path", ""),
            "editor",
        ):
            self.restoring_selection = False
            return
        active = self.config.data.get("active_terminal")
        if isinstance(active, str) and "/" in active:
            project_name, terminal_name_value = active.split("/", 1)
            if self._select_tree_row(project_name, terminal_name_value, "terminal"):
                self.restoring_selection = False
                return
        projects = self.config.data["projects"]
        if projects:
            project = projects[0]
            terminal_name_value = project.get("last_terminal")
            self._select_tree_row(
                project["name"], terminal_name_value or "", "terminal"
            )
        else:
            self._show_inactive_workspace()
        self.restoring_selection = False

    def _show_inactive_workspace(self) -> None:
        """Leave central content blank and disable project-specific controls."""

        self.active_project_name = None
        self.active_terminal_name = None
        self.active_editor_ref = None
        self.project_tree.get_selection().unselect_all()
        self.inactive_terminal.show()
        self.terminal_stack.set_visible_child_name("__inactive__")
        self.editor_workspace.show_inactive()
        self.add_terminal_button.set_sensitive(False)
        self.add_command_button.set_sensitive(False)
        self.resume_codex_button.set_sensitive(False)
        add_browser_button = getattr(self, "add_browser_button", None)
        if add_browser_button is not None:
            add_browser_button.set_sensitive(False)
        add_private_browser_button = getattr(self, "add_private_browser_button", None)
        if add_private_browser_button is not None:
            add_private_browser_button.set_sensitive(False)
        self.right_notebook.set_sensitive(False)
        self.file_manager.clear_project()

    def _finish_inactive_startup(self) -> bool:
        """Clear post-map GTK selection before accepting explicit user choices."""

        self._show_inactive_workspace()
        self.startup_inactive = False
        self.terminals.set_activity_monitoring(self.is_active())
        return GLib.SOURCE_REMOVE

    def _select_tree_row(
        self, project_name: str, item_value: str, kind: str = "terminal"
    ) -> bool:
        """Select and reveal a project child by its kind and stable identity."""

        tree_iter = self.project_store.get_iter_first()
        while tree_iter:
            if self.project_store.get_value(tree_iter, self.COL_PROJECT) == project_name:
                target = tree_iter
                child = self.project_store.iter_children(tree_iter)
                while child:
                    if (
                        self.project_store.get_value(child, self.COL_KIND) == kind
                        and self.project_store.get_value(child, self.COL_ITEM)
                        == item_value
                    ):
                        target = child
                        break
                    child = self.project_store.iter_next(child)
                path = self.project_store.get_path(target)
                self.project_tree.get_selection().select_path(path)
                self.project_tree.scroll_to_cell(path, None, True, 0.5, 0.0)
                return True
            tree_iter = self.project_store.iter_next(tree_iter)
        return False

    def _render_tree_name(
        self,
        _column: Gtk.TreeViewColumn,
        renderer: Gtk.CellRendererText,
        model: Gtk.TreeModel,
        tree_iter: Gtk.TreeIter,
        _data: object = None,
    ) -> None:
        """Render row names without changing their searchable stored values."""

        name = model.get_value(tree_iter, self.COL_TEXT)
        project_row = model.get_value(tree_iter, self.COL_KIND) == "project"
        renderer.set_property("text", name)
        renderer.set_property("weight", Pango.Weight.BOLD if project_row else Pango.Weight.NORMAL)
        renderer.set_property("weight-set", True)
        self._set_project_row_background(renderer, project_row)

    def _render_tree_icon(
        self,
        _column: Gtk.TreeViewColumn,
        renderer: Gtk.CellRendererPixbuf,
        model: Gtk.TreeModel,
        tree_iter: Gtk.TreeIter,
        _data: object = None,
    ) -> None:
        """Render terminal, editor and browser icons below iconless projects."""

        kind = model.get_value(tree_iter, self.COL_KIND)
        project_row = kind == "project"
        self._set_project_row_background(renderer, project_row)
        renderer.set_property("visible", not project_row)
        # Gtk reuses one renderer for every row, so clear a custom bitmap before
        # selecting the themed or MIME icon belonging to the current item.
        renderer.set_property("pixbuf", None)
        if project_row:
            renderer.set_property("gicon", None)
            renderer.set_property("icon-name", None)
            return
        if kind == "editor":
            content_type, _uncertain = Gio.content_type_guess(
                model.get_value(tree_iter, self.COL_ITEM), None
            )
            renderer.set_property("icon-name", None)
            renderer.set_property("gicon", Gio.content_type_get_icon(content_type))
            return
        if kind == "browser":
            reference = (
                model.get_value(tree_iter, self.COL_PROJECT),
                model.get_value(tree_iter, self.COL_ITEM),
            )
            entry = self.browser_manager.pages.get(reference)
            renderer.set_property("gicon", None)
            if entry is not None and entry.private:
                renderer.set_property("icon-name", None)
                renderer.set_property("pixbuf", self.incognito_icon)
            else:
                renderer.set_property("icon-name", "web-browser")
            return
        icon_names = {
            "terminal": "utilities-terminal",
        }
        renderer.set_property("gicon", None)
        renderer.set_property("icon-name", icon_names.get(kind))

    def _render_expander_cell(
        self,
        _column: Gtk.TreeViewColumn,
        renderer: Gtk.CellRendererPixbuf,
        model: Gtk.TreeModel,
        tree_iter: Gtk.TreeIter,
        _data: object = None,
    ) -> None:
        """Render a readable project toggle inside the shaded first cell."""

        project_row = model.get_value(tree_iter, self.COL_KIND) == "project"
        renderer.set_property("visible", project_row)
        renderer.set_property("icon-name", None)
        renderer.set_property(
            "gicon",
            Gio.ThemedIcon.new(
                "pan-down"
                if self.project_tree.row_expanded(model.get_path(tree_iter))
                else "pan-end"
            )
            if project_row
            else None,
        )
        self._set_project_row_background(
            renderer, project_row
        )

    def _render_activity_icon(
        self,
        _column: Gtk.TreeViewColumn,
        renderer: Gtk.CellRendererPixbuf,
        model: Gtk.TreeModel,
        tree_iter: Gtk.TreeIter,
        _data: object = None,
    ) -> None:
        """Show terminal activity or unsaved editor state beside the row."""

        active = model.get_value(tree_iter, self.COL_ACTIVITY)
        renderer.set_property("icon-name", "media-record" if active else None)
        self._set_project_row_background(
            renderer, model.get_value(tree_iter, self.COL_KIND) == "project"
        )

    def _render_attention_icon(
        self,
        _column: Gtk.TreeViewColumn,
        renderer: Gtk.CellRendererPixbuf,
        model: Gtk.TreeModel,
        tree_iter: Gtk.TreeIter,
        _data: object = None,
    ) -> None:
        """Render the colored bell for terminals or editors needing attention."""

        attention = model.get_value(tree_iter, self.COL_ATTENTION)
        renderer.set_property("icon-name", None)
        renderer.set_property("pixbuf", self.attention_icon if attention else None)
        self._set_project_row_background(
            renderer, model.get_value(tree_iter, self.COL_KIND) == "project"
        )

    def _set_project_row_background(
        self, renderer: Gtk.CellRenderer, project_row: bool
    ) -> None:
        """Apply the theme-derived gray background only to project cells."""

        # 2026-08-17: una lieve miscela del colore testo con la base rimane
        # grigia e leggibile sia nei temi chiari sia in quelli scuri.
        renderer.set_property("cell-background-set", project_row)
        if project_row:
            renderer.set_property(
                "cell-background-rgba", self.project_row_background
            )

    def _on_tree_selection(self, selection: Gtk.TreeSelection) -> None:
        """Activate the selected terminal, editor or browser without expanding."""

        # 2026-08-17: GtkTreeStore rebuilds emit transient selections; accepting
        # them eagerly activated every project and defeated lazy repository load.
        if self.restoring_tree or self.startup_inactive:
            return
        model, tree_iter = selection.get_selected()
        if tree_iter is None:
            return
        project_name = model.get_value(tree_iter, self.COL_PROJECT)
        kind = model.get_value(tree_iter, self.COL_KIND)
        project = self.config.find_project(project_name)
        if not project:
            return
        item_value = model.get_value(tree_iter, self.COL_ITEM)
        if kind == "editor":
            self._activate_editor(project, item_value)
            return
        if kind == "browser":
            self._activate_browser(project, item_value)
            return
        if kind == "project":
            item_value = project.get("last_terminal") or (
                project["terminals"][0] if project["terminals"] else ""
            )
        self._activate(project, item_value)

    def _activate_editor(self, project: dict, relative_path: str) -> None:
        """Show an editor row while aligning the shared project-side context."""

        terminal_name_value = project.get("last_terminal") or (
            project["terminals"][0] if project["terminals"] else ""
        )
        # 2026-08-16: riutilizziamo l'attivazione del progetto senza mostrare il
        # VTE, così watcher e colonna destra seguono il file selezionato.
        self._activate(project, terminal_name_value, show_terminal=False)
        if not self.editor_workspace.show_editor(project["name"], relative_path):
            self._activate(project, terminal_name_value)
            return
        self.active_editor_ref = (project["name"], relative_path)

    def _activate_browser(self, project: dict, identifier: str) -> None:
        """Show a browser row while aligning its shared project-side context."""

        terminal_name_value = project.get("last_terminal") or (
            project["terminals"][0] if project["terminals"] else ""
        )
        # 2026-08-17: come l'editor, il browser attiva watcher e pannello senza
        # creare il VTE relativo all'ultimo terminale configurato.
        self._activate(project, terminal_name_value, show_terminal=False)
        if not self.browser_manager.show_page(project["name"], identifier):
            self._activate(project, terminal_name_value)
            return
        self.active_editor_ref = None

    def _activate(
        self,
        project: dict,
        terminal_name_value: str,
        show_terminal: bool = True,
    ) -> None:
        """Switch terminal and project panel without remounting a shared watcher."""

        self._close_preview()
        project_changed = self.active_project_name != project["name"]
        valid_terminal = (
            terminal_name_value
            if terminal_name_value and terminal_name_value in project["terminals"]
            else ""
        )
        destination_changed = self.active_project_name is not None and (
            project_changed or self.active_terminal_name != valid_terminal
        )
        # 2026-08-16: il messaggio appartiene al contesto terminale/progetto in
        # cui è stato scritto e viene scartato soltanto passando a un altro.
        if destination_changed:
            self.panel.clear_message()
        self.active_terminal_name = valid_terminal
        if show_terminal:
            self.active_editor_ref = None
        self.add_terminal_button.set_sensitive(True)
        self.add_command_button.set_sensitive(True)
        self.resume_codex_button.set_sensitive(True)
        add_browser_button = getattr(self, "add_browser_button", None)
        if add_browser_button is not None:
            add_browser_button.set_sensitive(True)
        add_private_browser_button = getattr(self, "add_private_browser_button", None)
        if add_private_browser_button is not None:
            add_private_browser_button.set_sensitive(True)
        self.right_notebook.set_sensitive(True)
        if valid_terminal:
            if show_terminal:
                # 2026-08-17: configured rows are cheap; only an explicit
                # terminal selection materializes its VTE/tmux client once.
                initial_command = project.get("terminal_commands", {}).get(
                    terminal_name_value
                )
                self.terminals.add(
                    project,
                    terminal_name_value,
                    initial_command=initial_command,
                )
                self.terminals.show(project["name"], terminal_name_value)
                self.editor_workspace.show_terminal()
            project["last_terminal"] = terminal_name_value
            self.config.data["active_terminal"] = terminal_key(
                project["name"], terminal_name_value
            )
            if not self.restoring_selection:
                self.config.save()
        else:
            self.terminal_stack.set_visible_child_name("__empty__")
            if show_terminal:
                self.editor_workspace.show_terminal()
            self.config.data["active_terminal"] = None
        if not project_changed:
            return
        # 2026-08-16: ogni cambio progetto riparte dalla vista Revisioni come
        # scelta esplicita, mentre filtri ed espansioni File restano persistiti.
        self.right_notebook.set_current_page(0)
        self.file_manager.set_active(False)
        self.active_project_name = project["name"]
        # 2026-08-17: attach the cached project model before discovery; binding
        # it as supported avoids clearing clean repository roots during switch.
        self.panel.bind_project(project["name"], True)
        repositories = self._ensure_project_repositories(project)
        for (name, _repository), watcher in self.watchers.items():
            watcher.set_active(name == self.active_project_name)
        self.panel.set_repositories(
            repositories, project["name"] in self.discovery_by_project
        )
        self.panel.clear_error()
        for repository in repositories:
            cached = self.snapshots.get((project["name"], repository))
            if cached:
                self.panel.update_status(*cached, repository)
        self._update_active_revision_count()
        self._queue_unattended_project_verification(project["name"])

    def _on_row_expanded(
        self, _tree: Gtk.TreeView, tree_iter: Gtk.TreeIter, _path: Gtk.TreePath
    ) -> None:
        """Persist expansion only when it changes through the tree interaction."""

        if self.restoring_tree:
            return
        name = self.project_store.get_value(tree_iter, self.COL_PROJECT)
        expanded = self.config.data["expanded_projects"]
        if name not in expanded:
            expanded.append(name)
            self.config.save()

    def _on_row_collapsed(
        self, _tree: Gtk.TreeView, tree_iter: Gtk.TreeIter, _path: Gtk.TreePath
    ) -> None:
        """Persist an explicit project collapse without affecting other rows."""

        if self.restoring_tree:
            return
        name = self.project_store.get_value(tree_iter, self.COL_PROJECT)
        expanded = self.config.data["expanded_projects"]
        if name in expanded:
            expanded.remove(name)
            self.config.save()

    def _on_tree_button(self, tree: Gtk.TreeView, event: Gdk.EventButton) -> bool:
        """Suppress parent double-click expansion and open contextual actions."""

        self.project_drag_candidate = None
        hit = tree.get_path_at_pos(int(event.x), int(event.y))
        if hit:
            path = hit[0]
            tree_iter = self.project_store.get_iter(path)
            kind = self.project_store.get_value(tree_iter, self.COL_KIND)
            if (
                event.button == 1
                and kind == "project"
                and hit[1] is self.project_expander_column
            ):
                tree.get_selection().select_path(path)
                if tree.row_expanded(path):
                    tree.collapse_row(path)
                else:
                    tree.expand_row(path, False)
                tree.queue_draw()
                return True
            if (
                event.button == 1
                and event.state & Gdk.ModifierType.CONTROL_MASK
            ):
                self.project_drag_candidate = (
                    (
                        kind,
                        self.project_store.get_value(tree_iter, self.COL_PROJECT),
                        self.project_store.get_value(tree_iter, self.COL_ITEM),
                    ),
                    path.copy(),
                    int(event.x),
                    int(event.y),
                )
                # Il press Ctrl non seleziona la sorgente: un riordino non deve
                # cambiare terminale, editor o progetto attivo.
                return True
            tree.get_selection().select_path(path)
            if event.type == Gdk.EventType.DOUBLE_BUTTON_PRESS and kind == "project":
                return True
            if event.button == 3:
                self._show_tree_menu(kind, event)
                return True
        return False

    def _on_tree_button_release(
        self, _tree: Gtk.TreeView, _event: Gdk.EventButton
    ) -> bool:
        """Discard a Ctrl press that ended before crossing the drag threshold."""

        self.project_drag_candidate = None
        return False

    def _on_tree_motion(self, tree: Gtk.TreeView, event: Gdk.EventMotion) -> bool:
        """Start one internal sidebar drag after Ctrl+left crosses GTK's threshold."""

        candidate = self.project_drag_candidate
        if candidate is None:
            return False
        required = Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.BUTTON1_MASK
        if event.state & required != required:
            self.project_drag_candidate = None
            return False
        identity, path, start_x, start_y = candidate
        if not tree.drag_check_threshold(
            start_x, start_y, int(event.x), int(event.y)
        ):
            return True
        self.project_drag_candidate = None
        self.project_drag_source = identity
        context = tree.drag_begin(
            self.project_drag_targets, self.PROJECT_DRAG_ACTIONS, 1, event
        )
        Gtk.drag_set_icon_surface(context, tree.create_row_drag_icon(path))
        return True

    def _sidebar_drop_order(
        self,
        source: _SidebarIdentity,
        target: _SidebarIdentity,
        before: bool,
    ) -> list[object] | None:
        """Plan a valid project or same-project child reorder without mutating state."""

        source_kind, source_project, source_item = source
        target_kind, target_project, target_item = target
        if source_kind == "project":
            if target_kind != "project":
                return None
            names = [project["name"] for project in self.config.data["projects"]]
            return _moved_sequence(names, source_project, target_project, before)
        if (
            source_kind not in {"terminal", "editor", "browser"}
            or target_kind not in {"terminal", "editor", "browser"}
            or source_project != target_project
        ):
            return None
        project = self.config.find_project(source_project)
        if project is None:
            return None
        items = [
            (item.get("kind", ""), item.get("value", ""))
            for item in project.setdefault("item_order", [])
        ]
        return _moved_sequence(
            items,
            (source_kind, source_item),
            (target_kind, target_item),
            before,
        )

    def _sidebar_drop_at(
        self, tree: Gtk.TreeView, x: int, y: int
    ) -> tuple[
        _SidebarIdentity,
        bool,
        Gtk.TreePath,
        Gtk.TreeViewDropPosition,
    ] | None:
        """Resolve coordinates to one effective drop on the same sidebar level."""

        source = self.project_drag_source
        if source is None:
            return None
        destination = tree.get_dest_row_at_pos(x, y)
        if destination is None:
            return None
        # 2026-08-17: PyGObject omette il gboolean C dalla tupla restituita;
        # il binding GTK 3 espone quindi soltanto TreePath e posizione.
        path, position = destination
        target_iter = self.project_store.get_iter(path)
        target = (
            self.project_store.get_value(target_iter, self.COL_KIND),
            self.project_store.get_value(target_iter, self.COL_PROJECT),
            self.project_store.get_value(target_iter, self.COL_ITEM),
        )
        before = position in {
            Gtk.TreeViewDropPosition.BEFORE,
            Gtk.TreeViewDropPosition.INTO_OR_BEFORE,
        }
        normalized = (
            Gtk.TreeViewDropPosition.BEFORE
            if before
            else Gtk.TreeViewDropPosition.AFTER
        )
        if self._sidebar_drop_order(source, target, before) is None:
            return None
        indicator_path = path
        if source[0] == "project" and not before:
            # 2026-08-17: AFTER su un parent viene disegnato da GTK subito sotto
            # l'intestazione, sembrando un drop interno. Mostriamo invece il
            # confine dopo l'intero blocco progetto, inclusi i figli visibili.
            next_project = self.project_store.iter_next(target_iter)
            if next_project is not None:
                indicator_path = self.project_store.get_path(next_project)
                normalized = Gtk.TreeViewDropPosition.BEFORE
            else:
                last_child = self.project_store.iter_children(target_iter)
                if last_child is not None:
                    following_child = self.project_store.iter_next(last_child)
                    while following_child is not None:
                        last_child = following_child
                        following_child = self.project_store.iter_next(last_child)
                    indicator_path = self.project_store.get_path(last_child)
        return target, before, indicator_path, normalized

    def _find_sidebar_iter(
        self, identity: _SidebarIdentity
    ) -> Gtk.TreeIter | None:
        """Find a sidebar row by its stable kind, project and item identity."""

        kind, project_name, item = identity
        # 2026-08-17: la ricerca sui due soli livelli usa identità stabili invece
        # dei TreePath, che cambiano proprio durante il riordino in-place.
        parent = self.project_store.get_iter_first()
        while parent:
            if kind == "project":
                if self.project_store.get_value(parent, self.COL_PROJECT) == project_name:
                    return parent
            else:
                child = self.project_store.iter_children(parent)
                while child:
                    if (
                        self.project_store.get_value(child, self.COL_KIND) == kind
                        and self.project_store.get_value(child, self.COL_PROJECT)
                        == project_name
                        and self.project_store.get_value(child, self.COL_ITEM) == item
                    ):
                        return child
                    child = self.project_store.iter_next(child)
            parent = self.project_store.iter_next(parent)
        return None

    def _apply_sidebar_drop(
        self,
        source: _SidebarIdentity,
        target: _SidebarIdentity,
        before: bool,
    ) -> bool:
        """Persist and mirror one already validated sidebar reorder in place."""

        reordered = self._sidebar_drop_order(source, target, before)
        source_iter = self._find_sidebar_iter(source)
        target_iter = self._find_sidebar_iter(target)
        if reordered is None or source_iter is None or target_iter is None:
            return False
        if source[0] == "project":
            projects_by_name = {
                project["name"]: project for project in self.config.data["projects"]
            }
            self.config.data["projects"] = [
                projects_by_name[str(name)] for name in reordered
            ]
        else:
            project = self.config.find_project(source[1])
            if project is None:
                return False
            project["item_order"] = [
                {"kind": str(kind), "value": str(value)}
                for kind, value in reordered
            ]
        if before:
            self.project_store.move_before(source_iter, target_iter)
        else:
            self.project_store.move_after(source_iter, target_iter)
        self.config.save()
        return True

    def _clear_tree_drag_destination(self, tree: Gtk.TreeView) -> None:
        """Remove GTK's insertion marker after an invalid or completed drag."""

        tree.set_drag_dest_row(None, Gtk.TreeViewDropPosition.BEFORE)

    def _on_tree_drag_data_get(
        self,
        _tree: Gtk.TreeView,
        _context: Gdk.DragContext,
        selection_data: Gtk.SelectionData,
        _info: int,
        _time: int,
    ) -> None:
        """Publish an opaque same-widget token without serializing row contents."""

        if self.project_drag_source is not None:
            selection_data.set(
                selection_data.get_target(), 8, self.PROJECT_DRAG_PAYLOAD
            )

    def _on_tree_drag_motion(
        self,
        tree: Gtk.TreeView,
        context: Gdk.DragContext,
        x: int,
        y: int,
        time: int,
    ) -> bool:
        """Expose a move cursor and insertion marker only for valid destinations."""

        drop = self._sidebar_drop_at(tree, x, y)
        if drop is None:
            self._clear_tree_drag_destination(tree)
            Gdk.drag_status(context, Gdk.DragAction(0), time)
            return True
        _target, _before, path, position = drop
        tree.set_drag_dest_row(path, position)
        protocol_action = context.get_suggested_action()
        if protocol_action not in {Gdk.DragAction.COPY, Gdk.DragAction.MOVE}:
            protocol_action = Gdk.DragAction.COPY
        Gdk.drag_status(context, protocol_action, time)
        return True

    def _on_tree_drag_drop(
        self,
        tree: Gtk.TreeView,
        context: Gdk.DragContext,
        x: int,
        y: int,
        time: int,
    ) -> bool:
        """Request the internal token only after validating the final coordinates."""

        if self._sidebar_drop_at(tree, x, y) is None:
            self._clear_tree_drag_destination(tree)
            return False
        target = tree.drag_dest_find_target(context, None)
        if target is None:
            return False
        tree.drag_get_data(context, target, time)
        return True

    def _on_tree_drag_data_received(
        self,
        tree: Gtk.TreeView,
        context: Gdk.DragContext,
        x: int,
        y: int,
        selection_data: Gtk.SelectionData,
        _info: int,
        time: int,
    ) -> None:
        """Apply a valid internal reorder and finish the DnD transaction once."""

        source = self.project_drag_source
        drop = self._sidebar_drop_at(tree, x, y)
        payload = bytes(selection_data.get_data() or b"")
        success = False
        if source is not None and drop is not None and payload == self.PROJECT_DRAG_PAYLOAD:
            target, before, _path, _position = drop
            success = self._apply_sidebar_drop(source, target, before)
        self._clear_tree_drag_destination(tree)
        Gtk.drag_finish(context, success, False, time)

    def _on_tree_drag_leave(
        self, tree: Gtk.TreeView, _context: Gdk.DragContext, _time: int
    ) -> None:
        """Clear insertion feedback when the pointer exits the sidebar."""

        self._clear_tree_drag_destination(tree)

    def _on_tree_drag_end(
        self, tree: Gtk.TreeView, _context: Gdk.DragContext
    ) -> None:
        """Release all transient drag state after success or cancellation."""

        self.project_drag_candidate = None
        self.project_drag_source = None
        self._clear_tree_drag_destination(tree)

    def _on_tree_popup_menu(self, _tree: Gtk.TreeView) -> bool:
        """Open the selected row context menu from Menu or Shift+F10."""

        model, tree_iter = self.project_tree.get_selection().get_selected()
        if tree_iter is None:
            return False
        self._show_tree_menu(model.get_value(tree_iter, self.COL_KIND), None)
        return True

    def _show_tree_menu(
        self, kind: str, event: Gdk.EventButton | None
    ) -> None:
        """Show actions appropriate to a project or one of its child rows."""

        menu = Gtk.Menu()
        if kind == "project":
            add_item = Gtk.MenuItem(label="Open Terminal")
            remove_item = Gtk.MenuItem(label="Remove Project")
            add_item.connect("activate", self._on_add_terminal)
            remove_item.connect("activate", self._on_remove_project)
            menu.append(add_item)
            menu.append(remove_item)
        elif kind == "terminal":
            rename_item = Gtk.MenuItem(label="Rename")
            close_item = Gtk.MenuItem(label="Close")
            rename_item.connect("activate", self._prompt_terminal_rename)
            close_item.connect("activate", self._on_close_terminal)
            menu.append(rename_item)
            menu.append(close_item)
        elif kind == "editor":
            close_item = Gtk.MenuItem(label="Close")
            close_item.connect("activate", self._on_close_editor)
            menu.append(close_item)
        elif kind == "browser":
            close_item = Gtk.MenuItem(label="Close")
            close_item.connect("activate", self._on_close_browser)
            menu.append(close_item)
        menu.show_all()
        if event is not None:
            menu.popup_at_pointer(event)
        else:
            menu.popup_at_widget(
                self.project_tree,
                Gdk.Gravity.CENTER,
                Gdk.Gravity.CENTER,
                None,
            )

    def _on_close_editor(self, *_args: object) -> None:
        """Close the selected editor row while protecting unsaved changes."""

        model, tree_iter = self.project_tree.get_selection().get_selected()
        if tree_iter is None or model.get_value(tree_iter, self.COL_KIND) != "editor":
            return
        reference = (
            model.get_value(tree_iter, self.COL_PROJECT),
            model.get_value(tree_iter, self.COL_ITEM),
        )
        self.editor_workspace.request_close_reference(reference)

    def _on_close_browser(self, *_args: object) -> None:
        """Close the runtime browser explicitly targeted in the project tree."""

        model, tree_iter = self.project_tree.get_selection().get_selected()
        if tree_iter is None or model.get_value(tree_iter, self.COL_KIND) != "browser":
            return
        reference = (
            model.get_value(tree_iter, self.COL_PROJECT),
            model.get_value(tree_iter, self.COL_ITEM),
        )
        self.browser_manager.close_page(reference)

    def _on_key_press(self, _widget: Gtk.Widget, event: Gdk.EventKey) -> bool:
        """Close previews or handle global shortcuts while preserving tree arrows."""

        if self.browser_manager.handle_key(event):
            return True
        if self.editor_workspace.handle_key(event):
            return True
        if event.keyval == Gdk.KEY_Escape and self.preview.get_visible():
            self._close_preview()
            return True
        if event.keyval == Gdk.KEY_q and event.state & Gdk.ModifierType.CONTROL_MASK:
            self._request_close()
            return True
        if event.keyval == Gdk.KEY_F2:
            self._prompt_terminal_rename()
            return True
        return False

    def _prompt_terminal_rename(self, *_args: object) -> None:
        """Request a terminal session name in a modal text-entry dialog."""

        selected = self._selected_terminal()
        if selected is None:
            return
        project, old_name = selected
        project_name = project["name"]
        dialog = Gtk.Dialog(
            title="Rename Terminal",
            transient_for=self,
            modal=True,
        )
        dialog.add_buttons(
            "Cancel", Gtk.ResponseType.CANCEL, "Rename", Gtk.ResponseType.OK
        )
        entry = Gtk.Entry()
        entry.set_text(old_name)
        entry.select_region(0, -1)
        entry.set_activates_default(True)
        entry.set_margin_start(12)
        entry.set_margin_end(12)
        entry.set_margin_top(12)
        entry.set_margin_bottom(12)
        dialog.get_content_area().add(entry)
        dialog.set_default_response(Gtk.ResponseType.OK)
        dialog.show_all()
        response = dialog.run()
        clean_name = entry.get_text().strip()
        dialog.destroy()
        if response != Gtk.ResponseType.OK or clean_name == old_name:
            return
        error = self._validate_terminal_name(project, clean_name, old_name)
        if error:
            self._show_error(error)
            return

        def renamed(success: bool) -> None:
            """Update config and tree only after the session rename succeeds."""

            if not success or not project:
                return
            index = project["terminals"].index(old_name)
            project["terminals"][index] = clean_name
            terminal_commands = project.setdefault("terminal_commands", {})
            if old_name in terminal_commands:
                # 2026-08-18: il launcher appartiene al terminale persistente,
                # quindi una rinomina deve conservarne il comportamento.
                terminal_commands[clean_name] = terminal_commands.pop(old_name)
            for item in project.setdefault("item_order", []):
                if item.get("kind") == "terminal" and item.get("value") == old_name:
                    item["value"] = clean_name
                    break
            if project.get("last_terminal") == old_name:
                project["last_terminal"] = clean_name
            self.config.data["active_terminal"] = terminal_key(project_name, clean_name)
            self.config.save()
            self._populate_projects()
            self._select_tree_row(project_name, clean_name)

        self.terminals.rename(project_name, old_name, clean_name, renamed)

    def _on_add_project(self, _button: Gtk.Widget) -> None:
        """Choose and explicitly add one local project directory."""

        dialog = Gtk.FileChooserDialog(
            title="New Project",
            transient_for=self,
            action=Gtk.FileChooserAction.SELECT_FOLDER,
        )
        dialog.add_buttons(
            "Cancel", Gtk.ResponseType.CANCEL, "Add", Gtk.ResponseType.OK
        )
        response = dialog.run()
        selected = dialog.get_filename()
        dialog.destroy()
        if response != Gtk.ResponseType.OK or not selected:
            return
        path = str(Path(selected).resolve())
        if any(project["path"] == path for project in self.config.data["projects"]):
            self._show_error("The project has already been added.")
            return
        name = Path(path).name
        error = self._validate_project_name(name)
        if error:
            self._show_error(error)
            return
        project = new_project_config(name, path, ("main",))
        self.config.data["projects"].append(project)
        self.config.data["expanded_projects"].append(name)
        self.config.save()
        self._populate_projects()
        self.empty_label.hide()
        self._select_tree_row(name, "main")

    def _on_remove_project(self, _button: Gtk.Widget) -> None:
        """Ask whether configured terminal sessions should survive project removal."""

        project = self._selected_project()
        if not project:
            return
        self.editor_workspace.request_close_project(
            project["name"], partial(self._after_project_editors_closed, project)
        )

    def _after_project_editors_closed(
        self, project: dict, proceed: bool
    ) -> None:
        """Continue project removal only after dirty editors are resolved."""

        if proceed:
            self.terminals.query_panes(
                partial(self._show_remove_project_dialog, project)
            )

    def _show_remove_project_dialog(
        self, project: dict, panes: list[PaneInfo]
    ) -> None:
        """List project foreground activity before removal or termination."""

        project_sessions = {
            session_name(project["name"], terminal_name_value)
            for terminal_name_value in project["terminals"]
        }
        relevant = [pane for pane in panes if pane.session in project_sessions]
        dialog = Gtk.MessageDialog(
            transient_for=self,
            modal=True,
            message_type=Gtk.MessageType.WARNING,
            buttons=Gtk.ButtonsType.NONE,
            text=f"Remove {project['name']} from SLATE?",
        )
        if not relevant:
            # 2026-08-17: senza sessioni tmux reali non esiste alcuna scelta
            # lascia/termina; proporla in base alla sola config è fuorviante.
            dialog.format_secondary_text(
                "No running tmux sessions were found. "
                "The project files will not be deleted."
            )
            dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
            dialog.add_button("Remove", Gtk.ResponseType.OK)
            response = dialog.run()
            dialog.destroy()
            if response == Gtk.ResponseType.OK:
                for terminal_name_value in tuple(project["terminals"]):
                    self.terminals.forget(project["name"], terminal_name_value)
                self._finish_project_removal(project)
            return
        details = ["You can leave tmux sessions in the background or terminate them."]
        for pane in relevant:
            if pane.active:
                details.append(f"⚠ {pane.session}: {pane.command} is running")
            else:
                details.append(f"✓ {pane.session}: shell at prompt")
        dialog.format_secondary_text("\n".join(details))
        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dialog.add_button("Leave in Background", 1)
        dialog.add_button("Terminate Sessions", 2)
        response = dialog.run()
        dialog.destroy()
        if response == 1:
            for terminal_name_value in tuple(project["terminals"]):
                self.terminals.forget(project["name"], terminal_name_value)
            self._finish_project_removal(project)
        elif response == 2:
            self._kill_project_sessions(project)

    def _kill_project_sessions(self, project: dict) -> None:
        """Terminate every project session before removing its configuration."""

        pending = set(project["terminals"])
        if not pending:
            self._finish_project_removal(project)
            return

        def make_closed(name: str):
            """Create a completion handler for one terminal name."""

            def closed(success: bool) -> None:
                """Finish removal only when all explicit kills succeed."""

                if success:
                    pending.discard(name)
                if not pending:
                    self._finish_project_removal(project)

            return closed

        for terminal_name_value in tuple(pending):
            self.terminals.close(
                project["name"], terminal_name_value, make_closed(terminal_name_value)
            )

    def _finish_project_removal(self, project: dict) -> None:
        """Remove project UI/config and close its shared watcher."""

        # 2026-08-17: la rimozione ricostruisce l'intero albero subito dopo;
        # evitare il callback intermedio impedisce di riattivare il progetto.
        self.browser_manager.close_project(project["name"], notify=False)
        self._remove_project_repository_runtime(project["name"])
        self.panel.forget_project(project["name"])
        self.file_manager.forget_project(project["name"])
        self.revision_counts.pop(project["name"], None)
        self.config.data["projects"].remove(project)
        if project["name"] in self.config.data["expanded_projects"]:
            self.config.data["expanded_projects"].remove(project["name"])
        self.config.data["active_terminal"] = None
        self.config.save()
        self.active_project_name = None
        self._populate_projects()
        self._restore_active_terminal()
        self.empty_label.set_visible(not bool(self.config.data["projects"]))

    def _on_add_terminal(self, _button: Gtk.Widget) -> None:
        """Add a terminal immediately with the next available default name."""

        self._create_terminal(None)

    def _on_add_command(self, _button: Gtk.Widget) -> None:
        """Prompt for one persistent shell command and create its terminal."""

        dialog = Gtk.Dialog(title="Execute", transient_for=self, modal=True)
        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dialog.add_button("Create", Gtk.ResponseType.OK)
        dialog.set_default_response(Gtk.ResponseType.OK)
        content = dialog.get_content_area()
        content.set_spacing(6)
        content.set_border_width(12)
        label = Gtk.Label(label="Command")
        label.set_xalign(0)
        entry = Gtk.Entry()
        entry.set_activates_default(True)
        error_label = Gtk.Label()
        error_label.set_xalign(0)
        error_label.set_line_wrap(True)
        error_label.get_style_context().add_class("error")
        content.pack_start(label, False, False, 0)
        content.pack_start(entry, False, False, 0)
        content.pack_start(error_label, False, False, 0)
        dialog.show_all()
        entry.grab_focus()

        command = ""
        name_prefix = ""
        while dialog.run() == Gtk.ResponseType.OK:
            command = entry.get_text().strip()
            if not command or "\n" in command or "\r" in command or "\0" in command:
                error_label.set_text("Enter a valid single-line command.")
                continue
            try:
                words = shlex.split(command, posix=True)
            except ValueError:
                error_label.set_text("The command quoting is invalid.")
                continue
            # 2026-08-18: gli assegnamenti iniziali configurano l'ambiente ma
            # non identificano l'eseguibile utile per nominare il terminale.
            executable = next(
                (
                    word
                    for word in words
                    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", word)
                ),
                "",
            )
            executable_name = os.path.basename(executable.rstrip("/"))
            name_prefix = slug(executable_name, 20) or "command"
            break
        dialog.destroy()
        if not command or not name_prefix:
            return
        self._create_terminal(command, name_prefix=name_prefix)

    def _on_add_browser(self, _button: Gtk.Widget) -> None:
        """Open one persistent browser page under the selected project."""

        project = self._selected_project()
        if project is not None:
            self.browser_manager.open_page(project["name"])

    def _on_add_private_browser(self, _button: Gtk.Widget) -> None:
        """Open one persistently listed browser with an ephemeral web profile."""

        project = self._selected_project()
        if project is not None:
            self.browser_manager.open_page(project["name"], private=True)

    def _on_resume_codex(self, _button: Gtk.Widget) -> None:
        """Create a terminal and immediately open the Codex resume selector."""

        self._create_terminal("codex resume", name_prefix="codex")

    def _create_terminal(
        self,
        initial_command: str | None,
        working_directory: str | None = None,
        *,
        name_prefix: str | None = None,
    ) -> None:
        """Create one configured terminal with an optional first shell command."""

        project = self._selected_project()
        if not project:
            return
        if name_prefix:
            # 2026-08-18: launcher differenti conservano contatori leggibili e
            # indipendenti; il prefisso viene accorciato per mantenere sempre il
            # suffisso numerico dentro il limite tmux di venti caratteri.
            normalized_prefix = slug(name_prefix, 20) or "command"
            existing_slugs = {slug(existing, 20) for existing in project["terminals"]}
            number = 1
            while True:
                suffix = f"-{number}"
                bounded_prefix = normalized_prefix[: 20 - len(suffix)].rstrip("-")
                name = f"{bounded_prefix or 'command'}{suffix}"
                if (
                    name not in project["terminals"]
                    and slug(name, 20) not in existing_slugs
                ):
                    break
                number += 1
        elif not project["terminals"]:
            name = "main"
        else:
            number = 2
            while f"term-{number}" in project["terminals"]:
                number += 1
            name = f"term-{number}"
        project["terminals"].append(name)
        if initial_command:
            project.setdefault("terminal_commands", {})[name] = initial_command
        self._append_project_item(project, "terminal", name)
        project["last_terminal"] = name
        self.config.data["active_terminal"] = terminal_key(project["name"], name)
        self.config.save()
        if working_directory is None:
            self.terminals.add(
                project,
                name,
                initial_command=initial_command,
            )
        else:
            self.terminals.add(
                project,
                name,
                initial_command=initial_command,
                working_directory=working_directory,
            )
        self._populate_projects()
        self._select_tree_row(project["name"], name)

    def _open_terminal_in_project_directory(self, relative_path: str) -> None:
        """Create and select a terminal whose initial cwd is one safe directory."""

        absolute_path = self._project_file_path(relative_path)
        if absolute_path is None or not os.path.isdir(absolute_path):
            self._show_file_error(f"The directory does not exist: {relative_path}")
            return
        # 2026-08-16: una nuova sessione evita di inviare `cd` a un terminale
        # che potrebbe contenere un agente o un altro processo in foreground.
        self._create_terminal(None, absolute_path)

    def _set_revision_count(self, count: int) -> None:
        """Mirror the received active-project count on its tab and sidebar row."""

        self.changes_tab.set_text(f"Changes ({count})" if count else "Changes")
        project_name = getattr(self, "active_project_name", None)
        if not project_name:
            return
        self.revision_counts[project_name] = count
        project_iter = self._find_sidebar_iter(("project", project_name, ""))
        if project_iter is not None:
            self.project_store.set_value(
                project_iter,
                self.COL_TEXT,
                f"{project_name} ({count})" if count else project_name,
            )

    def _on_close_terminal(self, *_args: object) -> None:
        """Confirm and kill the explicitly selected terminal session."""

        selected = self._selected_terminal()
        if not selected:
            return
        project, terminal_name_value = selected
        dialog = Gtk.MessageDialog(
            transient_for=self,
            modal=True,
            message_type=Gtk.MessageType.WARNING,
            buttons=Gtk.ButtonsType.OK_CANCEL,
            text=f"Close terminal {terminal_name_value}?",
        )
        dialog.format_secondary_text("The tmux session and its processes will terminate.")
        response = dialog.run()
        dialog.destroy()
        if response != Gtk.ResponseType.OK:
            return

        def closed(success: bool) -> None:
            """Remove terminal config after tmux termination succeeds."""

            if not success:
                return
            self._remove_terminal_configuration(project, terminal_name_value)

        self.terminals.close(project["name"], terminal_name_value, closed)

    def _on_terminal_exited(
        self, project_name: str, terminal_name_value: str, _status: int
    ) -> None:
        """Drop config for a shell/session that has already ended by itself."""

        project = self.config.find_project(project_name)
        if project is not None:
            self._remove_terminal_configuration(project, terminal_name_value)

    def _remove_terminal_configuration(
        self, project: dict, terminal_name_value: str
    ) -> None:
        """Remove one terminal from config and select a deterministic fallback."""

        if terminal_name_value not in project["terminals"]:
            return
        dead_key = terminal_key(project["name"], terminal_name_value)
        active_key = self.config.data.get("active_terminal")
        project["terminals"].remove(terminal_name_value)
        project.setdefault("terminal_commands", {}).pop(terminal_name_value, None)
        project["item_order"] = [
            item
            for item in project.setdefault("item_order", [])
            if not (
                item.get("kind") == "terminal"
                and item.get("value") == terminal_name_value
            )
        ]
        if project.get("last_terminal") == terminal_name_value:
            project["last_terminal"] = (
                project["terminals"][0] if project["terminals"] else None
            )
        if active_key == dead_key:
            fallback = project.get("last_terminal") or ""
            active_key = terminal_key(project["name"], fallback) if fallback else None
            self.config.data["active_terminal"] = active_key
        self.config.save()
        self._populate_projects()
        # 2026-08-16: una ricostruzione dell'albero non deve spostare l'utente
        # su un altro progetto quando termina un terminale non attivo.
        if isinstance(active_key, str) and "/" in active_key:
            active_project, active_terminal = active_key.split("/", 1)
            self._select_tree_row(active_project, active_terminal)
        else:
            self._select_tree_row(project["name"], "")

    def _on_orphans(self, _button: Gtk.Widget) -> None:
        """Query and present dedicated-server sessions missing from config."""

        configured_sessions = {
            session_name(project["name"], terminal_name_value)
            for project in self.config.data["projects"]
            for terminal_name_value in project["terminals"]
        }
        self.terminals.list_orphans(
            configured_sessions, self._show_orphans_dialog
        )

    def _show_orphans_dialog(self, orphans: list[OrphanSession]) -> None:
        """Allow explicit adoption or termination of one orphan at a time."""

        if not orphans:
            self._info("Orphan Sessions", "No orphan sessions.")
            return
        dialog = Gtk.Dialog(title="Orphan Sessions", transient_for=self, modal=True)
        dialog.add_button("Close", Gtk.ResponseType.CLOSE)
        dialog.add_button("Terminate", 2)
        dialog.add_button("Adopt", 1)
        combo = Gtk.ComboBoxText()
        for orphan in orphans:
            path = orphan.project_path or orphan.session_path
            combo.append_text(f"{orphan.session} — {path}")
        combo.set_active(0)
        dialog.get_content_area().pack_start(combo, False, False, 12)
        dialog.show_all()
        response = dialog.run()
        index = combo.get_active()
        dialog.destroy()
        if index < 0 or response not in (1, 2):
            return
        orphan = orphans[index]
        if response == 2:
            self.terminals.kill_session(orphan.session, self._on_orphan_killed)
        else:
            self._adopt_orphan(orphan)

    def _adopt_orphan(self, orphan: OrphanSession) -> None:
        """Recreate config from tmux metadata after explicit user selection."""

        project_path = orphan.project_path or orphan.session_path
        project_name = orphan.project_name or Path(project_path).name
        terminal_name_value = orphan.terminal_name
        if not terminal_name_value and "--" in orphan.session:
            terminal_name_value = orphan.session.split("--", 1)[1]
        if (
            not project_path
            or not Path(project_path).is_dir()
            or not project_name
            or not terminal_name_value
            or session_name(project_name, terminal_name_value) != orphan.session
        ):
            self._show_error(
                "The session does not contain enough metadata for safe adoption."
            )
            return
        project = self.config.find_project(project_name)
        if project is None:
            if any(
                existing["path"] == project_path
                for existing in self.config.data["projects"]
            ):
                self._show_error("The session path already belongs to another project.")
                return
            error = self._validate_project_name(project_name)
            if error:
                self._show_error(error)
                return
            project = new_project_config(project_name, project_path)
            self.config.data["projects"].append(project)
            if project_name not in self.config.data["expanded_projects"]:
                self.config.data["expanded_projects"].append(project_name)
        elif project["path"] != project_path:
            self._show_error("The tmux metadata specifies a different path from the existing project.")
            return
        terminal_error = self._validate_terminal_name(
            project, terminal_name_value, terminal_name_value
        )
        if terminal_error and terminal_name_value not in project["terminals"]:
            self._show_error(terminal_error)
            return
        if terminal_name_value not in project["terminals"]:
            project["terminals"].append(terminal_name_value)
            self._append_project_item(project, "terminal", terminal_name_value)
        project["last_terminal"] = terminal_name_value
        self.config.save()
        self._populate_projects()
        self._select_tree_row(project_name, terminal_name_value)

    def _on_orphan_killed(self, result: CommandResult) -> None:
        """Report an orphan termination failure without altering configuration."""

        if not result.ok:
            self._show_error(result.stderr.strip() or "Unable to terminate the session.")

    def _commit(self, message: str, statuses: list[FileStatus]) -> None:
        """Commit checked files repository-by-repository, stopping on first error."""

        if not message:
            self.panel.show_error("Enter a commit message.")
            return
        if not statuses:
            self.panel.show_error("Select at least one file to commit.")
            return
        untracked = [status.path for status in statuses if status.state == "untracked"]
        if untracked:
            self.panel.show_error(
                "Untracked files are not added automatically. "
                "Use Add and try again."
            )
            return
        groups = SlateWindow._group_statuses_by_repository(statuses)
        operations: list[tuple[RepositoryRef, list[FileStatus], list[str]]] = []
        project_name = self.active_project_name
        for repository, repository_statuses in groups:
            scm = self._repository_scm(repository)
            if scm is None:
                self.panel.show_error(
                    f"Repository unavailable: {repository.path}"
                )
                return
            operations.append(
                (
                    repository,
                    repository_statuses,
                    scm.commit_argv(
                        message,
                        sorted(
                            {
                                path
                                for status in repository_statuses
                                for path in status.operation_paths()
                            }
                        ),
                    ),
                )
            )
        self._close_preview()
        self.panel.set_commit_busy(True)

        def run_commit(index: int) -> None:
            """Run the next repository commit after the previous one succeeds."""

            if index >= len(operations):
                self.panel.set_commit_busy(False)
                self.panel.clear_error()
                return
            repository, repository_statuses, argv = operations[index]
            scm = self._repository_scm(repository)
            if scm is None:
                self.panel.set_commit_busy(False)
                self.panel.show_error(
                    f"Repository unavailable: {repository.path}"
                )
                return
            watcher = self._repository_watcher(repository)
            if watcher is not None:
                watcher.mute_metadata_events()

            def committed(result: CommandResult) -> None:
                """Advance only after success and retain all unprocessed targets."""

                if watcher is not None:
                    watcher.request_full()
                if not result.ok:
                    self.panel.set_commit_busy(False)
                    detail = result.stderr.strip() or "Commit failed."
                    self.panel.show_error(f"{repository.path}: {detail}")
                    return
                # 2026-08-19: the successful commit invalidates the displayed
                # comparison and queues a fresh unattended Verify behind any
                # remote check already in progress.
                self.panel.set_remote_status(
                    repository, RepositorySyncStatus()
                )
                verification_key = (project_name, repository)
                if (
                    project_name is not None
                    and verification_key not in self.unattended_verification_queue
                ):
                    self.unattended_verification_queue.append(verification_key)
                    self._start_next_unattended_verification()
                # 2026-08-17: cross-repository commits cannot be atomic; clearing
                # only successful targets makes partial completion explicit.
                self.panel.uncheck_statuses(repository_statuses)
                run_commit(index + 1)

            run_async(argv, committed, cwd=scm.root, env=scm.environment)

        run_commit(0)

    def _preview_file(self, status: FileStatus | None) -> None:
        """Show one asynchronous file preview over project and terminal columns."""

        repository = (
            RepositoryRef(status.repository, status.scm_type) if status else None
        )
        scm = self._repository_scm(repository) if repository else None
        if status is None or scm is None:
            self._close_preview()
            return
        self.preview_status = status
        self.preview.show_status(scm.root, scm, status)
        self._update_preview_geometry()
        # Conditional children use no-show-all so unrelated window refreshes
        # cannot reopen a preview that the user explicitly dismissed.
        self.preview.set_no_show_all(False)
        self.preview.show_all()
        self.preview.set_no_show_all(True)

    def _preview_project_file(self, relative_path: str | None) -> None:
        """Preview a normal file selected from the active project's file tree."""

        project = self.config.find_project(self.active_project_name or "")
        if relative_path is None or project is None:
            self._close_preview()
            return
        self.preview.show_file(project["path"], relative_path)
        self._update_preview_geometry()
        self.preview.set_no_show_all(False)
        self.preview.show_all()
        self.preview.set_no_show_all(True)

    def _refresh_visible_preview(
        self,
        statuses: list[FileStatus],
        repository: RepositoryRef = RepositoryRef(".", "hg"),
    ) -> None:
        """Refresh a preview only from its owning repository snapshot."""

        if not self.preview.get_visible() or not self.preview.current_path:
            return
        if self.right_notebook.get_current_page() == 1:
            return
        current_status = getattr(self, "preview_status", None)
        if (
            current_status is None
            or current_status.repository != repository.path
            or current_status.scm_type != repository.scm_type
        ):
            return
        current = next(
            (
                status
                for status in statuses
                if status.repository == current_status.repository
                and status.scm_type == current_status.scm_type
                and status.path == current_status.path
            ),
            None,
        )
        if current is None:
            self._close_preview()
        else:
            self._preview_file(current)

    def _close_preview(self) -> None:
        """Cancel pending preview work and hide the overlay immediately."""

        if not hasattr(self, "preview"):
            return
        self.preview.cancel()
        self.preview.current_path = None
        self.preview_status = None
        self.preview.hide()

    def _update_preview_geometry(self, *_args: object) -> None:
        """Size the overlay through the second pane while leaving SCM visible."""

        if not hasattr(self, "preview"):
            return
        # inner_paned.position is relative to outer pane's second child; adding
        # the outer divider position yields the right edge of column two.
        width = self.outer_paned.get_position() + self.inner_paned.get_position()
        self.preview.set_size_request(max(320, width), -1)

    def _on_window_button_press(
        self, _window: Gtk.Widget, event: Gdk.EventButton
    ) -> bool:
        """Dismiss an open preview when a click lands outside it or its file tree."""

        if not hasattr(self, "preview") or not self.preview.get_visible():
            return False
        target = Gtk.get_event_widget(event)
        if target is None:
            self._close_preview()
            return False
        if self._is_widget_below(target, self.preview):
            return False
        # File-tree clicks decide whether to replace or close the preview in
        # SCMPanel, avoiding a race with this window-level dismissal handler.
        if self._is_widget_below(target, self.panel.tree) or self._is_widget_below(
            target, self.file_manager.tree
        ):
            return False
        self._close_preview()
        return False

    @staticmethod
    def _is_widget_below(widget: Gtk.Widget, ancestor: Gtk.Widget) -> bool:
        """Return whether a GTK event target belongs to a given widget subtree."""

        current: Gtk.Widget | None = widget
        while current is not None:
            if current is ancestor:
                return True
            current = current.get_parent()
        return False

    def _add_statuses(self, statuses: list[FileStatus]) -> None:
        """Add selected new files through their owning repositories."""

        operations: list[tuple[RepositoryRef, list[str], list[str]]] = []
        for repository, items in SlateWindow._group_statuses_by_repository(statuses):
            paths = sorted({status.path for status in items if status.state == "untracked"})
            scm = self._repository_scm(repository)
            if scm is not None and paths:
                operations.append((repository, scm.add_argv(paths), paths))
        # 2026-08-17: la selezione contestuale o il pulsante sono già una scelta
        # esplicita e l'aggiunta al tracking non elimina contenuti.
        self._run_scm_mutations(operations, "Add")

    def _forget_statuses(self, statuses: list[FileStatus]) -> None:
        """Return explicitly selected added files to their SCM's untracked state."""

        operations: list[tuple[RepositoryRef, list[str], list[str]]] = []
        for repository, items in SlateWindow._group_statuses_by_repository(statuses):
            paths = sorted({status.path for status in items if status.state == "added"})
            scm = self._repository_scm(repository)
            if scm is not None and paths:
                operations.append((repository, scm.forget_argv(paths), paths))
        # 2026-08-17: entrambi gli adapter conservano il contenuto sul disco,
        # quindi non serve una conferma distruttiva superflua.
        self._run_scm_mutations(operations, "Untrack")

    def _revert_statuses(self, statuses: list[FileStatus]) -> None:
        """Confirm irreversible discard before reverting selected tracked files."""

        groups = SlateWindow._group_statuses_by_repository(statuses)
        display_paths = [
            self._workspace_repository_path(repository.path, path)
            for repository, items in groups
            for status in items
            if status.state != "untracked"
            for path in status.operation_paths()
        ]
        if not display_paths:
            return
        if not self._confirm_scm_paths(
            "Revert the selected files?",
            "Local changes will be discarded without creating backup files.",
            "Revert",
            display_paths,
            Gtk.MessageType.WARNING,
        ):
            return
        operations: list[tuple[RepositoryRef, list[str], list[str]]] = []
        for repository, items in groups:
            scm = self._repository_scm(repository)
            paths = sorted(
                {
                    path
                    for status in items
                    if status.state not in {"untracked", "added"}
                    for path in status.operation_paths()
                }
            )
            if scm is not None and paths:
                operations.append((repository, scm.revert_argv(paths), paths))
            added_paths = sorted(
                status.path for status in items if status.state == "added"
            )
            if isinstance(scm, GitSCM) and added_paths:
                # 2026-08-17: HEAD has no version to restore for newly added
                # Git paths; removing only their index entries matches hg revert
                # while preserving the user's working files.
                operations.append(
                    (repository, scm.forget_argv(added_paths), added_paths)
                )
            elif scm is not None and added_paths:
                operations.append(
                    (repository, scm.revert_argv(added_paths), added_paths)
                )
        # 2026-08-17: un ripristino può ricreare file mancanti, quindi si
        # riconcilia il repository completo invece dei soli vecchi path.
        self._run_scm_mutations(
            operations, "Revert", full_refresh=True
        )

    def _project_file_path(
        self, relative_path: str, follow_symlink: bool = True
    ) -> str | None:
        """Resolve a project-relative path while rejecting traversal and unsafe links."""

        project = self.config.find_project(self.active_project_name or "")
        if project is None:
            return None
        if (
            not relative_path
            or os.path.isabs(relative_path)
            or ".." in relative_path.split("/")
        ):
            self._show_file_error(f"Invalid path: {relative_path}")
            return None
        root = os.path.realpath(os.path.abspath(project["path"]))
        candidate = os.path.abspath(os.path.join(root, relative_path))
        try:
            inside_root = os.path.commonpath((root, candidate)) == root
        except ValueError:
            inside_root = False
        if not inside_root:
            self._show_file_error(f"Invalid path: {relative_path}")
            return None
        parent_resolved = os.path.realpath(os.path.dirname(candidate))
        try:
            parent_inside = os.path.commonpath((root, parent_resolved)) == root
        except ValueError:
            parent_inside = False
        if not parent_inside:
            self._show_file_error(
                f"The path traverses an external link: {relative_path}"
            )
            return None
        if follow_symlink and os.path.lexists(candidate):
            resolved = os.path.realpath(candidate)
            try:
                resolved_inside = os.path.commonpath((root, resolved)) == root
            except ValueError:
                resolved_inside = False
            if not resolved_inside:
                self._show_file_error(
                    f"The symbolic link points outside the project: {relative_path}"
                )
                return None
        return candidate

    def _view_status(self, status: FileStatus) -> None:
        """Open a working-copy file through the desktop's default application."""

        self._view_project_file(
            self._workspace_repository_path(status.repository, status.path)
        )

    def _view_project_file(self, relative_path: str) -> None:
        """Open one safe project file through the desktop's default application."""

        path = self._project_file_path(relative_path)
        project = self.config.find_project(self.active_project_name or "")
        if path is None or project is None:
            return
        try:
            # 2026-08-16: xdg-open delegates images, documents and binaries to
            # the user's desktop association without embedding format logic.
            spawn_detached(("xdg-open", path), cwd=project["path"])
        except (GLib.Error, OSError) as error:
            self._show_file_error(
                f"Unable to view {relative_path}: {error}"
            )

    def _edit_status_internal(self, status: FileStatus) -> None:
        """Open a working-copy status file as a project editor row."""

        self._edit_project_file_internal(
            self._workspace_repository_path(status.repository, status.path)
        )

    def _edit_status_external(self, status: FileStatus) -> None:
        """Open a working-copy status file in a new default gVim window."""

        self._edit_project_file_external(
            self._workspace_repository_path(status.repository, status.path)
        )

    def _edit_project_file_internal(self, relative_path: str) -> None:
        """Open one safe active-project file in the persistent internal editor."""

        path = self._project_file_path(relative_path)
        project = self.config.find_project(self.active_project_name or "")
        if path is None or project is None or not os.path.isfile(path):
            return
        self._close_preview()
        order_changed = self._append_project_item(
            project, "editor", relative_path
        )
        self.editor_workspace.open_file(
            project["name"], project["path"], relative_path
        )
        if order_changed:
            self.config.save()
        # 2026-08-16: la riga creata nell'albero, non il contenitore centrale,
        # governa sempre il cambio di vista e il focus del documento aperto.
        self._select_tree_row(project["name"], relative_path, "editor")

    def _edit_project_file_external(self, relative_path: str) -> None:
        """Open one safe project file in a new default gVim window."""

        path = self._project_file_path(relative_path)
        project = self.config.find_project(self.active_project_name or "")
        if path is None or project is None:
            return
        try:
            # 2026-08-16: -f mantiene il processo associato alla nuova finestra
            # senza usare il server gVim di un'altra sessione già aperta.
            spawn_detached(("gvim", "-f", path), cwd=project["path"])
        except (GLib.Error, OSError) as error:
            self._show_file_error(
                f"Unable to open {relative_path} with gVim: {error}"
            )

    def _delete_status(self, status: FileStatus) -> None:
        """Delete one Revision path and record tracked removal in its SCM."""

        relative_path = self._workspace_repository_path(
            status.repository, status.path
        )
        absolute_path = self._project_file_path(
            relative_path, follow_symlink=False
        )
        if absolute_path is None:
            return
        repository = RepositoryRef(status.repository, status.scm_type)
        scm = self._repository_scm(repository)
        if status.state != "untracked" and scm is None:
            self.panel.show_error(
                f"Repository unavailable: {repository.path}"
            )
            return
        exists = os.path.lexists(absolute_path)
        scm_detail = (
            f" and the removal will be recorded in {scm.display_name}"
            if scm is not None and status.state != "untracked"
            else ""
        )
        detail = (
            f"The file will be permanently deleted from disk{scm_detail}."
            if exists
            else f"The file is already missing from disk{scm_detail}."
        )
        if not self._confirm_scm_paths(
            "Delete the selected file?",
            detail,
            "Delete",
            [relative_path],
            Gtk.MessageType.WARNING,
        ):
            return
        # 2026-08-17: il pannello Revisioni conosce repository e stato; dopo la
        # cancellazione deve registrare il path nello SCM, non lasciarlo missing.
        callback = partial(
            self._on_status_entry_deleted, status, relative_path
        )
        if exists:
            self.project_file_operations.delete_entry(absolute_path, callback)
        else:
            callback(None)

    def _on_status_entry_deleted(
        self,
        status: FileStatus,
        relative_path: str,
        error: str | None,
    ) -> None:
        """Record a successful Revision deletion or refresh an untracked path."""

        if error is not None:
            self._show_file_error(
                f"Failed to delete {relative_path}: {error}"
            )
            return
        self.file_manager.refresh()
        if status.state == "untracked":
            self._close_preview()
            self._queue_project_status_paths(
                self.active_project_name or "", (relative_path,)
            )
            return
        repository = RepositoryRef(status.repository, status.scm_type)
        scm = self._repository_scm(repository)
        if scm is None:
            self.panel.show_error(
                f"Repository unavailable: {repository.path}"
            )
            return
        paths = sorted(set(status.operation_paths()))
        self._run_scm_mutations(
            [(repository, scm.record_removal_argv(paths), paths)],
            "Record removal",
        )

    def _new_project_file(self, parent_relative: str) -> None:
        """Prompt for and asynchronously create an empty project file."""

        self._prompt_project_entry(parent_relative, directory=False)

    def _new_project_directory(self, parent_relative: str) -> None:
        """Prompt for and asynchronously create a project directory."""

        self._prompt_project_entry(parent_relative, directory=True)

    def _rename_project_file(self, relative_path: str) -> None:
        """Prompt for a sibling name and asynchronously rename one project entry."""

        source_path = self._project_file_path(relative_path, follow_symlink=False)
        if source_path is None or not os.path.lexists(source_path):
            self._show_file_error(f"The path does not exist: {relative_path}")
            return
        old_name = os.path.basename(relative_path)
        parent_relative = os.path.dirname(relative_path)
        project_name = self.active_project_name or ""
        dialog = Gtk.Dialog(title="Rename", transient_for=self, modal=True)
        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dialog.add_button("Rename", Gtk.ResponseType.OK)
        dialog.set_default_response(Gtk.ResponseType.OK)
        content = dialog.get_content_area()
        content.set_spacing(6)
        content.set_border_width(12)
        label = Gtk.Label(label="New name")
        label.set_xalign(0)
        entry = Gtk.Entry(text=old_name)
        entry.set_activates_default(True)
        entry.select_region(0, -1)
        error_label = Gtk.Label()
        error_label.set_xalign(0)
        error_label.set_line_wrap(True)
        error_label.get_style_context().add_class("error")
        content.pack_start(label, False, False, 0)
        content.pack_start(entry, False, False, 0)
        content.pack_start(error_label, False, False, 0)
        dialog.show_all()
        entry.grab_focus()

        new_relative = None
        destination_path = None
        while dialog.run() == Gtk.ResponseType.OK:
            new_name = entry.get_text().strip()
            if (
                not new_name
                or new_name in {".", ".."}
                or "/" in new_name
                or "\0" in new_name
            ):
                error_label.set_text("Enter a simple, valid name.")
                continue
            if new_name == old_name:
                break
            new_relative = (
                f"{parent_relative}/{new_name}" if parent_relative else new_name
            )
            destination_path = self._project_file_path(
                new_relative, follow_symlink=False
            )
            if destination_path is None:
                new_relative = None
                break
            if os.path.lexists(destination_path):
                error_label.set_text("A file or folder with this name already exists.")
                continue
            break
        dialog.destroy()
        if new_relative is None or destination_path is None:
            return
        # 2026-08-16: il rename resta nella directory corrente, così la nuova
        # scorciatoia non diventa implicitamente anche un'operazione di spostamento.
        self.project_file_operations.rename_entry(
            source_path,
            destination_path,
            partial(
                self._on_project_entry_renamed,
                project_name,
                relative_path,
                new_relative,
            ),
        )

    def _on_project_entry_renamed(
        self,
        project_name: str,
        old_relative: str,
        new_relative: str,
        error: str | None,
    ) -> None:
        """Reconcile editors and filesystem views after an asynchronous rename."""

        if error is not None:
            self._show_file_error(
                f"Failed to rename {old_relative}: {error}"
            )
            return
        project = self.config.find_project(project_name)
        if project is not None:
            old_prefix = f"{old_relative}/"
            preferences = project.setdefault("file_manager", {})
            expanded = preferences.get("expanded_paths", [])
            if isinstance(expanded, list):
                preferences["expanded_paths"] = [
                    f"{new_relative}{path[len(old_relative):]}"
                    if isinstance(path, str)
                    and (path == old_relative or path.startswith(old_prefix))
                    else path
                    for path in expanded
                ]
            # 2026-08-16: rinominare una directory non deve spostare in fondo
            # espansioni o schede, anche se nel frattempo si cambia progetto.
            for item in project.setdefault("item_order", []):
                value = item.get("value", "")
                if item.get("kind") == "editor" and (
                    value == old_relative or value.startswith(old_prefix)
                ):
                    item["value"] = f"{new_relative}{value[len(old_relative):]}"
            self.config.save()
        self.editor_workspace.relocate_path(project_name, old_relative, new_relative)
        self._close_preview()
        if self.file_manager.project_name == project_name:
            self.file_manager.relocate_path(old_relative, new_relative)
            self.file_manager.refresh()
        self._queue_project_status_paths(
            project_name, (old_relative, new_relative)
        )

    def _prompt_project_entry(self, parent_relative: str, directory: bool) -> None:
        """Validate a modal entry name before starting one create operation."""

        title = "New Folder" if directory else "New File"
        dialog = Gtk.Dialog(title=title, transient_for=self, modal=True)
        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dialog.add_button("Create", Gtk.ResponseType.OK)
        dialog.set_default_response(Gtk.ResponseType.OK)
        content = dialog.get_content_area()
        content.set_spacing(6)
        content.set_border_width(12)
        label = Gtk.Label(label="Name")
        label.set_xalign(0)
        entry = Gtk.Entry()
        entry.set_activates_default(True)
        error_label = Gtk.Label()
        error_label.set_xalign(0)
        error_label.set_line_wrap(True)
        error_label.get_style_context().add_class("error")
        content.pack_start(label, False, False, 0)
        content.pack_start(entry, False, False, 0)
        content.pack_start(error_label, False, False, 0)
        dialog.show_all()
        entry.grab_focus()

        relative_path = None
        absolute_path = None
        while dialog.run() == Gtk.ResponseType.OK:
            name = entry.get_text().strip()
            if not name or name in {".", ".."} or "/" in name or "\0" in name:
                error_label.set_text("Enter a simple, valid name.")
                continue
            relative_path = f"{parent_relative}/{name}" if parent_relative else name
            absolute_path = self._project_file_path(
                relative_path, follow_symlink=False
            )
            if absolute_path is None:
                relative_path = None
                break
            if os.path.lexists(absolute_path):
                error_label.set_text("A file or folder with this name already exists.")
                continue
            break
        else:
            relative_path = None
        dialog.destroy()
        if relative_path is None or absolute_path is None:
            return
        callback = partial(self._on_project_entry_created, relative_path)
        if directory:
            self.project_file_operations.create_directory(absolute_path, callback)
        else:
            self.project_file_operations.create_file(absolute_path, callback)

    def _on_project_entry_created(
        self, relative_path: str, error: str | None
    ) -> None:
        """Report creation errors or refresh views without changing their cursor."""

        if error is not None:
            self._show_file_error(f"Failed to create {relative_path}: {error}")
            return
        self._refresh_project_file_views((relative_path,))

    def _delete_project_file(self, relative_path: str) -> None:
        """Request safe deletion of a file-manager file, link or directory."""

        self._delete_project_path(relative_path)

    def _delete_project_path(self, relative_path: str) -> None:
        """Validate and dispatch confirmed deletion for one project entry."""

        path = self._project_file_path(relative_path, follow_symlink=False)
        if path is None:
            return
        if os.path.isdir(path) and not os.path.islink(path):
            self.project_file_operations.inspect_directory(
                path,
                partial(self._on_project_directory_inspected, relative_path, path),
            )
            return
        if not self._confirm_scm_paths(
            "Delete the selected file?",
            "The file will be permanently deleted from disk.",
            "Delete",
            [relative_path],
            Gtk.MessageType.WARNING,
        ):
            return
        self.project_file_operations.delete_entry(
            path,
            partial(self._on_project_entry_deleted, relative_path),
        )

    def _on_project_directory_inspected(
        self,
        relative_path: str,
        absolute_path: str,
        inspection: DirectoryInspection | None,
        error: str | None,
    ) -> None:
        """Reject nested directories or ask the appropriate deletion confirmation."""

        if error is not None or inspection is None:
            self._show_file_error(
                f"Failed to inspect {relative_path}: {error or 'unknown error'}"
            )
            return
        if inspection.contains_directory:
            self._show_file_error(
                "The folder contains other folders and cannot be deleted."
            )
            return
        if inspection.empty:
            title = "Delete the folder?"
            detail = "The empty folder will be permanently deleted."
        else:
            title = "Destroy the non-empty folder?"
            detail = (
                "All contained files and links will be permanently deleted. "
                "The folder contains no subfolders."
            )
        if not self._confirm_scm_paths(
            title,
            detail,
            "Delete",
            [relative_path],
            Gtk.MessageType.WARNING,
        ):
            return
        # 2026-08-16: l'helper ripete l'ispezione dopo la conferma e non scende
        # mai in sottodirectory, anche se il contenuto cambia durante il dialogo.
        self.project_file_operations.delete_flat_directory(
            absolute_path,
            partial(self._on_project_entry_deleted, relative_path),
        )

    def _on_project_entry_deleted(
        self, relative_path: str, error: str | None
    ) -> None:
        """Report deletion failure or reconcile both file and SCM views."""

        if error is not None:
            self._show_file_error(f"Failed to delete {relative_path}: {error}")
            return
        self._close_preview()
        self._refresh_project_file_views((relative_path,))

    def _refresh_project_file_views(self, paths: Sequence[str]) -> None:
        """Refresh file-manager and SCM state after a filesystem mutation."""

        self.file_manager.refresh()
        self._queue_project_status_paths(self.active_project_name or "", paths)

    def _show_file_error(self, message: str) -> None:
        """Show path-operation errors in whichever third-column page is visible."""

        if self.right_notebook.get_current_page() == 1:
            self.file_manager.show_error(message)
        else:
            self.panel.show_error(message)

    def _confirm_scm_paths(
        self,
        title: str,
        detail: str,
        action_label: str,
        paths: Sequence[str],
        message_type: Gtk.MessageType,
    ) -> bool:
        """Require an explicit user decision before any working-copy mutation."""

        dialog = Gtk.MessageDialog(
            transient_for=self,
            modal=True,
            message_type=message_type,
            buttons=Gtk.ButtonsType.NONE,
            text=title,
        )
        dialog.format_secondary_text(f"{detail}\n\n" + "\n".join(paths))
        cancel = dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dialog.add_button(action_label, Gtk.ResponseType.OK)
        dialog.set_default_response(Gtk.ResponseType.CANCEL)
        dialog.set_focus(cancel)
        response = dialog.run()
        dialog.destroy()
        return response == Gtk.ResponseType.OK

    def _run_scm_mutations(
        self,
        operations: Sequence[tuple[RepositoryRef, list[str], Sequence[str]]],
        action_name: str,
        *,
        full_refresh: bool = False,
    ) -> None:
        """Run repository mutations and refresh either affected paths or all state."""

        if not operations:
            return
        self._close_preview()

        def run_operation(index: int) -> None:
            """Start the indexed mutation only after its predecessor succeeds."""

            if index >= len(operations):
                self.panel.clear_error()
                return
            repository, argv, paths = operations[index]
            scm = self._repository_scm(repository)
            if scm is None:
                self.panel.show_error(
                    f"Repository unavailable: {repository.path}"
                )
                return
            watcher = self._repository_watcher(repository)
            if watcher is not None:
                watcher.mute_metadata_events()

            def completed(result: CommandResult) -> None:
                """Refresh the affected watcher and continue only after success."""

                if watcher is not None:
                    if full_refresh:
                        watcher.request_full()
                    else:
                        watcher.request_paths(paths)
                if not result.ok:
                    detail = result.stderr.strip() or f"{action_name} failed."
                    self.panel.show_error(f"{repository.path}: {detail}")
                    return
                run_operation(index + 1)

            run_async(list(argv), completed, cwd=scm.root, env=scm.environment)

        run_operation(0)

    def _open_diff(
        self, repository: RepositoryRef, paths: Sequence[str]
    ) -> None:
        """Launch Meld using the repository selected in the Revision tree."""

        scm = self._repository_scm(repository)
        if not scm:
            return
        try:
            spawn_detached(scm.diff_argv(paths), cwd=scm.root, env=scm.environment)
        except GLib.Error as error:
            self.panel.show_error(f"Failed to start Meld: {error}")

    def _open_external(self, repository: RepositoryRef) -> None:
        """Launch TortoiseHg using the contextual repository root."""

        scm = self._repository_scm(repository)
        if not isinstance(scm, MercurialSCM):
            return
        try:
            spawn_detached(scm.external_tool_argv(), cwd=scm.root, env=scm.environment)
        except GLib.Error as error:
            self.panel.show_error(f"Failed to start TortoiseHg: {error}")

    def _update_repository(self, repository: RepositoryRef) -> None:
        """Open the dedicated update workflow for the contextual repository."""

        if self.repository_dialog is not None:
            self.repository_dialog.present()
            return
        self._stop_unattended_verification()
        scm = self._repository_scm(repository)
        watcher = self._repository_watcher(repository)
        if scm is None or watcher is None:
            self.panel.show_error(f"Repository unavailable: {repository.path}")
            return
        self.panel.set_remote_status(repository, RepositorySyncStatus())
        # 2026-08-17: the modal owns one explicit network/update transaction so
        # the main window never mixes it with another repository action.
        self.repository_dialog = RepositoryUpdateDialog(
            self,
            scm,
            watcher,
            self._on_repository_dialog_closed,
        )
        self.repository_dialog.show_all()
        self.repository_dialog.start()

    def _open_repository_action(
        self, action: str, repository: RepositoryRef
    ) -> None:
        """Open one dedicated simple repository-action dialog."""

        if self.repository_dialog is not None:
            self.repository_dialog.present()
            return
        self._stop_unattended_verification()
        scm = self._repository_scm(repository)
        watcher = self._repository_watcher(repository)
        if scm is None or watcher is None:
            self.panel.show_error(f"Repository unavailable: {repository.path}")
            return
        # 2026-08-18: explicit dispatch keeps each modal independent without
        # introducing a workflow controller or a configurable command engine.
        dialog_types = {
            "verify": RepositoryVerifyDialog,
            "publish": RepositoryPublishDialog,
            "new_branch": RepositoryCreateBranchDialog,
            "switch_branch": RepositorySwitchBranchDialog,
            "merge_branch": RepositoryMergeBranchDialog,
            "tag": RepositoryTagDialog,
        }
        dialog_type = dialog_types.get(action)
        if dialog_type is None:
            self.panel.show_error(f"Unsupported repository action: {action}")
            return
        if dialog_type is RepositoryVerifyDialog:
            self.repository_dialog = RepositoryVerifyDialog(
                self,
                scm,
                watcher,
                self._on_repository_dialog_closed,
                partial(self._on_repository_verified, repository),
            )
        else:
            # 2026-08-19: branch and history actions invalidate a previous
            # comparison locally; they never trigger an implicit remote check.
            self.panel.set_remote_status(repository, RepositorySyncStatus())
            self.repository_dialog = dialog_type(
                self,
                scm,
                watcher,
                self._on_repository_dialog_closed,
            )
        self.repository_dialog.show_all()
        self.repository_dialog.start()

    def _on_repository_verified(
        self,
        repository: RepositoryRef,
        status: RepositorySyncStatus,
    ) -> None:
        """Publish an explicit remote comparison on the active repository row."""

        self.panel.set_remote_status(repository, status)

    def _on_repository_dialog_closed(self) -> None:
        """Forget the completed modal so another repository action may open."""

        self.repository_dialog = None

    def _on_delete_event(self, _window: Gtk.Window, _event: Gdk.Event) -> bool:
        """Intercept clean window closure to inspect tmux foreground processes."""

        self._request_close()
        return True

    def _on_window_activity_changed(
        self, window: Gtk.Window, _property: GObject.ParamSpec
    ) -> None:
        """Enable debounced process monitoring only for the foreground window."""

        self.terminals.set_activity_monitoring(window.is_active())

    def _request_close(self) -> None:
        """Protect dirty editors before beginning the terminal exit decision."""

        if self.closing:
            return
        self.closing = True
        self.editor_workspace.request_close_all(self._after_editors_close)

    def _after_editors_close(self, proceed: bool) -> None:
        """Query terminal activity only after editor closure is approved."""

        if proceed:
            self.terminals.set_activity_monitoring(False)
            self.terminals.query_panes(self._decide_close)
        else:
            self.closing = False

    def _decide_close(self, panes: list[PaneInfo]) -> None:
        """Skip confirmation at shell prompts or present the required three choices."""

        active = [pane for pane in panes if pane.active]
        if not active:
            self._terminate_all(panes)
            return
        lines = [f"{len(panes)} open sessions:"]
        for pane in panes:
            if pane.active:
                duration = self._format_duration(pane.duration)
                lines.append(f"⚠ {pane.session} ({pane.command} — running {duration})")
            else:
                lines.append(f"✓ {pane.session} (shell at prompt)")
        dialog = Gtk.MessageDialog(
            transient_for=self,
            modal=True,
            message_type=Gtk.MessageType.WARNING,
            buttons=Gtk.ButtonsType.NONE,
            text="Close SLATE?",
        )
        dialog.format_secondary_text("\n".join(lines))
        cancel_button = dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dialog.add_button("Leave in Background", 1)
        dialog.add_button("Terminate All", 2)
        dialog.set_default_response(Gtk.ResponseType.CANCEL)
        dialog.set_focus(cancel_button)
        response = dialog.run()
        dialog.destroy()
        if response == 1:
            self._save_window_state()
            self._final_destroy()
        elif response == 2:
            self._terminate_all(panes)
        else:
            self.closing = False
            self.terminals.set_activity_monitoring(self.is_active())

    def _terminate_all(self, panes: list[PaneInfo]) -> None:
        """Kill sessions, then poll briefly before cleaning server remnants."""

        # 2026-08-16: i child-exited provocati dalla chiusura dell'app non sono
        # terminali rimossi dall'utente e non devono cancellare la configurazione.
        self.terminals.begin_shutdown()
        self.set_sensitive(False)
        self._save_window_state()
        configured_sessions = {
            session_name(project["name"], terminal_name_value)
            for project in self.config.data["projects"]
            for terminal_name_value in project["terminals"]
        }
        sessions = {pane.session for pane in panes} | configured_sessions
        if not sessions:
            self.terminals.kill_server(self._after_server_kill)
            return
        pending = set(sessions)

        def make_killed(name: str):
            """Create a completion handler for one shutdown session."""

            def killed(_result: CommandResult) -> None:
                """Start disappearance polling after every kill has returned."""

                pending.discard(name)
                if not pending:
                    self._terminate_started_at = GLib.get_monotonic_time() // 1000
                    GLib.timeout_add(200, self._poll_termination)

            return killed

        for name in sessions:
            self.terminals.kill_session(name, make_killed(name))

    def _poll_termination(self) -> bool:
        """Poll pane disappearance until empty or the two-second deadline."""

        self.terminals.query_panes(
            self._on_termination_poll,
            include_duration=False,
        )
        return GLib.SOURCE_REMOVE

    def _on_termination_poll(self, panes: list[PaneInfo]) -> None:
        """Continue bounded polling or kill the dedicated server remnants."""

        elapsed = GLib.get_monotonic_time() // 1000 - self._terminate_started_at
        if panes and elapsed < 2000:
            GLib.timeout_add(200, self._poll_termination)
        else:
            self.terminals.kill_server(self._after_server_kill)

    def _after_server_kill(self, _result: CommandResult) -> None:
        """Finish application shutdown even when tmux was already absent."""

        self._final_destroy()

    def _save_window_state(self) -> None:
        """Persist geometry and paned positions immediately before clean exit."""

        width, height = self.get_size()
        maximized = bool(self.get_window() and self.get_window().get_state() & Gdk.WindowState.MAXIMIZED)
        self.config.data["window"] = {
            "width": width,
            "height": height,
            "maximized": maximized,
        }
        self.config.data["pane_positions"] = [
            self.outer_paned.get_position(),
            self.inner_paned.get_position(),
        ]
        self.config.save()

    def _final_destroy(self) -> None:
        """Release monitors and timers, then destroy the sole window."""

        self._close_preview()
        self._stop_unattended_verification()
        if self.browser_bell_timeout_id is not None:
            GLib.source_remove(self.browser_bell_timeout_id)
            self.browser_bell_timeout_id = None
        self.browser_bell_project_name = None
        self.browser_manager.shutdown()
        self.editor_workspace.shutdown()
        for discovery in self.discovery_by_project.values():
            discovery.cancel()
        for watcher in self.watchers.values():
            watcher.close()
        self.terminals.shutdown()
        self.destroy()

    def _selected_project(self) -> dict | None:
        """Return the project represented by the current tree selection."""

        model, tree_iter = self.project_tree.get_selection().get_selected()
        if tree_iter is None:
            return self.config.find_project(self.active_project_name or "")
        return self.config.find_project(model.get_value(tree_iter, self.COL_PROJECT))

    def _selected_terminal(self) -> tuple[dict, str] | None:
        """Return selected project and terminal only for a terminal row."""

        model, tree_iter = self.project_tree.get_selection().get_selected()
        if tree_iter is None or model.get_value(tree_iter, self.COL_KIND) != "terminal":
            return None
        project = self.config.find_project(model.get_value(tree_iter, self.COL_PROJECT))
        return (
            (project, model.get_value(tree_iter, self.COL_ITEM))
            if project
            else None
        )

    def _repository_scm(self, repository: RepositoryRef) -> SCM | None:
        """Return the typed adapter identified inside the active project."""

        return self.scm_by_repository.get(
            (self.active_project_name or "", repository)
        )

    def _repository_watcher(self, repository: RepositoryRef) -> RepoWatcher | None:
        """Return the typed watcher identified inside the active project."""

        return self.watchers.get((self.active_project_name or "", repository))

    def _refresh_project_watchers(self, project_name: str) -> None:
        """Queue a full status after repository ownership boundaries change."""

        for (name, _repository), watcher in self.watchers.items():
            if name == project_name:
                watcher.request_full()

    def _queue_project_status_paths(
        self, project_name: str, workspace_paths: Sequence[str]
    ) -> None:
        """Route workspace-relative paths to their most specific repository."""

        repositories = self.repositories_by_project.get(project_name, set())
        for workspace_path in workspace_paths:
            owners = [
                repository
                for repository in repositories
                if repository.path == "."
                or workspace_path == repository.path
                or workspace_path.startswith(f"{repository.path}/")
            ]
            if not owners:
                continue
            owner = max(owners, key=SlateWindow._repository_path_length)
            relative = (
                workspace_path
                if owner.path == "."
                else workspace_path[len(owner.path):].strip("/")
            )
            watcher = self.watchers.get((project_name, owner))
            if watcher is not None:
                watcher.request_paths(
                    (relative or ".",),
                )

    @staticmethod
    def _group_statuses_by_repository(
        statuses: Sequence[FileStatus],
    ) -> list[tuple[RepositoryRef, list[FileStatus]]]:
        """Group file targets by repository while retaining visible order."""

        grouped: dict[RepositoryRef, list[FileStatus]] = {}
        for status in statuses:
            repository = RepositoryRef(status.repository, status.scm_type)
            grouped.setdefault(repository, []).append(status)
        return list(grouped.items())

    @staticmethod
    def _repository_path_length(repository: RepositoryRef) -> int:
        """Rank repository ownership candidates by path specificity."""

        return len(repository.path)

    def _update_activity(self, activity: dict[str, bool]) -> None:
        """Update only terminal activity cells whose foreground state changed."""

        parent = self.project_store.get_iter_first()
        while parent:
            child = self.project_store.iter_children(parent)
            while child:
                if self.project_store.get_value(child, self.COL_KIND) != "terminal":
                    child = self.project_store.iter_next(child)
                    continue
                project_name = self.project_store.get_value(child, self.COL_PROJECT)
                terminal_name_value = self.project_store.get_value(child, self.COL_ITEM)
                active = activity.get(session_name(project_name, terminal_name_value), False)
                if self.project_store.get_value(child, self.COL_ACTIVITY) != active:
                    self.project_store.set_value(child, self.COL_ACTIVITY, active)
                    terminal_name_value = self.project_store.get_value(
                        child, self.COL_ITEM
                    )
                    tooltip = (
                        f"{terminal_name_value} — process running"
                        if active
                        else terminal_name_value
                    )
                    self.project_store.set_value(
                        child,
                        self.COL_TOOLTIP,
                        GLib.markup_escape_text(tooltip),
                    )
                child = self.project_store.iter_next(child)
            parent = self.project_store.iter_next(parent)

    def _validate_project_name(self, name: str) -> str | None:
        """Reject ambiguous names and tmux slug collisions before config writes."""

        if not name or any(character in name for character in "/|\n\r"):
            return "The project name is empty or contains reserved characters."
        candidate = slug(name, 30)
        if not candidate:
            return "The project name must contain at least one ASCII letter or digit."
        for project in self.config.data["projects"]:
            if project["name"] == name or slug(project["name"], 30) == candidate:
                return "The project name or tmux slug is already in use."
        return None

    def _validate_terminal_name(
        self, project: dict | None, name: str, old_name: str = ""
    ) -> str | None:
        """Reject terminal names that cannot map uniquely to a tmux session."""

        if project is None:
            return "Project not found."
        if not name or any(character in name for character in "/|\n\r"):
            return "The terminal name is empty or contains reserved characters."
        candidate = slug(name, 20)
        if not candidate:
            return "The terminal name must contain at least one ASCII letter or digit."
        for existing in project["terminals"]:
            if existing != old_name and (
                existing == name or slug(existing, 20) == candidate
            ):
                return "The terminal name or tmux slug is already in use in this project."
        return None

    def _show_error(self, message: str) -> bool:
        """Display operational errors in the persistent third-column area."""

        self.panel.show_error(message)
        return GLib.SOURCE_REMOVE

    def _info(self, title: str, message: str) -> None:
        """Show a short informational modal for an explicit user request."""

        dialog = Gtk.MessageDialog(
            transient_for=self,
            modal=True,
            message_type=Gtk.MessageType.INFO,
            buttons=Gtk.ButtonsType.OK,
            text=title,
        )
        dialog.format_secondary_text(message)
        dialog.run()
        dialog.destroy()

    @staticmethod
    def _format_duration(seconds: int | None) -> str:
        """Format process elapsed seconds for the shutdown warning."""

        if seconds is None:
            return "for an unknown duration"
        if seconds < 60:
            return f"for {seconds}s"
        return f"for {seconds // 60}m"
