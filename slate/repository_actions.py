"""Simple dedicated dialogs for explicit repository actions."""

from __future__ import annotations

from typing import Callable

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # noqa: E402

from .processes import CommandResult
from .repository_dialog import RepositoryOperationDialog
from .scm.base import BranchTarget, SCM
from .scm.git import GitSCM
from .scm.hg import MercurialSCM
from .watcher import RepoWatcher


class _RepositoryActionDialog(RepositoryOperationDialog):
    """Add explicit form submission to the shared repository-operation shell."""

    # 2026-08-18: dialogs share process safety but keep separate workflows;
    # blockers end an action instead of feeding a generic recovery controller.

    def __init__(
        self,
        parent: Gtk.Window,
        title: str,
        action_label: str,
        scm: SCM,
        watcher: RepoWatcher,
        on_closed: Callable[[], None],
    ) -> None:
        """Build the common shell with one action button and idle cancellation."""

        super().__init__(
            parent,
            title,
            scm,
            watcher,
            on_closed,
            action_label=action_label,
            cancellation_title="Operazione annullata",
            allow_idle_close=True,
        )
        assert self.action_button is not None
        self.after_merge_check: Callable[[], None] | None = None
        self.after_clean_check: Callable[[], None] | None = None

    def _begin(self) -> None:
        """Start the action-specific setup after watcher ownership is acquired."""

        raise NotImplementedError

    def _check_no_merge(self, callback: Callable[[], None]) -> None:
        """Continue only when the repository has no merge already in progress."""

        self.after_merge_check = callback
        self._run_command(
            self.scm.update_merge_state_argv(),
            self._on_merge_checked,
            "Controllo lo stato del repository…",
        )

    def _on_merge_checked(self, result: CommandResult) -> None:
        """Interpret the SCM-specific absence of a pending merge."""

        if not self._prepare_result():
            return
        if isinstance(self.scm, MercurialSCM):
            query_failed = not result.ok
            merge_pending = result.ok and bool(result.stdout.strip())
        else:
            query_failed = result.error is not None or result.returncode not in (0, 1)
            merge_pending = result.ok
        if query_failed:
            self._finish_error("Verifica del repository fallita", result)
            return
        if merge_pending:
            self._finish(
                "Merge già in corso",
                "Concludi o annulla manualmente il merge prima di continuare.",
            )
            return
        callback = self.after_merge_check
        self.after_merge_check = None
        if callback is not None:
            callback()

    def _check_clean(self, callback: Callable[[], None]) -> None:
        """Continue only when tracked index and working-copy state are clean."""

        self.after_clean_check = callback
        self._run_command(
            self.scm.update_tracked_status_argv(),
            self._on_clean_checked,
            "Controllo le modifiche locali…",
        )

    def _on_clean_checked(self, result: CommandResult) -> None:
        """Reject tracked changes while allowing harmless untracked files."""

        if not self._prepare_result():
            return
        if not result.ok:
            self._finish_error("Lettura dello stato fallita", result)
            return
        try:
            dirty = bool(self.scm.parse_status(result.stdout))
        except (KeyError, IndexError, TypeError, ValueError) as error:
            self._finish("Stato repository non valido", str(error))
            return
        if dirty:
            self._finish(
                "Modifiche locali presenti",
                "Commit o ripristina le modifiche tracciate prima di continuare.",
            )
            return
        callback = self.after_clean_check
        self.after_clean_check = None
        if callback is not None:
            callback()

    def _submit(self) -> None:
        """Execute the action represented by the dialog's current form values."""

        raise NotImplementedError


