"""Recursive event-driven repository monitoring for supported local SCMs."""

from __future__ import annotations

import os
from collections import deque
from functools import partial
from operator import attrgetter
from pathlib import Path
from typing import Callable, Iterable

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

from .processes import AsyncCommand, CommandResult, run_async
from .scm.base import FileStatus, SCM


class RepoWatcher:
    """Watch one repository and publish coherent normalized SCM snapshots."""

    ACTIVE_DEBOUNCE_MS = 100
    INACTIVE_DEBOUNCE_MS = 2000
    INCREMENTAL_PATH_LIMIT = 20
    MOUNT_ACTIVE_BATCH = 8
    MOUNT_INACTIVE_BATCH = 1
    MOUNT_BUDGET_US = 4_000
    HARD_PRUNE = {
        "node_modules",
        "vendor",
        "dist",
        "build",
        ".venv",
        "__pycache__",
        ".cache",
    }

    def __init__(
        self,
        path: str,
        scm: SCM,
        on_status: Callable[[list[FileStatus], str], None],
        on_error: Callable[[str], None] | None = None,
        on_file_change: Callable[[str], None] | None = None,
        on_ignored: Callable[[set[str]], None] | None = None,
        on_history_change: Callable[[], None] | None = None,
    ) -> None:
        """Discover ignores, mount monitors and publish status and file events."""

        self.path = str(Path(path).resolve())
        self.scm = scm
        self.on_status = on_status
        self.on_error = on_error or self._ignore_error
        self.on_file_change = on_file_change or self._ignore_file_change
        self.on_ignored = on_ignored or self._ignore_ignored
        self.on_history_change = on_history_change or self._ignore_history_change
        self.monitors: dict[str, Gio.FileMonitor] = {}
        self.ignored: set[str] = set()
        self.nested_repositories: set[str] = set()
        self.pending_id: int | None = None
        self.mount_idle_id: int | None = None
        self.mute_until = 0
        self.active = False
        self.closed = False
        self.ready = False
        self.waiting_for_unlock = False
        self.paused = False
        # 2026-08-17: explicit remote operations wait for the active automatic
        # read, retain later filesystem work, then request one coherent refresh.
        self.pause_idle_id: int | None = None
        self.pause_callback: Callable[[], None] | None = None
        self.command: AsyncCommand | None = None
        self._mount_queue: deque[str] = deque()
        self._branch = ""
        self._statuses: dict[str, FileStatus] = {}
        self._published_snapshot: (
            tuple[tuple[tuple[str, str, str | None], ...], str] | None
        ) = None
        self._pending_paths: set[str] = set()
        self._pending_full = True
        self._pending_branch = True
        self._pending_ignored = True
        # 2026-08-18: lo status iniziale può precedere la copertura ricorsiva;
        # questi flag chiudono una sola volta la finestra di eventi non osservati.
        self._initial_full_started = False
        self._initial_full_complete = False
        self._initial_mount_complete = False
        self._initial_monitor_reconciliation_required = False
        # 2026-08-17: startup work enters the same per-repository scheduler as
        # later events, preventing the former burst of parallel hg processes.
        self._schedule_pending()

    def set_active(self, active: bool) -> None:
        """Switch pending work between immediate and background scheduling."""

        if self.active == active:
            return
        self.active = active
        if self._has_pending_work():
            self._schedule_pending()

    def set_nested_repositories(self, repositories: set[str]) -> None:
        """Exclude independently watched nested repositories from this watcher."""

        normalized = {path.strip("/") for path in repositories if path.strip("/")}
        if normalized == self.nested_repositories:
            return
        removed_boundaries = self.nested_repositories - normalized
        self.nested_repositories = normalized
        # 2026-08-17: once discovery identifies a nested working copy, the
        # ancestor watcher must release duplicate monitors already mounted there.
        for directory, monitor in tuple(self.monitors.items()):
            relative = os.path.relpath(directory, self.path).replace(os.sep, "/")
            if self._is_nested_repository_path(relative):
                monitor.cancel()
                self.monitors.pop(directory, None)
        self._mount_queue = deque(
            directory
            for directory in self._mount_queue
            if not self._is_nested_repository_path(
                os.path.relpath(directory, self.path).replace(os.sep, "/")
            )
        )
        if removed_boundaries:
            self.request_full()
            return
        # 2026-08-17: newly discovered child repositories can be removed from
        # the cached ancestor snapshot locally, without launching another hg.
        for status_path in tuple(self._statuses):
            if self._is_nested_repository_path(status_path):
                self._statuses.pop(status_path, None)
        if self._published_snapshot is not None:
            self._publish_status()

    def request_paths(self, paths: Iterable[str]) -> None:
        """Queue one path-limited status, promoting large batches to a full scan."""

        if self.closed:
            return
        if self._pending_full:
            self._schedule_pending()
            return
        for path in paths:
            normalized = path.replace(os.sep, "/").strip("/")
            if not normalized or normalized == "." or ".." in normalized.split("/"):
                self.request_full()
                return
            self._add_pending_path(normalized)
            if len(self._pending_paths) >= self.INCREMENTAL_PATH_LIMIT:
                self.request_full()
                return
        if self._pending_paths:
            self._schedule_pending()

    def request_full(
        self,
        *,
        refresh_branch: bool = False,
        refresh_ignored: bool = False,
    ) -> None:
        """Queue a full status and optionally refresh repository-wide metadata."""

        if self.closed:
            return
        self._pending_full = True
        self._pending_paths.clear()
        self._pending_branch = self._pending_branch or refresh_branch
        self._pending_ignored = self._pending_ignored or refresh_ignored
        self._schedule_pending()

    def request_scan(self) -> None:
        """Queue the explicit full status, branch and ignore refresh."""

        self.request_full(
            refresh_branch=True,
            refresh_ignored=True,
        )

    def mute_metadata_events(self, milliseconds: int = 500) -> None:
        """Suppress self-generated SCM metadata events for a short window."""

        now = GLib.get_monotonic_time() // 1000
        self.mute_until = max(self.mute_until, now + milliseconds)

    def pause_after_current(self, callback: Callable[[], None]) -> None:
        """Pause automatic commands after the currently running read completes."""

        if self.closed:
            return
        self.paused = True
        self.pause_callback = callback
        if self.pending_id is not None:
            GLib.source_remove(self.pending_id)
            self.pending_id = None
        self.ready = False
        if self.command is None:
            self._schedule_pause_ready()

    def resume_with_full_refresh(self, *, refresh_branch: bool = True) -> None:
        """Resume automatic work with one complete post-operation snapshot."""

        if self.closed:
            return
        self.paused = False
        self.pause_callback = None
        if self.pause_idle_id is not None:
            GLib.source_remove(self.pause_idle_id)
            self.pause_idle_id = None
        self.request_full(refresh_branch=refresh_branch)

    def close(self) -> None:
        """Cancel timers, monitors and commands owned by this watcher."""

        self.closed = True
        for source_id in (
            self.pending_id,
            self.mount_idle_id,
            self.pause_idle_id,
        ):
            if source_id is not None:
                GLib.source_remove(source_id)
        self.pending_id = self.mount_idle_id = self.pause_idle_id = None
        self.pause_callback = None
        for monitor in self.monitors.values():
            monitor.cancel()
        self.monitors.clear()
        if self.command is not None:
            self.command.cancel()
            self.command = None

    def _run(
        self,
        argv: list[str],
        callback: Callable[[CommandResult], None],
    ) -> None:
        """Start the sole automatic repository command and retain its lifetime."""

        def completed(result: CommandResult) -> None:
            """Release the command, apply its result and continue queued work."""

            self.command = None
            if not self.closed:
                callback(result)
                if self.paused:
                    self._schedule_pause_ready()
                else:
                    self._start_next()

        self.command = run_async(
            argv, completed, cwd=self.path, env=self.scm.environment
        )

    def _on_ignored(self, result: CommandResult) -> None:
        """Store ignored paths and begin incremental directory mounting."""

        if result.ok:
            try:
                self.ignored = self.scm.parse_ignored(result.stdout)
            except (ValueError, TypeError) as error:
                self.on_error(
                    f"Invalid {self.scm.display_name} ignored files: {error}"
                )
        else:
            self.on_error(self._describe_error("Reading ignored files", result))
        # 2026-08-16: il file manager riusa la stessa classificazione del
        # watcher, evitando un secondo comando VCS e filtri divergenti.
        self.on_ignored(set(self.ignored))
        self._queue_mount(self.path)

    def _on_branch(self, result: CommandResult) -> None:
        """Cache the named branch without coupling it to every status request."""

        if result.ok:
            self._branch = result.stdout.strip()
        else:
            self.on_error(
                self._describe_error(
                    f"Reading {self.scm.display_name} branch", result
                )
            )

    def _on_status(self, paths: tuple[str, ...], result: CommandResult) -> None:
        """Merge a full or path-limited SCM result into the stable snapshot."""

        if not result.ok:
            self.on_error(
                self._describe_error(
                    f"Updating {self.scm.display_name}", result
                )
            )
            if not paths and not self._initial_full_complete:
                self._complete_initial_full_status()
            return
        try:
            statuses = self.scm.parse_status(result.stdout)
        except (ValueError, TypeError) as error:
            self.on_error(f"Invalid {self.scm.display_name} status: {error}")
            if not paths and not self._initial_full_complete:
                self._complete_initial_full_status()
            return
        if paths:
            # 2026-08-17: an empty incremental result means that every cached
            # row below the queried roots became clean and must disappear.
            for cached_path in tuple(self._statuses):
                if any(
                    cached_path == root or cached_path.startswith(f"{root}/")
                    for root in paths
                ):
                    self._statuses.pop(cached_path, None)
        else:
            self._statuses.clear()
        self._statuses.update({status.path: status for status in statuses})
        if paths and any(status.source_path for status in statuses):
            # 2026-08-17: a path-limited result exposes the copy source but not
            # whether that source is removed. Publish only after a full, coherent
            # snapshot can distinguish a copy from a recorded move.
            self.request_full()
            return
        self._publish_status()
        if not paths and not self._initial_full_complete:
            self._complete_initial_full_status()

    def _complete_initial_full_status(self) -> None:
        """Record the first full status and reconcile any incomplete monitor mount."""

        self._initial_full_complete = True
        self._reconcile_initial_monitor_coverage()

    def _reconcile_initial_monitor_coverage(self) -> None:
        """Queue one full status after closing the initial monitor-coverage gap."""

        if (
            not self._initial_monitor_reconciliation_required
            or not self._initial_full_complete
            or not self._initial_mount_complete
        ):
            return
        self._initial_monitor_reconciliation_required = False
        self.request_full()

    def _publish_status(self) -> None:
        """Publish only a snapshot whose paths, states or cached branch changed."""

        removed_paths = {
            item.path for item in self._statuses.values() if item.state == "removed"
        }
        moved_sources = {
            item.source_path
            for item in self._statuses.values()
            if item.state == "added"
            and item.source_path
            and item.source_path in removed_paths
        }
        ordered: list[FileStatus] = []
        for item in sorted(self._statuses.values(), key=attrgetter("path")):
            if item.state == "removed" and item.path in moved_sources:
                continue
            if item.state == "added" and item.source_path in moved_sources:
                item = FileStatus(
                    item.path,
                    "moved",
                    item.staged,
                    item.repository,
                    item.source_path,
                    item.scm_type,
                )
            ordered.append(item)
        signature = (
            tuple((item.path, item.state, item.source_path) for item in ordered),
            self._branch,
        )
        if signature != self._published_snapshot:
            self._published_snapshot = signature
            self.on_status(ordered, self._branch)

    def _add_pending_path(self, path: str) -> None:
        """Add one root while removing redundant descendants from the batch."""

        if any(
            path == root or path.startswith(f"{root}/")
            for root in self._pending_paths
        ):
            return
        self._pending_paths = {
            root for root in self._pending_paths if not root.startswith(f"{path}/")
        }
        self._pending_paths.add(path)

    def _schedule_pending(self) -> None:
        """Schedule pending work after the active or background quiet window."""

        if self.closed or self.paused or not self._has_pending_work():
            return
        if self.pending_id is not None:
            GLib.source_remove(self.pending_id)
        self.ready = False
        delay = (
            self.ACTIVE_DEBOUNCE_MS
            if self.active
            else self.INACTIVE_DEBOUNCE_MS
        )
        # 2026-08-17: anche il repository attivo attende una breve finestra
        # trailing, così gli eventi multipli dello stesso salvataggio producono
        # un solo status SCM senza rendere percepibile il ritardo nella UI.
        self.pending_id = GLib.timeout_add(delay, self._mark_ready)

    def _mark_ready(self) -> bool:
        """Mark the coalesced request executable and start it when possible."""

        self.pending_id = None
        if self.paused:
            return GLib.SOURCE_REMOVE
        self.ready = True
        self._start_next()
        return GLib.SOURCE_REMOVE

    def _start_next(self) -> None:
        """Run the next queued command sequentially after debounce and lock release."""

        if self.closed or self.paused or self.command is not None or not self.ready:
            return
        if not self._has_pending_work():
            self.ready = False
            return
        if self._pending_ignored:
            self._pending_ignored = False
            self._run(
                self.scm.ignored_argv(),
                self._on_ignored,
            )
            return
        if self._pending_branch:
            self._pending_branch = False
            self._run(
                self.scm.branch_argv(),
                self._on_branch,
            )
            return
        if self.scm.is_locked():
            self.waiting_for_unlock = True
            return
        self.waiting_for_unlock = False
        paths: tuple[str, ...] = ()
        if self._pending_full:
            self._pending_full = False
            self._pending_paths.clear()
            if not self._initial_full_started:
                self._initial_full_started = True
                self._initial_monitor_reconciliation_required = (
                    not self._initial_mount_complete
                )
        elif self._pending_paths:
            paths = tuple(sorted(self._pending_paths))
            self._pending_paths.clear()
        else:
            self.ready = False
            return
        self._run(
            self.scm.status_argv(paths),
            partial(self._on_status, paths),
        )

    def _schedule_pause_ready(self) -> None:
        """Deliver exclusive-operation ownership on the next main-loop turn."""

        if self.pause_idle_id is None and self.pause_callback is not None:
            self.pause_idle_id = GLib.idle_add(self._deliver_pause_ready)

    def _deliver_pause_ready(self) -> bool:
        """Notify the waiting explicit operation after automatic work is quiet."""

        self.pause_idle_id = None
        callback = self.pause_callback
        self.pause_callback = None
        if callback is not None and not self.closed and self.paused:
            callback()
        return GLib.SOURCE_REMOVE

    def _has_pending_work(self) -> bool:
        """Return whether any automatic SCM query remains queued."""

        return bool(
            self._pending_ignored
            or self._pending_branch
            or self._pending_full
            or self._pending_paths
        )

    def _queue_mount(self, directory: str) -> None:
        """Queue a subtree for non-blocking breadth-first monitor creation."""

        resolved = str(Path(directory).resolve())
        if resolved not in self.monitors and resolved not in self._mount_queue:
            self._mount_queue.append(resolved)
        if self.mount_idle_id is None:
            self.mount_idle_id = GLib.idle_add(self._mount_batch)

    def _drop_monitor_subtree(self, directory: str) -> None:
        """Remove dead monitors and queued mounts below a replaced directory."""

        root = str(Path(directory).resolve())
        prefix = f"{root}{os.sep}"
        for monitored, monitor in tuple(self.monitors.items()):
            if monitored == root or monitored.startswith(prefix):
                monitor.cancel()
                self.monitors.pop(monitored, None)
        self._mount_queue = deque(
            queued
            for queued in self._mount_queue
            if queued != root and not queued.startswith(prefix)
        )

    def _mount_batch(self) -> bool:
        """Mount monitors within a small count and wall-time main-loop budget."""

        processed = 0
        started_at = GLib.get_monotonic_time()
        count_limit = (
            self.MOUNT_ACTIVE_BATCH if self.active else self.MOUNT_INACTIVE_BATCH
        )
        # 2026-08-17: monitor_directory e scandir sono sincroni; un limite sia
        # temporale sia numerico impedisce ai repository grandi di congelare GTK.
        while (
            self._mount_queue
            and processed < count_limit
            and GLib.get_monotonic_time() - started_at < self.MOUNT_BUDGET_US
            and not self.closed
        ):
            directory = self._mount_queue.popleft()
            processed += 1
            if self._should_prune(directory) or directory in self.monitors:
                continue
            try:
                monitor = Gio.File.new_for_path(directory).monitor_directory(
                    Gio.FileMonitorFlags.WATCH_MOVES, None
                )
                monitor.connect("changed", self._on_change)
                self.monitors[directory] = monitor
                with os.scandir(directory) as entries:
                    for entry in entries:
                        if entry.is_dir(follow_symlinks=False):
                            self._mount_queue.append(entry.path)
            except (OSError, GLib.Error) as error:
                self.on_error(f"Monitor unavailable for {directory}: {error}")
        if self._mount_queue and not self.closed:
            # 2026-08-17: startup can observe a lock before the .hg monitor is
            # mounted; each mount batch rechecks release without a poll timer.
            if self.waiting_for_unlock and not self.scm.is_locked():
                self.waiting_for_unlock = False
                self._start_next()
            return GLib.SOURCE_CONTINUE
        self.mount_idle_id = None
        self._initial_mount_complete = True
        self._reconcile_initial_monitor_coverage()
        if self.waiting_for_unlock and not self.scm.is_locked():
            self.waiting_for_unlock = False
            self._start_next()
        return GLib.SOURCE_REMOVE

    def _should_prune(self, directory: str) -> bool:
        """Return whether a directory must not consume an inotify watch."""

        try:
            relative = Path(directory).resolve().relative_to(self.path).as_posix()
        except ValueError:
            return True
        if relative == ".":
            return False
        parts = relative.split("/")
        if any(part in self.HARD_PRUNE for part in parts):
            return True
        if self._is_nested_repository_path(relative):
            return True
        if relative == ".hg/store/data" or relative.startswith(".hg/store/data/"):
            return True
        if relative in {".git/objects", ".git/logs"} or relative.startswith(
            (".git/objects/", ".git/logs/")
        ):
            return True
        return any(
            relative == ignored or relative.startswith(f"{ignored}/")
            for ignored in self.ignored
        )

    def _on_change(
        self,
        _monitor: Gio.FileMonitor,
        file: Gio.File,
        _other: Gio.File | None,
        event_type: Gio.FileMonitorEvent,
    ) -> None:
        """Mount new directories and queue their relevant filesystem changes."""

        path = file.get_path()
        if not path or self.closed:
            return
        relative = os.path.relpath(path, self.path).replace(os.sep, "/")
        if self._is_nested_repository_path(relative):
            return
        now = GLib.get_monotonic_time() // 1000
        metadata_root = f".{self.scm.kind}"
        metadata = relative == metadata_root or relative.startswith(
            f"{metadata_root}/"
        )
        if metadata and now < self.mute_until:
            return
        if metadata:
            self._on_metadata_change(relative, event_type)
            return
        # 2026-08-16: gli osservatori UI ricevono solo working-tree events;
        # i metadati SCM non devono ricostruire inutilmente l'albero file.
        self.on_file_change(relative)
        other_path = _other.get_path() if _other is not None else None
        # 2026-08-18: rinomina e sostituzione di una directory invalidano i
        # monitor legati al vecchio inode; conservarli lascia il nuovo albero
        # apparentemente coperto ma senza più eventi durante il lavoro.
        if event_type in {
            Gio.FileMonitorEvent.DELETED,
            Gio.FileMonitorEvent.MOVED_OUT,
            Gio.FileMonitorEvent.MOVED,
            Gio.FileMonitorEvent.RENAMED,
        }:
            self._drop_monitor_subtree(path)
        mount_path: str | None = None
        if event_type in {
            Gio.FileMonitorEvent.CREATED,
            Gio.FileMonitorEvent.MOVED_IN,
        } and os.path.isdir(path):
            mount_path = path
        elif (
            event_type
            in {Gio.FileMonitorEvent.MOVED, Gio.FileMonitorEvent.RENAMED}
            and other_path
            and os.path.isdir(other_path)
        ):
            mount_path = other_path
        if mount_path is not None:
            # Un percorso ricreato può avere ancora in mappa il monitor morto
            # della directory precedente con lo stesso nome.
            self._drop_monitor_subtree(mount_path)
            self._queue_mount(mount_path)
        paths = [relative]
        if other_path:
            other_relative = os.path.relpath(
                other_path, self.path
            ).replace(os.sep, "/")
            if not self._is_nested_repository_path(other_relative):
                paths.append(other_relative)
        if relative in {".hgignore", ".gitignore"}:
            self._pending_ignored = True
        self.request_paths(paths)

    def _is_nested_repository_path(self, relative: str) -> bool:
        """Return whether a path belongs to an independently watched child root."""

        return any(
            relative == repository or relative.startswith(f"{repository}/")
            for repository in self.nested_repositories
        )

    def _on_metadata_change(
        self, relative: str, event_type: Gio.FileMonitorEvent
    ) -> None:
        """Translate transaction metadata into one full status and branch request."""

        if self.scm.kind == "git":
            self._on_git_metadata_change(relative, event_type)
            return
        if relative.startswith((".hg/cache/", ".hg/wcache/", ".hg/store/data/")):
            return
        if relative == ".hg/wlock":
            if event_type in (
                Gio.FileMonitorEvent.DELETED,
                Gio.FileMonitorEvent.MOVED_OUT,
            ) or not self.scm.is_locked():
                self.waiting_for_unlock = False
                self.request_full()
            return
        if relative == ".hg/branch":
            self.on_history_change()
            self.request_full(
                refresh_branch=True,
            )
            return
        if (
            relative == ".hg/dirstate"
            or relative == ".hg/store"
            or relative.startswith(".hg/merge/")
        ):
            if relative in {".hg/dirstate", ".hg/store"}:
                self.on_history_change()
            self.request_full()

    def _on_git_metadata_change(
        self, relative: str, event_type: Gio.FileMonitorEvent
    ) -> None:
        """Translate useful normal-Git metadata events without watching objects."""

        if relative.startswith((".git/objects/", ".git/logs/")):
            return
        if relative == ".git/index.lock":
            if event_type in (
                Gio.FileMonitorEvent.DELETED,
                Gio.FileMonitorEvent.MOVED_OUT,
            ) or not self.scm.is_locked():
                self.waiting_for_unlock = False
                self.request_full()
            return
        if relative == ".git/HEAD":
            self.on_history_change()
            self.request_full(refresh_branch=True)
            return
        if relative == ".git/packed-refs" or relative.startswith(".git/refs/"):
            # 2026-08-19: local and remote ref movements make a previous
            # explicit comparison stale without authorizing another fetch.
            self.on_history_change()
            return
        if relative == ".git/index":
            self.request_full()
            return
        if relative == ".git/info/exclude":
            self._pending_ignored = True
            self._schedule_pending()

    @staticmethod
    def _describe_error(context: str, result: CommandResult) -> str:
        """Build a concise user-facing subprocess error."""

        detail = str(result.error) if result.error else result.stderr.strip()
        return f"{context}: {detail or f'exit code {result.returncode}'}"

    @staticmethod
    def _ignore_error(_message: str) -> None:
        """Accept intentionally unobserved errors for headless callers."""

    @staticmethod
    def _ignore_file_change(_relative_path: str) -> None:
        """Accept an intentionally unobserved working-tree event."""

    @staticmethod
    def _ignore_ignored(_ignored: set[str]) -> None:
        """Accept intentionally unobserved ignore classification updates."""

    @staticmethod
    def _ignore_history_change() -> None:
        """Accept intentionally unobserved repository-history changes."""
