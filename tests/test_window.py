"""Focused window-action tests without constructing another SLATE GUI."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

import gi

gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, GdkPixbuf, Gtk

from slate.processes import CommandResult
from slate.scm.base import FileStatus, RepositoryRef
from slate.scm.git import GitSCM
from slate.window import SlateWindow, _moved_sequence


class WindowActionTest(unittest.TestCase):
    """Verify high-level SCM actions independently from GTK window startup."""

    def test_browser_title_is_escaped_for_the_markup_tooltip_column(self) -> None:
        """The complete title remains safe when the sidebar text is ellipsized."""

        tree_iter = object()
        project = {"browsers": []}
        config = SimpleNamespace(
            find_project=MagicMock(return_value=project),
            save=MagicMock(),
        )
        owner = SimpleNamespace(
            COL_TEXT=SlateWindow.COL_TEXT,
            COL_TOOLTIP=SlateWindow.COL_TOOLTIP,
            project_store=MagicMock(),
            browser_manager=SimpleNamespace(
                serialized_project=MagicMock(return_value=[]),
            ),
            config=config,
            _find_sidebar_iter=MagicMock(return_value=tree_iter),
        )
        page = SimpleNamespace(
            project_name="repo",
            identifier="browser-1",
            entry=SimpleNamespace(display_title="Articoli & Pagine"),
            uri="https://example.test/post.php?post=10087&action=edit",
        )

        SlateWindow._on_browser_state_changed(owner, page)

        owner.project_store.set.assert_called_once_with(
            tree_iter,
            SlateWindow.COL_TEXT,
            "Articoli & Pagine",
            SlateWindow.COL_TOOLTIP,
            "Articoli &amp; Pagine",
        )
        config.save.assert_not_called()

    def test_sequence_move_rejects_invalid_and_unchanged_positions(self) -> None:
        """The shared reorder primitive returns only effective stable moves."""

        self.assertEqual(
            _moved_sequence(["first", "second", "third"], "third", "first", True),
            ["third", "first", "second"],
        )
        self.assertIsNone(
            _moved_sequence(["first", "second"], "first", "second", True)
        )
        self.assertIsNone(
            _moved_sequence(["first", "second"], "missing", "second", False)
        )

    def test_project_drop_reorders_config_and_tree_without_losing_children(self) -> None:
        """Moving one project updates both orders in place and retains its subtree."""

        projects = [
            {"name": "first"},
            {"name": "second"},
            {"name": "third"},
        ]
        config = SimpleNamespace(data={"projects": projects}, save=MagicMock())
        store = Gtk.TreeStore(str, str, str, str, bool, str, bool)
        first = store.append(
            None, ["first", "project", "first", "", False, "first", False]
        )
        second = store.append(
            None, ["second", "project", "second", "", False, "second", False]
        )
        store.append(
            second, ["main", "terminal", "second", "main", False, "main", False]
        )
        store.append(
            None, ["third", "project", "third", "", False, "third", False]
        )
        tree = Gtk.TreeView(model=store)
        tree.get_selection().select_iter(first)
        owner = SimpleNamespace(
            config=config,
            project_store=store,
            COL_KIND=SlateWindow.COL_KIND,
            COL_PROJECT=SlateWindow.COL_PROJECT,
            COL_ITEM=SlateWindow.COL_ITEM,
        )
        owner._sidebar_drop_order = SlateWindow._sidebar_drop_order.__get__(owner)
        owner._find_sidebar_iter = SlateWindow._find_sidebar_iter.__get__(owner)
        self.assertTrue(
            SlateWindow._apply_sidebar_drop(
                owner,
                ("project", "third", ""),
                ("project", "first", ""),
                True,
            )
        )
        self.assertEqual(
            [project["name"] for project in config.data["projects"]],
            ["third", "first", "second"],
        )
        roots = []
        tree_iter = store.get_iter_first()
        while tree_iter:
            roots.append(store.get_value(tree_iter, SlateWindow.COL_PROJECT))
            tree_iter = store.iter_next(tree_iter)
        self.assertEqual(roots, ["third", "first", "second"])
        moved_second = owner._find_sidebar_iter(("project", "second", ""))
        self.assertIsNotNone(store.iter_children(moved_second))
        selected_model, selected = tree.get_selection().get_selected()
        self.assertEqual(selected_model.get_value(selected, SlateWindow.COL_PROJECT), "first")
        config.save.assert_called_once_with()

    def test_child_drop_reorders_mixed_items_only_inside_its_project(self) -> None:
        """Terminal and editor rows share item_order but cannot cross projects."""

        project = {
            "name": "repo",
            "terminals": ["main", "logs"],
            "last_terminal": "main",
            "item_order": [
                {"kind": "terminal", "value": "main"},
                {"kind": "editor", "value": "README.md"},
                {"kind": "terminal", "value": "logs"},
            ],
        }
        other = {
            "name": "other",
            "item_order": [{"kind": "terminal", "value": "main"}],
        }

        def find_project(name: str) -> dict:
            """Return the fixture project selected by its stable name."""

            return project if name == "repo" else other

        config = SimpleNamespace(
            data={"projects": [project, other]},
            find_project=MagicMock(side_effect=find_project),
            save=MagicMock(),
        )
        store = Gtk.TreeStore(str, str, str, str, bool, str, bool)
        parent = store.append(
            None, ["repo", "project", "repo", "", False, "repo", False]
        )
        for text, kind, value in (
            ("main", "terminal", "main"),
            ("README.md", "editor", "README.md"),
            ("logs", "terminal", "logs"),
        ):
            store.append(
                parent, [text, kind, "repo", value, False, value, False]
            )
        other_parent = store.append(
            None, ["other", "project", "other", "", False, "other", False]
        )
        store.append(
            other_parent,
            ["main", "terminal", "other", "main", False, "main", False],
        )
        owner = SimpleNamespace(
            config=config,
            project_store=store,
            COL_KIND=SlateWindow.COL_KIND,
            COL_PROJECT=SlateWindow.COL_PROJECT,
            COL_ITEM=SlateWindow.COL_ITEM,
        )
        owner._sidebar_drop_order = SlateWindow._sidebar_drop_order.__get__(owner)
        owner._find_sidebar_iter = SlateWindow._find_sidebar_iter.__get__(owner)
        self.assertTrue(
            SlateWindow._apply_sidebar_drop(
                owner,
                ("terminal", "repo", "logs"),
                ("terminal", "repo", "main"),
                True,
            )
        )
        self.assertEqual(
            project["item_order"],
            [
                {"kind": "terminal", "value": "logs"},
                {"kind": "terminal", "value": "main"},
                {"kind": "editor", "value": "README.md"},
            ],
        )
        self.assertEqual(project["terminals"], ["main", "logs"])
        self.assertEqual(project["last_terminal"], "main")
        self.assertFalse(
            SlateWindow._apply_sidebar_drop(
                owner,
                ("terminal", "repo", "main"),
                ("terminal", "other", "main"),
                False,
            )
        )
        config.save.assert_called_once_with()

    def test_ctrl_press_prepares_drag_without_selecting_or_activating_row(self) -> None:
        """Ctrl+left captures a stable source while leaving selection untouched."""

        store = Gtk.TreeStore(str, str, str, str, bool, str, bool)
        tree_iter = store.append(
            None, ["repo", "project", "repo", "", False, "repo", False]
        )
        path = store.get_path(tree_iter)
        normal_column = object()
        tree = MagicMock()
        tree.get_path_at_pos.return_value = (path, normal_column, 0, 0)
        owner = SimpleNamespace(
            project_drag_candidate=None,
            project_store=store,
            project_expander_column=object(),
            COL_KIND=SlateWindow.COL_KIND,
            COL_PROJECT=SlateWindow.COL_PROJECT,
            COL_ITEM=SlateWindow.COL_ITEM,
        )
        event = SimpleNamespace(
            button=1,
            state=Gdk.ModifierType.CONTROL_MASK,
            type=Gdk.EventType.BUTTON_PRESS,
            x=24,
            y=12,
        )
        self.assertTrue(SlateWindow._on_tree_button(owner, tree, event))
        self.assertEqual(owner.project_drag_candidate[0], ("project", "repo", ""))
        tree.get_selection().select_path.assert_not_called()

    def test_ctrl_drag_uses_gtk_copy_protocol_for_a_real_reorder_target(self) -> None:
        """GTK's Ctrl action remains acceptable while SLATE shows the drop marker."""

        path = Gtk.TreePath.new_from_indices([1])
        tree = MagicMock()
        context = MagicMock()
        context.get_suggested_action.return_value = Gdk.DragAction.COPY
        owner = SimpleNamespace(
            _sidebar_drop_at=MagicMock(
                return_value=(
                    ("project", "second", ""),
                    True,
                    path,
                    Gtk.TreeViewDropPosition.BEFORE,
                )
            ),
            _clear_tree_drag_destination=MagicMock(),
        )
        with patch("slate.window.Gdk.drag_status") as drag_status:
            self.assertTrue(
                SlateWindow._on_tree_drag_motion(owner, tree, context, 10, 20, 30)
            )
        tree.set_drag_dest_row.assert_called_once_with(
            path, Gtk.TreeViewDropPosition.BEFORE
        )
        drag_status.assert_called_once_with(context, Gdk.DragAction.COPY, 30)

    def test_destination_change_clears_commit_message(self) -> None:
        """Changing terminal or project discards text tied to the old context."""

        panel = MagicMock()
        config = SimpleNamespace(data={"active_terminal": None}, save=MagicMock())
        owner = SimpleNamespace(
            active_project_name="first",
            active_terminal_name="main",
            active_editor_ref=None,
            panel=panel,
            config=config,
            terminals=MagicMock(),
            headerbar=MagicMock(),
            terminal_stack=MagicMock(),
            editor_workspace=MagicMock(),
            add_terminal_button=MagicMock(),
            add_command_button=MagicMock(),
            resume_codex_button=MagicMock(),
            right_notebook=MagicMock(),
            file_manager=MagicMock(),
            restoring_selection=False,
            watchers={},
            snapshots={},
            discovery_by_project={},
            _close_preview=MagicMock(),
            _configure_file_manager=MagicMock(),
            _set_revision_count=MagicMock(),
            _ensure_project_repositories=MagicMock(return_value=[]),
            _update_active_revision_count=MagicMock(),
        )
        first = {
            "name": "first",
            "path": "/tmp/first",
            "terminals": ["main", "test"],
            "terminal_commands": {"test": "codex resume"},
            "last_terminal": "main",
        }
        second = {
            "name": "second",
            "path": "/tmp/second",
            "terminals": ["main"],
            "last_terminal": "main",
        }

        # 2026-08-16: la stessa destinazione conserva il testo, mentre ciascun
        # cambio effettivo lo azzera una sola volta.
        SlateWindow._activate(owner, first, "main")
        panel.clear_message.assert_not_called()
        SlateWindow._activate(owner, first, "test")
        panel.clear_message.assert_called_once_with()
        SlateWindow._activate(owner, second, "main")
        self.assertEqual(panel.clear_message.call_count, 2)
        self.assertEqual(
            owner.terminals.add.call_args_list[1],
            call(first, "test", initial_command="codex resume"),
        )
        owner._configure_file_manager.assert_not_called()

    def test_file_tab_recovers_the_active_project_binding(self) -> None:
        """Opening File repairs a missing project binding before loading its tree."""

        project = {"name": "project", "path": "/tmp/project"}
        owner = SimpleNamespace(
            active_project_name="project",
            config=SimpleNamespace(find_project=MagicMock(return_value=project)),
            file_manager=MagicMock(),
            _close_preview=MagicMock(),
            _configure_file_manager=MagicMock(),
        )
        SlateWindow._on_right_page_changed(owner, MagicMock(), MagicMock(), 1)
        owner._configure_file_manager.assert_called_once_with(project)
        owner.file_manager.set_active.assert_called_once_with(True)

    def test_nested_repositories_do_not_disable_workspace_file_monitors(self) -> None:
        """Only a root SCM watcher can cover files outside nested repositories."""

        project = {
            "name": "workspace",
            "path": "/tmp/workspace",
            "file_manager": {
                "show_hidden": False,
                "show_excluded": False,
                "expanded_paths": [],
            },
        }
        owner = SimpleNamespace(
            file_manager=SimpleNamespace(project_name=None, root=None),
            repositories_by_project={
                "workspace": {RepositoryRef("nested/repo", "hg")}
            },
            _project_ignored_paths=MagicMock(return_value=set()),
        )
        owner.file_manager.set_project = MagicMock()
        with patch("slate.window.Path.resolve", return_value=Path("/tmp/workspace")):
            SlateWindow._configure_file_manager(owner, project)
        owner.file_manager.set_project.assert_called_once_with(
            "workspace",
            "/tmp/workspace",
            project["file_manager"],
            set(),
            False,
        )

    def test_revision_count_is_rendered_on_the_native_tab_label(self) -> None:
        """The File page can still expose the active project's visible changes."""

        owner = SimpleNamespace(changes_tab=Gtk.Label(label="Changes"))
        SlateWindow._set_revision_count(owner, 7)
        self.assertEqual(owner.changes_tab.get_text(), "Changes (7)")
        SlateWindow._set_revision_count(owner, 0)
        self.assertEqual(owner.changes_tab.get_text(), "Changes")

    def test_revision_tab_count_is_mirrored_on_active_project_row(self) -> None:
        """The sidebar reuses the received tab count without changing identity."""

        store = Gtk.TreeStore(str, str, str, str, bool, str, bool)
        project_iter = store.append(
            None, ["repo", "project", "repo", "", False, "repo", False]
        )
        owner = SimpleNamespace(
            changes_tab=Gtk.Label(label="Revisioni"),
            active_project_name="repo",
            revision_counts={},
            project_store=store,
            COL_TEXT=SlateWindow.COL_TEXT,
            COL_PROJECT=SlateWindow.COL_PROJECT,
        )
        owner._find_sidebar_iter = SlateWindow._find_sidebar_iter.__get__(owner)

        SlateWindow._set_revision_count(owner, 7)

        self.assertEqual(store.get_value(project_iter, SlateWindow.COL_TEXT), "repo (7)")
        self.assertEqual(store.get_value(project_iter, SlateWindow.COL_PROJECT), "repo")
        self.assertEqual(owner.revision_counts, {"repo": 7})
        SlateWindow._set_revision_count(owner, 0)
        self.assertEqual(store.get_value(project_iter, SlateWindow.COL_TEXT), "repo")

    def test_repository_watcher_is_created_once_per_discovered_root(self) -> None:
        """Repeated discovery reuses the repository adapter and watcher."""

        project = {"name": "repo", "path": "/tmp/repo"}
        owner = SimpleNamespace(
            scm_by_repository={},
            watchers={},
            snapshots={},
            repositories_by_project={},
            ignored_by_repository={},
            active_project_name="repo",
            panel=MagicMock(),
            file_manager=MagicMock(),
            _repository_root=MagicMock(return_value="/tmp/repo"),
            _update_repository_boundaries=MagicMock(),
        )
        repository = RepositoryRef(".", "hg")
        with patch("slate.window.RepoWatcher") as watcher_class:
            SlateWindow._attach_repository(owner, project, repository)
            SlateWindow._attach_repository(owner, project, repository)
        watcher_class.assert_called_once()

    def test_git_repository_uses_typed_adapter_and_shared_watcher(self) -> None:
        """A discovered Git root plugs into the same lazy watcher architecture."""

        project = {"name": "repo", "path": "/tmp/repo"}
        owner = SimpleNamespace(
            scm_by_repository={},
            watchers={},
            snapshots={},
            repositories_by_project={},
            ignored_by_repository={},
            active_project_name="repo",
            panel=MagicMock(),
            file_manager=MagicMock(),
            _repository_root=MagicMock(return_value="/tmp/repo"),
            _update_repository_boundaries=MagicMock(),
        )
        repository = RepositoryRef(".", "git")
        with patch("slate.window.RepoWatcher") as watcher_class:
            SlateWindow._attach_repository(owner, project, repository)
        self.assertIsInstance(owner.scm_by_repository[("repo", repository)], GitSCM)
        watcher_class.assert_called_once()

    def test_automatic_repository_scan_runs_only_on_first_project_use(self) -> None:
        """Switching back to a loaded project never repeats its filesystem scan."""

        project = {
            "name": "repo",
            "path": "/tmp/repo",
            "repositories": {"known": [], "excluded": []},
        }
        owner = SimpleNamespace(
            repositories_by_project={},
            discovery_by_project={},
            scanned_projects=set(),
            config=SimpleNamespace(save=MagicMock()),
            _scan_project_repositories=MagicMock(),
        )
        SlateWindow._ensure_project_repositories(owner, project)
        owner.scanned_projects.add("repo")
        SlateWindow._ensure_project_repositories(owner, project)
        owner._scan_project_repositories.assert_called_once_with(project)

    def test_project_population_does_not_materialize_lazy_terminals(self) -> None:
        """Sidebar reconstruction creates configured rows without VTE/tmux clients."""

        project = {
            "name": "repo",
            "terminals": ["main"],
            "item_order": [{"kind": "terminal", "value": "main"}],
        }
        terminals = MagicMock()
        terminals.terminals = {}
        owner = SimpleNamespace(
            restoring_tree=False,
            revision_counts={"repo": 3},
            project_store=Gtk.TreeStore(str, str, str, str, bool, str, bool),
            project_tree=MagicMock(),
            config=SimpleNamespace(
                data={"projects": [project], "expanded_projects": []}
            ),
            terminals=terminals,
            attention_terminals=set(),
            editor_workspace=SimpleNamespace(editors={}),
            _ordered_project_items=SlateWindow._ordered_project_items.__get__(
                SimpleNamespace(editor_workspace=SimpleNamespace(editors={}))
            ),
        )
        SlateWindow._populate_projects(owner)
        terminals.add.assert_not_called()
        self.assertEqual(owner.project_store.iter_n_children(None), 1)
        project_iter = owner.project_store.get_iter_first()
        self.assertEqual(
            owner.project_store.get_value(project_iter, SlateWindow.COL_TEXT),
            "repo (3)",
        )

    def test_private_browser_rows_enter_persistent_item_order(self) -> None:
        """Anonymous browser identities retain their project position on restart."""

        project = {
            "name": "repo",
            "terminals": ["main"],
            "item_order": [{"kind": "terminal", "value": "main"}],
        }
        browser = SimpleNamespace(
            project_name="repo", identifier="browser-1", private=True
        )
        owner = SimpleNamespace(
            editor_workspace=SimpleNamespace(editors={}),
            browser_manager=SimpleNamespace(pages={browser.identifier: browser}),
        )
        ordered = SlateWindow._ordered_project_items(owner, project)
        self.assertEqual(
            ordered,
            [("terminal", "main"), ("browser", "browser-1")],
        )
        self.assertEqual(
            project["item_order"],
            [
                {"kind": "terminal", "value": "main"},
                {"kind": "browser", "value": "browser-1"},
            ],
        )

    def test_normal_browser_rows_share_persistent_item_order(self) -> None:
        """Normal browser identities participate in mixed project ordering."""

        project = {
            "name": "repo",
            "terminals": ["main"],
            "item_order": [
                {"kind": "terminal", "value": "main"},
                {"kind": "browser", "value": "browser-2"},
            ],
        }
        browser = SimpleNamespace(
            project_name="repo", identifier="browser-2", private=False
        )
        owner = SimpleNamespace(
            editor_workspace=SimpleNamespace(editors={}),
            browser_manager=SimpleNamespace(
                pages={("repo", browser.identifier): browser}
            ),
        )
        ordered = SlateWindow._ordered_project_items(owner, project)
        self.assertEqual(
            ordered,
            [("terminal", "main"), ("browser", "browser-2")],
        )

    def test_inactive_startup_keeps_workspace_blank_and_controls_disabled(self) -> None:
        """Startup does not select a project or initialize repository state."""

        selection = MagicMock()
        owner = SimpleNamespace(
            active_project_name="old",
            active_terminal_name="main",
            active_editor_ref=("old", "file.py"),
            project_tree=MagicMock(),
            inactive_terminal=MagicMock(),
            terminal_stack=MagicMock(),
            editor_workspace=MagicMock(),
            add_terminal_button=MagicMock(),
            add_command_button=MagicMock(),
            resume_codex_button=MagicMock(),
            right_notebook=MagicMock(),
            file_manager=MagicMock(),
        )
        owner.project_tree.get_selection.return_value = selection
        SlateWindow._show_inactive_workspace(owner)
        self.assertIsNone(owner.active_project_name)
        self.assertIsNone(owner.active_terminal_name)
        self.assertIsNone(owner.active_editor_ref)
        selection.unselect_all.assert_called_once_with()
        owner.terminal_stack.set_visible_child_name.assert_called_once_with(
            "__inactive__"
        )
        owner.editor_workspace.show_inactive.assert_called_once_with()
        owner.right_notebook.set_sensitive.assert_called_once_with(False)
        owner.file_manager.clear_project.assert_called_once_with()

    def test_tree_rebuild_selection_does_not_activate_projects(self) -> None:
        """Transient GTK selection changes cannot defeat repository lazy loading."""

        selection = MagicMock()
        owner = SimpleNamespace(
            restoring_tree=True,
            startup_inactive=False,
            config=MagicMock(),
            _activate=MagicMock(),
            _activate_editor=MagicMock(),
        )
        SlateWindow._on_tree_selection(owner, selection)
        selection.get_selected.assert_not_called()
        owner._activate.assert_not_called()
        owner._activate_editor.assert_not_called()

    def test_post_map_selection_remains_blocked_until_idle_cleanup(self) -> None:
        """GTK mapping cannot activate projects before the neutral idle state."""

        selection = MagicMock()
        owner = SimpleNamespace(
            restoring_tree=False,
            startup_inactive=True,
            config=MagicMock(),
            _activate=MagicMock(),
            _activate_editor=MagicMock(),
            _show_inactive_workspace=MagicMock(),
            terminals=MagicMock(),
            is_active=MagicMock(return_value=True),
        )
        SlateWindow._on_tree_selection(owner, selection)
        selection.get_selected.assert_not_called()
        self.assertFalse(SlateWindow._finish_inactive_startup(owner))
        owner._show_inactive_workspace.assert_called_once_with()
        self.assertFalse(owner.startup_inactive)
        owner.terminals.set_activity_monitoring.assert_called_once_with(True)

    def test_orphan_scan_uses_all_configured_lazy_terminal_names(self) -> None:
        """Unloaded configured sessions cannot be misreported as tmux orphans."""

        owner = SimpleNamespace(
            config=SimpleNamespace(
                data={
                    "projects": [
                        {"name": "Repo", "terminals": ["main", "test"]},
                        {"name": "Altro", "terminals": ["shell"]},
                    ]
                }
            ),
            terminals=MagicMock(),
            _show_orphans_dialog=MagicMock(),
        )
        SlateWindow._on_orphans(owner, MagicMock())
        owner.terminals.list_orphans.assert_called_once_with(
            {"repo--main", "repo--test", "altro--shell"},
            owner._show_orphans_dialog,
        )

    def test_ancestor_status_excludes_paths_owned_by_nested_repository(self) -> None:
        """The root watcher cannot publish duplicate rows from a child repository."""

        owner = SimpleNamespace(
            repositories_by_project={
                "workspace": {
                    RepositoryRef(".", "hg"),
                    RepositoryRef("nested/repo", "hg"),
                }
            }
        )
        root_repository = RepositoryRef(".", "hg")
        nested_repository = RepositoryRef("nested/repo", "hg")
        self.assertTrue(
            SlateWindow._status_owned_by_repository(
                owner, "workspace", root_repository, "root-file.txt"
            )
        )
        self.assertFalse(
            SlateWindow._status_owned_by_repository(
                owner, "workspace", root_repository, "nested/repo/file.txt"
            )
        )
        self.assertTrue(
            SlateWindow._status_owned_by_repository(
                owner, "workspace", nested_repository, "file.txt"
            )
        )

    def test_open_terminal_here_creates_a_session_at_the_safe_directory(self) -> None:
        """Directory action starts a new terminal without typing into a busy one."""

        owner = SimpleNamespace(
            _project_file_path=MagicMock(return_value="/tmp/project/src"),
            _show_file_error=MagicMock(),
            _create_terminal=MagicMock(),
        )
        with patch("slate.window.os.path.isdir", return_value=True):
            SlateWindow._open_terminal_in_project_directory(owner, "src")
        owner._create_terminal.assert_called_once_with(None, "/tmp/project/src")

    def test_font_setting_applies_to_only_its_section_and_persists(self) -> None:
        """A settings change updates one view and the single config file."""

        config = SimpleNamespace(
            data={
                "settings": {
                    "revisions": {"font_size": 10},
                    "files": {"font_size": 10},
                    "editor": {"font_size": 10},
                    "terminal": {"status_bar": False},
                }
            },
            save=MagicMock(),
        )
        owner = SimpleNamespace(
            config=config,
            panel=MagicMock(),
            file_manager=MagicMock(),
            editor_workspace=MagicMock(),
        )
        SlateWindow._update_font_setting(owner, "files", 14)
        self.assertEqual(config.data["settings"]["files"]["font_size"], 14)
        owner.file_manager.set_font_size.assert_called_once_with(14)
        owner.panel.set_font_size.assert_not_called()
        config.save.assert_called_once_with()

    def test_tmux_status_setting_applies_and_persists(self) -> None:
        """The terminal switch updates the dedicated server and global config."""

        config = SimpleNamespace(
            data={"settings": {"terminal": {"status_bar": False}}},
            save=MagicMock(),
        )
        owner = SimpleNamespace(config=config, terminals=MagicMock())
        SlateWindow._update_tmux_status_setting(owner, True)
        self.assertTrue(config.data["settings"]["terminal"]["status_bar"])
        owner.terminals.set_status_bar_enabled.assert_called_once_with(True)
        config.save.assert_called_once_with()

    def test_editor_state_persists_only_when_it_changes(self) -> None:
        """Editor-row references update config without redundant writes."""

        state = {
            "tabs": [{"project": "repo", "path": "TODO.md"}],
            "active_tab": {"project": "repo", "path": "TODO.md"},
        }
        config = SimpleNamespace(data={"editor": state.copy()}, save=MagicMock())
        owner = SimpleNamespace(config=config)
        SlateWindow._persist_editor_state(
            owner, list(state["tabs"]), dict(state["active_tab"])
        )
        config.save.assert_not_called()
        SlateWindow._persist_editor_state(owner, list(state["tabs"]), None)
        self.assertIsNone(config.data["editor"]["active_tab"])
        config.save.assert_called_once_with()

    def test_window_close_resolves_dirty_editors_before_querying_tmux(self) -> None:
        """The terminal shutdown flow cannot begin before editor approval."""

        owner = SimpleNamespace(
            closing=False,
            editor_workspace=MagicMock(),
            terminals=MagicMock(),
            _after_editors_close=MagicMock(),
            _decide_close=MagicMock(),
        )
        SlateWindow._request_close(owner)
        self.assertTrue(owner.closing)
        owner.editor_workspace.request_close_all.assert_called_once_with(
            owner._after_editors_close
        )
        owner.terminals.query_panes.assert_not_called()

        owner._after_editors_close = SlateWindow._after_editors_close.__get__(owner)
        owner._after_editors_close(True)
        owner.terminals.set_activity_monitoring.assert_called_once_with(False)
        owner.terminals.query_panes.assert_called_once_with(owner._decide_close)

    def test_commit_starts_immediately_after_panel_validation(self) -> None:
        """Commit does not insert a second confirmation after message and checkbox."""

        scm = SimpleNamespace(
            root="/tmp/repo",
            environment={"HGPLAIN": "1"},
            commit_argv=MagicMock(return_value=["hg", "commit", "a.py"]),
        )
        panel = MagicMock()
        watcher = MagicMock()
        owner = SimpleNamespace(
            panel=panel,
            active_project_name="repo",
            _repository_scm=MagicMock(return_value=scm),
            _repository_watcher=MagicMock(return_value=watcher),
            _close_preview=MagicMock(),
        )
        status = FileStatus("a.py", "modified")
        with patch("slate.window.run_async") as run:
            SlateWindow._commit(owner, "messaggio", [status])
        scm.commit_argv.assert_called_once_with("messaggio", ["a.py"])
        owner._close_preview.assert_called_once_with()
        panel.set_commit_busy.assert_called_once_with(True)
        run.assert_called_once()

        # 2026-08-16: un esito positivo non deve consumare il testo, perché il
        # messaggio può servire per commit successivi correlati.
        completed = run.call_args.args[1]
        completed(CommandResult(("hg", "commit", "a.py"), 0, "", ""))
        watcher.request_full.assert_called_once_with()
        watcher.request_paths.assert_not_called()
        panel.clear_message.assert_not_called()
        panel.clear_error.assert_called_once_with()
        self.assertEqual(
            panel.set_commit_busy.call_args_list,
            [call(True), call(False)],
        )

    def test_multi_repository_commit_stops_and_retains_unprocessed_targets(self) -> None:
        """A failed repository prevents later commits and clears only prior success."""

        scms = {
            RepositoryRef(repository, "hg"): SimpleNamespace(
                root=f"/tmp/{repository}",
                environment={"HGPLAIN": "1"},
                commit_argv=MagicMock(
                    return_value=["hg", "commit", repository]
                ),
            )
            for repository in ("one", "two", "three")
        }
        panel = MagicMock()
        owner = SimpleNamespace(
            panel=panel,
            active_project_name="workspace",
            _repository_scm=MagicMock(side_effect=scms.get),
            _repository_watcher=MagicMock(return_value=None),
            _close_preview=MagicMock(),
        )
        statuses = [
            FileStatus("a", "modified", repository="one"),
            FileStatus("b", "modified", repository="two"),
            FileStatus("c", "modified", repository="three"),
        ]
        with patch("slate.window.run_async") as run:
            SlateWindow._commit(owner, "messaggio", statuses)
            first_callback = run.call_args_list[0].args[1]
            first_callback(CommandResult(("hg",), 0, "", ""))
            second_callback = run.call_args_list[1].args[1]
            second_callback(CommandResult(("hg",), 1, "", "errore"))
        self.assertEqual(run.call_count, 2)
        panel.uncheck_statuses.assert_called_once_with([statuses[0]])
        panel.show_error.assert_called_once_with("two: errore")
        self.assertEqual(panel.set_commit_busy.call_args_list, [call(True), call(False)])

    def test_commit_expands_one_moved_row_to_both_mercurial_paths(self) -> None:
        """A move commit includes its removal and destination atomically."""

        scm = SimpleNamespace(
            root="/tmp/repo",
            environment={"HGPLAIN": "1"},
            commit_argv=MagicMock(return_value=["hg", "commit"]),
        )
        owner = SimpleNamespace(
            panel=MagicMock(),
            active_project_name="repo",
            _repository_scm=MagicMock(return_value=scm),
            _repository_watcher=MagicMock(return_value=None),
            _close_preview=MagicMock(),
        )
        moved = FileStatus("new.py", "moved", source_path="old.py")
        with patch("slate.window.run_async"):
            SlateWindow._commit(owner, "sposta", [moved])
        scm.commit_argv.assert_called_once_with("sposta", ["new.py", "old.py"])

    def test_project_path_rejects_traversal_and_external_symlink_parents(self) -> None:
        """Generic file actions cannot escape through relative paths or symlinks."""

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "project"
            outside = base / "outside"
            root.mkdir()
            outside.mkdir()
            (outside / "secret.txt").write_text("secret", encoding="utf-8")
            (root / "external").symlink_to(outside, target_is_directory=True)
            (root / "link.txt").symlink_to(outside / "secret.txt")
            owner = SimpleNamespace(
                active_project_name="project",
                config=SimpleNamespace(
                    find_project=MagicMock(
                        return_value={"name": "project", "path": str(root)}
                    )
                ),
                _show_file_error=MagicMock(),
            )
            self.assertIsNone(
                SlateWindow._project_file_path(owner, "../outside/secret.txt")
            )
            self.assertIsNone(
                SlateWindow._project_file_path(owner, "external/secret.txt", False)
            )
            self.assertIsNone(SlateWindow._project_file_path(owner, "link.txt"))
            self.assertEqual(
                SlateWindow._project_file_path(owner, "link.txt", False),
                str(root / "link.txt"),
            )

    def test_revision_delete_records_tracked_removal_after_filesystem_success(self) -> None:
        """A tracked file deleted by Revision is immediately recorded in its SCM."""

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tracked.py"
            path.write_text("modified", encoding="utf-8")
            scm = SimpleNamespace(
                display_name="Mercurial",
                record_removal_argv=MagicMock(
                    return_value=["hg", "remove", "--after", "tracked.py"]
                ),
            )
            owner = SimpleNamespace(
                active_project_name="project",
                panel=MagicMock(),
                file_manager=MagicMock(),
                project_file_operations=MagicMock(),
                _workspace_repository_path=MagicMock(return_value="tracked.py"),
                _project_file_path=MagicMock(return_value=str(path)),
                _repository_scm=MagicMock(return_value=scm),
                _confirm_scm_paths=MagicMock(return_value=True),
                _show_file_error=MagicMock(),
                _close_preview=MagicMock(),
                _queue_project_status_paths=MagicMock(),
                _run_scm_mutations=MagicMock(),
            )
            owner._on_status_entry_deleted = (
                SlateWindow._on_status_entry_deleted.__get__(owner)
            )
            status = FileStatus("tracked.py", "modified")
            SlateWindow._delete_status(owner, status)
            callback = owner.project_file_operations.delete_entry.call_args.args[1]
            callback(None)
            scm.record_removal_argv.assert_called_once_with(["tracked.py"])
            owner._run_scm_mutations.assert_called_once_with(
                [
                    (
                        RepositoryRef(".", "hg"),
                        ["hg", "remove", "--after", "tracked.py"],
                        ["tracked.py"],
                    )
                ],
                "Record removal",
            )
            owner.file_manager.refresh.assert_called_once_with()

    def test_revision_delete_repairs_an_already_missing_tracked_path(self) -> None:
        """A missing row skips disk deletion and still reaches remove-after."""

        scm = SimpleNamespace(
            display_name="Mercurial",
            record_removal_argv=MagicMock(return_value=["hg", "remove", "--after"]),
        )
        owner = SimpleNamespace(
            active_project_name="project",
            panel=MagicMock(),
            file_manager=MagicMock(),
            project_file_operations=MagicMock(),
            _workspace_repository_path=MagicMock(return_value="missing.py"),
            _project_file_path=MagicMock(return_value="/missing/project/missing.py"),
            _repository_scm=MagicMock(return_value=scm),
            _confirm_scm_paths=MagicMock(return_value=True),
            _show_file_error=MagicMock(),
            _close_preview=MagicMock(),
            _queue_project_status_paths=MagicMock(),
            _run_scm_mutations=MagicMock(),
        )
        owner._on_status_entry_deleted = (
            SlateWindow._on_status_entry_deleted.__get__(owner)
        )
        SlateWindow._delete_status(
            owner, FileStatus("missing.py", "removed")
        )
        owner.project_file_operations.delete_entry.assert_not_called()
        scm.record_removal_argv.assert_called_once_with(["missing.py"])
        owner._run_scm_mutations.assert_called_once()

    def test_add_starts_immediately_for_selected_new_files(self) -> None:
        """Contextual and keyboard additions do not insert a confirmation dialog."""

        scm = SimpleNamespace(
            add_argv=MagicMock(return_value=["hg", "add", "first", "second"]),
        )
        owner = SimpleNamespace(
            _repository_scm=MagicMock(return_value=scm),
            _run_scm_mutations=MagicMock(),
        )
        statuses = [
            FileStatus("second", "untracked"),
            FileStatus("tracked", "modified"),
            FileStatus("first", "untracked"),
        ]
        SlateWindow._add_statuses(owner, statuses)
        scm.add_argv.assert_called_once_with(["first", "second"])
        owner._run_scm_mutations.assert_called_once_with(
            [
                (
                    RepositoryRef(".", "hg"),
                    ["hg", "add", "first", "second"],
                    ["first", "second"],
                )
            ],
            "Add",
        )

    def test_forget_returns_only_added_files_to_new(self) -> None:
        """Contextual forget runs directly and never includes other states."""

        scm = SimpleNamespace(
            forget_argv=MagicMock(return_value=["hg", "forget", "added"]),
        )
        owner = SimpleNamespace(
            _repository_scm=MagicMock(return_value=scm),
            _run_scm_mutations=MagicMock(),
        )
        statuses = [
            FileStatus("modified", "modified"),
            FileStatus("added", "added"),
        ]
        SlateWindow._forget_statuses(owner, statuses)
        scm.forget_argv.assert_called_once_with(["added"])
        owner._run_scm_mutations.assert_called_once_with(
            [
                (
                    RepositoryRef(".", "hg"),
                    ["hg", "forget", "added"],
                    ["added"],
                )
            ],
            "Untrack",
        )

    def test_same_repository_path_keeps_git_and_hg_actions_separate(self) -> None:
        """Typed grouping cannot send a Git path to the HG adapter or vice versa."""

        groups = SlateWindow._group_statuses_by_repository(
            (
                FileStatus("hg.py", "modified"),
                FileStatus("git.py", "modified", scm_type="git"),
            )
        )
        self.assertEqual(
            groups,
            [
                (RepositoryRef(".", "hg"), [FileStatus("hg.py", "modified")]),
                (
                    RepositoryRef(".", "git"),
                    [FileStatus("git.py", "modified", scm_type="git")],
                ),
            ],
        )

    def test_git_revert_returns_added_path_to_new_without_deleting_it(self) -> None:
        """Git added paths use index removal because HEAD cannot restore them."""

        scm = GitSCM("/tmp/repo")
        owner = SimpleNamespace(
            _repository_scm=MagicMock(return_value=scm),
            _confirm_scm_paths=MagicMock(return_value=True),
            _workspace_repository_path=MagicMock(return_value="added.py"),
            _run_scm_mutations=MagicMock(),
        )
        SlateWindow._revert_statuses(
            owner, [FileStatus("added.py", "added", scm_type="git")]
        )
        owner._run_scm_mutations.assert_called_once_with(
            [
                (
                    RepositoryRef(".", "git"),
                    ["git", "rm", "--cached", "-f", "--", "added.py"],
                    ["added.py"],
                )
            ],
            "Revert",
            full_refresh=True,
        )

    def test_revert_requests_full_repository_refresh(self) -> None:
        """A confirmed revert reconciles the complete repository afterward."""

        scm = SimpleNamespace(
            revert_argv=MagicMock(return_value=["hg", "revert", "modified"]),
        )
        owner = SimpleNamespace(
            _repository_scm=MagicMock(return_value=scm),
            _confirm_scm_paths=MagicMock(return_value=True),
            _workspace_repository_path=MagicMock(return_value="modified"),
            _run_scm_mutations=MagicMock(),
        )
        statuses = [FileStatus("modified", "modified")]
        SlateWindow._revert_statuses(owner, statuses)
        owner._run_scm_mutations.assert_called_once_with(
            [
                (
                    RepositoryRef(".", "hg"),
                    ["hg", "revert", "modified"],
                    ["modified"],
                )
            ],
            "Revert",
            full_refresh=True,
        )

    def test_revert_expands_one_moved_row_to_both_mercurial_paths(self) -> None:
        """Confirmed move rollback restores both ends with one full refresh."""

        scm = SimpleNamespace(
            revert_argv=MagicMock(return_value=["hg", "revert", "new.py", "old.py"]),
        )
        owner = SimpleNamespace(
            _repository_scm=MagicMock(return_value=scm),
            _confirm_scm_paths=MagicMock(return_value=True),
            _workspace_repository_path=MagicMock(
                side_effect=lambda _repository, path: path
            ),
            _run_scm_mutations=MagicMock(),
        )
        moved = FileStatus("new.py", "moved", source_path="old.py")
        SlateWindow._revert_statuses(owner, [moved])
        owner._confirm_scm_paths.assert_called_once()
        self.assertEqual(
            owner._confirm_scm_paths.call_args.args[3], ["old.py", "new.py"]
        )
        scm.revert_argv.assert_called_once_with(["new.py", "old.py"])
        owner._run_scm_mutations.assert_called_once_with(
            [
                (
                    RepositoryRef(".", "hg"),
                    ["hg", "revert", "new.py", "old.py"],
                    ["new.py", "old.py"],
                )
            ],
            "Revert",
            full_refresh=True,
        )

    def test_resume_codex_reuses_terminal_creation_flow(self) -> None:
        """The dedicated action persists a terminal before sending codex resume."""

        project = {
            "name": "repo",
            "path": "/tmp/repo",
            "terminals": ["main"],
            "terminal_commands": {},
            "last_terminal": "main",
        }
        config = SimpleNamespace(
            data={"active_terminal": "repo/main"},
            save=MagicMock(),
        )
        owner = SimpleNamespace(
            config=config,
            terminals=MagicMock(),
            _selected_project=MagicMock(return_value=project),
            _append_project_item=SlateWindow._append_project_item,
            _populate_projects=MagicMock(),
            _select_tree_row=MagicMock(),
            _prompt_terminal_rename=MagicMock(),
        )
        with patch("slate.window.GLib.idle_add") as idle_add:
            SlateWindow._create_terminal(
                owner, "codex resume", name_prefix="codex"
            )
            SlateWindow._create_terminal(
                owner, "codex resume", name_prefix="codex"
            )
        self.assertEqual(project["terminals"], ["main", "codex-1", "codex-2"])
        self.assertEqual(
            project["terminal_commands"],
            {"codex-1": "codex resume", "codex-2": "codex resume"},
        )
        self.assertEqual(project["last_terminal"], "codex-2")
        owner.terminals.add.assert_has_calls(
            [
                call(project, "codex-1", initial_command="codex resume"),
                call(project, "codex-2", initial_command="codex resume"),
            ]
        )
        idle_add.assert_not_called()
        self.assertEqual(config.save.call_count, 2)

    def test_custom_command_uses_executable_name_and_preserves_quoting(self) -> None:
        """The command dialog derives ssh-N without rewriting the entered shell line."""

        command = "/usr/bin/ssh -l root vps1.helecomedia.com -t 'tmux a -d || bash -i'"
        dialog = MagicMock()
        dialog.run.return_value = Gtk.ResponseType.OK
        entry = MagicMock()
        entry.get_text.return_value = command
        owner = SimpleNamespace(_create_terminal=MagicMock())
        with patch("slate.window.Gtk.Dialog", return_value=dialog), patch(
            "slate.window.Gtk.Entry", return_value=entry
        ), patch("slate.window.Gtk.Label", side_effect=(MagicMock(), MagicMock())):
            SlateWindow._on_add_command(owner, MagicMock())

        owner._create_terminal.assert_called_once_with(command, name_prefix="ssh")
        dialog.destroy.assert_called_once_with()

    def test_cancelled_custom_command_does_not_create_terminal(self) -> None:
        """Cancelling the command dialog leaves project configuration untouched."""

        dialog = MagicMock()
        dialog.run.return_value = Gtk.ResponseType.CANCEL
        owner = SimpleNamespace(_create_terminal=MagicMock())
        with patch("slate.window.Gtk.Dialog", return_value=dialog), patch(
            "slate.window.Gtk.Entry", return_value=MagicMock()
        ), patch("slate.window.Gtk.Label", side_effect=(MagicMock(), MagicMock())):
            SlateWindow._on_add_command(owner, MagicMock())

        owner._create_terminal.assert_not_called()

    def test_invalid_custom_command_quoting_stays_in_dialog(self) -> None:
        """Unbalanced shell quoting reports inline and creates no terminal."""

        dialog = MagicMock()
        dialog.run.side_effect = (Gtk.ResponseType.OK, Gtk.ResponseType.CANCEL)
        entry = MagicMock()
        entry.get_text.return_value = "ssh 'unfinished"
        error_label = MagicMock()
        owner = SimpleNamespace(_create_terminal=MagicMock())
        with patch("slate.window.Gtk.Dialog", return_value=dialog), patch(
            "slate.window.Gtk.Entry", return_value=entry
        ), patch(
            "slate.window.Gtk.Label", side_effect=(MagicMock(), error_label)
        ):
            SlateWindow._on_add_command(owner, MagicMock())

        error_label.set_text.assert_called_once_with(
            "The command quoting is invalid."
        )
        owner._create_terminal.assert_not_called()

    def test_long_command_prefix_keeps_unique_numeric_suffixes(self) -> None:
        """Long executable names retain distinct suffixes within tmux's limit."""

        project = {
            "name": "repo",
            "path": "/tmp/repo",
            "terminals": ["main"],
            "terminal_commands": {},
            "last_terminal": "main",
        }
        owner = SimpleNamespace(
            config=SimpleNamespace(
                data={"active_terminal": "repo/main"}, save=MagicMock()
            ),
            terminals=MagicMock(),
            _selected_project=MagicMock(return_value=project),
            _append_project_item=SlateWindow._append_project_item,
            _populate_projects=MagicMock(),
            _select_tree_row=MagicMock(),
        )

        SlateWindow._create_terminal(
            owner, "very-long-executable-name --serve", name_prefix="very-long-executable-name"
        )
        SlateWindow._create_terminal(
            owner, "very-long-executable-name --test", name_prefix="very-long-executable-name"
        )

        generated = project["terminals"][-2:]
        self.assertEqual([name[-2:] for name in generated], ["-1", "-2"])
        self.assertTrue(all(len(name) <= 20 for name in generated))
        self.assertEqual(len(set(generated)), 2)

    def test_plain_terminal_creation_keeps_automatic_name(self) -> None:
        """A manually created terminal does not open an unsolicited dialog."""

        project = {
            "name": "repo",
            "path": "/tmp/repo",
            "terminals": ["main"],
            "last_terminal": "main",
        }
        owner = SimpleNamespace(
            config=SimpleNamespace(
                data={"active_terminal": "repo/main"},
                save=MagicMock(),
            ),
            terminals=MagicMock(),
            _selected_project=MagicMock(return_value=project),
            _append_project_item=SlateWindow._append_project_item,
            _populate_projects=MagicMock(),
            _select_tree_row=MagicMock(),
            _prompt_terminal_rename=MagicMock(),
        )
        with patch("slate.window.GLib.idle_add") as idle_add:
            SlateWindow._create_terminal(owner, None)
        idle_add.assert_not_called()
        owner._prompt_terminal_rename.assert_not_called()

    def test_row_bells_track_each_ringing_terminal_independently(self) -> None:
        """Focusing one terminal preserves the bell raised by another terminal."""

        first = object()
        second = object()
        store = Gtk.TreeStore(str, str, str, str, bool, str, bool)
        parent = store.append(
            None, ["repo", "project", "repo", "", False, "repo", False]
        )
        first_iter = store.append(
            parent, ["main", "terminal", "repo", "main", True, "main", False]
        )
        second_iter = store.append(
            parent, ["test", "terminal", "repo", "test", True, "test", False]
        )
        owner = SimpleNamespace(
            attention_terminals=set(),
            project_store=store,
            terminals=SimpleNamespace(
                terminals={"repo/main": first, "repo/test": second}
            ),
            COL_KIND=SlateWindow.COL_KIND,
            COL_PROJECT=SlateWindow.COL_PROJECT,
            COL_ITEM=SlateWindow.COL_ITEM,
            COL_ATTENTION=SlateWindow.COL_ATTENTION,
        )
        SlateWindow._update_terminal_attention(owner, first, True)
        SlateWindow._update_terminal_attention(owner, second, True)
        SlateWindow._update_terminal_attention(owner, first, False)
        self.assertEqual(owner.attention_terminals, {second})
        self.assertFalse(
            store.get_value(first_iter, SlateWindow.COL_ATTENTION)
        )
        self.assertTrue(
            store.get_value(second_iter, SlateWindow.COL_ATTENTION)
        )

    def test_browser_bell_debounce_keeps_only_the_latest_project(self) -> None:
        """Rapid terminal bells share one timer and retain the last project."""

        owner = SimpleNamespace(
            browser_bell_project_name="old",
            browser_bell_timeout_id=12,
            BROWSER_BELL_DEBOUNCE_MS=SlateWindow.BROWSER_BELL_DEBOUNCE_MS,
            _finish_browser_bell_reload=MagicMock(),
        )
        with patch("slate.window.GLib.source_remove") as source_remove, patch(
            "slate.window.GLib.timeout_add", return_value=34
        ) as timeout_add:
            SlateWindow._queue_browser_bell_reload(owner, "repo", "main")
        source_remove.assert_called_once_with(12)
        timeout_add.assert_called_once_with(
            SlateWindow.BROWSER_BELL_DEBOUNCE_MS,
            owner._finish_browser_bell_reload,
        )
        self.assertEqual(owner.browser_bell_project_name, "repo")
        self.assertEqual(owner.browser_bell_timeout_id, 34)

    def test_browser_bell_selects_hard_reloads_and_presents_target(self) -> None:
        """A configured BELL target replaces the workspace and activates SLATE."""

        page = SimpleNamespace(reload_bypass_cache=MagicMock())
        target = SimpleNamespace(
            project_name="repo", identifier="browser-2", page=page
        )
        owner = SimpleNamespace(
            browser_bell_timeout_id=99,
            browser_bell_project_name="repo",
            browser_manager=SimpleNamespace(
                reload_on_bell_target=MagicMock(return_value=target)
            ),
            _select_tree_row=MagicMock(return_value=True),
            present=MagicMock(),
        )
        self.assertFalse(SlateWindow._finish_browser_bell_reload(owner))
        owner.browser_manager.reload_on_bell_target.assert_called_once_with(
            "repo"
        )
        owner._select_tree_row.assert_called_once_with(
            "repo", "browser-2", "browser"
        )
        page.reload_bypass_cache.assert_called_once_with()
        owner.present.assert_called_once_with()
        self.assertIsNone(owner.browser_bell_timeout_id)
        self.assertIsNone(owner.browser_bell_project_name)

    def test_project_row_is_iconless_gray_and_children_keep_icons(self) -> None:
        """Sidebar renderers distinguish project headers without extra indentation."""

        store = Gtk.TreeStore(str, str, str, str, bool, str, bool)
        project_iter = store.append(
            None, ["repo", "project", "repo", "", False, "repo", False]
        )
        terminal_iter = store.append(
            project_iter,
            ["main", "terminal", "repo", "main", False, "main", False],
        )
        private_browser_iter = store.append(
            project_iter,
            ["Private", "browser", "repo", "browser-1", False, "Private", False],
        )
        background = Gdk.RGBA()
        background.parse("#e8e8e8")
        incognito_icon = GdkPixbuf.Pixbuf.new_from_file_at_scale(
            str(Path(__file__).parents[1] / "slate" / "incognito.svg"),
            16,
            16,
            True,
        )
        owner = SimpleNamespace(
            COL_TEXT=SlateWindow.COL_TEXT,
            COL_KIND=SlateWindow.COL_KIND,
            COL_PROJECT=SlateWindow.COL_PROJECT,
            COL_ITEM=SlateWindow.COL_ITEM,
            project_row_background=background,
            browser_manager=SimpleNamespace(
                pages={("repo", "browser-1"): SimpleNamespace(private=True)}
            ),
            incognito_icon=incognito_icon,
        )
        owner._set_project_row_background = (
            SlateWindow._set_project_row_background.__get__(owner)
        )
        renderer = Gtk.CellRendererPixbuf()
        SlateWindow._render_tree_icon(owner, MagicMock(), renderer, store, project_iter)
        self.assertIsNone(renderer.get_property("icon-name"))
        self.assertFalse(renderer.get_property("visible"))
        self.assertTrue(renderer.get_property("cell-background-set"))
        SlateWindow._render_tree_icon(owner, MagicMock(), renderer, store, terminal_iter)
        self.assertEqual(renderer.get_property("icon-name"), "utilities-terminal")
        self.assertTrue(renderer.get_property("visible"))
        self.assertFalse(renderer.get_property("cell-background-set"))
        SlateWindow._render_tree_icon(
            owner, MagicMock(), renderer, store, private_browser_iter
        )
        self.assertIsNone(renderer.get_property("icon-name"))
        self.assertEqual(renderer.get_property("pixbuf"), incognito_icon)

    def test_project_expander_cell_toggles_at_button_icon_size(self) -> None:
        """The shaded custom indicator remains readable and owns project toggling."""

        store = Gtk.TreeStore(str, str, str, str, bool, str, bool)
        project_iter = store.append(
            None, ["repo", "project", "repo", "", False, "repo", False]
        )
        path = store.get_path(project_iter)
        background = Gdk.RGBA()
        background.parse("#e8e8e8")
        tree = MagicMock()
        tree.row_expanded.return_value = False
        expander_column = object()
        tree.get_path_at_pos.return_value = (path, expander_column, 0, 0)
        owner = SimpleNamespace(
            project_store=store,
            project_tree=tree,
            project_expander_column=expander_column,
            project_row_background=background,
            COL_KIND=SlateWindow.COL_KIND,
        )
        event = SimpleNamespace(button=1, x=2, y=2)
        self.assertTrue(SlateWindow._on_tree_button(owner, tree, event))
        tree.expand_row.assert_called_once_with(path, False)
        tree.queue_draw.assert_called_once_with()

    def test_exited_active_terminal_selects_configured_fallback(self) -> None:
        """Removing an ended shell updates config and activates its sibling."""

        project = {
            "name": "repo",
            "terminals": ["main", "test"],
            "terminal_commands": {"main": "codex resume"},
            "last_terminal": "main",
        }
        config = SimpleNamespace(
            data={"active_terminal": "repo/main"},
            save=MagicMock(),
        )
        owner = SimpleNamespace(
            config=config,
            _populate_projects=MagicMock(),
            _select_tree_row=MagicMock(),
        )
        SlateWindow._remove_terminal_configuration(owner, project, "main")
        self.assertEqual(project["terminals"], ["test"])
        self.assertEqual(project["terminal_commands"], {})
        self.assertEqual(project["last_terminal"], "test")
        self.assertEqual(config.data["active_terminal"], "repo/test")
        config.save.assert_called_once_with()
        owner._select_tree_row.assert_called_once_with("repo", "test")

    def test_project_without_live_sessions_gets_plain_removal_confirmation(self) -> None:
        """Absent tmux sessions never expose meaningless leave-or-kill choices."""

        project = {"name": "repo", "terminals": ["main", "test"]}
        dialog = MagicMock()
        dialog.run.return_value = Gtk.ResponseType.OK
        owner = SimpleNamespace(
            terminals=MagicMock(),
            _finish_project_removal=MagicMock(),
        )
        with patch("slate.window.Gtk.MessageDialog", return_value=dialog):
            SlateWindow._show_remove_project_dialog(owner, project, [])
        labels = [call_args.args[0] for call_args in dialog.add_button.call_args_list]
        self.assertEqual(labels, ["Cancel", "Remove"])
        owner.terminals.forget.assert_has_calls(
            [call("repo", "main"), call("repo", "test")]
        )
        owner._finish_project_removal.assert_called_once_with(project)


if __name__ == "__main__":
    unittest.main()
