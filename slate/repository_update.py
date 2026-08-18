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
            "Aggiorna repository",
            scm,
            watcher,
            on_closed,
            cancellation_title="Aggiornamento annullato",
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
                "Verifico lo stato Mercurial…",
            )
        elif isinstance(self.scm, GitSCM):
            self._run_command(
                self.scm.update_merge_state_argv(),
                self._on_git_merge_state,
                "Verifico lo stato Git…",
            )
        else:
            self._finish("Repository non supportato", "Aggiorna supporta HG e Git.")

    def _on_hg_merge_state(self, result: CommandResult) -> None:
        """Reject an existing Mercurial merge before inspecting local changes."""

        if not self._prepare_result():
            return
        if not result.ok:
            self._finish_error("Verifica del merge Mercurial fallita", result)
        elif result.stdout.strip():
            self._finish(
                "Merge già in corso",
                "Concludi o annulla il merge corrente prima di aggiornare.",
            )
        else:
            self._run_command(
                self.scm.update_tracked_status_argv(),
                self._on_hg_status,
                "Controllo le modifiche locali…",
            )

    def _on_hg_status(self, result: CommandResult) -> None:
        """Require a clean tracked Mercurial working copy."""

        if not self._prepare_result():
            return
        if not result.ok:
            self._finish_error("Lettura dello stato Mercurial fallita", result)
            return
        try:
            dirty = bool(self.scm.parse_status(result.stdout))
        except (KeyError, TypeError, ValueError) as error:
            self._finish("Stato Mercurial non valido", str(error))
            return
        if dirty:
            self._finish(
                "Modifiche locali presenti",
                "Commit, ripristina o metti al sicuro le modifiche tracciate prima di aggiornare.",
            )
            return
        self._run_command(
            self.scm.update_remote_argv(),
            self._on_hg_remote,
            "Cerco il repository remoto Mercurial…",
        )

    def _on_hg_remote(self, result: CommandResult) -> None:
        """Require Mercurial's default pull path before starting network work."""

        if not self._prepare_result():
            return
        # 2026-08-17: a missing default path is configuration, not a generic
        # pull failure; identifying it before pull gives the user an actionable result.
        if result.returncode == 1 or (result.ok and not result.stdout.strip()):
            self._finish(
                "Nessuna sorgente remota",
                "Il repository non ha un percorso “default”: non c'è nulla da aggiornare.",
            )
            return
        if not result.ok:
            self._finish_error("Verifica del repository remoto fallita", result)
            return
        self._run_command(
            self.scm.update_current_node_argv(),
            self._on_hg_current_node,
            "Memorizzo la revisione corrente…",
        )

    def _on_hg_current_node(self, result: CommandResult) -> None:
        """Retain the immutable pre-pull parent for no-op detection."""

        if not self._prepare_result():
            return
        self.current_node = result.stdout.strip()
        if not result.ok or not self.current_node:
            self._finish_error("Lettura della revisione Mercurial fallita", result)
            return
        self._run_command(
            self.scm.pull_argv(),
            self._on_hg_pull,
            "Scarico le modifiche Mercurial…",
            cancellable=True,
        )

    def _on_hg_pull(self, result: CommandResult) -> None:
        """Inspect current-branch heads only after a successful pull."""

        if not self._prepare_result():
            return
        if not result.ok:
            self._finish_error("Pull Mercurial fallito", result)
            return
        self._run_command(
            self.scm.update_heads_argv(),
            self._on_hg_heads,
            "Controllo la storia scaricata…",
        )

    def _on_hg_heads(self, result: CommandResult) -> None:
        """Update to the sole branch head or stop on Mercurial divergence."""

        if not self._prepare_result():
            return
        if not result.ok:
            self._finish_error("Lettura delle head Mercurial fallita", result)
            return
        try:
            heads = self.scm.parse_update_heads(result.stdout)
        except (KeyError, TypeError, ValueError) as error:
            self._finish("Elenco head Mercurial non valido", str(error))
            return
        if len(heads) != 1:
            self._finish(
                "Storia divergente",
                "Il branch corrente ha più head: la working copy non è stata aggiornata. Serve un merge esplicito.",
            )
        elif heads[0] == self.current_node:
            self._finish("Repository già aggiornato", "Non ci sono nuove revisioni.")
        else:
            self._run_command(
                self.scm.update_to_argv(heads[0]),
                self._on_hg_update,
                "Aggiorno la working copy…",
            )

    def _on_hg_update(self, result: CommandResult) -> None:
        """Report completion of the checked Mercurial working-copy update."""

        if not self._prepare_result():
            return
        if result.ok:
            self._finish("Repository aggiornato", "La working copy è sulla nuova head.")
        else:
            self._finish_error("Update Mercurial fallito", result)

    def _on_git_merge_state(self, result: CommandResult) -> None:
        """Distinguish absent MERGE_HEAD from an active or failed Git query."""

        if not self._prepare_result():
            return
        if result.ok:
            self._finish(
                "Merge già in corso",
                "Concludi o annulla il merge corrente prima di aggiornare.",
            )
        elif result.error is not None or result.returncode not in (1,):
            self._finish_error("Verifica del merge Git fallita", result)
        else:
            self._run_command(
                self.scm.update_tracked_status_argv(),
                self._on_git_status,
                "Controllo le modifiche locali…",
            )

    def _on_git_status(self, result: CommandResult) -> None:
        """Require a clean tracked Git index and working tree."""

        if not self._prepare_result():
            return
        if not result.ok:
            self._finish_error("Lettura dello stato Git fallita", result)
            return
        try:
            dirty = bool(self.scm.parse_status(result.stdout))
        except (IndexError, TypeError, ValueError) as error:
            self._finish("Stato Git non valido", str(error))
            return
        if dirty:
            self._finish(
                "Modifiche locali presenti",
                "Commit, ripristina o metti al sicuro le modifiche tracciate prima di aggiornare.",
            )
            return
        self._run_command(
            self.scm.update_current_branch_argv(),
            self._on_git_branch,
            "Controllo il branch corrente…",
        )

    def _on_git_branch(self, result: CommandResult) -> None:
        """Reject detached HEAD before resolving a Git upstream."""

        if not self._prepare_result():
            return
        if not result.ok or not result.stdout.strip():
            self._finish(
                "HEAD scollegata",
                "Aggiorna richiede un branch Git locale attivo.",
            )
            return
        self._run_command(
            self.scm.remotes_argv(),
            self._on_git_remotes,
            "Cerco il repository remoto Git…",
        )

    def _on_git_remotes(self, result: CommandResult) -> None:
        """Treat a Git repository without remotes as a valid local repository."""

        if not self._prepare_result():
            return
        if not result.ok:
            self._finish_error("Lettura dei remote Git fallita", result)
            return
        if not result.stdout.split():
            self._finish(
                "Nessuna sorgente remota",
                "Il repository è locale: non c'è nulla da aggiornare.",
            )
            return
        self._run_command(
            self.scm.update_upstream_argv(),
            self._on_git_upstream,
            "Cerco il repository remoto del branch…",
        )

    def _on_git_upstream(self, result: CommandResult) -> None:
        """Retain the explicit upstream or explain that none is configured."""

        if not self._prepare_result():
            return
        self.upstream = result.stdout.strip()
        if not result.ok or not self.upstream:
            self._finish(
                "Upstream non configurato",
                "Il branch Git corrente non indica ancora da quale branch remoto aggiornarsi.",
            )
            return
        environment = dict(self.scm.environment)
        environment["GIT_TERMINAL_PROMPT"] = "0"
        self._run_command(
            self.scm.fetch_argv(),
            self._on_git_fetch,
            "Scarico le modifiche Git…",
            cancellable=True,
            environment=environment,
        )

    def _on_git_fetch(self, result: CommandResult) -> None:
        """Compare local and upstream history after a successful Git fetch."""

        if not self._prepare_result():
            return
        if not result.ok:
            self._finish_error("Fetch Git fallito", result)
            return
        self._run_command(
            self.scm.update_comparison_argv(self.upstream),
            self._on_git_comparison,
            "Confronto la storia locale e remota…",
        )

    def _on_git_comparison(self, result: CommandResult) -> None:
        """Fast-forward only when Git reports an exclusively remote advance."""

        if not self._prepare_result():
            return
        if not result.ok:
            self._finish_error("Confronto Git fallito", result)
            return
        try:
            local_ahead, remote_ahead = self.scm.parse_update_comparison(
                result.stdout
            )
        except ValueError as error:
            self._finish("Confronto Git non valido", str(error))
            return
        if not local_ahead and not remote_ahead:
            self._finish("Repository già aggiornato", "Non ci sono nuovi commit.")
        elif local_ahead and not remote_ahead:
            self._finish(
                "Commit locali non pubblicati",
                "Il branch locale è già più avanti del suo upstream; la working copy non è stata modificata.",
            )
        elif local_ahead and remote_ahead:
            self._finish(
                "Storia divergente",
                "Locale e upstream contengono commit diversi: la working copy non è stata modificata. Serve un merge esplicito.",
            )
        else:
            self._run_command(
                self.scm.fast_forward_argv(self.upstream),
                self._on_git_fast_forward,
                "Aggiorno la working copy…",
            )

    def _on_git_fast_forward(self, result: CommandResult) -> None:
        """Report completion of the guarded Git fast-forward."""

        if not self._prepare_result():
            return
        if result.ok:
            self._finish("Repository aggiornato", "Il branch locale è ora allineato all'upstream.")
        else:
            self._finish_error("Fast-forward Git fallito", result)
