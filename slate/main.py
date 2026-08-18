"""GTK application bootstrap for SLATE."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("Vte", "2.91")
from gi.repository import Gdk, Gio, GLib, Gtk  # noqa: E402

from .browser import WEBKIT_DEPENDENCY_ERROR
from .config import ConfigStore
from .instance import AlreadyRunningError, InstanceLock
from .window import SlateWindow


class SlateApplication(Gtk.Application):
    """Own the sole workbench window and its single configuration store."""

    def __init__(self) -> None:
        """Register the single production or isolated SLATE application."""

        super().__init__(
            # 2026-08-18: l'override è riservato all'istanza agent-debug isolata;
            # il normale application ID continua a garantire l'attivazione D-Bus.
            application_id=os.environ.get(
                "SLATE_APPLICATION_ID",
                "it.slate.SLATE",
            ),
            flags=Gio.ApplicationFlags.FLAGS_NONE,
        )
        self.window: SlateWindow | None = None

    def do_activate(self) -> None:
        """Create the window once or present the existing instance."""

        if self.window is None:
            load_stylesheet()
            self.window = SlateWindow(self, ConfigStore())
            self.window.connect("destroy", self._on_window_destroyed)
        self.window.present()

    def _on_window_destroyed(self, _window: Gtk.Window) -> None:
        """Release the window reference after its clean shutdown."""

        self.window = None


def validate_dependencies() -> bool:
    """Revalidate runtime dependencies inside the application process."""

    # 2026-08-17: SCM executables are optional per project; their asynchronous
    # watcher reports a missing tool locally instead of blocking all of SLATE.
    missing = [name for name in ("tmux",) if shutil.which(name) is None]
    if WEBKIT_DEPENDENCY_ERROR is not None:
        missing.append("typelib WebKit2 4.1")
    if not missing:
        return True
    print(
        "Dipendenze mancanti: " + ", ".join(missing),
        file=sys.stderr,
    )
    return False


def load_stylesheet() -> None:
    """Load hierarchy rules while retaining all system-theme colors."""

    screen = Gdk.Screen.get_default()
    if screen is None:
        return
    stylesheet = Path(__file__).with_name("style.css")
    provider = Gtk.CssProvider()
    try:
        provider.load_from_path(str(stylesheet))
    except GLib.Error as error:
        print(f"Stylesheet non caricato: {error}", file=sys.stderr)
        return
    Gtk.StyleContext.add_provider_for_screen(
        screen, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
    )


def _prepare_agent_debug(
    argv: list[str],
) -> tuple[list[str], tempfile.TemporaryDirectory[str] | None]:
    """Remove --agent-debug and isolate every mutable application namespace."""

    if "--agent-debug" not in argv:
        return argv, None
    clean_argv = [argument for argument in argv if argument != "--agent-debug"]
    debug_directory = tempfile.TemporaryDirectory(prefix="slate-agent-debug-")
    debug_pid = os.getpid()
    # 2026-08-16: la seconda istanza è sicura soltanto se config, D-Bus e tmux
    # non possono scrivere o collegarsi alle risorse dell'istanza principale.
    configured_path = os.environ.get("SLATE_CONFIG")
    production_config = Path.home() / ".config" / "slate" / "config.json"
    if (
        configured_path is None
        or Path(configured_path).expanduser().resolve() == production_config.resolve()
    ):
        debug_config = Path(debug_directory.name) / "config.json"
        if production_config.exists():
            shutil.copy2(production_config, debug_config)
        os.environ["SLATE_CONFIG"] = str(debug_config)
    if os.environ.get("SLATE_TMUX_SOCKET", "slate") == "slate":
        os.environ["SLATE_TMUX_SOCKET"] = f"slate-agent-debug-{debug_pid}"
    if os.environ.get("SLATE_APPLICATION_ID", "it.slate.SLATE") == "it.slate.SLATE":
        os.environ["SLATE_APPLICATION_ID"] = (
            f"it.slate.SLATE.AgentDebug.p{debug_pid}"
        )
    # 2026-08-17: WebKit usa le directory XDG per cookie, storage e cache;
    # uno smoke isolato non deve contaminare il profilo browser di produzione.
    os.environ["XDG_DATA_HOME"] = str(Path(debug_directory.name) / "data")
    os.environ["XDG_CACHE_HOME"] = str(Path(debug_directory.name) / "cache")
    os.environ["SLATE_AGENT_DEBUG"] = "1"
    return clean_argv, debug_directory


def main(argv: list[str] | None = None) -> int:
    """Validate dependencies, acquire the local lock and run GTK."""

    arguments = list(sys.argv if argv is None else argv)
    clean_argv, debug_directory = _prepare_agent_debug(arguments)
    agent_debug = debug_directory is not None
    if not validate_dependencies():
        if debug_directory is not None:
            debug_directory.cleanup()
        return 2
    instance_lock: InstanceLock | None = None
    if not agent_debug:
        try:
            instance_lock = InstanceLock.acquire()
        except AlreadyRunningError:
            # 2026-08-16: g_application_run gestisce anche unregister e cleanup
            # del client remoto; register()+activate() lasciava D-Bus registrato.
            application = SlateApplication()
            return application.run(clean_argv)
    try:
        application = SlateApplication()
        return application.run(clean_argv)
    finally:
        if instance_lock is not None:
            instance_lock.close()
        if debug_directory is not None:
            debug_directory.cleanup()
