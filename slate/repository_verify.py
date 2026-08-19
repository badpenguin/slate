"""Explicit remote-state verification for Git and Mercurial repositories."""

from __future__ import annotations

from collections.abc import Callable, Mapping

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # noqa: E402

from .processes import CommandResult
from .repository_dialog import RepositoryOperationDialog
from .scm.base import RepositorySyncStatus, SCM
from .scm.git import GitSCM
from .scm.hg import MercurialSCM
from .watcher import RepoWatcher


def _git_verification_environment(base: Mapping[str, str]) -> dict[str, str]:
    """Disable interactive Git/SSH credential prompts for verification."""

    environment = dict(base)
    # 2026-08-19: verification is explicit but informational; it may reuse
    # silent credential helpers and SSH agents, but must never open a login UI.
    environment.update(
        {
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": "/bin/false",
            "SSH_ASKPASS": "/bin/false",
            "SSH_ASKPASS_REQUIRE": "never",
            "GIT_SSH_COMMAND": "ssh -oBatchMode=yes",
            "GCM_INTERACTIVE": "Never",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "credential.interactive",
            "GIT_CONFIG_VALUE_0": "false",
        }
    )
    return environment


def _mercurial_verification_environment(
    base: Mapping[str, str],
) -> dict[str, str]:
    """Disable graphical SSH password prompts for Mercurial verification."""

    environment = dict(base)
    environment["SSH_ASKPASS"] = "/bin/false"
    environment["SSH_ASKPASS_REQUIRE"] = "never"
    return environment