class RepositoryPublishDialog(_RepositoryActionDialog):
    """Confirm and execute one normal push to an existing configured destination."""

    def __init__(
        self,
        parent: Gtk.Window,
        scm: SCM,
        watcher: RepoWatcher,
        on_closed: Callable[[], None],
    ) -> None:
        """Build the Publish confirmation modal."""

        super().__init__(
            parent,
            "Pubblica repository",
            "Pubblica",
            scm,
            watcher,
            on_closed,
        )

    def _begin(self) -> None:
        """Reject a pending merge before resolving the normal push destination."""

        self._check_no_merge(self._find_destination)

    def _find_destination(self) -> None:
        """Resolve only existing default/upstream configuration."""

        if isinstance(self.scm, MercurialSCM):
            self._run_command(
                self.scm.remote_path_argv("default-push"),
                self._on_hg_default_push,
                "Cerco la destinazione Mercurial…",
            )
        else:
            self._run_command(
                self.scm.remotes_argv(),
                self._on_git_remotes,
                "Cerco la destinazione Git…",
            )

    def _on_hg_default_push(self, result: CommandResult) -> None:
        """Fall back from default-push to Mercurial's normal default path."""

        if not self._prepare_result():
            return
        if result.ok and result.stdout.strip():
            self._enable_publish()
            return
        if result.error is not None or result.returncode not in (1,):
            self._finish_error("Verifica della destinazione fallita", result)
            return
        self._run_command(
            self.scm.remote_path_argv("default"),
            self._on_hg_default,
            "Cerco la destinazione Mercurial…",
        )

    def _on_hg_default(self, result: CommandResult) -> None:
        """Treat an absent default path as a valid local-only repository."""

        if not self._prepare_result():
            return
        if result.ok and result.stdout.strip():
            self._enable_publish()
        elif result.returncode == 1 and result.error is None:
            self._finish(
                "Nessuna destinazione remota",
                "Il repository è locale: non c'è nulla da pubblicare.",
            )
        else:
            self._finish_error("Verifica della destinazione fallita", result)

    def _on_git_remotes(self, result: CommandResult) -> None:
        """Distinguish a local-only Git repository before checking upstream."""

        if not self._prepare_result():
            return
        if not result.ok:
            self._finish_error("Lettura dei remote Git fallita", result)
            return
        if not result.stdout.split():
            self._finish(
                "Nessuna destinazione remota",
                "Il repository è locale: non c'è nulla da pubblicare.",
            )
            return
        self._run_command(
            self.scm.update_upstream_argv(),
            self._on_git_upstream,
            "Controllo l'upstream Git…",
        )

    def _on_git_upstream(self, result: CommandResult) -> None:
        """Require an existing upstream instead of configuring one implicitly."""

        if not self._prepare_result():
            return
        if result.ok and result.stdout.strip():
            self._enable_publish()
        elif result.returncode != 0 and result.error is None:
            self._finish(
                "Upstream non configurato",
                "Configura manualmente l'upstream del branch prima di pubblicare.",
            )
        else:
            self._finish_error("Verifica dell'upstream fallita", result)

    def _enable_publish(self) -> None:
        """Expose the single explicit confirmation after destination validation."""

        self.spinner.stop()
        self.ready_to_submit = True
        self.action_button.set_sensitive(True)
        self._set_progress(
            "Pronto per pubblicare",
            "Verranno inviati i commit e i tag previsti dal normale push.",
        )

    def _submit(self) -> None:
        """Start the normal push without making cancellation or force available."""

        self.ready_to_submit = False
        environment = dict(self.scm.environment)
        if isinstance(self.scm, GitSCM):
            environment["GIT_TERMINAL_PROMPT"] = "0"
        self._run_command(
            self.scm.push_argv(),
            self._on_pushed,
            "Pubblico il repository…",
            environment=environment,
        )

    def _on_pushed(self, result: CommandResult) -> None:
        """Report the unmodified push result without retries."""

        if not self._prepare_result():
            return
        if result.ok:
            self._finish("Repository pubblicato", "Il push è terminato correttamente.")
        else:
            self._finish_error("Pubblicazione fallita", result)


