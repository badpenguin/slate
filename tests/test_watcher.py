"""Focused tests for non-blocking recursive watcher mounting."""

from collections import deque
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from gi.repository import Gio, GLib

from slate.processes import CommandResult
from slate.scm.base import FileStatus
from slate.scm.git import GitSCM
from slate.scm.hg import MercurialSCM
from slate.watcher import RepoWatcher


class RepoWatcherMountTest(unittest.TestCase):
    """Verify that background monitor discovery yields frequently to GTK."""

    def test_newline_in_ignored_filename_cannot_prune_an_unrelated_source_directory(self) -> None:
        """Parsing ignored names cannot invent a src boundary and suppress its filesystem events."""

        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            root = Path(directory)
            (root / "src").mkdir()
            owner = SimpleNamespace(path=directory, scm=SimpleNamespace(kind="hg"),
                                    nested_repositories=set(),
                                    ignored=MercurialSCM(directory).parse_ignored("src\nassets/app.js\0"),
                                    HARD_PRUNE=RepoWatcher.HARD_PRUNE)
            owner._is_nested_repository_path = RepoWatcher._is_nested_repository_path.__get__(owner)
            self.assertFalse(RepoWatcher._should_prune(owner, str(root / "src")))

    def test_inactive_mount_processes_only_one_directory_per_idle(self) -> None:
        """An inactive large repository cannot monopolize one main-loop turn."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("one", "two", "three"):
                (root / name).mkdir()
            owner = SimpleNamespace(
                _mount_queue=deque(str(root / name) for name in ("one", "two", "three")),
                monitors={},
                ignored=set(),
                nested_repositories=set(),
                path=str(root),
                active=False,
                closed=False,
                waiting_for_unlock=False,
                scm=SimpleNamespace(
                    kind="hg", is_locked=MagicMock(return_value=False)
                ),
                mount_idle_id=1,
                MOUNT_ACTIVE_BATCH=RepoWatcher.MOUNT_ACTIVE_BATCH,
                MOUNT_INACTIVE_BATCH=RepoWatcher.MOUNT_INACTIVE_BATCH,
                MOUNT_BUDGET_US=RepoWatcher.MOUNT_BUDGET_US,
                HARD_PRUNE=RepoWatcher.HARD_PRUNE,
                _should_prune=None,
                _is_nested_repository_path=None,
                _on_change=MagicMock(),
                _debug=MagicMock(),
                on_error=MagicMock(),
            )
            owner._should_prune = RepoWatcher._should_prune.__get__(owner)
            owner._is_nested_repository_path = (
                RepoWatcher._is_nested_repository_path.__get__(owner)
            )
            result = RepoWatcher._mount_batch(owner)
            self.assertEqual(result, GLib.SOURCE_CONTINUE)
            self.assertEqual(len(owner.monitors), 1)
            self.assertEqual(len(owner._mount_queue), 2)
            for monitor in owner.monitors.values():
                monitor.cancel()

    def test_renamed_directory_replaces_dead_monitors_and_mounts_destination(
        self,
    ) -> None:
        """A directory rename keeps later changes below its destination observable."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old = root / "old"
            destination = root / "renamed"
            destination.mkdir()
            old_monitor = MagicMock()
            child_monitor = MagicMock()
            owner = SimpleNamespace(
                path=str(root),
                closed=False,
                mute_until=0,
                scm=SimpleNamespace(kind="hg"),
                nested_repositories=set(),
                monitors={
                    str(old): old_monitor,
                    str(old / "child"): child_monitor,
                },
                _mount_queue=deque([str(old / "queued")]),
                mount_idle_id=1,
                on_file_change=MagicMock(),
                request_paths=MagicMock(),
                _on_metadata_change=MagicMock(),
                _debug=MagicMock(),
            )
            owner._is_nested_repository_path = (
                RepoWatcher._is_nested_repository_path.__get__(owner)
            )
            owner._drop_monitor_subtree = (
                RepoWatcher._drop_monitor_subtree.__get__(owner)
            )
            owner._queue_mount = RepoWatcher._queue_mount.__get__(owner)
            source = MagicMock()
            source.get_path.return_value = str(old)
            target = MagicMock()
            target.get_path.return_value = str(destination)

            RepoWatcher._on_change(
                owner,
                MagicMock(),
                source,
                target,
                Gio.FileMonitorEvent.RENAMED,
            )

            old_monitor.cancel.assert_called_once_with()
            child_monitor.cancel.assert_called_once_with()
            self.assertEqual(owner.monitors, {})
            self.assertEqual(owner._mount_queue, deque([str(destination)]))
            owner.request_paths.assert_called_once_with(["old", "renamed"])

    def test_recreated_directory_discards_stale_same_path_monitor(self) -> None:
        """A directory rebuilt at the same path receives a fresh recursive mount."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rebuilt = root / "src"
            rebuilt.mkdir()
            stale_monitor = MagicMock()
            owner = SimpleNamespace(
                path=str(root),
                closed=False,
                mute_until=0,
                scm=SimpleNamespace(kind="hg"),
                nested_repositories=set(),
                monitors={str(rebuilt): stale_monitor},
                _mount_queue=deque(),
                mount_idle_id=1,
                on_file_change=MagicMock(),
                request_paths=MagicMock(),
                _on_metadata_change=MagicMock(),
                _debug=MagicMock(),
            )
            owner._is_nested_repository_path = (
                RepoWatcher._is_nested_repository_path.__get__(owner)
            )
            owner._drop_monitor_subtree = (
                RepoWatcher._drop_monitor_subtree.__get__(owner)
            )
            owner._queue_mount = RepoWatcher._queue_mount.__get__(owner)
            changed = MagicMock()
            changed.get_path.return_value = str(rebuilt)

            RepoWatcher._on_change(
                owner,
                MagicMock(),
                changed,
                None,
                Gio.FileMonitorEvent.CREATED,
            )

            stale_monitor.cancel.assert_called_once_with()
            self.assertEqual(owner.monitors, {})
            self.assertEqual(owner._mount_queue, deque([str(rebuilt)]))
            owner.request_paths.assert_called_once_with(["src"])

    def test_reconciliation_walks_below_an_existing_root_monitor(self) -> None:
        """A manual rescan discovers directories missed by the live root monitor."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            missed = root / "missed"
            missed.mkdir()
            root_monitor = MagicMock()
            owner = SimpleNamespace(
                _mount_queue=deque([str(root)]),
                monitors={str(root): root_monitor},
                ignored=set(),
                nested_repositories=set(),
                path=str(root),
                active=False,
                closed=False,
                waiting_for_unlock=False,
                scm=SimpleNamespace(
                    kind="hg", is_locked=MagicMock(return_value=False)
                ),
                mount_idle_id=1,
                MOUNT_ACTIVE_BATCH=RepoWatcher.MOUNT_ACTIVE_BATCH,
                MOUNT_INACTIVE_BATCH=RepoWatcher.MOUNT_INACTIVE_BATCH,
                MOUNT_BUDGET_US=RepoWatcher.MOUNT_BUDGET_US,
                HARD_PRUNE=RepoWatcher.HARD_PRUNE,
                _should_prune=None,
                _is_nested_repository_path=None,
                _on_change=MagicMock(),
                _reconcile_initial_monitor_coverage=MagicMock(),
                _debug=MagicMock(),
                on_error=MagicMock(),
            )
            owner._should_prune = RepoWatcher._should_prune.__get__(owner)
            owner._is_nested_repository_path = (
                RepoWatcher._is_nested_repository_path.__get__(owner)
            )

            result = RepoWatcher._mount_batch(owner)

            self.assertEqual(result, GLib.SOURCE_CONTINUE)
            self.assertEqual(owner._mount_queue, deque([str(missed)]))
            self.assertIs(owner.monitors[str(root)], root_monitor)

    def test_mercurial_metadata_tree_is_pruned_from_recursive_mounts(self) -> None:
        """The worktree traversal never subscribes to all Mercurial temporaries."""

        owner = SimpleNamespace(
            path="/project",
            scm=SimpleNamespace(kind="hg"),
            ignored=set(),
            nested_repositories=set(),
            HARD_PRUNE=RepoWatcher.HARD_PRUNE,
            _is_nested_repository_path=MagicMock(return_value=False),
        )

        self.assertTrue(RepoWatcher._should_prune(owner, "/project/.hg"))
        self.assertTrue(
            RepoWatcher._should_prune(owner, "/project/.hg/store/data")
        )

    def test_git_metadata_tree_is_pruned_from_recursive_mounts(self) -> None:
        """The worktree traversal never subscribes to Git administrative noise."""

        owner = SimpleNamespace(
            path="/project",
            scm=SimpleNamespace(kind="git"),
            ignored=set(),
            nested_repositories=set(),
            HARD_PRUNE=RepoWatcher.HARD_PRUNE,
            _is_nested_repository_path=MagicMock(return_value=False),
        )

        self.assertTrue(RepoWatcher._should_prune(owner, "/project/.git"))
        self.assertTrue(
            RepoWatcher._should_prune(owner, "/project/.git/objects/ab")
        )


