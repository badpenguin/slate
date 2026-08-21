"""Launcher tests that avoid starting or disturbing the active SLATE instance."""

import io
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import call, patch

from slate import launcher


class DetachedLauncherTest(unittest.TestCase):
    """Verify foreground selection and terminal descriptor separation."""

    def test_preflight_requires_tmux_but_not_optional_feature_tools(self) -> None:
        """A setup without SCM or search executables may pass preflight."""

        def _find_tmux_only(name: str) -> str | None:
            """Expose only the mandatory tmux executable to the preflight."""

            return "/usr/bin/tmux" if name == "tmux" else None

        with patch.object(launcher, "_missing_gi_dependencies", return_value=[]):
            with patch.object(
                launcher.shutil, "which", side_effect=_find_tmux_only
            ) as which:
                self.assertTrue(launcher._preflight_application())
        which.assert_called_once_with("tmux")

    def test_typelib_preflight_reports_every_missing_namespace(self) -> None:
        """GI availability is validated without importing or initializing GTK."""

        def require_version(namespace: str, _version: str) -> None:
            """Simulate two absent native introspection namespaces."""

            if namespace in {"Vte", "WebKit2"}:
                raise ValueError(namespace)

        fake_gi = SimpleNamespace(require_version=require_version)
        with patch.dict(sys.modules, {"gi": fake_gi}):
            self.assertEqual(
                launcher._missing_gi_dependencies(),
                ["typelib Vte 2.91", "typelib WebKit2 4.1"],
            )

    def test_agent_debug_runs_directly_in_foreground(self) -> None:
        """The explicit debug mode bypasses every daemonization operation."""

        with patch.object(launcher, "_run_application", return_value=7) as run:
            with patch.object(launcher, "_detach") as detach:
                result = launcher.main(["slate", "--agent-debug"])
        self.assertEqual(result, 7)
        run.assert_called_once_with(["slate", "--agent-debug"])
        detach.assert_not_called()

    def test_unknown_option_fails_generically_before_detaching(self) -> None:
        """Every unsupported option fails visibly without reaching GTK or fork."""

        error_stream = io.StringIO()
        with patch.object(sys, "stderr", error_stream):
            with patch.object(launcher, "_run_application") as run:
                with patch.object(launcher, "_detach") as detach:
                    result = launcher.main(["slate", "--sconosciuta"])
        self.assertEqual(result, 2)
        self.assertEqual(
            error_stream.getvalue(), "Unsupported option: '--sconosciuta'\n"
        )
        run.assert_not_called()
        detach.assert_not_called()

    def test_debug_modes_cannot_be_combined(self) -> None:
        """Production and isolated diagnostics remain mutually exclusive."""

        with patch.object(launcher, "_run_application") as run:
            with patch.object(launcher, "_detach") as detach:
                result = launcher.main(["slate", "--debug", "--agent-debug"])
        self.assertEqual(result, 2)
        run.assert_not_called()
        detach.assert_not_called()

    def test_production_debug_removes_only_its_launcher_flag(self) -> None:
        """Debug diagnosis enables fatal-signal stacks without detaching."""

        with patch.object(launcher.faulthandler, "is_enabled", return_value=False):
            with patch.object(launcher.faulthandler, "enable") as enable:
                with patch.object(launcher.faulthandler, "disable") as disable:
                    with patch.object(launcher, "_run_application", return_value=4) as run:
                        with patch.object(launcher, "_detach") as detach:
                            result = launcher.main(["slate", "--debug"])
        self.assertEqual(result, 4)
        run.assert_called_once_with(["slate"])
        detach.assert_not_called()
        enable.assert_called_once_with(all_threads=True)
        disable.assert_called_once_with()

    def test_normal_launch_uses_detached_path(self) -> None:
        """A normal invocation detaches only after a successful preflight."""

        with patch.object(launcher, "_preflight_application", return_value=True) as preflight:
            with patch.object(launcher, "_detach", return_value=0) as detach:
                with patch.object(launcher, "_run_application") as run:
                    result = launcher.main(["slate"])
        self.assertEqual(result, 0)
        preflight.assert_called_once_with()
        detach.assert_called_once_with(["slate"])
        run.assert_not_called()

    def test_failed_preflight_reports_failure_without_detaching(self) -> None:
        """A startup validation failure remains visible in the calling terminal."""

        with patch.object(launcher, "_preflight_application", return_value=False):
            with patch.object(launcher, "_detach") as detach:
                result = launcher.main(["slate"])
        self.assertEqual(result, 2)
        detach.assert_not_called()

    def test_parent_reaps_intermediate_child_and_returns(self) -> None:
        """The calling terminal waits only for the short intermediate process."""

        with patch.object(launcher.os, "fork", return_value=123):
            with patch.object(launcher.os, "waitpid", return_value=(123, 0)) as wait:
                result = launcher._detach(["slate"])
        self.assertEqual(result, 0)
        wait.assert_called_once_with(123, 0)

    def test_final_child_detaches_before_importing_the_application(self) -> None:
        """Only the final daemon process imports and runs the GTK application."""

        events: list[object] = []

        def record_setsid() -> None:
            """Record creation of the detached POSIX session."""

            events.append("setsid")

        def record_chdir(path: str) -> None:
            """Record the daemon working-directory change."""

            events.append(("chdir", path))

        def record_redirect() -> None:
            """Record standard-stream detachment."""

            events.append("redirect")

        def record_run(argv: list[str]) -> int:
            """Record application import and return its simulated status."""

            events.append(("run", argv))
            return 5

        def record_exit(status: int) -> None:
            """Record the final daemon exit status."""

            events.append(("exit", status))

        with patch.object(launcher.os, "fork", side_effect=(0, 0)):
            with patch.object(launcher.os, "setsid", side_effect=record_setsid):
                with patch.object(
                    launcher.os,
                    "chdir",
                    side_effect=record_chdir,
                ):
                    with patch.object(
                        launcher,
                        "_redirect_standard_streams",
                        side_effect=record_redirect,
                    ):
                        with patch.object(
                            launcher,
                            "_run_application",
                            side_effect=record_run,
                        ):
                            with patch.object(
                                launcher.os,
                                "_exit",
                                side_effect=record_exit,
                            ):
                                launcher._detach(["slate"])
        self.assertEqual(
            events,
            ["setsid", ("chdir", "/"), "redirect", ("run", ["slate"]), ("exit", 5)],
        )

    def test_standard_streams_are_redirected_to_dev_null(self) -> None:
        """Detached GTK cannot retain stdin, stdout or stderr from the terminal."""

        with patch.object(launcher.os, "open", return_value=10) as opened:
            with patch.object(launcher.os, "dup2") as duplicated:
                with patch.object(launcher.os, "close") as closed:
                    launcher._redirect_standard_streams()
        opened.assert_called_once_with(launcher.os.devnull, launcher.os.O_RDWR)
        self.assertEqual(
            duplicated.call_args_list,
            [call(10, 0), call(10, 1), call(10, 2)],
        )
        closed.assert_called_once_with(10)


if __name__ == "__main__":
    unittest.main()
