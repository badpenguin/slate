"""SLATE application package."""

from importlib.metadata import PackageNotFoundError, version


try:
    # 2026-08-18: pyproject è l'unica fonte della versione; il pacchetto
    # installato la espone tramite i metadati generati dal backend di build.
    __version__ = version("slate-agent-terminal-environment")
except PackageNotFoundError:
    # 2026-08-18: un checkout non installato può non disporre di metadata; il
    # marker esplicito evita di introdurre una seconda versione di rilascio.
    __version__ = "0+unknown"
