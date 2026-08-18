"""Static guardrails for the normative repository architecture rules."""

import ast
import unittest
from pathlib import Path


class ArchitectureRulesTest(unittest.TestCase):
    """Prevent regressions to blocking processes or undocumented functions."""

    def _python_sources(self) -> list[Path]:
        """Return production Python files without generated cache content."""

        return sorted(Path("slate").rglob("*.py"))

    def test_every_function_has_a_docstring(self) -> None:
        """Every named production function explains its behavior."""

        missing: list[str] = []
        for path in self._python_sources():
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if ast.get_docstring(node) is None:
                        missing.append(f"{path}:{node.lineno}:{node.name}")
        self.assertEqual(missing, [])

    def test_production_has_no_anonymous_callbacks(self) -> None:
        """Named callbacks remain inspectable and commentable."""

        locations: list[str] = []
        for path in self._python_sources():
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Lambda):
                    locations.append(f"{path}:{node.lineno}")
        self.assertEqual(locations, [])

    def test_blocking_process_modules_are_not_used(self) -> None:
        """Production commands must stay on Gio.Subprocess and the GLib loop."""

        forbidden: list[str] = []
        for path in self._python_sources():
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    names = (
                        [alias.name for alias in node.names]
                        if isinstance(node, ast.Import)
                        else [node.module or ""]
                    )
                    if any(name in {"subprocess", "threading"} for name in names):
                        forbidden.append(f"{path}:{node.lineno}")
        self.assertEqual(forbidden, [])


if __name__ == "__main__":
    unittest.main()
