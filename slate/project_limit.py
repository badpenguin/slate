"""Coordinate the revision limit across all repositories of a project."""

from typing import Callable

from .scm.base import FileStatus, RepositoryRef


class ProjectStatusLimit:
    """Keep blocked projects suspended until a complete manual scan succeeds."""

    LIMIT = 1000

    def __init__(self, on_block: Callable[[str], None], on_ready: Callable[[str], None]) -> None:
        """Bind lifecycle hooks without starting processes or changing config."""

        self.on_block = on_block
        self.on_ready = on_ready
        self.blocked: set[str] = set()
        self.pending: dict[str, set[RepositoryRef]] = {}
        self.snapshots: dict[str, dict[RepositoryRef, tuple[list[FileStatus], str]]] = {}

    def accept(self, project: str, repository: RepositoryRef,
               statuses: list[FileStatus], branch: str) -> bool:
        """Check the aggregate before any GTK insertion or publish recovery atomically."""

        if project in self.blocked and project not in self.pending:
            return False
        snapshots = self.snapshots.setdefault(project, {})
        # 2026-10-23: count the replacement snapshot, not the sum of events;
        # different repositories must never each consume a separate allowance.
        count = len(statuses) + sum(len(value[0]) for ref, value in snapshots.items() if ref != repository)
        if count >= self.LIMIT:
            self.blocked.add(project)
            self.pending.pop(project, None)
            self.on_block(project)
            return False
        snapshots[repository] = (statuses, branch)
        if project in self.pending:
            self.pending[project].discard(repository)
            if not self.pending[project]:
                self.pending.pop(project)
                self.blocked.discard(project)
                self.on_ready(project)
            return False
        return True

    def begin_scan(self, project: str, repositories: set[RepositoryRef]) -> None:
        """Stage fresh snapshots while all automatic observers remain suspended."""

        self.snapshots[project] = {}
        self.pending[project] = set(repositories)
        if not repositories:
            self.pending.pop(project)
            self.blocked.discard(project)
            self.on_ready(project)

    def fail_scan(self, project: str) -> None:
        """Retain the block after a failed probe without classifying normal errors as overload."""

        if project in self.pending:
            self.pending.pop(project)
            self.blocked.add(project)
            self.on_block(project)

    def remove_repository(self, project: str, repository: RepositoryRef) -> None:
        """Stop counting a repository explicitly removed from the project."""

        self.snapshots.get(project, {}).pop(repository, None)

    def forget_project(self, project: str) -> None:
        """Release overload state when the project itself is removed or reset."""

        self.snapshots.pop(project, None)
        self.pending.pop(project, None)
        self.blocked.discard(project)
