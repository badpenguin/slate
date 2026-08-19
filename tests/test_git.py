"""Git adapter parser and explicit-command tests."""

import tempfile
import unittest
from pathlib import Path

from slate.scm.base import BranchTarget, FileStatus
from slate.scm.git import GitSCM


class GitSCMTest(unittest.TestCase):
    """Verify the Mercurial-like Git workflow and porcelain-v2 parsing."""

    def setUp(self) -> None:
        """Create an adapter rooted in a disposable normal working copy."""

        self.temporary = tempfile.TemporaryDirectory()
        self.scm = GitSCM(self.temporary.name)

    def tearDown(self) -> None:
        """Release the disposable adapter directory."""

        self.temporary.cleanup()

    def test_status_collapses_index_and_worktree_without_staging_sections(self) -> None:
        """Both Git columns map to one selectable normalized status list."""

        output = (
            "1 .M N... 100644 100644 100644 aaa bbb file one.py\0"
            "1 M. N... 100644 100644 100644 aaa bbb staged.py\0"
            "1 D. N... 100644 000000 000000 aaa 000 removed.py\0"
            "u UU N... 100644 100644 100644 100644 a b c conflict.py\0"
            "? nuovo file.txt\0"
        )
        self.assertEqual(
            self.scm.parse_status(output),
            [
                FileStatus("file one.py", "modified", scm_type="git"),
                FileStatus("staged.py", "modified", scm_type="git"),
                FileStatus("removed.py", "removed", scm_type="git"),
                FileStatus("conflict.py", "conflict", scm_type="git"),
                FileStatus("nuovo file.txt", "untracked", scm_type="git"),
            ],
        )

    def test_porcelain_rename_retains_both_atomic_endpoints(self) -> None:
        """A porcelain type-2 rename becomes one moved row with its source."""

        output = (
            "2 R. N... 100644 100644 100644 aaa bbb R100 nuovo.py\0"
            "vecchio.py\0"
        )
        self.assertEqual(
            self.scm.parse_status(output),
            [
                FileStatus(
                    "nuovo.py",
                    "moved",
                    source_path="vecchio.py",
                    scm_type="git",
                )
            ],
        )

    def test_commands_keep_commit_and_add_explicit(self) -> None:
        """Git never stages implicitly and commits only the selected paths."""

        self.assertEqual(
            self.scm.status_argv(("a b.py",)),
            [
                "git",
                "status",
                "--porcelain=v2",
                "-z",
                "--untracked-files=all",
                "--find-renames=50%",
                "--",
                "a b.py",
            ],
        )
        self.assertEqual(
            self.scm.commit_argv("messaggio", ("a b.py",)),
            ["git", "commit", "--only", "-m", "messaggio", "--", "a b.py"],
        )
        self.assertEqual(
            self.scm.add_argv(("nuovo.py",)),
            ["git", "add", "--", "nuovo.py"],
        )
        self.assertEqual(
            self.scm.record_removal_argv(("rimosso.py",)),
            ["git", "add", "-u", "--", "rimosso.py"],
        )
        self.assertEqual(self.scm.environment["GIT_OPTIONAL_LOCKS"], "0")
        self.assertEqual(
            self.scm.diff_argv(("a b.py",))[-3:], ["HEAD", "--", "a b.py"]
        )

    def test_ignore_and_index_lock_use_normal_git_metadata(self) -> None:
        """Ignored paths are NUL-safe and index.lock gates automatic status."""

        self.assertEqual(
            self.scm.parse_ignored("build/cache\0nome con spazio\0"),
            {"build/cache", "nome con spazio"},
        )
        metadata = Path(self.temporary.name) / ".git"
        metadata.mkdir()
        self.assertFalse(self.scm.is_locked())
        (metadata / "index.lock").touch()
        self.assertTrue(self.scm.is_locked())

    def test_update_commands_allow_only_an_explicit_fast_forward(self) -> None:
        """Remote update inspects upstream history before a guarded merge."""

        self.assertEqual(
            self.scm.update_merge_state_argv(),
            ["git", "rev-parse", "--quiet", "--verify", "MERGE_HEAD"],
        )
        self.assertEqual(
            self.scm.update_tracked_status_argv(),
            ["git", "status", "--porcelain=v2", "-z", "--untracked-files=no"],
        )
        self.assertEqual(self.scm.fetch_argv(), ["git", "fetch", "--no-recurse-submodules"])
        self.assertEqual(self.scm.parse_update_comparison("2\t3\n"), (2, 3))
        self.assertEqual(
            self.scm.fast_forward_argv("origin/main"),
            ["git", "merge", "--ff-only", "origin/main"],
        )

    def test_remote_comparison_parser_returns_exact_counts(self) -> None:
        """Explicit verification reuses the stable Git comparison parser."""

        self.assertEqual(self.scm.parse_update_comparison("4\t7\n"), (4, 7))
        with self.assertRaises(ValueError):
            self.scm.parse_update_comparison("invalid")

    def test_repository_actions_remain_local_and_never_force(self) -> None:
        """Branch, merge, publish and tag commands use only the simple path."""

        self.assertEqual(self.scm.push_argv(), ["git", "push", "--follow-tags"])
        self.assertEqual(
            self.scm.parse_branches("main\0\nfeature one\0\n"),
            [BranchTarget("main"), BranchTarget("feature one")],
        )
        self.assertEqual(
            self.scm.recent_tags_argv(),
            [
                "git",
                "for-each-ref",
                "--sort=-creatordate",
                "--count=3",
                "--format=%(refname:short)%00",
                "refs/tags",
            ],
        )
        self.assertEqual(
            self.scm.parse_recent_tags("v3\0\nv2\0\nv1\0\nv0\0\n"),
            ["v3", "v2", "v1"],
        )
        self.assertEqual(
            self.scm.create_branch_argv("topic"),
            ["git", "switch", "--no-track", "-c", "topic"],
        )
        self.assertEqual(
            self.scm.merge_branch_argv("topic"),
            ["git", "merge", "--no-ff", "--no-commit", "--no-edit", "topic"],
        )
        self.assertEqual(
            self.scm.parse_merge_conflicts("a.py\0dir/b.py\0"),
            ["a.py", "dir/b.py"],
        )
        self.assertEqual(
            self.scm.tag_argv("v1"),
            ["git", "tag", "-a", "-m", "Tag v1", "--", "v1"],
        )


if __name__ == "__main__":
    unittest.main()
