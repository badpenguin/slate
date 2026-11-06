"""Verify project accessibility through real Gio events without GUI windows."""

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from gi.repository import Gio, GLib

from slate.project_availability import ProjectAvailability


class ProjectAvailabilityTest(unittest.TestCase):
    """Exercise disappearance, recreation, permissions and stale access requests."""

    def setUp(self) -> None:
        """Create a disposable project and a shared access tracker."""

        self.temporary = tempfile.TemporaryDirectory(dir="/tmp", prefix="slate-access-")
        self.root = Path(self.temporary.name) / "container" / "project"
        self.root.mkdir(parents=True)
        self.changed = MagicMock()
        self.tracker = ProjectAvailability(self.changed)

    def tearDown(self) -> None:
        """Cancel all asynchronous work before deleting the private filesystem fixture."""

        self.tracker.close()
        self.temporary.cleanup()

    def _wait_state(self, expected: bool) -> None:
        """Pump Gio until the project reaches its expected cached state, with a deadline."""

        deadline = time.monotonic() + 4
        context = GLib.MainContext.default()
        while self.tracker.available.get("project") is not expected and time.monotonic() < deadline:
            context.iteration(False)
            time.sleep(0.005)
        self.assertIs(self.tracker.available.get("project"), expected)

    def test_deleted_project_and_parent_recover_from_filesystem_events(self) -> None:
        """Ancestor events recover a project whose directory and container were removed."""

        self.tracker.set_projects([{"name": "project", "path": str(self.root)}])
        self._wait_state(True)
        self.root.rmdir()
        self.root.parent.rmdir()
        self._wait_state(False)
        self.root.mkdir(parents=True)
        self._wait_state(True)

    def test_initially_missing_ancestors_and_replaced_directory_recover(self) -> None:
        """Missing roots and regular files stay unavailable until a real directory returns."""

        self.root.rmdir()
        self.root.parent.rmdir()
        self.tracker.set_projects([{"name": "project", "path": str(self.root)}])
        self._wait_state(False)
        self.root.parent.mkdir()
        self.root.write_text("not a directory")
        self.changed.reset_mock()
        self.tracker.refresh("project")
        # Wait for the request even though the cached state is already False.
        deadline = time.monotonic() + 4
        context = GLib.MainContext.default()
        while self.tracker.requests and time.monotonic() < deadline:
            context.iteration(False)
            time.sleep(0.005)
        self.assertFalse(self.tracker.available["project"])
        self.root.unlink()
        self.root.mkdir()
        self._wait_state(True)

    def test_access_requires_both_read_and_search_permission(self) -> None:
        """Gio's effective permission flags decide access even under a privileged test runner."""

        self.tracker.roots = {"project": str(self.root)}
        for readable, searchable in ((False, True), (True, False), (True, True)):
            with self.subTest(read=readable, search=searchable):
                info = Gio.FileInfo()
                info.set_file_type(Gio.FileType.DIRECTORY)
                info.set_attribute_boolean("access::can-read", readable)
                info.set_attribute_boolean("access::can-execute", searchable)
                token = Gio.Cancellable()
                self.tracker.requests["project"] = token
                file = MagicMock()
                file.query_info_finish.return_value = info
                self.tracker._checked("project", token, file, object())
                self.assertEqual(self.tracker.available["project"], readable and searchable)

    def test_removed_project_ignores_old_results_and_releases_monitors(self) -> None:
        """An obsolete request cannot restore a removed project's cached state."""

        self.tracker.set_projects([{"name": "project", "path": str(self.root)}])
        token = self.tracker.requests["project"]
        self.tracker.set_projects([])
        self.assertTrue(token.is_cancelled())
        self.assertEqual(self.tracker.monitors, {})
        file = MagicMock()
        info = Gio.FileInfo()
        info.set_file_type(Gio.FileType.DIRECTORY)
        file.query_info_finish.return_value = info
        self.tracker._checked("project", token, file, object())
        self.assertEqual(self.tracker.available, {})
        self.changed.assert_not_called()