class _NamedRepositoryDialog(_RepositoryActionDialog):
    """Share one validated text field between branch creation and tagging."""

    def __init__(
        self,
        parent: Gtk.Window,
        title: str,
        action_label: str,
        scm: SCM,
        watcher: RepoWatcher,
        on_closed: Callable[[], None],
    ) -> None:
        """Add the single name input used by simple named operations."""

        super().__init__(parent, title, action_label, scm, watcher, on_closed)
        self.name_entry = Gtk.Entry()
        self.name_entry.set_activates_default(True)
        self.name_entry.connect("changed", self._on_name_changed)
        self.content.pack_start(self.name_entry, False, False, 0)
        self.form_widgets.append(self.name_entry)
        self.set_default_response(Gtk.ResponseType.OK)

    def _on_name_changed(self, _entry: Gtk.Entry) -> None:
        """Enable submission only for a non-empty trimmed name after preflight."""

        self.action_button.set_sensitive(
            self.ready_to_submit and bool(self.name_entry.get_text().strip())
        )

    def _enable_name(self, detail: str) -> None:
        """Expose the name field after repository preflight succeeds."""

        self.spinner.stop()
        self.ready_to_submit = True
        self.name_entry.set_sensitive(True)
        self._on_name_changed(self.name_entry)
        self._set_progress("Inserisci un nome", detail)
        self.name_entry.grab_focus()


class RepositoryCreateBranchDialog(_NamedRepositoryDialog):
    """Create and enter one local HG or Git branch."""

    def __init__(
        self,
        parent: Gtk.Window,
        scm: SCM,
        watcher: RepoWatcher,
        on_closed: Callable[[], None],
    ) -> None:
        """Build the New branch modal."""

        super().__init__(parent, "Nuovo branch", "Crea", scm, watcher, on_closed)
        self.name_entry.set_placeholder_text("Nome del branch")

    def _begin(self) -> None:
        """Allow branch creation only outside an existing merge."""

        self.name_entry.set_sensitive(False)
        self._check_no_merge(self._ready)

    def _ready(self) -> None:
        """Enable the sole branch-name input."""

        self._enable_name("Le modifiche locali resteranno nella working copy.")

    def _submit(self) -> None:
        """Create the exact trimmed branch name without tracking inference."""

        self.ready_to_submit = False
        name = self.name_entry.get_text().strip()
        argv = self.scm.create_branch_argv(name)
        self._run_command(argv, self._on_created, "Creo il branch…")

    def _on_created(self, result: CommandResult) -> None:
        """Report branch creation without force or alternate names."""

        if not self._prepare_result():
            return
        if result.ok:
            self._finish("Branch creato", "La working copy usa ora il nuovo branch.")
        else:
            self._finish_error("Creazione del branch fallita", result)


