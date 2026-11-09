"""Reversible experimental views over an existing canonical EditorDocument."""

from bisect import bisect_left, bisect_right
from dataclasses import dataclass
import json
from pathlib import Path
import sys

import gi

gi.require_version("GtkSource", "4")
from gi.repository import Gdk, GLib, GObject, Gtk, GtkSource

from .browser import WebKit2
from .editor_diff_model import DiffRow, compact_rows
from .editor_render import DocumentRenderer
from .editor_syntax import DiffSyntaxHighlighter
from .processes import run_async
from .text_search import TextSearchBar


@dataclass
class ProjectionSpan:
    """Keep matching canonical and visual marks for one editable projection span."""

    kind: str
    text: str
    start: object = None
    end: object = None
    visual_start: object = None
    visual_end: object = None
    old_line: int | None = None
    gap: tuple[int, int] | None = None


class ExperimentalEditor(Gtk.Box):
    """Offer code, editable unified/side-by-side diffs and isolated rendering."""

    RENDERABLE = {".md", ".markdown", ".html", ".htm"}

    def __init__(self, editor) -> None:
        """Attach derived views while leaving loading, saving and undo in the editor."""

        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.editor = editor
        self.enabled = False
        self.closed = False
        # 2026-10-30: la modalità iniziale è già il diff richiesto di default.
        # Non selezionare Editor mentre si attende la lettura asincrona della base.
        self.mode = "code" if self._is_minified() else "compact"
        self.automatic = True
        self.initial_chosen = self._is_minified()
        self.base = None
        self.base_identity = None
        self.rows = []
        self.spans = []
        self.syncing = False
        self.source_action_active = False
        self.composing = False
        self.serial = 0
        self.job_serial = 0
        self.base_serial = 0
        self.debounce = None
        self.command = None
        self.base_command = None
        self.timeout = None
        self.base_timeout = None
        self.renderer = None
        self.rendered_document = None
        self.render_search_box = None
        self.hunks = []
        self.rows_serial = -1
        self.rows_base_serial = -1
        self.projection_errors = {}
        self.expanded_gaps = set()
        self.expanded_gap_marks = []
        self.pending_navigation = None
        self.positioned_modes = set()
        self.first_scroll_id = None
        self.scroll_syncing = False
        self.last_side = "current"
        self.controls = Gtk.Box(spacing=4)
        # 2026-10-30: expose all modes directly without a dropdown; active
        # state and tooltips keep the compact icon controls understandable.
        self.mode_buttons = {}
        modes = Gtk.Box(spacing=0)
        modes.get_style_context().add_class("linked")
        for identifier, title, icon in (
            ("compact", "Compact", "view-filter-symbolic"),
            ("unified", "Unified", "view-list-symbolic"),
            ("side", "Side by side", "view-dual-symbolic"),
            ("code", "Editor", "text-x-generic-symbolic"),
            ("rendered", "Renderer", "web-browser-symbolic"),
        ):
            button = Gtk.ToggleButton()
            # Icona e testo restano distanziati di 4 px anche con temi diversi.
            content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
            content.pack_start(Gtk.Image.new_from_icon_name(icon, Gtk.IconSize.BUTTON), False, False, 0)
            content.pack_start(Gtk.Label(label=title), False, False, 0)
            button.add(content)
            button.set_tooltip_text(title)
            button.get_accessible().set_name(title)
            button.connect("clicked", self._mode_clicked, identifier)
            self.mode_buttons[identifier] = button
            modes.pack_start(button, False, False, 0)
        self.controls.pack_start(modes, False, False, 0)
        self._sync_mode_buttons()
        for icon, tooltip, callback in (("go-up", "Previous change", self._previous),
                                         ("go-down", "Next change", self._next)):
            self.controls.pack_start(editor._tool_button(icon, tooltip, callback), False, False, 0)
        self.controls.set_no_show_all(True)
        editor.toolbar.pack_start(self.controls, False, False, 0)
        editor.toolbar.reorder_child(self.controls, 0)
        self.notice = Gtk.Label(xalign=0)
        self.notice.set_selectable(True)
        self.notice.set_line_wrap(True)
        self.notice.set_no_show_all(True)
        self.pack_start(self.notice, False, False, 0)
        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.NONE)
        self.pack_start(self.stack, True, True, 0)
        # 2026-10-30: durante il primo caricamento non mostrare il sorgente
        # completo per poi sostituirlo con il diff, causando un salto visivo.
        self.loading_indicator = Gtk.Spinner()
        self.loading_indicator.set_halign(Gtk.Align.CENTER)
        self.loading_indicator.set_valign(Gtk.Align.CENTER)
        self.stack.add_named(self.loading_indicator, "loading")
        self.split = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        self.old_buffer = GtkSource.Buffer()
        self.old_view, self.old_scroller = self._source(self.old_buffer, False)
        self.old_view.set_no_show_all(True)
        self.old_search = TextSearchBar(self.old_buffer, self.old_view)
        self.old_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.old_box.pack_start(self.old_scroller, True, True, 0)
        self.old_box.pack_start(self.old_search, False, False, 0)
        self.split.pack1(self.old_box, True, False)
        self.old_view.connect("focus-in-event", self._side_focus)
        editor.view.connect("focus-in-event", self._side_focus)
        self.stack.add_named(self.split, "source")
        self.projection = GtkSource.Buffer()
        self.projection.set_max_undo_levels(0)
        self.projection_view, projection_scroller = self._source(self.projection, True)
        self.projection_view.set_auto_indent(True)
        self.projection_view.set_tab_width(4)
        self.projection_view.connect("preedit-changed", self._preedit)
        self.projection_view.connect("button-press-event", self._projection_button)
        self.projection_view.connect("cut-clipboard", self._cut)
        self.projection_view.connect("populate-popup", self._projection_menu)
        self.projection.connect("begin-user-action", self._begin_action)
        self.projection.connect("end-user-action", self._end_action)
        self.projection.connect("insert-text", self._insert)
        self.projection.connect("delete-range", self._delete)
        self.projection.connect_after("insert-text", self._after_projection_insert)
        self.projection_search = TextSearchBar(self.projection, self.projection_view)
        unified_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        unified_box.pack_start(projection_scroller, True, True, 0)
        unified_box.pack_start(self.projection_search, False, False, 0)
        self.stack.add_named(unified_box, "unified")
        self.tags = {}
        for name, color in (("added", "#a8eba8"), ("removed", "#f5a3ab")):
            self.tags[name] = self.projection.create_tag(name, background=color, foreground="#172017",
                                                        editable=name != "removed")
        self.tags["omitted"] = self.projection.create_tag("omitted", foreground="#777777", editable=False)
        self.new_tag = editor.buffer.create_tag(None, background="#a8eba8")
        self.old_tag = self.old_buffer.create_tag(None, background="#f5a3ab")
        self.syntax = DiffSyntaxHighlighter(self)
        editor.buffer.connect("changed", self._document_changed)
        editor.buffer.connect("modified-changed", self._document_ready)
        editor.buffer.connect_after("undo", self._history_restored)
        editor.buffer.connect_after("redo", self._history_restored)
        editor.view.connect("map", self._view_mapped)
        editor.connect("unmap", self._unmapped)
        editor.connect("map", self._mapped)
        editor.source_scroller.get_vadjustment().connect("value-changed", self._scroll_current)
        self.old_scroller.get_vadjustment().connect("value-changed", self._scroll_old)

    def _source(self, buffer, editable):
        """Construct one derived text surface without sharing canonical view layouts."""

        view = GtkSource.View.new_with_buffer(buffer)
        view.set_editable(editable)
        view.set_monospace(True)
        view.set_show_line_numbers(True)
        view.set_wrap_mode(Gtk.WrapMode.NONE)
        view.get_style_context().add_provider(self.editor.font_provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        view.get_style_context().add_provider(self.editor.appearance_provider, Gtk.STYLE_PROVIDER_PRIORITY_USER)
        scroller = Gtk.ScrolledWindow()
        scroller.add(view)
        return view, scroller

    def attach(self) -> None:
        """Install the unified presentations over the existing canonical source view."""

        if self.enabled:
            return
        self.enabled = True
        self.pending_selection = self._document_selection()
        source = self.editor.source_scroller
        self.editor.remove(source)
        self.split.pack2(source, True, False)
        # 2026-10-30: il blocco protegge soltanto la fase prima del collegamento.
        # Nel Gtk.Stack è la pagina selezionata a nascondere il sorgente; lasciare
        # no-show-all sullo scroller impedirebbe di mostrare anche la sua GtkSourceView.
        source.set_no_show_all(False)
        self.editor.pack_start(self, True, True, 0)
        self.editor.reorder_child(self, 2)
        self.show_all()
        self.controls.set_no_show_all(False)
        self.controls.show_all()
        self.controls.set_no_show_all(True)
        self._show_mode()
        self.schedule()
        self.editor.search_revealer.set_reveal_child(False)

    def configure_base(self, scm, path: str, state: str | None = None, source: str | None = None) -> None:
        """Load the appropriate repository parent without reading unsaved text from disk."""

        identity = (getattr(scm, "root", None), path, state, source)
        if identity == self.base_identity:
            return
        self.base_identity = identity
        self.repository_root = identity[0]
        self.base_serial += 1
        serial = self.base_serial
        if self.base_command is not None:
            self.base_command.cancel()
            self.base_command = None
        if self.base_timeout is not None:
            GLib.source_remove(self.base_timeout)
            self.base_timeout = None
        self.base = None
        self._sync_mode_buttons()
        # 2026-11-04: i file .min. restano testo modificabile, ma il diff
        # non è utile. Fermarsi prima di leggere la base o avviare il worker.
        if self._is_minified():
            self._cancel_diff()
            if self.debounce is not None:
                GLib.source_remove(self.debounce)
                self.debounce = None
            self.old_buffer.set_text("")
            if self.mode in {"compact", "unified", "side"}:
                self.set_mode("code")
            else:
                self._show_mode()
            return
        if scm is None or state in {"untracked", "added"}:
            self.base = "" if scm is not None else None
            self.old_buffer.set_text("")
            self._sync_mode_buttons()
            self._choose_initial(state not in {"untracked", None})
            self.schedule()
            return
        target = source or path
        argv = (["hg", "cat", "-r", ".", "--", f"path:{target}"] if scm.kind == "hg"
                else ["git", "show", f"HEAD:{target}"])

        def completed(result) -> None:
            """Ignore old base loads and choose the initial mode only once."""

            if self.closed or serial != self.base_serial:
                return
            self.base_command = None
            if self.base_timeout is not None:
                GLib.source_remove(self.base_timeout)
                self.base_timeout = None
            if result.ok and len(result.stdout.encode("utf-8")) <= self.editor.MAX_TEXT_BYTES:
                self.base = result.stdout
                self.old_buffer.set_text(self.base)
                self._sync_mode_buttons()
                self._choose_initial(self.base != self.text())
                self.schedule()
            else:
                # 2026-11-03: un errore transitorio non è una base valida;
                # una successiva apertura deve poter ripetere la lettura.
                self.base_identity = None
                self._choose_initial(False)
                self._error("Repository version unavailable; Code remains available.")

        self.base_command = run_async(argv, completed, cwd=scm.root, env=scm.environment)
        self.base_timeout = GLib.timeout_add(5000, self._base_timed_out)

    def _base_timed_out(self) -> bool:
        """Stop an unavailable repository read without blocking navigation."""

        self.base_timeout = None
        self.base_serial += 1
        self.base_identity = None
        if self.base_command is not None:
            self.base_command.cancel()
            self.base_command = None
        self._choose_initial(False)
        self._error("Repository version timed out; Code remains available.")
        return GLib.SOURCE_REMOVE

    def text(self) -> str:
        """Return the canonical unsaved source, never the visual diff text."""

        return self.editor.buffer.get_text(self.editor.buffer.get_start_iter(), self.editor.buffer.get_end_iter(), True)

    def _is_minified(self) -> bool:
        """Identify explicitly minified filenames without classifying their text as binary."""

        return ".min." in Path(self.editor.relative_path).name.casefold()

    def _choose_initial(self, changed: bool) -> None:
        """Apply common Files/Revisions defaults until the user selects a mode."""

        if not self.automatic or self.initial_chosen or self.editor.loading:
            return
        self.initial_chosen = True
        suffix = Path(self.editor.relative_path).suffix.lower()
        # 2026-10-30: i nuovi diff partono compatti per privilegiare le sole
        # differenze, senza introdurre preferenze persistenti o tra documenti.
        mode = "compact" if changed else ("rendered" if suffix in self.RENDERABLE else "code")
        self.mode = mode
        self._sync_mode_buttons()
        self._show_mode()

    def _document_ready(self, _buffer) -> None:
        """Recheck defaults after the original document finishes initial loading."""

        if self.base_identity is not None and self.base_command is None and not self.editor.loading:
            state = self.base_identity[2]
            self._choose_initial(self.base is not None and state != "untracked" and self.base != self.text())
        self.schedule()

    def _projection_error(self, mode: str) -> str | None:
        """Return a failed view's diagnostic only while both document versions still match."""

        if mode in {"compact", "unified", "side"} and self._is_minified():
            return "Diff disabled for minified files."
        error = self.projection_errors.get(mode)
        if error is not None and error[:2] == (self.serial, self.base_serial):
            return error[2]
        return None

    def _sync_mode_buttons(self) -> None:
        """Reflect the selected mode and disable unavailable views in place."""

        self.syncing_mode_buttons = True
        try:
            for identifier, button in self.mode_buttons.items():
                available = identifier != "rendered" or Path(self.editor.relative_path).suffix.lower() in self.RENDERABLE
                if identifier in {"unified", "side", "compact"}:
                    available = available and self.base is not None and self._projection_error(identifier) is None
                button.set_sensitive(available)
                button.set_active(identifier == self.mode)
        finally:
            self.syncing_mode_buttons = False

    def _mode_clicked(self, _button, identifier: str) -> None:
        """Activate a mode while preventing deselection of the current toggle."""

        if not self.syncing_mode_buttons:
            self.set_mode(identifier)

    def set_mode(self, identifier: str) -> None:
        """Switch derived views without replacing the canonical buffer or undo manager."""

        if identifier not in self.mode_buttons:
            raise ValueError("Unknown editor mode")
        error = self._projection_error(identifier)
        if error is not None:
            self._sync_mode_buttons()
            self._error(error)
            return
        previous_mode = self.mode
        self.automatic = False
        if identifier == self.mode and self.initial_chosen:
            self._sync_mode_buttons()
            return
        self.initial_chosen = True
        self._cancel_diff()
        if self.mode in {"unified", "compact"}:
            anchor, cursor = self._selection_offsets()
            self.editor.buffer.select_range(self.editor.buffer.get_iter_at_offset(cursor),
                                            self.editor.buffer.get_iter_at_offset(anchor))
        else:
            self.pending_selection = self._document_selection()
        self.mode = identifier
        # 2026-10-30: una scelta esplicita di modalità riparte dalla prima
        # differenza; i refresh ordinari continuano a preservare la navigazione.
        self.positioned_modes.discard(identifier)
        if self.mode in {"unified", "compact", "rendered"}:
            self.editor.search_revealer.set_reveal_child(False)
        if self.mode not in {"unified", "compact"}:
            self.projection_search.set_reveal_child(False)
        if self.mode != "side":
            self.old_search.set_reveal_child(False)
        self.automatic = False
        if (self.mode in {"unified", "compact"} and self.rows_serial == self.serial
                and self.rows_base_serial == self.base_serial):
            try:
                self._replace_spans()
            except ValueError as error:
                self.projection_errors[identifier] = (self.serial, self.base_serial, str(error))
                self.mode = previous_mode
                self._sync_mode_buttons()
                self._error(str(error))
                return
        self._show_mode()
        self._position_first_change()
        self.schedule()

    def _show_mode(self) -> None:
        """Present a requested mode and report non-applicable modes in place."""

        self._sync_mode_buttons()
        if not self.enabled:
            return
        self.notice.hide()
        self.syntax.configure(self.mode)
        if (self.automatic and not self.initial_chosen) or (
                self.mode in {"unified", "compact", "side"} and self.rows_serial < 0):
            self.stack.set_visible_child_name("loading")
            self.loading_indicator.start()
            return
        self.loading_indicator.stop()
        renderable = Path(self.editor.relative_path).suffix.lower() in self.RENDERABLE
        if self.mode == "rendered" and not renderable:
            self._error("Rendered is available for Markdown and HTML files.")
        if self.mode in {"code", "side"}:
            self.stack.set_visible_child_name("source")
            historical = getattr(self.editor, "historical", False)
            self.old_box.set_visible(self.mode == "side" or historical)
            self.old_scroller.show()
            self.editor.source_scroller.set_visible(not historical or self.mode == "side")
            if not historical or self.mode == "side":
                self.editor.source_scroller.show_all()
            if historical:
                self.old_view.show()
            self._clear_diff_tags()
            if self.mode == "side":
                self.old_view.show()
                self.split.set_position(max(100, self.editor.get_allocated_width() // 2))
                self._highlight_side()
        elif self.mode in {"unified", "compact"}:
            # Preparare il contenuto prima di esporre la vista, senza mostrare
            # la proiezione provvisoria contenente l'intero sorgente.
            self._render_projection()
            self.stack.set_visible_child_name("unified")
        elif renderable:
            self._render()
        if self.mode in {"side", "unified"} and self.base is None:
            self._error("Diff unavailable until a repository version can be loaded.")
        if self._is_minified():
            self._error("Diff disabled for minified files.")

    def _error(self, message: str) -> None:
        """Show a selectable non-modal warning without touching editor conflict UI."""

        self.loading_indicator.stop()
        if self.stack.get_visible_child_name() == "loading":
            self.stack.set_visible_child_name("source")
        self.notice.set_text(message)
        self.notice.show()

    def _document_changed(self, _buffer) -> None:
        """Update projections for external edits while preserving native source undo."""

        if self.pending_navigation is not None:
            self.editor.buffer.delete_mark(self.pending_navigation[0])
            self.pending_navigation = None
        self.serial += 1
        self._sync_mode_buttons()
        if self.enabled and self.mode in {"unified", "compact"} and not self.syncing and not self.composing:
            self._render_projection()
        self.schedule()

    def _history_restored(self, _buffer) -> None:
        """Mirror the canonical selection after GtkSource finishes restoring undo or redo."""

        if self.closed or not self.enabled or self.mode not in {"unified", "compact"}:
            return
        # 2026-11-08: changed arriva prima che GtkSource ripristini il cursore.
        # Sincronizzare dopo l'azione evita di mantenere la vecchia selezione
        # visiva; basta spostare i mark, senza ricostruire il diff né rubare focus.
        anchor, cursor = self._document_selection()
        self.projection.select_range(self.projection.get_iter_at_offset(self._visual_offset(cursor)),
                                     self.projection.get_iter_at_offset(self._visual_offset(anchor)))
        self.projection_view.scroll_to_mark(self.projection.get_insert(), 0.15, False, 0, 0)

    def schedule(self) -> None:
        """Debounce visible derived work and keep hidden editors idle."""

        if not self.enabled or self.closed or not self.editor.get_mapped() or self.editor.loading:
            return
        if self._is_minified() and self.mode != "rendered":
            return
        if self._projection_error(self.mode) is not None:
            return
        if self.rows_serial == self.serial and self.rows_base_serial == self.base_serial and self.mode != "rendered":
            return
        if self.debounce is not None:
            GLib.source_remove(self.debounce)
        # 2026-10-30: il debounce serve alle modifiche successive, non al
        # primo diff richiesto esplicitamente aprendo un file.
        self.debounce = (GLib.idle_add(self._refresh) if self.rows_serial < 0
                         else GLib.timeout_add(250, self._refresh))

    def _refresh(self) -> bool:
        """Render the latest buffer or start the sole diff worker for this document."""

        self.debounce = None
        if not self.enabled or self.closed or not self.editor.get_mapped() or self.composing:
            return GLib.SOURCE_REMOVE
        if self.mode == "rendered":
            self._render()
            return GLib.SOURCE_REMOVE
        if self._is_minified():
            return GLib.SOURCE_REMOVE
        if self.mode == "code" or self.base is None or self.command is not None:
            return GLib.SOURCE_REMOVE
        text = self.text()
        self.job_serial += 1
        job_serial = self.job_serial
        serial = self.serial
        base_serial = self.base_serial
        payload = json.dumps({"before": self.base, "current": text}).encode("utf-8")

        def computed(result) -> None:
            """Apply a version-matched worker result, never an obsolete projection."""

            if job_serial != self.job_serial:
                return
            self.command = None
            if self.timeout is not None:
                GLib.source_remove(self.timeout)
                self.timeout = None
            if self.closed or not self.enabled:
                return
            if serial != self.serial or base_serial != self.base_serial or self.composing:
                self.schedule()
                return
            try:
                if not result.ok:
                    detail = result.stderr.strip().splitlines()
                    raise ValueError(detail[-1].removeprefix("ValueError: ") if detail else "Diff computation failed")
                rows = json.loads(result.stdout)
                if len(rows) > 50000:
                    raise ValueError("Diff too large to display safely; use Code")
                candidate_rows = [DiffRow(**row) for row in rows]
                # 2026-10-30: i mark canonici seguono inserimenti e rimozioni;
                # gli indici delle righe del vecchio diff non sono stabili.
                candidate_gaps = set()
                # 2026-11-08: ricompattare dopo Undo può nascondere la riga
                # appena ripristinata. Conservare gli estremi della selezione
                # già visibile, senza espandere contesto alla prima apertura
                # né interferire con una navigazione esplicita ancora pendente.
                selection = (self._selection_offsets() if self.mode == "compact"
                             and self.mode in self.positioned_modes and self.pending_navigation is None else ())
                for gap in compact_rows(candidate_rows):
                    if gap.kind != "omitted":
                        continue
                    start = self.editor.buffer.get_iter_at_line(candidate_rows[gap.gap_start].new_line).get_offset()
                    last = candidate_rows[gap.gap_end - 1]
                    end = self.editor.buffer.get_iter_at_line(last.new_line).get_offset() + len(last.text)
                    if any(start <= offset < end for offset in selection):
                        candidate_gaps.add((gap.gap_start, gap.gap_end))
                        continue
                    for first_mark, last_mark in self.expanded_gap_marks:
                        if (self.editor.buffer.get_iter_at_mark(first_mark).get_offset() <= start
                                and self.editor.buffer.get_iter_at_mark(last_mark).get_offset() >= end):
                            candidate_gaps.add((gap.gap_start, gap.gap_end))
                            break
                # 2026-10-30: pubblicare righe e seriali solo dopo che tutti
                # i limiti della proiezione sono stati validati senza toccare
                # i mark precedenti. Un errore lascia riutilizzabile la vista.
                pending_line = None
                if self.pending_navigation is not None:
                    mark, insert, bound, mode = self.pending_navigation
                    if (self.mode == mode
                            and self.projection.get_iter_at_mark(self.projection.get_insert()).get_offset() == insert
                            and self.projection.get_iter_at_mark(self.projection.get_selection_bound()).get_offset() == bound):
                        pending_line = self.editor.buffer.get_iter_at_mark(mark).get_line()
                    self.editor.buffer.delete_mark(mark)
                    self.pending_navigation = None
                self._replace_spans(candidate_rows, candidate_gaps)
                self.rows = candidate_rows
                self.expanded_gaps = candidate_gaps
                self.rows_serial = self.serial
                self.rows_base_serial = self.base_serial
                self.projection_errors.pop(self.mode, None)
                self.hunks = []
                in_change = False
                for index, row in enumerate(self.rows):
                    if row.kind == "equal":
                        in_change = False
                        continue
                    if not in_change:
                        target = row.new_line
                        if target is None:
                            # 2026-10-30: gli slice copiano la coda a ogni
                            # hunk nel main loop. Cercare per indice visita solo
                            # le righe rimosse prima della prossima riga corrente.
                            target = next((self.rows[position].new_line
                                           for position in range(index + 1, len(self.rows))
                                           if self.rows[position].new_line is not None),
                                          max(0, self.editor.buffer.get_line_count() - 1))
                        self.hunks.append(target)
                    in_change = True
                self._show_mode()
                self._position_first_change()
                if pending_line is not None:
                    self.editor.goto_line_number(pending_line + 1, focus=False)
            except (ValueError, TypeError, KeyError) as error:
                self.projection_errors[self.mode] = (self.serial, self.base_serial, str(error))
                self._sync_mode_buttons()
                self._error(str(error))

        self.command = run_async([sys.executable, "-m", "slate.editor_diff_worker"], computed, cwd=str(Path(__file__).resolve().parent.parent), input_bytes=payload)
        self.timeout = GLib.timeout_add(5000, self._diff_timed_out)
        return GLib.SOURCE_REMOVE

    def _diff_timed_out(self) -> bool:
        """Bound pathological diff matching without freezing the GTK main loop."""

        self.timeout = None
        self.job_serial += 1
        # 2026-11-03: le tre viste condividono lo stesso calcolo; rimappare
        # il documento non deve ripetere un timeout sugli stessi due testi.
        message = "Diff computation timed out; use Code."
        for mode in ("compact", "unified", "side"):
            self.projection_errors[mode] = (self.serial, self.base_serial, message)
        self._sync_mode_buttons()
        if self.command is not None:
            command = self.command
            self.command = None
            command.cancel()
        self._error(message)
        return GLib.SOURCE_REMOVE

    def _replace_spans(self, rows=None, expanded_gaps=None) -> None:
        """Validate candidate rows before replacing canonical and visual projection marks."""

        rows = self.rows if rows is None else rows
        expanded_gaps = self.expanded_gaps if expanded_gaps is None else expanded_gaps
        source = self.editor.buffer
        lines = self.text().splitlines(keepends=True)
        offsets = [0]
        for line in lines:
            offsets.append(offsets[-1] + len(line))
        # 2026-10-30: equal/added blocks need only one mark pair. Creating
        # marks for every line of a large unchanged file stalls the GTK loop.
        groups = []
        display = compact_rows(rows, expanded=expanded_gaps) if self.mode == "compact" else rows
        for row in display:
            if not groups or groups[-1][0].kind != row.kind:
                groups.append([row])
            else:
                groups[-1].append(row)
        if len(groups) > 2000:
            raise ValueError("Diff too fragmented to display safely; use Code")
        # A trailing deleted block still needs an editable EOF insertion point.
        # Otherwise Ctrl+End would land in history with no way to append source.
        if not groups or groups[-1][0].new_line is None:
            groups.append([DiffRow("", "added", None, len(lines))])
        # 2026-10-30: anche coordinate errate devono fallire prima di eliminare
        # la vista valida; non lasciare mark parziali né una cache falsamente pronta.
        for group in groups:
            row = group[0]
            if row.new_line is not None:
                if not 0 <= row.new_line < len(offsets):
                    raise ValueError("Invalid diff coordinates; use Code")
                if offsets[row.new_line] + sum(len(item.text) for item in group) > source.get_char_count():
                    raise ValueError("Invalid diff coordinates; use Code")
        self.pending_selection = self._selection_offsets()
        self._delete_marks()
        last_live = max((index for index, group in enumerate(groups) if group[0].new_line is not None), default=-1)
        for index, group in enumerate(groups):
            row = group[0]
            text = "".join(item.text for item in group)
            span = ProjectionSpan(row.kind, text, old_line=row.old_line,
                                  gap=(row.gap_start, row.gap_end) if row.kind == "omitted" else None)
            if row.new_line is not None:
                offset = offsets[row.new_line]
                span.start = source.create_mark(None, source.get_iter_at_offset(offset), True)
                span.end = source.create_mark(None, source.get_iter_at_offset(offset + len(text)), index != last_live)
            self.spans.append(span)
        if last_live < 0:
            end = source.get_end_iter()
            self.spans.append(ProjectionSpan("added", "", source.create_mark(None, end, True),
                                             source.create_mark(None, end, False)))

    def _delete_marks(self) -> None:
        """Release old private marks while preserving the document's own cursor marks."""

        for span in self.spans:
            if span.visual_start is not None:
                self.projection.delete_mark(span.visual_start)
                self.projection.delete_mark(span.visual_end)
            if span.start is not None:
                self.editor.buffer.delete_mark(span.start)
                self.editor.buffer.delete_mark(span.end)
        self.spans = []

    def _render_projection(self) -> None:
        """Refresh presentation from mark-bound live text, retaining historical rows."""

        if self.composing or self.rows_serial < 0:
            return
        if not self.spans:
            source = self.editor.buffer
            self.spans = [ProjectionSpan("equal", "", source.create_mark(None, source.get_start_iter(), True),
                                         source.create_mark(None, source.get_end_iter(), False))]
        selection = getattr(self, "pending_selection", None) or self._selection_offsets()
        self.pending_selection = None
        for span in self.spans:
            if span.visual_start is not None:
                self.projection.delete_mark(span.visual_start)
                self.projection.delete_mark(span.visual_end)
                span.visual_start = span.visual_end = None
        adjustment = self.projection_view.get_parent().get_vadjustment()
        scroll = adjustment.get_value()
        self.syncing = True
        try:
            self.projection.set_text("")
            for index, span in enumerate(self.spans):
                text = span.text if span.start is None else self.editor.buffer.get_text(
                    self.editor.buffer.get_iter_at_mark(span.start), self.editor.buffer.get_iter_at_mark(span.end), True)
                visual = text
                if text and not text.endswith("\n") and index < len(self.spans) - 1:
                    visual += "\n"
                start = self.projection.get_char_count()
                self.projection.insert(self.projection.get_end_iter(), visual)
                span.visual_start = self.projection.create_mark(None, self.projection.get_iter_at_offset(start), True)
                span.visual_end = self.projection.create_mark(None, self.projection.get_end_iter(), index != len(self.spans) - 1)
                if span.kind in self.tags:
                    self.projection.apply_tag(self.tags[span.kind], self.projection.get_iter_at_offset(start), self.projection.get_end_iter())
            anchor, cursor = selection
            self.projection.select_range(self.projection.get_iter_at_offset(self._visual_offset(cursor)),
                                         self.projection.get_iter_at_offset(self._visual_offset(anchor)))
            adjustment.set_value(scroll)
        finally:
            self.syncing = False
        self.syntax.projection_changed()

    def _live_ranges(self, start: int, end: int):
        """Map visual intersections onto canonical offsets, excluding historical text."""

        ranges = []
        for span in self.spans:
            if span.start is None or span.visual_start is None:
                continue
            left = self.projection.get_iter_at_mark(span.visual_start).get_offset()
            right = self.projection.get_iter_at_mark(span.visual_end).get_offset()
            source_start = self.editor.buffer.get_iter_at_mark(span.start).get_offset()
            source_end = self.editor.buffer.get_iter_at_mark(span.end).get_offset()
            right = min(right, left + source_end - source_start)
            if start < right and end > left:
                a, b = max(left, start), min(right, end)
                ranges.append((a, b, source_start + a - left, source_start + b - left))
        return ranges

    def _source_offset(self, offset: int):
        """Resolve an insertion point only in a current-text span or its EOF."""

        for span in self.spans:
            if span.start is None or span.visual_start is None:
                continue
            left = self.projection.get_iter_at_mark(span.visual_start).get_offset()
            start = self.editor.buffer.get_iter_at_mark(span.start).get_offset()
            end = self.editor.buffer.get_iter_at_mark(span.end).get_offset()
            if left <= offset < left + end - start:
                return start + offset - left
            # 2026-11-08: dopo una cancellazione GTK può conservare il punto
            # del vecchio span, ora vuoto, davanti alla coda storica. È ancora
            # un'ancora del sorgente a EOF, non un carattere della storia.
            if offset == left and start == end == self.editor.buffer.get_char_count():
                return end
            if offset == self.projection.get_char_count() and offset == left + end - start:
                return end
        return None

    def _document_selection(self):
        """Read the canonical caret and selection independently of a hidden projection."""

        source = self.editor.buffer
        return (source.get_iter_at_mark(source.get_selection_bound()).get_offset(),
                source.get_iter_at_mark(source.get_insert()).get_offset())

    def _selection_offsets(self):
        """Capture canonical selection anchors before replacing a derived layout."""

        if not any(span.visual_start is not None for span in self.spans):
            return self._document_selection()
        anchor = self.projection.get_iter_at_mark(self.projection.get_selection_bound()).get_offset()
        # 2026-11-07: ogni conversione visita gli span; conservare i risultati
        # evita di cercare due volte l'ancora o il cursore di fallback.
        source_anchor = self._source_offset(anchor)
        cursor = self._canonical_cursor()
        return (source_anchor if source_anchor is not None else cursor, cursor)

    def _canonical_cursor(self) -> int:
        """Read the projection caret as a source offset, with a canonical fallback."""

        cursor = self.projection.get_iter_at_mark(self.projection.get_insert()).get_offset()
        value = self._source_offset(cursor)
        if value is not None:
            return value
        return self.editor.buffer.get_iter_at_mark(self.editor.buffer.get_insert()).get_offset()

    def _visual_offset(self, offset: int) -> int:
        """Map a canonical caret back to its live visual span."""

        # 2026-11-07: la fine del sorgente deve seguire anche la coda storica;
        # il primo span che termina a EOF può precedere righe eliminate.
        # _replace_spans mantiene un punto di inserimento vivo in fondo alla vista.
        if offset == self.editor.buffer.get_char_count():
            return self.projection.get_char_count()
        boundary = None
        for span in self.spans:
            if span.start is None or span.visual_start is None:
                continue
            start = self.editor.buffer.get_iter_at_mark(span.start).get_offset()
            end = self.editor.buffer.get_iter_at_mark(span.end).get_offset()
            # 2026-11-07: il confine appartiene al blocco corrente successivo,
            # altrimenti la navigazione atterra sulle righe storiche interposte.
            # L'EOF resta un punto di inserimento valido nell'ultimo blocco.
            if start <= offset < end:
                return self.projection.get_iter_at_mark(span.visual_start).get_offset() + offset - start
            if offset == end:
                boundary = self.projection.get_iter_at_mark(span.visual_start).get_offset() + end - start
        # 2026-11-09: un bordo davanti a un gap è un punto d'inserimento vivo.
        # Usarlo solo in assenza di uno span successivo che rappresenti il punto
        # conserva la precedenza del testo corrente dopo le righe storiche.
        return boundary if boundary is not None else self.projection.get_char_count()

    def _begin_action(self, _buffer) -> None:
        """Keep native paste/replacement edits in one canonical undo action."""

        if not self.syncing:
            self.source_action_active = True
            self.editor.buffer.begin_user_action()

    def _end_action(self, _buffer) -> None:
        """Close the canonical action after GTK completes the whole visual operation."""

        if self.source_action_active:
            self.source_action_active = False
            self.editor.buffer.end_user_action()

    def _projection_menu(self, _view, menu) -> None:
        """Expose canonical undo and native clipboard commands in the derived view."""

        for child in menu.get_children():
            menu.remove(child)
        selection = self.projection.get_selection_bounds()
        cuttable = (not self.editor.loading and self.editor.view.get_editable() and bool(selection)
                    and bool(self._live_ranges(selection[0].get_offset(), selection[1].get_offset())))
        for action, label in (("undo", "Undo"), ("redo", "Redo"),
                              ("cut-clipboard", "Cut"), ("copy-clipboard", "Copy"),
                              ("paste-clipboard", "Paste"), ("select-all", "Select all")):
            item = Gtk.MenuItem(label=label)
            item.connect("activate", self._context_action, action)
            if action == "undo":
                item.set_sensitive(self.editor.buffer.can_undo())
            elif action == "redo":
                item.set_sensitive(self.editor.buffer.can_redo())
            elif action == "cut-clipboard":
                item.set_sensitive(cuttable)
            menu.append(item)
        menu.show_all()

    def _context_action(self, _item, action: str) -> None:
        """Route derived context commands to the correct source or visual buffer."""

        if action == "undo":
            self.editor.undo()
        elif action == "redo":
            self.editor.redo()
        elif action == "select-all":
            self.projection.select_range(self.projection.get_start_iter(), self.projection.get_end_iter())
        else:
            self.projection_view.emit(action)

    def _insert(self, _buffer, position, text: str, _length: int) -> None:
        """Mirror an accepted visual insertion into the canonical undo history."""

        if self.syncing:
            return
        boundary_span = None
        boundary_gap = None
        offset = self._source_offset(position.get_offset())
        if offset is None and not self.editor.loading and self.editor.view.get_editable():
            # 2026-11-08: cancellare fino al bordo di uno span può lasciare
            # l'iteratore GTK davanti alla storia. Il bordo conserva un punto
            # canonico valido: inserirlo nel successivo span corrente, senza
            # rendere modificabile il testo storico né usare il fallback a EOF
            # quando il punto canonico non è rappresentato nella proiezione.
            for index, span in enumerate(self.spans):
                if span.start is None or span.visual_start is None:
                    continue
                left = self.projection.get_iter_at_mark(span.visual_start).get_offset()
                start = self.editor.buffer.get_iter_at_mark(span.start).get_offset()
                end = self.editor.buffer.get_iter_at_mark(span.end).get_offset()
                if position.get_offset() != left + end - start:
                    continue
                visual = self._visual_offset(end)
                if self._source_offset(visual) == end:
                    offset = end
                    position.assign(self.projection.get_iter_at_offset(visual))
                    break
                if index + 1 < len(self.spans):
                    following = self.spans[index + 1]
                    if (following.gap is not None and following.visual_start is not None
                            and self.projection.get_iter_at_mark(following.visual_start).get_offset() == position.get_offset()):
                        # 2026-11-09: il testo nascosto resta nel sorgente;
                        # inserire sul bordo dello span visibile senza usare EOF.
                        offset = end
                        boundary_span = span
                        boundary_gap = following
                        break
        if offset is None or self.editor.loading or not self.editor.view.get_editable():
            GObject.signal_stop_emission_by_name(self.projection, "insert-text")
            return
        # 2026-11-08: anche l'iteratore privato dell'incolla deve puntare al
        # vero EOF visivo, così il testo entra nello span vivo dopo la storia.
        if offset == self.editor.buffer.get_char_count():
            position.assign(self.projection.get_end_iter())
        self.syncing = True
        try:
            source = self.editor.buffer
            source.begin_user_action()
            source.insert(source.get_iter_at_offset(offset), text)
            if boundary_span is not None:
                # 2026-11-09: lo span corrente deve possedere il nuovo testo,
                # mentre il marker del gap deve seguirlo. La gravità destra
                # conserva questo confine anche durante Undo e Redo nativi.
                source.delete_mark(boundary_span.end)
                boundary_span.end = source.create_mark(None, source.get_iter_at_offset(offset + len(text)), False)
                self.projection.delete_mark(boundary_span.visual_end)
                boundary_span.visual_end = self.projection.create_mark(None, position, False)
                self.projection.delete_mark(boundary_gap.visual_start)
                boundary_gap.visual_start = self.projection.create_mark(None, position, False)
            source.place_cursor(source.get_iter_at_offset(offset + len(text)))
            source.end_user_action()
        finally:
            self.syncing = False

    def _after_projection_insert(self, _buffer, _position, _text, _length) -> None:
        """Schedule diff recalculation only after GTK has completed its visual insertion."""

        if not self.syncing:
            self.schedule()

    def _delete(self, _buffer, start, end) -> None:
        """Delete only editable intersections, keeping historical rows out of source undo."""

        if self.syncing:
            return
        if _buffer is not None:
            GObject.signal_stop_emission_by_name(self.projection, "delete-range")
        if self.editor.loading or not self.editor.view.get_editable():
            return
        ranges = self._live_ranges(start.get_offset(), end.get_offset())
        if not ranges:
            return
        self.syncing = True
        source = self.editor.buffer
        source.begin_user_action()
        try:
            for left, right, a, b in reversed(ranges):
                source.delete(source.get_iter_at_offset(a), source.get_iter_at_offset(b))
                self.projection.delete(self.projection.get_iter_at_offset(left), self.projection.get_iter_at_offset(right))
            source.place_cursor(source.get_iter_at_offset(ranges[0][2]))
            # 2026-10-30: delete-range normalmente riconvalida gli iteratori
            # del chiamante. Qui fermiamo il default GTK, quindi dobbiamo farlo
            # noi prima che un incolla nativo riutilizzi la posizione d'inserimento.
            insertion = self.projection.get_iter_at_offset(ranges[0][0])
            # 2026-11-08: se la selezione ha eliminato la coda del sorgente,
            # la vecchia posizione può ora appartenere a righe storiche protette.
            # Il punto EOF vivo è dopo quella storia: GTK deve poter completare
            # l'inserimento della clipboard nella stessa azione di Undo.
            if ranges[0][2] == source.get_char_count():
                insertion = self.projection.get_end_iter()
            start.assign(insertion)
            end.assign(insertion)
            self.projection.place_cursor(insertion)
        finally:
            source.end_user_action()
            self.syncing = False
        self.schedule()

    def _cut(self, _view) -> None:
        """Copy only selected current text before removing editable intersections."""

        GObject.signal_stop_emission_by_name(self.projection_view, "cut-clipboard")
        if self.editor.loading or not self.editor.view.get_editable():
            return
        selection = self.projection.get_selection_bounds()
        if not selection:
            return
        start, end = selection
        ranges = self._live_ranges(start.get_offset(), end.get_offset())
        # 2026-11-06: una selezione soltanto storica non è tagliabile;
        # preservare la clipboard anche se il comando arriva da tastiera.
        if not ranges:
            return
        text = "".join(self.editor.buffer.get_text(self.editor.buffer.get_iter_at_offset(a),
                                                  self.editor.buffer.get_iter_at_offset(b), True)
                       for _left, _right, a, b in ranges)
        self.projection_view.get_clipboard(Gdk.SELECTION_CLIPBOARD).set_text(text, -1)
        self._delete(None, start, end)

    def _preedit(self, _view, text: str) -> None:
        """Never replace a projection during an unfinished IME composition."""

        self.composing = bool(text)
        if not self.composing:
            self.schedule()

    def _clear_diff_tags(self) -> None:
        """Remove only experimental decoration, retaining syntax and document state."""

        self.editor.buffer.remove_tag(self.new_tag, self.editor.buffer.get_start_iter(), self.editor.buffer.get_end_iter())

    def _highlight_side(self) -> None:
        """Decorate both versions without replacing the editable source contents."""

        self._clear_diff_tags()
        self.old_buffer.remove_tag(self.old_tag, self.old_buffer.get_start_iter(), self.old_buffer.get_end_iter())
        for span in self.spans:
            if span.kind == "added" and span.start is not None:
                self.editor.buffer.apply_tag(self.new_tag, self.editor.buffer.get_iter_at_mark(span.start),
                                             self.editor.buffer.get_iter_at_mark(span.end))
            elif span.kind == "removed" and span.old_line is not None:
                start = self.old_buffer.get_iter_at_line(span.old_line)
                end = self.old_buffer.get_iter_at_offset(start.get_offset() + len(span.text))
                self.old_buffer.apply_tag(self.old_tag, start, end)

    def _projection_button(self, view, event) -> bool:
        """Expand a compact gap without modifying the canonical document."""

        if event.type != Gdk.EventType._2BUTTON_PRESS or self.mode != "compact":
            return False
        x, y = view.window_to_buffer_coords(Gtk.TextWindowType.TEXT, int(event.x), int(event.y))
        _inside, cursor = view.get_iter_at_location(x, y)
        return self.expand_gap_at(cursor.get_offset())

    def expand_gap_at(self, offset: int) -> bool:
        """Expand one explicit omitted range and keep all surrounding source intact."""

        for span in self.spans:
            if span.gap is None or span.visual_start is None:
                continue
            start = self.projection.get_iter_at_mark(span.visual_start).get_offset()
            end = self.projection.get_iter_at_mark(span.visual_end).get_offset()
            if start <= offset < end:
                if self.rows_serial != self.serial or self.rows_base_serial != self.base_serial:
                    # 2026-11-05: durante il debounce gli indici del vecchio
                    # diff non sono validi. I mark live dei bordi conservano
                    # l'espansione richiesta per il prossimo risultato worker.
                    index = self.spans.index(span)
                    source = self.editor.buffer
                    previous = next((item for item in reversed(self.spans[:index]) if item.end is not None), None)
                    following = next((item for item in self.spans[index + 1:] if item.start is not None), None)
                    first = source.get_iter_at_mark(previous.end) if previous is not None else source.get_start_iter()
                    last = source.get_iter_at_mark(following.start) if following is not None else source.get_end_iter()
                    self.expanded_gap_marks.append((source.create_mark(None, first, True),
                                                    source.create_mark(None, last, False)))
                    self.schedule()
                    return True
                self.expanded_gaps.add(span.gap)
                # 2026-10-30: mantenere l'espansione sul testo sorgente, anche
                # quando una modifica precedente sposta gli indici del diff.
                first = self.rows[span.gap[0]]
                last = self.rows[span.gap[1] - 1]
                source = self.editor.buffer
                start = source.get_iter_at_line(first.new_line)
                end = source.get_iter_at_line(last.new_line)
                end.forward_chars(len(last.text))
                self.expanded_gap_marks.append((source.create_mark(None, start, True),
                                                source.create_mark(None, end, False)))
                self._replace_spans()
                self._render_projection()
                return True
        return False

    def _position_first_change(self) -> None:
        """Position each diff presentation once, without moving focus or later edits."""

        if self.mode not in {"unified", "side", "compact"} or self.mode in self.positioned_modes or not self.hunks:
            return
        self.positioned_modes.add(self.mode)
        target = self.hunks[0]
        mode = self.mode
        serial = self.serial
        self.editor.goto_line_number(target + 1, focus=False)
        if mode == "side":
            self.editor.view.scroll_to_mark(self.editor.buffer.get_insert(), 0.15, False, 0, 0)
        self.align_line(target)
        old_line = next((row.old_line for row in self.rows if row.kind != "equal" and row.old_line is not None), target)
        old_line = min(old_line, self.old_buffer.get_line_count() - 1)

        def allocated() -> bool:
            """Repeat scrolling after allocation only if the user has not navigated away."""

            self.first_scroll_id = None
            if self.closed or not self.enabled or self.mode != mode or self.serial != serial:
                return GLib.SOURCE_REMOVE
            navigation, _view = self.editor._navigation_source()
            if navigation.get_iter_at_mark(navigation.get_insert()).get_line() != target:
                return GLib.SOURCE_REMOVE
            if mode == "side":
                self.old_buffer.place_cursor(self.old_buffer.get_iter_at_line(old_line))
                self.old_view.scroll_to_mark(self.old_buffer.get_insert(), 0.15, False, 0, 0)
            else:
                self.align_line(target)
            return GLib.SOURCE_REMOVE

        if self.first_scroll_id is not None:
            GLib.source_remove(self.first_scroll_id)
        self.first_scroll_id = GLib.idle_add(allocated)

    def _navigate(self, delta: int) -> None:
        """Move to the previous or next changed source line without altering text."""

        if not self.hunks:
            return
        # 2026-10-30: un click nella proiezione non sposta il cursore del
        # buffer nascosto; partire sempre dalla posizione visibile all'utente.
        cursor = (self.editor.buffer.get_iter_at_offset(self._canonical_cursor())
                  if self.mode in {"unified", "compact"}
                  else self.editor.buffer.get_iter_at_mark(self.editor.buffer.get_insert()))
        line = cursor.get_line()
        if self.mode == "side" and self.last_side == "old":
            # 2026-11-06: il clic nella baseline non sposta il cursore live.
            # Mappare la riga storica al primo punto corrente corrispondente,
            # includendo il blocco aggiunto che segue eventuali righe rimosse.
            old_line = self.old_buffer.get_iter_at_mark(self.old_buffer.get_insert()).get_line()
            index = next((index for index, row in enumerate(self.rows)
                          if row.old_line is not None and row.old_line >= old_line), len(self.rows))
            line = next((self.rows[position].new_line for position in range(index, len(self.rows))
                         if self.rows[position].new_line is not None),
                        max(0, self.editor.buffer.get_line_count() - 1))
        # 2026-11-07: gli hunk seguono l'ordine delle righe sorgenti. Cercare
        # il vicino stretto senza scandire/copiarli tutti, mantenendo il wrap.
        index = bisect_right(self.hunks, line) if delta > 0 else bisect_left(self.hunks, line) - 1
        target = self.hunks[index % len(self.hunks)]
        self.editor.goto_line_number(target + 1, focus=self.mode not in {"unified", "compact"})
        if self.mode in {"unified", "compact"}:
            offset = self._visual_offset(self.editor.buffer.get_iter_at_mark(self.editor.buffer.get_insert()).get_offset())
            cursor = self.projection.get_iter_at_offset(offset)
            self.projection.place_cursor(cursor)
            self.projection_view.scroll_to_iter(cursor, 0.15, False, 0, 0)
            self.projection_view.grab_focus()

    def _previous(self, _button) -> None:
        """Navigate to the previous diff change."""

        self._navigate(-1)

    def _next(self, _button) -> None:
        """Navigate to the next diff change."""

        self._navigate(1)

    def _side_focus(self, view, _event) -> bool:
        """Remember which side owns search without changing the document mode."""

        self.last_side = "old" if view is self.old_view else "current"
        return False

    def _scroll_current(self, adjustment) -> None:
        """Align the baseline scroll with the current document proportionally."""

        self._sync_scroll(adjustment, self.old_scroller.get_vadjustment())

    def _scroll_old(self, adjustment) -> None:
        """Align the current document when the baseline is scrolled."""

        self._sync_scroll(adjustment, self.editor.source_scroller.get_vadjustment())

    def _sync_scroll(self, source, target) -> None:
        """Synchronize only visible side-by-side scrolling without feedback loops."""

        if self.scroll_syncing or not self.enabled or self.mode != "side":
            return
        self.scroll_syncing = True
        try:
            span = source.get_upper() - source.get_page_size()
            fraction = source.get_value() / span if span > 0 else 0
            target.set_value(fraction * max(0, target.get_upper() - target.get_page_size()))
        finally:
            self.scroll_syncing = False

    def resource_changed(self, relative_path: str) -> None:
        """Invalidate rendered content when the shared watcher reports a used local asset."""

        if self.renderer is None:
            return
        if any(path == relative_path or path.startswith(relative_path.rstrip("/") + "/")
               for path in self.renderer.resource_paths):
            self.rendered_document = None
            if self.mode == "rendered":
                self.schedule()

    def _render(self) -> None:
        """Render the canonical buffer lazily in an isolated local WebView."""

        if not self.enabled or not self.editor.get_mapped():
            return
        try:
            if self.renderer is None:
                self.renderer = DocumentRenderer(self.editor.root, self.editor.relative_path)
                box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
                box.pack_start(self.renderer.view, True, True, 0)
                self.render_search_box = Gtk.Revealer()
                self.render_entry = Gtk.SearchEntry()
                self.render_entry.connect("search-changed", self._render_find)
                self.render_entry.connect("key-press-event", self._render_find_key)
                search_row = Gtk.Box(spacing=4)
                search_row.pack_start(self.render_entry, True, True, 0)
                search_row.pack_start(self.editor._tool_button("window-close-symbolic", "Close search", self._close_render_find), False, False, 0)
                self.render_search_box.add(search_row)
                box.pack_start(self.render_search_box, False, False, 0)
                self.stack.add_named(box, "rendered")
                box.show_all()
            self.renderer.relative_path = self.editor.relative_path
            text = (self.base or "") if self.editor.historical else self.text()
            document = (self.editor.relative_path, text)
            # 2026-10-30: rimappare o riselezionare una vista già pronta non
            # deve richiamare load_html né perdere scroll e selezione WebKit.
            if document != self.rendered_document:
                self.renderer.render(text)
                self.rendered_document = document
            self.stack.set_visible_child_name("rendered")
        except (RuntimeError, ValueError) as error:
            self._error(str(error))

    def show_search(self) -> bool:
        """Search the visible derived surface without silently changing modes."""

        if ((self.mode == "side" and self.last_side == "old")
                or (self.mode == "code" and self.editor.historical)):
            self.old_search.show_search()
            return True
        if self.mode in {"unified", "compact"}:
            self.projection_search.show_search()
            return True
        if self.mode == "rendered" and self.render_search_box is not None:
            self.render_search_box.set_reveal_child(True)
            self.render_entry.grab_focus()
            return True
        return False

    def _render_find(self, entry) -> None:
        """Search the rendered document through WebKit's native find controller."""

        controller = self.renderer.view.get_find_controller()
        text = entry.get_text()
        if text:
            controller.search(text, WebKit2.FindOptions.CASE_INSENSITIVE | WebKit2.FindOptions.WRAP_AROUND, 1000)
        else:
            controller.search_finish()

    def _close_render_find(self, _button) -> None:
        """Hide rendered search and restore focus to its presentation."""

        self.render_search_box.set_reveal_child(False)
        self.renderer.view.get_find_controller().search_finish()
        self.renderer.view.grab_focus()

    def _render_find_key(self, _entry, event) -> bool:
        """Close rendered search with Escape without navigating away."""

        if event.keyval == Gdk.KEY_Escape:
            self._close_render_find(None)
            return True
        return False

    def align_line(self, line: int, focus: bool = False) -> None:
        """Mirror a canonical line navigation in the editable unified projection."""

        if self.mode not in {"unified", "compact"}:
            return
        if self.mode == "compact" and not self.editor.historical and (
                self.rows_serial != self.serial or self.rows_base_serial != self.base_serial):
            # 2026-11-06: il blocco può essere espanso soltanto dopo il worker.
            # Conservare anche la navigazione, senza spostare il cursore a EOF;
            # clic e modifiche successivi dell'utente prevalgono sulla richiesta.
            if self.pending_navigation is not None:
                self.editor.buffer.delete_mark(self.pending_navigation[0])
            source = self.editor.buffer.get_iter_at_line(line)
            self.pending_navigation = (
                self.editor.buffer.create_mark(None, source, True),
                self.projection.get_iter_at_mark(self.projection.get_insert()).get_offset(),
                self.projection.get_iter_at_mark(self.projection.get_selection_bound()).get_offset(),
                self.mode,
            )
            if focus:
                self.projection_view.grab_focus()
            self.schedule()
            return
        # 2026-11-05: una riga omessa non ha coordinate nella proiezione;
        # espandere il suo blocco evita il fallback a EOF e inserimenti altrove.
        if self.mode == "compact":
            for span in self.spans:
                if span.gap is None or span.visual_start is None:
                    continue
                first = self.rows[span.gap[0]].new_line
                last = self.rows[span.gap[1] - 1].new_line
                if first is not None and last is not None and first <= line <= last:
                    self.expand_gap_at(
                        self.projection.get_iter_at_mark(span.visual_start).get_offset()
                    )
                    break
        if self.editor.historical:
            # 2026-11-05: anche nel diff di un file eliminato la riga richiesta
            # appartiene alla baseline e va mappata sulle righe storiche visibili.
            source = self.old_buffer.get_iter_at_line(line)
            offset = self.projection.get_char_count()
            for span in self.spans:
                if span.old_line is None or span.visual_start is None:
                    continue
                start = self.old_buffer.get_iter_at_line(span.old_line).get_offset()
                relative = source.get_offset() - start
                if 0 <= relative <= len(span.text):
                    offset = self.projection.get_iter_at_mark(span.visual_start).get_offset() + relative
                    break
        else:
            source = self.editor.buffer.get_iter_at_line(line)
            offset = self._visual_offset(source.get_offset())
        cursor = self.projection.get_iter_at_offset(offset)
        self.projection.place_cursor(cursor)
        # 2026-10-30: lo scroll su un mark viene applicato da GTK dopo il
        # layout; scroll_to_iter può fallire quando la pagina è appena cambiata.
        self.projection_view.scroll_to_mark(self.projection.get_insert(), 0.15, False, 0, 0)
        if focus:
            self.projection_view.grab_focus()

    def focus_view(self) -> None:
        """Focus the current presentation without changing its selected mode."""

        if self.mode == "rendered" and self.renderer is not None:
            self.renderer.view.grab_focus()
        elif self.mode in {"unified", "compact"}:
            self.projection_view.grab_focus()
        else:
            self.editor.view.grab_focus()

    def focus_editor(self) -> None:
        """Activate the appropriate editing surface when E is explicitly requested."""

        if self.mode == "rendered":
            self.set_mode("code")
        (self.projection_view if self.mode in {"unified", "compact"} else self.editor.view).grab_focus()

    def handle_key(self, event) -> bool:
        """Handle projection-specific shortcuts while save/undo remain canonical."""

        modifiers = event.state & Gtk.accelerator_get_default_mod_mask()
        if event.keyval == Gdk.KEY_F3 and modifiers in (0, Gdk.ModifierType.SHIFT_MASK):
            # 2026-11-06: la barra è un sibling del testo, quindi non riceve
            # F3 quando si torna nel diff. Instradare al buffer visibile senza
            # riaprire la ricerca o cambiare modalità.
            if self.mode in {"unified", "compact"}:
                return self.projection_search.handle_key(event)
            if (self.mode == "side" and self.last_side == "old") or (
                    self.mode == "code" and self.editor.historical):
                return self.old_search.handle_key(event)
            if self.mode in {"side", "code"}:
                self.editor.search(bool(modifiers & Gdk.ModifierType.SHIFT_MASK))
                return True
        if not event.state & Gdk.ModifierType.CONTROL_MASK:
            return False
        key = Gdk.keyval_to_lower(event.keyval)
        if key == Gdk.KEY_f and self.show_search():
            return True
        if self.mode in {"unified", "compact"} and key == Gdk.KEY_b:
            # 2026-11-04: il simbolo appartiene alla superficie visibile,
            # comprese righe rimosse; non usare il cursore del buffer nascosto.
            self.editor.go_to_definition(self.projection)
            return True
        if key == Gdk.KEY_b and ((self.mode == "side" and self.last_side == "old")
                                or getattr(self.editor, "historical", False)):
            self.editor.go_to_definition(self.old_buffer)
            return True
        if key == Gdk.KEY_g and self.mode == "rendered":
            self.set_mode("code")
        return False

    def _cancel_diff(self) -> None:
        """Cancel derived work and its timeout; late results are ignored when disabled."""

        self.job_serial += 1
        if self.timeout is not None:
            GLib.source_remove(self.timeout)
            self.timeout = None
        if self.command is not None:
            self.command.cancel()
            self.command = None

    def _view_mapped(self, _view) -> None:
        """Restart visible derived work after the original source view is remapped."""

        self.schedule()

    def _mapped(self, _editor) -> None:
        """Restart calculations when a previously hidden document is selected."""

        if self.enabled:
            self.syntax.queue()
            # 2026-10-30: Gtk.Stack conserva già la vista; rimapparla non deve
            # ricostruire il diff pronto né mostrarne uno stato intermedio.
            self.schedule()

    def _unmapped(self, _editor) -> None:
        """Keep hidden documents free of recalculation timers and workers."""

        self.syntax.pause()
        if self.first_scroll_id is not None:
            GLib.source_remove(self.first_scroll_id)
            self.first_scroll_id = None
        if self.debounce is not None:
            GLib.source_remove(self.debounce)
            self.debounce = None
        self._cancel_diff()

    def shutdown(self) -> None:
        """Release derived resources without saving or closing the canonical document."""

        if self.pending_navigation is not None:
            self.editor.buffer.delete_mark(self.pending_navigation[0])
            self.pending_navigation = None
        self.closed = True
        self.syntax.close()
        self._unmapped(self.editor)
        self.base_serial += 1
        if self.base_command is not None:
            self.base_command.cancel()
            self.base_command = None
        if self.base_timeout is not None:
            GLib.source_remove(self.base_timeout)
            self.base_timeout = None
        if self.renderer is not None:
            self.renderer.shutdown()
        for first_mark, last_mark in self.expanded_gap_marks:
            self.editor.buffer.delete_mark(first_mark)
            self.editor.buffer.delete_mark(last_mark)
        self.expanded_gap_marks.clear()
        self._delete_marks()
