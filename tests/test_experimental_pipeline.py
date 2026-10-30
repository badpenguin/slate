"""Integration boundaries for experimental rendering, worker transport and UI routing."""

import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gio, GLib

from slate.editor_render import DocumentRenderer
from slate.processes import run_async
from slate.scm.base import FileStatus
from slate.window import SlateWindow


class ExperimentalPipelineTest(unittest.TestCase):
    """Test private subprocesses and resource validation without launching SLATE."""

    def test_worker_receives_unsaved_text_via_gio_stdin(self) -> None:
        """The real asynchronous transport returns current lines without writing files."""

        results = []
        loop = GLib.MainLoop()

        def completed(result) -> None:
            """Record a single worker result and end this isolated test loop."""

            results.append(result)
            loop.quit()

        def expired() -> bool:
            """Bound the test if the asynchronous worker fails to return."""

            loop.quit()
            return GLib.SOURCE_REMOVE

        timeout = GLib.timeout_add(5000, expired)
        command = run_async([sys.executable, "-m", "slate.editor_diff_worker"], completed,
                            input_bytes=json.dumps({"before": "10px\n", "current": "14px\n"}).encode())
        try:
            loop.run()
            self.assertTrue(results)
            self.assertTrue(results[0].ok, results[0].stderr)
            rows = json.loads(results[0].stdout)
            self.assertEqual([row["text"] for row in rows if row["new_line"] is not None], ["14px\n"])
        finally:
            command.cancel()
            GLib.source_remove(timeout)

    def test_local_resource_validation_rejects_symlink_escape_and_traversal(self) -> None:
        """Project-local resources are streamed; paths beyond the root are never opened."""

        with tempfile.TemporaryDirectory(prefix="slate-render-", dir="/tmp") as directory:
            root = Path(directory) / "project"
            root.mkdir()
            (root / "style.css").write_text("body{}")
            (Path(directory) / "private.txt").write_text("private")
            (root / "escape").symlink_to(Path(directory) / "private.txt")
            renderer = DocumentRenderer.__new__(DocumentRenderer)
            renderer.root = root
            renderer.closed = False
            renderer.cancellable = Gio.Cancellable()
            with patch("slate.editor_render.Gio.File.new_for_path") as file_factory:
                for path in ("../private.txt", "escape", "%2e%2e/private.txt"):
                    request = MagicMock()
                    request.get_uri.return_value = "slate-editor://preview/" + path
                    renderer._resource(request)
                    request.finish.assert_called_once()
                file_factory.assert_not_called()
                request = MagicMock()
                request.get_uri.return_value = "slate-editor://preview/style.css"
                renderer._resource(request)
                file_factory.assert_called_once_with(str(root / "style.css"))

    def test_rendered_document_has_strict_csp_and_relative_asset_base(self) -> None:
        """An HTML buffer cannot opt into scripts, remote resources or arbitrary files."""

        renderer = DocumentRenderer.__new__(DocumentRenderer)
        renderer.relative_path = "docs/page.html"
        renderer.view = MagicMock()
        renderer.render('<script>fetch("https://example.org")</script><img src="image.png">')
        document, base = renderer.view.load_html.call_args.args
        self.assertIn("Content-Security-Policy", document)
        self.assertIn("script-src &#x27;none&#x27;", document)
        self.assertIn("default-src &#x27;none&#x27;", document)
        self.assertEqual(base, "slate-editor://preview/docs/page.html")

    def test_revision_and_files_selection_use_the_same_opening_route(self) -> None:
        """Experimental selection bypasses the classic preview in either sidebar."""

        status = FileStatus("style.css", "modified")
        owner = SimpleNamespace(
            _workspace_repository_path=MagicMock(return_value="style.css"),
            _open_experimental_file=MagicMock(return_value=object()))
        SlateWindow._preview_file(owner, status)
        owner._open_experimental_file.assert_called_once_with("style.css", status)
        owner._open_experimental_file.reset_mock()
        owner.active_project_name = "project"
        owner.config = SimpleNamespace(find_project=MagicMock(return_value={"name": "project", "path": "/tmp/project"}))
        SlateWindow._preview_project_file(owner, "style.css")
        owner._open_experimental_file.assert_called_once_with("style.css")

    def test_removed_files_are_routed_to_readonly_history(self) -> None:
        """A missing source file must never be created merely by selecting its diff."""

        owner = SimpleNamespace(
            _workspace_repository_path=MagicMock(return_value="gone.css"),
            _open_experimental_history=MagicMock())
        status = FileStatus("gone.css", "removed")
        SlateWindow._preview_file(owner, status)
        owner._open_experimental_history.assert_called_once_with(status)
