"""Vte terminals backed by sessions on the dedicated tmux server."""

from __future__ import annotations

import base64
import binascii
import os
import re
from dataclasses import dataclass, replace
from typing import Callable, Iterable, Sequence

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Vte", "2.91")
from gi.repository import Gdk, Gio, GLib, Gtk, Vte  # noqa: E402

from .processes import AsyncCommand, CommandResult, run_async


SHELL_COMMANDS = {"bash", "zsh", "fish", "sh", "dash"}
_TERMINAL_URL_PATTERN = r"(?i)https?://[^\s<>\[\]{}\"']+"
# 2026-08-17: VTE richiede esplicitamente il bit PCRE2_MULTILINE per regex
# usate sul buffer a più righe; il binding GI non esporta la costante PCRE2.
_PCRE2_MULTILINE = 0x00000400


def slug(value: str, maximum: int) -> str:
    """Convert a display name into a bounded tmux session-name component."""

    normalized = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return normalized[:maximum].rstrip("-")


def session_name(project_name: str, terminal_name: str) -> str:
    """Build the unambiguous dedicated-server tmux session name."""

    return f"{slug(project_name, 30)}--{slug(terminal_name, 20)}"


def terminal_key(project_name: str, terminal_name: str) -> str:
    """Build the Gtk.Stack name used for a configured terminal."""

    return f"{project_name}/{terminal_name}"


@dataclass(frozen=True)
class PaneInfo:
    """Describe foreground activity in a tmux pane."""

    session: str
    command: str
    pid: int
    tty: str = ""
    duration: int | None = None

    @property
    def active(self) -> bool:
        """Return whether the pane is running something other than a shell."""

        return self.command not in SHELL_COMMANDS


@dataclass(frozen=True)
class OrphanSession:
    """Describe recovery metadata stored on one tmux session."""

    session: str
    project_name: str
    project_path: str
    terminal_name: str
    session_path: str


