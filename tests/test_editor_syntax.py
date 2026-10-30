"""Verify independent syntax styles in Unified and Side by side without GTK windows."""

import tempfile
import fnmatch
import tomllib
from pathlib import Path
import time
import unittest
from unittest.mock import MagicMock, patch

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("GtkSource", "4")
from gi.repository import GLib, Gtk, GtkSource

from slate.editor import EditorDocument
from slate.editor_diff_model import project_diff
from slate.experimental_editor import ExperimentalEditor


class DiffSyntaxTest(unittest.TestCase):
    """Exercise actual GtkSource lexical contexts and copied foreground styles."""

    @classmethod
    def setUpClass(cls) -> None:
        """Require the isolated test display without constructing a window."""

        if not Gtk.init_check(None)[0]:
            raise unittest.SkipTest("GTK display unavailable")

    def setUp(self) -> None:
        """Build separate Python versions whose combined lexical state would be wrong."""

        self.temp = tempfile.TemporaryDirectory(prefix="slate-syntax-", dir="/tmp")
        self.editor = EditorDocument("project", self.temp.name, "snippet.py", 11, MagicMock(), MagicMock())
        GLib.source_remove(self.editor.initial_load_id)
        self.editor.initial_load_id = None
        self.before = '"""unfinished removed string\nremoved_word\n'
        self.current = 'value = 14\n# current comment\n'
        self.editor.buffer.set_text(self.current)
        self.editor._configure_language()
        self.view = ExperimentalEditor(self.editor)
        self.editor.experimental = self.view
        self.view.base = self.before
        self.view.old_buffer.set_text(self.before)
        self.view.rows = project_diff(self.before, self.current)
        self.view.rows_serial = self.view.serial
        self.view.rows_base_serial = self.view.base_serial
        self.view.attach()
        self.mapped = patch.object(self.editor, "get_mapped", return_value=True)
        self.mapped.start()
        self.schedule = patch.object(self.view, "schedule")
        self.schedule.start()
        self.view.set_mode("unified")

    def tearDown(self) -> None:
        """Disconnect syntax signals before destroying private test buffers."""

        self.editor.close()
        self.editor.destroy()
        self.schedule.stop()
        self.mapped.stop()
        self.temp.cleanup()

    def _flush_syntax(self) -> None:
        """Dispatch the production parser and copier jobs without forcing test-only highlighting."""

        self.view.syntax.queue()
        context = GLib.MainContext.default()
        deadline = time.monotonic() + 2
        while context.pending() and time.monotonic() < deadline:
            context.iteration(False)
        self.assertIsNone(self.view.syntax.pending_id)

    def _foreground_at(self, buffer, offset: int):
        """Return the highest-priority explicit foreground at one source or diff character."""

        priority = -1
        foreground = None
        for tag in buffer.get_iter_at_offset(offset).get_tags():
            if tag.get_property("foreground-set") and tag.get_priority() > priority:
                priority = tag.get_priority()
                foreground = tag.get_property("foreground-rgba").to_string()
        return foreground

    def test_unified_uses_independent_lexers_for_removed_and_current_text(self) -> None:
        """An unfinished removed string cannot color current identifiers, numbers or comments."""

        self._flush_syntax()
        projection = self.view.projection
        visual = projection.get_text(projection.get_start_iter(), projection.get_end_iter(), True)
        self.assertIsNone(projection.get_language())
        self.assertFalse(projection.get_highlight_syntax())
        for token in ("value", "14", "# current"):
            self.assertEqual(self._foreground_at(projection, visual.index(token)),
                             self._foreground_at(self.editor.buffer, self.current.index(token)))
        removed_color = self._foreground_at(self.view.old_buffer, self.before.index("removed_word"))
        self.assertIsNotNone(removed_color)
        self.assertEqual(self._foreground_at(projection, visual.index("removed_word")), removed_color)
        self.assertIsNotNone(self._foreground_at(projection, visual.index("14")))
        self.assertFalse(self.view.tags["added"].get_property("foreground-set"))
        self.assertFalse(self.view.tags["removed"].get_property("foreground-set"))
        self.assertFalse(self.view.tags["removed"].get_property("editable"))

    def test_live_edits_recolor_without_replacing_source_or_importing_history(self) -> None:
        """Style copying follows live marks after a number is replaced by a string."""

        self._flush_syntax()
        source = self.editor.buffer
        projection = self.view.projection
        visual = projection.get_text(projection.get_start_iter(), projection.get_end_iter(), True)
        offset = visual.index("14")
        projection.delete(projection.get_iter_at_offset(offset), projection.get_iter_at_offset(offset + 2))
        projection.insert(projection.get_iter_at_offset(offset), '"new string"')
        self._flush_syntax()
        current = self.view.text()
        visual = projection.get_text(projection.get_start_iter(), projection.get_end_iter(), True)
        self.assertIs(self.editor.buffer, source)
        self.assertNotIn("removed_word", current)
        self.assertEqual(self._foreground_at(projection, visual.index("new string")),
                         self._foreground_at(source, current.index("new string")))
        self.assertIsNotNone(self._foreground_at(projection, visual.index("new string")))

    def test_compact_clears_syntax_and_unified_restores_it(self) -> None:
        """Reusing the projection buffer never leaks syntax coloring into Compact."""

        self._flush_syntax()
        self.view.set_mode("compact")
        projection = self.view.projection
        visual = projection.get_text(projection.get_start_iter(), projection.get_end_iter(), True)
        self.assertFalse(projection.get_highlight_syntax())
        self.assertTrue(self.view.tags["added"].get_property("foreground-set"))
        copied_tags = set(self.view.syntax.tags.values())
        for token in ("14", "removed_word", "# current"):
            self.assertFalse(copied_tags.intersection(projection.get_iter_at_offset(visual.index(token)).get_tags()))
        self.view.set_mode("unified")
        self._flush_syntax()
        visual = projection.get_text(projection.get_start_iter(), projection.get_end_iter(), True)
        self.assertEqual(self._foreground_at(projection, visual.index("14")),
                         self._foreground_at(self.editor.buffer, self.current.index("14")))

    def test_side_by_side_keeps_native_syntax_on_both_versions_in_a_dark_theme(self) -> None:
        """Diff backgrounds preserve the native foreground styles and theme on both columns."""

        scheme = GtkSource.StyleSchemeManager.get_default().get_scheme("oblivion")
        self.assertIsNotNone(scheme)
        self.editor.buffer.set_style_scheme(scheme)
        self.view.set_mode("side")
        # Le GtkSourceView visibili richiedono il parsing dei propri viewport.
        # Il test non apre finestre: simula quella richiesta su questi pochi byte.
        for buffer in (self.editor.buffer, self.view.old_buffer):
            buffer.ensure_highlight(buffer.get_start_iter(), buffer.get_end_iter())
        self._flush_syntax()
        self.assertEqual(self.view.old_buffer.get_language().get_id(), "python")
        self.assertEqual(self.view.old_buffer.get_style_scheme().get_id(), "oblivion")
        self.assertTrue(self.view.old_buffer.get_highlight_syntax())
        self.assertTrue(self.editor.buffer.get_highlight_syntax())
        self.assertIsNotNone(self._foreground_at(self.view.old_buffer, self.before.index("removed_word")))
        self.assertIsNotNone(self._foreground_at(self.editor.buffer, self.current.index("14")))
        self.assertFalse(self.view.new_tag.get_property("foreground-set"))
        self.assertFalse(self.view.old_tag.get_property("foreground-set"))
        rgba = self.view.new_tag.get_property("background-rgba")
        self.assertLess(rgba.red, 0.3)

    def test_user_colors_do_not_override_syntax_and_apply_to_all_diff_modes(self) -> None:
        """Plain text uses the custom color while native/copied token colors retain their theme."""

        buffer = self.editor.buffer
        self.editor.set_appearance({"style_scheme": "oblivion", "text_color": "#aabbcc",
                                    "diff_added_color": "#123456", "diff_removed_color": "#654321"})
        self._flush_syntax()
        visual = self.view.projection.get_text(
            self.view.projection.get_start_iter(), self.view.projection.get_end_iter(), True)
        self.assertIs(self.editor.buffer, buffer)
        self.assertEqual(buffer.get_style_scheme().get_id(), "oblivion")
        self.assertEqual(self._foreground_at(self.view.projection, visual.index("14")),
                         self._foreground_at(buffer, self.current.index("14")))
        self.assertFalse(self.view.tags["added"].get_property("foreground-set"))
        for view in (self.editor.view, self.view.old_view, self.view.projection_view):
            color = view.get_style_context().get_color(Gtk.StateFlags.NORMAL)
            self.assertAlmostEqual(color.red, 0xaa / 255, places=3)
        for mode in ("compact", "side", "unified"):
            self.view.set_mode(mode)
            added = self.view.tags["added"].get_property("background-rgba")
            removed = self.view.tags["removed"].get_property("background-rgba")
            self.assertAlmostEqual(added.red, 0x12 / 255, places=3)
            self.assertAlmostEqual(removed.red, 0x65 / 255, places=3)
        self.editor.set_appearance({})
        self.assertIsNone(self.editor.appearance["text_color"])
        self.assertIs(self.editor.buffer, buffer)

    def test_monospace_font_and_size_reach_every_code_view_without_affecting_syntax(self) -> None:
        """All native and projected views share the selected fixed-width family and size provider."""

        from slate.editor_appearance import editor_font_css, monospaced_font_families
        chosen = monospaced_font_families()[0]
        source = self.editor.buffer
        self.editor.set_appearance({"font_family": chosen, "style_scheme": "oblivion"})
        self.editor.set_font_size(17)
        self._flush_syntax()
        self.assertIs(self.editor.buffer, source)
        for view in (self.editor.view, self.view.old_view, self.view.projection_view):
            font = view.get_style_context().get_property("font", Gtk.StateFlags.NORMAL)
            self.assertIn(chosen, font.get_family())
        css = self.editor.font_provider.to_string()
        self.assertIn(chosen, css)
        self.assertIn("17pt", css)
        self.assertIsNotNone(self._foreground_at(source, self.current.index("14")))
        self.assertEqual(editor_font_css({"font_family": "missing-family-for-slate-test"}, 12),
                         b"textview { font-family: monospace; font-size: 12pt; }")
        proportional = next(family.get_name() for family in self.editor.view.get_pango_context().list_families()
                            if not family.is_monospace())
        self.assertEqual(editor_font_css({"font_family": proportional}, 12),
                         b"textview { font-family: monospace; font-size: 12pt; }")

    def test_bundled_intellij_theme_loads_without_the_personal_theme_directory(self) -> None:
        """The shipped XML is discoverable on a clean installation and included in package data."""

        from slate.editor_appearance import editor_scheme_manager
        project = Path(__file__).resolve().parent.parent
        bundled = project / "slate" / "styles"
        classic = GtkSource.StyleSchemeManager.get_default().get_scheme("classic")
        manager = GtkSource.StyleSchemeManager()
        manager.set_search_path([str(bundled), str(Path(classic.get_filename()).parent)])
        scheme = manager.get_scheme("intellij-light")
        self.assertIsNotNone(scheme)
        self.assertEqual(Path(scheme.get_filename()), bundled / "intellij-light.xml")
        self.assertEqual(scheme.get_style("php:variable").get_property("foreground").lower(), "#660000")
        self.assertEqual(scheme.get_style("def:string").get_property("foreground").lower(), "#067d17")
        self.assertTrue(scheme.get_style("def:comment").get_property("italic"))
        metadata = tomllib.loads((project / "pyproject.toml").read_text())
        patterns = metadata["tool"]["setuptools"]["package-data"]["slate"]
        self.assertTrue(any(fnmatch.fnmatch("styles/intellij-light.xml", pattern) for pattern in patterns))
        shared = editor_scheme_manager()
        editor_scheme_manager()
        self.assertEqual(shared.get_search_path().count(str(bundled)), 1)

    def test_hidden_lexers_are_parsed_in_bounded_line_chunks(self) -> None:
        """Large versions yield between parser chunks instead of forcing the entire file at once."""

        current = "value = 14\n" * 600
        self.editor.buffer.set_text(current)
        self.view.rows = project_diff(self.before, current)
        self.view.rows_serial = self.view.serial
        self.view.rows_base_serial = self.view.base_serial
        self.view._replace_spans()
        self.view._render_projection()
        with patch.object(self.editor.buffer, "ensure_highlight", wraps=self.editor.buffer.ensure_highlight) as highlight:
            self._flush_syntax()
        self.assertGreaterEqual(highlight.call_count, 4)
        for call in highlight.call_args_list:
            start, end = call.args
            self.assertLessEqual(end.get_line() - start.get_line(), 200)
        visual = self.view.projection.get_text(
            self.view.projection.get_start_iter(), self.view.projection.get_end_iter(), True)
        self.assertIsNotNone(self._foreground_at(self.view.projection, visual.index("14")))

    def test_hidden_views_cancel_pending_style_work(self) -> None:
        """Highlight updates never keep a hidden editor doing derived work."""

        self.view.syntax.queue()
        self.assertIsNotNone(self.view.syntax.pending_id)
        self.view._unmapped(self.editor)
        self.assertIsNone(self.view.syntax.pending_id)
        with patch.object(self.editor, "get_mapped", return_value=False):
            self.view.syntax.queue()
        self.assertIsNone(self.view.syntax.pending_id)
