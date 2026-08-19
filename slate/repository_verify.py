"""Explicit remote-state verification for Git and Mercurial repositories."""

from __future__ import annotations

from collections.abc import Callable, Mapping

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # noqa: E402

from .git_credentials import CREDENTIAL_CACHE_TIMEOUT_SECONDS
from .processes import AsyncCommand, CommandResult, run_async
from .repository_dialog import RepositoryOperationDialog
from .scm.base import RepositorySyncStatus, SCM
from .scm.git import GitSCM
from .scm.hg import MercurialSCM
from .watcher import RepoWatcher


def _git_verification_environment(base: Mapping[str, str]) -> dict[str, str]:
    """Disable interactive Git/SSH credential prompts for verification."""

    environment = dict(base)
    # 2026-08-19: verification may reuse only Git's memory cache and SSH agent;
    # clearing the helper list prevents global helpers from opening login UI.
    environment.update(
        {
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": "/bin/false",
            "SSH_ASKPASS": "/bin/false",
            "SSH_ASKPASS_REQUIRE": "never",
            "GIT_SSH_COMMAND": "ssh -oBatchMode=yes",
            "GCM_INTERACTIVE": "Never",
            "GIT_CONFIG_COUNT": "3",
            "GIT_CONFIG_KEY_0": "credential.helper",
            "GIT_CONFIG_VALUE_0": "",
            "GIT_CONFIG_KEY_1": "credential.helper",
            "GIT_CONFIG_VALUE_1": (
                f"cache --timeout={CREDENTIAL_CACHE_TIMEOUT_SECONDS}"
            ),
            "GIT_CONFIG_KEY_2": "credential.interactive",
            "GIT_CONFIG_VALUE_2": "false",
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


class _RepositoryVerificationWorkflow:
    """Share the asynchronous Git/HG comparison state machine across frontends."""

    def _initialize_verification(
        self, on_verified: Callable[[RepositorySyncStatus], None]
    ) -> None:
        """Initialize mutable state used by one repository comparison."""

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
        self._finish_verified(
            RepositorySyncStatus(state, ahead, behind),
            title,
            f"Ahead: {ahead} · Behind: {behind}",
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


class UnattendedRepositoryVerifier(_RepositoryVerificationWorkflow):
    """Run one lazy remote comparison without constructing or showing a dialog."""

    def __init__(
        self,
        scm: SCM,
        watcher: RepoWatcher,
        on_verified: Callable[[RepositorySyncStatus], None],
        on_closed: Callable[[], None],
    ) -> None:
        """Retain one repository, its watcher boundary and completion callbacks."""

        self.scm = scm
        self.watcher = watcher
        self.on_closed = on_closed
        self.command: AsyncCommand | None = None
        self.watcher_pause_requested = False
        self.closed = False
        self._initialize_verification(on_verified)

    def start(self) -> None:
        """Wait for the watcher read in flight before starting remote work."""

        self.watcher_pause_requested = True
        self.watcher.pause_after_current(self._on_watcher_paused)

    def _on_watcher_paused(self) -> None:
        """Start verification after obtaining exclusive repository access."""

        if self.closed:
            return
        self._begin()

    def _run_command(
        self,
        argv: list[str],
        callback: Callable[[CommandResult], None],
        _phase: str,
        *,
        cancellable: bool = False,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        """Launch one serialized command without exposing progress UI."""

        del cancellable
        self.command = run_async(
            argv,
            callback,
            cwd=self.scm.root,
            env=environment or self.scm.environment,
        )

    def _prepare_result(self) -> bool:
        """Release the completed command and reject callbacks after closure."""

        self.command = None
        return not self.closed

    def _finish_verified(
        self,
        status: RepositorySyncStatus,
        _title: str,
        _detail: str,
    ) -> None:
        """Publish a terminal informational status without presenting UI."""

        self.on_verified(status)
        self._close()

    def _finish_error(self, _title: str, _result: CommandResult) -> None:
        """Finish silently after the workflow publishes its failure status."""

        self._close()

    def close(self) -> None:
        """Cancel an unfinished verification and release its watcher boundary."""

        if self.command is not None:
            self.command.cancel()
            self.command = None
        self._close()

    def _close(self) -> None:
        """Resume the watcher once and notify the unattended queue."""

        if self.closed:
            return
        self.closed = True
        if self.watcher_pause_requested:
            self.watcher_pause_requested = False
            self.watcher.resume_with_full_refresh(refresh_branch=True)
        self.on_closed()


class RepositoryVerifyDialog(
    _RepositoryVerificationWorkflow, RepositoryOperationDialog
):
    """Compare one repository with its remote through an explicit modal."""

    def __init__(
        self,
        parent: Gtk.Window,
        scm: SCM,
        watcher: RepoWatcher,
        on_closed: Callable[[], None],
        on_verified: Callable[[RepositorySyncStatus], None],
    ) -> None:
        """Build the verification modal and retain its result callback."""

        RepositoryOperationDialog.__init__(
            self,
            parent,
            "Verify repository",
            scm,
            watcher,
            on_closed,
            cancellation_title="Verification cancelled",
            allow_idle_close=False,
        )
        self._initialize_verification(on_verified)
