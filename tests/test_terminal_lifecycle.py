"""Verify selective retirement without interacting with production terminals."""

import subprocess
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from slate.processes import CommandResult
from slate.terminals import PaneInfo, TerminalManager


class TerminalLifecycleTest(unittest.TestCase):
    """Keep launchers and jobs while retiring idle plain shells safely."""

    def test_retirement_keeps_jobs_launchers_and_multiple_panes(self) -> None:
        """Only a verified childless plain shell receives a kill-session request."""

        projects = [{"name": "repo", "terminals": ["idle", "jobs", "mc", "custom", "multi", "agent"],
                     "terminal_commands": {"custom": "ssh example"}}]
        panes = [PaneInfo("repo--idle", "bash", 10), PaneInfo("repo--jobs", "bash", 20),
                 PaneInfo("repo--mc", "mc", 30), PaneInfo("repo--custom", "bash", 40),
                 PaneInfo("repo--multi", "bash", 50), PaneInfo("repo--multi", "bash", 60),
                 PaneInfo("repo--agent", "bash", 70, agent_display="Codex")]
        owner = SimpleNamespace(query_panes=MagicMock(), kill_session=MagicMock())
        finished = MagicMock()
        with patch("slate.terminals.run_async") as run:
            TerminalManager.retire_idle_shells(owner, projects, finished)
            owner.query_panes.call_args.args[0](panes)
            run.call_args.args[1](CommandResult((), 0,
                "10 1 10 10 S bash\n20 1 20 20 S bash\n21 20 21 20 S sleep\n"
                "30 1 30 30 S bash\n40 1 40 40 S bash\n50 1 50 50 S bash\n60 1 60 60 S bash\n70 1 70 70 S bash\n", ""))
        self.assertEqual(owner.kill_session.call_count, 1)
        self.assertEqual(owner.kill_session.call_args.args[0], "repo--idle")
        finished.assert_not_called()
        owner.kill_session.call_args.args[1](CommandResult((), 0, "", ""))
        finished.assert_called_once_with()

    def test_uncertain_process_state_never_terminates_a_shell(self) -> None:
        """Errors, malformed rows, changed foreground and missing PIDs keep sessions."""

        for code, output in ((1, ""), (0, "invalid"), (0, ""),
                             (0, "10 1 10 11 S bash\n"), (0, "10 1 10 10 S mc\n"),
                             (0, "10 1 10 10 R bash\n"), (0, "10 1 10 10 T bash\n")):
            with self.subTest(code=code, output=output):
                owner = SimpleNamespace(query_panes=MagicMock(), kill_session=MagicMock())
                finished = MagicMock()
                with patch("slate.terminals.run_async") as run:
                    TerminalManager.retire_idle_shells(
                        owner, [{"name": "repo", "terminals": ["main"]}], finished)
                    owner.query_panes.call_args.args[0]([PaneInfo("repo--main", "bash", 10)])
                    run.call_args.args[1](CommandResult((), code, output, ""))
                owner.kill_session.assert_not_called()
                finished.assert_called_once_with()

    def test_tmux_hangup_saves_bash_history(self) -> None:
        """An isolated PTY closure persists interactive Bash history without GUI."""

        # 2026-10-16: validate the actual shutdown mechanism, not just mocked
        # commands. The unique socket and /tmp profile never touch user sessions.
        with tempfile.TemporaryDirectory(prefix="slate-history-", dir="/tmp") as directory:
            root = Path(directory)
            tmux = ["tmux", "-L", "slate-test-" + uuid.uuid4().hex, "-f", "/dev/null"]

            def execute(*arguments: str) -> subprocess.CompletedProcess:
                """Execute a bounded command only on this test's private server."""

                return subprocess.run(tmux + list(arguments), capture_output=True, timeout=5)

            try:
                result = execute("new-session", "-d", "-s", "test", "-e",
                                 f"HISTFILE={root / 'history'}", "bash --noprofile --norc -i")
                self.assertEqual(result.returncode, 0, result.stderr)
                execute("send-keys", "-t", "test",
                        f"history -s slate-persisted-marker; touch {root}/ready", "Enter")
                deadline = time.monotonic() + 3
                while not (root / "ready").exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue((root / "ready").exists())
                self.assertEqual(execute("kill-session", "-t", "test").returncode, 0)
                deadline = time.monotonic() + 3
                while not (root / "history").exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertIn("slate-persisted-marker", (root / "history").read_text())
            finally:
                execute("kill-server")
