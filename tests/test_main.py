"""Bootstrap tests for explicit isolated agent-debug instances."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from slate import main as slate_main
from slate.instance import AlreadyRunningError
from slate.main import _prepare_agent_debug


class AgentDebugBootstrapTest(unittest.TestCase):
    """Verify that only the explicit flag creates isolated mutable namespaces."""

    def test_normal_arguments_do_not_enable_a_second_namespace(self) -> None:
        """A normal invocation leaves production identity and locking unchanged."""

        arguments = ["slate"]
        clean, directory = _prepare_agent_debug(arguments)
        self.assertEqual(clean, arguments)
        self.assertIsNone(directory)

    def test_preflight_requires_tmux_but_not_optional_feature_tools(self) -> None:
        """Git, HG and ripgrep stay optional while tmux remains mandatory."""

        def executable(name: str) -> str | None:
            """Expose only tmux to the dependency validator."""

            return "/usr/bin/tmux" if name == "tmux" else None

        with patch("slate.main.shutil.which", side_effect=executable) as which:
            self.assertTrue(slate_main.validate_dependencies())
        which.assert_called_once_with("tmux")

    def test_agent_debug_copies_config_and_changes_process_namespaces(self) -> None:
        """The explicit debug flag cannot attach to production config, D-Bus or tmux."""

        with tempfile.TemporaryDirectory() as home_directory:
            production = Path(home_directory) / ".config" / "slate" / "config.json"
            production.parent.mkdir(parents=True)
            production.write_text('{"projects": []}\n', encoding="utf-8")
            with patch.dict(os.environ, {"HOME": home_directory}, clear=False):
                for name in (
                    "SLATE_CONFIG",
                    "SLATE_TMUX_SOCKET",
                    "SLATE_APPLICATION_ID",
                    "SLATE_AGENT_DEBUG",
                    "XDG_DATA_HOME",
                    "XDG_CACHE_HOME",
                ):
                    os.environ.pop(name, None)
                clean, directory = _prepare_agent_debug(
                    ["slate", "--agent-debug"]
                )
                try:
                    self.assertEqual(clean, ["slate"])
                    self.assertIsNotNone(directory)
                    self.assertNotEqual(Path(os.environ["SLATE_CONFIG"]), production)
                    self.assertEqual(
                        Path(os.environ["SLATE_CONFIG"]).read_text(encoding="utf-8"),
                        '{"projects": []}\n',
                    )
                    self.assertTrue(
                        os.environ["SLATE_TMUX_SOCKET"].startswith("slate-agent-debug-")
                    )
                    self.assertIn("AgentDebug", os.environ["SLATE_APPLICATION_ID"])
                    self.assertEqual(os.environ["SLATE_AGENT_DEBUG"], "1")
                    self.assertTrue(
                        Path(os.environ["XDG_DATA_HOME"]).is_relative_to(
                            directory.name
                        )
                    )
                    self.assertTrue(
                        Path(os.environ["XDG_CACHE_HOME"]).is_relative_to(
                            directory.name
                        )
                    )
                finally:
                    if directory is not None:
                        directory.cleanup()

    def test_second_launch_uses_application_run_for_remote_activation(self) -> None:
        """An existing instance is activated through the complete GLib lifecycle."""

        application = Mock()
        application.run.return_value = 0
        with patch.object(slate_main, "validate_dependencies", return_value=True):
            with patch.object(
                slate_main.InstanceLock,
                "acquire",
                side_effect=AlreadyRunningError(123),
            ):
                with patch.object(
                    slate_main,
                    "SlateApplication",
                    return_value=application,
                ):
                    result = slate_main.main(["slate"])
        self.assertEqual(result, 0)
        application.run.assert_called_once_with(["slate"])

    def test_activation_builds_and_presents_the_application_window(self) -> None:
        """Normal and diagnostic activation always present their window."""

        window = Mock()
        config = Mock()
        owner = Mock(
            window=None,
            _on_window_destroyed=Mock(),
        )
        with patch.object(slate_main, "load_stylesheet"):
            with patch.object(slate_main, "ConfigStore", return_value=config):
                with patch.object(
                    slate_main,
                    "SlateWindow",
                    return_value=window,
                ) as window_class:
                    slate_main.SlateApplication.do_activate(owner)
        window_class.assert_called_once_with(owner, config)
        window.connect.assert_called_once_with(
            "destroy",
            owner._on_window_destroyed,
        )
        window.present.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