class RepositoryTagDialog(_NamedRepositoryDialog):
    """Create one normal HG or annotated Git tag on the current revision."""

    def __init__(
        self,
        parent: Gtk.Window,
        scm: SCM,
        watcher: RepoWatcher,
        on_closed: Callable[[], None],
    ) -> None:
        """Build the Assign tag modal."""

        super().__init__(parent, "Assegna tag", "Assegna", scm, watcher, on_closed)
        self.recent_tags_label = Gtk.Label(xalign=0)
        self.recent_tags_label.set_line_wrap(True)
        self.recent_tags_label.set_selectable(True)
        self.content.pack_start(self.recent_tags_label, False, False, 0)
        self.name_entry.set_placeholder_text("Nome del tag")

    def _begin(self) -> None:
        """Require no merge and a clean visible revision before tagging."""

        self.name_entry.set_sensitive(False)
        self._check_no_merge(self._check_tag_clean)

    def _check_tag_clean(self) -> None:
        """Continue tag setup after the shared tracked-status preflight."""

        self._check_clean(self._ready)

    def _ready(self) -> None:
        """Load recent local tags before enabling the tag-name field."""

        # 2026-08-18: lo storico viene letto dall'SCM, senza aggiungere stato
        # persistente a SLATE o file nelle working copy.
        self._run_command(
            self.scm.recent_tags_argv(),
            self._on_recent_tags,
            "Carico gli ultimi tag…",
        )

    def _on_recent_tags(self, result: CommandResult) -> None:
        """Show up to three recent tags without blocking tag creation on failure."""

        if not self._prepare_result():
            return
        recent_tags: list[str] = []
        history_available = result.ok
        if result.ok:
            try:
                recent_tags = self.scm.parse_recent_tags(result.stdout)
            except (KeyError, TypeError, ValueError):
                history_available = False
        if recent_tags:
            self.recent_tags_label.set_text(f"Ultimi tag: {'  ·  '.join(recent_tags)}")
        elif history_available:
            self.recent_tags_label.set_text("Ultimi tag: nessuno")
        else:
            self.recent_tags_label.set_text("Ultimi tag: non disponibili")
        self._enable_name("Il tag verrà assegnato alla revisione corrente.")

    def _submit(self) -> None:
        """Create one tag without replacing an existing name."""

        self.ready_to_submit = False
        name = self.name_entry.get_text().strip()
        self._run_command(self.scm.tag_argv(name), self._on_tagged, "Creo il tag…")

    def _on_tagged(self, result: CommandResult) -> None:
        """Report the normal non-force tag result."""

        if not self._prepare_result():
            return
        if result.ok:
            self._finish("Tag assegnato", "Il nuovo tag è stato creato localmente.")
        else:
            self._finish_error("Creazione del tag fallita", result)


class _BranchChoiceDialog(_RepositoryActionDialog):
    """Share local branch loading and selection between Switch and Merge."""

    def __init__(
        self,
        parent: Gtk.Window,
        title: str,
        action_label: str,
        scm: SCM,
        watcher: RepoWatcher,
        on_closed: Callable[[], None],
    ) -> None:
        """Add a disabled local-branch selector to the modal shell."""

        super().__init__(parent, title, action_label, scm, watcher, on_closed)
        self.current_branch = ""
        self.targets: dict[str, BranchTarget] = {}
        self.branch_combo = Gtk.ComboBoxText()
        self.branch_combo.set_sensitive(False)
        self.branch_combo.connect("changed", self._on_branch_changed)
        self.content.pack_start(self.branch_combo, False, False, 0)
        self.form_widgets.append(self.branch_combo)

    def _load_current_branch(self) -> None:
        """Read the active branch before filtering local choices."""

        self._run_command(
            self.scm.branch_argv(),
            self._on_current_branch,
            "Leggo il branch corrente…",
        )

    def _on_current_branch(self, result: CommandResult) -> None:
        """Reject detached/empty branch state and request local branches."""

        if not self._prepare_result():
            return
        self.current_branch = result.stdout.strip()
        if not result.ok or not self.current_branch:
            self._finish("Branch corrente non disponibile", "L'operazione richiede un branch locale attivo.")
            return
        self._run_command(
            self.scm.branches_argv(),
            self._on_branches,
            "Carico i branch locali…",
        )

    def _on_branches(self, result: CommandResult) -> None:
        """Populate choices with open local branches other than the current one."""

        if not self._prepare_result():
            return
        if not result.ok:
            self._finish_error("Lettura dei branch fallita", result)
            return
        try:
            branches = self.scm.parse_branches(result.stdout)
        except (KeyError, TypeError, ValueError) as error:
            self._finish("Elenco branch non valido", str(error))
            return
        choices = [target for target in branches if target.name != self.current_branch]
        if not choices:
            self._finish("Nessun branch disponibile", "Non esistono altri branch locali selezionabili.")
            return
        self.targets = {target.name: target for target in choices}
        for target in choices:
            self.branch_combo.append(target.name, target.name)
        self.branch_combo.set_active(0)
        self.branch_combo.set_sensitive(True)
        self.ready_to_submit = True
        self.spinner.stop()
        self._set_progress("Scegli un branch")
        self._on_branch_changed(self.branch_combo)

    def _on_branch_changed(self, _combo: Gtk.ComboBoxText) -> None:
        """Enable the primary action only for a loaded explicit branch target."""

        self.action_button.set_sensitive(
            self.ready_to_submit and self.branch_combo.get_active_id() in self.targets
        )


