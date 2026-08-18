"""Non-blocking process helpers built on the GLib main loop."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Mapping, Sequence

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib  # noqa: E402


@dataclass(frozen=True)
class CommandResult:
    """Describe the completed execution of an external command."""

    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    error: Exception | None = None

    @property
    def ok(self) -> bool:
        """Return whether the command completed successfully."""

        return self.error is None and self.returncode == 0


class AsyncCommand:
    """Own one Gio subprocess until its asynchronous communication completes."""

    def __init__(
        self,
        argv: Sequence[str],
        cwd: str | None,
        env: Mapping[str, str] | None,
        callback: Callable[[CommandResult], None],
    ) -> None:
        """Spawn a command without blocking and retain callback state."""

        self.argv = tuple(argv)
        self.callback = callback
        self.cancellable = Gio.Cancellable()
        self.process: Gio.Subprocess | None = None
        self.finished = False

        # 2026-08-16: a launcher gives every VCS call deterministic cwd and
        # locale without modifying the process-wide environment.
        launcher = Gio.SubprocessLauncher.new(
            Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_PIPE
        )
        if cwd:
            launcher.set_cwd(cwd)
        for key, value in (env or {}).items():
            launcher.setenv(key, value, True)

        try:
            self.process = launcher.spawnv(self.argv)
            # 2026-08-18: Git usa record separati da NUL per conservare path
            # arbitrari; l'API UTF-8 li tronca al primo NUL, quindi il trasporto
            # resta binario fino alla decodifica esplicita del risultato completo.
            self.process.communicate_async(
                None, self.cancellable, self._on_communicated
            )
        except Exception as error:  # GLib.Error is not the only startup error.
            # 2026-08-16: startup failures are delivered on the next main-loop
            # turn so owners can retain the command before its callback runs.
            GLib.idle_add(
                self._deliver_start_error,
                CommandResult(self.argv, -1, "", "", error),
            )

    def cancel(self) -> None:
        """Cancel pending communication and force-exit the child if necessary."""

        self.cancellable.cancel()
        if self.process and not self.finished:
            self.process.force_exit()

    def _on_communicated(
        self, process: Gio.Subprocess, result: Gio.AsyncResult
    ) -> None:
        """Convert Gio completion data into the application result type."""

        try:
            _successful, stdout, stderr = process.communicate_finish(result)
            self.finished = True
            returncode = process.get_exit_status() if process.get_if_exited() else -1
            command_result = CommandResult(
                self.argv,
                returncode,
                stdout.get_data().decode("utf-8") if stdout is not None else "",
                stderr.get_data().decode("utf-8") if stderr is not None else "",
            )
        except Exception as error:
            self.finished = True
            command_result = CommandResult(self.argv, -1, "", "", error)
        self.callback(command_result)

    def _deliver_start_error(self, result: CommandResult) -> bool:
        """Deliver a spawn failure asynchronously like normal completion."""

        self.finished = True
        self.callback(result)
        return GLib.SOURCE_REMOVE


def run_async(
    argv: Sequence[str],
    callback: Callable[[CommandResult], None],
    *,
    cwd: str | None = None,
    env: Mapping[str, str] | None = None,
) -> AsyncCommand:
    """Start a command and return the object that owns its lifetime."""

    return AsyncCommand(argv, cwd, env, callback)


def spawn_detached(
    argv: Sequence[str], *, cwd: str | None = None, env: Mapping[str, str] | None = None
) -> Gio.Subprocess:
    """Launch an external GUI/tool without waiting for it to finish."""

    launcher = Gio.SubprocessLauncher.new(Gio.SubprocessFlags.NONE)
    if cwd:
        launcher.set_cwd(cwd)
    for key, value in (env or {}).items():
        launcher.setenv(key, value, True)
    return launcher.spawnv(tuple(argv))
