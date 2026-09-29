"""Package metadata tests."""

import unittest
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
import tomllib

from slate import __version__


class PackageMetadataTest(unittest.TestCase):
    """Verify runtime package metadata has no second release-version source."""

    def test_version_comes_from_installed_distribution_metadata(self) -> None:
        """Prefer canonical source metadata, then installed generated metadata."""

        pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
        if pyproject.is_file():
            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
            expected = data["project"]["version"]
        else:
            try:
                expected = version("slate-agent-terminal-environment")
            except PackageNotFoundError:
                expected = "0+unknown"
        self.assertEqual(__version__, expected)


if __name__ == "__main__":
    unittest.main()