class TerminalManager:
    """Create, switch and inspect all Vte/tmux terminal sessions."""

    ACTIVITY_DEBOUNCE_MS = 100
    ACTIVITY_INTERVAL_MS = 5000

    def __init__(
        self,
        stack: Gtk.Stack,
        on_error: Callable[[str], None],
        on_activity: Callable[[dict[str, bool]], None],
        on_exit: Callable[[str, str, int], None],
        on_attention: Callable[[Vte.Terminal, bool], None],
        on_bell: Callable[[str, str], None],
        on_project_search: Callable[[], None],
        project_search_available: bool,
        status_bar_enabled: bool,
    ) -> None:
        """Bind terminal lifecycle to a stack without starting background work."""

        # 2026-08-16: calcolare il comando alla creazione consente a
        # --agent-debug di scegliere il proprio socket prima di costruire la UI.
        self.tmux = (
            "tmux",
            "-L",
            os.environ.get("SLATE_TMUX_SOCKET", "slate"),
        )
        self.stack = stack
        self.on_error = on_error
        self.on_activity = on_activity
        self.on_exit = on_exit
        self.on_attention = on_attention
        self.on_bell = on_bell
        self.on_project_search = on_project_search
        self.project_search_available = project_search_available
        self.status_bar_enabled = status_bar_enabled
        self.terminals: dict[str, Vte.Terminal] = {}
        self.sessions: dict[str, str] = {}
        self.initial_commands: dict[str, str] = {}
        self.session_checks: dict[str, AsyncCommand] = {}
        self.spawn_cancellables: dict[str, Gio.Cancellable] = {}
        self.closing_keys: set[str] = set()
        self.shutting_down = False
        self.activity_monitoring = False
        self.activity_dirty = False
        self.activity_debounce_id: int | None = None
        self.activity_interval_id: int | None = None
        self.activity_command: AsyncCommand | None = None
        self.server_configured = False
        self.server_configuring = False
        self.server_configuration_dirty = False
        self.url_regex: Vte.Regex | None = None

    def add(
        self,
        project: dict,
        terminal_name_value: str,
        *,
        initial_command: str | None = None,
        working_directory: str | None = None,
    ) -> Vte.Terminal:
        """Create a Vte child and asynchronously attach/create its tmux session."""

        key = terminal_key(project["name"], terminal_name_value)
        if key in self.terminals:
            return self.terminals[key]
        terminal = Vte.Terminal()
        terminal.set_scrollback_lines(10000)
        terminal.set_mouse_autohide(True)
        # 2026-08-17: VTE espone automaticamente gli hyperlink OSC 8, mentre
        # gli URL stampati come testo richiedono un match esplicito per offrire
        # lo stesso clic diretto senza analizzare l'output a fini applicativi.
        if self.url_regex is None:
            self.url_regex = Vte.Regex.new_for_match(
                _TERMINAL_URL_PATTERN,
                -1,
                _PCRE2_MULTILINE,
            )
        url_match_tag = terminal.match_add_regex(self.url_regex, 0)
        terminal.match_set_cursor_name(url_match_tag, "pointer")
        # 2026-08-16: Vte non installa automaticamente le scorciatoie clipboard
        # di un emulatore completo; SLATE deve collegarle su ogni terminale.
        terminal.connect("key-press-event", self._on_terminal_key_press)
        terminal.connect("button-press-event", self._on_terminal_button_press)
        terminal.connect("popup-menu", self._on_terminal_popup_menu)
        terminal.connect("child-exited", self._on_terminal_child_exited)
        # 2026-08-16: il BEL e il successivo focus sono segnali terminale
        # affidabili anche attraverso tmux e non richiedono di analizzare il
        # contenuto prodotto da Codex o da altri programmi.
        terminal.connect(
            "bell",
            self._on_terminal_bell,
            project["name"],
            terminal_name_value,
        )
        terminal.connect("focus-in-event", self._on_terminal_focus_in)
        self.stack.add_named(terminal, key)
        self.terminals[key] = terminal
        tmux_session = session_name(project["name"], terminal_name_value)
        self.sessions[key] = tmux_session
        spawn_directory = working_directory or project["path"]
        if initial_command:
            # 2026-08-18: una verifica esplicita sul socket dedicato impedisce
            # di reinviare il launcher dentro una sessione tmux ancora viva.
            def session_checked(result: CommandResult) -> None:
                """Continue terminal creation after checking persisted tmux state."""

                self._on_session_checked(
                    result,
                    terminal,
                    key,
                    project["name"],
                    project["path"],
                    terminal_name_value,
                    spawn_directory,
                    initial_command,
                )

            self.session_checks[key] = run_async(
                [*self.tmux, "has-session", "-t", tmux_session],
                session_checked,
            )
        else:
            self._spawn_terminal(
                terminal,
                key,
                project["name"],
                project["path"],
                terminal_name_value,
                spawn_directory,
                None,
            )
        terminal.show()
        self.request_activity_refresh()
        return terminal

    def _on_session_checked(
        self,
        result: CommandResult,
        terminal: Vte.Terminal,
        key: str,
        project_name: str,
        project_path: str,
        terminal_name_value: str,
        spawn_directory: str,
        initial_command: str,
    ) -> None:
        """Run a persisted launcher only when its tmux session is absent."""

        self.session_checks.pop(key, None)
        if (
            self.shutting_down
            or key in self.closing_keys
            or self.terminals.get(key) is not terminal
        ):
            return
        missing = self._server_absent(result) or "can't find session" in result.stderr.lower()
        if not result.ok and not missing:
            # Un errore non riconosciuto non autorizza l'invio automatico di un
            # comando dentro una sessione il cui stato non è stato accertato.
            self.on_error(self._error("Checking tmux session", result))
        self._spawn_terminal(
            terminal,
            key,
            project_name,
            project_path,
            terminal_name_value,
            spawn_directory,
            initial_command if missing else None,
        )

    def _spawn_terminal(
        self,
        terminal: Vte.Terminal,
        key: str,
        project_name: str,
        project_path: str,
        terminal_name_value: str,
        spawn_directory: str,
        initial_command: str | None,
    ) -> None:
        """Attach VTE to tmux and retain a launcher for a newly created session."""

        if initial_command:
            self.initial_commands[key] = initial_command
        tmux_session = session_name(project_name, terminal_name_value)
        argv = [
            *self.tmux,
            "new-session",
            "-A",
            "-D",
            "-s",
            tmux_session,
            "-c",
            spawn_directory,
        ]
        cancellable = Gio.Cancellable()
        self.spawn_cancellables[key] = cancellable
        terminal.spawn_async(
            Vte.PtyFlags.DEFAULT,
            spawn_directory,
            argv,
            GLib.get_environ(),
            GLib.SpawnFlags.DEFAULT,
            None,
            None,
            -1,
            cancellable,
            self._on_spawned,
            (key, project_name, project_path, terminal_name_value),
        )

    def _on_terminal_bell(
        self,
        terminal: Vte.Terminal,
        project_name: str,
        terminal_name_value: str,
    ) -> None:
        """Publish BELL identity and mark an unfocused terminal for attention."""

        self.on_bell(project_name, terminal_name_value)
        self.on_attention(terminal, not terminal.has_focus())

    def _on_terminal_focus_in(
        self, terminal: Vte.Terminal, _event: Gdk.EventFocus
    ) -> bool:
        """Clear terminal attention when the user returns keyboard focus to it."""

        self.on_attention(terminal, False)
        return False

    @staticmethod
    def _on_terminal_key_press(terminal: Vte.Terminal, event: Gdk.EventKey) -> bool:
        """Implement standard terminal clipboard keys without stealing Ctrl+C."""

        control = bool(event.state & Gdk.ModifierType.CONTROL_MASK)
        shift = bool(event.state & Gdk.ModifierType.SHIFT_MASK)
        keyval = Gdk.keyval_to_lower(event.keyval)
        if control and shift and keyval == Gdk.KEY_v:
            terminal.paste_clipboard()
            return True
        if shift and event.keyval in (Gdk.KEY_Insert, Gdk.KEY_KP_Insert):
            terminal.paste_clipboard()
            return True
        if control and shift and keyval == Gdk.KEY_c:
            terminal.copy_clipboard_format(Vte.Format.TEXT)
            return True
        if control and event.keyval in (Gdk.KEY_Insert, Gdk.KEY_KP_Insert):
            terminal.copy_clipboard_format(Vte.Format.TEXT)
            return True
        return False

    def _on_terminal_button_press(
        self, terminal: Vte.Terminal, event: Gdk.EventButton
    ) -> bool:
        """Open an explicit terminal URL or the clipboard menu."""

        if event.button == 1:
            uri = terminal.hyperlink_check_event(event)
            if not uri:
                matched_uri, _tag = terminal.match_check_event(event)
                uri = matched_uri
            if uri:
                # 2026-08-17: la punteggiatura finale appartiene spesso alla
                # frase che contiene il link, non all'indirizzo da aprire.
                uri = uri.rstrip(".,;:!?)]}")
                if GLib.uri_parse_scheme(uri) in {"http", "https"}:
                    Gio.AppInfo.launch_default_for_uri_async(
                        uri,
                        None,
                        None,
                        self._on_terminal_uri_opened,
                        uri,
                    )
                    return True

        if event.button != 3:
            return False
        self._show_terminal_menu(terminal, event)
        return True

    def _on_terminal_uri_opened(
        self, _source: object, result: Gio.AsyncResult, uri: str
    ) -> None:
        """Report failure while asynchronously opening a terminal URL."""

        try:
            Gio.AppInfo.launch_default_for_uri_finish(result)
        except GLib.Error as error:
            self.on_error(f"Unable to open {uri}: {error}")

    def _on_terminal_popup_menu(self, terminal: Vte.Terminal) -> bool:
        """Open terminal clipboard actions from Menu or Shift+F10."""

        self._show_terminal_menu(terminal, None)
        return True

    def _show_terminal_menu(
        self, terminal: Vte.Terminal, event: Gdk.EventButton | None
    ) -> None:
        """Build terminal clipboard and project-search contextual actions."""

        menu = Gtk.Menu()
        copy_item = self._terminal_menu_item(
            "Copy",
            "edit-copy",
            Gdk.KEY_c,
            Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK,
            "Copy (Ctrl+Shift+C / Ctrl+Insert)",
        )
        paste_item = self._terminal_menu_item(
            "Paste",
            "edit-paste",
            Gdk.KEY_v,
            Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK,
            "Paste (Ctrl+Shift+V / Shift+Insert)",
        )
        search_item = self._terminal_menu_item(
            "Project Search",
            "edit-find",
            Gdk.KEY_f,
            Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK,
            "Project Search (Ctrl+Shift+F)",
        )
        copy_item.set_sensitive(terminal.get_has_selection())
        search_item.set_sensitive(self.project_search_available)
        copy_item.connect("activate", self._copy_from_terminal_menu, terminal)
        paste_item.connect("activate", self._paste_from_terminal_menu, terminal)
        search_item.connect("activate", self._on_project_search_from_terminal_menu)
        menu.append(copy_item)
        menu.append(paste_item)
        # 2026-08-20: la ricerca del progetto è distinta dalle operazioni sugli
        # appunti, ma riusa esattamente la stessa azione globale della HeaderBar.
        menu.append(Gtk.SeparatorMenuItem())
        menu.append(search_item)
        menu.show_all()
        if event is not None:
            menu.popup_at_pointer(event)
        else:
            menu.popup_at_widget(
                terminal,
                Gdk.Gravity.CENTER,
                Gdk.Gravity.CENTER,
                None,
            )

    @staticmethod
    def _terminal_menu_item(
        label: str,
        icon_name: str,
        keyval: int,
        modifiers: Gdk.ModifierType,
        tooltip: str,
    ) -> Gtk.MenuItem:
        """Create a terminal context item with icon and shortcut hint."""

        item = Gtk.MenuItem()
        content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        icon = Gtk.Image.new_from_icon_name(icon_name, Gtk.IconSize.MENU)
        accel_label = Gtk.AccelLabel(label=label)
        accel_label.set_xalign(0)
        accel_label.set_accel_widget(item)
        accel_label.set_accel(keyval, modifiers)
        content.pack_start(icon, False, False, 0)
        content.pack_start(accel_label, True, True, 0)
        item.add(content)
        item.set_tooltip_text(tooltip)
        return item

    @staticmethod
    def _copy_from_terminal_menu(
        _item: Gtk.MenuItem, terminal: Vte.Terminal
    ) -> None:
        """Copy the current Vte selection as plain text."""

        terminal.copy_clipboard_format(Vte.Format.TEXT)

    @staticmethod
    def _paste_from_terminal_menu(
        _item: Gtk.MenuItem, terminal: Vte.Terminal
    ) -> None:
        """Paste clipboard text into the selected Vte terminal."""

        terminal.paste_clipboard()

    def _on_project_search_from_terminal_menu(self, _item: Gtk.MenuItem) -> None:
        """Open the shared project search from one terminal context menu."""

        self.on_project_search()

    def show(self, project_name: str, terminal_name_value: str) -> bool:
        """Show and focus one existing terminal stack child."""

        key = terminal_key(project_name, terminal_name_value)
        terminal = self.terminals.get(key)
        if terminal is None:
            return False
        self.stack.set_visible_child_name(key)
        # La selezione esplicita equivale al ritorno dell'utente sul terminale
        # e rimuove l'avviso senza dipendere dalla tempistica del focus GTK.
        self.on_attention(terminal, False)
        # 2026-08-16: il click che cambia la selezione dell'albero termina dopo
        # questo callback; rimandare il focus evita che GTK lo restituisca alla
        # colonna dei progetti al termine dello stesso evento mouse.
        GLib.idle_add(terminal.grab_focus)
        return True

    def rename(
        self,
        project_name: str,
        old_name: str,
        new_name: str,
        callback: Callable[[bool], None],
    ) -> None:
        """Rename an attached or still-lazy tmux session before updating config."""

        old_key = terminal_key(project_name, old_name)
        new_key = terminal_key(project_name, new_name)
        old_session = session_name(project_name, old_name)
        new_session = session_name(project_name, new_name)

        def renamed(result: CommandResult) -> None:
            """Commit local terminal bookkeeping only after tmux accepts rename."""

            terminal = self.terminals.get(old_key)
            missing = self._server_absent(result) or "can't find session" in result.stderr.lower()
            if not result.ok and not (missing and terminal is None):
                self.on_error(self._error("Rename terminal", result))
                callback(False)
                return
            if terminal is not None:
                self.terminals.pop(old_key, None)
                self.sessions.pop(old_key, None)
                self.stack.remove(terminal)
                self.stack.add_named(terminal, new_key)
                self.terminals[new_key] = terminal
                self.sessions[new_key] = new_session
                terminal.show()
            if result.ok:
                self._set_metadata(new_session, project_name, "", new_name)
            self.request_activity_refresh()
            callback(True)

        run_async(
            [*self.tmux, "rename-session", "-t", old_session, new_session], renamed
        )

    def close(
        self,
        project_name: str,
        terminal_name_value: str,
        callback: Callable[[bool], None],
    ) -> None:
        """Kill an explicitly chosen attached or still-lazy tmux session."""

        key = terminal_key(project_name, terminal_name_value)
        tmux_session = session_name(project_name, terminal_name_value)
        self.closing_keys.add(key)
        session_check = self.session_checks.pop(key, None)
        if session_check is not None:
            session_check.cancel()

        def killed(result: CommandResult) -> None:
            """Remove local widgets after tmux confirms or no longer has a server."""

            missing_session = "can't find session" in result.stderr.lower()
            if (
                not result.ok
                and not missing_session
                and "no server running" not in result.stderr.lower()
            ):
                self.closing_keys.discard(key)
                self.on_error(self._error("Closing terminal", result))
                callback(False)
                return
            terminal = self.terminals.pop(key, None)
            self.sessions.pop(key, None)
            self.initial_commands.pop(key, None)
            self.closing_keys.discard(key)
            if terminal:
                self.on_attention(terminal, False)
                self.stack.remove(terminal)
            self.request_activity_refresh()
            callback(True)

        run_async([*self.tmux, "kill-session", "-t", tmux_session], killed)

    def forget(self, project_name: str, terminal_name_value: str) -> None:
        """Detach and remove a Vte client while leaving its tmux session alive."""

        key = terminal_key(project_name, terminal_name_value)
        session_check = self.session_checks.pop(key, None)
        if session_check is not None:
            session_check.cancel()
        terminal = self.terminals.pop(key, None)
        self.sessions.pop(key, None)
        self.initial_commands.pop(key, None)
        self.closing_keys.discard(key)
        if terminal:
            self.on_attention(terminal, False)
            self.stack.remove(terminal)
        self.request_activity_refresh()

    def query_panes(
        self,
        callback: Callable[[list[PaneInfo]], None],
        *,
        include_duration: bool = True,
    ) -> AsyncCommand:
        """Read pane state and optionally enrich foreground-process durations."""

        format_value = (
            "#{session_name}|#{pane_current_command}|#{pane_pid}|#{pane_tty}"
        )

        def completed(result: CommandResult) -> None:
            """Parse the dedicated server pane listing, treating no server as empty."""

            if not result.ok:
                if self._server_absent(result):
                    self.server_configured = False
                    self.server_configuring = False
                    self.server_configuration_dirty = False
                    callback([])
                else:
                    self.on_error(self._error("Reading tmux sessions", result))
                    callback([])
                return
            panes: list[PaneInfo] = []
            for line in result.stdout.splitlines():
                fields = line.split("|", 3)
                if len(fields) == 4:
                    try:
                        panes.append(PaneInfo(fields[0], fields[1], int(fields[2]), fields[3]))
                    except ValueError:
                        continue
            if include_duration:
                self._enrich_durations(panes, callback)
            else:
                callback(panes)

        return run_async(
            [*self.tmux, "list-panes", "-a", "-F", format_value], completed
        )

    def list_orphans(
        self,
        configured_sessions: Iterable[str],
        callback: Callable[[list[OrphanSession]], None],
    ) -> None:
        """Return dedicated-server sessions absent from current configuration."""

        configured = set(configured_sessions)
        format_value = (
            "#{session_name}|#{@slate_project_name}|#{@slate_project_path}|"
            "#{@slate_terminal_name}|#{session_path}"
        )

        def completed(result: CommandResult) -> None:
            """Parse tmux user options used for reliable explicit re-adoption."""

            if not result.ok:
                callback([])
                return
            orphans: list[OrphanSession] = []
            for line in result.stdout.splitlines():
                fields = line.split("|", 4)
                if len(fields) == 5 and fields[0] not in configured:
                    orphans.append(
                        OrphanSession(
                            fields[0],
                            self._decode_metadata(fields[1]),
                            self._decode_metadata(fields[2]),
                            self._decode_metadata(fields[3]),
                            fields[4],
                        )
                    )
            callback(orphans)

        run_async([*self.tmux, "list-sessions", "-F", format_value], completed)

    def kill_server(self, callback: Callable[[CommandResult], None]) -> None:
        """Terminate only the dedicated SLATE tmux server."""

        run_async([*self.tmux, "kill-server"], callback)

    def kill_session(self, name: str, callback: Callable[[CommandResult], None]) -> None:
        """Terminate one named session on the dedicated server."""

        run_async([*self.tmux, "kill-session", "-t", name], callback)

    def shutdown(self) -> None:
        """Stop manager timers without terminating background sessions."""

        self.begin_shutdown()
        self.set_activity_monitoring(False)
        if self.activity_command is not None:
            self.activity_command.cancel()
            self.activity_command = None
        for cancellable in self.spawn_cancellables.values():
            cancellable.cancel()
        self.spawn_cancellables.clear()
        for command in self.session_checks.values():
            command.cancel()
        self.session_checks.clear()
        self.initial_commands.clear()
        self.closing_keys.clear()

    def begin_shutdown(self) -> None:
        """Suppress child-exit config updates during deliberate application exit."""

        self.shutting_down = True

    def _on_terminal_child_exited(
        self, terminal: Vte.Terminal, status: int
    ) -> None:
        """Remove an unexpectedly ended terminal and notify configuration owner."""

        if self.shutting_down:
            return
        key = next(
            (
                candidate
                for candidate, candidate_terminal in self.terminals.items()
                if candidate_terminal is terminal
            ),
            None,
        )
        if key is None or key in self.closing_keys:
            return
        # 2026-08-16: child-exited indica che il client tmux non può più essere
        # usato; rimuoverlo evita di lasciare una vista morta con “exited”.
        self.terminals.pop(key, None)
        self.sessions.pop(key, None)
        self.initial_commands.pop(key, None)
        self.session_checks.pop(key, None)
        self.spawn_cancellables.pop(key, None)
        self.on_attention(terminal, False)
        self.stack.remove(terminal)
        if not self.terminals:
            self.server_configured = False
            self.server_configuring = False
            self.server_configuration_dirty = False
        self.request_activity_refresh()
        project_name, terminal_name_value = key.split("/", 1)
        self.on_exit(project_name, terminal_name_value, status)

    def _on_spawned(
        self,
        terminal: Vte.Terminal,
        pid: int,
        error: GLib.Error | None,
        metadata: tuple[str, str, str, str],
    ) -> None:
        """Report Vte startup errors or attach recovery metadata to tmux."""

        key, project_name, project_path, terminal_name_value = metadata
        self.spawn_cancellables.pop(key, None)
        initial_command = self.initial_commands.pop(key, None)
        if error or pid == -1:
            self.on_error(f"Starting terminal {key}: {error or 'spawn failed'}")
            return
        self._configure_server()
        tmux_session = self.sessions.get(key)
        if tmux_session:
            self._set_metadata(
                tmux_session,
                project_name,
                project_path,
                terminal_name_value,
                "Codex" if initial_command == "codex resume" else None,
            )
        if initial_command:
            self._feed_initial_command(terminal, initial_command)
        self.request_activity_refresh()

    @staticmethod
    def _feed_initial_command(terminal: Vte.Terminal, command: str) -> None:
        """Send one trusted launcher command to the newly attached tmux shell."""

        # 2026-08-16: feed_child usa il normale input terminale, così Codex gira
        # dentro la shell persistente e al termine lascia il terminale aperto.
        terminal.feed_child((command + "\n").encode("utf-8"))

    def _configure_server(self) -> None:
        """Apply all global tmux options once for the current server lifetime."""

        if not self.sessions or self.server_configured or self.server_configuring:
            return
        # 2026-08-17: tmux owns these options globally; batching and retaining
        # completion state avoids repeating nine processes for every VTE child.
        options = {
            "mouse": "on",
            "status": "on" if self.status_bar_enabled else "off",
            "status-left": (
                "#{@slate_project_display} | #{@slate_terminal_display} | "
                "#{?#{&&:#{@slate_agent_display},"
                "#{==:#{pane_current_command},node}},"
                "#{@slate_agent_display},#{pane_current_command}}"
            ),
            "status-left-length": "120",
            "status-right": "%H:%M",
            "status-right-length": "5",
            "window-status-format": "",
            "window-status-current-format": "",
            "status-interval": "2",
        }
        commands = [
            ("set-option", "-g", option, value)
            for option, value in options.items()
        ]
        # 2026-08-18: una sessione tmux sopravvissuta può conservare un valore
        # locale che prevale sul nuovo default globale; riallineiamo quindi
        # tutte le sessioni già collegate al setting caricato da SLATE.
        status_value = "on" if self.status_bar_enabled else "off"
        commands.extend(
            (
                "set-option",
                "-t",
                tmux_session,
                "status",
                status_value,
            )
            for tmux_session in sorted(set(self.sessions.values()))
        )
        # 2026-08-18: il binding tmux predefinito inoltra WheelUp alle TUI che
        # dichiarano il mouse; Codex finisce così per muovere il prompt invece
        # di aprire lo storico persistente della sessione.
        commands.extend(
            (
                (
                    "bind-key",
                    "-T",
                    "root",
                    "WheelUpPane",
                    "if-shell",
                    "-F",
                    "#{pane_in_mode}",
                    "send-keys -M",
                    "copy-mode -e",
                ),
                (
                    "bind-key",
                    "-T",
                    "root",
                    "WheelDownPane",
                    "select-pane",
                ),
            )
        )
        self.server_configuring = True
        self.server_configuration_dirty = False
        run_async(self._batched_tmux_argv(commands), self._on_server_configured)

    def _on_server_configured(self, result: CommandResult) -> None:
        """Retain successful global configuration and permit retries on failure."""

        self.server_configuring = False
        rerun = self.server_configuration_dirty
        self.server_configuration_dirty = False
        self.server_configured = result.ok and not rerun
        if not result.ok:
            self.on_error(self._error("tmux configuration", result))
        if rerun:
            self._configure_server()

    def _set_metadata(
        self,
        tmux_session: str,
        project_name: str,
        project_path: str,
        terminal_name_value: str,
        agent_display: str | None = None,
    ) -> None:
        """Persist display names and root path on a tmux session for recovery."""

        values = {
            "@slate_project_name": self._encode_metadata(project_name),
            "@slate_terminal_name": self._encode_metadata(terminal_name_value),
            "@slate_project_display": self._status_label(project_name),
            "@slate_terminal_display": self._status_label(terminal_name_value),
        }
        if project_path:
            # 2026-08-16: encoded metadata cannot collide with the delimiter
            # used by tmux's line-oriented format, even for unusual paths.
            values["@slate_project_path"] = self._encode_metadata(project_path)
        if agent_display:
            # 2026-08-16: il marcatore resta nella sessione tmux dopo il
            # riaggancio della GUI e distingue Codex da un vero processo Node.
            values["@slate_agent_display"] = self._status_label(agent_display)
        commands = [
            ("set-option", "-t", tmux_session, option, value)
            for option, value in values.items()
        ]
        # 2026-08-18: le righe lazy vengono collegate anche dopo la prima
        # configurazione globale del server e devono ricevere lo stesso valore.
        commands.append(
            (
                "set-option",
                "-t",
                tmux_session,
                "status",
                "on" if self.status_bar_enabled else "off",
            )
        )
        run_async(self._batched_tmux_argv(commands), self._ignore_metadata_result)

    def _batched_tmux_argv(self, commands: Sequence[Sequence[str]]) -> list[str]:
        """Build one tmux argv containing explicitly separated subcommands."""

        argv = list(self.tmux)
        for index, command in enumerate(commands):
            if index:
                argv.append(";")
            argv.extend(command)
        return argv

    def set_status_bar_enabled(self, enabled: bool) -> None:
        """Apply one status-bar preference without recreating terminal sessions."""

        self.status_bar_enabled = enabled
        if self.sessions:
            self.server_configured = False
            if self.server_configuring:
                self.server_configuration_dirty = True
            self._configure_server()

    def _ignore_metadata_result(self, result: CommandResult) -> None:
        """Report metadata failures without tearing down a working terminal."""

        if not result.ok:
            self.on_error(self._error("Saving tmux metadata", result))

    @staticmethod
    def _status_label(value: str) -> str:
        """Make a user-facing name safe and unambiguous inside the tmux bar."""

        return " ".join(value.split()).replace("|", "¦").replace("#[", "#［")

    def set_activity_monitoring(self, enabled: bool) -> None:
        """Run activity checks only while the owning window is foreground-active."""

        self.activity_monitoring = enabled and not self.shutting_down
        if not self.activity_monitoring:
            self.activity_dirty = False
            self._cancel_activity_timers()
            return
        self.request_activity_refresh()

    def request_activity_refresh(self) -> None:
        """Debounce one topology or focus event into a single activity query."""

        if not self.activity_monitoring or not self.terminals:
            if not self.terminals:
                self._cancel_activity_timers()
            return
        self.activity_dirty = True
        if self.activity_interval_id is not None:
            GLib.source_remove(self.activity_interval_id)
            self.activity_interval_id = None
        if self.activity_command is not None:
            return
        if self.activity_debounce_id is not None:
            GLib.source_remove(self.activity_debounce_id)
        self.activity_debounce_id = GLib.timeout_add(
            self.ACTIVITY_DEBOUNCE_MS, self._on_activity_debounce
        )

    def _cancel_activity_timers(self) -> None:
        """Cancel debounce and interval sources without killing an in-flight query."""

        for source_id in (self.activity_debounce_id, self.activity_interval_id):
            if source_id is not None:
                GLib.source_remove(source_id)
        self.activity_debounce_id = None
        self.activity_interval_id = None

    def _on_activity_debounce(self) -> bool:
        """Start the coalesced foreground activity query after 100 ms quiet."""

        self.activity_debounce_id = None
        self._start_activity_query()
        return GLib.SOURCE_REMOVE

    def _on_activity_interval(self) -> bool:
        """Start the next query five seconds after the preceding completion."""

        self.activity_interval_id = None
        self.activity_dirty = True
        self._start_activity_query()
        return GLib.SOURCE_REMOVE

    def _start_activity_query(self) -> None:
        """Launch the sole lightweight pane query when monitoring remains useful."""

        if (
            not self.activity_monitoring
            or not self.terminals
            or self.activity_command is not None
        ):
            return
        self.activity_dirty = False
        self.activity_command = self.query_panes(
            self._activity_completed,
            include_duration=False,
        )

    def _activity_completed(self, panes: list[PaneInfo]) -> None:
        """Publish one result and schedule deferred or periodic foreground work."""

        self.activity_command = None
        if not self.activity_monitoring or not self.terminals:
            return
        self._publish_activity(panes)
        if self.activity_dirty:
            self.request_activity_refresh()
            return
        self.activity_interval_id = GLib.timeout_add(
            self.ACTIVITY_INTERVAL_MS, self._on_activity_interval
        )

    def _publish_activity(self, panes: list[PaneInfo]) -> None:
        """Map pane activity by tmux session and notify the project tree."""

        self.on_activity({pane.session: pane.active for pane in panes})

    def _enrich_durations(
        self,
        panes: list[PaneInfo],
        callback: Callable[[list[PaneInfo]], None],
    ) -> None:
        """Resolve active foreground durations from each pane tty using ps."""

        active_indexes = [index for index, pane in enumerate(panes) if pane.active]
        if not active_indexes:
            callback(panes)
            return
        pending = {index for index in active_indexes}

        def make_completed(index: int) -> Callable[[CommandResult], None]:
            """Build a result handler tied to one immutable pane index."""

            def completed(result: CommandResult) -> None:
                """Choose the foreground process-group row and store its age."""

                duration = self._parse_foreground_duration(result.stdout)
                panes[index] = replace(panes[index], duration=duration)
                pending.discard(index)
                if not pending:
                    callback(panes)

            return completed

        for index in active_indexes:
            tty = panes[index].tty.removeprefix("/dev/")
            run_async(
                ["ps", "-t", tty, "-o", "etimes=,pid=,pgid=,tpgid="],
                make_completed(index),
            )

    @staticmethod
    def _parse_foreground_duration(output: str) -> int | None:
        """Return elapsed seconds for the process-group leader in foreground."""

        candidates: list[tuple[int, int]] = []
        for line in output.splitlines():
            fields = line.split()
            if len(fields) != 4:
                continue
            try:
                elapsed, pid, pgid, foreground_pgid = map(int, fields)
            except ValueError:
                continue
            if pgid == foreground_pgid:
                candidates.append((0 if pid == pgid else 1, elapsed))
        return sorted(candidates)[0][1] if candidates else None

    @staticmethod
    def _server_absent(result: CommandResult) -> bool:
        """Return whether tmux reported that the dedicated server is absent."""

        text = f"{result.stdout}\n{result.stderr}".lower()
        return "no server running" in text or "no such file or directory" in text

    @staticmethod
    def _error(context: str, result: CommandResult) -> str:
        """Format a concise tmux/process error for the GUI."""

        detail = str(result.error) if result.error else result.stderr.strip()
        return f"{context}: {detail or f'exit code {result.returncode}'}"

    @staticmethod
    def _encode_metadata(value: str) -> str:
        """Encode arbitrary display/path text into delimiter-safe tmux metadata."""

        return base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii")

    @staticmethod
    def _decode_metadata(value: str) -> str:
        """Decode app metadata, returning empty text for absent/legacy values."""

        if not value:
            return ""
        try:
            return base64.urlsafe_b64decode(value.encode("ascii")).decode("utf-8")
        except (binascii.Error, ValueError, UnicodeDecodeError):
            return ""
