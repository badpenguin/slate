"""Common immutable source-control data and adapter protocol."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class RepositoryRef:
    """Identify one repository by project-relative path and SCM type."""

    # 2026-08-18: path alone is ambiguous when Git and HG coexist, so identity
    # always carries both fields and never falls back to an untyped path string.
    path: str
    scm_type: str


@dataclass(frozen=True)
class RepositorySyncStatus:
    """Describe the last explicit comparison with a configured remote."""

    # 2026-08-19: remote verification is deliberately explicit and ephemeral;
    # a small normalized value lets Git and Mercurial share one stable UI label.
    state: str = "unverified"
    ahead: int = 0
    behind: int = 0


@dataclass(frozen=True)
class FileStatus:
    """Represent one normalized path state independently from its SCM."""

    path: str
    state: str
    staged: bool = False
    repository: str = "."
    source_path: str | None = None
    scm_type: str = "hg"

    def operation_paths(self) -> tuple[str, ...]:
        """Return every SCM path represented by this visible status row."""

        # 2026-08-17: a move is one UI row but its SCM must receive both
        # endpoints, otherwise a path-limited commit can leave the removal pending.
        if self.state == "moved" and self.source_path:
            return (self.source_path, self.path)
        return (self.path,)


@dataclass(frozen=True)
class BranchTarget:
    """Represent one local branch and its immutable merge/switch revision."""

    # 2026-08-18: adapters normalize branch choices so dialogs never parse
    # display output or choose a mutable Mercurial head by name after preflight.
    name: str
    revision: str = ""


class SCM(ABC):
    """Describe commands and parsers without executing blocking work."""

    kind = ""
    display_name = "SCM"
    environment = {"LC_ALL": "C"}

    def __init__(self, root: str) -> None:
        """Store the canonical working-copy root."""

        self.root = root

    @abstractmethod
    def status_argv(self, paths: Sequence[str] = ()) -> list[str]:
        """Return stable status arguments, optionally limited to paths."""

    @abstractmethod
    def parse_status(self, output: str) -> list[FileStatus]:
        """Parse machine-readable status output."""

    @abstractmethod
    def ignored_argv(self) -> list[str]:
        """Return the command that enumerates ignored paths."""

    @abstractmethod
    def parse_ignored(self, output: str) -> set[str]:
        """Parse ignored paths without assuming newline-safe filenames."""

    @abstractmethod
    def branch_argv(self) -> list[str]:
        """Return the command that reads the current local branch."""

    @abstractmethod
    def recent_tags_argv(self) -> list[str]:
        """Return a local-only command that lists the most recent tags."""

    @abstractmethod
    def parse_recent_tags(self, output: str) -> list[str]:
        """Parse at most three recent tag names from stable command output."""

    @abstractmethod
    def is_locked(self) -> bool:
        """Return whether a repository transaction lock currently exists."""

    @abstractmethod
    def commit_argv(self, message: str, paths: Sequence[str]) -> list[str]:
        """Return an explicit-path commit command."""

    @abstractmethod
    def record_removal_argv(self, paths: Sequence[str]) -> list[str]:
        """Return a command that records files already removed from disk."""
