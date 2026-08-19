"""Dedicated asynchronous modal for conservative repository updates."""

from __future__ import annotations

from typing import Callable

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # noqa: E402

from .processes import CommandResult
from .repository_dialog import RepositoryOperationDialog
from .scm.base import SCM
from .scm.git import GitSCM
from .scm.hg import MercurialSCM
from .watcher import RepoWatcher


class RepositoryUpdateDialog(RepositoryOperationDialog):
    """Run one linear HG/Git update while presenting each asynchronous phase."""

    def __init__(
        self,
        parent: Gtk.Window,
        scm: SCM,
        watcher: RepoWatcher,
        on_closed: Callable[[], None],
    ) -> None:
        """Build the shared modal shell for the guarded update transaction."""

        super().__init__(
            parent,
            "Update repository",
            scm,
            watcher,
            on_closed,
            cancellation_title="Update cancelled",
            allow_idle_close=False,
        )
        self.current_node = ""
        self.upstream = ""

    def _begin(self) -> None:
        """Begin preflight after all automatic commands have become quiet."""

        # 2026-08-17: Update accepts only clean, linear histories; divergence is
        # reported here and never silently converted into a merge or rebase.
        if isinstance(self.scm, MercurialSCM):
            self._run_command(
                self.scm.update_merge_state_argv(),
                self._on_hg_merge_state,
                "Checking Mercurial status…",
            )
        elif isinstance(self.scm, GitSCM):
            self._run_command(
                self.scm.update_merge_state_argv(),
                self._on_git_merge_state,
                "Checking Git status…",
            )
        else:
            self._finish("Unsupported repository", "Update supports HG and Git.")

    def _on_hg_merge_state(self, result: CommandResult) -> None:
        """Reject an existing Mercurial merge before inspecting local changes."""

        if not self._prepare_result():
            return
        if not result.ok:
            self._finish_error("Mercurial merge check failed", result)
        elif result.stdout.strip():
            self._finish(
                "Merge already in progress",
                "Complete or abort the current merge before updating.",
            )
        else:
            self._run_command(
                self.scm.update_tracked_status_argv(),
                self._on_hg_status,
                "Checking local changes…",
            )

    def _on_hg_status(self, result: CommandResult) -> None:
        """Require a clean tracked Mercurial working copy."""

        if not self._prepare_result():
            return
        if not result.ok:
            self._finish_error("Failed to read Mercurial status", result)
            return
        try:
            dirty = bool(self.scm.parse_status(result.stdout))
        except (KeyError, TypeError, ValueError) as error:
            self._finish("Invalid Mercurial status", str(error))
            return
        if dirty:
            self._finish(
                "Local changes present",
                "Commit, revert, or otherwise preserve tracked changes before updating.",
            )
            return
        self._run_command(
            self.scm.update_remote_argv(),
            self._on_hg_remote,
            "Finding the remote Mercurial repository…",
        )

    def _on_hg_remote(self, result: CommandResult) -> None:
        """Require Mercurial's default pull path before starting network work."""

        if not self._prepare_result():
            return
        # 2026-08-17: a missing default path is configuration, not a generic
        # pull failure; identifying it before pull gives the user an actionable result.
        if result.returncode == 1 or (result.ok and not result.stdout.strip()):
            self._finish(
                "No remote source",
                "The repository has no default path: there is nothing to update.",
            )
            return
        if not result.ok:
            self._finish_error("Remote repository check failed", result)
            return
        self._run_command(
            self.scm.update_current_node_argv(),
            self._on_hg_current_node,
            "Recording the current revision…",
        )

    def _on_hg_current_node(self, result: CommandResult) -> None:
        """Retain the immutable pre-pull parent for no-op detection."""

        if not self._prepare_result():
            return
        self.current_node = result.stdout.strip()
        if not result.ok or not self.current_node:
            self._finish_error("Failed to read the Mercurial revision", result)
            return
        self._run_command(
            self.scm.pull_argv(),
            self._on_hg_pull,
            "Downloading Mercurial changes…",
            cancellable=True,
        )

    def _on_hg_pull(self, result: CommandResult) -> None:
        """Inspect current-branch heads only after a successful pull."""

        if not self._prepare_result():
            return
        if not result.ok:
            self._finish_error("Mercurial pull failed", result)
            return
        self._run_command(
            self.scm.update_heads_argv(),
            self._on_hg_heads,
            "Checking downloaded history…",
        )

    def _on_hg_heads(self, result: CommandResult) -> None:
        """Update to the sole branch head or stop on Mercurial divergence."""

        if not self._prepare_result():
            return
        if not result.ok:
            self._finish_error("Failed to read Mercurial heads", result)
            return
        try:
            heads = self.scm.parse_update_heads(result.stdout)
        except (KeyError, TypeError, ValueError) as error:
            self._finish("Invalid Mercurial head list", str(error))
            return
        if len(heads) != 1:
            self._finish(
                "Divergent history",
                "The current branch has multiple heads: the working copy was not updated. An explicit merge is required.",
            )
        elif heads[0] == self.current_node:
            self._finish("Repository already up to date", "There are no new revisions.")
        else:
            self._run_command(
                self.scm.update_to_argv(heads[0]),
                self._on_hg_update,
                "Updating the working copy…",
            )

    def _on_hg_update(self, result: CommandResult) -> None:
        """Report completion of the checked Mercurial working-copy update."""

        if not self._prepare_result():
            return
        if result.ok:
            self._finish("Repository updated", "The working copy is now on the new head.")
        else:
            self._finish_error("Mercurial update failed", result)

    def _on_git_merge_state(self, result: CommandResult) -> None:
        """Distinguish absent MERGE_HEAD from an active or failed Git query."""

        if not self._prepare_result():
            return
        if result.ok:
            self._finish(
                "Merge already in progress",
                "Complete or abort the current merge before updating.",
            )
        elif result.error is not None or result.returncode not in (1,):
            self._finish_error("Git merge check failed", result)
        else:
            self._run_command(
                self.scm.update_tracked_status_argv(),
                self._on_git_status,
                "Checking local changes…",
            )

    def _on_git_status(self, result: CommandResult) -> None:
        """Require a clean tracked Git index and working tree."""

        if not self._prepare_result():
            return
        if not result.ok:
            self._finish_error("Failed to read Git status", result)
            return
        try:
            dirty = bool(self.scm.parse_status(result.stdout))
        except (IndexError, TypeError, ValueError) as error:
            self._finish("Invalid Git status", str(error))
            return
        if dirty:
            self._finish(
                "Local changes present",
                "Commit, revert, or otherwise preserve tracked changes before updating.",
            )
            return
        self._run_command(
            self.scm.update_current_branch_argv(),
            self._on_git_branch,
            "Checking the current branch…",
        )

    def _on_git_branch(self, result: CommandResult) -> None:
        """Reject detached HEAD before resolving a Git upstream."""

        if not self._prepare_result():
            return
        if not result.ok or not result.stdout.strip():
            self._finish(
                "Detached HEAD",
                "Update requires an active local Git branch.",
            )
            return
        self._run_command(
            self.scm.remotes_argv(),
            self._on_git_remotes,
            "Finding the remote Git repository…",
        )

    def _on_git_remotes(self, result: CommandResult) -> None:
        """Treat a Git repository without remotes as a valid local repository."""

        if not self._prepare_result():
            return
        if not result.ok:
            self._finish_error("Failed to read Git remotes", result)
            return
        if not result.stdout.split():
            self._finish(
                "No remote source",
                "This is a local repository: there is nothing to update.",
            )
            return
        self._run_command(
            self.scm.update_upstream_argv(),
            self._on_git_upstream,
            "Finding the branch remote…",
        )

    def _on_git_upstream(self, result: CommandResult) -> None:
        """Retain the explicit upstream or explain that none is configured."""

        if not self._prepare_result():
            return
        self.upstream = result.stdout.strip()
        if not result.ok or not self.upstream:
            self._finish(
                "Upstream not configured",
                "The current Git branch does not specify a remote branch to update from.",
            )
            return
        environment = dict(self.scm.environment)
        environment["GIT_TERMINAL_PROMPT"] = "0"
        self._run_command(
            self.scm.fetch_argv(),
            self._on_git_fetch,
            "Downloading Git changes…",
            cancellable=True,
            environment=environment,
        )

    def _on_git_fetch(self, result: CommandResult) -> None:
        """Compare local and upstream history after a successful Git fetch."""

        if not self._prepare_result():
            return
        if not result.ok:
            self._finish_error("Git fetch failed", result)
            return
        self._run_command(
            self.scm.update_comparison_argv(self.upstream),
            self._on_git_comparison,
            "Comparing local and remote history…",
        )

    def _on_git_comparison(self, result: CommandResult) -> None:
        """Fast-forward only when Git reports an exclusively remote advance."""

        if not self._prepare_result():
            return
        if not result.ok:
            self._finish_error("Git comparison failed", result)
            return
        try:
            local_ahead, remote_ahead = self.scm.parse_update_comparison(
                result.stdout
            )
        except ValueError as error:
            self._finish("Invalid Git comparison", str(error))
            return
        if not local_ahead and not remote_ahead:
            self._finish("Repository already up to date", "There are no new commits.")
        elif local_ahead and not remote_ahead:
            self._finish(
                "Unpublished local commits",
                "The local branch is already ahead of its upstream; the working copy was not changed.",
            )
        elif local_ahead and remote_ahead:
            self._finish(
                "Divergent history",
                "Local and upstream contain different commits: the working copy was not changed. An explicit merge is required.",
            )
        else:
            self._run_command(
                self.scm.fast_forward_argv(self.upstream),
                self._on_git_fast_forward,
                "Updating the working copy…",
            )

    def _on_git_fast_forward(self, result: CommandResult) -> None:
        """Report completion of the guarded Git fast-forward."""

        if not self._prepare_result():
            return
        if result.ok:
            self._finish("Repository updated", "The local branch is now aligned with its upstream.")
        else:
            self._finish_error("Git fast-forward failed", result)