class RepositoryVerifyDialog(RepositoryOperationDialog):
    """Compare one local branch with its configured remote without updating it."""

    def __init__(
        self,
        parent: Gtk.Window,
        scm: SCM,
        watcher: RepoWatcher,
        on_closed: Callable[[], None],
        on_verified: Callable[[RepositorySyncStatus], None],
    ) -> None:
        """Build the verification modal and retain its result callback."""

        super().__init__(
            parent,
            "Verify repository",
            scm,
            watcher,
            on_closed,
            cancellation_title="Verification cancelled",
            allow_idle_close=False,
        )
        self.on_verified = on_verified
        self.branch = ""
        self.upstream = ""
        self.incoming = 0

    def _begin(self) -> None:
        """Start the SCM-specific explicit remote comparison."""

        if isinstance(self.scm, GitSCM):
            self._run_command(
                self.scm.update_current_branch_argv(),
                self._on_git_branch,
                "Checking the current Git branch…",
            )
        elif isinstance(self.scm, MercurialSCM):
            self._run_command(
                self.scm.branch_argv(),
                self._on_hg_branch,
                "Checking the current Mercurial branch…",
            )
        else:
            self._finish_verified(
                RepositorySyncStatus(),
                "Unsupported repository",
                "Verification supports Git and Mercurial.",
            )

    def _on_git_branch(self, result: CommandResult) -> None:
        """Reject detached HEAD before resolving a Git remote."""

        if not self._prepare_result():
            return
        self.branch = result.stdout.strip()
        if result.returncode == 1 and result.error is None:
            self._finish_verified(
                RepositorySyncStatus("detached"),
                "Detached HEAD",
                "Remote verification requires an active local branch.",
            )
        elif not result.ok or not self.branch:
            self._finish_verification_error("Git branch check failed", result)
        else:
            self._run_command(
                self.scm.remotes_argv(),
                self._on_git_remotes,
                "Looking for configured Git remotes…",
            )

    def _on_git_remotes(self, result: CommandResult) -> None:
        """Treat a Git repository without remotes as intentionally local."""

        if not self._prepare_result():
            return
        if not result.ok:
            self._finish_verification_error("Git remote check failed", result)
        elif not result.stdout.split():
            self._finish_verified(
                RepositorySyncStatus("local"),
                "Local repository",
                "No Git remote is configured.",
            )
        else:
            self._run_command(
                self.scm.update_upstream_argv(),
                self._on_git_upstream,
                "Resolving the Git upstream…",
            )

    def _on_git_upstream(self, result: CommandResult) -> None:
        """Require an existing upstream without configuring one implicitly."""

        if not self._prepare_result():
            return
        self.upstream = result.stdout.strip()
        if not result.ok or not self.upstream:
            self._finish_verified(
                RepositorySyncStatus("unconfigured"),
                "Upstream not configured",
                "The current Git branch has no configured upstream.",
            )
            return
        self._run_command(
            self.scm.fetch_argv(),
            self._on_git_fetch,
            "Fetching Git remote state…",
            cancellable=True,
            environment=_git_verification_environment(self.scm.environment),
        )

    def _on_git_fetch(self, result: CommandResult) -> None:
        """Compare Git histories only after a successful non-interactive fetch."""

        if not self._prepare_result():
            return
        if not result.ok:
            self._finish_access_required(result)
            return
        self._run_command(
            self.scm.update_comparison_argv(self.upstream),
            self._on_git_comparison,
            "Comparing local and upstream Git history…",
        )

    def _on_git_comparison(self, result: CommandResult) -> None:
        """Publish normalized ahead and behind counts from Git."""

        if not self._prepare_result():
            return
        if not result.ok:
            self._finish_verification_error("Git comparison failed", result)
            return
        try:
            ahead, behind = self.scm.parse_update_comparison(result.stdout)
        except (TypeError, ValueError) as error:
            self._finish_verified(
                RepositorySyncStatus(),
                "Invalid Git comparison",
                str(error),
            )
            return
        self._finish_counts(ahead, behind)

    def _on_hg_branch(self, result: CommandResult) -> None:
        """Read the named Mercurial branch used by both comparisons."""

        if not self._prepare_result():
            return
        self.branch = result.stdout.strip()
        if not result.ok or not self.branch:
            self._finish_verification_error("Mercurial branch check failed", result)
            return
        self._run_command(
            self.scm.update_remote_argv(),
            self._on_hg_remote,
            "Looking for the Mercurial default path…",
        )

    def _on_hg_remote(self, result: CommandResult) -> None:
        """Treat a missing Mercurial default path as a local repository."""

        if not self._prepare_result():
            return
        if result.returncode == 1 and result.error is None:
            self._finish_verified(
                RepositorySyncStatus("local"),
                "Local repository",
                "No Mercurial default path is configured.",
            )
        elif not result.ok or not result.stdout.strip():
            self._finish_verification_error("Mercurial remote check failed", result)
        else:
            self._run_command(
                self.scm.verify_incoming_argv(self.branch),
                self._on_hg_incoming,
                "Checking incoming Mercurial changesets…",
                cancellable=True,
                environment=_mercurial_verification_environment(
                    self.scm.environment
                ),
            )

    def _on_hg_incoming(self, result: CommandResult) -> None:
        """Retain incoming changes before starting the outgoing comparison."""

        if not self._prepare_result():
            return
        if result.error is not None or result.returncode not in (0, 1):
            self._finish_access_required(result)
            return
        try:
            self.incoming = (
                self.scm.parse_verify_count(result.stdout)
                if result.returncode == 0
                else 0
            )
        except (TypeError, ValueError) as error:
            self._finish_verified(
                RepositorySyncStatus(),
                "Invalid Mercurial comparison",
                str(error),
            )
            return
        self._run_command(
            self.scm.verify_outgoing_argv(self.branch),
            self._on_hg_outgoing,
            "Checking outgoing Mercurial changesets…",
            cancellable=True,
            environment=_mercurial_verification_environment(self.scm.environment),
        )

    def _on_hg_outgoing(self, result: CommandResult) -> None:
        """Publish normalized incoming and outgoing Mercurial counts."""

        if not self._prepare_result():
            return
        if result.error is not None or result.returncode not in (0, 1):
            self._finish_access_required(result)
            return
        try:
            outgoing = (
                self.scm.parse_verify_count(result.stdout)
                if result.returncode == 0
                else 0
            )
        except (TypeError, ValueError) as error:
            self._finish_verified(
                RepositorySyncStatus(),
                "Invalid Mercurial comparison",
                str(error),
            )
            return
        self._finish_counts(outgoing, self.incoming)

    def _finish_counts(self, ahead: int, behind: int) -> None:
        """Finish with one shared relationship label and exact counts."""

        if ahead and behind:
            state = "diverged"
            title = "Repository histories diverged"
        elif ahead:
            state = "ahead"
            title = "Local repository is ahead"
        elif behind:
            state = "behind"
            title = "Local repository is behind"
        else:
            state = "synced"
            title = "Repository is up to date"
        detail = f"Ahead: {ahead} · Behind: {behind}"
        self._finish_verified(
            RepositorySyncStatus(state, ahead, behind), title, detail
        )

    def _finish_access_required(self, result: CommandResult) -> None:
        """Expose a failed non-interactive remote access without prompting."""

        self.on_verified(RepositorySyncStatus("access_required"))
        self._finish_error("Remote access required", result)

    def _finish_verification_error(
        self, title: str, result: CommandResult
    ) -> None:
        """Keep the repository unverified after a local command failure."""

        self.on_verified(RepositorySyncStatus())
        self._finish_error(title, result)

    def _finish_verified(
        self,
        status: RepositorySyncStatus,
        title: str,
        detail: str,
    ) -> None:
        """Publish a terminal verification status and finish the modal."""

        self.on_verified(status)
        self._finish(title, detail)