class _RepoWatcherSchedulingFixture:
    """Share command interception and GLib lifecycle across SCM schedulers."""

    repository_marker = ""
    scm_class = MercurialSCM

    def setUp(self) -> None:
        """Create one disposable repository and deterministic watcher scheduler."""

        # 2026-08-18: HG e Git esercitano lo stesso scheduler; marker e adapter
        # sono gli unici dati variabili e non giustificano due lifecycle fixture.
        self.debounce_patch = patch.object(RepoWatcher, "ACTIVE_DEBOUNCE_MS", 0)
        self.debounce_patch.start()
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / self.repository_marker).mkdir()
        self.calls: list[tuple[list[str], object]] = []
        self.statuses: list[tuple[list[FileStatus], str]] = []
        self.run_patch = patch("slate.watcher.run_async", self._record_command)
        self.run_patch.start()
        self.watcher = RepoWatcher(
            str(self.root),
            self.scm_class(str(self.root)),
            self._record_status,
        )
        self.watcher._initial_mount_complete = True
        self.watcher.set_active(True)
        self._drain_main_context()

    def tearDown(self) -> None:
        """Close GLib sources and release the disposable repository."""

        self.watcher.close()
        self.run_patch.stop()
        self.debounce_patch.stop()
        self.temporary.cleanup()

    def _record_command(self, argv, callback, **_kwargs):
        """Retain one asynchronous callback without executing an SCM command."""

        self.calls.append((list(argv), callback))
        return MagicMock()

    def _record_status(self, statuses: list[FileStatus], branch: str) -> None:
        """Retain published snapshots for assertions that need their contents."""

        self.statuses.append((list(statuses), branch))

    @staticmethod
    def _drain_main_context() -> None:
        """Run ready GLib sources without waiting for future timeouts."""

        context = GLib.MainContext.default()
        while context.pending():
            context.iteration(False)

    def _complete(self, index: int, stdout: str = "") -> None:
        """Complete one intercepted command successfully with controlled output."""

        argv, callback = self.calls[index]
        callback(CommandResult(tuple(argv), 0, stdout, ""))

    def test_ignore_change_refreshes_rules_and_complete_status(self) -> None:
        """Ignore edits reclassify all cached paths rather than only the rule file."""

        self._complete(0)
        self._complete(1, "main")
        self._complete(2, "[]" if self.watcher.scm.kind == "hg" else "")
        self.watcher._statuses = {"new.txt": FileStatus("new.txt", "untracked")}
        self.watcher._publish_status()
        rule = ".gitignore" if self.watcher.scm.kind == "git" else ".hgignore"
        self.watcher._on_change(None, Gio.File.new_for_path(str(self.root / rule)), None,
                                Gio.FileMonitorEvent.CHANGES_DONE_HINT)
        self._drain_main_context()
        self.assertEqual(self.calls[3][0], self.watcher.scm.ignored_argv(()))
        self._complete(3)
        self.assertEqual(self.calls[4][0], self.watcher.scm.status_argv(()))
        self._complete(4, "[]" if self.watcher.scm.kind == "hg" else "")
        self.assertEqual(self.statuses[-1][0], [])

    def test_read_errors_recover_independently_even_with_unchanged_status(self) -> None:
        """Status success preserves ignore/branch errors; each recovery clears only its scope."""

        self._complete(0)
        self._complete(1, "main")
        self._complete(2, "[]" if self.watcher.scm.kind == "hg" else "")
        self.watcher.on_error = MagicMock()
        failed = CommandResult((), 1, "", "temporary failure")
        self.watcher._on_ignored(failed)
        self.watcher._on_branch(failed)
        output = "[]" if self.watcher.scm.kind == "hg" else ""
        published = len(self.statuses)
        self.watcher._on_status((), CommandResult((), 0, output, ""))
        self.assertEqual(set(self.watcher.read_errors), {"ignored", "branch"})
        self.watcher._on_ignored(CommandResult((), 0, "", ""))
        self.assertEqual(set(self.watcher.read_errors), {"branch"})
        self.assertIn("branch", self.watcher.on_error.call_args.args[0])
        self.watcher._on_branch(CommandResult((), 0, "main", ""))
        self.assertEqual(self.watcher.read_errors, {})
        self.watcher.on_error.assert_called_with("")
        self.assertEqual(len(self.statuses), published)



