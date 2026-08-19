"""Workflow tests for explicit repository remote verification."""

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from slate.repository_verify import (
    RepositoryVerifyDialog,
    UnattendedRepositoryVerifier,
)
from slate.scm.base import RepositoryRef, RepositorySyncStatus
from slate.scm.git import GitSCM
from slate.scm.hg import MercurialSCM
from slate.window import SlateWindow
from tests.repository_dialog_fixture import RepositoryDialogFixture


class RepositoryVerifyDialogTest(RepositoryDialogFixture, unittest.TestCase):
    """Verify remote comparisons remain explicit, normalized and non-interactive."""

    def setUp(self) -> None:
        """Add the verification-result recorder to the shared dialog fixture."""

        super().setUp()
        self.on_verified = MagicMock()

    def _open_verify(self, scm: GitSCM | MercurialSCM) -> RepositoryVerifyDialog:
        """Open a verification dialog and grant its watcher boundary."""

        dialog = RepositoryVerifyDialog(
            None,
            scm,
            self.watcher,
            self.on_closed,
            self.on_verified,
        )
        self.dialog = dialog
        dialog.start()
        paused_callback = self.watcher.pause_after_current.call_args.args[0]
        paused_callback()
        return dialog

    def test_git_fetch_reports_diverged_counts_without_prompts(self) -> None:
        """Git fetches explicitly and publishes both sides of a divergence."""

        dialog = self._open_verify(GitSCM(self.temporary.name))
        self._complete(0, "main\n")
        self._complete(1, "origin\n")
        self._complete(2, "origin/main\n")
        self.assertEqual(
            self.calls[3][0], ["git", "fetch", "--no-recurse-submodules"]
        )
        environment = self.calls[3][2]["env"]
        self.assertEqual(environment["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(environment["GIT_ASKPASS"], "/bin/false")
        self.assertEqual(environment["GIT_SSH_COMMAND"], "ssh -oBatchMode=yes")
        self.assertEqual(environment["GIT_CONFIG_VALUE_0"], "")
        self.assertEqual(environment["GIT_CONFIG_VALUE_1"], "cache --timeout=3600")
        self.assertNotIn("slate.git_credentials", str(environment))
        self._complete(3)
        self._complete(4, "2\t3\n")
        self.on_verified.assert_called_once_with(
            RepositorySyncStatus("diverged", 2, 3)
        )
        self.assertEqual(dialog.phase_label.get_text(), "Repository histories diverged")
        self.on_closed.assert_not_called()

    def test_git_without_remotes_is_reported_as_local(self) -> None:
        """A local-only Git repository finishes without starting network work."""

        dialog = self._open_verify(GitSCM(self.temporary.name))
        self._complete(0, "main\n")
        self._complete(1)
        self.assertEqual(len(self.calls), 2)
        self.on_verified.assert_called_once_with(RepositorySyncStatus("local"))
        self.assertEqual(dialog.phase_label.get_text(), "Local repository")
        self.on_closed.assert_not_called()

    def test_mercurial_without_default_closes_as_local(self) -> None:
        """A local-only Mercurial repository is a completed verification."""

        dialog = self._open_verify(MercurialSCM(self.temporary.name))
        self._complete(0, "default\n")
        self._complete(1, returncode=1)
        self.on_verified.assert_called_once_with(RepositorySyncStatus("local"))
        self.assertEqual(dialog.phase_label.get_text(), "Local repository")
        self.on_closed.assert_not_called()

    def test_git_access_failure_is_sanitized_and_requires_access(self) -> None:
        """A failed fetch exposes no password and never opens a credential dialog."""

        dialog = self._open_verify(GitSCM(self.temporary.name))
        self._complete(0, "main\n")
        self._complete(1, "origin\n")
        self._complete(2, "origin/main\n")
        self._complete(
            3,
            stderr="fatal: https://alice:secret@example.test/repo",
            returncode=1,
        )
        self.on_verified.assert_called_once_with(
            RepositorySyncStatus("access_required")
        )
        self.assertEqual(dialog.phase_label.get_text(), "Remote access required")
        self.assertNotIn("secret", dialog.detail_label.get_text())
        self.on_closed.assert_not_called()

    def test_mercurial_counts_incoming_and_outgoing_on_default(self) -> None:
        """Mercurial compares both directions against one explicit default path."""

        dialog = self._open_verify(MercurialSCM(self.temporary.name))
        self._complete(0, "default\n")
        self._complete(1, "https://example.test/repository\n")
        self._complete(2, returncode=1)
        first = "a" * 40
        second = "b" * 40
        self._complete(3, f"{first}\n{second}\n")
        self.assertEqual(self.calls[2][0][-1], "default")
        self.assertEqual(self.calls[3][0][-1], "default")
        self.on_verified.assert_called_once_with(
            RepositorySyncStatus("ahead", 2, 0)
        )
        self.assertEqual(dialog.phase_label.get_text(), "Local repository is ahead")
        self.on_closed.assert_not_called()

    def test_window_routes_verify_result_to_the_selected_repository(self) -> None:
        """Window dispatch keeps verification separate from mutating actions."""

        repository = RepositoryRef(".", "git")
        owner = SimpleNamespace(
            repository_dialog=None,
            panel=MagicMock(),
            _repository_scm=MagicMock(return_value=GitSCM(self.temporary.name)),
            _repository_watcher=MagicMock(return_value=self.watcher),
            _on_repository_dialog_closed=MagicMock(),
            _stop_unattended_verification=MagicMock(),
        )
        owner._on_repository_verified = SlateWindow._on_repository_verified.__get__(
            owner
        )
        with patch("slate.window.RepositoryVerifyDialog") as dialog_type:
            SlateWindow._open_repository_action(owner, "verify", repository)
        verified_callback = dialog_type.call_args.args[4]
        result = RepositorySyncStatus("synced")
        verified_callback(result)
        owner.panel.set_remote_status.assert_called_once_with(repository, result)
        dialog_type.return_value.show_all.assert_called_once_with()
        dialog_type.return_value.start.assert_called_once_with()

    def test_unattended_git_verification_uses_cache_without_password_ui(self) -> None:
        """The headless runner reuses memory credentials and constructs no dialog."""

        verified = MagicMock()
        closed = MagicMock()
        with patch("slate.repository_verify.run_async", self._record_command):
            runner = UnattendedRepositoryVerifier(
                GitSCM(self.temporary.name),
                self.watcher,
                verified,
                closed,
            )
            runner.start()
            paused_callback = self.watcher.pause_after_current.call_args.args[0]
            paused_callback()
            self._complete(0, "main\n")
            self._complete(1, "origin\n")
            self._complete(2, "origin/main\n")
            environment = self.calls[3][2]["env"]
            self.assertEqual(environment["GIT_TERMINAL_PROMPT"], "0")
            self.assertEqual(environment["GIT_ASKPASS"], "/bin/false")
            self.assertEqual(
                environment["GIT_CONFIG_VALUE_1"], "cache --timeout=3600"
            )
            self.assertNotIn("slate.git_credentials", str(environment))
            self._complete(3)
            self._complete(4, "0\t0\n")
        verified.assert_called_once_with(RepositorySyncStatus("synced", 0, 0))
        closed.assert_called_once_with()
        self.watcher.resume_with_full_refresh.assert_called_once_with(
            refresh_branch=True
        )

    def test_unattended_close_releases_a_pending_watcher_pause(self) -> None:
        """An explicit action can preempt a queued check without freezing its watcher."""

        closed = MagicMock()
        runner = UnattendedRepositoryVerifier(
            GitSCM(self.temporary.name),
            self.watcher,
            MagicMock(),
            closed,
        )
        runner.start()
        paused_callback = self.watcher.pause_after_current.call_args.args[0]
        runner.close()
        self.watcher.resume_with_full_refresh.assert_called_once_with(
            refresh_branch=True
        )
        closed.assert_called_once_with()
        paused_callback()
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
