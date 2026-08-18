"""Pure terminal naming and process-duration parser tests."""

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from gi.repository import Gdk, Vte

from slate.processes import CommandResult
from slate.terminals import TerminalManager, session_name, slug


class TerminalHelpersTest(unittest.TestCase):
    """Verify deterministic tmux identifiers and foreground age parsing."""

    def test_slug_is_normalized_and_bounded(self) -> None:
        """Names normalize exactly as required and respect component limits."""

        self.assertEqual(slug("Mio Sito!!!", 30), "mio-sito")
        self.assertEqual(len(slug("A" * 40, 30)), 30)
        self.assertEqual(session_name("Mio Sito", "Test Uno"), "mio-sito--test-uno")

    def test_foreground_duration_uses_process_group_leader(self) -> None:
        """The tty parser selects the leader of the foreground process group."""

        output = "100 10 10 20\n12 20 20 20\n11 21 20 20\n"
        self.assertEqual(TerminalManager._parse_foreground_duration(output), 12)

    def test_metadata_round_trip_is_delimiter_safe(self) -> None:
        """Orphan metadata safely preserves paths containing format delimiters."""

        value = "/tmp/a|b/à capo"
        encoded = TerminalManager._encode_metadata(value)
        self.assertNotIn("|", encoded)
        self.assertEqual(TerminalManager._decode_metadata(encoded), value)

    def test_status_labels_cannot_break_layout_or_inject_tmux_style(self) -> None:
        """Status metadata flattens whitespace, separators and style openers."""

        self.assertEqual(
            TerminalManager._status_label(" Progetto\n| #[rosso] "),
            "Progetto ¦ #［rosso]",
        )

    def test_standard_paste_shortcuts_reach_vte_clipboard(self) -> None:
        """Ctrl+Shift+V and Shift+Insert paste without entering terminal text."""

        terminal = MagicMock()
        ctrl_shift_v = SimpleNamespace(
            keyval=Gdk.KEY_v,
            state=(
                Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK
            ),
        )
        self.assertTrue(
            TerminalManager._on_terminal_key_press(terminal, ctrl_shift_v)
        )
        shift_insert = SimpleNamespace(
            keyval=Gdk.KEY_Insert,
            state=Gdk.ModifierType.SHIFT_MASK,
        )
        self.assertTrue(
            TerminalManager._on_terminal_key_press(terminal, shift_insert)
        )
        self.assertEqual(terminal.paste_clipboard.call_count, 2)

    def test_copy_shortcut_does_not_steal_plain_ctrl_c(self) -> None:
        """Only Ctrl+Shift+C copies, leaving plain Ctrl+C to the foreground process."""

        terminal = MagicMock()
        ctrl_shift_c = SimpleNamespace(
            keyval=Gdk.KEY_c,
            state=(
                Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK
            ),
        )
        self.assertTrue(
            TerminalManager._on_terminal_key_press(terminal, ctrl_shift_c)
        )
        terminal.copy_clipboard_format.assert_called_once_with(Vte.Format.TEXT)
        plain_ctrl_c = SimpleNamespace(
            keyval=Gdk.KEY_c,
            state=Gdk.ModifierType.CONTROL_MASK,
        )
        self.assertFalse(
            TerminalManager._on_terminal_key_press(terminal, plain_ctrl_c)
        )

    def test_click_opens_terminal_hyperlink_externally(self) -> None:
        """A direct click launches an OSC 8 hyperlink with the default application."""

        terminal = MagicMock()
        terminal.hyperlink_check_event.return_value = "https://example.com/docs"
        event = SimpleNamespace(
            button=1,
            state=0,
        )
        owner = SimpleNamespace(_on_terminal_uri_opened=MagicMock())
        with patch(
            "slate.terminals.Gio.AppInfo.launch_default_for_uri_async"
        ) as launch:
            consumed = TerminalManager._on_terminal_button_press(
                owner,
                terminal,
                event,
            )
        self.assertTrue(consumed)
        launch.assert_called_once_with(
            "https://example.com/docs",
            None,
            None,
            owner._on_terminal_uri_opened,
            "https://example.com/docs",
        )
        terminal.match_check_event.assert_not_called()

    def test_click_outside_terminal_url_remains_unhandled(self) -> None:
        """A click outside recognized links remains available to terminal programs."""

        terminal = MagicMock()
        terminal.hyperlink_check_event.return_value = None
        terminal.match_check_event.return_value = (None, -1)
        event = SimpleNamespace(button=1, state=0)
        owner = SimpleNamespace(_on_terminal_uri_opened=MagicMock())
        with patch(
            "slate.terminals.Gio.AppInfo.launch_default_for_uri_async"
        ) as launch:
            consumed = TerminalManager._on_terminal_button_press(
                owner,
                terminal,
                event,
            )
        self.assertFalse(consumed)
        launch.assert_not_called()

    def test_click_opens_matched_text_url_without_sentence_punctuation(self) -> None:
        """Textual URLs use VTE matches and discard adjacent sentence punctuation."""

        terminal = MagicMock()
        terminal.hyperlink_check_event.return_value = None
        terminal.match_check_event.return_value = ("https://example.com/help).", 1)
        event = SimpleNamespace(
            button=1,
            state=0,
        )
        owner = SimpleNamespace(_on_terminal_uri_opened=MagicMock())
        with patch(
            "slate.terminals.Gio.AppInfo.launch_default_for_uri_async"
        ) as launch:
            consumed = TerminalManager._on_terminal_button_press(
                owner,
                terminal,
                event,
            )
        self.assertTrue(consumed)
        self.assertEqual(launch.call_args.args[0], "https://example.com/help")

    def test_initial_command_is_fed_to_persistent_shell(self) -> None:
        """The Codex resume action writes one command followed by Enter."""

        terminal = MagicMock()
        TerminalManager._feed_initial_command(terminal, "codex resume")
        terminal.feed_child.assert_called_once_with(b"codex resume\n")

    def test_existing_tmux_session_does_not_receive_persisted_command(self) -> None:
        """A live tmux session is attached without typing its launcher again."""

        terminal = object()
        owner = SimpleNamespace(
            session_checks={"repo/codex-1": object()},
            terminals={"repo/codex-1": terminal},
            shutting_down=False,
            closing_keys=set(),
            _server_absent=TerminalManager._server_absent,
            _error=TerminalManager._error,
            on_error=MagicMock(),
            _spawn_terminal=MagicMock(),
        )
        result = CommandResult(("tmux", "has-session"), 0, "", "")

        TerminalManager._on_session_checked(
            owner,
            result,
            terminal,
            "repo/codex-1",
            "repo",
            "/tmp/repo",
            "codex-1",
            "/tmp/repo",
            "codex resume",
        )

        owner._spawn_terminal.assert_called_once_with(
            terminal,
            "repo/codex-1",
            "repo",
            "/tmp/repo",
            "codex-1",
            "/tmp/repo",
            None,
        )
        owner.on_error.assert_not_called()

    def test_missing_tmux_session_receives_persisted_command_once(self) -> None:
        """An absent tmux session queues its persisted launcher for VTE spawn."""

        terminal = object()
        owner = SimpleNamespace(
            session_checks={"repo/codex-1": object()},
            terminals={"repo/codex-1": terminal},
            shutting_down=False,
            closing_keys=set(),
            _server_absent=TerminalManager._server_absent,
            _error=TerminalManager._error,
            on_error=MagicMock(),
            _spawn_terminal=MagicMock(),
        )
        result = CommandResult(
            ("tmux", "has-session"), 1, "", "can't find session: repo--codex-1"
        )

        TerminalManager._on_session_checked(
            owner,
            result,
            terminal,
            "repo/codex-1",
            "repo",
            "/tmp/repo",
            "codex-1",
            "/tmp/repo",
            "codex resume",
        )

        owner._spawn_terminal.assert_called_once_with(
            terminal,
            "repo/codex-1",
            "repo",
            "/tmp/repo",
            "codex-1",
            "/tmp/repo",
            "codex resume",
        )

    def test_global_tmux_options_are_batched_once(self) -> None:
        """Mouse and status configuration share one retained tmux invocation."""

        owner = SimpleNamespace(
            tmux=("tmux", "-L", "slate"),
            sessions={"repo/main": "repo--main"},
            status_bar_enabled=False,
            server_configured=False,
            server_configuring=False,
            server_configuration_dirty=False,
            _on_server_configured=MagicMock(),
        )
        owner._batched_tmux_argv = TerminalManager._batched_tmux_argv.__get__(owner)
        with patch("slate.terminals.run_async") as run:
            TerminalManager._configure_server(owner)
            TerminalManager._configure_server(owner)
        run.assert_called_once()
        argv = run.call_args.args[0]
        self.assertIn("mouse", argv)
        self.assertIn("repo--main", argv)
        status_target = argv.index("repo--main")
        self.assertEqual(
            argv[status_target - 2 : status_target + 3],
            ["set-option", "-t", "repo--main", "status", "off"],
        )
        self.assertIn("WheelUpPane", argv)
        self.assertIn("WheelDownPane", argv)
        self.assertNotIn("#{mouse_any_flag}", argv)
        self.assertIn("status-left", argv)
        self.assertIn("#{@slate_agent_display}", " ".join(argv))
        self.assertTrue(owner.server_configuring)

    def test_codex_session_metadata_marks_node_launcher(self) -> None:
        """Codex terminals persist a label without renaming unrelated Node jobs."""

        owner = SimpleNamespace(
            tmux=("tmux", "-L", "slate"),
            status_bar_enabled=True,
            _encode_metadata=TerminalManager._encode_metadata,
            _status_label=TerminalManager._status_label,
            _ignore_metadata_result=MagicMock(),
        )
        owner._batched_tmux_argv = TerminalManager._batched_tmux_argv.__get__(owner)
        with patch("slate.terminals.run_async") as run:
            TerminalManager._set_metadata(
                owner, "repo--main", "Repo", "/tmp/repo", "main", "Codex"
            )
        run.assert_called_once()
        argv = run.call_args.args[0]
        self.assertIn("@slate_agent_display", argv)
        self.assertIn("Codex", argv)
        status_target = argv.index("status")
        self.assertEqual(
            argv[status_target - 2 : status_target + 2],
            ["-t", "repo--main", "status", "on"],
        )
        self.assertGreaterEqual(argv.count(";"), 4)

    def test_status_bar_setting_applies_to_existing_sessions(self) -> None:
        """Changing the preference updates tmux without rebuilding terminals."""

        owner = SimpleNamespace(
            sessions={"project/terminal": "project--terminal"},
            status_bar_enabled=False,
            server_configured=True,
            server_configuring=False,
            server_configuration_dirty=False,
            _configure_server=MagicMock(),
        )
        TerminalManager.set_status_bar_enabled(owner, True)
        self.assertTrue(owner.status_bar_enabled)
        self.assertFalse(owner.server_configured)
        owner._configure_server.assert_called_once_with()

    def test_show_focuses_terminal_after_tree_click_finishes(self) -> None:
        """Terminal focus is deferred until GTK completes the tree mouse event."""

        terminal = MagicMock()
        owner = SimpleNamespace(
            terminals={"repo/main": terminal},
            stack=MagicMock(),
            on_attention=MagicMock(),
        )
        with patch("slate.terminals.GLib.idle_add") as idle_add:
            self.assertTrue(TerminalManager.show(owner, "repo", "main"))
        owner.stack.set_visible_child_name.assert_called_once_with("repo/main")
        owner.on_attention.assert_called_once_with(terminal, False)
        idle_add.assert_called_once_with(terminal.grab_focus)
        terminal.grab_focus.assert_not_called()

    def test_activity_events_share_one_hundred_millisecond_debounce(self) -> None:
        """Repeated foreground events reset one lightweight trailing timer."""

        owner = SimpleNamespace(
            activity_monitoring=True,
            terminals={"repo/main": object()},
            activity_dirty=False,
            activity_interval_id=None,
            activity_command=None,
            activity_debounce_id=None,
            ACTIVITY_DEBOUNCE_MS=100,
            _on_activity_debounce=MagicMock(),
            _cancel_activity_timers=MagicMock(),
        )
        with (
            patch("slate.terminals.GLib.timeout_add", side_effect=(41, 42)) as timeout,
            patch("slate.terminals.GLib.source_remove") as remove,
        ):
            TerminalManager.request_activity_refresh(owner)
            TerminalManager.request_activity_refresh(owner)
        self.assertTrue(owner.activity_dirty)
        self.assertEqual(timeout.call_count, 2)
        remove.assert_called_once_with(41)
        self.assertEqual(owner.activity_debounce_id, 42)

    def test_background_window_cancels_every_activity_timer(self) -> None:
        """Losing foreground activity stops debounce and interval scheduling."""

        owner = SimpleNamespace(
            shutting_down=False,
            activity_monitoring=True,
            activity_dirty=True,
            _cancel_activity_timers=MagicMock(),
            request_activity_refresh=MagicMock(),
        )
        TerminalManager.set_activity_monitoring(owner, False)
        self.assertFalse(owner.activity_monitoring)
        self.assertFalse(owner.activity_dirty)
        owner._cancel_activity_timers.assert_called_once_with()
        owner.request_activity_refresh.assert_not_called()

    def test_activity_completion_waits_five_seconds_without_overlap(self) -> None:
        """A completed pane query schedules its successor from completion time."""

        owner = SimpleNamespace(
            activity_command=object(),
            activity_monitoring=True,
            terminals={"repo/main": object()},
            activity_dirty=False,
            activity_interval_id=None,
            ACTIVITY_INTERVAL_MS=5000,
            _publish_activity=MagicMock(),
            _on_activity_interval=MagicMock(),
            request_activity_refresh=MagicMock(),
        )
        panes = []
        with patch("slate.terminals.GLib.timeout_add", return_value=77) as timeout:
            TerminalManager._activity_completed(owner, panes)
        owner._publish_activity.assert_called_once_with(panes)
        timeout.assert_called_once_with(5000, owner._on_activity_interval)
        self.assertEqual(owner.activity_interval_id, 77)

    def test_periodic_activity_tick_starts_one_query(self) -> None:
        """Each elapsed five-second interval starts one single-flight query."""

        owner = SimpleNamespace(
            activity_interval_id=55,
            activity_dirty=False,
            _start_activity_query=MagicMock(),
        )
        self.assertFalse(TerminalManager._on_activity_interval(owner))
        self.assertTrue(owner.activity_dirty)
        owner._start_activity_query.assert_called_once_with()

    def test_periodic_activity_query_never_requests_process_durations(self) -> None:
        """The five-second activity path cannot launch per-pane ps processes."""

        command = object()
        owner = SimpleNamespace(
            activity_monitoring=True,
            terminals={"repo/main": object()},
            activity_command=None,
            activity_dirty=True,
            query_panes=MagicMock(return_value=command),
            _activity_completed=MagicMock(),
        )
        TerminalManager._start_activity_query(owner)
        owner.query_panes.assert_called_once_with(
            owner._activity_completed,
            include_duration=False,
        )
        self.assertIs(owner.activity_command, command)

    def test_lazy_terminal_rename_accepts_an_unmaterialized_session(self) -> None:
        """Renaming config succeeds when no tmux server or VTE exists yet."""

        callback = MagicMock()
        owner = SimpleNamespace(
            tmux=("tmux", "-L", "slate"),
            terminals={},
            sessions={},
            on_error=MagicMock(),
            _server_absent=TerminalManager._server_absent,
            _error=TerminalManager._error,
            _set_metadata=MagicMock(),
            request_activity_refresh=MagicMock(),
        )
        with patch("slate.terminals.run_async") as run:
            TerminalManager.rename(owner, "Repo", "main", "work", callback)
        completed = run.call_args.args[1]
        completed(
            CommandResult(tuple(run.call_args.args[0]), 1, "", "no server running")
        )
        callback.assert_called_once_with(True)
        owner.on_error.assert_not_called()
        owner._set_metadata.assert_not_called()

    def test_lazy_terminal_close_still_targets_a_background_session(self) -> None:
        """Closing an unloaded row issues kill-session instead of returning early."""

        callback = MagicMock()
        owner = SimpleNamespace(
            tmux=("tmux", "-L", "slate"),
            terminals={},
            sessions={},
            initial_commands={},
            session_checks={},
            closing_keys=set(),
            stack=MagicMock(),
            on_attention=MagicMock(),
            on_error=MagicMock(),
            request_activity_refresh=MagicMock(),
        )
        with patch("slate.terminals.run_async") as run:
            TerminalManager.close(owner, "Repo", "main", callback)
        self.assertEqual(
            run.call_args.args[0][-3:], ["kill-session", "-t", "repo--main"]
        )
        completed = run.call_args.args[1]
        completed(CommandResult(tuple(run.call_args.args[0]), 0, "", ""))
        callback.assert_called_once_with(True)

    def test_bell_attention_clears_only_when_terminal_regains_focus(self) -> None:
        """VTE bell and focus signals publish the matching attention states."""

        terminal = MagicMock()
        terminal.has_focus.return_value = False
        owner = SimpleNamespace(
            on_attention=MagicMock(), on_bell=MagicMock()
        )
        TerminalManager._on_terminal_bell(owner, terminal, "repo", "main")
        owner.on_bell.assert_called_once_with("repo", "main")
        owner.on_attention.assert_called_once_with(terminal, True)
        self.assertFalse(
            TerminalManager._on_terminal_focus_in(owner, terminal, MagicMock())
        )
        self.assertEqual(
            owner.on_attention.call_args_list,
            [
                unittest.mock.call(terminal, True),
                unittest.mock.call(terminal, False),
            ],
        )

    def test_bell_on_focused_terminal_does_not_request_attention(self) -> None:
        """A visible terminal cannot leave a redundant bell on its own row."""

        terminal = MagicMock()
        terminal.has_focus.return_value = True
        owner = SimpleNamespace(
            on_attention=MagicMock(), on_bell=MagicMock()
        )
        TerminalManager._on_terminal_bell(owner, terminal, "repo", "main")
        owner.on_bell.assert_called_once_with("repo", "main")
        owner.on_attention.assert_called_once_with(terminal, False)

    def test_ended_child_is_removed_and_reported(self) -> None:
        """A naturally ended tmux client cannot leave a dead Vte widget behind."""

        terminal = object()
        owner = SimpleNamespace(
            shutting_down=False,
            closing_keys=set(),
            terminals={"repo/main": terminal},
            sessions={"repo/main": "repo--main"},
            initial_commands={},
            session_checks={},
            spawn_cancellables={},
            stack=MagicMock(),
            on_exit=MagicMock(),
            on_attention=MagicMock(),
            server_configured=True,
            server_configuring=False,
            server_configuration_dirty=False,
            request_activity_refresh=MagicMock(),
        )
        TerminalManager._on_terminal_child_exited(owner, terminal, 0)
        self.assertEqual(owner.terminals, {})
        self.assertEqual(owner.sessions, {})
        owner.stack.remove.assert_called_once_with(terminal)
        owner.on_exit.assert_called_once_with("repo", "main", 0)

    def test_application_shutdown_ignores_child_exit_signal(self) -> None:
        """Deliberate app shutdown retains configured terminals for next launch."""

        terminal = object()
        owner = SimpleNamespace(
            shutting_down=True,
            closing_keys=set(),
            terminals={"repo/main": terminal},
            sessions={"repo/main": "repo--main"},
            initial_commands={},
            session_checks={},
            spawn_cancellables={},
            stack=MagicMock(),
            on_exit=MagicMock(),
            on_attention=MagicMock(),
            server_configured=True,
            server_configuring=False,
            server_configuration_dirty=False,
            request_activity_refresh=MagicMock(),
        )
        TerminalManager._on_terminal_child_exited(owner, terminal, 0)
        self.assertIn("repo/main", owner.terminals)
        owner.stack.remove.assert_not_called()
        owner.on_exit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
