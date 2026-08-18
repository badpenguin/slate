"""Detach normal SLATE launches while retaining an observable debug mode."""

from __future__ import annotations

import faulthandler
import os
import shutil
import sys
from collections.abc import Sequence


def _missing_gi_dependencies() -> list[str]:
    """Return unavailable Python bindings and typelibs without initializing GTK."""

    try:
        import gi
    except ImportError:
        return ["PyGObject"]

    missing: list[str] = []
    # 2026-08-18: require_version validates typelib availability but does not
    # construct GTK objects, so the normal process remains safe to fork.
    for namespace, version, label in (
        ("Gtk", "3.0", "typelib GTK 3"),
        ("Vte", "2.91", "typelib Vte 2.91"),
        ("GtkSource", "4", "typelib GtkSourceView 4"),
        ("WebKit2", "4.1", "typelib WebKit2 4.1"),
    ):
        try:
            gi.require_version(namespace, version)
        except ValueError:
            missing.append(label)
    return missing


def _preflight_application() -> bool:
    """Report mandatory dependencies before the normal launcher forks."""

    missing = _missing_gi_dependencies()
    missing.extend(name for name in ("tmux",) if shutil.which(name) is None)
    if not missing:
        return True
    print("Dipendenze mancanti: " + ", ".join(missing), file=sys.stderr)
    return False


def _run_application(argv: list[str]) -> int:
    """Import GTK after process handling and run SLATE."""

    from .main import main as application_main

    return application_main(argv)


def _redirect_standard_streams() -> None:
    """Disconnect the detached GUI from the launching terminal file descriptors."""

    null_fd = os.open(os.devnull, os.O_RDWR)
    try:
        for descriptor in (0, 1, 2):
            os.dup2(null_fd, descriptor)
    finally:
        if null_fd > 2:
            os.close(null_fd)


def _detach(argv: list[str]) -> int:
    """Start SLATE through the standard Linux double-fork sequence."""

    try:
        first_pid = os.fork()
    except OSError as error:
        print(f"Impossibile separare SLATE dal terminale: {error}", file=sys.stderr)
        return 1
    if first_pid > 0:
        # 2026-08-16: il launcher raccoglie il figlio intermedio per non lasciare
        # zombie e restituisce appena il processo GUI è stato affidato a init.
        _waited_pid, status = os.waitpid(first_pid, 0)
        return os.waitstatus_to_exitcode(status)

    try:
        # 2026-08-16: setsid rimuove terminale e process group ereditati; il
        # secondo fork impedisce al processo GTK di acquisirli nuovamente.
        os.setsid()
        second_pid = os.fork()
        if second_pid > 0:
            os._exit(0)
        os.chdir("/")
        _redirect_standard_streams()
        # 2026-08-16: soltanto il processo GUI finale importa GTK e acquisisce
        # il lock, così il PID diagnostico coincide con il processo persistente.
        result = _run_application(argv)
    except BaseException:
        # A questo punto stderr non appartiene più al terminale: l'exit status
        # evita comunque che un errore lasci vivo un launcher intermedio.
        os._exit(1)
    os._exit(result)


def main(argv: Sequence[str] | None = None) -> int:
    """Select detached, production-debug or isolated-agent startup."""

    arguments = list(sys.argv if argv is None else argv)
    supported_options = {"--debug", "--agent-debug"}
    unsupported = [
        argument for argument in arguments[1:] if argument not in supported_options
    ]
    if unsupported:
        # 2026-08-18: validate the current CLI before detach so every unknown
        # option gets the same visible error without historical aliases.
        print(f"Opzione non supportata: {unsupported[0]!r}", file=sys.stderr)
        return 2
    if supported_options.issubset(arguments):
        print("Le opzioni --debug e --agent-debug sono incompatibili.", file=sys.stderr)
        return 2
    if "--agent-debug" in arguments:
        return _run_application(arguments)
    if "--debug" in arguments:
        # 2026-08-18: debug retains production resources and removes only its
        # launcher flag before Gtk.Application parses the remaining arguments.
        debug_arguments = [
            argument for argument in arguments if argument != "--debug"
        ]
        fault_handler_was_enabled = faulthandler.is_enabled()
        if not fault_handler_was_enabled:
            # 2026-08-17: i SIGSEGV nelle librerie GTK non generano eccezioni
            # Python; il terminale debug deve comunque mostrare gli stack.
            faulthandler.enable(all_threads=True)
        try:
            return _run_application(debug_arguments)
        finally:
            if not fault_handler_was_enabled:
                faulthandler.disable()
    # 2026-08-16: il detach è l'ultima fase del launcher normale, dopo tutti i
    # controlli capaci di produrre un errore utile per chi lo avvia da shell.
    if not _preflight_application():
        return 2
    return _detach(arguments)
