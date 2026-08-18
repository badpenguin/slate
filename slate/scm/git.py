"""Git adapter for SLATE's intentionally Mercurial-like local workflow."""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

from .base import BranchTarget, FileStatus, SCM


class GitSCM(SCM):
    """Build and parse Git commands without exposing staging as a UI concept."""

    # 2026-08-17: status and commit deliberately collapse Git's two state
    # columns into the Mercurial-like explicit-path workflow chosen for SLATE.
    kind = "git"
    display_name = "Git"
    environment = {"GIT_OPTIONAL_LOCKS": "0", "LC_ALL": "C"}

    def status_argv(self, paths: Sequence[str] = ()) -> list[str]:
        """Return deterministic NUL-delimited status arguments for optional paths."""

        return [
            "git",
            "status",
            "--porcelain=v2",
            "-z",
            "--untracked-files=all",
            "--find-renames=50%",
            *(["--", *paths] if paths else []),
        ]

    def parse_status(self, output: str) -> list[FileStatus]:
        """Collapse index/worktree columns and retain atomic rename endpoints."""

        records = output.split("\0")
        statuses: list[FileStatus] = []
        index = 0
        while index < len(records):
            record = records[index]
            index += 1
            if not record or record.startswith("# "):
                continue
            if record.startswith("? "):
                statuses.append(
                    FileStatus(record[2:], "untracked", scm_type=self.kind)
                )
                continue
            if record.startswith("u "):
                fields = record.split(" ", 10)
                if len(fields) == 11:
                    statuses.append(
                        FileStatus(fields[10], "conflict", scm_type=self.kind)
                    )
                continue
            if record.startswith("2 "):
                fields = record.split(" ", 9)
                source = records[index] if index < len(records) else ""
                index += 1
                if len(fields) == 10 and source:
                    state = "moved" if fields[8].startswith("R") else "added"
                    statuses.append(
                        FileStatus(
                            fields[9],
                            state,
                            source_path=source,
                            scm_type=self.kind,
                        )
                    )
                continue
            if not record.startswith("1 "):
                continue
            fields = record.split(" ", 8)
            if len(fields) != 9:
                continue
            state = self._normalized_state(fields[1][0], fields[1][1])
            if state is not None:
                statuses.append(FileStatus(fields[8], state, scm_type=self.kind))
        return statuses

    @staticmethod
    def _normalized_state(index_state: str, worktree_state: str) -> str | None:
        """Map both Git status columns to one visible SLATE state."""

        if "U" in (index_state, worktree_state):
            return "conflict"
        if "A" in (index_state, worktree_state):
            return "added"
        if "D" in (index_state, worktree_state):
            return "removed"
        if any(code not in {".", " "} for code in (index_state, worktree_state)):
            return "modified"
        return None

    def ignored_argv(self) -> list[str]:
        """Return ignored untracked paths in an unambiguous machine format."""

        return [
            "git",
            "ls-files",
            "--others",
            "--ignored",
            "--exclude-standard",
            "-z",
        ]

    def parse_ignored(self, output: str) -> set[str]:
        """Parse NUL-delimited ignored paths."""

        return {path.rstrip("/") for path in output.split("\0") if path}

    def branch_argv(self) -> list[str]:
        """Return a branch query that succeeds with empty output when detached."""

        return ["git", "branch", "--show-current"]

    def update_merge_state_argv(self) -> list[str]:
        """Return a quiet query for a pending merge parent."""

        return ["git", "rev-parse", "--quiet", "--verify", "MERGE_HEAD"]

    def update_tracked_status_argv(self) -> list[str]:
        """Return tracked index/worktree changes without enumerating new files."""

        return [
            "git",
            "status",
            "--porcelain=v2",
            "-z",
            "--untracked-files=no",
        ]

    def update_current_branch_argv(self) -> list[str]:
        """Return the current branch or fail for a detached HEAD."""

        return ["git", "symbolic-ref", "--quiet", "--short", "HEAD"]

    def update_upstream_argv(self) -> list[str]:
        """Return the configured upstream reference of the current branch."""

        return [
            "git",
            "rev-parse",
            "--abbrev-ref",
            "--symbolic-full-name",
            "@{upstream}",
        ]

    def fetch_argv(self) -> list[str]:
        """Return a fetch that never initializes or updates submodules."""

        return ["git", "fetch", "--no-recurse-submodules"]

    def remotes_argv(self) -> list[str]:
        """Return configured remote names without contacting them."""

        # 2026-08-18: repository actions require existing Git configuration;
        # they never infer origin/upstream or alter branch tracking themselves.
        return ["git", "remote"]

    def push_argv(self) -> list[str]:
        """Push the upstream branch and reachable annotated tags without force."""

        return ["git", "push", "--follow-tags"]

    def branches_argv(self) -> list[str]:
        """Return NUL-delimited local branch names only."""

        return [
            "git",
            "for-each-ref",
            "--format=%(refname:short)%00",
            "refs/heads",
        ]

    @staticmethod
    def parse_branches(output: str) -> list[BranchTarget]:
        """Parse local Git branches without exposing remote-tracking refs."""

        # Git appends a record newline even when the custom field ends in NUL;
        # ref names cannot contain newlines, so remove only that framing byte.
        names = [record.strip("\n") for record in output.split("\0")]
        return [BranchTarget(name) for name in names if name]

    @staticmethod
    def recent_tags_argv() -> list[str]:
        """Return the three newest local tags by their creator timestamp."""

        # 2026-08-18: Git crea tag annotati in SLATE, quindi creatordate riflette
        # il momento di assegnazione senza leggere o modificare alcun remote.
        return [
            "git",
            "for-each-ref",
            "--sort=-creatordate",
            "--count=3",
            "--format=%(refname:short)%00",
            "refs/tags",
        ]

    @staticmethod
    def parse_recent_tags(output: str) -> list[str]:
        """Parse up to three NUL-delimited Git tag names."""

        # Il NUL conserva correttamente ogni nome valido indipendentemente dagli
        # spazi; il limite resta anche nel parser come difesa dell'interfaccia.
        names = [record.strip("\n") for record in output.split("\0")]
        return [name for name in names if name][:3]

    @staticmethod
    def create_branch_argv(name: str) -> list[str]:
        """Create and switch to a local branch without tracking inference."""

        return ["git", "switch", "--no-track", "-c", name]

    @staticmethod
    def switch_branch_argv(name: str) -> list[str]:
        """Switch to one exact local branch without remote name guessing."""

        return ["git", "switch", "--no-guess", "--no-recurse-submodules", name]

    @staticmethod
    def merge_branch_argv(name: str) -> list[str]:
        """Start an explicit non-fast-forward local branch merge without commit."""

        return ["git", "merge", "--no-ff", "--no-commit", "--no-edit", name]

    def merge_conflicts_argv(self) -> list[str]:
        """Return NUL-delimited unresolved paths from the Git index."""

        return ["git", "diff", "--name-only", "-z", "--diff-filter=U"]

    @staticmethod
    def parse_merge_conflicts(output: str) -> list[str]:
        """Parse unresolved Git paths without relying on display text."""

        return [path for path in output.split("\0") if path]

    def merge_tool_argv(self) -> list[str]:
        """Open Meld sequentially for unresolved Git merge paths."""

        return ["git", "mergetool", "--no-prompt", "--tool=meld"]

    @staticmethod
    def tag_argv(name: str) -> list[str]:
        """Create one annotated Git tag with a deterministic short message."""

        return ["git", "tag", "-a", "-m", f"Tag {name}", "--", name]

    @staticmethod
    def update_comparison_argv(upstream: str) -> list[str]:
        """Return left/right commit counts between HEAD and its upstream."""

        return ["git", "rev-list", "--left-right", "--count", f"HEAD...{upstream}"]

    @staticmethod
    def parse_update_comparison(output: str) -> tuple[int, int]:
        """Parse local-ahead and remote-ahead counts from rev-list."""

        fields = output.split()
        if len(fields) != 2:
            raise ValueError("conteggio Git locale/remoto non valido")
        return int(fields[0]), int(fields[1])

    @staticmethod
    def fast_forward_argv(upstream: str) -> list[str]:
        """Return an update that refuses every non-fast-forward history."""

        return ["git", "merge", "--ff-only", upstream]

    def is_locked(self) -> bool:
        """Return whether a normal Git working copy owns its index lock."""

        return (Path(self.root) / ".git" / "index.lock").exists()

    def commit_argv(self, message: str, paths: Sequence[str]) -> list[str]:
        """Commit complete current content of only explicitly selected paths."""

        return ["git", "commit", "--only", "-m", message, "--", *paths]

    def add_argv(self, paths: Sequence[str]) -> list[str]:
        """Add only explicitly selected untracked paths."""

        return ["git", "add", "--", *paths]

    def forget_argv(self, paths: Sequence[str]) -> list[str]:
        """Remove added paths from the index while preserving disk content."""

        return ["git", "rm", "--cached", "-f", "--", *paths]

    def record_removal_argv(self, paths: Sequence[str]) -> list[str]:
        """Stage only explicitly selected deletions already made by the UI."""

        return ["git", "add", "-u", "--", *paths]

    def revert_argv(self, paths: Sequence[str]) -> list[str]:
        """Discard selected tracked changes from index and working tree."""

        return [
            "git",
            "restore",
            "--source=HEAD",
            "--staged",
            "--worktree",
            "--",
            *paths,
        ]

    def base_argv(self, path: str) -> list[str]:
        """Return the HEAD content of one removed path."""

        return ["git", "show", f"HEAD:{path}"]

    def preview_diff_argv(self, path: str) -> list[str]:
        """Return the full current difference from HEAD for one path."""

        return [
            "git",
            "diff",
            "--no-ext-diff",
            "--no-color",
            "--unified=8",
            "HEAD",
            "--",
            path,
        ]

    def preview_move_diff_argv(self, source: str, destination: str) -> list[str]:
        """Return one rename-aware Git diff for both move endpoints."""

        return [
            "git",
            "diff",
            "--no-ext-diff",
            "--no-color",
            "--find-renames=50%",
            "--unified=8",
            "HEAD",
            "--",
            source,
            destination,
        ]

    def diff_argv(self, paths: Sequence[str] = ()) -> list[str]:
        """Return a non-interactive Meld directory-diff invocation."""

        return [
            "git",
            "difftool",
            "--no-prompt",
            "--tool=meld",
            "--dir-diff",
            "HEAD",
            *(["--", *paths] if paths else []),
        ]
