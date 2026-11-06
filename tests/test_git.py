"""Git adapter parser and explicit-command tests."""

import os
import shutil
import subprocess
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

    def test_ignore_query_excludes_literal_nested_repository_paths(self) -> None:
        """Git applies the same repository boundary without treating names as globs."""

        self.assertEqual(self.scm.ignored_argv(["nested/[literal]"])[-2:],
                         ["--", ":(top,exclude,literal)nested/[literal]"])

    def test_status_collapses_index_and_worktree_without_staging_sections(self) -> None:
        """Both Git columns map to one selectable normalized status list."""

        output = (
            "1 .M N... 100644 100644 100644 aaa bbb file one.py\0"
            "1 M. N... 100644 100644 100644 aaa bbb staged.py\0"
            "1 D. N... 100644 000000 000000 aaa 000 removed.py\0"
            "1 .D N... 100644 100644 000000 aaa bbb missing.py\0"
            "u UU N... 100644 100644 100644 100644 a b c conflict.py\0"
            "? nuovo file.txt\0"
        )
        self.assertEqual(
            self.scm.parse_status(output),
            [
                FileStatus("file one.py", "modified", scm_type="git"),
                FileStatus("staged.py", "modified", scm_type="git"),
                FileStatus("removed.py", "removed", scm_type="git"),
                FileStatus("missing.py", "missing", scm_type="git"),
                FileStatus("conflict.py", "conflict", scm_type="git"),
                FileStatus("nuovo file.txt", "untracked", scm_type="git"),
            ],
        )

    def test_deleted_staged_addition_is_missing(self) -> None:
        """Real Git AD status exposes the missing-file action instead of Added."""

        def git(*arguments: str) -> str:
            """Run Git only inside this test's disposable repository."""

            return subprocess.check_output(
                ["git", "-C", self.temporary.name, *arguments], stderr=subprocess.STDOUT
            ).decode()

        git("init", "-q")
        target = Path(self.temporary.name) / "new.txt"
        target.write_text("new\n")
        git("add", "--", "new.txt")
        target.unlink()
        output = git(*self.scm.status_argv()[1:])
        self.assertIn("1 AD ", output)
        self.assertEqual(self.scm.parse_status(output),
                         [FileStatus("new.txt", "missing", scm_type="git")])

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
                ":(top,literal)a b.py",
            ],
        )
        self.assertEqual(
            self.scm.commit_argv("messaggio", ("a b.py",)),
            ["git", "commit", "--only", "-m", "messaggio", "--", ":(top,literal)a b.py"],
        )
        self.assertEqual(
            self.scm.add_argv(("nuovo.py",)),
            ["git", "add", "--", ":(top,literal)nuovo.py"],
        )
        self.assertEqual(
            self.scm.record_removal_argv(("rimosso.py",)),
            ["git", "add", "-u", "--", ":(top,literal)rimosso.py"],
        )
        self.assertEqual(self.scm.environment["GIT_OPTIONAL_LOCKS"], "0")
        self.assertIn(
            "--extcmd=meld --newtab", self.scm.diff_argv(("a b.py",))
        )
        self.assertNotIn("--newtab", self.scm.merge_tool_argv())
        self.assertEqual(
            self.scm.diff_argv(("a b.py",))[-3:], ["HEAD", "--", ":(top,literal)a b.py"]
        )

    def test_every_path_operation_encodes_special_names_as_literals(self) -> None:
        """Wildcards and pathspec prefixes cannot expand an explicit UI selection."""

        paths = ["src/[id]/page.tsx", "file?.txt", "file*.txt", ":(exclude)*"]
        commands = [self.scm.status_argv(paths), self.scm.commit_argv("message", paths),
                    self.scm.add_argv(paths), self.scm.forget_argv(paths),
                    self.scm.record_removal_argv(paths), self.scm.revert_argv(paths), self.scm.diff_argv(paths)]
        for command in commands:
            self.assertEqual(command[command.index("--") + 1:], [f":(top,literal){path}" for path in paths])
        for path in paths:
            for command in (self.scm.preview_diff_argv(path), self.scm.editor_diff_argv(path)):
                self.assertEqual(command[command.index("--") + 1:], [f":(top,literal){path}"])
        for command in (self.scm.preview_move_diff_argv(paths[0], paths[-1]),
                        self.scm.editor_move_diff_argv(paths[0], paths[-1])):
            self.assertEqual(command[command.index("--") + 1:],
                             [f":(top,literal){paths[0]}", f":(top,literal){paths[-1]}"])

    def test_literal_selection_does_not_match_another_tracked_filename(self) -> None:
        """A real read-only Git query cannot interpret [e] as a match for editor.py."""

        root = Path(__file__).resolve().parent.parent
        if not (root / ".git").is_dir() or shutil.which("git") is None:
            self.skipTest("Requires a normal Git source checkout")
        environment = dict(os.environ)
        environment.update(self.scm.environment)
        control = subprocess.run(["git", "ls-files", "--", "slate/editor.py"], cwd=root, env=environment,
                                 capture_output=True, text=True, check=True)
        if control.stdout.strip() != "slate/editor.py":
            self.skipTest("Source file is not tracked in this checkout")
        encoded = self.scm.status_argv(["slate/[e]ditor.py"])[-1]
        result = subprocess.run(["git", "ls-files", "--", encoded], cwd=root, env=environment,
                                capture_output=True, text=True, check=True)
        self.assertEqual(result.stdout, "")

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

    def test_editor_diff_uses_less_context_than_preview(self) -> None:
        """Assisted editing requests three lines without changing preview context."""

        self.assertIn("--unified=8", self.scm.preview_diff_argv("file.py"))
        self.assertIn("--unified=3", self.scm.editor_diff_argv("file.py"))
        self.assertIn(
            "--unified=3",
            self.scm.editor_move_diff_argv("old.py", "new.py"),
        )

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

    def test_branch_tag_collision_preserves_exact_switch_and_merge_targets(self) -> None:
        """Real Git switches and merges the selected branch despite an identically named tag."""

        def git(*arguments: str) -> str:
            """Execute Git inside the disposable test working copy."""

            return subprocess.check_output(
                ["git", "-C", self.temporary.name, *arguments], stderr=subprocess.STDOUT
            ).decode()

        git("init", "-q", "-b", "main")
        git("config", "user.name", "Audit")
        git("config", "user.email", "audit@example.invalid")
        root = Path(self.temporary.name)
        (root / "base.txt").write_text("base\n")
        git("add", "base.txt")
        git("commit", "-qm", "Create base")
        git("tag", "topic")
        git("branch", "heads/topic")
        git("switch", "-c", "topic")
        (root / "branch.txt").write_text("branch version\n")
        git("add", "branch.txt")
        git("commit", "-qm", "Add branch content")
        git("switch", "main")
        branches = self.scm.parse_branches(git(*self.scm.branches_argv()[1:]))
        self.assertEqual({target.name for target in branches}, {"main", "topic", "heads/topic"})
        for name in ("topic", "heads/topic"):
            with self.subTest(branch=name):
                git(*self.scm.switch_branch_argv(name)[1:])
                self.assertEqual(git("branch", "--show-current").strip(), name)
        git("switch", "main")
        git(*self.scm.merge_branch_argv("topic")[1:])
        self.assertEqual((root / "branch.txt").read_text(), "branch version\n")
        self.assertEqual(git("rev-parse", "MERGE_HEAD"), git("rev-parse", "refs/heads/topic"))
        self.assertNotEqual(git("rev-parse", "MERGE_HEAD"), git("rev-parse", "refs/tags/topic"))

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
            ["git", "merge", "--no-ff", "--no-commit", "--no-edit", "refs/heads/topic"],
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
