"""Internal diff navigation backed by SLATE's persistent editor document."""

from __future__ import annotations

from functools import partial
from typing import Callable

import gi

gi.require_version("Gdk", "3.0")
gi.require_version("Gtk", "3.0")
gi.require_version("GtkSource", "4")
from gi.repository import Gdk, Gtk, GtkSource, Pango  # noqa: E402

from .editor import EditorDocument
from .text_search import TextSearchBar


class DiffEditorSurface(Gtk.Box):
    """Show compact diff editing over workbench columns one and two."""

    def __init__(
        self,
        editor: EditorDocument,
        path: str,
        diff_text: str,
        edit_lines: tuple[int | None, ...],
        initial_diff_line: int,
    ) -> None:
        """Build the two-level diff/editor surface for one persistent file."""

        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.get_style_context().add_class("file-preview")
        self.editor = editor
        self.path = path
        self.edit_lines = edit_lines
        self.diff_lines = tuple(diff_text.splitlines(keepends=True))
        self.search_bars: dict[Gtk.Widget, TextSearchBar] = {}
        self.cleanup_started = False
        self.editor_key_id = 0
        self.editor_parent = editor.get_parent()
        self.editor_info_visible = editor.info_bar.get_visible()
        self.editor_child_name = (
            self.editor_parent.child_get_property(editor, "name")
            if isinstance(self.editor_parent, Gtk.Stack)
            else None
        )
        # 2026-09-27: il segnale destroy consente di ripristinare l'editor
        # prima della distruzione dei figli senza sovrascrivere il vfunc GTK,
        # che può rientrare e bloccare il main loop durante il cambio revisione.
        self.connect("destroy", self._on_destroy)
        self.hunks = tuple(
            index
            for index, line in enumerate(diff_text.splitlines())
            if line.startswith("@@") and index < len(edit_lines) and edit_lines[index]
        )
        self.hunk_index = self._initial_hunk(initial_diff_line)
        self.initial_target_line = (
            edit_lines[initial_diff_line]
            if initial_diff_line < len(edit_lines)
            and not self.diff_lines[initial_diff_line].startswith("@@")
            else None
        )
        self.connect("key-press-event", self._on_key_press)
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        path_label = Gtk.Label(label=path)
        path_label.set_xalign(0)
        path_label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        header.pack_start(path_label, True, True, 8)
        self.position_label = Gtk.Label()
        self.pack_start(header, False, False, 0)

        self.diff_buffer = GtkSource.Buffer()
        self.diff_tags = {
            "added": self.diff_buffer.create_tag(
                "dialog-diff-added", foreground="#26a269"
            ),
            "removed": self.diff_buffer.create_tag(
                "dialog-diff-removed", foreground="#e01b24"
            ),
            "hunk": self.diff_buffer.create_tag(
                "dialog-diff-hunk",
                foreground="#1c71d8",
                weight=Pango.Weight.BOLD,
            ),
            "header": self.diff_buffer.create_tag(
                "dialog-diff-header", weight=Pango.Weight.BOLD
            ),
        }
        self.diff_view = self._source_view(self.diff_buffer, False)
        self.diff_view.connect("key-press-event", self._on_key_press)
        diff_box = self._view_with_search(self.diff_view, self.diff_buffer)
        self.diff_box = diff_box
        # 2026-09-27: spostare l'EditorDocument già registrato evita due layout
        # GtkSource simultanei sullo stesso buffer, causa di blocchi e signal storm.
        if self.editor_parent is not None:
            self.editor_parent.remove(editor)
        self.editor_key_id = editor.view.connect(
            "key-press-event", self._on_key_press
        )

        # 2026-09-27: la toolbar è divisa in tre settori reali: i controlli
        # preesistenti dell'editor restano a sinistra, la navigazione occupa e
        # centra lo spazio elastico, mentre salvataggio e chiusura restano a
        # destra. Questo evita sia overlay sia spostamenti dovuti ai path lunghi.
        self.editor_path_packing = editor.toolbar.query_child_packing(
            editor.path_label
        )
        editor.toolbar.set_child_packing(
            editor.path_label, False, False, 0, Gtk.PackType.START
        )
        self.previous_button = self._action_button(
            "Previous", "go-previous", self._on_previous_clicked
        )
        self.next_button = self._action_button(
            "Next", "go-next", self._on_next_clicked
        )
        self.save_close_button = self._action_button(
            "Save & Close", "document-save", self._on_save_close_clicked
        )
        self.close_button = self._action_button(
            None, "window-close", self._on_close_clicked
        )
        self.close_button.set_tooltip_text("Close")
        self.close_button.get_accessible().set_name("Close")
        self.close_button.get_style_context().add_class("destructive-action")
        self.position_label.get_style_context().add_class(
            "diff-change-position"
        )
        self.position_style = Gtk.CssProvider()
        self.position_style.load_from_data(
            b".diff-change-position { color: #000000; font-size: 12pt; "
            b"font-weight: 700; }"
        )
        self.position_label.get_style_context().add_provider(
            self.position_style,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )
        self.navigation_box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=6
        )
        self.navigation_box.set_halign(Gtk.Align.CENTER)
        self.navigation_box.set_valign(Gtk.Align.CENTER)
        self.navigation_box.pack_start(
            self.previous_button, False, False, 0
        )
        self.navigation_box.pack_start(
            self.position_label, False, False, 4
        )
        self.navigation_box.pack_start(self.next_button, False, False, 0)
        self.navigation_sector = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=0
        )
        self.navigation_sector.set_halign(Gtk.Align.FILL)
        self.navigation_sector.pack_start(
            self.navigation_box, True, False, 0
        )
        self.diff_action_box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=6
        )
        for button in (
            self.save_close_button,
            self.close_button,
        ):
            self.diff_action_box.pack_start(button, False, False, 0)
        editor.toolbar.pack_end(self.diff_action_box, False, False, 0)
        editor.toolbar.pack_start(self.navigation_sector, True, True, 0)

        self.split = Gtk.Paned.new(Gtk.Orientation.VERTICAL)
        # 2026-09-27: il diff cambia altezza a ogni hunk; shrink=True permette
        # al Paned di ridurlo subito senza attendere il ricalcolo della size
        # request del GtkSourceView che conteneva il frammento precedente.
        self.split.pack1(diff_box, resize=True, shrink=True)
        self.split.pack2(editor, resize=True, shrink=False)
        self.split.connect("size-allocate", self._on_split_size_allocate)
        self.pack_start(self.split, True, True, 0)
        self.show_all()
        editor.save_button.hide()
        self._show_hunk()

    @staticmethod
    def _action_button(
        label: str | None,
        icon_name: str,
        callback: Callable[[Gtk.Button], None],
    ) -> Gtk.Button:
        """Create one slim toolbar action with an optional text label."""

        button = Gtk.Button()
        button.set_relief(Gtk.ReliefStyle.NONE)
        content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        content.pack_start(
            Gtk.Image.new_from_icon_name(icon_name, Gtk.IconSize.BUTTON),
            False,
            False,
            0,
        )
        if label is not None:
            content.pack_start(Gtk.Label(label=label), False, False, 0)
            button.set_tooltip_text(label)
            button.get_accessible().set_name(label)
        button.add(content)
        button.connect("clicked", callback)
        return button

    def _populate_diff_buffer(self, text: str) -> None:
        """Render the copied unified diff with the preview's semantic colors."""

        self.diff_buffer.set_text(text)
        offset = 0
        # 2026-09-27: colorare la copia già caricata conserva la leggibilità
        # della preview senza rieseguire alcun comando SCM dentro il dialog.
        for line in text.splitlines(keepends=True):
            start = self.diff_buffer.get_iter_at_offset(offset)
            offset += len(line)
            end = self.diff_buffer.get_iter_at_offset(offset)
            tag = None
            if line.startswith("@@"):
                tag = self.diff_tags["hunk"]
            elif line.startswith(("diff ", "--- ", "+++ ")):
                tag = self.diff_tags["header"]
            elif line.startswith("+"):
                tag = self.diff_tags["added"]
            elif line.startswith("-"):
                tag = self.diff_tags["removed"]
            if tag is not None:
                self.diff_buffer.apply_tag(tag, start, end)

    @staticmethod
    def _source_view(
        buffer: GtkSource.Buffer, editable: bool
    ) -> GtkSource.View:
        """Create one consistently configured source surface."""

        view = GtkSource.View.new_with_buffer(buffer)
        view.set_editable(editable)
        view.set_cursor_visible(editable)
        view.set_monospace(True)
        view.set_show_line_numbers(editable)
        view.set_highlight_current_line(editable)
        view.set_wrap_mode(Gtk.WrapMode.NONE)
        return view

    def _view_with_search(
        self, view: GtkSource.View, buffer: GtkSource.Buffer
    ) -> Gtk.Box:
        """Wrap one source view with scrolling and local search controls."""

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroller.add(view)
        box.pack_start(scroller, True, True, 0)
        search = TextSearchBar(buffer, view)
        box.pack_end(search, False, False, 0)
        self.search_bars[view] = search
        return box

    def _initial_hunk(self, diff_line: int) -> int:
        """Choose the hunk containing or preceding the original preview row."""

        selected = 0
        for index, row in enumerate(self.hunks):
            if row > diff_line:
                break
            selected = index
        return selected

    def _show_hunk(self) -> None:
        """Scroll the complete diff and editor to the selected hunk."""

        count = len(self.hunks)
        self.position_label.set_text(
            f"Change {self.hunk_index + 1} of {count}" if count else "No changes"
        )
        self.previous_button.set_sensitive(count > 0 and self.hunk_index > 0)
        self.next_button.set_sensitive(count > 0 and self.hunk_index + 1 < count)
        if not count:
            self._populate_diff_buffer("No textual changes available.\n")
            return
        diff_line = self.hunks[self.hunk_index]
        next_hunk = (
            self.hunks[self.hunk_index + 1]
            if self.hunk_index + 1 < count
            else len(self.diff_lines)
        )
        # 2026-09-27: mostrare soltanto l'hunk attivo riduce l'ambiguità visiva;
        # Previous/Next sostituiscono il frammento usando il diff -U3 già caricato.
        self._populate_diff_buffer(
            "".join(self.diff_lines[diff_line:next_hunk])
        )
        self._resize_diff_pane(self.split.get_allocated_height())
        diff_iter = self.diff_buffer.get_start_iter()
        self.diff_buffer.place_cursor(diff_iter)
        self.diff_view.scroll_to_iter(diff_iter, 0.0, False, 0.0, 0.0)
        target = self.initial_target_line
        self.initial_target_line = None
        if target is None:
            # 2026-09-27: l'intestazione @@ punta all'inizio del contesto, non
            # alla modifica. La navigazione deve selezionare la prima vera riga
            # aggiunta/rimossa dell'hunk per allineare correttamente l'editor.
            target = next(
                (
                    self.edit_lines[index]
                    for index in range(diff_line + 1, next_hunk)
                    if self.diff_lines[index].startswith(("+", "-"))
                    and not self.diff_lines[index].startswith(("+++", "---"))
                    and self.edit_lines[index] is not None
                ),
                self.edit_lines[diff_line],
            )
        if target is not None:
            self.editor.goto_line_number(target)

    def _on_split_size_allocate(
        self, _split: Gtk.Paned, allocation: Gdk.Rectangle
    ) -> None:
        """Fit the current hunk while limiting the diff to 40% of the height."""

        self._resize_diff_pane(allocation.height)

    def _resize_diff_pane(self, available_height: int) -> None:
        """Apply the compact hunk height immediately from current allocation."""

        if available_height < 2:
            return
        _line_y, line_height = self.diff_view.get_line_yrange(
            self.diff_buffer.get_start_iter()
        )
        line_height = max(16, line_height)
        # 2026-09-27: l'hunk compatto deve occupare soltanto lo spazio utile;
        # 72 px mantengono usabile un frammento minimo, il 40% evita che un hunk
        # lungo sottragga troppo spazio all'editor sottostante.
        content_height = self.diff_buffer.get_line_count() * line_height + 12
        desired = min(
            max(72, content_height),
            max(1, int(available_height * 0.4)),
        )
        if self.split.get_position() != desired:
            self.split.set_position(desired)
        # 2026-09-27: set_position aggiorna subito il valore ma GTK può rinviare
        # l'allocazione fisica. La request esplicita e check_resize rendono
        # immediata anche la contrazione dopo un hunk più lungo.
        self.diff_box.set_size_request(-1, desired)
        self.split.check_resize()

    def _move_hunk(self, delta: int) -> None:
        """Move within bounded hunks without wrapping around the document."""

        if not self.hunks:
            return
        target = max(0, min(len(self.hunks) - 1, self.hunk_index + delta))
        if target == self.hunk_index:
            return
        self.hunk_index = target
        self._show_hunk()
        self.editor.view.grab_focus()

    def finish_before_navigation(self, callback: Callable[[], None]) -> bool:
        """Save a dirty shared editor before allowing another preview selection."""

        if not self.editor.dirty:
            self.destroy()
            return False
        self.editor.save(partial(self._on_navigation_saved, callback))
        return True

    def close_if_clean(self) -> bool:
        """Close on Escape only when the persistent document has no pending edit."""

        if self.editor.dirty:
            return False
        self.destroy()
        return True

    def _on_navigation_saved(
        self, callback: Callable[[], None], success: bool
    ) -> None:
        """Close and continue deferred navigation after a successful save."""

        if success:
            self.destroy()
            callback()

    def _on_key_press(self, widget: Gtk.Widget, event: Gdk.EventKey) -> bool:
        """Handle diff navigation, saving and focused local search shortcuts."""

        modifiers = event.state & Gtk.accelerator_get_default_mod_mask()
        if event.keyval == Gdk.KEY_Escape and modifiers == Gdk.ModifierType(0):
            return self.close_if_clean()
        if modifiers == Gdk.ModifierType.CONTROL_MASK:
            if event.keyval == Gdk.KEY_Left:
                self._move_hunk(-1)
                return True
            if event.keyval == Gdk.KEY_Right:
                self._move_hunk(1)
                return True
            if Gdk.keyval_to_lower(event.keyval) == Gdk.KEY_s:
                self.editor.save()
                return True
            if Gdk.keyval_to_lower(event.keyval) == Gdk.KEY_f:
                search = self.search_bars.get(widget)
                if search is not None:
                    search.show_search()
                    return True
                if widget is self.editor.view:
                    self.editor.show_search()
                    return True
        return False

    def _on_previous_clicked(self, _button: Gtk.Button) -> None:
        """Navigate to the previous hunk."""

        self._move_hunk(-1)

    def _on_next_clicked(self, _button: Gtk.Button) -> None:
        """Navigate to the next hunk."""

        self._move_hunk(1)

    def _on_close_clicked(self, _button: Gtk.Button) -> None:
        """Close only the transient view and retain the persistent editor."""

        self.destroy()

    def _on_save_close_clicked(self, _button: Gtk.Button) -> None:
        """Save through EditorDocument and close only after success."""

        self.editor.save(self._on_save_close_finished)

    def _on_save_close_finished(self, success: bool) -> None:
        """Close the dialog after a successful asynchronous save."""

        if success:
            self.destroy()

    def _on_destroy(self, _surface: Gtk.Widget) -> None:
        """Return the persistent editor to its stack before children are destroyed."""

        if getattr(self, "cleanup_started", False):
            return
        self.cleanup_started = True
        editor = getattr(self, "editor", None)
        editor_key_id = getattr(self, "editor_key_id", 0)
        editor_parent = getattr(self, "editor_parent", None)
        editor_child_name = getattr(self, "editor_child_name", None)
        split = getattr(self, "split", None)
        if editor is not None:
            toolbar = getattr(editor, "toolbar", None)
            if toolbar is not None:
                action_box = getattr(self, "diff_action_box", None)
                if action_box is not None and action_box.get_parent() is toolbar:
                    toolbar.remove(action_box)
                path_packing = getattr(self, "editor_path_packing", None)
                if path_packing is not None:
                    toolbar.set_child_packing(editor.path_label, *path_packing)
            navigation_sector = getattr(self, "navigation_sector", None)
            if (
                toolbar is not None
                and navigation_sector is not None
                and navigation_sector.get_parent() is toolbar
            ):
                toolbar.remove(navigation_sector)
            editor.save_button.show()
        if (
            editor is not None
            and editor_key_id
            and editor.view.handler_is_connected(editor_key_id)
        ):
            editor.view.disconnect(editor_key_id)
            self.editor_key_id = 0
        if (
            editor is not None
            and split is not None
            and editor.get_parent() is split
        ):
            split.remove(editor)
        if (
            editor is not None
            and isinstance(editor_parent, Gtk.Stack)
            and editor.get_parent() is None
            and editor_child_name
        ):
            editor_parent.add_named(editor, editor_child_name)
            editor_parent.set_visible_child(editor)
            editor.show_all()
            editor.info_bar.set_visible(
                getattr(self, "editor_info_visible", False)
            )