class RepoWatcherSchedulingTest(_RepoWatcherSchedulingFixture, unittest.TestCase):
    """Verify that automatic Mercurial reads are coalesced and sequential."""

    repository_marker = ".hg"
    scm_class = MercurialSCM

    def _finish_startup(self, status_output: str = "[]") -> None:
        """Complete the one-time ignore, branch and full-status sequence."""

        self.assertEqual(self.calls[0][0][:3], ["hg", "status", "-i"])
        self._complete(0)
        self.assertEqual(self.calls[1][0], ["hg", "branch"])
        self._complete(1, "default\n")
        self.assertEqual(
            self.calls[2][0], ["hg", "status", "--copies", "-Tjson"]
        )
        self._complete(2, status_output)

    def test_startup_commands_are_strictly_sequential(self) -> None:
        """Only completion of one startup read may launch the following read."""

        self.assertEqual(len(self.calls), 1)
        self._complete(0)
        self.assertEqual(len(self.calls), 2)
        self._complete(1, "default\n")
        self.assertEqual(len(self.calls), 3)
        self._complete(2, "[]")
        self.assertEqual(len(self.calls), 3)

    def test_new_nested_boundary_retries_the_ignore_query_with_an_exclusion(self) -> None:
        """Discovery can repair an ignore read that started before the child was known."""

        self._finish_startup()
        self.watcher.set_nested_repositories({"public/backoffice/atk"})
        self._drain_main_context()
        argv = self.calls[-1][0]
        self.assertEqual(argv, ["hg", "status", "-i", "-T", "{path}\\0",
                                "--exclude", "path:public/backoffice/atk"])

    def test_watcher_diagnostics_are_emitted_only_in_debug_mode(self) -> None:
        """Focused lifecycle traces remain silent outside explicit debug mode."""

        diagnostics = io.StringIO()
        with patch("slate.watcher.sys.stderr", diagnostics):
            self.watcher._debug("hidden")
            self.watcher.debug = True
            self.watcher._debug("visible")

        self.assertNotIn("hidden", diagnostics.getvalue())
        self.assertIn(
            f"[SLATE watcher] repository={self.watcher.path} visible",
            diagnostics.getvalue(),
        )

    def test_mercurial_uses_only_explicit_metadata_file_monitors(self) -> None:
        """HG metadata coverage excludes its recursively noisy administrative tree."""

        relatives = {
            Path(path).relative_to(self.root).as_posix()
            for path in self.watcher.metadata_monitors
        }
        self.assertEqual(
            relatives,
            {
                ".hg/branch",
                ".hg/dirstate",
                ".hg/merge/state",
                ".hg/store/00changelog.i",
                ".hg/wlock",
            },
        )

    def test_initial_mount_gap_queues_one_reconciliation_status(self) -> None:
        """Changes during recursive monitor mounting receive one final full status."""

        self.watcher._initial_mount_complete = False
        self._complete(0)
        self._complete(1, "default\n")
        self._complete(2, "[]")
        self.assertEqual(len(self.calls), 3)

        self._drain_main_context()
        self.assertEqual(
            self.calls[3][0], ["hg", "status", "--copies", "-Tjson"]
        )
        self._complete(3, '[{"status":"M","path":"missed.py"}]')
        self._drain_main_context()
        self.assertEqual(
            self.statuses[-1],
            ([FileStatus("missed.py", "modified")], "default"),
        )
        self.assertEqual(len(self.calls), 4)

    def test_explicit_operation_waits_for_active_read_and_retains_refresh(self) -> None:
        """Watcher pause grants ownership only after its command and resumes once."""

        acquired = MagicMock()
        self.watcher.pause_after_current(acquired)
        self._drain_main_context()
        acquired.assert_not_called()
        self._complete(0)
        self._drain_main_context()
        acquired.assert_called_once_with()
        self.assertEqual(len(self.calls), 1)
        self.watcher.request_paths(("changed.py",))
        self._drain_main_context()
        self.assertEqual(len(self.calls), 1)
        self.watcher.resume_with_full_refresh(refresh_branch=True)
        self._drain_main_context()
        self.assertEqual(self.calls[1][0], ["hg", "branch"])

    def test_watcher_has_no_periodic_or_lock_polling_hooks(self) -> None:
        """Filesystem events and lock removal replace every watcher poll timer."""

        self.assertFalse(hasattr(RepoWatcher, "POLL_MS"))
        self.assertFalse(hasattr(RepoWatcher, "LOCK_RETRY_MS"))
        self.assertFalse(hasattr(RepoWatcher, "_poll"))

    def test_full_status_collapses_a_recorded_move_into_one_row(self) -> None:
        """An added copy whose source is removed becomes one atomic move."""

        self._finish_startup(
            '[{"status":"A","path":"new.py","source":"old.py"},'
            '{"status":"R","path":"old.py"}]'
        )
        self.assertEqual(
            self.statuses[-1],
            ([FileStatus("new.py", "moved", source_path="old.py")], "default"),
        )

    def test_full_status_keeps_a_copy_in_the_added_group(self) -> None:
        """Copy metadata alone cannot be mislabeled as a removed-source move."""

        self._finish_startup(
            '[{"status":"A","path":"copy.py","source":"original.py"}]'
        )
        self.assertEqual(
            self.statuses[-1],
            (
                [FileStatus("copy.py", "added", source_path="original.py")],
                "default",
            ),
        )

    def test_incremental_copy_metadata_promotes_to_full_status(self) -> None:
        """A partial source relation waits for a repository-wide classification."""

        self._finish_startup()
        published_count = len(self.statuses)
        self.watcher.request_paths(("new.py",))
        self._drain_main_context()
        self._complete(
            3,
            '[{"status":"A","path":"new.py","source":"old.py"}]',
        )
        self._drain_main_context()
        self.assertEqual(len(self.statuses), published_count)
        self.assertEqual(
            self.calls[4][0], ["hg", "status", "--copies", "-Tjson"]
        )

    def test_incremental_status_replaces_only_queried_cached_paths(self) -> None:
        """A clean incremental result removes its roots and preserves other rows."""

        initial = (
            '[{"status":"M","path":"a.py"},'
            '{"status":"?","path":"other.py"}]'
        )
        self._finish_startup(initial)
        self.watcher.request_paths(("a.py",))
        self._drain_main_context()
        self.assertEqual(
            self.calls[3][0],
            ["hg", "status", "--copies", "-Tjson", "--", 'path:a.py'],
        )
        self._complete(3, "[]")
        self.assertEqual(
            self.statuses[-1],
            ([FileStatus("other.py", "untracked")], "default"),
        )

    def test_twenty_paths_promote_one_request_to_full_status(self) -> None:
        """The configured threshold avoids a large incremental argument vector."""

        self._finish_startup()
        self.watcher.request_paths(f"file-{index}.py" for index in range(20))
        self._drain_main_context()
        self.assertEqual(
            self.calls[3][0], ["hg", "status", "--copies", "-Tjson"]
        )

    def test_active_events_wait_for_running_command_then_start_immediately(self) -> None:
        """Single-flight queues active changes without a second running process."""

        self._finish_startup()
        self.watcher.request_paths(("first.py",))
        self._drain_main_context()
        self.assertEqual(len(self.calls), 4)
        self.watcher.request_paths(("second.py",))
        self._drain_main_context()
        self.assertEqual(len(self.calls), 4)
        self._complete(3, "[]")
        self.assertEqual(len(self.calls), 5)
        self.assertEqual(self.calls[4][0][-1], "path:second.py")

    def test_active_events_reset_one_hundred_millisecond_trailing_debounce(
        self,
    ) -> None:
        """Nearby active changes share one status after resetting the short timer."""

        self._finish_startup()
        self.watcher.ACTIVE_DEBOUNCE_MS = 100
        with patch(
            "slate.watcher.GLib.timeout_add", side_effect=(401, 402)
        ) as timeout_add, patch("slate.watcher.GLib.source_remove") as source_remove:
            self.watcher.request_paths(("first.py",))
            self.watcher.request_paths(("second.py",))

        self.assertEqual(
            timeout_add.call_args_list,
            [
                unittest.mock.call(100, self.watcher._mark_ready),
                unittest.mock.call(100, self.watcher._mark_ready),
            ],
        )
        source_remove.assert_called_once_with(401)
        self.assertEqual(len(self.calls), 3)
        self.watcher._mark_ready()
        self.assertEqual(
            self.calls[3][0],
            [
                "hg",
                "status",
                "--copies",
                "-Tjson",
                "--",
                'path:first.py',
                'path:second.py',
            ],
        )

    def test_inactive_events_use_two_second_trailing_debounce(self) -> None:
        """Background repositories reset one two-second timer without running hg."""

        self._finish_startup()
        self.watcher.set_active(False)
        with patch("slate.watcher.GLib.timeout_add", return_value=987) as timeout_add:
            self.watcher.request_paths(("background.py",))
        timeout_add.assert_called_once_with(2000, self.watcher._mark_ready)
        self.assertEqual(len(self.calls), 3)
        self.watcher.pending_id = None

    def test_dirstate_event_queues_only_full_status(self) -> None:
        """A normal external transaction refreshes status without rereading branch."""

        self._finish_startup()
        self.watcher.on_history_change = MagicMock()
        self.watcher._on_metadata_change(".hg/dirstate", 0)
        self._drain_main_context()
        self.watcher.on_history_change.assert_called_once_with()
        self.assertEqual(
            self.calls[3][0], ["hg", "status", "--copies", "-Tjson"]
        )

    def test_branch_metadata_event_queues_branch_then_full_status(self) -> None:
        """A real named-branch change refreshes branch before publishing status."""

        self._finish_startup()
        self.watcher._on_metadata_change(".hg/branch", 0)
        self._drain_main_context()
        self.assertEqual(self.calls[3][0], ["hg", "branch"])
        self._complete(3, "feature\n")
        self.assertEqual(
            self.calls[4][0], ["hg", "status", "--copies", "-Tjson"]
        )

    def test_manual_scan_rereads_ignores_branch_and_full_status(self) -> None:
        """The explicit scan action is the complete repository reconciliation."""

        self._finish_startup()
        self.watcher.request_scan()
        self._drain_main_context()
        self.assertEqual(self.calls[3][0][:3], ["hg", "status", "-i"])
        self._complete(3)
        self.assertEqual(self.calls[4][0], ["hg", "branch"])
        self._complete(4, "default\n")
        self.assertEqual(
            self.calls[5][0], ["hg", "status", "--copies", "-Tjson"]
        )

    def test_unlock_event_releases_one_waiting_status_without_polling(self) -> None:
        """Removing wlock starts a ready status through the metadata monitor."""

        self._complete(0)
        (self.root / ".hg" / "wlock").touch()
        self._complete(1, "default\n")
        self.assertEqual(len(self.calls), 2)
        (self.root / ".hg" / "wlock").unlink()
        self.watcher._on_metadata_change(".hg/wlock", 0)
        self._drain_main_context()
        self.assertEqual(
            self.calls[2][0], ["hg", "status", "--copies", "-Tjson"]
        )

    def test_muted_lock_removal_still_releases_waiting_status(self) -> None:
        """Metadata muting cannot consume the event needed to resume the scheduler."""

        self._complete(0)
        self._complete(1, "default\n")
        self.watcher.waiting_for_unlock = True
        self.watcher.mute_until = (GLib.get_monotonic_time() // 1000) + 500
        changed = MagicMock()
        changed.get_path.return_value = str(self.root / ".hg" / "wlock")

        self.watcher._on_change(
            MagicMock(),
            changed,
            None,
            Gio.FileMonitorEvent.DELETED,
        )
        self._drain_main_context()

        self.assertFalse(self.watcher.waiting_for_unlock)
        self.assertEqual(
            self.calls[2][0], ["hg", "status", "--copies", "-Tjson"]
        )

    def test_mercurial_temporary_metadata_is_discarded_before_diagnostics(
        self,
    ) -> None:
        """HG command temporaries neither queue work nor flood debug output."""

        self.watcher.debug = True
        self.watcher._debug = MagicMock()
        self.watcher._on_metadata_change = MagicMock()
        temporary = MagicMock()
        temporary.get_path.return_value = str(self.root / ".hg" / "tmp123")

        self.watcher._on_change(
            MagicMock(),
            temporary,
            None,
            Gio.FileMonitorEvent.CREATED,
        )

        self.watcher._debug.assert_not_called()
        self.watcher._on_metadata_change.assert_not_called()


class GitRepoWatcherSchedulingTest(_RepoWatcherSchedulingFixture, unittest.TestCase):
    """Verify Git reuses the event-driven, sequential incremental scheduler."""

    repository_marker = ".git"
    scm_class = GitSCM

    def _finish_startup(self) -> None:
        """Complete ignore, branch and full status in scheduler order."""

        self._complete(0)
        self._complete(1, "main\n")
        self._complete(2)

    def test_nested_ignore_and_ignore_rename_queue_full_refresh(self) -> None:
        """Nested rules and atomic replacements invalidate rules and complete Git status."""

        self._finish_startup()
        for source, destination in (("src/.gitignore", None), ("src/temp", "src/.gitignore"),
                                    ("src/.gitignore", "src/old-ignore")):
            with self.subTest(source=source, destination=destination), patch.object(
                    self.watcher, "request_full") as full:
                self.watcher._on_change(
                    None, Gio.File.new_for_path(str(self.root / source)),
                    Gio.File.new_for_path(str(self.root / destination)) if destination else None,
                    Gio.FileMonitorEvent.RENAMED if destination else Gio.FileMonitorEvent.CHANGES_DONE_HINT)
                full.assert_called_once_with(refresh_ignored=True)
        with patch.object(self.watcher, "request_full") as full:
            self.watcher._on_git_metadata_change(".git/info/exclude", Gio.FileMonitorEvent.CHANGED)
            full.assert_called_once_with(refresh_ignored=True)

    def test_git_startup_is_strictly_sequential(self) -> None:
        """Git never starts branch or status alongside its ignore query."""

        self.assertEqual(self.calls[0][0][:3], ["git", "ls-files", "--others"])
        self.assertEqual(len(self.calls), 1)
        self._complete(0)
        self.assertEqual(self.calls[1][0], ["git", "branch", "--show-current"])
        self.assertEqual(len(self.calls), 2)
        self._complete(1, "main\n")
        self.assertEqual(self.calls[2][0][:3], ["git", "status", "--porcelain=v2"])
        self.assertEqual(len(self.calls), 3)

    def test_git_uses_explicit_metadata_and_current_branch_monitors(self) -> None:
        """Git avoids recursive metadata watches while retaining its active ref."""

        self._finish_startup()
        relatives = {
            Path(path).relative_to(self.root).as_posix()
            for path in self.watcher.metadata_monitors
        }
        self.assertEqual(
            relatives,
            {
                ".git/FETCH_HEAD",
                ".git/HEAD",
                ".git/index",
                ".git/index.lock",
                ".git/info/exclude",
                ".git/packed-refs",
                ".git/refs/heads/main",
            },
        )

    def test_git_branch_change_retargets_the_precise_ref_monitor(self) -> None:
        """Changing branch cancels the old ref monitor and watches the new ref."""

        self._finish_startup()
        previous_path = str(self.root / ".git" / "refs" / "heads" / "main")
        previous_monitor = self.watcher.metadata_monitors[previous_path]

        self.watcher._branch = "feature/topic"
        self.watcher._mount_git_branch_monitor()

        current_path = str(
            self.root / ".git" / "refs" / "heads" / "feature" / "topic"
        )
        self.assertTrue(previous_monitor.is_cancelled())
        self.assertNotIn(previous_path, self.watcher.metadata_monitors)
        self.assertIn(current_path, self.watcher.metadata_monitors)

    def test_git_worktree_change_uses_path_limited_status(self) -> None:
        """A small Git change receives the same bounded incremental refresh as HG."""

        self._finish_startup()
        self.watcher.request_paths(("dir/file.py",))
        self._drain_main_context()
        self.assertEqual(self.calls[3][0][-2:], ["--", ":(top,literal)dir/file.py"])

    def test_git_index_and_head_events_request_only_needed_metadata(self) -> None:
        """Index refreshes status while HEAD refreshes branch then status."""

        self._finish_startup()
        self.watcher.on_history_change = MagicMock()
        self.watcher._on_metadata_change(".git/index", 0)
        self._drain_main_context()
        self.watcher.on_history_change.assert_not_called()
        self.assertEqual(self.calls[3][0][:3], ["git", "status", "--porcelain=v2"])
        self._complete(3)
        self.watcher._on_metadata_change(".git/HEAD", 0)
        self._drain_main_context()
        self.watcher.on_history_change.assert_called_once_with()
        self.assertEqual(self.calls[4][0], ["git", "branch", "--show-current"])
        self._complete(4, "feature\n")
        self.assertEqual(self.calls[5][0][:3], ["git", "status", "--porcelain=v2"])

    def test_git_ref_change_refreshes_local_base_without_remote_commands(self) -> None:
        """A moved Git ref never authorizes an automatic remote command."""

        self._finish_startup()
        self.watcher.on_history_change = MagicMock()
        self.watcher._on_metadata_change(".git/refs/heads/main", 0)
        self._drain_main_context()
        self.watcher.on_history_change.assert_called_once_with()
        self.assertEqual(len(self.calls), 4)
        self.assertEqual(self.calls[3][0][:3], ["git", "status", "--porcelain=v2"])
        published = len(self.statuses)
        self._complete(3)
        self.assertEqual(len(self.statuses), published + 1)


if __name__ == "__main__":
    unittest.main()


class RepoWatcherLimitTest(_RepoWatcherSchedulingFixture, unittest.TestCase):
    """Exercise overflow, cancellation and probes through the real scheduler."""

    repository_marker = ".hg"
    scm_class = MercurialSCM

    def test_large_status_yields_and_stops_at_provable_limit(self) -> None:
        """A huge result yields after 100 entries and stops decoding after 2,000."""

        import json
        from slate.project_limit import ProjectStatusLimit
        from slate.scm.base import RepositoryRef

        self.watcher.row_limit = 1000
        gate = ProjectStatusLimit(MagicMock(), MagicMock())
        # The project hook takes a name, whereas the watcher is already bound.
        def block_project(_project: str) -> None:
            """Suspend this test's single repository on project overflow."""

            self.watcher.suspend_for_limit()

        gate.on_block.side_effect = block_project
        def received(rows: list[FileStatus], branch: str) -> None:
            """Pass normalized rows to the aggregate guard before UI publication."""

            gate.accept("project", RepositoryRef(".", "hg"), rows, branch)

        self.watcher.on_status = received
        self._complete(0)
        self._complete(1, "default")
        output = json.dumps([{"path": f"file-{index}", "status": "?"} for index in range(10000)])
        self._complete(2, output)
        self.assertTrue(self.watcher.processing)
        self.assertFalse(gate.blocked)
        self._drain_main_context()
        self.assertIn("project", gate.blocked)
        self.assertTrue(self.watcher.limited)
        self.assertEqual(len(self.watcher._statuses), 2000)
        self.assertFalse(self.watcher.monitors)
        self.assertFalse(self.watcher.metadata_monitors)
        self.assertIsNone(self.watcher.parse_idle_id)

    def test_suspended_watcher_ignores_old_completions_and_only_probes_manually(self) -> None:
        """Suspension invalidates an in-flight result and has no automatic retry."""

        self.watcher.suspend_for_limit()
        self._complete(0, "ignored/path")
        self.assertEqual(self.watcher.ignored, set())
        self.watcher.request_full()
        self.watcher.set_active(True)
        self._drain_main_context()
        self.assertEqual(len(self.calls), 1)
        self.watcher.probe_limit()
        self._drain_main_context()
        self.assertEqual(len(self.calls), 2)
        self._complete(1)
        self._complete(2, "default")
        self.watcher.request_full(refresh_ignored=True)
        self.watcher.request_paths(["edited-while-probing.txt"])
        self._complete(3, "[]")
        self.assertEqual(self.statuses, [([], "default")])
        self.assertEqual(len(self.calls), 4)
        self.assertFalse(self.watcher.monitors)
        self.assertFalse(self.watcher.metadata_monitors)
        self.assertIsNone(self.watcher.mount_idle_id)

    def test_limit_does_not_strand_an_already_requested_modal(self) -> None:
        """Suspending observers still releases the existing exclusive-operation waiter."""

        waiting = MagicMock()
        self.watcher.pause_after_current(waiting)
        self.watcher.suspend_for_limit()
        self._drain_main_context()
        waiting.assert_called_once_with()
        self.assertTrue(self.watcher.limited)
        self.assertFalse(self.watcher.metadata_monitors)

    def test_manual_recovery_refreshes_ignores_without_mounting_observers(self) -> None:
        """Updating ignore rules takes effect only through an explicit guarded full scan."""

        self.watcher.suspend_for_limit()
        self.watcher.probe_limit()
        self._drain_main_context()
        self._complete(1, "mangia-sano/file.txt\0")
        self.assertEqual(self.watcher.ignored, {"mangia-sano/file.txt"})
        self._complete(2, "main")
        self._complete(3, "[]")
        self.assertEqual(self.statuses, [([], "main")])
        self.assertTrue(self.watcher.limited)
        self.assertFalse(self.watcher.monitors)
        self.assertFalse(self.watcher.metadata_monitors)
        self.assertEqual(len(self.calls), 4)

    def test_one_wide_directory_yields_after_at_most_100_entries(self) -> None:
        """Entry budgeting works even when a directory has no child directories."""

        for index in range(250):
            (self.root / f"file-{index}").touch()
        self.watcher._mount_queue = deque([str(self.root)])
        self.watcher._mount_queued = {str(self.root)}
        self.assertEqual(self.watcher._mount_batch(), GLib.SOURCE_CONTINUE)
        self.assertIsNotNone(self.watcher._mount_entries)
        remaining = list(self.watcher._mount_entries)
        self.assertGreaterEqual(len(remaining), 151)
        self.watcher._mount_entries.close()
        self.watcher._mount_entries = None
