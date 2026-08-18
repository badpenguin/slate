"""Tests for bounded nested Mercurial repository discovery."""

import tempfile
import unittest
from operator import attrgetter
from pathlib import Path

from gi.repository import GLib

from slate.repository_discovery import RepositoryDiscovery
from slate.scm.base import RepositoryRef


def _make_hg(root: Path) -> None:
    """Create the minimum metadata written by a normal Mercurial init."""

    (root / ".hg").mkdir(parents=True)
    (root / ".hg" / "requires").write_text("revlogv1\n", encoding="utf-8")


def _make_git(root: Path) -> None:
    """Create the minimum metadata used to recognize a normal Git working copy."""

    (root / ".git").mkdir(parents=True)
    (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (root / ".git" / "config").write_text("[core]\n", encoding="utf-8")


class RepositoryDiscoveryTest(unittest.TestCase):
    """Verify discovery boundaries, exclusions and stable relative identities."""

    def test_repository_reference_never_equals_an_untyped_path(self) -> None:
        """Repository identity always requires both its path and SCM type."""

        self.assertNotEqual(RepositoryRef(".", "hg"), ".")

    def test_finds_sibling_repositories_and_keeps_empty_working_copies(self) -> None:
        """A .hg marker is enough to discover repositories without changed files."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _make_hg(root / "app")
            _make_hg(root / "public" / "theme")
            found: list[RepositoryRef] = []
            completed: list[str | None] = []
            discovery = RepositoryDiscovery(
                str(root), set(), found.append, completed.append
            )
            while discovery._scan_batch() == GLib.SOURCE_CONTINUE:
                pass
            self.assertEqual(
                sorted(found, key=attrgetter("path")),
                [RepositoryRef("app", "hg"), RepositoryRef("public/theme", "hg")],
            )
            self.assertEqual(completed, [None])

    def test_exclusion_prunes_one_repository_without_hiding_siblings(self) -> None:
        """Excluded repository paths are skipped while later siblings remain visible."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _make_hg(root / "excluded")
            _make_hg(root / "visible")
            found: list[RepositoryRef] = []
            completed: list[str | None] = []
            discovery = RepositoryDiscovery(
                str(root),
                {RepositoryRef("excluded", "hg")},
                found.append,
                completed.append,
            )
            while discovery._scan_batch() == GLib.SOURCE_CONTINUE:
                pass
            self.assertEqual(found, [RepositoryRef("visible", "hg")])
            self.assertEqual(completed, [None])

    def test_nested_scan_stops_at_first_working_copy(self) -> None:
        """Discovery never descends into an already identified working copy."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _make_hg(root / "outer")
            _make_hg(root / "outer" / "nested")
            found: list[RepositoryRef] = []
            discovery = RepositoryDiscovery(
                str(root), set(), found.append, self._ignore_completion
            )
            while discovery._scan_batch() == GLib.SOURCE_CONTINUE:
                pass
            self.assertEqual(found, [RepositoryRef("outer", "hg")])

    def test_root_repository_does_not_hide_nested_repositories(self) -> None:
        """A workspace-root .hg is reported while scanning continues below it."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _make_hg(root)
            _make_hg(root / "nested")
            found: list[RepositoryRef] = []
            discovery = RepositoryDiscovery(
                str(root), set(), found.append, self._ignore_completion
            )
            while discovery._scan_batch() == GLib.SOURCE_CONTINUE:
                pass
            self.assertEqual(
                found,
                [RepositoryRef(".", "hg"), RepositoryRef("nested", "hg")],
            )

    def test_git_directory_is_discovered_but_git_file_is_not(self) -> None:
        """Only normal Git repositories enter the cache, not worktree marker files."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _make_git(root / "normal")
            (root / "worktree").mkdir()
            (root / "worktree" / ".git").write_text("gitdir: elsewhere")
            found: list[RepositoryRef] = []
            discovery = RepositoryDiscovery(
                str(root), set(), found.append, self._ignore_completion
            )
            while discovery._scan_batch() == GLib.SOURCE_CONTINUE:
                pass
            self.assertEqual(found, [RepositoryRef("normal", "git")])

    def test_empty_git_directory_is_not_a_repository(self) -> None:
        """Infrastructure-created empty .git directories never start a watcher."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".git").mkdir()
            found: list[RepositoryRef] = []
            discovery = RepositoryDiscovery(
                str(root), set(), found.append, self._ignore_completion
            )
            while discovery._scan_batch() == GLib.SOURCE_CONTINUE:
                pass
            self.assertEqual(found, [])

    @staticmethod
    def _ignore_completion(_error: str | None) -> None:
        """Accept completion when only discovered paths matter to the assertion."""


if __name__ == "__main__":
    unittest.main()
