"""Copy independent GtkSource syntax styles into editable unified diff projections."""

from operator import methodcaller

from gi.repository import GLib

from .editor_appearance import diff_colors


class DiffSyntaxHighlighter:
    """Keep both versions lexically separate while preserving diff and read-only tags."""

    PROPERTIES = ("foreground-rgba", "weight", "style", "underline", "strikethrough")

    def __init__(self, presentation) -> None:
        """Observe source highlighting without parsing the combined diff as one file."""

        self.presentation = presentation
        self.source = presentation.editor.buffer
        self.baseline = presentation.old_buffer
        self.projection = presentation.projection
        self.mode = "compact"
        self.closed = False
        self.pending_id = None
        self.parsing = False
        self.needs_parse = True
        self.parse_work = []
        self.runs = None
        self.styles_dirty = False
        self.tags = {}
        self.style_cache = {}
        self.connections = []
        # 2026-10-30: GtkSource esegue il parsing delle due versioni con i suoi
        # aggiornamenti incrementali. Copiare soltanto gli stili, mai il testo o
        # i tag editable, evita che stringhe/commenti rimossi contaminino il live.
        for buffer in (self.source, self.baseline):
            self.connections.append((buffer, buffer.connect("highlight-updated", self._highlight_updated)))
        for buffer in (self.source, self.baseline):
            self.connections.append((buffer, buffer.connect("changed", self._source_changed)))
        for property_name in ("notify::language", "notify::style-scheme"):
            self.connections.append((self.source, self.source.connect(property_name, self._settings_changed)))

    def configure(self, mode: str) -> None:
        """Match source language and theme, enabling copied styles only in Unified."""

        if mode != self.mode:
            self.needs_parse = True
        self.mode = mode
        language = self.source.get_language()
        scheme = self.source.get_style_scheme()
        if self.baseline.get_language() != language:
            self.baseline.set_language(language)
        if self.baseline.get_style_scheme() != scheme:
            self.baseline.set_style_scheme(scheme)
        self.baseline.set_highlight_syntax(language is not None and (
            mode in {"unified", "side"} or (mode == "code" and self.presentation.editor.historical)
        ))
        self.projection.set_highlight_syntax(False)
        self.projection.set_style_scheme(scheme)
        self.update_colors()
        if mode != "unified":
            self.pause()
            for tag in self.tags.values():
                self.projection.remove_tag(tag, self.projection.get_start_iter(), self.projection.get_end_iter())
        self.queue()

    def update_colors(self) -> None:
        """Update diff decorations without restarting language parsing or syntax copying."""

        # 2026-11-07: le preferenze di colore sono indipendenti dai lexer;
        # riusare i tag esistenti mantiene validi anche i job già in corso.
        preferences = self.presentation.editor.appearance
        scheme = self.source.get_style_scheme()
        colors = diff_colors(preferences, scheme, compact=self.mode == "compact")
        for kind, color in zip(("added", "removed"), colors):
            self.presentation.tags[kind].set_property("background", color)
            self.presentation.tags[kind].set_property("foreground", preferences.get("text_color") or "#172017")
            self.presentation.tags[kind].set_property("foreground-set", self.mode == "compact")
        self.presentation.new_tag.set_property("background", colors[0])
        self.presentation.old_tag.set_property("background", colors[1])

    def _settings_changed(self, _buffer, _property) -> None:
        """Propagate filename-language and source-theme changes to derived views."""

        if not self.closed:
            self.needs_parse = True
            self.configure(self.mode)

    def _highlight_updated(self, _buffer, _start, _end) -> None:
        """Coalesce asynchronous syntax changes from either independent version."""

        if not self.parsing and self.pending_id is not None:
            self.styles_dirty = True
        self.queue()

    def _source_changed(self, _buffer) -> None:
        """Refresh copied ranges after canonical edits have updated their projection marks."""

        self.needs_parse = True
        self.queue()

    def queue(self) -> None:
        """Coalesce bounded parsing and copying jobs only while Unified is visible."""

        if self.closed or self.mode != "unified" or not self.presentation.editor.get_mapped():
            return
        if self.needs_parse and not self.parsing:
            if self.pending_id is not None:
                GLib.source_remove(self.pending_id)
            self.runs = None
            self.parsing = True
            self.pending_id = GLib.idle_add(self._parse_next, priority=GLib.PRIORITY_LOW)
        elif self.pending_id is None:
            self.pending_id = GLib.idle_add(self._apply, priority=GLib.PRIORITY_LOW)

    def projection_changed(self) -> None:
        """Discard a style iterator after visual marks are rebuilt, retaining valid lexer work."""

        self.runs = None
        if not self.parsing and self.pending_id is not None:
            GLib.source_remove(self.pending_id)
            self.pending_id = None
        self.queue()

    def _parse_next(self) -> bool:
        """Highlight hidden source buffers in small chunks before copying their styles."""

        if self.closed or self.mode != "unified" or not self.presentation.editor.get_mapped():
            self.pending_id = None
            self.parsing = False
            return GLib.SOURCE_REMOVE
        if self.needs_parse:
            self.parse_work = [[self.source, 0], [self.baseline, 0]]
            self.needs_parse = False
        buffer, line = self.parse_work[0]
        # 2026-10-30: i buffer nascosti non garantiscono parsing della vista.
        # Forzare al massimo 200 righe per idle evita un ensure_highlight unico
        # su tutto il file, che potrebbe bloccare il main loop.
        if buffer.get_language() is not None and buffer.get_highlight_syntax():
            count = buffer.get_line_count()
            next_line = min(line + 200, count)
            end = buffer.get_end_iter() if next_line == count else buffer.get_iter_at_line(next_line)
            buffer.ensure_highlight(buffer.get_iter_at_line(line), end)
        else:
            next_line = buffer.get_line_count()
        if next_line >= buffer.get_line_count():
            self.parse_work.pop(0)
        else:
            self.parse_work[0][1] = next_line
        if self.parse_work:
            return GLib.SOURCE_CONTINUE
        self.pending_id = None
        self.parsing = False
        self.queue()
        return GLib.SOURCE_REMOVE

    def _apply(self) -> bool:
        """Copy syntax runs with a four-millisecond budget per main-loop iteration."""

        if self.closed or self.mode != "unified" or not self.presentation.editor.get_mapped():
            self.pending_id = None
            self.runs = None
            return GLib.SOURCE_REMOVE
        if self.runs is None:
            self.style_cache.clear()
            self.styles_dirty = False
            for tag in self.tags.values():
                self.projection.remove_tag(tag, self.projection.get_start_iter(), self.projection.get_end_iter())
            self.runs = self._iter_runs()
        deadline = GLib.get_monotonic_time() + 4000
        try:
            for _index in range(300):
                source_tag, left, right = next(self.runs)
                if source_tag is not None:
                    self._copy_style(source_tag, left, right)
                if GLib.get_monotonic_time() >= deadline:
                    return GLib.SOURCE_CONTINUE
        except StopIteration:
            # I tag sovrapposti conservano la priorità delle versioni originali;
            # i tag del diff non impongono foreground né proprietà editable.
            first_priority = self.projection.get_tag_table().get_size() - len(self.tags)
            for priority, source_tag in enumerate(sorted(self.tags, key=methodcaller("get_priority")), first_priority):
                self.tags[source_tag].set_priority(priority)
            self.pending_id = None
            self.runs = None
            if self.styles_dirty:
                self.queue()
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE

    def _iter_runs(self):
        """Yield independent source styles mapped through the latest canonical and visual marks."""

        for span in self.presentation.spans:
            # 2026-11-03: anche righe senza stili e span omessi consumano
            # tempo; restituire un checkpoint rende effettivo il budget idle.
            yield None, 0, 0
            if span.visual_start is None or span.visual_start.get_deleted():
                continue
            if span.start is not None:
                buffer = self.source
                start = buffer.get_iter_at_mark(span.start)
                end = buffer.get_iter_at_mark(span.end)
            elif span.kind == "removed" and span.old_line is not None:
                buffer = self.baseline
                start = buffer.get_iter_at_line(span.old_line)
                end = buffer.get_iter_at_offset(start.get_offset() + len(span.text))
            else:
                continue
            source_start = start.get_offset()
            visual_start = self.projection.get_iter_at_mark(span.visual_start).get_offset()
            visual_end = self.projection.get_iter_at_mark(span.visual_end).get_offset()
            while start.compare(end) < 0:
                # Avanzare per intervalli di tag, non per carattere.
                following = start.copy()
                following.forward_to_tag_toggle(None)
                if following.compare(end) > 0:
                    following = end.copy()
                if following.compare(start) <= 0:
                    break
                left = visual_start + start.get_offset() - source_start
                right = min(visual_end, visual_start + following.get_offset() - source_start)
                for source_tag in start.get_tags():
                    yield source_tag, left, right
                start = following
                yield None, 0, 0

    def _copy_style(self, source_tag, left: int, right: int) -> None:
        """Copy only explicit syntax foreground and font properties, preserving diff semantics."""

        if left >= right:
            return
        if source_tag in self.style_cache:
            tag = self.style_cache[source_tag]
            if tag is None:
                return
        else:
            properties = {}
            for name in self.PROPERTIES:
                flag = "foreground-set" if name == "foreground-rgba" else name + "-set"
                if source_tag.get_property(flag):
                    properties[name] = source_tag.get_property(name)
            if not properties:
                self.style_cache[source_tag] = None
                return
            tag = self.tags.get(source_tag)
            if tag is None:
                tag = self.projection.create_tag(None)
                self.tags[source_tag] = tag
            for name in self.PROPERTIES:
                flag = "foreground-set" if name == "foreground-rgba" else name + "-set"
                tag.set_property(flag, False)
            for name, value in properties.items():
                tag.set_property(name, value)
            self.style_cache[source_tag] = tag
        self.projection.apply_tag(tag, self.projection.get_iter_at_offset(left), self.projection.get_iter_at_offset(right))

    def pause(self) -> None:
        """Cancel queued style copying while the editor is hidden."""

        if self.pending_id is not None:
            GLib.source_remove(self.pending_id)
            self.pending_id = None
        self.parsing = False
        self.needs_parse = True
        self.runs = None
        self.parse_work.clear()

    def close(self) -> None:
        """Disconnect highlighting signals and cancel pending work before buffer destruction."""

        self.closed = True
        self.pause()
        for buffer, identifier in self.connections:
            buffer.disconnect(identifier)
        self.connections.clear()
        self.tags.clear()
        self.style_cache.clear()
