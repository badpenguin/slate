"""Read-only overlay previews for SCM diffs and complete source files."""

from __future__ import annotations

from functools import partial
from pathlib import Path
from typing import Callable

import gi

gi.require_version("Gio", "2.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("Gtk", "3.0")
gi.require_version("GtkSource", "4")
from gi.repository import Gdk, GdkPixbuf, Gio, GLib, Gtk, GtkSource, Pango  # noqa: E402

from .processes import AsyncCommand, CommandResult, run_async
from .search import source_symbol_at_cursor
from .scm.base import FileStatus, SCM


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
        self.command: AsyncCommand | None = None
        self.file_cancellable: Gio.Cancellable | None = None
        self.request_serial = 0
        self.image_source_pixbuf: GdkPixbuf.Pixbuf | None = None
        self.image_fit_size: tuple[int, int] | None = None
        self.get_style_context().add_class("file-preview")
        self.set_no_show_all(True)

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
        self.view.connect("populate-popup", self._on_populate_popup)
        # 2026-09-08: il focus nella preview non deve disattivare le azioni
        # pubblicizzate dagli alberi Revisioni e Files.
        self.view.connect("key-press-event", self._on_key_press)
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
        self._set_plain_text("Select a file to preview it.")

    def show_status(
        self, root: str, scm: SCM, status: FileStatus
    ) -> None:
        """Choose complete-file or diff presentation from normalized SCM state."""

        self.cancel()
        request_serial = self.request_serial
        self.current_path = status.path
        self.current_root = str(Path(root).resolve())
        title = (
            f"{status.source_path} → {status.path}"
            if status.state == "moved" and status.source_path
            else status.path
        )
        self.title.set_text(title)
        self.title.set_tooltip_text(title)
        self._set_plain_text("Loading preview…")
        if status.state != "removed" and self._is_image_path(status.path):
            # 2026-09-02: i diff binari non aiutano a valutare una modifica;
            # Revisioni mostra quindi l'immagine corrente con lo stesso loader
            # confinato e limitato usato dalla vista File.
            self._load_working_file(root, status.path, request_serial)
            return
        if status.state == "moved" and status.source_path:
            # 2026-08-17: rename-aware output is required because a normal patch
            # can render a move as unrelated add/remove blocks.
            self.command = run_async(
                scm.preview_move_diff_argv(status.source_path, status.path),
                partial(self._on_diff_loaded, request_serial, status.path),
                cwd=scm.root,
                env=scm.environment,
            )
            return
        if status.state in {"untracked", "added"}:
            self._load_working_file(root, status.path, request_serial)
            return
        if status.state in {"missing", "removed"}:
            self.command = run_async(
                scm.base_argv(status.path),
                partial(self._on_base_loaded, request_serial, status.path),
                cwd=scm.root,
                env=scm.environment,
            )
            return
        self.command = run_async(
            scm.preview_diff_argv(status.path),
            partial(self._on_diff_loaded, request_serial, status.path),
            cwd=scm.root,
            env=scm.environment,
        )

    def show_file(self, root: str, relative_path: str) -> None:
        """Preview a normal project file independently from source-control state."""

        self.cancel()
        request_serial = self.request_serial
        self.current_path = relative_path
        self.current_root = str(Path(root).resolve())
        self.title.set_text(relative_path)
        self.title.set_tooltip_text(relative_path)
        self._set_plain_text("Loading preview…")
        # 2026-08-16: la vista File usa lo stesso lettore sicuro e gli stessi
        # limiti della preview SCM, senza creare un secondo renderer di sorgenti.
        self._load_working_file(root, relative_path, request_serial)

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
        self.image_source_pixbuf = None
        self.image_fit_size = None
        self.image.clear()

    def _load_working_file(
        self, root: str, relative_path: str, request_serial: int
    ) -> None:
        """Read one safe current working-copy path asynchronously through Gio."""

        root_path = Path(root).resolve()
        candidate = (root_path / relative_path).resolve()
        try:
            candidate.relative_to(root_path)
        except ValueError:
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
                (request_serial, relative_path, str(candidate)),
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
        self._set_source_text(data.decode("utf-8", "replace"), relative_path)

    def _on_image_info_loaded(
        self,
        _source: object,
        result: Gio.AsyncResult,
        request: tuple[int, str, str],
    ) -> None:
        """Read image dimensions asynchronously and choose a non-enlarging size."""

        request_serial, relative_path, path = request
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
        """Append Go to Definition to the preview's existing context menu."""

        separator = Gtk.SeparatorMenuItem()
        menu.append(separator)
        item = Gtk.MenuItem(label="Go to Definition")
        item.set_sensitive(source_symbol_at_cursor(self.buffer) is not None)
        item.connect("activate", self._on_definition_activate)
        menu.append(item)
        separator.show()
        item.show()

    def _on_definition_activate(self, _item: Gtk.MenuItem) -> None:
        """Run contextual Go to Definition for the preview cursor."""

        self.go_to_definition()

    def _on_key_press(self, _view: Gtk.TextView, event: Gdk.EventKey) -> bool:
        """Forward unmodified file-action shortcuts from the focused preview."""

        if event.state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.MOD1_MASK):
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
