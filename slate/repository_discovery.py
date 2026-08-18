"""Incrementally discover supported working copies below one project root."""

from __future__ import annotations

from collections import deque
import os
from pathlib import Path
from typing import Callable

import gi

gi.require_version("GLib", "2.0")
from gi.repository import GLib  # noqa: E402

from .scm.base import RepositoryRef
from .scm.detect import is_normal_repository


class RepositoryDiscovery:
    """Scan a project in bounded idle batches without blocking the GTK loop."""

    BATCH_SIZE = 32
    BUDGET_US = 5_000
    HARD_PRUNE = {
        "node_modules",
        "vendor",
        "dist",
        "build",
        ".venv",
        "__pycache__",
        ".cache",
        ".git",
        ".svn",
    }

    def __init__(
        self,
        project_root: str,
        excluded: set[RepositoryRef],
        on_found: Callable[[RepositoryRef], None],
        on_complete: Callable[[str | None], None],
    ) -> None:
        """Store callbacks and queue the canonical project root for scanning."""

        self.root = Path(project_root).resolve()
        self.excluded = set(excluded)
        self.on_found = on_found
        self.on_complete = on_complete
        self.queue: deque[Path] = deque([self.root])
        self.source_id: int | None = None
        self.cancelled = False
        self.first_error: str | None = None

    def start(self) -> None:
        """Begin discovery on the next GTK idle iteration."""

        if self.source_id is None and not self.cancelled:
            self.source_id = GLib.idle_add(self._scan_batch)

    def cancel(self) -> None:
        """Stop a pending scan without publishing a completion event."""

        self.cancelled = True
        if self.source_id is not None:
            GLib.source_remove(self.source_id)
            self.source_id = None
        self.queue.clear()

    def _scan_batch(self) -> bool:
        """Inspect a bounded directory batch and yield back to GTK promptly."""

        if self.cancelled:
            self.source_id = None
            return GLib.SOURCE_REMOVE
        processed = 0
        started_at = GLib.get_monotonic_time()
        # 2026-08-17: scandir is synchronous, therefore both count and time
        # bounds are required to keep large workspaces responsive.
        while (
            self.queue
            and processed < self.BATCH_SIZE
            and GLib.get_monotonic_time() - started_at < self.BUDGET_US
        ):
            directory = self.queue.popleft()
            processed += 1
            relative = self._relative(directory)
            if relative is None or self._is_below_excluded(relative):
                continue
            markers = tuple(
                RepositoryRef(relative, scm_type)
                for scm_type in ("hg", "git")
                if is_normal_repository(directory, scm_type)
            )
            active_markers = tuple(
                reference for reference in markers if reference not in self.excluded
            )
            for reference in active_markers:
                self.on_found(reference)
            # 2026-08-17: normal nested repositories remain scan leaves; only a
            # repository at the project root may contain independent children.
            if relative != "." and markers:
                continue
            try:
                with os.scandir(directory) as entries:
                    for entry in entries:
                        if (
                            entry.name not in self.HARD_PRUNE
                            and entry.name not in {".hg", ".git"}
                            and entry.is_dir(follow_symlinks=False)
                        ):
                            self.queue.append(Path(entry.path))
            except OSError as error:
                # One unreadable subtree must not hide repositories in siblings.
                if self.first_error is None:
                    self.first_error = str(error)
        if self.queue:
            return GLib.SOURCE_CONTINUE
        self.source_id = None
        self.on_complete(self.first_error)
        return GLib.SOURCE_REMOVE

    def _relative(self, directory: Path) -> str | None:
        """Return a safe canonical repository identifier for one directory."""

        try:
            relative = directory.resolve().relative_to(self.root).as_posix()
        except ValueError:
            return None
        return relative or "."

    def _is_below_excluded(self, relative: str) -> bool:
        """Prune descendants of an explicitly excluded repository path."""

        return any(
            relative.startswith(f"{excluded.path}/") for excluded in self.excluded
        )
