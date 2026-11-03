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
from slate.browser import WebKit2
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
            (root / "internal").symlink_to("style.css")
            (root / "loop").symlink_to("loop")
            renderer = DocumentRenderer.__new__(DocumentRenderer)
            renderer.root = root
            renderer.closed = False
            renderer.resource_paths = set()
            renderer.cancellable = Gio.Cancellable()
            for path in ("../private.txt", "escape", "%2e%2e/private.txt", "loop", "missing", "internal", "style.css"):
                request = MagicMock()
                request.get_uri.return_value = "slate-editor://preview/" + path
                loop = GLib.MainLoop()

                def finished(*args):
                    """End the isolated resource request after async validation and open."""

                    loop.quit()

                request.finish.side_effect = finished
                timeout = GLib.timeout_add(2000, loop.quit)
                try:
                    renderer._resource(request)
                    if not request.finish.called:
                        loop.run()
                    request.finish.assert_called_once()
                    if path in {"style.css", "internal"}:
                        stream = request.finish.call_args.args[0]
                        self.assertEqual(stream.read_bytes(32, None).get_data(), b"body{}")
                        stream.close(None)
                    else:
                        self.assertEqual(request.finish.call_args.args[1], 0)
                finally:
                    GLib.source_remove(timeout)

    def test_rendered_document_has_strict_csp_and_relative_asset_base(self) -> None:
        """An HTML buffer cannot opt into scripts, remote resources or arbitrary files."""

        renderer = DocumentRenderer.__new__(DocumentRenderer)
        renderer.relative_path = "docs/page.html"
        renderer.resource_paths = set()
        renderer.view = MagicMock()
        renderer.render('<script>fetch("https://example.org")</script><img src="image.png">')
        document, base = renderer.view.load_html.call_args.args
        self.assertIn("Content-Security-Policy", document)
        self.assertIn("script-src &#x27;none&#x27;", document)
        self.assertIn("default-src &#x27;none&#x27;", document)
        self.assertEqual(base, "slate-editor://preview/docs/page.html")

    def test_renderer_allows_only_same_document_fragment_navigation(self) -> None:
        """Internal anchors work without allowing local files, scripts or unrelated documents."""

        renderer = DocumentRenderer.__new__(DocumentRenderer)
        renderer.uri = "slate-editor://preview/docs/page.md"
        for uri, allowed in ((renderer.uri, True), (renderer.uri + "#section", True),
                             ("slate-editor://preview/docs/other.md#section", False),
                             ("file:///etc/passwd", False), ("javascript:alert(1)", False)):
            with self.subTest(uri=uri):
                decision = MagicMock()
                action = decision.get_navigation_action.return_value
                action.is_user_gesture.return_value = True
                action.get_request.return_value.get_uri.return_value = uri
                handled = renderer._policy(None, decision, WebKit2.PolicyDecisionType.NAVIGATION_ACTION)
                self.assertEqual(handled, not allowed)
                self.assertEqual(decision.ignore.called, not allowed)

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
