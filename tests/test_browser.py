"""Tests for persistent and isolated WebKitGTK browser pages."""

import io
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import gi

gi.require_version("Gdk", "3.0")
gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, Gtk

from slate.browser import (
    BrowserManager,
    BrowserPage,
    WebKit2,
    normalize_browser_uri,
    responsive_fit_scale,
)


class _BrowserStack(Gtk.Stack):
    """Provide the terminal-return primitive expected by BrowserManager."""

    def __init__(self) -> None:
        """Create a stack containing one inert terminal placeholder."""

        super().__init__()
        self.terminal = Gtk.Box()
        self.add_named(self.terminal, "terminal")
        self.terminal.show()
        self.set_visible_child(self.terminal)

    def show_terminal(self) -> None:
        """Reveal the inert terminal placeholder after a browser closes."""

        self.set_visible_child(self.terminal)


class BrowserTest(unittest.TestCase):
    """Verify URI policy, lazy persistence and private browser ownership."""

    @classmethod
    def setUpClass(cls) -> None:
        """Require the GTK display used by WebKit widget integration."""

        initialized, _arguments = Gtk.init_check(None)
        if not initialized:
            raise unittest.SkipTest("display GTK non disponibile")

    def test_uri_normalization_prefers_http_only_for_local_hosts(self) -> None:
        """Missing schemes distinguish development servers from remote hosts."""

        self.assertEqual(
            normalize_browser_uri("localhost:8080/app"),
            "http://localhost:8080/app",
        )
        self.assertEqual(
            normalize_browser_uri("127.0.0.1:3000"),
            "http://127.0.0.1:3000",
        )
        self.assertEqual(
            normalize_browser_uri("example.com/docs"),
            "https://example.com/docs",
        )
        self.assertEqual(normalize_browser_uri("about:blank"), "about:blank")
        self.assertIsNone(normalize_browser_uri("file:///tmp/secret"))
        self.assertIsNone(normalize_browser_uri("javascript:alert(1)"))
        self.assertIsNone(normalize_browser_uri("   "))

    def test_responsive_fit_scale_never_enlarges_the_viewport(self) -> None:
        """Fit calculations preserve 100% or reduce both dimensions uniformly."""

        self.assertEqual(responsive_fit_scale(390, 844, 390, 844), 1.0)
        self.assertEqual(responsive_fit_scale(1000, 1200, 390, 844), 1.0)
        self.assertAlmostEqual(
            responsive_fit_scale(384, 512, 768, 1024), 0.5
        )
        self.assertEqual(responsive_fit_scale(0, 512, 768, 1024), 1.0)

    def test_manager_creates_one_shared_persistent_context_lazily(self) -> None:
        """Normal pages share an explicit on-disk profile created on first use."""

        with tempfile.TemporaryDirectory() as directory:
            stack = _BrowserStack()
            collections = MagicMock()
            manager = BrowserManager(
                stack,
                collections,
                MagicMock(),
                MagicMock(),
                Path(directory) / "data",
                Path(directory) / "cache",
            )
            self.assertIsNone(manager.context)
            first = manager.open_page("first")
            second = manager.open_page("second")
            self.assertIsNotNone(first)
            self.assertIsNotNone(second)
            self.assertFalse(manager.context.is_ephemeral())
            self.assertTrue(manager.data_directory.is_dir())
            self.assertTrue(manager.cache_directory.is_dir())
            self.assertTrue(
                manager.data_manager.get_persistent_credential_storage_enabled()
            )
            self.assertIs(first.page.web_view.get_context(), manager.context)
            self.assertIs(second.page.web_view.get_context(), manager.context)
            self.assertEqual(collections.call_count, 2)
            self.assertIs(manager.current_page(), second.page)
            manager.shutdown()

    def test_restore_keeps_normal_browser_rows_lazy(self) -> None:
        """Restored metadata does not create a context, WebView or network load."""

        manager = BrowserManager(
            _BrowserStack(), MagicMock(), MagicMock(), MagicMock()
        )
        manager.restore(
            [
                {
                    "name": "repo",
                    "browsers": [
                        {
                            "id": "browser-4",
                            "url": "https://example.com/docs",
                            "title": "Docs",
                        }
                    ],
                }
            ]
        )
        entry = manager.pages[("repo", "browser-4")]
        self.assertIsNone(manager.context)
        self.assertIsNone(entry.page)
        self.assertEqual(entry.title, "Docs")
        manager.shutdown()

    def test_private_pages_use_distinct_ephemeral_contexts(self) -> None:
        """Anonymous tabs persist metadata while profiles remain isolated."""

        manager = BrowserManager(
            _BrowserStack(), MagicMock(), MagicMock(), MagicMock()
        )
        first = manager.open_page("repo", private=True)
        second = manager.open_page("repo", private=True)
        self.assertTrue(first.private)
        self.assertTrue(second.private)
        self.assertTrue(first.page.web_view.is_ephemeral())
        self.assertTrue(second.page.web_view.is_ephemeral())
        self.assertIsNot(first.context, second.context)
        self.assertEqual(
            manager.serialized_project("repo"),
            [
                {
                    "id": "browser-1",
                    "url": "about:blank",
                    "title": "Browser",
                    "private": True,
                },
                {
                    "id": "browser-2",
                    "url": "about:blank",
                    "title": "Browser",
                    "private": True,
                },
            ],
        )
        manager.shutdown()

    def test_restored_private_page_is_lazy_and_gets_fresh_context(self) -> None:
        """A restored anonymous tab creates a new ephemeral profile on selection."""

        manager = BrowserManager(
            _BrowserStack(), MagicMock(), MagicMock(), MagicMock()
        )
        manager.restore(
            [
                {
                    "name": "repo",
                    "browsers": [
                        {
                            "id": "browser-7",
                            "url": "https://example.com/private",
                            "title": "Private",
                            "private": True,
                        }
                    ],
                }
            ]
        )
        entry = manager.pages[("repo", "browser-7")]
        self.assertIsNone(entry.page)
        self.assertIsNone(entry.context)
        self.assertIsNone(manager.context)
        self.assertTrue(manager.show_page("repo", "browser-7"))
        self.assertTrue(entry.page.web_view.is_ephemeral())
        self.assertEqual(
            entry.page.web_view.get_uri(), "https://example.com/private"
        )
        self.assertIsNone(manager.context)
        manager.shutdown()

    def test_viewport_container_forces_preview_but_fills_desktop(self) -> None:
        """Real child allocations use the preset width only while responsive."""

        manager = BrowserManager(
            _BrowserStack(), MagicMock(), MagicMock(), MagicMock()
        )
        entry = manager.open_page("repo", private=True)
        page = entry.page
        self.assertIn(page.web_view, page.viewport_stage.get_children())
        page.responsive_menu_items["galaxy-a-series"].set_active(True)
        page._apply_responsive_layout(1000, 900)
        responsive_allocation = Gdk.Rectangle()
        responsive_allocation.width = 1000
        responsive_allocation.height = 900
        page.viewport_stage.size_allocate(responsive_allocation)
        self.assertEqual(page.viewport_stage.forced_size, (360, 800))
        self.assertEqual(page.web_view.get_allocation().width, 360)
        self.assertEqual(page.web_view.get_allocation().height, 800)
        page.responsive_menu_items["none"].set_active(True)
        page._apply_desktop_layout(1280, 720)
        desktop_allocation = Gdk.Rectangle()
        desktop_allocation.width = 1280
        desktop_allocation.height = 720
        page.viewport_stage.size_allocate(desktop_allocation)
        self.assertIsNone(page.viewport_stage.forced_size)
        self.assertEqual(page.web_view.get_allocation().width, 1280)
        self.assertEqual(page.web_view.get_allocation().height, 720)
        manager.shutdown()

    def test_new_window_policy_opens_externally_without_project_row(self) -> None:
        """A target-new navigation is ignored internally and launched outside."""

        page = SimpleNamespace(_open_external_uri=MagicMock(), on_error=MagicMock())
        request = MagicMock()
        request.get_uri.return_value = "https://example.com/popup"
        action = MagicMock()
        action.get_request.return_value = request
        action.is_user_gesture.return_value = True
        decision = MagicMock()
        decision.get_navigation_action.return_value = action
        handled = BrowserPage._on_decide_policy(
            page,
            object(),
            decision,
            WebKit2.PolicyDecisionType.NEW_WINDOW_ACTION,
        )
        self.assertTrue(handled)
        decision.ignore.assert_called_once_with()
        page._open_external_uri.assert_called_once_with(
            "https://example.com/popup"
        )

    def test_blank_named_window_is_preserved_for_wordpress_preview_post(self) -> None:
        """WordPress may submit its preview form only after opening a blank target."""

        popup = MagicMock()
        page = SimpleNamespace(
            _create_external_popup_bridge=MagicMock(return_value=popup)
        )
        request = MagicMock()
        request.get_uri.return_value = "about:blank"
        action = MagicMock()
        action.get_request.return_value = request
        opener = MagicMock()

        created = BrowserPage._on_create(page, opener, action)

        self.assertIs(created, popup)
        page._create_external_popup_bridge.assert_called_once_with(opener)

    def test_preview_bridge_shares_context_without_creating_a_slate_window(self) -> None:
        """The hidden bridge retains login state and owns no native popup window."""

        manager = BrowserManager(
            _BrowserStack(), MagicMock(), MagicMock(), MagicMock()
        )
        entry = manager.open_page("repo", private=True)
        page = entry.page
        popup_view = page._create_external_popup_bridge(page.web_view)
        self.assertIs(popup_view.get_context(), page.web_view.get_context())
        self.assertIn(popup_view, page.external_popup_bridges)
        page._release_external_popup_bridge(popup_view)
        self.assertNotIn(popup_view, page.external_popup_bridges)
        manager.shutdown()

    def test_preview_bridge_opens_final_redirect_in_default_browser(self) -> None:
        """Only the completed HTTP destination reaches xdg-open after the POST."""

        bridge = MagicMock()
        bridge.get_uri.return_value = "https://example.test/?preview=true"
        page = SimpleNamespace(
            _open_external_uri=MagicMock(),
            _release_external_popup_bridge=MagicMock(),
        )

        BrowserPage._on_external_popup_bridge_load_changed(
            page, bridge, WebKit2.LoadEvent.FINISHED
        )

        page._open_external_uri.assert_called_once_with(
            "https://example.test/?preview=true"
        )
        page._release_external_popup_bridge.assert_called_once_with(bridge)

    def test_blank_new_window_policy_reaches_the_create_handler(self) -> None:
        """The initial blank target is used instead of being discarded externally."""

        page = SimpleNamespace(_open_external_uri=MagicMock(), on_error=MagicMock())
        request = MagicMock()
        request.get_uri.return_value = "about:blank"
        action = MagicMock()
        action.get_request.return_value = request
        decision = MagicMock()
        decision.get_navigation_action.return_value = action

        handled = BrowserPage._on_decide_policy(
            page,
            object(),
            decision,
            WebKit2.PolicyDecisionType.NEW_WINDOW_ACTION,
        )

        self.assertTrue(handled)
        decision.use.assert_called_once_with()
        decision.ignore.assert_not_called()
        page._open_external_uri.assert_not_called()

    def test_blob_navigation_remains_inside_webview_for_gutenberg_canvas(self) -> None:
        """A page-owned blob may initialize Gutenberg's authenticated iframe."""

        page = SimpleNamespace(_open_external_uri=MagicMock(), on_error=MagicMock())
        request = MagicMock()
        request.get_uri.return_value = "blob:https://example.test/editor-document"
        action = MagicMock()
        action.get_request.return_value = request
        decision = MagicMock()
        decision.get_navigation_action.return_value = action

        handled = BrowserPage._on_decide_policy(
            page,
            object(),
            decision,
            WebKit2.PolicyDecisionType.NAVIGATION_ACTION,
        )

        self.assertFalse(handled)
        decision.ignore.assert_not_called()
        page._open_external_uri.assert_not_called()

    def test_blocked_navigation_reports_the_complete_escaped_uri(self) -> None:
        """Foreground diagnostics retain the rejected URI on one safe log line."""

        page = SimpleNamespace(_open_external_uri=MagicMock(), on_error=MagicMock())
        request = MagicMock()
        request.get_uri.return_value = "javascript:secretToken()"
        action = MagicMock()
        action.get_request.return_value = request
        action.is_user_gesture.return_value = False
        decision = MagicMock()
        decision.get_navigation_action.return_value = action
        stderr = io.StringIO()

        with redirect_stderr(stderr):
            handled = BrowserPage._on_decide_policy(
                page,
                object(),
                decision,
                WebKit2.PolicyDecisionType.NAVIGATION_ACTION,
            )

        self.assertTrue(handled)
        self.assertEqual(
            stderr.getvalue(),
            "WebKit: blocked navigation (page; scheme: javascript; "
            "URL: 'javascript:secretToken()').\n",
        )
        self.assertIn("secretToken", stderr.getvalue())

    def test_browser_shortcuts_are_scoped_to_visible_page(self) -> None:
        """Navigation shortcuts act on the browser and ignore the terminal page."""

        stack = _BrowserStack()
        manager = BrowserManager(stack, MagicMock(), MagicMock(), MagicMock())
        ctrl_l = SimpleNamespace(
            keyval=Gdk.KEY_l,
            state=Gdk.ModifierType.CONTROL_MASK,
        )
        self.assertFalse(manager.handle_key(ctrl_l))
        entry = manager.open_page("repo", private=True)
        entry.page.focus_location = MagicMock()
        entry.page.reload = MagicMock()
        entry.page.reload_bypass_cache = MagicMock()
        self.assertTrue(manager.handle_key(ctrl_l))
        entry.page.focus_location.assert_called_once_with()
        reload_event = SimpleNamespace(
            keyval=Gdk.KEY_F5,
            state=Gdk.ModifierType(0),
        )
        self.assertTrue(manager.handle_key(reload_event))
        entry.page.reload.assert_called_once_with()
        hard_reload_event = SimpleNamespace(
            keyval=Gdk.KEY_r,
            state=(
                Gdk.ModifierType.CONTROL_MASK
                | Gdk.ModifierType.SHIFT_MASK
            ),
        )
        self.assertTrue(manager.handle_key(hard_reload_event))
        entry.page.reload_bypass_cache.assert_called_once_with()
        manager.shutdown()

    def test_reload_on_bell_is_runtime_only_and_exclusive_per_project(self) -> None:
        """One project owns one unsaved BELL target while others remain independent."""

        manager = BrowserManager(
            _BrowserStack(), MagicMock(), MagicMock(), MagicMock()
        )
        first = manager.open_page("repo", private=True)
        second = manager.open_page("repo", private=True)
        other = manager.open_page("other", private=True)
        first.page.reload_on_bell_check.set_active(True)
        self.assertIs(manager.reload_on_bell_target("repo"), first)
        second.page.reload_on_bell_check.set_active(True)
        self.assertFalse(first.reload_on_bell)
        self.assertFalse(first.page.reload_on_bell_check.get_active())
        self.assertIs(manager.reload_on_bell_target("repo"), second)
        other.page.reload_on_bell_check.set_active(True)
        self.assertIs(manager.reload_on_bell_target("repo"), second)
        self.assertIs(manager.reload_on_bell_target("other"), other)
        self.assertNotIn(
            "reload_on_bell", manager.serialized_project("repo")[0]
        )
        manager.shutdown()

    def test_clear_site_data_excludes_cookies_and_filters_the_clicked_host(self) -> None:
        """Website cleanup removes only matching non-cookie records then hard-reloads."""

        manager = BrowserManager(
            _BrowserStack(), MagicMock(), MagicMock(), MagicMock()
        )
        page = manager.open_page("repo", private=True).page
        data_manager = MagicMock()
        page.web_view = MagicMock()
        page.web_view.get_uri.return_value = "https://app.example.test/path"
        page.web_view.get_website_data_manager.return_value = data_manager
        with patch("slate.browser.Gtk.MessageDialog") as dialog_type:
            dialog = dialog_type.return_value
            dialog.run.return_value = Gtk.ResponseType.ACCEPT
            dialog.add_button.return_value = MagicMock()
            page._on_clear_site_data_activate(page.clear_site_data_item)
        fetched_types = data_manager.fetch.call_args.args[0]
        self.assertEqual(
            int(fetched_types & WebKit2.WebsiteDataTypes.COOKIES), 0
        )
        matching = MagicMock()
        matching.get_name.return_value = "app.example.test"
        unrelated = MagicMock()
        unrelated.get_name.return_value = "other.example.test"
        data_manager.fetch_finish.return_value = [matching, unrelated]
        page.web_view.get_uri.return_value = "https://other.example.test/"
        page._on_site_data_fetched(data_manager, MagicMock())
        self.assertEqual(data_manager.remove.call_args.args[1], [matching])
        page.reload_bypass_cache = MagicMock()
        data_manager.remove_finish.return_value = True
        page._on_site_data_removed(data_manager, MagicMock())
        page.reload_bypass_cache.assert_called_once_with()
        manager.shutdown()

    def test_responsive_menu_starts_inactive_and_applies_each_preset(self) -> None:
        """The toolbar menu enters a viewport and can restore Desktop."""

        manager = BrowserManager(
            _BrowserStack(),
            MagicMock(),
            MagicMock(),
            MagicMock(),
        )
        entry = manager.open_page("repo", private=True)
        page = entry.page
        self.assertTrue(page.responsive_menu_items["none"].get_active())
        self.assertIsNone(page.responsive_viewport)
        self.assertEqual(page.responsive_button.get_label(), "Responsive")
        self.assertFalse(page.exit_responsive_button.get_visible())
        page.responsive_menu_items["ipad-classic"].set_active(True)
        page._apply_responsive_layout(792, 1048)
        self.assertEqual(page.responsive_viewport, (768, 1024))
        self.assertEqual(page.responsive_label.get_text(), "768 × 1024 · 100%")
        self.assertEqual(
            page.responsive_button.get_label(), "Responsive · 768×1024"
        )
        self.assertTrue(page.exit_responsive_button.get_visible())
        self.assertEqual(page.viewport_stage.forced_size, (768, 1024))
        page.responsive_menu_items["iphone-x"].set_active(True)
        page._apply_responsive_layout(408, 536)
        self.assertEqual(page.responsive_viewport, (375, 812))
        self.assertEqual(page.viewport_stage.forced_size, (236, 512))
        self.assertAlmostEqual(page.web_view.get_zoom_level(), 512 / 812)
        page.exit_responsive_button.clicked()
        self.assertIsNone(page.responsive_viewport)
        self.assertEqual(page.responsive_button.get_label(), "Responsive")
        self.assertFalse(page.exit_responsive_button.get_visible())
        self.assertEqual(page.web_view.get_zoom_level(), 1.0)
        manager.shutdown()

    def test_escape_leaves_responsive_only_when_not_loading(self) -> None:
        """Escape preserves its Stop priority, then becomes the quick exit."""

        manager = BrowserManager(
            _BrowserStack(), MagicMock(), MagicMock(), MagicMock()
        )
        page = SimpleNamespace(
            web_view=SimpleNamespace(is_loading=MagicMock(return_value=False)),
            responsive_viewport=(360, 800),
            disable_responsive=MagicMock(),
        )
        manager.current_page = MagicMock(return_value=page)
        escape_event = SimpleNamespace(keyval=Gdk.KEY_Escape, state=0)
        self.assertTrue(manager.handle_key(escape_event))
        page.disable_responsive.assert_called_once_with()
        manager.shutdown()

    def test_inspector_shortcut_is_scoped_to_visible_browser(self) -> None:
        """F12 is consumed only while a browser owns the central workspace."""

        stack = _BrowserStack()
        manager = BrowserManager(stack, MagicMock(), MagicMock(), MagicMock())
        inactive_event = SimpleNamespace(keyval=Gdk.KEY_F12, state=0)
        self.assertFalse(manager.handle_key(inactive_event))
        entry = manager.open_page("repo", private=True)
        entry.page.toggle_inspector = MagicMock()
        self.assertTrue(manager.handle_key(inactive_event))
        entry.page.toggle_inspector.assert_called_once_with()
        manager.shutdown()

    def test_inspector_toolbar_button_opens_and_closes_with_a_colored_icon(self) -> None:
        """The DevTools toggle mirrors both actions and uses the bundled asset."""

        manager = BrowserManager(
            _BrowserStack(), MagicMock(), MagicMock(), MagicMock()
        )
        page = manager.open_page("repo", private=True).page
        page.inspector = MagicMock()
        self.assertEqual(
            page.inspector_button.get_image().get_storage_type(),
            Gtk.ImageType.PIXBUF,
        )
        page.inspector_button.set_active(True)
        page.inspector.show.assert_called_once_with()
        page.inspector_button.set_active(False)
        page.inspector.close.assert_called_once_with()
        manager.shutdown()

    def test_close_returns_to_terminal_and_forgets_runtime_page(self) -> None:
        """Closing the selected browser releases it without persistent state."""

        stack = _BrowserStack()
        collections = MagicMock()
        manager = BrowserManager(stack, collections, MagicMock(), MagicMock())
        entry = manager.open_page("repo", private=True)
        self.assertTrue(manager.close_page(entry.reference))
        self.assertEqual(manager.pages, {})
        self.assertIs(stack.get_visible_child(), stack.terminal)
        self.assertEqual(collections.call_count, 2)
        manager.shutdown()


if __name__ == "__main__":
    unittest.main()
