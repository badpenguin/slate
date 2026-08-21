"""GTK model tests for the polished multi-SCM status panel."""

import unittest
from types import SimpleNamespace
from unittest import mock

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, GLib, Gtk, Pango

from slate.panel import SCMPanel
from slate.scm.base import FileStatus, RepositoryRef, RepositorySyncStatus


class SCMPanelTest(unittest.TestCase):
    """Verify filtered groups and explicit repository presentation states."""

    @classmethod
    def setUpClass(cls) -> None:
        """Require the GTK display used by the final integration test session."""

        initialized, _arguments = Gtk.init_check(None)
        if not initialized:
            raise unittest.SkipTest("display GTK non disponibile")

    def setUp(self) -> None:
        """Create a panel with inert named action callbacks."""

        self.added: list[FileStatus] = []
        self.forgotten: list[FileStatus] = []
        self.committed: tuple[str, list[FileStatus]] | None = None
        self.reverted: list[FileStatus] = []
        self.previewed: FileStatus | None = None
        self.viewed: FileStatus | None = None
        self.edited_internal: FileStatus | None = None
        self.edited_external: FileStatus | None = None
        self.deleted: FileStatus | None = None
        self.diffed_repository: RepositoryRef | None = None
        self.diffed_paths: tuple[str, ...] = ()
        self.external_repository: RepositoryRef | None = None
        self.updated_repository: RepositoryRef | None = None
        self.repository_action: tuple[str, RepositoryRef] | None = None
        self.excluded_repository: RepositoryRef | None = None
        self.panel = SCMPanel(
            self._record_commit,
            self._ignore_diff,
            self._ignore_external,
            self._record_update,
            self._record_repository_action,
            self._ignore_scan,
            self._ignore_scan,
            self._ignore_exclude,
            self._record_preview,
            self._record_add,
            self._record_forget,
            self._record_revert,
            self._record_view,
            self._record_edit_internal,
            self._record_edit_external,
            self._record_delete,
        )

    def _record_commit(self, message: str, statuses: list[FileStatus]) -> None:
        """Record commit signals for explicit-checkbox assertions."""

        self.committed = (message, statuses)

    def _ignore_diff(self, repository: RepositoryRef, paths) -> None:
        """Record the repository explicitly chosen for Meld."""

        self.diffed_repository = repository
        self.diffed_paths = tuple(paths)

    def _ignore_external(self, repository: RepositoryRef) -> None:
        """Record the repository explicitly chosen for TortoiseHg."""

        self.external_repository = repository

    def _record_update(self, repository: RepositoryRef) -> None:
        """Record the repository explicitly selected for Update."""

        self.updated_repository = repository

    def _record_repository_action(
        self, action: str, repository: RepositoryRef
    ) -> None:
        """Record one deferred repository action and its explicit target."""

        self.repository_action = (action, repository)

    def _ignore_scan(self) -> None:
        """Accept repository scan signals irrelevant to presentation tests."""

    def _ignore_exclude(self, repository: RepositoryRef) -> None:
        """Record the repository explicitly excluded from Revisioni."""

        self.excluded_repository = repository

    def _record_preview(self, status: FileStatus | None) -> None:
        """Record the preview synchronized with the highlighted file row."""

        self.previewed = status

    def _record_add(self, statuses: list[FileStatus]) -> None:
        """Record explicit additions for action forwarding assertions."""

        self.added = statuses

    def _record_forget(self, statuses: list[FileStatus]) -> None:
        """Record added files explicitly returned to the untracked state."""

        self.forgotten = statuses

    def _record_revert(self, statuses: list[FileStatus]) -> None:
        """Record explicitly checked revert requests for safety assertions."""

        self.reverted = statuses

    def _record_view(self, status: FileStatus) -> None:
        """Record default-viewer requests from menus and keyboard shortcuts."""

        self.viewed = status

    def _record_edit_internal(self, status: FileStatus) -> None:
        """Record internal-editor requests from menus and keyboard shortcuts."""

        self.edited_internal = status

    def _record_edit_external(self, status: FileStatus) -> None:
        """Record gVim requests from menus and keyboard shortcuts."""

        self.edited_external = status

    def _record_delete(self, status: FileStatus) -> None:
        """Record confirmed-delete requests forwarded to the window."""

        self.deleted = status

    def test_repository_roots_use_distinct_colored_scm_badges(self) -> None:
        """Git and HG roots render their bundled badges instead of folder icons."""

        hg = RepositoryRef(".", "hg")
        git = RepositoryRef("app", "git")
        self.panel.set_repositories((hg, git))
        self.assertIsNot(self.panel.repository_icons["hg"], self.panel.repository_icons["git"])
        self.assertEqual(
            self.panel.store.get_value(
                self.panel.repository_iters[hg], self.panel.COL_ICON
            ),
            "hg",
        )
        self.assertEqual(
            self.panel.store.get_value(
                self.panel.repository_iters[git], self.panel.COL_ICON
            ),
            "git",
        )

    def _toggle_path(self, path: str) -> None:
        """Toggle a source-model file through its corresponding filtered path."""

        source_path = self.panel.store.get_path(self.panel.iter_by_path[path])
        filtered_path = self.panel.filtered_store.convert_child_path_to_path(
            source_path
        )
        self.panel._on_status_toggled(
            Gtk.CellRendererToggle(), filtered_path.to_string()
        )

    def test_native_typeahead_search_is_disabled(self) -> None:
        """Single-key SCM commands must never open GTK's search popup."""

        self.assertFalse(self.panel.tree.get_enable_search())

    def test_only_nonempty_groups_are_visible(self) -> None:
        """The filtered model exposes categories that contain at least one file."""

        self.panel.set_supported(True)
        self.panel.update_status(
            [
                FileStatus("src/app.py", "modified"),
                FileStatus("notes.txt", "untracked"),
            ],
            "default",
        )
        repository_iter = self.panel.filtered_store.get_iter_first()
        self.assertIsNotNone(repository_iter)
        labels: list[str] = []
        tree_iter = self.panel.filtered_store.iter_children(repository_iter)
        while tree_iter is not None:
            labels.append(
                self.panel.filtered_store.get_value(tree_iter, self.panel.COL_TEXT)
            )
            tree_iter = self.panel.filtered_store.iter_next(tree_iter)
        self.assertEqual(labels, ["Modified:  1", "New:  1"])
        self.assertFalse(hasattr(self.panel, "count_label"))
        self.assertEqual(self.panel.state_stack.get_visible_child_name(), "changes")

    def test_move_group_uses_one_labeled_row_with_destination_identity(self) -> None:
        """The move label cannot replace the canonical path used by UI actions."""

        moved = FileStatus("src/new.py", "moved", source_path="old.py")
        self.panel.update_status(
            [
                FileStatus("changed.py", "modified"),
                moved,
                FileStatus("add.py", "added"),
            ],
            "default",
        )
        repository_iter = self.panel.filtered_store.get_iter_first()
        labels: list[str] = []
        group_iter = self.panel.filtered_store.iter_children(repository_iter)
        moved_iter = None
        while group_iter is not None:
            labels.append(
                self.panel.filtered_store.get_value(group_iter, self.panel.COL_TEXT)
            )
            if self.panel.filtered_store.get_value(
                group_iter, self.panel.COL_STATE
            ) == "moved":
                moved_iter = self.panel.filtered_store.iter_children(group_iter)
            group_iter = self.panel.filtered_store.iter_next(group_iter)
        self.assertEqual(
            labels, ["Modified:  1", "Moved:  1", "Added:  1"]
        )
        self.assertIsNotNone(moved_iter)
        self.assertEqual(
            self.panel.filtered_store.get_value(moved_iter, self.panel.COL_TEXT),
            "old.py → src/new.py",
        )
        self.assertEqual(
            self.panel.filtered_store.get_value(moved_iter, self.panel.COL_PATH),
            "src/new.py",
        )
        self.panel.tree.get_selection().select_iter(moved_iter)
        self.assertEqual(self.panel.selected_statuses(), [moved])

    def test_revision_files_are_alphabetical_not_in_scan_order(self) -> None:
        """Mercurial result order cannot leak into visible file ordering."""

        self.panel.update_status(
            [
                FileStatus("zeta.py", "modified"),
                FileStatus("Alpha.py", "modified"),
                FileStatus("beta.py", "modified"),
            ],
            "default",
        )
        group_source = self.panel.group_iters[
            (RepositoryRef(".", "hg"), "modified")
        ]
        group_path = self.panel.filtered_store.convert_child_path_to_path(
            self.panel.store.get_path(group_source)
        )
        group_iter = self.panel.filtered_store.get_iter(group_path)
        names: list[str] = []
        tree_iter = self.panel.filtered_store.iter_children(group_iter)
        while tree_iter is not None:
            names.append(
                self.panel.filtered_store.get_value(tree_iter, self.panel.COL_TEXT)
            )
            tree_iter = self.panel.filtered_store.iter_next(tree_iter)
        self.assertEqual(names, ["Alpha.py", "beta.py", "zeta.py"])

    def test_revision_font_size_updates_renderer(self) -> None:
        """Revision settings change the shared group and file text renderer."""

        with mock.patch.object(self.panel.tree, "queue_resize") as resize:
            with mock.patch.object(self.panel.tree, "queue_draw") as draw:
                self.panel.set_font_size(16)
        self.assertEqual(self.panel.text_renderer.get_property("size-points"), 16.0)
        resize.assert_called_once_with()
        draw.assert_called_once_with()

    def test_clean_and_unsupported_are_distinct_states(self) -> None:
        """Clean repositories do not look like unsupported project directories."""

        self.panel.set_repositories([RepositoryRef(".", "hg")])
        self.panel.update_status([], "default")
        self.assertEqual(self.panel.state_stack.get_visible_child_name(), "changes")
        self.assertIn(RepositoryRef(".", "hg"), self.panel.repository_iters)
        self.assertTrue(self.panel.commit_section.get_visible())
        self.assertTrue(self.panel.tool_actions.get_visible())
        self.assertTrue(self.panel.commit_actions.get_visible())
        self.assertFalse(self.panel.add_new_button.get_sensitive())
        self.assertFalse(self.panel.revert_button.get_sensitive())
        self.assertFalse(self.panel.commit_button.get_sensitive())
        self.panel.set_supported(False)
        self.assertEqual(self.panel.state_stack.get_visible_child_name(), "unsupported")

    def test_root_repository_uses_explicit_root_label(self) -> None:
        """The root repository label uses the agreed concise root marker."""

        self.panel.bind_project("gtk-zed", True)
        root = RepositoryRef(".", "hg")
        self.panel.set_repositories([root])
        self.panel.update_status([FileStatus("a.py", "modified")], "default")
        label = self.panel.store.get_value(
            self.panel.repository_iters[root], self.panel.COL_TEXT
        )
        self.assertEqual(label, "[root] — default — remote: not verified")

    def test_multi_repository_labels_are_complete_and_have_no_counts(self) -> None:
        """Multi-repository labels distinguish root and full nested paths."""

        self.panel.bind_project("workspace", True)
        nested = "public/wp-content/themes/example"
        root_repository = RepositoryRef(".", "hg")
        nested_repository = RepositoryRef(nested, "hg")
        self.panel.set_repositories([root_repository, nested_repository])
        self.panel.update_status([], "default", root_repository)
        self.panel.update_status([], "stable", nested_repository)
        root_label = self.panel.store.get_value(
            self.panel.repository_iters[root_repository], self.panel.COL_TEXT
        )
        nested_label = self.panel.store.get_value(
            self.panel.repository_iters[nested_repository], self.panel.COL_TEXT
        )
        self.assertEqual(
            root_label, "[root] — default — remote: not verified"
        )
        self.assertEqual(
            nested_label, f"{nested} — stable — remote: not verified"
        )
        self.assertNotIn("(", root_label)
        self.assertNotIn("(", nested_label)

    def test_error_bar_is_dismissible_without_clearing_rows(self) -> None:
        """Dismissing an error retains the last successfully rendered snapshot."""

        self.panel.set_supported(True)
        self.panel.update_status([FileStatus("a.py", "modified")], "default")
        self.panel.show_error("errore di prova")
        self.assertEqual(self.panel.error_label.get_text(), "errore di prova")
        self.panel.clear_error()
        self.assertEqual(self.panel.error_label.get_text(), "")
        self.assertIn("a.py", self.panel.status_by_path)

    def test_empty_repository_remains_visible_and_owns_external_tools(self) -> None:
        """A clean repository node survives and provides contextual GUI actions."""

        repository_ref = RepositoryRef("app", "hg")
        self.panel.set_repositories([repository_ref])
        self.panel.update_status([], "default", repository_ref)
        self.assertIn(repository_ref, self.panel.repository_iters)
        self.panel.context_repository = repository_ref
        self.panel._on_context_meld(Gtk.MenuItem())
        self.panel._on_context_external(Gtk.MenuItem())
        with mock.patch("slate.panel.GLib.idle_add") as idle_add:
            self.panel._on_context_update(Gtk.MenuItem())
        update_callback, update_repository = idle_add.call_args.args
        with mock.patch("slate.panel.GLib.idle_add") as idle_add:
            self.panel._on_context_publish(Gtk.MenuItem())
        action_callback, action, action_repository = idle_add.call_args.args
        with mock.patch("slate.panel.GLib.idle_add") as idle_add:
            self.panel._on_context_exclude(Gtk.MenuItem())
        idle_callback, repository = idle_add.call_args.args
        self.assertEqual(self.diffed_repository, repository_ref)
        self.assertEqual(self.external_repository, repository_ref)
        self.assertIsNone(self.updated_repository)
        self.assertEqual(
            update_callback(update_repository), GLib.SOURCE_REMOVE
        )
        self.assertEqual(self.updated_repository, repository_ref)
        self.assertIsNone(self.repository_action)
        self.assertEqual(
            action_callback(action, action_repository), GLib.SOURCE_REMOVE
        )
        self.assertEqual(self.repository_action, ("publish", repository_ref))
        self.assertIsNone(self.excluded_repository)
        self.assertIs(idle_callback.__self__, self.panel)
        self.assertEqual(repository, repository_ref)
        self.assertEqual(idle_callback(repository), GLib.SOURCE_REMOVE)
        self.assertEqual(self.excluded_repository, repository_ref)

    def test_repository_menu_keeps_mutations_between_tools_and_exclusion(self) -> None:
        """Potentially mutating actions form the requested middle menu group."""

        self.panel.context_repository = RepositoryRef(".", "hg")
        menu = mock.MagicMock()
        with mock.patch("slate.panel.Gtk.Menu", return_value=menu):
            self.panel._show_repository_menu(None)
        labels: list[str] = []
        for call in menu.append.call_args_list:
            item = call.args[0]
            if isinstance(item, Gtk.SeparatorMenuItem):
                labels.append("|")
            else:
                labels.append(item.get_child().get_children()[1].get_text())
        self.assertEqual(
            labels,
            [
                "Open in Meld",
                "Open in TortoiseHg",
                "Verify…",
                "|",
                "Update…",
                "Publish…",
                "New branch…",
                "Switch branch…",
                "Merge branch…",
                "Assign tag…",
                "|",
                "Exclude repository",
            ],
        )

    def test_remote_status_updates_only_the_repository_label(self) -> None:
        """Explicit verification changes one stable row without rebuilding it."""

        repository = RepositoryRef(".", "git")
        self.panel.set_repositories([repository])
        self.panel.update_status([], "main", repository)
        tree_iter = self.panel.repository_iters[repository]
        self.panel.set_remote_status(
            repository, RepositorySyncStatus("diverged", 2, 3)
        )
        self.assertEqual(
            self.panel.store.get_value(tree_iter, self.panel.COL_TEXT),
            "[root] — main — remote: diverged · ahead 2, behind 3",
        )
        self.assertIs(self.panel.repository_iters[repository], tree_iter)

    def test_inactive_history_change_invalidates_cached_remote_status(self) -> None:
        """An inactive project cannot retain a stale verified relationship."""

        repository = RepositoryRef(".", "git")
        self.panel.bind_project("first", True)
        self.panel.set_repositories([repository])
        self.panel.update_status([], "main", repository)
        self.panel.set_remote_status(repository, RepositorySyncStatus("synced"))
        self.panel.bind_project("second", True)
        self.panel.set_project_remote_status(
            "first", repository, RepositorySyncStatus()
        )
        self.panel.bind_project("first", True)
        self.panel.set_repositories([repository])
        self.assertEqual(
            self.panel.store.get_value(
                self.panel.repository_iters[repository], self.panel.COL_TEXT
            ),
            "[root] — main — remote: not verified",
        )

    def test_clean_repository_root_is_not_bold(self) -> None:
        """Repository roots use bold text only while they own visible changes."""

        repository_ref = RepositoryRef(".", "hg")
        self.panel.set_repositories([repository_ref])
        self.panel.update_status([], "default", repository_ref)
        source_iter = self.panel.repository_iters[repository_ref]
        filtered_path = self.panel.filtered_store.convert_child_path_to_path(
            self.panel.store.get_path(source_iter)
        )
        tree_iter = self.panel.filtered_store.get_iter(filtered_path)
        self.panel._render_status_text(
            self.panel.tree.get_columns()[0],
            self.panel.text_renderer,
            self.panel.filtered_store,
            tree_iter,
        )
        self.assertEqual(
            self.panel.text_renderer.get_property("weight"), Pango.Weight.NORMAL
        )
        self.panel.update_status(
            [FileStatus("changed.py", "modified")], "default", repository_ref
        )
        filtered_path = self.panel.filtered_store.convert_child_path_to_path(
            self.panel.store.get_path(source_iter)
        )
        tree_iter = self.panel.filtered_store.get_iter(filtered_path)
        self.panel._render_status_text(
            self.panel.tree.get_columns()[0],
            self.panel.text_renderer,
            self.panel.filtered_store,
            tree_iter,
        )
        self.assertEqual(
            self.panel.text_renderer.get_property("weight"), Pango.Weight.BOLD
        )

    def test_revision_repositories_and_groups_are_expanded_by_default(self) -> None:
        """New Revision trees open fully while an explicit collapse survives refresh."""

        status = FileStatus("src/app.py", "modified", repository="app")
        repository_ref = RepositoryRef("app", "hg")
        self.panel.set_repositories([repository_ref])
        self.panel.update_status([status], "default", repository_ref)
        repository_source = self.panel.repository_iters[repository_ref]
        repository_path = self.panel.filtered_store.convert_child_path_to_path(
            self.panel.store.get_path(repository_source)
        )
        group_source = self.panel.group_iters[(repository_ref, "modified")]
        group_path = self.panel.filtered_store.convert_child_path_to_path(
            self.panel.store.get_path(group_source)
        )
        self.assertTrue(self.panel.tree.row_expanded(repository_path))
        self.assertTrue(self.panel.tree.row_expanded(group_path))
        with mock.patch(
            "slate.panel.Gtk.get_current_event", return_value=SimpleNamespace()
        ):
            self.panel.tree.collapse_row(group_path)
        self.panel.update_status(
            [status, FileStatus("src/other.py", "modified", repository="app")],
            "default",
            repository_ref,
        )
        self.assertFalse(self.panel.tree.row_expanded(group_path))

    def test_automatic_revision_collapse_is_not_remembered(self) -> None:
        """A GTK layout collapse is reopened because it is not user intent."""

        status = FileStatus("src/app.py", "modified", repository="app")
        repository_ref = RepositoryRef("app", "hg")
        self.panel.set_repositories([repository_ref])
        self.panel.update_status([status], "default", repository_ref)
        repository_path = self.panel.filtered_store.convert_child_path_to_path(
            self.panel.store.get_path(self.panel.repository_iters[repository_ref])
        )
        self.panel.tree.collapse_row(repository_path)
        self.assertIn("repository:app", self.panel.current_state.expanded_rows)
        self.panel._restore_revision_expansion_idle()
        self.assertTrue(self.panel.tree.row_expanded(repository_path))

    def test_revision_tree_does_not_collapse_when_status_changes_group(self) -> None:
        """Automatic refiltering cannot be mistaken for a manual tree collapse."""

        modified = FileStatus("src/app.py", "modified", repository="app")
        added = FileStatus("src/app.py", "added", repository="app")
        repository_ref = RepositoryRef("app", "hg")
        self.panel.set_repositories([repository_ref])
        self.panel.update_status([modified], "default", repository_ref)
        self.panel.update_status([], "default", repository_ref)
        self.panel.update_status([added], "default", repository_ref)
        repository_path = self.panel.filtered_store.convert_child_path_to_path(
            self.panel.store.get_path(self.panel.repository_iters[repository_ref])
        )
        added_path = self.panel.filtered_store.convert_child_path_to_path(
            self.panel.store.get_path(
                self.panel.group_iters[(repository_ref, "added")]
            )
        )
        self.assertTrue(self.panel.tree.row_expanded(repository_path))
        self.assertTrue(self.panel.tree.row_expanded(added_path))

    def test_commit_requires_a_nonempty_message_and_respects_busy_state(self) -> None:
        """A visible commit remains inert until all of its prerequisites exist."""

        self.panel.set_supported(True)
        self.panel.update_status([FileStatus("a.py", "modified")], "default")
        self.assertFalse(self.panel.commit_button.get_sensitive())
        self.panel.message.get_buffer().set_text("messaggio")
        self.assertFalse(self.panel.commit_button.get_sensitive())
        self._toggle_path("a.py")
        self.assertTrue(self.panel.commit_button.get_sensitive())
        self.panel.set_commit_busy(True)
        self.assertFalse(self.panel.commit_button.get_sensitive())
        self.panel.set_commit_busy(False)
        self.assertTrue(self.panel.commit_button.get_sensitive())
        self.assertNotIn(
            "suggested-action", self.panel.commit_button.get_style_context().list_classes()
        )

    def test_valid_snapshot_needs_no_duplicate_supported_flag(self) -> None:
        """A real SCM snapshot and explicit inputs fully determine Commit state."""

        status = FileStatus("a.py", "modified")
        self.panel.update_status([status], "default")
        self.panel.message.get_buffer().set_text("messaggio")
        self._toggle_path(status.path)
        self.assertEqual(self.panel.checked_statuses(), [status])
        self.assertTrue(self.panel.commit_button.get_sensitive())

    def test_commit_message_uses_tab_for_focus_navigation(self) -> None:
        """The commit editor never consumes Tab as message content."""

        self.assertFalse(self.panel.message.get_accepts_tab())

    def test_ctrl_enter_commits_only_when_the_normal_action_is_enabled(self) -> None:
        """The message shortcut shares the button's complete validation state."""

        event = SimpleNamespace(
            keyval=Gdk.KEY_Return,
            state=Gdk.ModifierType.CONTROL_MASK,
        )
        status = FileStatus("a.py", "modified")
        self.panel.set_supported(True)
        self.panel.update_status([status], "default")
        self.assertTrue(self.panel._on_message_key_press(self.panel.message, event))
        self.assertIsNone(self.committed)

        self.panel.message.get_buffer().set_text("messaggio")
        self._toggle_path(status.path)
        self.assertTrue(self.panel._on_message_key_press(self.panel.message, event))
        self.assertEqual(self.committed, ("messaggio", [status]))

    def test_plain_enter_remains_available_for_multiline_commit_messages(self) -> None:
        """Enter without Ctrl is left to GtkTextView for multiline editing."""

        event = SimpleNamespace(keyval=Gdk.KEY_Return, state=0)
        self.assertFalse(self.panel._on_message_key_press(self.panel.message, event))
        self.assertIsNone(self.committed)

    def test_commit_message_does_not_request_content_width(self) -> None:
        """Long commit text wraps without widening the surrounding SCM panel."""

        self.panel.message.get_buffer().set_text("messaggio-senza-spazi" * 100)
        self.assertEqual(self.panel.message.get_wrap_mode(), Gtk.WrapMode.WORD_CHAR)
        self.assertEqual(self.panel.message.do_get_preferred_width(), (0, 0))

    def test_add_new_forwards_only_untracked_files(self) -> None:
        """The bulk action never includes already tracked working-copy changes."""

        self.panel.set_supported(True)
        new_file = FileStatus("new.py", "untracked")
        self.panel.update_status(
            [FileStatus("old.py", "modified"), new_file], "default"
        )
        self.panel._on_add_new_clicked(self.panel.add_new_button)
        self.assertEqual(self.added, [new_file])

    def test_tool_row_precedes_message_and_final_row_contains_commit_controls(self) -> None:
        """Review tools stay above the editor and Commit shares its final row."""

        children = self.panel.get_children()
        self.assertLess(
            children.index(self.panel.tool_actions),
            children.index(self.panel.commit_section),
        )
        self.assertLess(
            children.index(self.panel.commit_section),
            children.index(self.panel.commit_actions),
        )
        self.assertEqual(
            self.panel.commit_actions.get_children(),
            [
                self.panel.commit_select_all_check,
                self.panel.commit_shortcut_label,
                self.panel.commit_button,
            ],
        )
        self.assertEqual(self.panel.commit_shortcut_label.get_text(), "Ctrl+Enter")
        self.assertIn(
            "commit-shortcut",
            self.panel.commit_shortcut_label.get_style_context().list_classes(),
        )
        self.assertEqual(self.panel.commit_select_all_check.get_label(), "Select all")
        self.assertEqual(
            self.panel.button_labels[self.panel.add_new_button].get_text(),
            "Add new",
        )
        icon_name, _size = self.panel.button_icons[
            self.panel.add_new_button
        ].get_icon_name()
        self.assertNotEqual(icon_name, "list-add")
        for button in self.panel.button_labels:
            self.assertEqual(button.get_child().get_spacing(), 4)

    def test_repository_actions_share_select_all_row_on_the_right(self) -> None:
        """Scan and Reset occupy the right side of the single selection toolbar."""

        self.assertEqual(
            self.panel.repository_actions.get_children(),
            [self.panel.scan_button, self.panel.reset_button],
        )
        self.assertIs(
            self.panel.repository_actions.get_parent(), self.panel.select_all_bar
        )
        self.assertEqual(
            self.panel.select_all_bar.query_child_packing(
                self.panel.repository_actions
            )[3],
            Gtk.PackType.END,
        )
        repository_ref = RepositoryRef("app", "hg")
        self.panel.set_repositories([repository_ref])
        self.panel.update_status([], "default", repository_ref)
        self.assertTrue(self.panel.select_all_bar.get_visible())
        self.assertTrue(self.panel.repository_actions.get_visible())
        self.assertFalse(self.panel.select_all_check.get_visible())
        self.assertFalse(hasattr(self.panel, "branch_label"))
        self.assertFalse(hasattr(self.panel, "count_label"))
        for button in (self.panel.scan_button, self.panel.reset_button):
            content = button.get_child()
            self.assertEqual(content.get_spacing(), 4)
            self.assertIsInstance(content.get_children()[0], Gtk.Image)

    def test_checkboxes_define_commit_and_revert_targets(self) -> None:
        """Tracked operations ignore row focus and use only explicit checkboxes."""

        checked = FileStatus("checked.py", "modified")
        unchecked = FileStatus("unchecked.py", "modified")
        new_file = FileStatus("new.py", "untracked")
        self.panel.set_supported(True)
        self.panel.update_status([checked, unchecked, new_file], "default")
        self._toggle_path("checked.py")
        self.panel.message.get_buffer().set_text("solo spuntato")
        self.panel._on_commit_clicked(self.panel.commit_button)
        self.panel._on_revert_checked_clicked(self.panel.revert_button)
        self.assertEqual(self.committed, ("solo spuntato", [checked]))
        self.assertEqual(self.reverted, [checked])
        self.assertNotIn("new.py", self.panel.checked_paths)
        new_iter = self.panel.iter_by_path["new.py"]
        self.assertFalse(
            self.panel.store.get_value(new_iter, self.panel.COL_CHECKABLE)
        )

    def test_select_all_tracks_only_checkable_files_and_reports_partial_state(self) -> None:
        """The master checkbox excludes new files and becomes partial after one toggle."""

        first = FileStatus("first.py", "modified")
        second = FileStatus("second.py", "added")
        new_file = FileStatus("new.py", "untracked")
        self.panel.set_supported(True)
        self.panel.update_status([first, second, new_file], "default")
        self.panel.select_all_check.set_active(True)
        self.assertEqual(self.panel.checked_statuses(), [first, second])
        self.assertNotIn("new.py", self.panel.checked_paths)
        self.assertTrue(self.panel.commit_select_all_check.get_active())
        self._toggle_path("first.py")
        self.assertTrue(self.panel.select_all_check.get_inconsistent())
        self.assertTrue(self.panel.commit_select_all_check.get_inconsistent())
        self.assertEqual(self.panel.checked_statuses(), [second])
        self.panel.commit_select_all_check.set_active(True)
        self.assertEqual(self.panel.checked_statuses(), [first, second])
        self.assertTrue(self.panel.select_all_check.get_active())

    def test_both_select_all_controls_focus_commit_message(self) -> None:
        """Either master checkbox focuses the message after selecting files."""

        self.panel.set_supported(True)
        self.panel.update_status([FileStatus("tracked.py", "modified")], "default")
        window = Gtk.Window()
        window.add(self.panel)
        window.show_all()
        self.addCleanup(window.destroy)
        while Gtk.events_pending():
            Gtk.main_iteration_do(False)

        for control in (
            self.panel.select_all_check,
            self.panel.commit_select_all_check,
        ):
            self.panel.select_all_check.set_active(False)
            control.grab_focus()
            control.set_active(True)
            while Gtk.events_pending():
                Gtk.main_iteration_do(False)
            self.assertIs(window.get_focus(), self.panel.message)

    def test_checkbox_remains_in_original_expander_column(self) -> None:
        """Checkbox layout stays intact while CSS alone compacts indentation."""

        columns = self.panel.tree.get_columns()
        self.assertEqual(len(columns), 1)
        self.assertIs(self.panel.tree.get_expander_column(), columns[0])
        self.assertIn(self.panel.toggle_renderer, columns[0].get_cells())

    def test_keyboard_actions_and_selection_keep_preview_in_sync(self) -> None:
        """File shortcuts act on the highlighted row and arrows refresh preview."""

        tracked = FileStatus("tracked.py", "modified")
        new_file = FileStatus("new.py", "untracked")
        other_new_file = FileStatus("other-new.py", "untracked")
        self.panel.set_supported(True)
        self.panel.update_status([tracked, new_file, other_new_file], "default")
        selection = self.panel.tree.get_selection()
        source_path = self.panel.store.get_path(self.panel.iter_by_path[tracked.path])
        tracked_path = self.panel.filtered_store.convert_child_path_to_path(source_path)
        self.panel.tree.set_cursor(tracked_path)
        self.assertEqual(self.previewed, tracked)

        def press(keyval: int) -> bool:
            """Send one unmodified key event to the SCM tree handler."""

            event = SimpleNamespace(keyval=keyval, state=Gdk.ModifierType(0))
            return self.panel._on_tree_key_press(self.panel.tree, event)

        self.assertTrue(press(Gdk.KEY_space))
        self.assertEqual(self.panel.checked_statuses(), [tracked])
        self.assertTrue(press(Gdk.KEY_v))
        self.assertEqual(self.viewed, tracked)
        self.assertTrue(press(Gdk.KEY_e))
        self.assertEqual(self.edited_internal, tracked)
        self.assertTrue(press(Gdk.KEY_m))
        self.assertEqual(self.edited_external, tracked)
        self.assertTrue(press(Gdk.KEY_d))
        self.assertEqual(self.diffed_repository, RepositoryRef(".", "hg"))
        self.assertEqual(self.diffed_paths, ("tracked.py",))
        self.assertTrue(press(Gdk.KEY_Delete))
        self.assertEqual(self.deleted, tracked)

        source_path = self.panel.store.get_path(self.panel.iter_by_path[new_file.path])
        new_path = self.panel.filtered_store.convert_child_path_to_path(source_path)
        self.panel.tree.set_cursor(new_path)
        selection.unselect_all()
        selection.select_path(new_path)
        self.assertEqual(self.previewed, new_file)
        source_path = self.panel.store.get_path(
            self.panel.iter_by_path[other_new_file.path]
        )
        other_new_path = self.panel.filtered_store.convert_child_path_to_path(
            source_path
        )
        selection.select_path(other_new_path)
        self.assertEqual(
            self.panel.selected_untracked_statuses(),
            [new_file, other_new_file],
        )
        self.assertTrue(press(Gdk.KEY_a))
        self.assertEqual(self.added, [new_file, other_new_file])
        self.panel.context_add_statuses = self.panel.selected_untracked_statuses()
        self.panel._on_context_add(Gtk.MenuItem())
        self.assertEqual(self.added, [new_file, other_new_file])

    def test_space_sets_all_selected_tracked_checkboxes_from_cursor(self) -> None:
        """Space applies the focused checkbox's next state to selected tracked rows."""

        first = FileStatus("first.py", "modified")
        second = FileStatus("second.py", "modified")
        new_file = FileStatus("new.py", "untracked")
        self.panel.set_supported(True)
        self.panel.update_status([first, second, new_file], "default")
        selection = self.panel.tree.get_selection()

        filtered_paths = {}
        for status in (first, second, new_file):
            source_path = self.panel.store.get_path(
                self.panel.iter_by_path[status.path]
            )
            filtered_paths[status.path] = (
                self.panel.filtered_store.convert_child_path_to_path(source_path)
            )

        self.panel.tree.set_cursor(filtered_paths[first.path])
        selection.unselect_all()
        for status in (first, second, new_file):
            selection.select_path(filtered_paths[status.path])
        event = SimpleNamespace(keyval=Gdk.KEY_space, state=Gdk.ModifierType(0))

        self.assertTrue(self.panel._on_tree_key_press(self.panel.tree, event))
        self.assertEqual(self.panel.checked_statuses(), [first, second])

        # Una selezione mista deve diventare uniformemente deselezionata perché
        # la riga col cursore è già spuntata, senza coinvolgere il file nuovo.
        self._toggle_path(second.path)
        self.assertEqual(self.panel.checked_statuses(), [first])
        self.assertTrue(self.panel._on_tree_key_press(self.panel.tree, event))
        self.assertEqual(self.panel.checked_statuses(), [])

    def test_multi_selection_menu_toggles_available_checkboxes(self) -> None:
        """The multi-file menu exposes Space semantics for tracked selections."""

        first = FileStatus("first.py", "modified")
        second = FileStatus("second.py", "added")
        new_file = FileStatus("new.py", "untracked")
        self.panel.set_supported(True)
        self.panel.update_status([first, second, new_file], "default")
        self.panel.context_selected_statuses = [first, second, new_file]
        self.panel.context_checkbox_statuses = [first, second]
        self.panel.context_checkbox_checked = True

        menu = self.panel._build_file_menu()
        labels = [
            child.get_child().get_children()[1].get_label()
            for child in menu.get_children()
            if isinstance(child.get_child(), Gtk.Box)
        ]
        self.assertEqual(labels, ["Check (2)"])
        checkbox_item = next(
            child
            for child in menu.get_children()
            if isinstance(child.get_child(), Gtk.Box)
            and child.get_child().get_children()[1].get_label()
            == "Check (2)"
        )
        checkbox_item.activate()

        self.assertEqual(self.panel.checked_statuses(), [first, second])
        self.assertNotIn(new_file.path, self.panel.checked_paths)

    def test_multi_selection_blocks_single_file_shortcuts(self) -> None:
        """Single-file keyboard actions stay inert while several rows are selected."""

        first = FileStatus("first.py", "modified")
        second = FileStatus("second.py", "modified")
        self.panel.set_supported(True)
        self.panel.update_status([first, second], "default")
        selection = self.panel.tree.get_selection()
        for status in (first, second):
            source_path = self.panel.store.get_path(
                self.panel.iter_by_path[status.path]
            )
            filtered_path = self.panel.filtered_store.convert_child_path_to_path(
                source_path
            )
            selection.select_path(filtered_path)
            if status is first:
                self.panel.tree.set_cursor(filtered_path)

        for keyval in (
            Gdk.KEY_v,
            Gdk.KEY_m,
            Gdk.KEY_e,
            Gdk.KEY_d,
            Gdk.KEY_Delete,
        ):
            event = SimpleNamespace(keyval=keyval, state=Gdk.ModifierType(0))
            self.assertTrue(self.panel._on_tree_key_press(self.panel.tree, event))

        self.assertIsNone(self.viewed)
        self.assertIsNone(self.edited_internal)
        self.assertIsNone(self.edited_external)
        self.assertIsNone(self.deleted)

    def test_d_opens_meld_for_repository_root(self) -> None:
        """D on a repository row launches its full directory comparison."""

        repository = RepositoryRef(".", "git")
        self.panel.set_repositories([repository])
        source_path = self.panel.store.get_path(
            self.panel.repository_iters[repository]
        )
        filtered_path = self.panel.filtered_store.convert_child_path_to_path(
            source_path
        )
        self.panel.tree.set_cursor(filtered_path)
        event = SimpleNamespace(
            keyval=Gdk.KEY_d,
            state=Gdk.ModifierType(0),
        )

        self.assertTrue(self.panel._on_tree_key_press(self.panel.tree, event))
        self.assertEqual(self.diffed_repository, repository)
        self.assertEqual(self.diffed_paths, ())

    def test_modified_file_menu_exposes_meld_with_d(self) -> None:
        """The contextual tracked-file action displays the shared D shortcut."""

        status = FileStatus("tracked.py", "modified")
        self.panel.context_status = status
        self.panel.context_selected_statuses = [status]
        menu = self.panel._build_file_menu()
        meld_item = next(
            child
            for child in menu.get_children()
            if isinstance(child.get_child(), Gtk.Box)
            and child.get_child().get_children()[1].get_text() == "Open in Meld"
        )
        meld_item.activate()

        self.assertEqual(self.diffed_repository, RepositoryRef(".", "hg"))
        self.assertEqual(self.diffed_paths, ("tracked.py",))

    def test_context_menu_items_always_have_icons(self) -> None:
        """Every contextual file action exposes an icon with the required gap."""

        for label, icon, keyval in (
            ("Visualizza", "document-open", Gdk.KEY_v),
            ("Modifica in SLATE", "accessories-text-editor", Gdk.KEY_e),
            ("Modifica in gVim", "gvim", Gdk.KEY_m),
            ("Aggiungi", "list-add", Gdk.KEY_a),
            ("Annulla aggiunta", "list-remove", None),
            ("Elimina", "edit-delete", Gdk.KEY_Delete),
        ):
            item = self.panel._menu_item(label, icon, keyval)
            content = item.get_child()
            self.assertEqual(content.get_spacing(), 4)
            self.assertIsInstance(content.get_children()[0], Gtk.Image)
            self.assertIsInstance(content.get_children()[1], Gtk.AccelLabel)

    def test_revision_actions_are_separated_from_generic_file_actions(self) -> None:
        """Mercurial add is visually isolated from view and edit commands."""

        new_file = FileStatus("new.py", "untracked")
        self.panel.context_status = new_file
        self.panel.context_add_statuses = [new_file]
        self.panel.context_forget_statuses = []
        menu = self.panel._build_file_menu()
        children = menu.get_children()
        add_index = next(
            index
            for index, child in enumerate(children)
            if isinstance(child.get_child(), Gtk.Box)
            and child.get_child().get_children()[1].get_text() == "Add"
        )
        self.assertIsInstance(children[add_index - 1], Gtk.SeparatorMenuItem)

    def test_added_selection_can_be_returned_to_new(self) -> None:
        """The contextual forget action targets added files and preserves others."""

        added = FileStatus("added.py", "added")
        modified = FileStatus("modified.py", "modified")
        self.panel.set_supported(True)
        self.panel.update_status([added, modified], "default")
        source_path = self.panel.store.get_path(self.panel.iter_by_path[added.path])
        added_path = self.panel.filtered_store.convert_child_path_to_path(source_path)
        self.panel.tree.set_cursor(added_path)
        selection = self.panel.tree.get_selection()
        selection.unselect_all()
        selection.select_path(added_path)
        self.assertEqual(self.panel.selected_added_statuses(), [added])
        self.panel.context_forget_statuses = self.panel.selected_added_statuses()
        self.panel._on_context_forget(Gtk.MenuItem())
        self.assertEqual(self.forgotten, [added])

    def test_status_reconciliation_does_not_open_next_file_preview(self) -> None:
        """Removing a committed cursor row cannot preview the remaining sibling."""

        committed = FileStatus("committed.py", "modified")
        remaining = FileStatus("remaining.py", "modified")
        self.panel.set_supported(True)
        self.panel.update_status([committed, remaining], "default")
        source_path = self.panel.store.get_path(
            self.panel.iter_by_path[committed.path]
        )
        committed_path = self.panel.filtered_store.convert_child_path_to_path(
            source_path
        )
        self.panel.tree.set_cursor(committed_path)
        self.assertEqual(self.previewed, committed)
        self.previewed = None
        self.panel.update_status([remaining], "default")
        self.assertIsNone(self.previewed)

    def test_project_switch_reuses_flat_revision_rows_without_reconciliation(self) -> None:
        """Returning to a project reattaches its unchanged flat status model."""

        statuses = [
            FileStatus("src/one.py", "modified"),
            FileStatus("src/two.py", "untracked"),
        ]
        self.panel.bind_project("first", True)
        self.panel.update_status(statuses, "default")
        first_store = self.panel.store
        self.assertIn("src/one.py", self.panel.iter_by_path)

        self.panel.bind_project("second", True)
        self.panel.update_status([FileStatus("other.py", "added")], "default")
        self.panel.bind_project("first", True)
        with mock.patch.object(self.panel, "_reconcile_status_rows") as reconcile:
            self.panel.update_status(list(statuses), "default")
        reconcile.assert_not_called()
        self.assertIs(self.panel.store, first_store)
        self.assertEqual(set(self.panel.status_by_path), {"src/one.py", "src/two.py"})

    def test_same_path_in_two_repositories_has_independent_checkbox_targets(self) -> None:
        """Repository-qualified keys prevent identical paths from colliding."""

        first = FileStatus("README.md", "modified", repository="app")
        second = FileStatus("README.md", "modified", repository="theme")
        app_repository = RepositoryRef("app", "hg")
        theme_repository = RepositoryRef("theme", "hg")
        self.panel.set_repositories([app_repository, theme_repository])
        self.panel.update_status([first], "default", app_repository)
        self.panel.update_status([second], "default", theme_repository)
        first_key = self.panel._status_key(first)
        second_key = self.panel._status_key(second)
        self.assertNotEqual(first_key, second_key)
        self.assertEqual(set(self.panel.status_by_path), {first_key, second_key})
        source_path = self.panel.store.get_path(self.panel.iter_by_path[first_key])
        filtered_path = self.panel.filtered_store.convert_child_path_to_path(source_path)
        self.panel._on_status_toggled(
            Gtk.CellRendererToggle(), filtered_path.to_string()
        )
        self.assertEqual(self.panel.checked_statuses(), [first])


if __name__ == "__main__":
    unittest.main()
