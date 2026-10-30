"""Isolated local-document rendering for the experimental editor."""

import html
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

from gi.repository import Gio, GLib

from .browser import WebKit2
from .preview import github_flavored_markdown_to_html


class DocumentRenderer:
    """Render buffer text without granting WebKit filesystem or network access."""

    def __init__(self, root: str, relative_path: str) -> None:
        """Create an ephemeral web context and a validated local resource scheme."""

        if WebKit2 is None:
            raise RuntimeError("Rendered mode requires WebKitGTK 4.1")
        self.root = Path(root).resolve()
        self.relative_path = relative_path
        self.closed = False
        self.cancellable = Gio.Cancellable()
        self.context = WebKit2.WebContext.new_ephemeral()
        # 2026-10-30: file:// would grant pages access beyond the project.
        # Every resource instead passes this scheme's canonical path check.
        self.context.register_uri_scheme("slate-editor", self._resource)
        self.view = WebKit2.WebView.new_with_context(self.context)
        settings = self.view.get_settings()
        settings.set_enable_javascript(False)
        settings.set_enable_html5_database(False)
        settings.set_enable_html5_local_storage(False)
        self.view.connect("decide-policy", self._policy)

    def render(self, text: str) -> None:
        """Display the current unsaved Markdown or HTML with a strict resource policy."""

        if Path(self.relative_path).suffix.lower() in {".md", ".markdown"}:
            if github_flavored_markdown_to_html is None:
                raise RuntimeError("Markdown rendering requires python3-cmarkgfm")
            text = github_flavored_markdown_to_html(text)
            text = '<style>body{font:16px/1.55 sans-serif;max-width:980px;margin:auto;padding:24px}' \
                   'pre{overflow:auto}img{max-width:100%}table{border-collapse:collapse}' \
                   'td,th{border:1px solid #aaa;padding:6px}</style>' + text
        policy = ("default-src 'none'; img-src slate-editor: data:; "
                  "style-src 'unsafe-inline' slate-editor:; font-src slate-editor: data:; "
                  "script-src 'none'; connect-src 'none'; frame-src 'none'; "
                  "base-uri 'none'; form-action 'none'")
        document = '<!doctype html><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="' \
                   + html.escape(policy, quote=True) + '">' + text
        self.uri = "slate-editor://preview/" + quote(self.relative_path, safe="/")
        self.view.load_html(document, self.uri)

    def _resource(self, request) -> None:
        """Stream only canonical project files requested through the private scheme."""

        try:
            parsed = urlsplit(request.get_uri())
            relative = unquote(parsed.path).lstrip("/")
            if self.closed or parsed.netloc != "preview" or ".." in Path(relative).parts:
                raise ValueError("Resource is outside this document")
            target = (self.root / relative).resolve(strict=True)
            target.relative_to(self.root)
            if not target.is_file():
                raise ValueError("Resource is not a file")
        except (ValueError, OSError, RuntimeError):
            request.finish(Gio.MemoryInputStream.new_from_bytes(GLib.Bytes.new(b"")), 0, "text/plain")
            return
        content_type, _uncertain = Gio.content_type_guess(str(target), None)
        mime = Gio.content_type_get_mime_type(content_type) or "application/octet-stream"

        def opened(source, result) -> None:
            """Finish the resource without loading its full contents into Python memory."""

            try:
                stream = source.read_finish(result)
                if self.closed:
                    stream.close(None)
                    return
                request.finish(stream, -1, mime)
            except Exception:
                request.finish(Gio.MemoryInputStream.new_from_bytes(GLib.Bytes.new(b"")), 0, "text/plain")

        Gio.File.new_for_path(str(target)).read_async(0, self.cancellable, opened)

    def _policy(self, _view, decision, kind) -> bool:
        """Allow the synthetic document, routing explicit web links to the desktop."""

        if kind not in (WebKit2.PolicyDecisionType.NAVIGATION_ACTION,
                        WebKit2.PolicyDecisionType.NEW_WINDOW_ACTION):
            return False
        action = decision.get_navigation_action()
        uri = action.get_request().get_uri()
        if not action.is_user_gesture() and uri == getattr(self, "uri", None):
            return False
        decision.ignore()
        if action.is_user_gesture() and urlsplit(uri).scheme in {"http", "https", "mailto"}:
            Gio.AppInfo.launch_default_for_uri_async(uri, None, None, self._link_opened, None)
        return True

    def _link_opened(self, _source, result, _data=None) -> None:
        """Consume desktop link-launch completion without changing editor state."""

        try:
            Gio.AppInfo.launch_default_for_uri_finish(result)
        except Exception:
            pass

    def shutdown(self) -> None:
        """Cancel pending local resource reads and stop rendering."""

        self.closed = True
        self.cancellable.cancel()
        self.view.stop_loading()