class RepositorySwitchBranchDialog(_BranchChoiceDialog):
    """Switch a clean working copy to one unambiguous local branch."""

    def __init__(
        self,
        parent: Gtk.Window,
        scm: SCM,
        watcher: RepoWatcher,
        on_closed: Callable[[], None],
    ) -> None:
        """Build the Switch branch modal."""

        super().__init__(parent, "Passa a branch", "Passa", scm, watcher, on_closed)

    def _begin(self) -> None:
        """Require no merge and clean tracked state before loading branches."""

        self._check_no_merge(self._check_switch_clean)

    def _check_switch_clean(self) -> None:
        """Continue switch setup after the shared clean-state check."""

        self._check_clean(self._load_current_branch)

    def _submit(self) -> None:
        """Resolve an HG head or switch directly to the selected Git branch."""

        self.ready_to_submit = False
        name = self.branch_combo.get_active_id() or ""
        if isinstance(self.scm, MercurialSCM):
            self._run_command(
                self.scm.branch_heads_argv(name),
                self._on_hg_heads,
                "Controllo la head Mercurial…",
            )
        else:
            self._run_command(
                self.scm.switch_branch_argv(name),
                self._on_switched,
                "Cambio branch…",
            )

    def _on_hg_heads(self, result: CommandResult) -> None:
        """Switch HG only when the selected named branch has exactly one head."""

        if not self._prepare_result():
            return
        if not result.ok:
            self._finish_error("Lettura della head Mercurial fallita", result)
            return
        try:
            heads = self.scm.parse_update_heads(result.stdout)
        except (KeyError, TypeError, ValueError) as error:
            self._finish("Elenco head Mercurial non valido", str(error))
            return
        if len(heads) != 1:
            self._finish("Branch ambiguo", "Il branch Mercurial ha più head e va gestito manualmente.")
            return
        self._run_command(self.scm.update_to_argv(heads[0]), self._on_switched, "Cambio branch…")

    def _on_switched(self, result: CommandResult) -> None:
        """Report the exact checked branch switch result."""

        if not self._prepare_result():
            return
        if result.ok:
            self._finish("Branch cambiato", "La working copy usa ora il branch selezionato.")
        else:
            self._finish_error("Cambio branch fallito", result)


