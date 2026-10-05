"""Read-only overlay previews for SCM diffs and complete source files."""

from __future__ import annotations

from functools import partial
import html
from pathlib import Path
import re
from typing import Callable
from urllib.parse import unquote, urlsplit

import gi

gi.require_version("Gio", "2.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("Gtk", "3.0")
gi.require_version("GtkSource", "4")
from gi.repository import Gdk, GdkPixbuf, Gio, GLib, Gtk, GtkSource, Pango  # noqa: E402

from .processes import AsyncCommand, CommandResult, run_async
from .browser import WebKit2
from .search import source_symbol_at_cursor
from .scm.base import FileStatus, SCM
from .text_search import TextSearchBar


CMARK_DEPENDENCY_ERROR: str | None = None
try:
    from cmarkgfm import github_flavored_markdown_to_html
except ImportError as error:
    # 2026-09-27: l'import tollerante consente al preflight di mostrare una
    # dipendenza mancante leggibile invece di interrompere l'avvio con traceback.
    github_flavored_markdown_to_html = None  # type: ignore[assignment]
    CMARK_DEPENDENCY_ERROR = str(error)


def map_diff_lines(text: str) -> tuple[int | None, ...]:
    """Map every unified-diff row to its one-based working-file line."""

    mapped: list[int | None] = []
    current_line: int | None = None
    # 2026-09-27: la mappa pura può essere riusata sia dalla preview sia
    # dall'apertura diretta di Edit with diff dal pannello Revisioni.
    for line in text.splitlines(keepends=True):
        if line.startswith("@@"):
            match = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
            current_line = max(1, int(match.group(1))) if match else None
            edit_line = current_line
        elif line.startswith(("diff ", "index ", "--- ", "+++ ")):
            edit_line = None
        elif line.startswith("+"):
            edit_line = current_line
            if current_line is not None:
                current_line += 1
        elif line.startswith("-"):
            edit_line = current_line
        elif line.startswith("\\ No newline"):
            edit_line = None
        elif line.startswith(" "):
            edit_line = current_line
            if current_line is not None:
                current_line += 1
        else:
            edit_line = None
        mapped.append(edit_line)
    return tuple(mapped)


class FilePreview(Gtk.Box):
    """Render one selected path without allowing edits to the working copy."""

    MAX_TEXT_BYTES = 5 * 1024 * 1024
    MAX_IMAGE_BYTES = 20 * 1024 * 1024
    MAX_IMAGE_PIXELS = 40_000_000

    def __init__(
        self,
        on_close: Callable[[], None],
        on_definition: Callable[[str, str, str], None] | None = None,
        on_action: Callable[[str], None] | None = None,
    ) -> None:
        """Build a header and GtkSourceView suitable for diff and source content."""

        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.on_close = on_close
        self.on_definition = on_definition
        self.on_action = on_action
        self.current_path: str | None = None
        self.current_root: str | None = None
        self.current_file_panel_external = False
        self.command: AsyncCommand | None = None
        self.file_cancellable: Gio.Cancellable | None = None
        self.request_serial = 0
        self.image_source_pixbuf: GdkPixbuf.Pixbuf | None = None
        self.image_fit_size: tuple[int, int] | None = None
        self.diff_edit_lines: list[int | None] = []
        self.markdown_view: object | None = None
        self.revision_scm: SCM | None = None
        self.revision_status: FileStatus | None = None
        self.get_style_context().add_class("file-preview")
        self.set_no_show_all(True)
        self.set_can_focus(True)
        self.connect("key-press-event", self._on_key_press)

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        header.get_style_context().add_class("preview-header")
        self.title = Gtk.Label()
        self.title.set_xalign(0)
        self.title.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        self.title.get_style_context().add_class("preview-title")
        close_button = Gtk.Button()
        close_button.set_relief(Gtk.ReliefStyle.NONE)
        close_button.set_image(
            Gtk.Image.new_from_icon_name("window-close-symbolic", Gtk.IconSize.BUTTON)
        )
        close_button.set_tooltip_text("Close preview (Esc)")
        close_button.get_accessible().set_name("Close preview")
        close_button.connect("clicked", self._on_close_clicked)
        header.pack_start(self.title, True, True, 0)
        self.markdown_mode_button = Gtk.Button(label="Rendered")
        self.markdown_mode_button.set_relief(Gtk.ReliefStyle.NONE)
        self.markdown_mode_button.set_tooltip_text(
            "Switch between the Markdown document and its revision diff"
        )
        self.markdown_mode_button.set_no_show_all(True)
        self.markdown_mode_button.connect("clicked", self._on_markdown_mode_clicked)
        header.pack_end(self.markdown_mode_button, False, False, 0)
        header.pack_end(close_button, False, False, 0)
        self.pack_start(header, False, False, 0)

        # 2026-08-17: mantenere stabile il GtkTextBTree evita che callback di
        # layout già accodati conservino righe appartenenti a un buffer sostituito.
        self.buffer = GtkSource.Buffer()
        self.diff_tags = {
            "added": self.buffer.create_tag("diff-added", foreground="#26a269"),
            "removed": self.buffer.create_tag("diff-removed", foreground="#e01b24"),
            "hunk": self.buffer.create_tag(
                "diff-hunk", foreground="#1c71d8", weight=Pango.Weight.BOLD
            ),
            "header": self.buffer.create_tag(
                "diff-header", weight=Pango.Weight.BOLD
            ),
            "note": self.buffer.create_tag("diff-note", style=Pango.Style.ITALIC),
        }
        self.view = GtkSource.View.new_with_buffer(self.buffer)
        self.view.set_editable(False)
        # 2026-09-02: la preview resta non modificabile, ma il cursore visibile
        # rende esplicito quale simbolo verrà usato da Ctrl+B e dal menu nativo.
        self.view.set_cursor_visible(True)
        self.view.set_monospace(True)
        self.view.set_show_line_numbers(True)
        self.view.set_highlight_current_line(False)
        self.view.set_tab_width(4)
        # 2026-09-27: il focus sul contenitore passivo impedisce ai binding
        # interni di GtkSourceView di trattenere tasti XF86 o macro non gestiti.
        self.view.set_can_focus(False)
        self.view.connect("populate-popup", self._on_populate_popup)
        self.view.connect("button-press-event", self._on_button_press)
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroller.add(self.view)
        self.text_scroller = scroller
        self.image = Gtk.Image()
        self.image.set_halign(Gtk.Align.CENTER)
        self.image.set_valign(Gtk.Align.CENTER)
        image_viewport = Gtk.Viewport()
        image_viewport.add(self.image)
        self.image_scroller = Gtk.ScrolledWindow()
        self.image_scroller.set_policy(
            Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC
        )
        self.image_scroller.add(image_viewport)
        self.image_scroller.connect("size-allocate", self._on_image_area_allocated)
        self.content_stack = Gtk.Stack()
        self.content_stack.set_transition_type(Gtk.StackTransitionType.NONE)
        self.content_stack.add_named(self.text_scroller, "text")
        self.content_stack.add_named(self.image_scroller, "image")
        self.pack_start(self.content_stack, True, True, 0)
        self.search_bar = TextSearchBar(self.buffer, self.view, self)
        self.pack_end(self.search_bar, False, False, 0)
        self._set_plain_text("Select a file to preview it.")

    def show_status(
        self, root: str, scm: SCM, status: FileStatus
    ) -> None:
        """Choose complete-file or diff presentation from normalized SCM state."""

        self.cancel()
        request_serial = self.request_serial
        self.current_path = status.path
        self.current_root = str(Path(root).resolve())
        self.current_file_panel_external = False
        self.revision_scm = scm
        self.revision_status = status
        title = (
            f"{status.source_path} → {status.path}"
            if status.state == "moved" and status.source_path
            else status.path
        )
        self.title.set_text(title)
        self.title.set_tooltip_text(title)
        markdown_diff = (
            Path(status.path).suffix.casefold() in {".md", ".markdown"}
            and status.state not in {"untracked", "added", "missing", "removed"}
        )
        self.markdown_mode_button.set_visible(markdown_diff)
        self.markdown_mode_button.set_label("Rendered")
        self._set_plain_text("Loading preview…")
        if status.state == "missing":
            self._set_plain_text("The file is missing from the working copy.")
            return
        if status.state != "removed" and self._is_image_path(status.path):
            self._load_working_file(root, status.path, request_serial)
            return
        if markdown_diff:
            # 2026-10-01: Revisioni serve prima di tutto a controllare le
            # modifiche; il documento Markdown renderizzato resta una modalità
            # esplicita, mentre Files continua a mostrarlo direttamente.
            self._load_revision_diff(scm, status, request_serial)
            return
        if status.state in {"untracked", "added"}:
            self._load_working_file(root, status.path, request_serial)
            return
        if status.state == "removed":
            self.command = run_async(
                scm.base_argv(status.path),
                partial(self._on_base_loaded, request_serial, status.path),
                cwd=scm.root,
                env=scm.environment,
            )
            return
        self._load_revision_diff(scm, status, request_serial)

    def show_file(self, root: str, relative_path: str) -> None:
        """Preview a normal project file independently from source-control state."""

        self.cancel()
        request_serial = self.request_serial
        self.current_path = relative_path
        self.current_root = str(Path(root).resolve())
        self.current_file_panel_external = True
        self.revision_scm = None
        self.revision_status = None
        self.markdown_mode_button.hide()
        self.title.set_text(relative_path)
        self.title.set_tooltip_text(relative_path)
        self._set_plain_text("Loading preview…")
        # 2026-08-16: la vista File usa lo stesso lettore sicuro e gli stessi
        # limiti della preview SCM, senza creare un secondo renderer di sorgenti.
        self._load_working_file(root, relative_path, request_serial)

    def _load_revision_diff(
        self, scm: SCM, status: FileStatus, request_serial: int
    ) -> None:
        """Start the normal or rename-aware diff for one revision row."""

        argv = (
            scm.preview_move_diff_argv(status.source_path, status.path)
            if status.state == "moved" and status.source_path
            else scm.preview_diff_argv(status.path)
        )
        self.command = run_async(
            argv,
            partial(self._on_diff_loaded, request_serial, status.path),
            cwd=scm.root,
            env=scm.environment,
        )

    def _on_markdown_mode_clicked(self, _button: Gtk.Button) -> None:
        """Toggle a tracked Markdown revision between diff and rendered document."""

        scm = self.revision_scm
        status = self.revision_status
        root = self.current_root
        if scm is None or status is None or root is None:
            return
        show_rendered = self.markdown_mode_button.get_label() == "Rendered"
        self.cancel()
        request_serial = self.request_serial
        self._set_plain_text("Loading preview…")
        if show_rendered:
            self.markdown_mode_button.set_label("Diff")
            self._load_working_file(root, status.path, request_serial)
        else:
            self.markdown_mode_button.set_label("Rendered")
            self._load_revision_diff(scm, status, request_serial)

    def cancel(self) -> None:
        """Cancel asynchronous work belonging to a superseded or closed preview."""

        # 2026-08-16: il seriale invalida anche callback già accodati nel main
        # loop, che una cancellazione Gio da sola non può ritirare.
        self.request_serial += 1
        if self.command is not None:
            self.command.cancel()
            self.command = None
        if self.file_cancellable is not None:
            self.file_cancellable.cancel()
            self.file_cancellable = None
        if self.markdown_view is not None:
            self.markdown_view.stop_loading()
        self.image_source_pixbuf = None
        self.image_fit_size = None
        self.image.clear()

    def _load_working_file(
        self, root: str, relative_path: str, request_serial: int
    ) -> None:
        """Read one current file, allowing external links only from Files."""

        root_path = Path(root).resolve()
        if (
            not relative_path
            or Path(relative_path).is_absolute()
            or ".." in Path(relative_path).parts
        ):
            self._set_plain_text("Preview rejected: invalid project path.")
            return
        candidate = (root_path / relative_path).resolve()
        try:
            candidate.relative_to(root_path)
        except ValueError:
            if not self.current_file_panel_external:
                self._set_plain_text("Preview rejected: the path leaves the project.")
                return
        try:
            size = candidate.stat().st_size
        except OSError as error:
            self._set_plain_text(f"Unable to read the file:\n{error}")
            return
        image_path = self._is_image_path(relative_path)
        maximum = self.MAX_IMAGE_BYTES if image_path else self.MAX_TEXT_BYTES
        if size > maximum:
            self._set_plain_text(
                f"File too large to preview ({size / 1024 / 1024:.1f} MiB)."
            )
            return
        self.file_cancellable = Gio.Cancellable()
        if image_path:
            GdkPixbuf.Pixbuf.get_file_info_async(
                str(candidate),
                self.file_cancellable,
                self._on_image_info_loaded,
                (request_serial, relative_path, str(candidate), size),
            )
            return
        Gio.File.new_for_path(str(candidate)).load_contents_async(
            self.file_cancellable,
            self._on_file_loaded,
            (request_serial, relative_path),
        )

    def _on_file_loaded(
        self,
        source: Gio.File,
        result: Gio.AsyncResult,
        request: tuple[int, str],
    ) -> None:
        """Decode and syntax-highlight a completed untracked-file read."""

        request_serial, relative_path = request
        if request_serial != self.request_serial or relative_path != self.current_path:
            return
        self.file_cancellable = None
        try:
            _success, contents, _etag = source.load_contents_finish(result)
        except GLib.Error as error:
            if not error.matches(Gio.io_error_quark(), Gio.IOErrorEnum.CANCELLED):
                self._set_plain_text(f"Unable to read the file:\n{error}")
            return
        data = bytes(contents)
        if len(data) > self.MAX_TEXT_BYTES:
            self._set_plain_text(
                f"File too large to preview ({len(data) / 1024 / 1024:.1f} MiB)."
            )
            return
        if b"\0" in data:
            self._set_plain_text("Binary file: text preview is unavailable.")
            return
        text = data.decode("utf-8", "replace")
        if Path(relative_path).suffix.casefold() in {".md", ".markdown"}:
            self._set_markdown_text(text, relative_path)
        else:
            self._set_source_text(text, relative_path)

    def _ensure_markdown_view(self) -> None:
        """Create the isolated Markdown WebView only for the first rendered file."""

        if self.markdown_view is not None:
            return
        if WebKit2 is None:
            raise RuntimeError("WebKit2 is unavailable")
        context = WebKit2.WebContext.new_ephemeral()
        view = WebKit2.WebView.new_with_context(context)
        settings = view.get_settings()
        settings.set_enable_javascript(False)
        settings.set_enable_developer_extras(False)
        view.connect("decide-policy", self._on_markdown_policy)
        view.connect("key-press-event", self._on_key_press)
        self.content_stack.add_named(view, "markdown")
        self.markdown_view = view
        view.show()

    def _set_markdown_text(self, text: str, path: str) -> None:
        """Render safe GitHub-flavored Markdown in the isolated preview WebView."""

        if github_flavored_markdown_to_html is None:
            self._set_plain_text("Markdown preview requires python3-cmarkgfm.")
            return
        self.diff_edit_lines = []
        self.image_source_pixbuf = None
        self.image_fit_size = None
        self.image.clear()
        rendered = github_flavored_markdown_to_html(text)
        # 2026-10-03: le immagini relative sono utili nella documentazione, ma
        # vengono risolte e convalidate singolarmente per non concedere alla
        # WebView accesso a URL remoti o file esterni alla root del progetto.
        rendered = re.sub(
            r'<img\b(?P<prefix>[^>]*?\bsrc=")(?P<src>[^"]*)(?P<suffix>"[^>]*)>',
            partial(self._sanitize_markdown_image, path),
            rendered,
            flags=re.IGNORECASE,
        )
        # 2026-09-27: cmark-gfm produce correttamente gli input disabilitati ma
        # non aggiunge le classi CSS usate da GitHub; marcare le relative righe
        # elimina il doppio indicatore pallino/checkbox senza rendere editabile
        # una preview che non deve modificare il documento sorgente.
        rendered = re.sub(
            r"<li>(\s*<input\s+type=\"checkbox\")",
            r'<li class="task-list-item">\1',
            rendered,
            flags=re.IGNORECASE,
        )
        rendered = re.sub(
            r"<ul>(\s*<li class=\"task-list-item\">)",
            r'<ul class="task-list">\1',
            rendered,
            flags=re.IGNORECASE,
        )
        document = (
            "<!doctype html><html><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width\">"
            "<style>"
            "body{max-width:980px;margin:0 auto;padding:28px 36px;"
            "font:16px/1.55 sans-serif;color:#202124;background:#fff}"
            "h1,h2{border-bottom:1px solid #d8dee4;padding-bottom:.3em}"
            "pre,code{font-family:monospace;background:#f3f4f6}"
            "code{padding:.15em .3em;border-radius:3px}"
            "pre{padding:14px;overflow:auto;border-radius:5px}"
            "pre code{padding:0;background:transparent}"
            "body>ul,body>ol{padding-left:1.4em}li{margin-bottom:.5rem}"
            "table{border-collapse:collapse}th,td{border:1px solid #d0d7de;"
            "padding:6px 12px}blockquote{margin-left:0;padding-left:16px;"
            "border-left:4px solid #d0d7de;color:#57606a}"
            ".task-list{padding-left:0}.task-list-item{list-style:none;position:relative;"
            "padding-left:1.45em}.task-list-item input{position:absolute;left:0;top:.4em;"
            "margin:0;accent-color:#1f883d}"
            "img{display:block;max-width:100%;height:auto}"
            "a{color:#0969da}.blocked-image{color:#6e7781;font-style:italic}"
            "</style><title>"
            + html.escape(path)
            + "</title></head><body>"
            + rendered
            + "</body></html>"
        )
        self._ensure_markdown_view()
        self.content_stack.set_visible_child_name("markdown")
        base_uri = "about:blank"
        if self.current_root is not None:
            base_uri = Path(self.current_root).resolve().as_uri() + "/"
        self.markdown_view.load_html(document, base_uri)

    def _sanitize_markdown_image(self, path: str, match: re.Match[str]) -> str:
        """Allow one local Markdown image only when it remains inside the project."""

        blocked = '<span class="blocked-image">[Image omitted]</span>'
        if self.current_root is None:
            return blocked
        source = html.unescape(match.group("src"))
        parsed = urlsplit(source)
        if parsed.scheme or parsed.netloc or not parsed.path:
            return blocked
        if parsed.path.startswith("/") or "\0" in parsed.path:
            return blocked
        root = Path(self.current_root).resolve()
        image_path = (root / Path(path).parent / unquote(parsed.path)).resolve()
        try:
            image_path.relative_to(root)
        except ValueError:
            if not self.current_file_panel_external:
                return blocked
        if not image_path.is_file():
            return blocked
        source_uri = html.escape(image_path.as_uri(), quote=True)
        return (
            "<img"
            + match.group("prefix")
            + source_uri
            + match.group("suffix")
            + ">"
        )

    def _on_markdown_policy(
        self, _view: object, decision: object, decision_type: object
    ) -> bool:
        """Keep every Markdown navigation inside the inert generated document."""

        if WebKit2 is None or decision_type != WebKit2.PolicyDecisionType.NAVIGATION_ACTION:
            return False
        request = decision.get_request()
        uri = request.get_uri() if request is not None else ""
        if uri == "about:blank":
            return False
        navigation_action = decision.get_navigation_action()
        navigation_type = navigation_action.get_navigation_type()
        # 2026-10-03: load_html usa il base URI file come URI del documento;
        # consentire soltanto quella navigazione tecnica di tipo OTHER evita
        # la pagina bianca senza rendere visitabili i link contenuti nel Markdown.
        if (
            navigation_type == WebKit2.NavigationType.OTHER
            and self.current_root is not None
            and uri == Path(self.current_root).resolve().as_uri() + "/"
        ):
            return False
        decision.ignore()
        return True

    def _on_image_info_loaded(
        self,
        _source: object,
        result: Gio.AsyncResult,
        request: tuple[int, str, str, int],
    ) -> None:
        """Read image dimensions and show its geometry and disk size."""

        request_serial, relative_path, path, file_size = request
        if request_serial != self.request_serial or relative_path != self.current_path:
            return
        try:
            image_format, width, height = GdkPixbuf.Pixbuf.get_file_info_finish(result)
        except GLib.Error as error:
            self.file_cancellable = None
            if not error.matches(Gio.io_error_quark(), Gio.IOErrorEnum.CANCELLED):
                self._set_plain_text(f"Unable to inspect the image:\n{error}")
            return
        if image_format is None or width <= 0 or height <= 0:
            self.file_cancellable = None
            self._set_plain_text("Unsupported or invalid image format.")
            return
        if width * height > self.MAX_IMAGE_PIXELS:
            self.file_cancellable = None
            self._set_plain_text(
                f"Image dimensions are too large to preview ({width}×{height})."
            )
            return
        # 2026-09-15: le dimensioni già ottenute dal controllo preliminare
        # completano il titolo senza aggiungere letture o decodifiche dell'immagine.
        title = (
            f"{self.title.get_text()} — {width}×{height} px — "
            f"{GLib.format_size(file_size)}"
        )
        self.title.set_text(title)
        self.title.set_tooltip_text(title)
        Gio.File.new_for_path(path).read_async(
            GLib.PRIORITY_DEFAULT,
            self.file_cancellable,
            self._on_image_stream_opened,
            (request_serial, relative_path),
        )

    def _on_image_stream_opened(
        self,
        source: Gio.File,
        result: Gio.AsyncResult,
        request: tuple[int, str],
    ) -> None:
        """Begin a bounded asynchronous pixbuf decode for the current image."""

        request_serial, relative_path = request
        if request_serial != self.request_serial or relative_path != self.current_path:
            return
        try:
            stream = source.read_finish(result)
        except GLib.Error as error:
            self.file_cancellable = None
            if not error.matches(Gio.io_error_quark(), Gio.IOErrorEnum.CANCELLED):
                self._set_plain_text(f"Unable to read the image:\n{error}")
            return
        GdkPixbuf.Pixbuf.new_from_stream_async(
            stream,
            self.file_cancellable,
            self._on_image_loaded,
            request,
        )

    def _on_image_loaded(
        self,
        _source: object,
        result: Gio.AsyncResult,
        request: tuple[int, str],
    ) -> None:
        """Display a decoded image only while its preview request remains current."""

        request_serial, relative_path = request
        if request_serial != self.request_serial or relative_path != self.current_path:
            return
        self.file_cancellable = None
        try:
            pixbuf = GdkPixbuf.Pixbuf.new_from_stream_finish(result)
        except GLib.Error as error:
            if not error.matches(Gio.io_error_quark(), Gio.IOErrorEnum.CANCELLED):
                self._set_plain_text(f"Unable to decode the image:\n{error}")
            return
        if pixbuf is None:
            self._set_plain_text("Unable to decode the image.")
            return
        self.image_source_pixbuf = pixbuf
        self.image_fit_size = None
        self.content_stack.set_visible_child_name("image")
        allocation = self.image_scroller.get_allocation()
        self._fit_image(allocation.width, allocation.height)

    def _on_image_area_allocated(
        self, _scroller: Gtk.ScrolledWindow, allocation: object
    ) -> None:
        """Refit the current image whenever the visible preview area changes."""

        self._fit_image(allocation.width, allocation.height)

    def _fit_image(self, available_width: int, available_height: int) -> None:
        """Scale the decoded image to fit the viewport without enlarging it."""

        source = self.image_source_pixbuf
        if source is None or available_width <= 1 or available_height <= 1:
            return
        # 2026-09-02: il rendering segue l'allocazione GTK reale invece di un
        # limite arbitrario; sottrarre il bordo evita scrollbar per pochi pixel.
        width = max(1, available_width - 4)
        height = max(1, available_height - 4)
        scale = min(1.0, width / source.get_width(), height / source.get_height())
        target = (
            max(1, round(source.get_width() * scale)),
            max(1, round(source.get_height() * scale)),
        )
        if target == self.image_fit_size:
            return
        self.image_fit_size = target
        if target == (source.get_width(), source.get_height()):
            displayed = source
        else:
            displayed = source.scale_simple(
                target[0], target[1], GdkPixbuf.InterpType.BILINEAR
            )
        self.image.set_from_pixbuf(displayed)

    def _on_base_loaded(
        self, request_serial: int, relative_path: str, result: CommandResult
    ) -> None:
        """Render the complete parent-revision content of a removed file."""

        if request_serial != self.request_serial or relative_path != self.current_path:
            return
        self.command = None
        if not result.ok:
            self._set_plain_text(
                result.stderr.strip() or "Unable to read the base revision."
            )
            return
        encoded = result.stdout.encode("utf-8", "replace")
        if len(encoded) > self.MAX_TEXT_BYTES:
            self._set_plain_text("Base file too large for the internal preview.")
            return
        if "\0" in result.stdout:
            self._set_plain_text("Binary file: text preview is unavailable.")
            return
        if Path(relative_path).suffix.casefold() in {".md", ".markdown"}:
            self._set_markdown_text(result.stdout, relative_path)
        else:
            self._set_source_text(result.stdout, relative_path)

    def _on_diff_loaded(
        self, request_serial: int, relative_path: str, result: CommandResult
    ) -> None:
        """Render a completed SCM diff only if it is still current."""

        if request_serial != self.request_serial or relative_path != self.current_path:
            return
        self.command = None
        if not result.ok:
            self._set_plain_text(
                result.stderr.strip() or "Unable to generate the diff."
            )
            return
        if len(result.stdout.encode("utf-8", "replace")) > self.MAX_TEXT_BYTES:
            self._set_plain_text("Diff too large for the internal preview.")
            return
        self._set_diff_text(result.stdout or "No textual differences available.\n")

    def _set_source_text(self, text: str, path: str) -> None:
        """Display an entire text file with language-aware highlighting."""

        self.diff_edit_lines = []
        self.content_stack.set_visible_child_name("text")
        self.image_source_pixbuf = None
        self.image_fit_size = None
        self.image.clear()
        language = GtkSource.LanguageManager.get_default().guess_language(path, None)
        self.buffer.set_language(language)
        self.buffer.set_highlight_syntax(language is not None)
        scheme_name = self._preferred_scheme_name()
        scheme = GtkSource.StyleSchemeManager.get_default().get_scheme(scheme_name)
        if scheme is not None:
            self.buffer.set_style_scheme(scheme)
        self.buffer.set_text(text)

    def _set_diff_text(self, text: str) -> None:
        """Display a unified diff with semantic colors based on line prefixes."""

        self.content_stack.set_visible_child_name("text")
        self.image_source_pixbuf = None
        self.image_fit_size = None
        self.image.clear()
        self.buffer.set_language(None)
        self.buffer.set_highlight_syntax(False)
        self.buffer.set_text(text)
        self.diff_edit_lines = list(map_diff_lines(text))
        offset = 0
        for line in text.splitlines(keepends=True):
            # 2026-08-17: impostare il testo una sola volta riduce le
            # invalidazioni del layout; gli offset Unicode delimitano poi le righe.
            start = self.buffer.get_iter_at_offset(offset)
            offset += len(line)
            end = self.buffer.get_iter_at_offset(offset)
            tag = None
            if line.startswith("@@"):
                tag = self.diff_tags["hunk"]
            elif line.startswith(("diff ", "--- ", "+++ ")):
                tag = self.diff_tags["header"]
            elif line.startswith("+"):
                tag = self.diff_tags["added"]
            elif line.startswith("-"):
                tag = self.diff_tags["removed"]
            elif line.startswith("\\ No newline"):
                tag = self.diff_tags["note"]
            if tag is not None:
                self.buffer.apply_tag(tag, start, end)

    def _set_plain_text(self, text: str) -> None:
        """Display an informational or error message without syntax processing."""

        self.diff_edit_lines = []
        self.content_stack.set_visible_child_name("text")
        self.image_source_pixbuf = None
        self.image_fit_size = None
        self.image.clear()
        self.buffer.set_language(None)
        self.buffer.set_highlight_syntax(False)
        self.buffer.set_text(text)

    @staticmethod
    def _is_image_path(path: str) -> bool:
        """Return whether Gio identifies the path as an image MIME type."""

        content_type, _uncertain = Gio.content_type_guess(path, None)
        return bool(content_type and content_type.startswith("image/"))

    @staticmethod
    def _preferred_scheme_name() -> str:
        """Choose a bundled GtkSource scheme that follows light/dark GTK themes."""

        settings = Gtk.Settings.get_default()
        theme_name = str(settings.get_property("gtk-theme-name") if settings else "")
        return "oblivion" if "dark" in theme_name.lower() else "classic"

    def _on_close_clicked(self, _button: Gtk.Button) -> None:
        """Forward an explicit close action to the overlay owner."""

        self.on_close()

    def go_to_definition(self) -> bool:
        """Request a project lookup for the symbol targeted in this preview."""

        if self.current_root is None or self.current_path is None:
            return False
        symbol = source_symbol_at_cursor(self.buffer)
        if symbol is None:
            return False
        if self.on_definition is None:
            return False
        self.on_definition(self.current_root, self.current_path, symbol)
        return True

    def _on_populate_popup(self, _view: Gtk.TextView, menu: Gtk.Menu) -> None:
        """Append source and file actions to the preview's context menu."""

        # 2026-09-27: il menu usa soltanto il puntatore; evitare il keyboard
        # grab lascia disponibili shortcut desktop e tasti XF86 mentre è aperto.
        menu.set_take_focus(False)
        separator = Gtk.SeparatorMenuItem()
        menu.append(separator)
        definition_item = Gtk.MenuItem(label="Go to Definition")
        definition_item.set_sensitive(source_symbol_at_cursor(self.buffer) is not None)
        definition_item.connect("activate", self._on_definition_activate)
        menu.append(definition_item)
        find_item = Gtk.MenuItem(label="Find")
        find_item.connect("activate", self._on_find_activate)
        menu.append(find_item)
        edit_item = None
        if self.diff_edit_lines:
            edit_item = Gtk.MenuItem(label="Edit this change")
            edit_item.set_sensitive(self.edit_line_at_cursor() is not None)
            edit_item.connect("activate", self._on_edit_change_activate)
            menu.append(edit_item)
            diff_editor_item = Gtk.MenuItem(label="Edit with diff…")
            diff_editor_item.connect("activate", self._on_edit_with_diff_activate)
            menu.append(diff_editor_item)
        else:
            diff_editor_item = None
        delete_item = Gtk.MenuItem(label="Delete")
        delete_item.set_sensitive(
            self.current_path is not None and self.on_action is not None
        )
        delete_item.connect("activate", self._on_delete_activate)
        menu.append(delete_item)
        separator.show()
        definition_item.show()
        find_item.show()
        if edit_item is not None:
            edit_item.show()
        if diff_editor_item is not None:
            diff_editor_item.show()
        delete_item.show()

    def edit_line_at_cursor(self) -> int | None:
        """Return the working-file line represented by the current diff row."""

        line_index = self.buffer.get_iter_at_mark(self.buffer.get_insert()).get_line()
        if line_index >= len(self.diff_edit_lines):
            return None
        return self.diff_edit_lines[line_index]

    def current_diff_line(self) -> int:
        """Return the zero-based diff row containing the insertion cursor."""

        return self.buffer.get_iter_at_mark(self.buffer.get_insert()).get_line()

    def diff_text(self) -> str:
        """Return the unified diff currently rendered by the preview."""

        return self.buffer.get_text(
            self.buffer.get_start_iter(), self.buffer.get_end_iter(), True
        )

    def _on_edit_change_activate(self, _item: Gtk.MenuItem) -> None:
        """Open the current mappable diff row in the persistent editor."""

        if self.edit_line_at_cursor() is not None and self.on_action is not None:
            self.on_action("edit-internal")

    def _on_edit_with_diff_activate(self, _item: Gtk.MenuItem) -> None:
        """Open the current diff in the persistent editor companion dialog."""

        if self.on_action is not None:
            self.on_action("edit-with-diff")

    def _on_find_activate(self, _item: Gtk.MenuItem) -> None:
        """Open local search from the preview context menu."""

        self.search_bar.show_search()

    def _on_button_press(
        self, view: Gtk.TextView, event: Gdk.EventButton
    ) -> bool:
        """Focus the passive preview and open a double-clicked mappable diff row."""

        self.grab_focus()
        if event.button != 1 or event.type != Gdk.EventType.DOUBLE_BUTTON_PRESS:
            return False
        window_type = view.get_window_type(event.window)
        x, y = view.window_to_buffer_coords(window_type, int(event.x), int(event.y))
        _inside_text, text_iter = view.get_iter_at_location(x, y)
        self.buffer.place_cursor(text_iter)
        if self.edit_line_at_cursor() is None or self.on_action is None:
            return False
        self.on_action("edit-internal")
        return True

    def _on_definition_activate(self, _item: Gtk.MenuItem) -> None:
        """Run contextual Go to Definition for the preview cursor."""

        self.go_to_definition()

    def _on_delete_activate(self, _item: Gtk.MenuItem) -> None:
        """Forward deletion of the exact file currently shown in preview."""

        if self.on_action is not None and self.current_path is not None:
            self.on_action("delete")

    def _on_key_press(self, _view: Gtk.TextView, event: Gdk.EventKey) -> bool:
        """Handle preview copy/search and forward unmodified file actions."""

        if (
            self.content_stack.get_visible_child_name() != "markdown"
            and self.search_bar.handle_key(event)
        ):
            return True
        modifiers = event.state & Gtk.accelerator_get_default_mod_mask()
        if (
            self.content_stack.get_visible_child_name() != "markdown"
            and modifiers == Gdk.ModifierType.CONTROL_MASK
            and Gdk.keyval_to_lower(event.keyval) == Gdk.KEY_c
        ):
            bounds = self.buffer.get_selection_bounds()
            if bounds:
                # Il GtkSourceView è intenzionalmente passivo per lasciare al
                # sistema i tasti speciali; la copia va quindi eseguita dal
                # contenitore che possiede realmente il focus.
                selected = self.buffer.get_text(bounds[0], bounds[1], True)
                Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text(selected, -1)
                return True
            return False
        if modifiers != Gdk.ModifierType(0):
            return False
        keyval = Gdk.keyval_to_lower(event.keyval)
        actions = {
            Gdk.KEY_v: "view",
            Gdk.KEY_e: "edit-internal",
            Gdk.KEY_m: "edit-external",
            Gdk.KEY_d: "meld",
        }
        action = actions.get(keyval)
        if action is None or self.on_action is None or self.current_path is None:
            return False
        self.on_action(action)
        return True
