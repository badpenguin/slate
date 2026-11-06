"""Exercise production tmux commands against real isolated servers and stdin receivers."""

import shlex
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from gi.repository import GLib

from slate.terminals import TerminalManager


class TmuxTargetTest(unittest.TestCase):
    """Verify effects of metadata, configuration and input commands, not just argv spelling."""

    def setUp(self) -> None:
        """Create an uninitialized manager bound exclusively to a temporary tmux socket."""

        self.temporary = tempfile.TemporaryDirectory(dir="/tmp", prefix="slate-tmux-targets-")
        self.root = Path(self.temporary.name)
        self.tmux = ["tmux", "-S", str(self.root / "socket"), "-f", "/dev/null"]
        self.manager = TerminalManager.__new__(TerminalManager)
        self.manager.tmux = tuple(self.tmux)
        self.manager.sessions = {}
        self.manager.terminals = {}
        self.manager.status_bar_enabled = False
        self.manager.server_configured = False
        self.manager.server_configuring = False
        self.manager.server_configuration_dirty = False
        self.errors = []
        self.completed = []
        self.manager.on_error = self.errors.append
        self.manager._ignore_metadata_result = self._metadata_finished
        self.manager._on_server_configured = self._configuration_finished
        self.manager._codex_rename_sent = self._rename_finished

    def tearDown(self) -> None:
        """Destroy only the private server and its disposable fixture files."""

        self._execute("kill-server")
        self.temporary.cleanup()

    def _execute(self, *arguments: str) -> subprocess.CompletedProcess:
        """Run a bounded fixture command on the explicitly isolated socket."""

        return subprocess.run(self.tmux + list(arguments), capture_output=True, text=True, timeout=5)

    def _session(self, name: str, command: str = "sleep 60") -> None:
        """Start a disposable session and retain its ordinary unqualified manager identity."""

        result = self._execute("new-session", "-d", "-s", name, command)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.manager.sessions[name] = name

    def _metadata_finished(self, result) -> None:
        """Record completion while exercising the real metadata error handler."""

        TerminalManager._ignore_metadata_result(self.manager, result)
        self.completed.append(result)

    def _configuration_finished(self, result) -> None:
        """Record completion while exercising the real configuration state transitions."""

        TerminalManager._on_server_configured(self.manager, result)
        self.completed.append(result)

    def _rename_finished(self, result) -> None:
        """Record the real send failure or final Enter result without mocking execution."""

        TerminalManager._codex_rename_sent(self.manager, result)
        self.completed.append(result)

    def _wait_command(self) -> None:
        """Pump Gio until the production callback completes or its deadline expires."""

        deadline = time.monotonic() + 5
        context = GLib.MainContext.default()
        while not self.completed and time.monotonic() < deadline:
            context.iteration(False)
            time.sleep(0.005)
        self.assertTrue(self.completed, "The real tmux operation never completed")

    def _option(self, session: str, option: str) -> str:
        """Read an explicit tmux option through its API rather than terminal output."""

        result = self._execute("show-options", "-v", "-t", f"={session}:", option)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def test_metadata_round_trip_and_missing_prefix_leave_other_session_unchanged(self) -> None:
        """Real metadata writes succeed and missing short names cannot modify longer names."""

        self._session("repo--main")
        self._session("repo--main-two")
        for session, title in (("repo--main", "main"), ("repo--main-two", "main-two")):
            self.completed.clear()
            self.manager._set_metadata(session, "Repo", "/tmp/project with | spaces", title, "Codex")
            self._wait_command()
            self.assertTrue(self.completed[-1].ok, self.errors)
            self.assertEqual(self.manager._decode_metadata(self._option(session, "@slate_terminal_name")), title)
            self.assertEqual(self.manager._decode_metadata(self._option(session, "@slate_project_path")),
                             "/tmp/project with | spaces")
            self.assertEqual(self._option(session, "@slate_agent_display"), "Codex")
            self.assertEqual(self._option(session, "status"), "off")
        self.assertEqual(self.errors, [])
        result = self._execute("kill-session", "-t", "=repo--main")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.completed.clear()
        self.manager._set_metadata("repo--main", "Wrong", "/tmp/wrong", "wrong")
        self._wait_command()
        self.assertFalse(self.completed[-1].ok)
        self.assertEqual(self.manager._decode_metadata(self._option("repo--main-two", "@slate_terminal_name")), "main-two")
        self.assertEqual(self.manager._decode_metadata(self._option("repo--main-two", "@slate_project_path")),
                         "/tmp/project with | spaces")

    def test_global_configuration_and_status_changes_reach_existing_sessions(self) -> None:
        """The complete production command batch works on actual tmux and applies each status toggle."""

        self._session("repo--main")
        self._session("repo--main-two")
        for enabled in (True, False):
            self.completed.clear()
            self.manager.set_status_bar_enabled(enabled)
            self._wait_command()
            self.assertTrue(self.completed[-1].ok, self.errors)
            self.assertTrue(self.manager.server_configured)
            for session in ("repo--main", "repo--main-two"):
                self.assertEqual(self._option(session, "status"), "on" if enabled else "off")
        self.assertEqual(self.errors, [])

    def test_codex_rename_delivers_literal_text_and_enter_to_only_the_intended_tty(self) -> None:
        """Real pane queries, ps checks and send-keys deliver a submitted line to an isolated receiver."""

        # 2026-11-06: a copied cat executable supplies a harmless codex-* foreground
        # process. Its canonical TTY delivers stdin only after Enter. Assert its
        # redirected input bytes, never parse a terminal screen or launch real Codex.
        receiver = self.root / "codex-audit"
        shutil.copyfile(shutil.which("cat"), receiver)
        receiver.chmod(0o755)
        selected = self.root / "selected-input"
        other = self.root / "other-input"
        for session, output in (("repo--work-two", selected), ("repo--work-two-other", other)):
            self._session(session, f"exec {shlex.quote(str(receiver))} > {shlex.quote(str(output))}")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            panes = self._execute("list-panes", "-a", "-F", "#{pane_current_command}")
            if panes.returncode == 0 and panes.stdout.splitlines() == ["codex-audit", "codex-audit"]:
                break
            time.sleep(0.01)
        self.assertEqual(panes.stdout.splitlines(), ["codex-audit", "codex-audit"])
        self.manager.terminals = {"Repo/Work two": object()}
        self.manager.sessions = {"Repo/Work two": "repo--work-two"}
        self.manager.request_codex_rename("Repo", "Work two")
        self._wait_command()
        self.assertTrue(self.completed[-1].ok, self.errors)
        deadline = time.monotonic() + 3
        while (not selected.exists() or not selected.read_bytes()) and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertEqual(selected.read_bytes(), b"/rename Work two\n")
        self.assertEqual(other.read_bytes(), b"")
        self.assertEqual(self.errors, [])
