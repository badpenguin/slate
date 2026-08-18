"""Workflow tests for simple explicit repository-action dialogs."""

import unittest
from unittest.mock import MagicMock

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk

from slate.repository_actions import (
    RepositoryCreateBranchDialog,
    RepositoryMergeBranchDialog,
    RepositoryPublishDialog,
    RepositorySwitchBranchDialog,
    RepositoryTagDialog,
)
from slate.scm.base import RepositoryRef
from slate.scm.git import GitSCM
from slate.scm.hg import MercurialSCM
from slate.window import SlateWindow
from tests.repository_dialog_fixture import RepositoryDialogFixture


class RepositoryActionDialogTest(RepositoryDialogFixture, unittest.TestCase):
    """Verify happy paths stop cleanly and conflicts offer only Meld."""

    def test_local_mercurial_publish_is_an_informational_result(self) -> None:
        """Absent default paths never become a misleading push failure."""

        dialog = self._open(
            RepositoryPublishDialog, MercurialSCM(self.temporary.name)
        )
        self._complete(0)
        self._complete(1, returncode=1)
        self._complete(2, returncode=1)
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(dialog.phase_label.get_text(), "Nessuna destinazione remota")
        self.assertIn("repository è locale", dialog.detail_label.get_text())

    def test_git_new_branch_uses_only_the_entered_local_name(self) -> None:
        """New branch does not infer tracking or alternate names."""

        dialog = self._open(
            RepositoryCreateBranchDialog, GitSCM(self.temporary.name)
        )
        self._complete(0, returncode=1)
        dialog.name_entry.set_text("topic")
        dialog._submit()
        self.assertEqual(
            self.calls[1][0],
            ["git", "switch", "--no-track", "-c", "topic"],
        )
        self._complete(1)
        self.assertEqual(dialog.phase_label.get_text(), "Branch creato")

    def test_idle_action_cancellation_releases_the_shared_watcher_once(self) -> None:
        """Closing a ready action resumes exactly one coherent repository refresh."""

        dialog = self._open(
            RepositoryCreateBranchDialog, GitSCM(self.temporary.name)
        )
        self._complete(0, returncode=1)
        dialog._on_response(dialog, Gtk.ResponseType.CANCEL)
        self.watcher.resume_with_full_refresh.assert_called_once_with(
            refresh_branch=True
        )
        self.on_closed.assert_called_once_with()
        self.dialog = None

    def test_git_publish_requires_existing_upstream_and_never_forces(self) -> None:
        """Configured Git publishing performs only the normal follow-tags push."""

        dialog = self._open(RepositoryPublishDialog, GitSCM(self.temporary.name))
        self._complete(0, returncode=1)
        self._complete(1, "origin\n")
        self._complete(2, "origin/main\n")
        dialog._submit()
        self.assertEqual(self.calls[3][0], ["git", "push", "--follow-tags"])
        self.assertNotIn("--force", self.calls[3][0])

    def test_git_switch_uses_only_a_loaded_local_branch(self) -> None:
        """Switch never guesses a similarly named remote branch."""

        dialog = self._open(
            RepositorySwitchBranchDialog, GitSCM(self.temporary.name)
        )
        self._complete(0, returncode=1)
        self._complete(1)
        self._complete(2, "main\n")
        self._complete(3, "main\0\nfeature\0\n")
        dialog._submit()
        self.assertEqual(
            self.calls[4][0],
            [
                "git",
                "switch",
                "--no-guess",
                "--no-recurse-submodules",
                "feature",
            ],
        )

    def test_git_tag_is_annotated_without_force(self) -> None:
        """Assign tag uses one automatic message after clean-state preflight."""

        dialog = self._open(RepositoryTagDialog, GitSCM(self.temporary.name))
        self._complete(0, returncode=1)
        self._complete(1)
        self._complete(2, "v3\0\nv2\0\nv1\0\n")
        self.assertEqual(
            dialog.recent_tags_label.get_text(),
            "Ultimi tag: v3  ·  v2  ·  v1",
        )
        dialog.name_entry.set_text("v1")
        dialog._submit()
        self.assertEqual(
            self.calls[3][0],
            ["git", "tag", "-a", "-m", "Tag v1", "--", "v1"],
        )

    def test_tag_form_stays_available_when_recent_tags_cannot_be_loaded(self) -> None:
        """Optional tag history never prevents the explicit local action."""

        dialog = self._open(RepositoryTagDialog, GitSCM(self.temporary.name))
        self._complete(0, returncode=1)
        self._complete(1)
        self._complete(2, stderr="lettura fallita", returncode=1)
        self.assertEqual(
            dialog.recent_tags_label.get_text(),
            "Ultimi tag: non disponibili",
        )
        dialog.name_entry.set_text("v1")
        self.assertTrue(dialog.action_button.get_sensitive())

    def test_mercurial_switch_stops_on_multiple_branch_heads(self) -> None:
        """An ambiguous named branch is reported without selecting a revision."""

        dialog = self._open(
            RepositorySwitchBranchDialog, MercurialSCM(self.temporary.name)
        )
        self._complete(0)
        self._complete(1, "[]")
        self._complete(2, "default\n")
        self._complete(
            3,
            '[{"branch":"default","node":"aaa","closed":false},'
            '{"branch":"feature","node":"bbb","closed":false}]',
        )
        dialog._submit()
        self._complete(4, '[{"node":"bbb"},{"node":"ccc"}]')
        self.assertEqual(dialog.phase_label.get_text(), "Branch ambiguo")
        self.assertEqual(len(self.calls), 5)

    def test_git_merge_conflict_offers_meld_without_commit_or_abort(self) -> None:
        """Conflict handling launches only the explicit external merge tool."""

        dialog = self._open(
            RepositoryMergeBranchDialog, GitSCM(self.temporary.name)
        )
        self._complete(0, returncode=1)
        self._complete(1)
        self._complete(2, "main\n")
        self._complete(3, "main\0\nfeature\0\n")
        dialog._submit()
        self.assertEqual(
            self.calls[4][0],
            ["git", "merge", "--no-ff", "--no-commit", "--no-edit", "feature"],
        )
        self._complete(4, stderr="merge conflict", returncode=1)
        self._complete(5, "conflict.py\0")
        self.assertEqual(dialog.phase_label.get_text(), "Conflitti nel merge")
        dialog._on_response(dialog, Gtk.ResponseType.APPLY)
        self.assertEqual(
            self.calls[6][0],
            ["git", "mergetool", "--no-prompt", "--tool=meld"],
        )
        flattened = [argument for argv, _callback, _kwargs in self.calls for argument in argv]
        self.assertNotIn("--abort", flattened)
        self.assertNotIn("commit", flattened)

    def test_existing_repository_modal_is_reused_instead_of_overlapped(self) -> None:
        """Window dispatch presents the active modal and never opens a second one."""

        active_dialog = MagicMock()
        owner = type("WindowOwner", (), {"repository_dialog": active_dialog})()
        SlateWindow._open_repository_action(
            owner, "publish", RepositoryRef(".", "hg")
        )
        active_dialog.present.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
