"""Headless tests of aggregate limits and atomic manual recovery."""

import unittest
from unittest.mock import MagicMock

from slate.project_limit import ProjectStatusLimit
from slate.scm.base import FileStatus, RepositoryRef


class ProjectLimitTest(unittest.TestCase):
    """Verify exact thresholds independently of GTK and external processes."""

    def setUp(self) -> None:
        """Create inert lifecycle hooks and one project coordinator."""

        self.block = MagicMock()
        self.ready = MagicMock()
        self.limit = ProjectStatusLimit(self.block, self.ready)
        self.root = RepositoryRef(".", "hg")
        self.child = RepositoryRef("child", "git")

    @staticmethod
    def _rows(count: int) -> list[FileStatus]:
        """Build distinct untracked paths without touching the filesystem."""

        return [FileStatus(f"file-{index}.txt", "untracked") for index in range(count)]

    def test_999_allowed_and_1000_blocks_before_publication(self) -> None:
        """The boundary is inclusive and an oversized snapshot is never cached."""

        self.assertTrue(self.limit.accept("project", self.root, self._rows(999), "main"))
        self.assertFalse(self.limit.accept("project", self.root, self._rows(1000), "main"))
        self.block.assert_called_once_with("project")
        self.assertEqual(len(self.limit.snapshots["project"][self.root][0]), 999)
        self.assertFalse(self.limit.accept("project", self.root, [], "main"))
        self.assertIn("project", self.limit.blocked)

    def test_aggregate_replacement_and_other_projects(self) -> None:
        """Incremental replacement counts once; repositories share the allowance."""

        self.assertTrue(self.limit.accept("project", self.root, self._rows(600), ""))
        self.assertTrue(self.limit.accept("project", self.root, self._rows(500), ""))
        self.assertTrue(self.limit.accept("project", self.child, self._rows(499), ""))
        self.assertFalse(self.limit.accept("project", self.child, self._rows(500), ""))
        self.assertTrue(self.limit.accept("other", self.root, self._rows(999), ""))

    def test_recovery_waits_for_all_repositories(self) -> None:
        """A clean first repository cannot prematurely resume a blocked project."""

        self.limit.accept("project", self.root, self._rows(1001), "")
        self.limit.begin_scan("project", {self.root, self.child})
        self.assertFalse(self.limit.accept("project", self.root, self._rows(400), "new"))
        self.assertIn("project", self.limit.blocked)
        self.ready.assert_not_called()
        self.assertFalse(self.limit.accept("project", self.child, self._rows(599), "main"))
        self.assertNotIn("project", self.limit.blocked)
        self.ready.assert_called_once_with("project")

    def test_recovery_overflow_and_errors_retain_block(self) -> None:
        """A failed scan keeps stale/late callbacks from unblocking the project."""

        self.limit.accept("project", self.root, self._rows(1000), "")
        self.limit.begin_scan("project", {self.root, self.child})
        self.limit.accept("project", self.root, self._rows(600), "")
        self.limit.accept("project", self.child, self._rows(400), "")
        self.assertIn("project", self.limit.blocked)
        self.assertNotIn("project", self.limit.pending)
        self.limit.begin_scan("project", {self.root})
        self.limit.fail_scan("project")
        self.assertFalse(self.limit.accept("project", self.root, [], ""))
        self.ready.assert_not_called()

    def test_normal_errors_do_not_create_a_limit_block(self) -> None:
        """Repository failures are not reported as an overload."""

        self.limit.fail_scan("project")
        self.assertNotIn("project", self.limit.blocked)
        self.block.assert_not_called()

    def test_removal_and_empty_manual_scan(self) -> None:
        """Removed repositories stop counting; removing the project clears its latch."""

        self.limit.accept("project", self.root, self._rows(900), "")
        self.limit.remove_repository("project", self.root)
        self.assertTrue(self.limit.accept("project", self.child, self._rows(500), ""))
        self.limit.accept("project", self.root, self._rows(1000), "")
        self.limit.begin_scan("project", set())
        self.ready.assert_called_once_with("project")
        self.limit.forget_project("project")
        self.assertNotIn("project", self.limit.snapshots)
