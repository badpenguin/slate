"""Mercurial command definitions and stable output parsers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Sequence

from .base import BranchTarget, FileStatus, SCM


class MercurialSCM(SCM):
    """Provide all Mercurial-specific commands without running them directly."""

    kind = "hg"
    display_name = "Mercurial"
    environment = {"HGPLAIN": "1", "LC_ALL": "C"}

    STATE_MAP = {
        "M": "modified",
        "A": "added",
        "R": "removed",
        "!": "removed",
        "?": "untracked",
        "C": "clean",
    }

    def status_argv(self, paths: Sequence[str] = ()) -> list[str]:
        """Return JSON status arguments, optionally limited to explicit paths."""

        return [
            "hg",
            "status",
            "--copies",
            "-Tjson",
            *(["--", *paths] if paths else []),
        ]

    def parse_status(self, output: str) -> list[FileStatus]:
        """Parse Mercurial JSON while tolerating future unknown state codes."""

        records = json.loads(output or "[]")
        statuses: list[FileStatus] = []
        for record in records:
            code = str(record.get("status", ""))
            path = str(record.get("path", ""))
            if path and code in self.STATE_MAP and code != "C":
                source = record.get("source")
                statuses.append(
                    FileStatus(
                        path,
                        self.STATE_MAP[code],
                        source_path=str(source) if source else None,
                    )
                )
        return statuses

    def ignored_argv(self) -> list[str]:
        """Return the plain path-only ignored-file query."""

        return ["hg", "status", "-i", "-T", "{path}\\n"]

    def parse_ignored(self, output: str) -> set[str]:
        """Parse Mercurial's plain newline-delimited ignored paths."""

        return {line.rstrip("/") for line in output.splitlines() if line}

    def branch_argv(self) -> list[str]:
        """Return the command for the current named branch."""

        return ["hg", "branch"]

    def update_merge_state_argv(self) -> list[str]:
        """Return a query that emits the second parent only during a merge."""

        return ["hg", "log", "-r", "p2()", "-T", "{node}\n"]

    def update_tracked_status_argv(self) -> list[str]:
        """Return machine-readable tracked changes for update preflight."""

        return ["hg", "status", "-mard", "-Tjson"]

    def update_current_node_argv(self) -> list[str]:
        """Return the immutable working-copy parent before pulling."""

        return ["hg", "log", "-r", ".", "-T", "{node}\n"]

    def update_remote_argv(self) -> list[str]:
        """Return the configured default pull path or fail when it is absent."""

        return ["hg", "paths", "default"]

    def pull_argv(self) -> list[str]:
        """Return a non-interactive pull that leaves the working copy unchanged."""

        return ["hg", "--noninteractive", "pull"]

    @staticmethod
    def verify_incoming_argv(branch: str) -> list[str]:
        """Return a quiet remote comparison for incoming branch changesets."""

        return [
            "hg",
            "--noninteractive",
            "incoming",
            "--quiet",
            "--branch",
            branch,
            "--template",
            "{node}\n",
            "default",
        ]

    @staticmethod
    def verify_outgoing_argv(branch: str) -> list[str]:
        """Return a quiet remote comparison for outgoing branch changesets."""

        # 2026-08-19: both directions target default explicitly so a separate
        # default-push cannot make the two counts describe different servers.
        return [
            "hg",
            "--noninteractive",
            "outgoing",
            "--quiet",
            "--branch",
            branch,
            "--template",
            "{node}\n",
            "default",
        ]

    @staticmethod
    def parse_verify_count(output: str) -> int:
        """Count machine-formatted Mercurial changeset nodes."""

        nodes = [line for line in output.splitlines() if line]
        if any(len(node) != 40 for node in nodes):
            raise ValueError("Invalid Mercurial remote changeset list")
        return len(nodes)

    @staticmethod
    def remote_path_argv(name: str) -> list[str]:
        """Return one configured Mercurial path without interpreting its URL."""

        # 2026-08-18: remote actions use only paths already configured by the
        # user; SLATE never writes hgrc or upgrades a rejected push to force.
        return ["hg", "paths", name]

    def push_argv(self) -> list[str]:
        """Return the normal non-interactive push without force-like options."""

        return ["hg", "--noninteractive", "push"]

    def branches_argv(self) -> list[str]:
        """Return machine-readable open local named branches."""

        return ["hg", "branches", "-Tjson"]

    @staticmethod
    def parse_branches(output: str) -> list[BranchTarget]:
        """Parse open named branches and their displayed head revisions."""

        records = json.loads(output or "[]")
        return [
            BranchTarget(str(record["branch"]), str(record["node"]))
            for record in records
            if record.get("branch") and record.get("node") and not record.get("closed")
        ]

    @staticmethod
    def recent_tags_argv() -> list[str]:
        """Return Mercurial's revision-ordered local tag metadata."""

        # 2026-08-18: hg tags espone localmente i tag dal changeset più recente;
        # il JSON evita di interpretare l'output destinato alla visualizzazione.
        return ["hg", "tags", "-Tjson"]

    @staticmethod
    def parse_recent_tags(output: str) -> list[str]:
        """Parse up to three real tags, excluding Mercurial's synthetic tip."""

        records = json.loads(output or "[]")
        # "tip" è un alias sintetico di Mercurial e non un tag assegnato.
        return [
            str(record["tag"])
            for record in records
            if record.get("tag") and record.get("tag") != "tip"
        ][:3]

    @staticmethod
    def branch_heads_argv(branch: str) -> list[str]:
        """Return every open head for one explicitly selected named branch."""

        return ["hg", "heads", "-Tjson", "--", branch]

    @staticmethod
    def create_branch_argv(name: str) -> list[str]:
        """Return a named-branch change that preserves working-copy changes."""

        return ["hg", "branch", "--", name]

    @staticmethod
    def merge_branch_argv(node: str) -> list[str]:
        """Start a non-interactive merge using Mercurial's internal merge tool."""

        return [
            "hg",
            "--noninteractive",
            "merge",
            "--tool",
            "internal:merge",
            "--rev",
            node,
        ]

    def merge_conflicts_argv(self) -> list[str]:
        """Return machine-readable files involved in an active Mercurial merge."""

        return ["hg", "resolve", "--list", "-Tjson"]

    @staticmethod
    def parse_merge_conflicts(output: str) -> list[str]:
        """Extract only unresolved Mercurial merge paths."""

        records = json.loads(output or "[]")
        return [
            str(record["path"])
            for record in records
            if record.get("path") and record.get("mergestatus") == "U"
        ]

    def merge_tool_argv(self) -> list[str]:
        """Open Meld for every unresolved Mercurial merge path."""

        return [
            "hg",
            "--noninteractive",
            "resolve",
            "--all",
            "--tool",
            "meld",
        ]

    @staticmethod
    def tag_argv(name: str) -> list[str]:
        """Create one normal global Mercurial tag without force."""

        return ["hg", "--noninteractive", "tag", "--", name]

    def update_heads_argv(self) -> list[str]:
        """Return JSON for every open head on the current named branch."""

        return ["hg", "heads", "-Tjson", "."]

    @staticmethod
    def parse_update_heads(output: str) -> list[str]:
        """Extract immutable head nodes from Mercurial JSON."""

        records = json.loads(output or "[]")
        return [str(record["node"]) for record in records if record.get("node")]

    @staticmethod
    def update_to_argv(node: str) -> list[str]:
        """Return a checked update to one previously inspected head node."""

        return ["hg", "--noninteractive", "update", "--check", "--rev", node]

    def base_argv(self, path: str) -> list[str]:
        """Return a command that reads parent-revision content for one path."""

        return ["hg", "cat", "-r", ".", "--", path]

    def is_locked(self) -> bool:
        """Return whether Mercurial currently owns its working-copy lock."""

        return (Path(self.root) / ".hg" / "wlock").exists()

    def commit_argv(self, message: str, paths: Sequence[str]) -> list[str]:
        """Return a commit constrained to already tracked selected paths."""

        return ["hg", "commit", "-m", message, "--", *paths]

    def revert_argv(self, paths: Sequence[str]) -> list[str]:
        """Return a no-backup revert command for explicitly selected paths."""

        return ["hg", "revert", "--no-backup", "--", *paths]

    def add_argv(self, paths: Sequence[str]) -> list[str]:
        """Return an add command constrained to explicitly selected new paths."""

        return ["hg", "add", "--", *paths]

    def forget_argv(self, paths: Sequence[str]) -> list[str]:
        """Return a forget command that keeps explicitly selected files on disk."""

        return ["hg", "forget", "--", *paths]

    def record_removal_argv(self, paths: Sequence[str]) -> list[str]:
        """Record explicitly selected paths that the UI already deleted."""

        return ["hg", "remove", "--after", "--", *paths]

    def preview_diff_argv(self, path: str) -> list[str]:
        """Return a contextual function-aware unified diff for the preview."""

        return ["hg", "diff", "-p", "-U", "8", "--nodates", "--", path]

    def preview_move_diff_argv(self, source: str, destination: str) -> list[str]:
        """Return one Git-style diff that preserves Mercurial rename metadata."""

        return [
            "hg",
            "diff",
            "--git",
            "-p",
            "-U",
            "8",
            "--nodates",
            "--",
            source,
            destination,
        ]

    def diff_argv(self, paths: Sequence[str] = ()) -> list[str]:
        """Return one Meld directory comparison, optionally path-limited."""

        # 2026-08-16: enable the bundled extension per invocation so the app
        # never edits the user's hgrc merely to launch Meld.
        return [
            "hg",
            "--config",
            "extensions.extdiff=",
            "extdiff",
            "-p",
            "meld",
            *( ["--", *paths] if paths else [] ),
        ]

    def external_tool_argv(self) -> list[str]:
        """Return the TortoiseHg commit-window command."""

        return ["thg", "commit", "-R", self.root]
