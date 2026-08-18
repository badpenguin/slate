"""Package metadata tests."""

import unittest
from importlib.metadata import PackageNotFoundError, version

from slate import __version__


class PackageMetadataTest(unittest.TestCase):
    """Verify runtime package metadata has no second release-version source."""

    def test_version_comes_from_installed_distribution_metadata(self) -> None:
        """Expose generated metadata or an explicit uninstalled-checkout marker."""

        try:
            expected = version("slate-agent-terminal-environment")
        except PackageNotFoundError:
            expected = "0+unknown"
        self.assertEqual(__version__, expected)


if __name__ == "__main__":
    unittest.main()
