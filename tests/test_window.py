"""Focused window-action tests without constructing another SLATE GUI."""

import io
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

from slate.project_files import DirectoryInspection
from slate.project_limit import ProjectStatusLimit
from slate.processes import CommandResult
from slate.preview import FilePreview
from slate.scm.base import FileStatus, RepositoryRef, RepositorySyncStatus
from slate.scm.git import GitSCM
from slate.terminals import PaneInfo
from slate.window import SlateWindow, _CODEX_RESUME_COMMAND, _moved_sequence


class WindowActionTest(unittest.TestCase):
    """Verify high-level SCM actions independently from GTK window startup."""

    def test_project_sort_keeps_fifo_pins_before_alphabetical_projects(self) -> None:
        """Alphabetical sorting affects only the unpinned project band."""

        projects = [{"name": name} for name in ("zeta", "beta", "alpha", "gamma")]
        config = SimpleNamespace(
            data={
                "projects": projects,
                "pinned_projects": ["gamma", "beta"],
                "expanded_projects": [],
            },
            save=MagicMock(),
        )
        owner = SimpleNamespace(
            config=config,
            project_tree=MagicMock(),
            COL_PROJECT=SlateWindow.COL_PROJECT,
            COL_ITEM=SlateWindow.COL_ITEM,
            COL_KIND=SlateWindow.COL_KIND,
            _populate_projects=MagicMock(),
            _select_tree_row=MagicMock(),
        )
        owner.project_tree.get_selection.return_value.get_selected.return_value = (
            None,
            None,
        )
        owner._ordered_top_level_projects = (
            SlateWindow._ordered_top_level_projects.__get__(owner)
        )
        owner._apply_project_order = SlateWindow._apply_project_order.__get__(owner)

        SlateWindow._on_sort_projects(owner, MagicMock())

        self.assertEqual(
            [project["name"] for project in config.data["projects"]],
            ["gamma", "beta", "alpha", "zeta"],
        )
        config.save.assert_called_once_with()
        owner._populate_projects.assert_called_once_with()

    def test_active_sort_is_stable_below_fifo_pins(self) -> None:
        """Expanded projects rise without changing relative order in either band."""

        projects = [{"name": name} for name in ("closed", "pin", "open-2", "open-1")]
        store = Gtk.TreeStore(str, str, str, str, bool, str, bool)
        rows = {}
        for project in projects:
            row = store.append(
                None,
                [
                    project["name"],
                    "project",
                    project["name"],
                    "",
                    False,
                    project["name"],
                    False,
                ],
            )
            store.append(
                row,
                ["main", "terminal", project["name"], "main", False, "", False],
            )
            rows[project["name"]] = row
        tree = Gtk.TreeView(model=store)
        tree.expand_row(store.get_path(rows["open-2"]), False)
        tree.expand_row(store.get_path(rows["open-1"]), False)
        config = SimpleNamespace(
            data={
                "projects": projects,
                "pinned_projects": ["pin"],
                "expanded_projects": ["open-2", "open-1"],
            },
            save=MagicMock(),
        )
        owner = SimpleNamespace(
            config=config,
            project_tree=tree,
            project_store=store,
            COL_PROJECT=SlateWindow.COL_PROJECT,
            COL_ITEM=SlateWindow.COL_ITEM,
            COL_KIND=SlateWindow.COL_KIND,
            _populate_projects=MagicMock(),
            _select_tree_row=MagicMock(),
        )
        owner._ordered_top_level_projects = (
            SlateWindow._ordered_top_level_projects.__get__(owner)
        )
        owner._apply_project_order = SlateWindow._apply_project_order.__get__(owner)

        SlateWindow._on_active_projects_first(owner, MagicMock())

        self.assertEqual(
            [project["name"] for project in config.data["projects"]],
            ["pin", "open-2", "open-1", "closed"],
        )
        self.assertEqual(
            config.data["expanded_projects"], ["open-2", "open-1"]
        )

    def test_project_pin_toggle_preserves_assignment_order(self) -> None:
        """Adding and removing pins updates the FIFO list through one reorder."""

        project = {"name": "beta"}
        config = SimpleNamespace(
            data={"projects": [project], "pinned_projects": ["alpha"]}
        )
        owner = SimpleNamespace(
            config=config,
            _selected_project=MagicMock(return_value=project),
            _apply_project_order=MagicMock(),
        )

        SlateWindow._on_toggle_project_pin(owner, Gtk.MenuItem())
        self.assertEqual(config.data["pinned_projects"], ["alpha", "beta"])
        owner._apply_project_order.assert_called_once_with([project])

        owner._apply_project_order.reset_mock()
        SlateWindow._on_toggle_project_pin(owner, Gtk.MenuItem())
        self.assertEqual(config.data["pinned_projects"], ["alpha"])
        owner._apply_project_order.assert_called_once_with([project])

    def test_project_menu_separates_dangerous_removal_after_pin(self) -> None:
        """Project removal remains the final action behind a separator."""

        menu = MagicMock()
        owner = SimpleNamespace(
            config=SimpleNamespace(data={"pinned_projects": ["repo"]}),
            project_tree=MagicMock(),
            _selected_project=MagicMock(return_value={"name": "repo"}),
            _on_add_terminal=MagicMock(),
            _on_toggle_project_pin=MagicMock(),
            _on_remove_project=MagicMock(),
        )
        with patch("slate.window.Gtk.Menu", return_value=menu):
            SlateWindow._show_tree_menu(owner, "project", None)

        appended = [call.args[0] for call in menu.append.call_args_list]
        self.assertEqual(appended[0].get_label(), "Open Terminal")
        self.assertEqual(appended[1].get_label(), "Remove Pin")
        self.assertIsInstance(appended[2], Gtk.SeparatorMenuItem)
        self.assertEqual(appended[3].get_label(), "Remove Project")

    def test_browser_context_menu_exposes_manual_bell_action(self) -> None:
        """Every non-terminal project child receives the contextual bell toggle."""

        store = Gtk.TreeStore(str, str, str, str, bool, str, bool)
        parent = store.append(
            None, ["repo", "project", "repo", "", False, "repo", False]
        )
        browser = store.append(
            parent,
            ["Docs", "browser", "repo", "browser-1", False, "Docs", False],
        )
        tree = Gtk.TreeView(model=store)
        tree.expand_row(store.get_path(parent), False)
        tree.get_selection().select_iter(browser)
        menu = MagicMock()
        owner = SimpleNamespace(
            project_tree=tree,
            manual_attention=set(),
            COL_KIND=SlateWindow.COL_KIND,
            COL_PROJECT=SlateWindow.COL_PROJECT,
            COL_ITEM=SlateWindow.COL_ITEM,
            _on_close_browser=MagicMock(),
            _on_toggle_manual_attention=MagicMock(),
        )
        owner._selected_sidebar_identity = (
            SlateWindow._selected_sidebar_identity.__get__(owner)
        )

        with patch("slate.window.Gtk.Menu", return_value=menu):
            SlateWindow._show_tree_menu(owner, "browser", None)

        bell_item = menu.prepend.call_args.args[0]
        self.assertEqual(bell_item.get_label(), "Add Bell")

    def test_visible_revision_preview_ignores_repeated_same_file_request(self) -> None:
        """A second click on the current revision row must not reload its preview."""

        status = FileStatus("tracked.py", "modified")
        preview = MagicMock(current_path=status.path)
        preview.get_visible.return_value = True
        owner = SimpleNamespace(
            preview=preview,
            preview_status=status,
            _repository_scm=MagicMock(),
            _close_preview=MagicMock(),
            _update_preview_geometry=MagicMock(),
            get_focus=MagicMock(return_value=None),
            panel=SimpleNamespace(message=Gtk.TextView()),
            project_tree=MagicMock(),
            editor_workspace=MagicMock(),
        )
        owner._protect_preview_background = (
            SlateWindow._protect_preview_background.__get__(owner)
        )

        SlateWindow._preview_legacy_status(owner, status)

        preview.show_status.assert_not_called()
        owner._update_preview_geometry.assert_not_called()
        preview.grab_focus.assert_not_called()


    def test_revision_preview_preserves_sidebar_focus_when_shown(self) -> None:
        """A displayed revision preview blocks the background without grabbing focus."""

        status = FileStatus("tracked.py", "modified")
        preview = MagicMock(current_path=None)
        preview.get_visible.return_value = False
        owner = SimpleNamespace(
            preview=preview,
            _repository_scm=MagicMock(),
            _close_preview=MagicMock(),
            _update_preview_geometry=MagicMock(),
            get_focus=MagicMock(return_value=None),
            panel=SimpleNamespace(message=Gtk.TextView()),
            project_tree=MagicMock(),
            editor_workspace=MagicMock(),
        )
        owner._protect_preview_background = (
            SlateWindow._protect_preview_background.__get__(owner)
        )

        SlateWindow._preview_legacy_status(owner, status)

        preview.show_status.assert_called_once()
        preview.grab_focus.assert_not_called()
        owner.project_tree.set_sensitive.assert_called_once_with(False)
        owner.editor_workspace.set_sensitive.assert_called_once_with(False)


    def test_revision_preview_does_not_redirect_focused_text_input(self) -> None:
        """Showing preview content never redirects an already focused text input."""

        status = FileStatus("tracked.py", "modified")
        message = Gtk.TextView()
        preview = MagicMock(current_path=None)
        preview.get_visible.return_value = False
        owner = SimpleNamespace(
            preview=preview,
            panel=SimpleNamespace(message=message),
            project_tree=MagicMock(),
            editor_workspace=MagicMock(),
            get_focus=MagicMock(return_value=message),
            _repository_scm=MagicMock(),
            _close_preview=MagicMock(),
            _update_preview_geometry=MagicMock(),
        )
        owner._protect_preview_background = (
            SlateWindow._protect_preview_background.__get__(owner)
        )

        SlateWindow._preview_legacy_status(owner, status)

        preview.grab_focus.assert_not_called()


    def test_preview_disables_covered_widgets_until_it_closes(self) -> None:
        """GTK cannot deliver keys to the covered editor or sidebar."""

        project_tree = Gtk.TreeView()
        editor_workspace = Gtk.Box()
        preview = MagicMock()
        owner = SimpleNamespace(
            project_tree=project_tree,
            editor_workspace=editor_workspace,
            preview=preview,
            panel=SimpleNamespace(message=Gtk.TextView(), _cancel_pending_preview=MagicMock()),
            get_focus=MagicMock(return_value=Gtk.TextView()),
            preview_status=FileStatus("tracked.py", "modified"),
        )

        SlateWindow._protect_preview_background(owner)

        self.assertFalse(project_tree.get_sensitive())
        self.assertFalse(editor_workspace.get_sensitive())
        preview.grab_focus.assert_not_called()

        SlateWindow._close_preview(owner)

        self.assertTrue(project_tree.get_sensitive())
        self.assertTrue(editor_workspace.get_sensitive())

    def test_revision_refresh_does_not_steal_commit_message_focus(self) -> None:
        """An automatic preview reload leaves the commit text editor usable."""

        status = FileStatus("tracked.py", "modified")
        preview = MagicMock(current_path=status.path)
        message = Gtk.TextView()
        owner = SimpleNamespace(
            preview=preview,
            preview_status=status,
            panel=SimpleNamespace(message=message),
            get_focus=MagicMock(return_value=message),
            project_tree=MagicMock(),
            editor_workspace=MagicMock(),
            _repository_scm=MagicMock(),
            _close_preview=MagicMock(),
            _update_preview_geometry=MagicMock(),
        )
        owner._protect_preview_background = (
            SlateWindow._protect_preview_background.__get__(owner)
        )

        SlateWindow._preview_legacy_status(owner, status, force=True)

        preview.show_status.assert_called_once()
        preview.grab_focus.assert_not_called()
        owner.project_tree.set_sensitive.assert_called_once_with(False)
        owner.editor_workspace.set_sensitive.assert_called_once_with(False)


    def test_revision_watcher_forces_current_preview_reload(self) -> None:
        """A watcher refresh reloads content even when the row identity is unchanged."""

        status = FileStatus("tracked.py", "modified")
        owner = SimpleNamespace(
            preview=MagicMock(current_path=status.path),
            preview_status=status,
            right_notebook=MagicMock(),
            _close_preview=MagicMock(),
            _preview_file=MagicMock(),
        )
        owner.preview.get_visible.return_value = True
        owner.right_notebook.get_current_page.return_value = 0

        SlateWindow._refresh_visible_preview(owner, [status])

        owner._preview_file.assert_called_once_with(status, force=True)

    def test_missing_revision_closes_preview_without_loading_content(self) -> None:
        """Selecting a physically absent path leaves Revision actions exposed."""

        status = FileStatus("missing.png", "missing")
        owner = SimpleNamespace(
            preview=MagicMock(),
            _repository_scm=MagicMock(return_value=object()),
            _close_preview=MagicMock(),
        )

        SlateWindow._preview_legacy_status(owner, status)

        owner._close_preview.assert_called_once_with()
        owner.preview.show_status.assert_not_called()


    def test_shared_preview_routes_revision_and_file_actions(self) -> None:
        """The shared preview resolves shortcuts from its originating surface."""

        status = FileStatus("tracked.py", "modified")
        owner = SimpleNamespace(
            preview=SimpleNamespace(
                current_path="tracked.py",
                diff_edit_lines=[],
                edit_line_at_cursor=MagicMock(return_value=None),
            ),
            preview_status=status,
            _view_status=MagicMock(),
            _edit_status_internal=MagicMock(),
            _edit_status_external=MagicMock(),
            _delete_status=MagicMock(),
            _open_diff=MagicMock(),
            _view_project_file=MagicMock(),
            _edit_project_file_internal=MagicMock(),
            _edit_project_file_external=MagicMock(),
            _delete_project_file=MagicMock(),
            _open_search_result_meld=MagicMock(),
        )
        for action in ("view", "edit-internal", "edit-external", "delete", "meld"):
            SlateWindow._handle_file_preview_action(owner, action)
        owner._view_status.assert_called_once_with(status)
        owner._edit_status_internal.assert_called_once_with(status)
        owner._edit_status_external.assert_not_called()
        owner._delete_status.assert_called_once_with(status)
        owner._open_diff.assert_called_once_with(
            RepositoryRef(".", "hg"), ("tracked.py",)
        )

        owner.preview_status = None
        for action in ("view", "edit-internal", "edit-external", "delete", "meld"):
            SlateWindow._handle_file_preview_action(owner, action)
        owner._view_project_file.assert_called_once_with("tracked.py")
        owner._edit_project_file_internal.assert_called_once_with("tracked.py")
        owner._edit_project_file_external.assert_not_called()
        owner._delete_project_file.assert_called_once_with("tracked.py")
        owner._open_search_result_meld.assert_called_once_with("tracked.py")

    def test_preview_contextual_shortcuts_target_its_own_file(self) -> None:
        """Revision checkbox/add and Files rename/refresh use preview origin."""

        status = FileStatus("tracked.py", "modified")
        panel = SimpleNamespace(
            checked_paths=set(),
            _status_key=MagicMock(return_value="tracked.py"),
            _set_operation_checkboxes=MagicMock(),
        )
        owner = SimpleNamespace(
            preview=SimpleNamespace(current_path="tracked.py"),
            preview_status=status,
            panel=panel,
            _scan_active_project=MagicMock(),
            _add_statuses=MagicMock(),
            _rename_project_file=MagicMock(),
            file_manager=SimpleNamespace(refresh=MagicMock()),
        )

        SlateWindow._handle_file_preview_action(owner, "toggle-selection")
        SlateWindow._handle_file_preview_action(owner, "refresh")
        panel._set_operation_checkboxes.assert_called_once_with([status], True)
        owner._scan_active_project.assert_called_once_with()

        owner.preview_status = FileStatus("new.py", "untracked")
        SlateWindow._handle_file_preview_action(owner, "add")
        owner._add_statuses.assert_called_once_with([owner.preview_status])

        owner.preview_status = None
        SlateWindow._handle_file_preview_action(owner, "rename")
        SlateWindow._handle_file_preview_action(owner, "refresh")
        owner._rename_project_file.assert_called_once_with("tracked.py")
        owner.file_manager.refresh.assert_called_once_with()

    def test_revision_preview_edit_forwards_the_mapped_diff_line(self) -> None:
        """Editing a diff row opens its persistent editor at the current line."""

        status = FileStatus("tracked.py", "modified")
        owner = SimpleNamespace(
            preview=SimpleNamespace(
                current_path="tracked.py",
                diff_edit_lines=[37],
                edit_line_at_cursor=MagicMock(return_value=37),
            ),
            preview_status=status,
            _edit_status_internal=MagicMock(),
        )

        SlateWindow._handle_file_preview_action(owner, "edit-internal")

        owner._edit_status_internal.assert_called_once_with(status, 37)



    def test_extra_command_closes_popover_and_creates_named_terminal(self) -> None:
        """Configured command buttons preserve their shell line, label and icon."""

        popover = MagicMock()
        owner = SimpleNamespace(_create_terminal=MagicMock())
        SlateWindow._on_extra_command(
            owner,
            MagicMock(),
            "journalctl -f",
            "Logs",
            "text-x-script",
            popover,
        )
        popover.popdown.assert_called_once_with()
        owner._create_terminal.assert_called_once_with(
            "journalctl -f",
            name_prefix="Logs",
            icon_name="text-x-script",
        )

    def test_extra_command_arrow_is_hidden_for_zero_and_closed_for_one(self) -> None:
        """The optional HeaderBar control appears only when it has a launcher."""

        menu_button = Gtk.MenuButton()
        owner = SimpleNamespace(extra_commands_button=menu_button)
        owner._on_extra_command = SlateWindow._on_extra_command.__get__(owner)

        SlateWindow._set_extra_commands(owner, [])
        self.assertFalse(menu_button.get_visible())
        SlateWindow._set_extra_commands(
            owner,
            [
                {
                    "label": "Logs",
                    "command": "journalctl -f",
                    "icon": "text-x-script",
                }
            ],
        )

        self.assertTrue(menu_button.get_visible())
        self.assertFalse(menu_button.get_popover().get_visible())
        self.assertEqual(
            len(menu_button.get_popover().get_child().get_children()),
            1,
        )

    def test_ctrl_shift_f_precedes_contextual_editor_shortcuts(self) -> None:
        """Global project search consumes its accelerator before child managers."""

        owner = SimpleNamespace(
            _show_project_search=MagicMock(),
            get_focus=MagicMock(return_value=None),
            panel=SimpleNamespace(message=Gtk.TextView()),
        )
        event = SimpleNamespace(
            keyval=Gdk.KEY_f,
            state=(
                Gdk.ModifierType.CONTROL_MASK
                | Gdk.ModifierType.SHIFT_MASK
            ),
        )
        self.assertTrue(SlateWindow._on_key_press(owner, None, event))
        owner._show_project_search.assert_called_once_with()


    def test_visible_preview_receives_keys_before_hidden_editor(self) -> None:
        """Window dispatch cannot let contextual keys reach the covered editor."""

        owner = SimpleNamespace(
            project_search=MagicMock(),
            preview=MagicMock(),
            browser_manager=MagicMock(),
            editor_workspace=MagicMock(),
            panel=SimpleNamespace(message=Gtk.TextView()),
        )
        owner.get_focus = MagicMock(return_value=owner.preview)
        owner.project_search.get_visible.return_value = False
        owner.preview.get_visible.return_value = True
        owner.preview.handle_key.return_value = True
        event = SimpleNamespace(keyval=Gdk.KEY_e, state=Gdk.ModifierType(0))

        self.assertTrue(SlateWindow._on_key_press(owner, None, event))

        owner.preview.handle_key.assert_called_once_with(event)
        owner.browser_manager.handle_key.assert_not_called()
        owner.editor_workspace.handle_key.assert_not_called()

    def test_preview_receives_e_even_if_covered_editor_retains_focus(self) -> None:
        """A covered editor cannot receive E while a revision preview is open."""

        editor = Gtk.TextView()
        workspace = Gtk.Box()
        workspace.add(editor)
        workspace.handle_key = MagicMock()
        owner = SimpleNamespace(
            panel=Gtk.Box(),
            get_focus=MagicMock(return_value=editor),
            project_search=MagicMock(),
            preview=MagicMock(),
            browser_manager=MagicMock(),
            editor_workspace=workspace,
        )
        owner.panel.message = Gtk.TextView()
        owner.project_search.get_visible.return_value = False
        owner.preview.get_visible.return_value = True
        owner.preview.is_ancestor.return_value = False
        owner.preview.handle_key.return_value = True
        event = SimpleNamespace(keyval=Gdk.KEY_e, state=Gdk.ModifierType(0))

        self.assertTrue(SlateWindow._on_key_press(owner, None, event))

        owner.preview.handle_key.assert_called_once_with(event)
        owner.editor_workspace.handle_key.assert_not_called()

    def test_preview_owns_e_over_covered_editor_search_entry(self) -> None:
        """A stale search-entry focus in the hidden editor cannot swallow E."""

        editor_workspace = Gtk.Box()
        entry = Gtk.Entry()
        editor_workspace.add(entry)
        owner = SimpleNamespace(
            panel=Gtk.Box(),
            get_focus=MagicMock(return_value=entry),
            project_tree=Gtk.TreeView(),
            project_search=MagicMock(),
            preview=MagicMock(),
            browser_manager=MagicMock(),
            editor_workspace=editor_workspace,
        )
        owner.panel.message = Gtk.TextView()
        owner.project_search.get_visible.return_value = False
        owner.preview.get_visible.return_value = True
        owner.preview.is_ancestor.return_value = False
        owner.preview.handle_key.return_value = True
        event = SimpleNamespace(keyval=Gdk.KEY_e, state=Gdk.ModifierType(0))

        self.assertTrue(SlateWindow._on_key_press(owner, None, event))

        owner.preview.handle_key.assert_called_once_with(event)

    def test_revision_preview_e_opens_editor_and_cancels_pending_preview(self) -> None:
        """An explicit revision Edit promotes through the unified opening boundary."""

        editor = MagicMock()
        owner = SimpleNamespace(_workspace_repository_path=MagicMock(return_value="file.py"),
                                _edit_project_file_internal=MagicMock(return_value=editor))
        self.assertIs(SlateWindow._edit_status_internal(owner, FileStatus("file.py", "modified")), editor)
        owner._edit_project_file_internal.assert_called_once_with("file.py")


    def test_commit_message_keys_are_never_dispatched_to_preview(self) -> None:
        """Typing in the commit text view bypasses preview and global shortcuts."""

        message = Gtk.TextView()
        owner = SimpleNamespace(
            panel=SimpleNamespace(message=message),
            get_focus=MagicMock(return_value=message),
            preview=MagicMock(),
            editor_workspace=MagicMock(),
            browser_manager=MagicMock(),
            _show_project_search=MagicMock(),
        )
        owner.preview.get_visible.return_value = True

        for keyval in (Gdk.KEY_e, Gdk.KEY_a, Gdk.KEY_space, Gdk.KEY_F5):
            event = SimpleNamespace(keyval=keyval, state=Gdk.ModifierType(0))
            self.assertFalse(SlateWindow._on_key_press(owner, None, event))

        owner.preview.handle_key.assert_not_called()
        owner.editor_workspace.handle_key.assert_not_called()
        owner.browser_manager.handle_key.assert_not_called()
        owner._show_project_search.assert_not_called()

    def test_sidebar_shortcuts_never_reach_visible_central_managers(self) -> None:
        """Files F5 and sidebar Ctrl+S cannot reload a browser or save an editor."""

        files = Gtk.Box()
        tree = Gtk.TreeView()
        files.add(tree)
        panel = Gtk.Box()
        panel.message = Gtk.TextView()
        owner = SimpleNamespace(
            panel=panel, file_manager=files, preview=MagicMock(),
            project_search=MagicMock(), editor_workspace=Gtk.Box(),
            browser_manager=MagicMock(), get_focus=MagicMock(return_value=tree),
        )
        owner.editor_workspace.handle_key = MagicMock(return_value=True)
        owner.project_search.get_visible.return_value = False
        owner.preview.get_visible.return_value = False
        for keyval, state in (
            (Gdk.KEY_F5, Gdk.ModifierType(0)),
            (Gdk.KEY_s, Gdk.ModifierType.CONTROL_MASK),
        ):
            event = SimpleNamespace(keyval=keyval, state=state)
            self.assertFalse(SlateWindow._on_key_press(owner, None, event))
        owner.browser_manager.handle_key.assert_not_called()
        owner.editor_workspace.handle_key.assert_not_called()

    def test_preview_e_opens_file_on_headers_end_and_mapped_rows(self) -> None:
        """Real diff content supports file opening independently of cursor mapping."""

        preview = FilePreview(MagicMock())
        preview.current_path = "tracked.py"
        preview._set_diff_text(
            "diff --git a/tracked.py b/tracked.py\n"
            "--- a/tracked.py\n+++ b/tracked.py\n@@ -9 +9 @@\n-old\n+new\n"
        )
        status = FileStatus("tracked.py", "modified")
        owner = SimpleNamespace(
            preview=preview, preview_status=status,
            _edit_status_internal=MagicMock(),
        )
        preview.on_action = SlateWindow._handle_file_preview_action.__get__(owner)
        event = SimpleNamespace(keyval=Gdk.KEY_e, state=Gdk.ModifierType(0))
        for row, mapped_line in ((None, None), (0, None), (5, 9)):
            with self.subTest(row=row):
                if row is not None:
                    preview.buffer.place_cursor(preview.buffer.get_iter_at_line(row))
                owner._edit_status_internal.reset_mock()
                self.assertTrue(preview.handle_key(event))
                if mapped_line is None:
                    owner._edit_status_internal.assert_called_once_with(status)
                else:
                    owner._edit_status_internal.assert_called_once_with(status, mapped_line)

    def test_f2_renames_selected_terminal_before_contextual_handlers(self) -> None:
        """F2 opens terminal rename even when the preview is visible."""

        owner = SimpleNamespace(
            panel=SimpleNamespace(message=Gtk.TextView()),
            get_focus=MagicMock(return_value=None),
            project_search=MagicMock(),
            preview=MagicMock(),
            editor_workspace=MagicMock(),
            browser_manager=MagicMock(),
            _selected_terminal=MagicMock(return_value=({"name": "repo"}, "main")),
            _prompt_terminal_rename=MagicMock(),
        )
        owner.project_search.get_visible.return_value = False
        event = SimpleNamespace(keyval=Gdk.KEY_F2, state=Gdk.ModifierType(0))

        self.assertTrue(SlateWindow._on_key_press(owner, None, event))

        owner._prompt_terminal_rename.assert_called_once_with(send_codex_rename=True)
        owner.preview.handle_key.assert_not_called()
        owner.editor_workspace.handle_key.assert_not_called()
        owner.browser_manager.handle_key.assert_not_called()

    def test_f2_does_not_rename_from_commit_input_or_without_terminal(self) -> None:
        """F2 respects text input and requires an explicit terminal selection."""

        message = Gtk.TextView()
        owner = SimpleNamespace(
            panel=SimpleNamespace(message=message),
            get_focus=MagicMock(return_value=message),
            project_search=MagicMock(),
            _selected_terminal=MagicMock(return_value=({"name": "repo"}, "main")),
            _prompt_terminal_rename=MagicMock(),
        )
        owner.project_search.get_visible.return_value = False
        event = SimpleNamespace(keyval=Gdk.KEY_F2, state=Gdk.ModifierType(0))

        self.assertFalse(SlateWindow._on_key_press(owner, None, event))
        owner._prompt_terminal_rename.assert_not_called()

        owner.get_focus.return_value = None
        owner._selected_terminal.return_value = None
        self.assertFalse(SlateWindow._on_key_press(owner, None, event))
        owner._prompt_terminal_rename.assert_not_called()

    def test_visible_preview_does_not_override_focused_tree_shortcuts(self) -> None:
        """A visible overlay handles keys only while it owns keyboard focus."""

        tree = Gtk.TreeView()
        panel = Gtk.Box()
        panel.message = Gtk.TextView()
        panel.add(tree)
        owner = SimpleNamespace(
            panel=panel,
            get_focus=MagicMock(return_value=tree),
            project_search=MagicMock(),
            preview=MagicMock(),
            browser_manager=MagicMock(),
            editor_workspace=MagicMock(),
        )
        owner.project_search.get_visible.return_value = False
        owner.preview.get_visible.return_value = True
        owner.preview.is_ancestor.return_value = False
        event = SimpleNamespace(keyval=Gdk.KEY_e, state=Gdk.ModifierType(0))

        self.assertFalse(SlateWindow._on_key_press(owner, None, event))

        owner.preview.handle_key.assert_not_called()
        owner.browser_manager.handle_key.assert_not_called()
        owner.editor_workspace.handle_key.assert_not_called()

    def test_global_search_is_raised_above_later_diff_overlay(self) -> None:
        """Opening project search restores it to the top GtkOverlay position."""

        project = {"name": "repo", "path": "/tmp/repo"}
        owner = SimpleNamespace(
            search_available=True,
            active_project_name="repo",
            config=SimpleNamespace(find_project=MagicMock(return_value=project)),
            project_search=MagicMock(),
            content_overlay=MagicMock(),
            _close_preview=MagicMock(),
        )
        owner.project_search.get_visible.return_value = False
        owner._raise_project_search = SlateWindow._raise_project_search.__get__(owner)

        SlateWindow._show_project_search(owner)

        owner.content_overlay.reorder_overlay.assert_called_once_with(
            owner.project_search, -1
        )
        owner.project_search.open.assert_called_once_with("repo", "/tmp/repo")

    def test_missing_ripgrep_blocks_only_project_search(self) -> None:
        """An unavailable optional search tool reports locally without startup impact."""

        owner = SimpleNamespace(
            search_available=False,
            _show_error=MagicMock(),
        )
        SlateWindow._show_project_search(owner)
        owner._show_error.assert_called_once_with(
            "Project search is unavailable: install ripgrep (rg) to enable it."
        )

    def test_search_result_uses_most_specific_repository_snapshot(self) -> None:
        """A nested result cannot accidentally resolve to its ancestor status."""

        root_repository = RepositoryRef(".", "git")
        nested_repository = RepositoryRef("nested", "hg")
        root_status = FileStatus(
            "nested/file.py", "modified", repository=".", scm_type="git"
        )
        nested_status = FileStatus(
            "file.py", "modified", repository="nested", scm_type="hg"
        )
        owner = SimpleNamespace(
            active_project_name="workspace",
            repositories_by_project={
                "workspace": {root_repository, nested_repository}
            },
            snapshots={
                ("workspace", root_repository): ([root_status], "main"),
                ("workspace", nested_repository): ([nested_status], "default"),
            },
            _repository_path_length=SlateWindow._repository_path_length,
        )
        self.assertEqual(
            SlateWindow._search_result_status(owner, "nested/file.py"),
            (nested_repository, nested_status),
        )

    def test_search_meld_dispatches_only_eligible_tracked_status(self) -> None:
        """Search launches Meld with move endpoints but rejects added files."""

        repository = RepositoryRef(".", "git")
        moved = FileStatus(
            "new.py",
            "moved",
            repository=".",
            source_path="old.py",
            scm_type="git",
        )
        owner = SimpleNamespace(
            _search_result_status=MagicMock(return_value=(repository, moved)),
            _repository_scm=MagicMock(return_value=object()),
            _open_diff=MagicMock(),
        )
        self.assertTrue(
            SlateWindow._can_open_search_result_meld(owner, "new.py")
        )
        SlateWindow._open_search_result_meld(owner, "new.py")
        owner._open_diff.assert_called_once_with(repository, ("old.py", "new.py"))

        added = FileStatus("new.py", "added", repository=".", scm_type="git")
        owner._search_result_status.return_value = (repository, added)
        self.assertFalse(
            SlateWindow._can_open_search_result_meld(owner, "new.py")
        )
        SlateWindow._open_search_result_meld(owner, "new.py")
        owner._open_diff.assert_called_once_with(repository, ("old.py", "new.py"))

    def test_missing_definition_falls_back_to_project_text_search(self) -> None:
        """An unresolved symbol opens the reusable Find surface with that text."""

        owner = SimpleNamespace(
            active_project_name="workspace",
            definition_search=object(),
            project_search=MagicMock(),
            _close_preview=MagicMock(),
            _raise_project_search=MagicMock(),
            _show_error=MagicMock(),
        )
        completion = SimpleNamespace(error="")

        SlateWindow._on_definition_search_complete(
            owner,
            "workspace",
            "/tmp/workspace",
            "src/app.py",
            "go",
            (),
            completion,
        )

        self.assertIsNone(owner.definition_search)
        owner._close_preview.assert_called_once_with()
        owner._raise_project_search.assert_called_once_with()
        owner.project_search.search_for.assert_called_once_with(
            "workspace", "/tmp/workspace", "go"
        )
        owner._show_error.assert_not_called()

    def test_search_result_promotes_without_a_separate_diff_surface(self) -> None:
        """Search results navigate the shared document without an obsolete save boundary."""

        editor = MagicMock()
        owner = SimpleNamespace(_edit_project_file_internal=MagicMock(return_value=editor))
        self.assertIs(SlateWindow._edit_search_result_internal(owner, "other.py", 12), editor)
        owner._edit_project_file_internal.assert_called_once_with("other.py", 12)


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

    def test_editor_click_consumes_event_and_reactivates_selected_row(self) -> None:
        """Editor clicks prevent GTK from reclaiming focus, including repeated clicks."""

        for already_selected in (False, True):
            with self.subTest(already_selected=already_selected):
                tree = MagicMock()
                path = Gtk.TreePath.new_from_indices([0])
                tree.get_path_at_pos.return_value = (path, object(), 0, 0)
                selection = tree.get_selection.return_value
                selection.path_is_selected.return_value = already_selected
                owner = SimpleNamespace(
                    project_store=MagicMock(),
                    COL_KIND=SlateWindow.COL_KIND,
                    COL_PROJECT=SlateWindow.COL_PROJECT,
                    COL_ITEM=SlateWindow.COL_ITEM,
                    _clear_manual_attention=MagicMock(),
                    _on_tree_selection=MagicMock(),
                )
                owner.project_store.get_value.return_value = "editor"
                event = SimpleNamespace(
                    button=1, state=Gdk.ModifierType(0),
                    type=Gdk.EventType.BUTTON_PRESS, x=24, y=12,
                )

                self.assertTrue(SlateWindow._on_tree_button(owner, tree, event))
                selection.select_path.assert_called_once_with(path)
                if already_selected:
                    owner._on_tree_selection.assert_called_once_with(selection)
                else:
                    owner._on_tree_selection.assert_not_called()

    def test_selected_workspace_row_click_reactivates_covered_content(self) -> None:
        """Clicking a selected terminal, browser or project restores its content."""

        for kind in ("terminal", "browser", "project"):
            with self.subTest(kind=kind):
                tree = MagicMock()
                path = Gtk.TreePath.new_from_indices([0])
                tree.get_path_at_pos.return_value = (path, object(), 0, 0)
                selection = tree.get_selection.return_value
                selection.path_is_selected.return_value = True
                owner = SimpleNamespace(
                    project_store=MagicMock(),
                    project_expander_column=object(),
                    COL_KIND=SlateWindow.COL_KIND,
                    COL_PROJECT=SlateWindow.COL_PROJECT,
                    COL_ITEM=SlateWindow.COL_ITEM,
                    _clear_manual_attention=MagicMock(),
                    _on_tree_selection=MagicMock(),
                )
                owner.project_store.get_value.return_value = kind
                event = SimpleNamespace(
                    button=1, state=Gdk.ModifierType(0),
                    type=Gdk.EventType.BUTTON_PRESS, x=24, y=12,
                )

                self.assertFalse(SlateWindow._on_tree_button(owner, tree, event))
                selection.select_path.assert_called_once_with(path)
                owner._on_tree_selection.assert_called_once_with(selection)

    def test_editor_row_double_click_reveals_it_in_files(self) -> None:
        """Double-clicking a sidebar editor switches to Files and locates it."""

        store = Gtk.TreeStore(str, str, str, str, bool, str, bool)
        tree_iter = store.append(
            None,
            ["app.py", "editor", "project", "src/app.py", False, "", False],
        )
        path = store.get_path(tree_iter)
        tree = MagicMock()
        tree.get_path_at_pos.return_value = (path, object(), 0, 0)
        tree.get_selection.return_value.path_is_selected.return_value = False
        owner = SimpleNamespace(
            project_drag_candidate=None,
            project_store=store,
            project_expander_column=object(),
            right_notebook=MagicMock(),
            file_manager=MagicMock(),
            _clear_manual_attention=MagicMock(),
            COL_KIND=SlateWindow.COL_KIND,
            COL_PROJECT=SlateWindow.COL_PROJECT,
            COL_ITEM=SlateWindow.COL_ITEM,
        )
        event = SimpleNamespace(
            button=1,
            state=Gdk.ModifierType(0),
            type=Gdk.EventType.DOUBLE_BUTTON_PRESS,
            x=24,
            y=12,
        )

        self.assertTrue(SlateWindow._on_tree_button(owner, tree, event))
        owner.right_notebook.set_current_page.assert_called_once_with(1)
        owner.file_manager.reveal_path.assert_called_once_with("src/app.py")
        owner._clear_manual_attention.assert_called_once()

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
            inactive_watcher_eviction_ids={},
            evicted_watcher_projects=set(),
            _close_preview=MagicMock(),
            _configure_file_manager=MagicMock(),
            _set_revision_count=MagicMock(),
            _ensure_project_repositories=MagicMock(return_value=[]),
            _update_active_revision_count=MagicMock(),
            _queue_unattended_project_verification=MagicMock(),
            _cancel_inactive_watcher_eviction=MagicMock(),
            _schedule_inactive_watcher_eviction=MagicMock(),
            _debug_watcher_lifecycle=MagicMock(),
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
        owner._schedule_inactive_watcher_eviction.assert_called_once_with("first")
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

        owner = SimpleNamespace(changes_tab=Gtk.Label(label="Revisions"))
        SlateWindow._set_revision_count(owner, 7)
        self.assertEqual(owner.changes_tab.get_text(), "Revisions (7)")
        SlateWindow._set_revision_count(owner, 0)
        self.assertEqual(owner.changes_tab.get_text(), "Revisions")

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

    def test_blocking_an_inactive_project_suspends_only_its_observers(self) -> None:
        """An overload never terminates terminals or suspends another project's watcher."""

        root = RepositoryRef(".", "hg")
        large = MagicMock()
        other = MagicMock()
        discovery = MagicMock()
        owner = SimpleNamespace(
            active_project_name="other", panel=MagicMock(),
            watchers={("large", root): large, ("other", root): other},
            discovery_by_project={"large": discovery},
            _remove_unattended_repository_verification=MagicMock(),
        )
        SlateWindow._block_project_revisions(owner, "large")
        large.suspend_for_limit.assert_called_once_with()
        other.suspend_for_limit.assert_not_called()
        discovery.cancel.assert_called_once_with()
        self.assertNotIn("large", owner.discovery_by_project)
        owner.panel.set_project_blocked.assert_called_once_with("large", True)

    def test_blocked_revision_preview_refuses_actions_but_files_still_work(self) -> None:
        """Old preview shortcuts cannot mutate a blocked revision target."""

        gate = ProjectStatusLimit(MagicMock(), MagicMock())
        gate.blocked.add("large")
        owner = SimpleNamespace(
            project_limit=gate, active_project_name="large",
            preview=SimpleNamespace(current_path="file.txt"),
            preview_status=FileStatus("file.txt", "untracked"),
            _delete_status=MagicMock(), _scan_active_project=MagicMock(),
            _edit_project_file_internal=MagicMock(),
        )
        SlateWindow._handle_file_preview_action(owner, "delete")
        owner._delete_status.assert_not_called()
        SlateWindow._handle_file_preview_action(owner, "refresh")
        owner._scan_active_project.assert_called_once_with()
        owner.preview_status = None
        SlateWindow._handle_file_preview_action(owner, "edit-internal")
        owner._edit_project_file_internal.assert_called_once_with("file.txt")

    def test_status_reuses_bases_and_history_invalidates_only_owning_repository(self) -> None:
        """Only visible owning documents are configured; hidden bases invalidate without I/O."""

        project = {"name": "repo", "path": "/tmp/repo"}
        documents = []
        for root, mapped in (("/tmp/repo", True), ("/tmp/repo", False), ("/tmp/repo/child", True)):
            document = SimpleNamespace(
                project_name="repo", experimental=SimpleNamespace(repository_root=root, base_identity=(root, "file")),
                get_mapped=MagicMock(return_value=mapped),
            )
            documents.append(document)
        historical = SimpleNamespace(
            project_name="repo", history_status=FileStatus("deleted.txt", "removed"),
            experimental=SimpleNamespace(repository_root="/tmp/repo", base_identity=("/tmp/repo", "deleted.txt")),
            get_mapped=MagicMock(return_value=True),
        )
        owner = SimpleNamespace(
            scm_by_repository={}, watchers={}, snapshots={}, repositories_by_project={},
            project_limit=ProjectStatusLimit(MagicMock(), MagicMock()), ignored_by_repository={},
            editor_workspace=SimpleNamespace(editors={
                str(index): SimpleNamespace(document=document) for index, document in enumerate(documents)
            }, preview_entry=None, historical_document=historical),
            active_project_name="repo", panel=MagicMock(), file_manager=MagicMock(),
            _repository_root=MagicMock(return_value="/tmp/repo"),
            _update_repository_boundaries=MagicMock(), _status_owned_by_repository=MagicMock(return_value=True),
            _update_active_revision_count=MagicMock(), _refresh_visible_preview=MagicMock(),
            _configure_experimental=MagicMock(),
        )
        with patch("slate.window.RepoWatcher") as watchers:
            SlateWindow._attach_repository(owner, project, RepositoryRef(".", "hg"))
            status_changed = watchers.call_args.args[2]
            history_changed = watchers.call_args.args[6]
        original = [document.experimental.base_identity for document in documents]
        owner.panel.clear_error.reset_mock()
        status_changed([FileStatus("file", "modified")], "main")
        self.assertEqual(owner._configure_experimental.call_args_list, [
            call(documents[0]), call(historical, historical.history_status)])
        self.assertEqual([document.experimental.base_identity for document in documents], original)
        owner.panel.clear_error.assert_not_called()
        owner._configure_experimental.reset_mock()
        history_changed()
        owner._configure_experimental.assert_not_called()
        self.assertIsNone(documents[0].experimental.base_identity)
        self.assertIsNone(documents[1].experimental.base_identity)
        self.assertEqual(documents[2].experimental.base_identity, original[2])

        self.assertIsNone(historical.experimental.base_identity)
        status_changed([FileStatus("file", "modified")], "main")
        self.assertEqual(owner._configure_experimental.call_args_list, [
            call(documents[0]), call(historical, historical.history_status)])

    def test_repository_errors_preserve_other_scopes_and_projects(self) -> None:
        """Repository recovery clears the panel only when all active-project reads recovered."""

        first = SimpleNamespace(read_errors={"ignored": "ignore failure"})
        second = SimpleNamespace(read_errors={"branch": "branch failure"})
        other = SimpleNamespace(read_errors={"status": "other project failure"})
        owner = SimpleNamespace(panel=MagicMock(), watchers={
            ("repo", RepositoryRef(".", "hg")): first,
            ("repo", RepositoryRef("nested", "git")): second,
            ("other", RepositoryRef(".", "git")): other,
        })
        SlateWindow._refresh_repository_errors(owner, "repo")
        message = owner.panel.show_error.call_args.args[0]
        self.assertIn("ignore failure", message)
        self.assertIn("branch failure", message)
        self.assertNotIn("other project failure", message)
        first.read_errors.clear()
        SlateWindow._refresh_repository_errors(owner, "repo")
        self.assertIn("branch failure", owner.panel.show_error.call_args.args[0])
        owner.panel.clear_error.assert_not_called()
        second.read_errors.clear()
        SlateWindow._refresh_repository_errors(owner, "repo")
        owner.panel.clear_error.assert_called_once_with()

    def test_rejected_rename_can_be_cancelled_or_restored_to_original_name(self) -> None:
        """A rejected destination cannot survive Cancel, window closure or the original name."""

        for response in (Gtk.ResponseType.CANCEL, Gtk.ResponseType.DELETE_EVENT, Gtk.ResponseType.OK):
            with self.subTest(response=response), tempfile.TemporaryDirectory(dir="/tmp") as root:
                source = Path(root) / "old.txt"
                destination = Path(root) / "existing.txt"
                source.write_text("original")
                destination.write_text("existing")
                owner = SimpleNamespace(
                    active_project_name="project",
                    _project_file_path=MagicMock(side_effect=[str(source), str(destination)]),
                    editor_workspace=MagicMock(), project_file_operations=MagicMock(),
                    _show_file_error=MagicMock(), _on_project_entry_renamed=MagicMock(),
                )
                with patch("slate.window.Gtk.Dialog") as dialog, patch("slate.window.Gtk.Entry") as entry, patch(
                        "slate.window.Gtk.Label"):
                    dialog.return_value.run.side_effect = (Gtk.ResponseType.OK, response)
                    entry.return_value.get_text.side_effect = ("existing.txt", "old.txt")
                    SlateWindow._rename_project_file(owner, "old.txt")
                    dialog.return_value.destroy.assert_called_once_with()
                owner.project_file_operations.rename_entry.assert_not_called()
                owner.editor_workspace.can_relocate_path.assert_not_called()
                owner.editor_workspace.set_rename_pending.assert_not_called()
                self.assertEqual(source.read_text(), "original")
                self.assertEqual(destination.read_text(), "existing")

    def test_rejected_rename_accepts_a_corrected_destination(self) -> None:
        """Retrying after a collision renames only to the newly accepted destination."""

        with tempfile.TemporaryDirectory(dir="/tmp") as root:
            source = Path(root) / "old.txt"
            existing = Path(root) / "existing.txt"
            destination = Path(root) / "new.txt"
            source.write_text("original")
            existing.write_text("existing")
            owner = SimpleNamespace(
                active_project_name="project",
                _project_file_path=MagicMock(side_effect=[str(source), str(existing), str(destination)]),
                editor_workspace=MagicMock(), project_file_operations=MagicMock(),
                _show_file_error=MagicMock(), _on_project_entry_renamed=MagicMock(),
            )
            with patch("slate.window.Gtk.Dialog") as dialog, patch("slate.window.Gtk.Entry") as entry, patch(
                    "slate.window.Gtk.Label"):
                dialog.return_value.run.side_effect = (Gtk.ResponseType.OK, Gtk.ResponseType.OK)
                entry.return_value.get_text.side_effect = ("existing.txt", "new.txt")
                SlateWindow._rename_project_file(owner, "old.txt")
            owner.project_file_operations.rename_entry.assert_called_once()
            self.assertEqual(owner.project_file_operations.rename_entry.call_args.args[:2],
                             (str(source), str(destination)))
            owner.editor_workspace.set_rename_pending.assert_called_once_with("project", "old.txt", True)

    def test_rename_does_not_start_while_an_affected_editor_is_saving(self) -> None:
        """The UI gate rejects a rename before launching any filesystem mutation."""

        with tempfile.TemporaryDirectory(dir="/tmp") as root:
            source = Path(root) / "old.txt"
            source.write_text("original")
            owner = SimpleNamespace(
                active_project_name="project", _project_file_path=MagicMock(side_effect=[str(source), str(Path(root) / "new.txt")]),
                editor_workspace=MagicMock(), project_file_operations=MagicMock(),
                _show_file_error=MagicMock(), _on_project_entry_renamed=MagicMock(),
            )
            owner.editor_workspace.set_rename_pending.return_value = False
            with patch("slate.window.Gtk.Dialog") as dialog, patch("slate.window.Gtk.Entry") as entry, patch(
                    "slate.window.Gtk.Label"):
                dialog.return_value.run.return_value = Gtk.ResponseType.OK
                entry.return_value.get_text.return_value = "new.txt"
                SlateWindow._rename_project_file(owner, "old.txt")
            owner.editor_workspace.set_rename_pending.assert_called_once_with("project", "old.txt", True)
            owner.project_file_operations.rename_entry.assert_not_called()
            owner._show_file_error.assert_called_once()
            self.assertEqual(source.read_text(), "original")

    def test_project_limit_checks_watcher_snapshots_before_touching_gtk(self) -> None:
        """Watcher callbacks from multiple repositories block before any oversized UI update."""

        block = MagicMock()
        project = {"name": "repo", "path": "/tmp/repo"}
        owner = SimpleNamespace(
            scm_by_repository={}, watchers={}, snapshots={}, repositories_by_project={},
            project_limit=ProjectStatusLimit(block, MagicMock()), ignored_by_repository={},
            editor_workspace=SimpleNamespace(editors={}, preview_entry=None),
            active_project_name="repo", panel=MagicMock(), file_manager=MagicMock(),
            _repository_root=MagicMock(return_value="/tmp/repo"),
            _update_repository_boundaries=MagicMock(), _status_owned_by_repository=MagicMock(return_value=True),
            _update_active_revision_count=MagicMock(), _refresh_visible_preview=MagicMock(),
        )
        with patch("slate.window.RepoWatcher") as watchers:
            SlateWindow._attach_repository(owner, project, RepositoryRef(".", "hg"))
            root_callback = watchers.call_args.args[2]
            SlateWindow._attach_repository(owner, project, RepositoryRef("child", "git"))
            child_callback = watchers.call_args.args[2]
        root_callback([FileStatus(f"root-{index}", "untracked") for index in range(600)], "main")
        self.assertEqual(owner.panel.update_status.call_count, 1)
        child_callback([FileStatus(f"child-{index}", "modified") for index in range(400)], "main")
        block.assert_called_once_with("repo")
        self.assertEqual(owner.panel.update_status.call_count, 1)
        child_callback([], "main")
        self.assertEqual(owner.panel.update_status.call_count, 1)

    def test_repository_watcher_is_created_once_per_discovered_root(self) -> None:
        """Repeated discovery reuses the repository adapter and watcher."""

        project = {"name": "repo", "path": "/tmp/repo"}
        owner = SimpleNamespace(
            scm_by_repository={},
            watchers={},
            project_limit=ProjectStatusLimit(MagicMock(), MagicMock()),
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
            project_limit=ProjectStatusLimit(MagicMock(), MagicMock()),
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

    def test_evicted_repository_reuses_adapter_and_recreates_watcher(self) -> None:
        """Reactivation remounts one watcher without replacing cached SCM state."""

        project = {"name": "repo", "path": "/tmp/repo"}
        repository = RepositoryRef(".", "hg")
        scm = MagicMock()
        owner = SimpleNamespace(
            scm_by_repository={("repo", repository): scm},
            watchers={},
            project_limit=ProjectStatusLimit(MagicMock(), MagicMock()),
            snapshots={},
            repositories_by_project={"repo": {repository}},
            ignored_by_repository={},
            active_project_name="repo",
            panel=MagicMock(),
            file_manager=MagicMock(),
            _repository_root=MagicMock(return_value="/tmp/repo"),
            _update_repository_boundaries=MagicMock(),
        )

        with patch("slate.window.RepoWatcher") as watcher_class:
            SlateWindow._attach_repository(owner, project, repository)

        self.assertIs(owner.scm_by_repository[("repo", repository)], scm)
        self.assertIs(
            owner.watchers[("repo", repository)], watcher_class.return_value
        )
        self.assertIs(watcher_class.call_args.args[1], scm)
        watcher_class.return_value.set_active.assert_called_once_with(True)

    def test_inactive_watcher_cleanup_is_scheduled_only_once(self) -> None:
        """One project switch owns one one-hour eviction source, never a poll."""

        owner = SimpleNamespace(
            inactive_watcher_eviction_ids={},
            INACTIVE_WATCHER_EVICTION_MS=SlateWindow.INACTIVE_WATCHER_EVICTION_MS,
            _evict_inactive_project_watchers=MagicMock(),
            _debug_watcher_lifecycle=MagicMock(),
        )
        with patch("slate.window.GLib.timeout_add", return_value=73) as timeout_add:
            SlateWindow._schedule_inactive_watcher_eviction(owner, "repo")
            SlateWindow._schedule_inactive_watcher_eviction(owner, "repo")

        timeout_add.assert_called_once_with(
            60 * 60 * 1000,
            owner._evict_inactive_project_watchers,
            "repo",
        )
        self.assertEqual(owner.inactive_watcher_eviction_ids, {"repo": 73})

    def test_reactivation_cancels_pending_watcher_cleanup(self) -> None:
        """Returning before one hour preserves the existing watcher instances."""

        owner = SimpleNamespace(
            inactive_watcher_eviction_ids={"repo": 73},
            _debug_watcher_lifecycle=MagicMock(),
        )
        with patch("slate.window.GLib.source_remove") as source_remove:
            SlateWindow._cancel_inactive_watcher_eviction(owner, "repo")

        source_remove.assert_called_once_with(73)
        self.assertEqual(owner.inactive_watcher_eviction_ids, {})

    def test_elapsed_inactive_cleanup_closes_only_project_watchers(self) -> None:
        """Eviction retains adapters and snapshots while releasing inotify monitors."""

        first = MagicMock()
        second = MagicMock()
        other = MagicMock()
        owner = SimpleNamespace(
            inactive_watcher_eviction_ids={"repo": 73},
            closing=False,
            active_project_name="other",
            watchers={
                ("repo", RepositoryRef(".", "hg")): first,
                ("repo", RepositoryRef("nested", "git")): second,
                ("other", RepositoryRef(".", "git")): other,
            },
            scm_by_repository={"retained": MagicMock()},
            snapshots={"retained": MagicMock()},
            evicted_watcher_projects=set(),
            _debug_watcher_lifecycle=MagicMock(),
        )

        result = SlateWindow._evict_inactive_project_watchers(owner, "repo")

        self.assertFalse(result)
        first.close.assert_called_once_with()
        second.close.assert_called_once_with()
        other.close.assert_not_called()
        self.assertEqual(list(owner.watchers), [("other", RepositoryRef(".", "git"))])
        self.assertIn("retained", owner.scm_by_repository)
        self.assertIn("retained", owner.snapshots)
        self.assertEqual(owner.evicted_watcher_projects, {"repo"})

    def test_watcher_cleanup_debug_is_silent_without_debug_flag(self) -> None:
        """Lifecycle messages appear only in an explicitly foreground debug run."""

        diagnostics = io.StringIO()
        with patch.dict("slate.window.os.environ", {}, clear=True), patch(
            "slate.window.sys.stderr", diagnostics
        ):
            SlateWindow._debug_watcher_lifecycle("repo", "hidden")
            with patch.dict("slate.window.os.environ", {"SLATE_DEBUG": "1"}):
                SlateWindow._debug_watcher_lifecycle("repo", "visible")

        self.assertNotIn("hidden", diagnostics.getvalue())
        self.assertIn("[SLATE watcher] project=repo visible", diagnostics.getvalue())

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

    def test_unattended_verification_is_lazy_once_and_scan_can_force_it(self) -> None:
        """Only the active initialized project runs one check until manual Scan."""

        repository = RepositoryRef(".", "git")
        key = ("repo", repository)
        owner = SimpleNamespace(
            active_project_name="repo",
            discovery_by_project={},
            verified_projects=set(),
            unattended_verification_queue=[],
            unattended_verifier=None,
            unattended_verifier_key=None,
            repositories_by_project={"repo": {repository}},
            scm_by_repository={key: GitSCM("/tmp/repo")},
            watchers={key: MagicMock()},
            panel=MagicMock(),
            _sorted_repositories=SlateWindow._sorted_repositories,
        )
        owner._start_next_unattended_verification = (
            SlateWindow._start_next_unattended_verification.__get__(owner)
        )
        owner._on_unattended_verification_closed = (
            SlateWindow._on_unattended_verification_closed.__get__(owner)
        )
        with patch("slate.window.UnattendedRepositoryVerifier") as verifier_type:
            SlateWindow._queue_unattended_project_verification(owner, "repo")
            verifier_type.assert_called_once()
            verifier_type.return_value.start.assert_called_once_with()
            verified = verifier_type.call_args.args[1]
            closed = verifier_type.call_args.args[2]
            verified(RepositorySyncStatus("synced"))
            owner.panel.set_project_remote_status.assert_called_once_with(
                "repo", repository, RepositorySyncStatus("synced")
            )
            closed()
            SlateWindow._queue_unattended_project_verification(owner, "repo")
            self.assertEqual(verifier_type.call_count, 1)
            SlateWindow._queue_unattended_project_verification(
                owner, "repo", force=True
            )
            self.assertEqual(verifier_type.call_count, 2)

    def test_inactive_project_never_queues_unattended_verification(self) -> None:
        """Configured projects remain network-lazy until explicitly activated."""

        owner = SimpleNamespace(
            active_project_name="other",
            discovery_by_project={},
            verified_projects=set(),
            unattended_verification_queue=[],
            unattended_verifier=None,
            unattended_verifier_key=None,
            repositories_by_project={"repo": {RepositoryRef(".", "git")}},
            _start_next_unattended_verification=MagicMock(),
        )
        SlateWindow._queue_unattended_project_verification(owner, "repo")
        self.assertEqual(owner.unattended_verification_queue, [])
        owner._start_next_unattended_verification.assert_not_called()

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
            manual_attention=set(),
            editor_workspace=SimpleNamespace(editors={}, preview_entry=None),
            _ordered_project_items=SlateWindow._ordered_project_items.__get__(
                SimpleNamespace(editor_workspace=SimpleNamespace(editors={}))
            ),
        )
        SlateWindow._populate_projects(owner)
        terminals.add.assert_not_called()
        self.assertEqual(owner.project_store.iter_n_children(None), 2)
        project_iter = owner.project_store.get_iter_first()
        self.assertEqual(
            owner.project_store.get_value(project_iter, SlateWindow.COL_TEXT),
            "repo (3)",
        )
        spacer_iter = owner.project_store.iter_next(project_iter)
        self.assertEqual(
            owner.project_store.get_value(spacer_iter, SlateWindow.COL_KIND),
            "spacer",
        )

    def test_manual_bell_supports_browser_rows_and_is_consumed_on_activation(self) -> None:
        """Non-terminal children can receive and consume the shared bell icon."""

        store = Gtk.TreeStore(str, str, str, str, bool, str, bool)
        parent = store.append(
            None, ["repo", "project", "repo", "", False, "repo", False]
        )
        browser = store.append(
            parent,
            ["Docs", "browser", "repo", "browser-1", False, "Docs", False],
        )
        tree = Gtk.TreeView(model=store)
        tree.expand_row(store.get_path(parent), False)
        tree.get_selection().select_iter(browser)
        owner = SimpleNamespace(
            project_tree=tree,
            project_store=store,
            manual_attention=set(),
            attention_terminals=set(),
            terminals=SimpleNamespace(terminals={}),
            editor_workspace=SimpleNamespace(editors={}, preview_entry=None),
            COL_KIND=SlateWindow.COL_KIND,
            COL_PROJECT=SlateWindow.COL_PROJECT,
            COL_ITEM=SlateWindow.COL_ITEM,
            COL_ATTENTION=SlateWindow.COL_ATTENTION,
        )
        owner._selected_sidebar_identity = (
            SlateWindow._selected_sidebar_identity.__get__(owner)
        )
        owner._find_sidebar_iter = SlateWindow._find_sidebar_iter.__get__(owner)
        owner._native_row_attention = SlateWindow._native_row_attention.__get__(owner)

        SlateWindow._on_toggle_manual_attention(owner, Gtk.MenuItem())

        identity = ("browser", "repo", "browser-1")
        self.assertEqual(owner.manual_attention, {identity})
        self.assertTrue(store.get_value(browser, SlateWindow.COL_ATTENTION))

        SlateWindow._clear_manual_attention(owner, identity, browser)

        self.assertEqual(owner.manual_attention, set())
        self.assertFalse(store.get_value(browser, SlateWindow.COL_ATTENTION))

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
            editor_workspace=SimpleNamespace(editors={}, preview_entry=None),
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
            editor_workspace=SimpleNamespace(editors={}, preview_entry=None),
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

    def test_close_to_tray_setting_updates_icon_and_persists(self) -> None:
        """The general switch synchronizes the tray icon and global config."""

        config = SimpleNamespace(
            data={"settings": {"general": {"close_to_tray": False}}},
            save=MagicMock(),
        )
        owner = SimpleNamespace(config=config, _update_tray_icon=MagicMock())

        SlateWindow._update_close_to_tray_setting(owner, True)

        self.assertTrue(config.data["settings"]["general"]["close_to_tray"])
        owner._update_tray_icon.assert_called_once_with(True)
        config.save.assert_called_once_with()

    def test_window_close_hides_to_tray_when_enabled(self) -> None:
        """Closing an opted-in window hides it without starting shutdown."""

        owner = SimpleNamespace(
            config=SimpleNamespace(
                data={"settings": {"general": {"close_to_tray": True}}}
            ),
            terminals=MagicMock(),
            hide=MagicMock(),
            _save_window_state=MagicMock(),
            _request_close=MagicMock(),
        )

        self.assertTrue(SlateWindow._on_delete_event(owner, MagicMock(), MagicMock()))

        owner._save_window_state.assert_called_once_with()
        owner.terminals.set_activity_monitoring.assert_called_once_with(False)
        owner.hide.assert_called_once_with()
        owner._request_close.assert_not_called()



    def test_definition_line_is_applied_after_final_editor_activation(self) -> None:
        """Definition navigation promotes first and positions the resulting shared buffer."""

        editor = MagicMock()
        owner = SimpleNamespace(_open_experimental_file=MagicMock(return_value=editor))
        self.assertIs(SlateWindow._edit_project_file_internal(owner, "target.py", 37), editor)
        owner._open_experimental_file.assert_called_once_with("target.py", focus=True)
        editor.goto_line_number.assert_called_once_with(37, focus=False)


    def test_symlink_alias_selects_existing_editor_row(self) -> None:
        """Alias promotion goes through the workspace's shared physical-file lookup."""

        editor = MagicMock(relative_path="src/file.php")
        owner = SimpleNamespace(_open_experimental_file=MagicMock(return_value=editor))
        result = SlateWindow._edit_project_file_internal(owner, "alias.php")
        self.assertIs(result, editor)
        owner._open_experimental_file.assert_called_once_with("alias.php", focus=True)


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

    def test_restart_protects_dirty_files_and_preserves_every_tmux_session(self) -> None:
        """Restart never queries, retires or terminates sessions and honors editor cancellation."""

        for approved in (False, True):
            with self.subTest(approved=approved):
                application = SimpleNamespace(restart_requested=False)
                owner = SimpleNamespace(
                    closing=False, editor_workspace=MagicMock(), terminals=MagicMock(),
                    _after_restart_editors=MagicMock(), _save_window_state=MagicMock(),
                    _final_destroy=MagicMock(), get_application=MagicMock(return_value=application),
                )
                SlateWindow._on_restart(owner, None)
                self.assertTrue(owner.closing)
                owner.editor_workspace.request_close_all.assert_called_once_with(owner._after_restart_editors)
                SlateWindow._after_restart_editors(owner, approved)
                self.assertEqual(application.restart_requested, approved)
                self.assertEqual(owner.closing, approved)
                self.assertEqual(owner._save_window_state.call_count, int(approved))
                self.assertEqual(owner._final_destroy.call_count, int(approved))
                self.assertEqual(owner.terminals.begin_shutdown.call_count, int(approved))
                owner.terminals.query_panes.assert_not_called()
                owner.terminals.retire_idle_shells.assert_not_called()
                owner.terminals.kill_session.assert_not_called()

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

    def test_codex_close_respects_background_cancel_and_terminate_choices(self) -> None:
        """Codex without a red dot still requires an explicit shutdown choice."""

        panes = [PaneInfo("repo--codex", "node", 123, agent_display="Codex"),
                 PaneInfo("repo--main", "bash", 124)]
        for response in (1, 2, Gtk.ResponseType.CANCEL):
            with self.subTest(response=response):
                owner = SimpleNamespace(
                    closing=True, terminals=MagicMock(), set_sensitive=MagicMock(),
                    config=SimpleNamespace(data={"projects": []}),
                    _terminate_all=MagicMock(), _save_window_state=MagicMock(),
                    _final_destroy=MagicMock(), is_active=MagicMock(return_value=True),
                    _format_duration=MagicMock(return_value="unknown"),
                )
                with patch("slate.window.Gtk.MessageDialog") as dialog_class:
                    dialog = dialog_class.return_value
                    dialog.run.return_value = response
                    SlateWindow._decide_close(owner, panes)
                dialog.run.assert_called_once_with()
                details = dialog.format_secondary_text.call_args.args[0]
                self.assertIn("repo--codex (node", details)
                self.assertNotIn("repo--codex (shell at prompt)", details)
                if response == 1:
                    owner._save_window_state.assert_called_once_with()
                    owner.terminals.begin_shutdown.assert_called_once_with()
                    owner.terminals.retire_idle_shells.assert_called_once_with([], owner._final_destroy)
                    owner._final_destroy.assert_not_called()
                    owner.terminals.retire_idle_shells.call_args.args[1]()
                    owner._final_destroy.assert_called_once_with()
                    owner._terminate_all.assert_not_called()
                elif response == 2:
                    owner._terminate_all.assert_called_once_with(panes)
                    owner._final_destroy.assert_not_called()
                else:
                    self.assertFalse(owner.closing)
                    owner._terminate_all.assert_not_called()
                    owner._final_destroy.assert_not_called()
                owner.terminals.kill_server.assert_not_called()
                owner.terminals.kill_session.assert_not_called()

    def test_idle_shells_still_offer_explicit_background_choice(self) -> None:
        """Shell-only sessions must not silently take the terminate-all path."""

        owner = SimpleNamespace(
            closing=True, terminals=MagicMock(), _terminate_all=MagicMock(),
            _save_window_state=MagicMock(), _final_destroy=MagicMock(),
            is_active=MagicMock(return_value=True),
        )
        with patch("slate.window.Gtk.MessageDialog") as dialog_class:
            dialog_class.return_value.run.return_value = Gtk.ResponseType.CANCEL
            SlateWindow._decide_close(owner, [PaneInfo("repo--main", "bash", 100)])
        dialog_class.return_value.run.assert_called_once_with()
        owner._terminate_all.assert_not_called()
        owner._final_destroy.assert_not_called()

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
            unattended_verification_queue=[],
            _repository_scm=MagicMock(return_value=scm),
            _repository_watcher=MagicMock(return_value=watcher),
            _close_preview=MagicMock(),
            _start_next_unattended_verification=MagicMock(),
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
        watcher.request_full.assert_called_once_with(refresh_base=True)
        watcher.request_paths.assert_not_called()
        self.assertEqual(
            owner.unattended_verification_queue,
            [("repo", RepositoryRef(".", "hg"))],
        )
        owner._start_next_unattended_verification.assert_called_once_with()
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
            unattended_verification_queue=[],
            _repository_scm=MagicMock(side_effect=scms.get),
            _repository_watcher=MagicMock(return_value=None),
            _close_preview=MagicMock(),
            _start_next_unattended_verification=MagicMock(),
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
        self.assertEqual(
            owner.unattended_verification_queue,
            [("workspace", RepositoryRef("one", "hg"))],
        )
        owner._start_next_unattended_verification.assert_called_once_with()
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

    def test_scm_mutation_sequence_keeps_original_targets_after_project_switch(self) -> None:
        """A pending sequence never resolves its next adapter from the new active project."""

        repositories = (RepositoryRef(".", "hg"), RepositoryRef("nested", "git"))
        adapters = [SimpleNamespace(root=f"/tmp/A/{repository.path}", environment={})
                    for repository in repositories]
        watchers = [MagicMock(), MagicMock()]
        owner = SimpleNamespace(
            active_project_name="A", project_limit=None, panel=MagicMock(),
            _repository_scm=MagicMock(side_effect=adapters),
            _repository_watcher=MagicMock(side_effect=watchers),
            _close_preview=MagicMock(), watchers={},
            _close_project_path_preview=MagicMock(),
            _workspace_repository_path=SlateWindow._workspace_repository_path,
        )
        operations = [(repository, ["scm", "operation"], ["file"]) for repository in repositories]
        with patch("slate.window.run_async") as run:
            SlateWindow._run_scm_mutations(owner, operations, "test", full_refresh=True)
            self.assertEqual(owner._repository_scm.call_count, 2)
            self.assertEqual(run.call_count, 1)
            first = run.call_args.args[1]
            owner.active_project_name = "B"
            first(CommandResult((), 0, "", ""))
            self.assertEqual(run.call_count, 2)
            self.assertEqual(run.call_args.kwargs["cwd"], "/tmp/A/nested")
            run.call_args.args[1](CommandResult((), 0, "", ""))
        for watcher in watchers:
            watcher.request_full.assert_called_once_with()
        owner.panel.clear_error.assert_not_called()
        self.assertEqual(owner._repository_scm.call_count, 2)

    def test_scm_mutation_does_not_close_an_unrelated_preview(self) -> None:
        """A delayed Revision removal cannot hide another file selected in the same project."""

        owner = SimpleNamespace(
            active_project_name="A", project_limit=None, preview_status=None,
            preview=SimpleNamespace(current_path="other.png"), _close_preview=MagicMock(),
            _workspace_repository_path=SlateWindow._workspace_repository_path,
            _repository_scm=MagicMock(return_value=SimpleNamespace(root="/tmp/A", environment={})),
            _repository_watcher=MagicMock(return_value=None), panel=MagicMock(),
        )
        owner._close_project_path_preview = SlateWindow._close_project_path_preview.__get__(owner)
        with patch("slate.window.run_async"):
            SlateWindow._run_scm_mutations(
                owner, [(RepositoryRef(".", "git"), ["git", "operation"], ["deleted.txt"])], "test")
        owner._close_preview.assert_not_called()

    def test_file_preferences_flush_updates_the_originating_project(self) -> None:
        """A delayed expansion flush cannot write A's expanded paths into active project B."""

        first, second = {"name": "A"}, {"name": "B"}
        projects = {"A": first, "B": second}
        owner = SimpleNamespace(active_project_name="B", config=SimpleNamespace(
            find_project=projects.get, save=MagicMock()))
        preferences = {"expanded_paths": ["src"]}
        SlateWindow._save_file_manager_preferences(owner, "A", preferences)
        self.assertEqual(first["file_manager"], preferences)
        self.assertNotIn("file_manager", second)
        owner.config.save.assert_called_once_with()

    def test_file_creation_completion_refreshes_only_the_originating_project(self) -> None:
        """Finishing a creation in A while viewing B refreshes only A's SCM state."""

        owner = SimpleNamespace(active_project_name="B", file_manager=MagicMock(project_name="B"),
                                _queue_project_status_paths=MagicMock(), _show_file_error=MagicMock())
        owner._refresh_project_file_views = SlateWindow._refresh_project_file_views.__get__(owner)
        SlateWindow._on_project_entry_created(owner, "A", "new.txt", None)
        owner.file_manager.refresh.assert_not_called()
        owner._queue_project_status_paths.assert_called_once_with("A", ("new.txt",))

    def test_deletion_and_rename_completion_preserve_another_projects_preview(self) -> None:
        """Completed Files mutations in A neither refresh nor hide B's current preview."""

        owner = SimpleNamespace(
            active_project_name="B", preview_status=None,
            preview=SimpleNamespace(current_path="image.png"), _close_preview=MagicMock(),
            file_manager=MagicMock(project_name="B"), editor_workspace=MagicMock(),
            config=SimpleNamespace(find_project=MagicMock(return_value=None)),
            _queue_project_status_paths=MagicMock(), _show_file_error=MagicMock(),
        )
        owner._refresh_project_file_views = SlateWindow._refresh_project_file_views.__get__(owner)
        owner._close_project_path_preview = SlateWindow._close_project_path_preview.__get__(owner)
        SlateWindow._on_project_entry_deleted(owner, "A", "image.png", None)
        SlateWindow._on_project_entry_renamed(owner, "A", "image.png", "renamed.png", None)
        owner._close_preview.assert_not_called()
        owner.file_manager.refresh.assert_not_called()
        owner.editor_workspace.relocate_path.assert_called_once_with("A", "image.png", "renamed.png")
        self.assertEqual(owner._queue_project_status_paths.call_args_list, [
            call("A", ("image.png",)), call("A", ("image.png", "renamed.png"))])

    def test_revision_deletion_records_removal_in_originating_project(self) -> None:
        """The SCM phase after asynchronous disk deletion retains A even while B is active."""

        repository = RepositoryRef(".", "hg")
        original = SimpleNamespace(record_removal_argv=MagicMock(return_value=["hg", "remove", "--after", "file"]))
        other = SimpleNamespace(record_removal_argv=MagicMock())
        owner = SimpleNamespace(active_project_name="B", panel=MagicMock(),
                                scm_by_repository={("A", repository): original, ("B", repository): other},
                                _refresh_project_file_views=MagicMock(), _close_project_path_preview=MagicMock(),
                                _run_scm_mutations=MagicMock())
        SlateWindow._on_status_entry_deleted(owner, "A", FileStatus("file", "modified"), "file", None)
        original.record_removal_argv.assert_called_once_with(["file"])
        other.record_removal_argv.assert_not_called()
        self.assertEqual(owner._run_scm_mutations.call_args.kwargs, {"project_name": "A"})
        owner._refresh_project_file_views.assert_called_once_with("A", ("file",))

    def test_directory_inspection_cannot_prompt_after_switching_project(self) -> None:
        """A late inspection cannot authorize deletion under another project's visible context."""

        owner = SimpleNamespace(active_project_name="B", _confirm_scm_paths=MagicMock(),
                                project_file_operations=MagicMock(), _show_file_error=MagicMock())
        SlateWindow._on_project_directory_inspected(owner, "A", "folder", "/tmp/A/folder",
                                                    DirectoryInspection((), False), None)
        owner._confirm_scm_paths.assert_not_called()
        owner.project_file_operations.delete_flat_directory.assert_not_called()

    def test_completed_mutation_preserves_unrelated_preview(self) -> None:
        """Only previews of the affected path within the originating project are closed."""

        owner = SimpleNamespace(active_project_name="B", preview_status=None,
                                preview=SimpleNamespace(current_path="folder/image.png"),
                                _close_preview=MagicMock())
        SlateWindow._close_project_path_preview(owner, "A", "folder")
        SlateWindow._close_project_path_preview(owner, "B", "unrelated")
        owner._close_preview.assert_not_called()
        SlateWindow._close_project_path_preview(owner, "B", "folder")
        owner._close_preview.assert_called_once_with()

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
            owner.scm_by_repository = {("project", RepositoryRef(".", "hg")): scm}
            owner.file_manager.project_name = "project"
            owner._refresh_project_file_views = SlateWindow._refresh_project_file_views.__get__(owner)
            owner._close_project_path_preview = MagicMock()
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
                project_name="project",
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
        owner.scm_by_repository = {("project", RepositoryRef(".", "hg")): scm}
        owner._refresh_project_file_views = MagicMock()
        owner._close_project_path_preview = MagicMock()
        SlateWindow._delete_status(
            owner, FileStatus("missing.py", "removed")
        )
        owner.project_file_operations.delete_entry.assert_not_called()
        scm.record_removal_argv.assert_called_once_with(["missing.py"])
        owner._run_scm_mutations.assert_called_once()

    def test_multiple_missing_paths_are_confirmed_and_recorded_together(self) -> None:
        """Batch deletion groups absent paths into one confirmed SCM operation."""

        scm = SimpleNamespace(
            display_name="Mercurial",
            record_removal_argv=MagicMock(return_value=["hg", "remove", "--after"]),
        )

        def workspace_path(_repository: str, path: str) -> str:
            """Return root-repository paths unchanged for this batch test."""

            return path

        owner = SimpleNamespace(
            panel=MagicMock(),
            _workspace_repository_path=MagicMock(side_effect=workspace_path),
            _repository_scm=MagicMock(return_value=scm),
            _confirm_scm_paths=MagicMock(return_value=True),
            _run_scm_mutations=MagicMock(),
        )
        statuses = [
            FileStatus("first.py", "missing"),
            FileStatus("second.py", "missing"),
        ]

        SlateWindow._delete_missing_statuses(owner, statuses)

        scm.record_removal_argv.assert_called_once_with(["first.py", "second.py"])
        owner._confirm_scm_paths.assert_called_once()
        owner._run_scm_mutations.assert_called_once_with(
            [
                (
                    RepositoryRef(".", "hg"),
                    ["hg", "remove", "--after"],
                    ["first.py", "second.py"],
                )
            ],
            "Record removal",
        )

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
                    ["git", "rm", "--cached", "-f", "--", ":(top,literal)added.py"],
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

    def test_extra_command_icon_is_persisted_on_created_terminal(self) -> None:
        """A launcher icon becomes terminal state independent from the launcher."""

        project = {
            "name": "repo",
            "path": "/tmp/repo",
            "terminals": [],
            "terminal_commands": {},
            "terminal_icons": {},
            "last_terminal": None,
        }
        owner = SimpleNamespace(
            config=SimpleNamespace(data={"active_terminal": None}, save=MagicMock()),
            terminals=MagicMock(),
            _selected_project=MagicMock(return_value=project),
            _append_project_item=SlateWindow._append_project_item,
            _populate_projects=MagicMock(),
            _select_tree_row=MagicMock(),
        )

        SlateWindow._create_terminal(
            owner,
            "journalctl -f",
            name_prefix="Logs",
            icon_name="text-x-script",
        )

        self.assertEqual(project["terminal_icons"], {"logs-1": "text-x-script"})

    def test_terminal_rename_completion_preserves_newer_project_or_view_selection(self) -> None:
        """Renaming A keeps B or an editor visible, while an unchanged terminal selection follows its new name."""

        for view in ("other-project", "other-terminal", "editor", "unchanged"):
            with self.subTest(view=view):
                project = {"name": "A", "terminals": ["main", "logs"], "last_terminal": "main"}
                page = object()
                workspace = SimpleNamespace(terminal_page=page, get_visible_child=MagicMock(return_value=page))
                owner = SimpleNamespace(
                    active_project_name="A", editor_workspace=workspace,
                    _selected_terminal=MagicMock(return_value=(project, "main")),
                    _validate_terminal_name=MagicMock(return_value=None), _show_error=MagicMock(),
                    terminals=MagicMock(), config=SimpleNamespace(data={"active_terminal": "A/main"}, save=MagicMock()),
                    _populate_projects=MagicMock(), _select_tree_row=MagicMock(),
                    preview=MagicMock(), project_search=MagicMock())
                owner.preview.get_visible.return_value = False
                owner.project_search.get_visible.return_value = False
                with patch("slate.window.Gtk.Dialog") as dialog, patch("slate.window.Gtk.Entry") as entry:
                    dialog.return_value.run.return_value = Gtk.ResponseType.OK
                    entry.return_value.get_text.return_value = "renamed"
                    SlateWindow._prompt_terminal_rename(owner, send_codex_rename=True)
                callback = owner.terminals.rename.call_args.args[3]
                if view == "other-project":
                    owner.active_project_name = "B"
                    owner.config.data["active_terminal"] = "B/main"
                    owner._selected_terminal.return_value = ({"name": "B"}, "main")
                elif view == "other-terminal":
                    owner.config.data["active_terminal"] = "A/logs"
                    owner._selected_terminal.return_value = (project, "logs")
                elif view == "editor":
                    owner._selected_terminal.return_value = None
                    workspace.get_visible_child.return_value = object()
                callback(True)
                self.assertEqual(project["terminals"], ["renamed", "logs"])
                self.assertEqual(project["last_terminal"], "renamed")
                expected = "B/main" if view == "other-project" else "A/logs" if view == "other-terminal" else "A/renamed"
                self.assertEqual(owner.config.data["active_terminal"], expected)
                if view == "unchanged":
                    owner._select_tree_row.assert_called_once_with("A", "renamed")
                else:
                    owner._select_tree_row.assert_not_called()
                owner.terminals.request_codex_rename.assert_called_once_with("A", "renamed")

    def test_terminal_rename_transfers_persisted_icon(self) -> None:
        """Renaming a session preserves the icon attached to its old identity."""

        project = {
            "name": "repo",
            "terminals": ["logs-1"],
            "terminal_commands": {"logs-1": "journalctl -f"},
            "terminal_icons": {"logs-1": "text-x-script"},
            "terminal_history_ids": {"logs-1": "a" * 32},
            "item_order": [{"kind": "terminal", "value": "logs-1"}],
            "last_terminal": "logs-1",
        }
        dialog = MagicMock()
        dialog.run.return_value = Gtk.ResponseType.OK
        entry = MagicMock()
        entry.get_text.return_value = "system-logs"
        terminals = MagicMock()
        owner = SimpleNamespace(
            active_project_name="repo",
            _selected_terminal=MagicMock(return_value=(project, "logs-1")),
            _validate_terminal_name=MagicMock(return_value=None),
            _show_error=MagicMock(),
            terminals=terminals,
            config=SimpleNamespace(data={"active_terminal": None}, save=MagicMock()),
            _populate_projects=MagicMock(),
            _select_tree_row=MagicMock(),
        )
        with patch("slate.window.Gtk.Dialog", return_value=dialog), patch(
            "slate.window.Gtk.Entry", return_value=entry
        ):
            SlateWindow._prompt_terminal_rename(owner)
        renamed = terminals.rename.call_args.args[3]

        renamed(True)

        self.assertEqual(project["terminal_icons"], {"system-logs": "text-x-script"})
        self.assertEqual(project["terminal_history_ids"], {"system-logs": "a" * 32})
        self.assertEqual(
            project["terminal_commands"],
            {"system-logs": "journalctl -f"},
        )
        terminals.request_codex_rename.assert_not_called()

    def test_f2_requests_codex_rename_after_terminal_rename_succeeds(self) -> None:
        """The Codex slash command follows only a successful F2 rename."""

        project = {
            "name": "repo",
            "terminals": ["main"],
            "terminal_commands": {"main": "codex resume"},
            "terminal_icons": {},
            "item_order": [{"kind": "terminal", "value": "main"}],
            "last_terminal": "main",
        }
        dialog = MagicMock()
        dialog.run.return_value = Gtk.ResponseType.OK
        entry = MagicMock()
        entry.get_text.return_value = "work"
        terminals = MagicMock()
        owner = SimpleNamespace(
            active_project_name="repo",
            _selected_terminal=MagicMock(return_value=(project, "main")),
            _validate_terminal_name=MagicMock(return_value=None),
            _show_error=MagicMock(),
            terminals=terminals,
            config=SimpleNamespace(data={"active_terminal": None}, save=MagicMock()),
            _populate_projects=MagicMock(),
            _select_tree_row=MagicMock(),
        )
        with patch("slate.window.Gtk.Dialog", return_value=dialog), patch(
            "slate.window.Gtk.Entry", return_value=entry
        ):
            SlateWindow._prompt_terminal_rename(owner, send_codex_rename=True)
        renamed = terminals.rename.call_args.args[3]
        renamed(False)
        terminals.request_codex_rename.assert_not_called()
        renamed(True)
        terminals.request_codex_rename.assert_called_once_with("repo", "work")

    def test_resume_codex_uses_native_bell_per_invocation(self) -> None:
        """The standard launcher isolates native BEL settings to its terminal."""

        owner = SimpleNamespace(_create_terminal=MagicMock())
        SlateWindow._on_resume_codex(owner, MagicMock())
        owner._create_terminal.assert_called_once_with(
            _CODEX_RESUME_COMMAND,
            name_prefix="codex",
        )
        self.assertTrue(
            _CODEX_RESUME_COMMAND.startswith("codex --no-alt-screen resume ")
        )
        self.assertIn("-c 'notify=[]'", _CODEX_RESUME_COMMAND)
        self.assertIn("tui.notification_method=\"bel\"", _CODEX_RESUME_COMMAND)
        self.assertIn(
            "tui.notification_condition=\"always\"",
            _CODEX_RESUME_COMMAND,
        )

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
        """A new terminal expands its project and needs no naming dialog."""

        project = {
            "name": "repo",
            "path": "/tmp/repo",
            "terminals": ["main"],
            "last_terminal": "main",
        }
        owner = SimpleNamespace(
            config=SimpleNamespace(
                data={"active_terminal": "repo/main", "expanded_projects": []},
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
        self.assertEqual(owner.config.data["expanded_projects"], ["repo"])
        owner._populate_projects.assert_called_once_with()
        owner._select_tree_row.assert_called_once_with("repo", "term-2")

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
            manual_attention=set(),
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
            codex_icon=GdkPixbuf.Pixbuf.new_from_file_at_scale(
                str(Path(__file__).parents[1] / "slate" / "codex.svg"),
                16,
                16,
                True,
            ),
            config=SimpleNamespace(
                data={"pinned_projects": ["repo"]},
                find_project=MagicMock(
                    return_value={"terminal_icons": {"main": "text-x-script"}}
                )
            ),
        )
        owner._set_project_row_background = (
            SlateWindow._set_project_row_background.__get__(owner)
        )
        renderer = Gtk.CellRendererPixbuf()
        SlateWindow._render_tree_icon(owner, MagicMock(), renderer, store, project_iter)
        self.assertIsNone(renderer.get_property("icon-name"))
        self.assertFalse(renderer.get_property("visible"))
        self.assertTrue(renderer.get_property("cell-background-set"))
        with patch("slate.window.Gtk.IconTheme.get_default") as get_theme:
            get_theme.return_value.has_icon.return_value = True
            SlateWindow._render_tree_icon(
                owner, MagicMock(), renderer, store, terminal_iter
            )
        self.assertEqual(renderer.get_property("icon-name"), "text-x-script")
        self.assertTrue(renderer.get_property("visible"))
        self.assertFalse(renderer.get_property("cell-background-set"))
        with patch("slate.window.Gtk.IconTheme.get_default") as get_theme:
            get_theme.return_value.has_icon.return_value = False
            SlateWindow._render_tree_icon(
                owner, MagicMock(), renderer, store, terminal_iter
            )
        self.assertEqual(renderer.get_property("icon-name"), "utilities-terminal")
        SlateWindow._render_tree_icon(
            owner, MagicMock(), renderer, store, private_browser_iter
        )
        self.assertIsNone(renderer.get_property("icon-name"))
        self.assertEqual(renderer.get_property("pixbuf"), incognito_icon)
        pin_renderer = Gtk.CellRendererPixbuf()
        SlateWindow._render_pin_icon(
            owner, MagicMock(), pin_renderer, store, project_iter
        )
        self.assertEqual(pin_renderer.get_property("icon-name"), "view-pin-symbolic")
        SlateWindow._render_pin_icon(
            owner, MagicMock(), pin_renderer, store, terminal_iter
        )
        self.assertIsNone(pin_renderer.get_property("icon-name"))

    def test_sidebar_indicators_do_not_reserve_space_when_absent(self) -> None:
        """A Codex bell reaches the right edge when no activity dot is shown."""

        store = Gtk.TreeStore(str, str, str, str, bool, str, bool)
        terminal = store.append(
            None, ["codex", "terminal", "repo", "codex", False, "codex", True]
        )
        owner = SimpleNamespace(
            COL_KIND=SlateWindow.COL_KIND,
            COL_PROJECT=SlateWindow.COL_PROJECT,
            COL_ACTIVITY=SlateWindow.COL_ACTIVITY,
            COL_ATTENTION=SlateWindow.COL_ATTENTION,
            config=SimpleNamespace(data={"pinned_projects": []}),
            attention_icon=GdkPixbuf.Pixbuf.new(
                GdkPixbuf.Colorspace.RGB, True, 8, 1, 1
            ),
            _set_project_row_background=MagicMock(),
        )
        activity = Gtk.CellRendererPixbuf()
        attention = Gtk.CellRendererPixbuf()
        pin = Gtk.CellRendererPixbuf()

        SlateWindow._render_activity_icon(owner, None, activity, store, terminal)
        SlateWindow._render_attention_icon(owner, None, attention, store, terminal)
        SlateWindow._render_pin_icon(owner, None, pin, store, terminal)

        self.assertFalse(activity.get_property("visible"))
        self.assertTrue(attention.get_property("visible"))
        self.assertFalse(pin.get_property("visible"))

        store.set_value(terminal, SlateWindow.COL_ACTIVITY, True)
        store.set_value(terminal, SlateWindow.COL_ATTENTION, False)
        SlateWindow._render_activity_icon(owner, None, activity, store, terminal)
        SlateWindow._render_attention_icon(owner, None, attention, store, terminal)
        self.assertTrue(activity.get_property("visible"))
        self.assertFalse(attention.get_property("visible"))

    def test_codex_terminal_uses_headerbar_icon_in_project_tree(self) -> None:
        """Persisted Codex launchers render the bundled icon, not a terminal."""

        store = Gtk.TreeStore(str, str, str, str, bool, str, bool)
        project_iter = store.append(
            None, ["repo", "project", "repo", "", False, "repo", False]
        )
        terminal_iter = store.append(
            project_iter,
            ["codex-1", "terminal", "repo", "codex-1", False, "codex-1", False],
        )
        codex_icon = GdkPixbuf.Pixbuf.new_from_file_at_scale(
            str(Path(__file__).parents[1] / "slate" / "codex.svg"),
            16,
            16,
            True,
        )
        owner = SimpleNamespace(
            COL_KIND=SlateWindow.COL_KIND,
            COL_PROJECT=SlateWindow.COL_PROJECT,
            COL_ITEM=SlateWindow.COL_ITEM,
            codex_icon=codex_icon,
            config=SimpleNamespace(
                find_project=MagicMock(
                    return_value={
                        "terminal_commands": {
                            "codex-1": "codex resume -c 'notify=[]'"
                        },
                        "terminal_icons": {},
                    }
                )
            ),
            _set_project_row_background=MagicMock(),
        )
        renderer = Gtk.CellRendererPixbuf()

        SlateWindow._render_tree_icon(
            owner, MagicMock(), renderer, store, terminal_iter
        )

        self.assertIs(renderer.get_property("pixbuf"), codex_icon)
        self.assertIsNone(renderer.get_property("icon-name"))

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

    def test_file_click_activates_an_already_open_internal_editor(self) -> None:
        """File-panel selection prefers the existing editor over quick preview."""

        project = {"name": "repo", "path": "/tmp/repo"}
        owner = SimpleNamespace(
            active_project_name="repo",
            config=SimpleNamespace(find_project=MagicMock(return_value=project)),
            editor_workspace=SimpleNamespace(
                editors={("repo", "src/app.py"): object()}
            ),
            preview=MagicMock(),
            _edit_project_file_internal=MagicMock(),
            _close_preview=MagicMock(),
            _update_preview_geometry=MagicMock(),
        )

        SlateWindow._preview_legacy_project_file(owner, "src/app.py")

        owner._edit_project_file_internal.assert_called_once_with("src/app.py")
        owner.preview.show_file.assert_not_called()


    def test_file_click_activates_editor_opened_through_another_symlink(self) -> None:
        """A file alias selects the existing editor rather than a preview."""

        project = {"name": "repo", "path": "/tmp/repo"}
        find_open = MagicMock(return_value=("repo", "src/app.py"))
        owner = SimpleNamespace(
            active_project_name="repo",
            config=SimpleNamespace(find_project=MagicMock(return_value=project)),
            editor_workspace=SimpleNamespace(
                editors={("repo", "src/app.py"): object()},
                find_open_reference=find_open,
            ),
            preview=MagicMock(),
            _edit_project_file_internal=MagicMock(),
            _close_preview=MagicMock(),
            _update_preview_geometry=MagicMock(),
        )

        SlateWindow._preview_legacy_project_file(owner, "alias.py")

        find_open.assert_called_once_with("repo", "/tmp/repo", "alias.py")
        owner._edit_project_file_internal.assert_called_once_with("alias.py")
        owner.preview.show_file.assert_not_called()


    def test_file_click_previews_a_file_without_an_editor(self) -> None:
        """Files not open in the editor retain the existing quick-preview flow."""

        project = {"name": "repo", "path": "/tmp/repo"}
        owner = SimpleNamespace(
            active_project_name="repo",
            config=SimpleNamespace(find_project=MagicMock(return_value=project)),
            editor_workspace=SimpleNamespace(editors={}, preview_entry=None),
            preview=MagicMock(),
            _edit_project_file_internal=MagicMock(),
            _close_preview=MagicMock(),
            _update_preview_geometry=MagicMock(),
            _protect_preview_background=MagicMock(),
        )

        SlateWindow._preview_legacy_project_file(owner, "src/app.py")

        owner.preview.show_file.assert_called_once_with(
            "/tmp/repo", "src/app.py"
        )
        self.assertIsNone(owner.preview_status)
        owner._edit_project_file_internal.assert_not_called()


    def test_files_preview_edit_ignores_previous_revision_status(self) -> None:
        """A standby Files preview still edits its own target instead of stale SCM state."""

        owner = SimpleNamespace(preview=SimpleNamespace(current_path="current.md"), preview_status=None,
                                _edit_project_file_internal=MagicMock(), _edit_status_internal=MagicMock())
        SlateWindow._handle_file_preview_action(owner, "edit-internal")
        owner._edit_project_file_internal.assert_called_once_with("current.md")
        owner._edit_status_internal.assert_not_called()


    def test_exited_active_terminal_selects_configured_fallback(self) -> None:
        """Removing an ended shell updates config and activates its sibling."""

        project = {
            "name": "repo",
            "terminals": ["main", "test"],
            "terminal_commands": {"main": "codex resume"},
            "terminal_icons": {"main": "system-run"},
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
            project_tree=MagicMock(), COL_PROJECT=2, COL_ITEM=3, COL_KIND=1,
            editor_workspace=MagicMock(), preview=MagicMock(), project_search=MagicMock(),
        )
        owner.project_tree.get_selection.return_value.get_selected.return_value = (None, None)
        owner.editor_workspace.get_visible_child.return_value = owner.editor_workspace.terminal_page
        owner.preview.get_visible.return_value = False
        owner.project_search.get_visible.return_value = False
        SlateWindow._remove_terminal_configuration(owner, project, "main")
        self.assertEqual(project["terminals"], ["test"])
        self.assertEqual(project["terminal_commands"], {})
        self.assertEqual(project["terminal_icons"], {})
        self.assertEqual(project["last_terminal"], "test")
        self.assertEqual(config.data["active_terminal"], "repo/test")
        config.save.assert_called_once_with()
        owner._select_tree_row.assert_called_once_with("repo", "test")

    def test_terminal_exit_preserves_selected_surface_without_activation(self) -> None:
        """Background exits preserve editor, browser and preview selections without routing keys away."""

        for kind in ("editor", "browser", "terminal"):
            for exiting_active in (False, True):
                with self.subTest(kind=kind, exiting_active=exiting_active):
                    project = {"name": "repo", "terminals": ["main", "worker"], "last_terminal": "main"}
                    model = MagicMock()
                    model.get_value.side_effect = ("repo", "selected", kind)
                    owner = SimpleNamespace(
                        config=SimpleNamespace(data={"active_terminal": "repo/main"}, save=MagicMock()),
                        project_tree=MagicMock(), COL_PROJECT=2, COL_ITEM=3, COL_KIND=1,
                        editor_workspace=MagicMock(), preview=MagicMock(), project_search=MagicMock(),
                        _populate_projects=MagicMock(), restoring_tree=False,
                    )
                    owner.project_tree.get_selection.return_value.get_selected.return_value = (model, object())
                    owner.editor_workspace.get_visible_child.return_value = (
                        owner.editor_workspace.terminal_page if kind == "terminal" else object()
                    )
                    owner.preview.get_visible.return_value = kind == "terminal"
                    owner.project_search.get_visible.return_value = False
                    restored = []

                    def selected(*identity):
                        """Record whether restoring selection suppresses activation signals."""

                        restored.append((identity, owner.restoring_tree))

                    owner._select_tree_row = selected
                    SlateWindow._remove_terminal_configuration(owner, project, "main" if exiting_active else "worker")
                    self.assertEqual(restored, [(("repo", "selected", kind), True)])
                    self.assertFalse(owner.restoring_tree)
                    self.assertEqual(owner.config.data["active_terminal"], "repo/worker" if exiting_active else "repo/main")

    def test_terminal_rename_is_single_flight_and_releases_guard_on_failure(self) -> None:
        """Repeated F2 cannot enqueue a rename for the old name while tmux is busy."""

        project = {"name": "repo", "terminals": ["main"], "last_terminal": "main"}
        owner = SimpleNamespace(
            _selected_terminal=MagicMock(return_value=(project, "main")),
            _validate_terminal_name=MagicMock(return_value=None), _show_error=MagicMock(),
            terminals=MagicMock(), config=SimpleNamespace(data={"active_terminal": "repo/main"}, save=MagicMock()),
            active_project_name="repo", editor_workspace=MagicMock(), preview=MagicMock(), project_search=MagicMock(),
            _populate_projects=MagicMock(), _select_tree_row=MagicMock(),
        )
        with patch("slate.window.Gtk.Dialog") as dialog, patch("slate.window.Gtk.Entry") as entry:
            dialog.return_value.run.return_value = Gtk.ResponseType.OK
            entry.return_value.get_text.return_value = "renamed"
            SlateWindow._prompt_terminal_rename(owner)
            SlateWindow._prompt_terminal_rename(owner)
            self.assertEqual(dialog.call_count, 1)
            owner.terminals.rename.assert_called_once()
            owner.terminals.rename.call_args.args[3](False)
            self.assertEqual(owner.pending_terminal_renames, set())
            SlateWindow._prompt_terminal_rename(owner)
            self.assertEqual(owner.terminals.rename.call_count, 2)
            owner.terminals.rename.call_args.args[3](True)
        self.assertEqual(project["terminals"], ["renamed"])
        self.assertEqual(owner.pending_terminal_renames, set())

    def test_terminal_rename_completion_ignores_removed_terminal(self) -> None:
        """Removing the terminal during rename cannot raise or resurrect its configuration."""

        project = {"name": "repo", "terminals": ["main"]}
        owner = SimpleNamespace(_selected_terminal=MagicMock(return_value=(project, "main")),
                                _validate_terminal_name=MagicMock(return_value=None), terminals=MagicMock(),
                                _show_error=MagicMock(), config=MagicMock())
        with patch("slate.window.Gtk.Dialog") as dialog, patch("slate.window.Gtk.Entry") as entry:
            dialog.return_value.run.return_value = Gtk.ResponseType.OK
            entry.return_value.get_text.return_value = "renamed"
            SlateWindow._prompt_terminal_rename(owner)
        project["terminals"].clear()
        owner.terminals.rename.call_args.args[3](True)
        self.assertEqual(project["terminals"], [])
        owner.config.save.assert_not_called()
        self.assertEqual(owner.pending_terminal_renames, set())

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
