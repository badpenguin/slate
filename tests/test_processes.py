"""GLib-loop tests for the shared non-blocking command runner."""

import unittest

from gi.repository import GLib

from slate.processes import CommandResult, run_async


class AsyncCommandTest(unittest.TestCase):
    """Verify completion and startup errors arrive through the main loop."""

    def test_success_captures_stdout_and_stderr(self) -> None:
        """A successful command returns both output streams and exit status."""

        loop = GLib.MainLoop()
        observed: list[CommandResult] = []

        def completed(result: CommandResult) -> None:
            """Retain the result and stop the isolated test main loop."""

            observed.append(result)
            loop.quit()

        command = run_async(
            ["sh", "-c", "printf out; printf err >&2"], completed
        )
        loop.run()
        self.assertTrue(command.finished)
        self.assertTrue(observed[0].ok)
        self.assertEqual(observed[0].stdout, "out")
        self.assertEqual(observed[0].stderr, "err")

    def test_success_preserves_nul_delimited_output(self) -> None:
        """Binary communication must retain every NUL-delimited Git record."""

        loop = GLib.MainLoop()
        observed: list[CommandResult] = []

        def completed(result: CommandResult) -> None:
            """Retain the NUL-safe result and stop the isolated main loop."""

            observed.append(result)
            loop.quit()

        command = run_async(
            [
                "python3",
                "-c",
                'import sys; sys.stdout.buffer.write(b"first\\0second\\0third")',
            ],
            completed,
        )
        loop.run()
        self.assertTrue(command.finished)
        self.assertTrue(observed[0].ok)
        self.assertEqual(observed[0].stdout, "first\0second\0third")

    def test_missing_executable_is_reported_asynchronously(self) -> None:
        """A spawn error remains observable after its owner retains the command."""

        loop = GLib.MainLoop()
        observed: list[CommandResult] = []

        def completed(result: CommandResult) -> None:
            """Retain the startup failure and stop the isolated main loop."""

            observed.append(result)
            loop.quit()

        command = run_async(["slate-command-that-does-not-exist"], completed)
        self.assertFalse(observed)
        loop.run()
        self.assertTrue(command.finished)
        self.assertIsNotNone(observed[0].error)

if __name__ == "__main__":
    unittest.main()
