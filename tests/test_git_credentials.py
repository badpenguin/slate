"""Tests for Git's ephemeral HTTPS credential integration."""

import io
import unittest
from unittest.mock import patch

from slate import git_credentials


class GitCredentialHelperTest(unittest.TestCase):
    """Verify credential protocol output and non-persistent Git configuration."""

    def test_environment_replaces_persistent_helpers_for_one_process(self) -> None:
        """The push environment selects only SLATE's one-use helper."""

        environment = git_credentials.credential_environment({"LC_ALL": "C"})
        self.assertEqual(environment["LC_ALL"], "C")
        self.assertEqual(environment["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(environment["GIT_CONFIG_COUNT"], "2")
        self.assertEqual(environment["GIT_CONFIG_VALUE_0"], "")
        self.assertIn("slate.git_credentials", environment["GIT_CONFIG_VALUE_1"])

    def test_get_returns_prompted_values_only_on_standard_output(self) -> None:
        """A get request emits the username and token accepted in the GTK form."""

        standard_input = io.StringIO("protocol=https\nhost=github.com\n\n")
        standard_output = io.StringIO()
        with (
            patch.object(git_credentials.sys, "stdin", standard_input),
            patch.object(git_credentials.sys, "stdout", standard_output),
            patch.object(
                git_credentials,
                "_prompt",
                return_value=("badpenguin", "secret-token"),
            ),
        ):
            result = git_credentials.main(["get"])
        self.assertEqual(result, 0)
        self.assertEqual(
            standard_output.getvalue(),
            "username=badpenguin\npassword=secret-token\n\n",
        )

    def test_cancelled_prompt_returns_no_credentials(self) -> None:
        """Cancellation aborts authentication without emitting partial values."""

        standard_input = io.StringIO("protocol=https\nhost=github.com\n\n")
        standard_output = io.StringIO()
        with (
            patch.object(git_credentials.sys, "stdin", standard_input),
            patch.object(git_credentials.sys, "stdout", standard_output),
            patch.object(git_credentials, "_prompt", return_value=None),
        ):
            result = git_credentials.main(["get"])
        self.assertEqual(result, 1)
        self.assertEqual(standard_output.getvalue(), "")

    def test_store_discards_the_token_without_opening_a_dialog(self) -> None:
        """Git's post-success store notification never persists credentials."""

        standard_input = io.StringIO(
            "protocol=https\nhost=github.com\npassword=secret-token\n\n"
        )
        with (
            patch.object(git_credentials.sys, "stdin", standard_input),
            patch.object(git_credentials, "_prompt") as prompt,
        ):
            result = git_credentials.main(["store"])
        self.assertEqual(result, 0)
        prompt.assert_not_called()


if __name__ == "__main__":
    unittest.main()