class RepositoryMergeBranchDialog(_BranchChoiceDialog):
    """Start one local-branch merge and offer only Meld for conflicts."""

    def __init__(
        self,
        parent: Gtk.Window,
        scm: SCM,
        watcher: RepoWatcher,
        on_closed: Callable[[], None],
    ) -> None:
        """Build the Merge branch modal and its initially hidden Meld action."""

        super().__init__(parent, "Merge branch", "Merge", scm, watcher, on_closed)
        self.merge_result: CommandResult | None = None
        self.meld_button = self.add_button("Apri in Meld", Gtk.ResponseType.APPLY)
        self.meld_button.set_no_show_all(True)

    def _begin(self) -> None:
        """Require no merge and clean tracked state before loading sources."""

        self._check_no_merge(self._check_merge_clean)

    def _check_merge_clean(self) -> None:
        """Continue merge setup after the shared clean-state check."""

        self._check_clean(self._load_current_branch)

    def _submit(self) -> None:
        """Start a merge from one exact local branch source."""

        self.ready_to_submit = False
        name = self.branch_combo.get_active_id() or ""
        if isinstance(self.scm, MercurialSCM):
            self._run_command(
                self.scm.branch_heads_argv(name),
                self._on_hg_heads,
                "Controllo la head Mercurial…",
            )
        else:
            self._run_command(
                self.scm.merge_branch_argv(name),
                self._on_merge_completed,
                "Eseguo il merge…",
            )

    def _on_hg_heads(self, result: CommandResult) -> None:
        """Merge HG only from a named branch with one unambiguous head."""

        if not self._prepare_result():
            return
        if not result.ok:
            self._finish_error("Lettura della head Mercurial fallita", result)
            return
        try:
            heads = self.scm.parse_update_heads(result.stdout)
        except (KeyError, TypeError, ValueError) as error:
            self._finish("Elenco head Mercurial non valido", str(error))
            return
        if len(heads) != 1:
            self._finish("Branch ambiguo", "Il branch Mercurial ha più head e va gestito manualmente.")
            return
        self._run_command(
            self.scm.merge_branch_argv(heads[0]),
            self._on_merge_completed,
            "Eseguo il merge…",
        )

    def _on_merge_completed(self, result: CommandResult) -> None:
        """Inspect merge state on success or explicit unresolved paths on failure."""

        if not self._prepare_result():
            return
        self.merge_result = result
        if result.ok:
            self._run_command(
                self.scm.update_merge_state_argv(),
                self._on_post_merge_state,
                "Verifico il risultato del merge…",
            )
        else:
            self._run_command(
                self.scm.merge_conflicts_argv(),
                self._on_merge_conflicts,
                "Controllo i conflitti…",
            )

    def _on_post_merge_state(self, result: CommandResult) -> None:
        """Distinguish an active clean merge from an already integrated source."""

        if not self._prepare_result():
            return
        if isinstance(self.scm, MercurialSCM):
            failed = not result.ok
            pending = result.ok and bool(result.stdout.strip())
        else:
            failed = result.error is not None or result.returncode not in (0, 1)
            pending = result.ok
        if failed:
            self._finish_error("Verifica del merge fallita", result)
        elif pending:
            self._finish(
                "Merge pronto",
                "Completa il commit o annulla il merge manualmente.",
            )
        else:
            self._finish("Nessuna modifica", "Il branch selezionato è già integrato.")

    def _on_merge_conflicts(self, result: CommandResult) -> None:
        """Offer Meld only when a machine-readable conflict list is non-empty."""

        if not self._prepare_result():
            return
        if not result.ok:
            self._finish_error("Verifica dei conflitti fallita", result)
            return
        try:
            conflicts = self.scm.parse_merge_conflicts(result.stdout)
        except (KeyError, TypeError, ValueError) as error:
            self._finish("Elenco conflitti non valido", str(error))
            return
        if not conflicts:
            if self.merge_result is not None:
                self._finish_error("Merge fallito", self.merge_result)
            return
        self.finished = True
        self.spinner.stop()
        self._set_progress(
            "Conflitti nel merge",
            f"{len(conflicts)} file da risolvere. Puoi aprirli in Meld oppure continuare manualmente.",
        )
        self.cancel_button.hide()
        self.action_button.hide()
        for widget in self.form_widgets:
            widget.set_sensitive(False)
        self.meld_button.show()
        self.close_button.show()

    def _on_response(self, dialog: Gtk.Dialog, response: int) -> None:
        """Launch Meld for conflicts or delegate normal dialog responses."""

        if response == Gtk.ResponseType.APPLY and self.command is None:
            self.finished = False
            self.meld_button.hide()
            self.close_button.hide()
            self.cancel_button.show()
            self._run_command(
                self.scm.merge_tool_argv(),
                self._on_meld_completed,
                "Attendo Meld…",
            )
            return
        super()._on_response(dialog, response)

    def _on_meld_completed(self, result: CommandResult) -> None:
        """Report Meld completion while leaving merge conclusion external."""

        if not self._prepare_result():
            return
        if result.ok:
            self._finish(
                "Meld terminato",
                "Completa il commit o annulla il merge manualmente.",
            )
        else:
            self._finish_error("Meld non ha completato la risoluzione", result)
