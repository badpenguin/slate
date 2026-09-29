"""SLATE application package."""

from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
import tomllib


def _source_version() -> str | None:
    """Read the canonical project version when running from a source checkout."""

    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    if not pyproject.is_file():
        return None
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return None
    project = data.get("project")
    if not isinstance(project, dict) or not isinstance(project.get("version"), str):
        return None
    return project["version"]


# 2026-09-28: il launcher dal checkout deve prevalere sui metadati di una
# vecchia installazione presente nel sistema; pyproject resta l'unica fonte
# modificabile della versione e il pacchetto installato usa i metadata generati.
__version__ = _source_version()
if __version__ is None:
    try:
        __version__ = version("slate-agent-terminal-environment")
    except PackageNotFoundError:
        __version__ = "0+unknown"
