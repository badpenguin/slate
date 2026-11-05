"""Mercurial adapter parser and command tests."""

import json
from pathlib import Path
import os
import shutil
import subprocess
import tempfile
import unittest

from slate.scm.base import BranchTarget, FileStatus
from slate.scm.hg import MercurialSCM


class MercurialSCMTest(unittest.TestCase):
    """Verify stable machine formats and explicit mutation boundaries."""

    def setUp(self) -> None:
        """Create an adapter rooted in a disposable directory."""

        self.temporary = tempfile.TemporaryDirectory()
        self.scm = MercurialSCM(self.temporary.name)

    def tearDown(self) -> None:
        """Release the disposable adapter directory."""

        self.temporary.cleanup()

    def test_status_parser_supports_special_paths(self) -> None:
        """JSON parsing preserves spaces, Unicode and embedded newlines."""

        output = json.dumps(
            [
                {"status": "M", "path": "dir/file name.py"},
                {"status": "?", "path": "à capo\nfile.txt"},
                {"status": "!", "path": "missing.txt"},
                {
                    "status": "A",
                    "path": "new name.txt",
                    "source": "old name.txt",
                },
            ]
        )
        self.assertEqual(
            self.scm.parse_status(output),
            [
                FileStatus("dir/file name.py", "modified"),
                FileStatus("à capo\nfile.txt", "untracked"),
                FileStatus("missing.txt", "missing"),
                FileStatus(
                    "new name.txt", "added", source_path="old name.txt"
                ),
            ],
        )

    def test_commands_use_machine_output_and_path_separator(self) -> None:
        """Status and commit commands remain locale-independent and unambiguous."""

        self.assertEqual(
            self.scm.status_argv(), ["hg", "status", "--copies", "-Tjson"]
        )
        self.assertEqual(
            self.scm.status_argv(["dir/a b.py", "--strano"]),
            [
                "hg",
                "status",
                "--copies",
                "-Tjson",
                "--",
                'path:dir/a b.py',
                'path:--strano',
            ],
        )
        self.assertEqual(
            self.scm.commit_argv("messaggio", ["a b", "--strano"]),
            ["hg", "commit", "-m", "messaggio", "--", 'path:a b', 'path:--strano'],
        )
        self.assertEqual(self.scm.environment["HGPLAIN"], "1")
        self.assertEqual(self.scm.environment["LC_ALL"], "C")

    def test_pattern_prefixes_are_literal_in_every_selected_path_operation(self) -> None:
        """Mercurial pattern syntax in a filename never broadens a selected operation."""

        paths = ["glob:*.py", "re:.*", "path:literal", "folder/[name].txt"]
        commands = [self.scm.status_argv(paths), self.scm.commit_argv("message", paths),
                    self.scm.add_argv(paths), self.scm.forget_argv(paths),
                    self.scm.record_removal_argv(paths), self.scm.revert_argv(paths), self.scm.diff_argv(paths)]
        for command in commands:
            self.assertEqual(command[command.index("--") + 1:], [f"path:{path}" for path in paths])
        for path in paths:
            for command in (self.scm.base_argv(path), self.scm.preview_diff_argv(path), self.scm.editor_diff_argv(path)):
                self.assertEqual(command[command.index("--") + 1:], [f"path:{path}"])
        for command in (self.scm.preview_move_diff_argv(paths[0], paths[-1]),
                        self.scm.editor_move_diff_argv(paths[0], paths[-1])):
            self.assertEqual(command[command.index("--") + 1:], [f"path:{paths[0]}", f"path:{paths[-1]}"])

    def test_literal_selection_does_not_expand_and_template_emits_nul(self) -> None:
        """Read-only Mercurial queries verify literal matching and the binary-safe template."""

        root = Path(__file__).resolve().parent.parent
        if not (root / ".hg").is_dir() or shutil.which("hg") is None:
            self.skipTest("Requires a Mercurial source checkout")
        environment = dict(os.environ)
        environment.update(self.scm.environment)
        template = self.scm.ignored_argv()[4]
        control = subprocess.run(["hg", "files", "-T", template, "--", "path:slate/editor.py"],
                                 cwd=root, env=environment, capture_output=True, check=True)
        self.assertEqual(control.stdout, b"slate/editor.py\0")
        encoded = self.scm.status_argv(["glob:slate/[e]ditor.py"])[-1]
        result = subprocess.run(["hg", "files", "--", encoded], cwd=root, env=environment,
                                capture_output=True)
        self.assertEqual(result.stdout, b"")
        self.assertEqual(result.returncode, 1)

    def test_ignored_names_preserve_newlines_and_other_control_characters(self) -> None:
        """Only NUL terminates a path; embedded newline and carriage return are literal name bytes."""

        paths = {"src\nassets/app.js", "cache\rfile.txt", "space name.txt"}
        self.assertEqual(self.scm.parse_ignored("\0".join(sorted(paths)) + "\0"), paths)
        self.assertEqual(self.scm.parse_ignored(""), set())

    def test_ignore_query_excludes_nested_repositories_as_literal_paths(self) -> None:
        """The parent ignore read must not audit paths inside independent child repos."""

        self.assertEqual(
            self.scm.ignored_argv(["public/backoffice/atk", "folder/[literal]"]),
            ["hg", "status", "-i", "-T", "{path}\\0",
             "--exclude", "path:public/backoffice/atk",
             "--exclude", "path:folder/[literal]"],
        )

    def test_diff_enables_extdiff_without_editing_hgrc(self) -> None:
        """Meld uses the bundled extension only for the launched process."""

        command = self.scm.diff_argv(["file.txt"])
        self.assertEqual(command[:4], ["hg", "--config", "extensions.extdiff=", "extdiff"])
        self.assertIn("--option=--newtab", command)
        self.assertEqual(command[-2:], ["--", 'path:file.txt'])
        self.assertNotIn("--newtab", self.scm.merge_tool_argv())

    def test_preview_add_and_revert_are_explicit_path_commands(self) -> None:
        """Preview and mutations cannot reinterpret filenames as options."""

        self.assertEqual(
            self.scm.preview_diff_argv("--strano.py"),
            [
                "hg",
                "diff",
                "-p",
                "-U",
                "8",
                "--nodates",
                "--",
                'path:--strano.py',
            ],
        )
        self.assertEqual(
            self.scm.add_argv(["nuovo file.py"]),
            ["hg", "add", "--", 'path:nuovo file.py'],
        )
        self.assertEqual(
            self.scm.forget_argv(["aggiunto.py"]),
            ["hg", "forget", "--", 'path:aggiunto.py'],
        )
        self.assertEqual(
            self.scm.record_removal_argv(["rimosso.py"]),
            ["hg", "remove", "--after", "--", 'path:rimosso.py'],
        )
        self.assertEqual(
            self.scm.revert_argv(["a.py"]),
            ["hg", "revert", "--no-backup", "--", 'path:a.py'],
        )

    def test_move_preview_and_operations_include_both_endpoints(self) -> None:
        """A visible move cannot leave its source outside a Mercurial action."""

        status = FileStatus("new.py", "moved", source_path="old.py")
        self.assertEqual(status.operation_paths(), ("old.py", "new.py"))
        self.assertEqual(
            self.scm.preview_move_diff_argv("old.py", "new.py"),
            [
                "hg",
                "diff",
                "--git",
                "-p",
                "-U",
                "8",
                "--nodates",
                "--",
                'path:old.py',
                'path:new.py',
            ],
        )
        self.assertIn("3", self.scm.editor_move_diff_argv("old.py", "new.py"))

    def test_editor_diff_uses_less_context_than_preview(self) -> None:
        """Assisted editing requests three lines without changing preview context."""

        self.assertIn("8", self.scm.preview_diff_argv("file.py"))
        self.assertIn("3", self.scm.editor_diff_argv("file.py"))

    def test_lock_is_read_from_working_copy_metadata(self) -> None:
        """The adapter treats the Mercurial wlock as a transaction semaphore."""

        from pathlib import Path

        metadata = Path(self.temporary.name) / ".hg"
        metadata.mkdir()
        self.assertFalse(self.scm.is_locked())
        (metadata / "wlock").touch()
        self.assertTrue(self.scm.is_locked())

    def test_update_commands_pull_then_target_one_inspected_head(self) -> None:
        """Mercurial pull remains separate from its checked working-copy update."""

        self.assertEqual(self.scm.update_remote_argv(), ["hg", "paths", "default"])
        self.assertEqual(self.scm.pull_argv(), ["hg", "--noninteractive", "pull"])
        self.assertEqual(self.scm.update_heads_argv(), ["hg", "heads", "-Tjson", "."])
        self.assertEqual(
            self.scm.parse_update_heads('[{"node": "abc"}, {"node": "def"}]'),
            ["abc", "def"],
        )
        self.assertEqual(
            self.scm.update_to_argv("abc"),
            ["hg", "--noninteractive", "update", "--check", "--rev", "abc"],
        )

    def test_verification_compares_one_branch_against_default(self) -> None:
        """Incoming and outgoing verification use one explicit remote identity."""

        incoming = self.scm.verify_incoming_argv("feature")
        outgoing = self.scm.verify_outgoing_argv("feature")
        self.assertEqual(incoming[:4], ["hg", "--noninteractive", "incoming", "--quiet"])
        self.assertEqual(outgoing[:4], ["hg", "--noninteractive", "outgoing", "--quiet"])
        self.assertEqual(incoming[-1], "default")
        self.assertEqual(outgoing[-1], "default")
        self.assertEqual(self.scm.parse_verify_count(f"{'a' * 40}\n{'b' * 40}\n"), 2)
        with self.assertRaises(ValueError):
            self.scm.parse_verify_count("not-a-node\n")

    def test_repository_actions_remain_unambiguous_and_never_force(self) -> None:
        """HG actions target exact local names and use only normal push/tag modes."""

        self.assertEqual(self.scm.push_argv(), ["hg", "--noninteractive", "push"])
        self.assertEqual(
            self.scm.branch_heads_argv("--strano"),
            ["hg", "heads", "-Tjson", "--", '--strano'],
        )
        self.assertEqual(
            self.scm.parse_branches(
                '[{"branch":"default","node":"abc","closed":false},'
                '{"branch":"old","node":"def","closed":true}]'
            ),
            [BranchTarget("default", "abc")],
        )
        self.assertEqual(self.scm.recent_tags_argv(), ["hg", "tags", "-Tjson"])
        self.assertEqual(
            self.scm.parse_recent_tags(
                '[{"tag":"tip","rev":9},{"tag":"v3","rev":8},'
                '{"tag":"v2","rev":7},{"tag":"v1","rev":6},'
                '{"tag":"v0","rev":5}]'
            ),
            ["v3", "v2", "v1"],
        )
        self.assertEqual(
            self.scm.merge_branch_argv("abc"),
            [
                "hg",
                "--noninteractive",
                "merge",
                "--tool",
                "internal:merge",
                "--rev",
                "abc",
            ],
        )
        self.assertEqual(
            self.scm.parse_merge_conflicts(
                '[{"path":"a.py","mergestatus":"U"},'
                '{"path":"b.py","mergestatus":"R"}]'
            ),
            ["a.py"],
        )
        self.assertEqual(
            self.scm.tag_argv("v1"),
            ["hg", "--noninteractive", "tag", "--", "v1"],
        )


if __name__ == "__main__":
    unittest.main()
