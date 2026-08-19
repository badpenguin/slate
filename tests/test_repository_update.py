"""Workflow tests for the dedicated repository Update modal."""

import unittest

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk

from slate.repository_update import RepositoryUpdateDialog
from slate.scm.git import GitSCM
from slate.scm.hg import MercurialSCM
from tests.repository_dialog_fixture import RepositoryDialogFixture


class RepositoryUpdateDialogTest(RepositoryDialogFixture, unittest.TestCase):
    """Verify linear updates and divergence stops without real network access."""

    def test_mercurial_pull_is_followed_by_checked_update_to_one_head(self) -> None:
        """A clean linear HG history updates only to the inspected immutable node."""

        dialog = self._open(
            RepositoryUpdateDialog, MercurialSCM(self.temporary.name)
        )
        self._complete(0)
        self._complete(1, "[]")
        self._complete(2, "https://example.test/repository\n")
        self._complete(3, "old-node\n")
        self._complete(4)
        self._complete(5, '[{"node": "new-node"}]')
        self.assertEqual(
            self.calls[6][0],
            ["hg", "--noninteractive", "update", "--check", "--rev", "new-node"],
        )
        self._complete(6)
        self.assertEqual(dialog.phase_label.get_text(), "Repository updated")
        self.watcher.resume_with_full_refresh.assert_not_called()
        dialog._close()
        self.watcher.resume_with_full_refresh.assert_called_once_with(
            refresh_branch=True
        )
        self.on_closed.assert_called_once_with()
        self.dialog = None

    def test_mercurial_without_default_reports_configuration_problem(self) -> None:
        """A missing default path stops before pull with an actionable result."""

        dialog = self._open(
            RepositoryUpdateDialog, MercurialSCM(self.temporary.name)
        )
        self._complete(0)
        self._complete(1, "[]")
        self._complete(2, stderr="abort: repository default not found", returncode=1)
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(
            dialog.phase_label.get_text(), "No remote source"
        )
        self.assertIn("nothing to update", dialog.detail_label.get_text())

    def test_git_divergence_never_starts_merge(self) -> None:
        """A Git history advanced on both sides stops after comparison."""

        dialog = self._open(RepositoryUpdateDialog, GitSCM(self.temporary.name))
        self._complete(0, returncode=1)
        self._complete(1)
        self._complete(2, "main\n")
        self._complete(3, "origin\n")
        self._complete(4, "origin/main\n")
        self._complete(5)
        self._complete(6, "2\t3\n")
        self.assertEqual(len(self.calls), 7)
        self.assertEqual(dialog.phase_label.get_text(), "Divergent history")
        self.assertIn("was not changed", dialog.detail_label.get_text())

    def test_remote_cancellation_uses_the_shared_dialog_lifecycle(self) -> None:
        """A cancellable fetch becomes one terminal result before watcher release."""

        dialog = self._open(RepositoryUpdateDialog, GitSCM(self.temporary.name))
        self._complete(0, returncode=1)
        self._complete(1)
        self._complete(2, "main\n")
        self._complete(3, "origin\n")
        self._complete(4, "origin/main\n")
        self.assertTrue(dialog.cancel_button.get_sensitive())
        dialog._on_response(dialog, Gtk.ResponseType.CANCEL)
        self.command.cancel.assert_called_once_with()
        self._complete(5, returncode=1)
        self.assertEqual(dialog.phase_label.get_text(), "Update cancelled")
        self.watcher.resume_with_full_refresh.assert_not_called()
        dialog._close()
        self.watcher.resume_with_full_refresh.assert_called_once_with(
            refresh_branch=True
        )
        self.dialog = None

    def test_git_without_remotes_is_reported_as_local_only(self) -> None:
        """A normal local Git repository does not produce an upstream error."""

        dialog = self._open(RepositoryUpdateDialog, GitSCM(self.temporary.name))
        self._complete(0, returncode=1)
        self._complete(1)
        self._complete(2, "main\n")
        self._complete(3)
        self.assertEqual(len(self.calls), 4)
        self.assertEqual(dialog.phase_label.get_text(), "No remote source")
        self.assertIn("local repository", dialog.detail_label.get_text())

    def test_error_text_redacts_embedded_http_password(self) -> None:
        """Credential-bearing remote failures never expose their password."""

        redacted = RepositoryUpdateDialog._redact_credentials(
            "fatal: https://alice:secret@example.test/repo"
        )
        self.assertNotIn("secret", redacted)
        self.assertIn("alice:…@", redacted)


if __name__ == "__main__":
    unittest.main()
