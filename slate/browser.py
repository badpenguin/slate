"""Persistent WebKitGTK browser tabs with normal or ephemeral profiles."""

from __future__ import annotations

import ipaddress
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

import gi

gi.require_version("Gio", "2.0")
gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, Gio, GLib, Gtk  # noqa: E402

from .config import BROWSER_VIEWPORT_PRESETS


WEBKIT_DEPENDENCY_ERROR: str | None = None
try:
    gi.require_version("WebKit2", "4.1")
    from gi.repository import WebKit2  # noqa: E402
except (ImportError, ValueError) as error:
    # 2026-08-17: l'import resta tollerante per consentire al preflight di
    # mostrare un errore leggibile invece di interrompere Python con traceback.
    WebKit2 = None  # type: ignore[assignment]
    WEBKIT_DEPENDENCY_ERROR = str(error)


BrowserRef = tuple[str, str]
_BLOCKED_SCHEMES = {"about", "blob", "data", "file", "javascript"}
_RESPONSIVE_STAGE_PADDING = 24


def _report_blocked_navigation(uri: str, scheme: str, source: str) -> None:
    """Write one complete WebKit policy rejection to standard error."""

    # 2026-08-18: URL e query possono contenere nonce, token o dati inseriti
    # dall'utente; la stampa completa è stata richiesta esplicitamente.
    # repr conserva il valore ma impedisce ai caratteri di controllo di creare
    # righe di log ingannevoli o di nascondere parte della destinazione.
    safe_scheme = scheme or "missing"
    print(
        f"WebKit: blocked navigation ({source}; scheme: {safe_scheme}; "
        f"URL: {uri!r}).",
        file=sys.stderr,
    )


def responsive_fit_scale(
    available_width: int,
    available_height: int,
    viewport_width: int,
    viewport_height: int,
) -> float:
    """Return a scale no larger than 100% that fits one complete viewport."""

    if min(
        available_width,
        available_height,
        viewport_width,
        viewport_height,
    ) <= 0:
        return 1.0
    return min(
        1.0,
        available_width / viewport_width,
        available_height / viewport_height,
    )


def normalize_browser_uri(value: str) -> str | None:
    """Return one allowed browser URI, adding a practical missing scheme."""

    candidate = value.strip()
    if not candidate or any(character.isspace() for character in candidate):
        return None
    if candidate == "about:blank":
        return candidate
    if "://" in candidate:
        parsed = urlsplit(candidate)
        return (
            candidate
            if parsed.scheme.lower() in {"http", "https"} and parsed.hostname
            else None
        )
    authority = candidate.split("/", 1)[0]
    if not authority.startswith("[") and ":" in authority:
        _host_part, port = authority.rsplit(":", 1)
        if not port.isdigit():
            return None

    # 2026-08-17: i server di sviluppo locali usano normalmente HTTP, mentre
    # gli host remoti senza schema ricevono il default sicuro HTTPS.
    host = candidate.split("/", 1)[0].rsplit("@", 1)[-1]
    if host.startswith("[") and "]" in host:
        host_name = host[1 : host.index("]")]
    else:
        host_name = host.split(":", 1)[0]
    local = host_name.casefold() == "localhost" or host_name.casefold().endswith(
        ".localhost"
    )
    try:
        address = ipaddress.ip_address(host_name)
        local = address.is_private or address.is_loopback or address.is_link_local
    except ValueError:
        pass
    return f"{'http' if local else 'https'}://{candidate}"


@dataclass
class BrowserEntry:
    """Keep one browser identity available before its WebKit widget exists."""

    project_name: str
    identifier: str
    url: str = "about:blank"
    title: str = "Browser"
    private: bool = False
    reload_on_bell: bool = False
    page: BrowserPage | None = None
    context: object | None = None

    @property
    def reference(self) -> BrowserRef:
        """Return the stable project and browser identifier pair."""

        return self.project_name, self.identifier

    @property
    def display_title(self) -> str:
        """Distinguish private rows without relying on color alone."""

        return f"Incognito — {self.title}" if self.private else self.title


class _ResponsiveViewport(Gtk.Bin):
    """Allocate a WebView at an exact preview size or fill all available space."""

    def __init__(self) -> None:
        """Start in unconstrained Desktop mode without owning a GDK window."""

        super().__init__()
        self.forced_size: tuple[int, int] | None = None

    def set_forced_size(self, width: int | None, height: int | None) -> None:
        """Set an exact child allocation, or clear it to restore Desktop fill."""

        self.forced_size = (
            (width, height)
            if width is not None and height is not None
            else None
        )
        self.queue_resize()
        self.queue_allocate()

    def do_get_preferred_width(self) -> tuple[int, int]:
        """Let the surrounding workspace decide its width without child minima."""

        return 1, 1

    def do_get_preferred_height(self) -> tuple[int, int]:
        """Let the surrounding workspace decide its height without child minima."""

        return 1, 1

    def do_size_allocate(self, allocation: Gdk.Rectangle) -> None:
        """Force the child rectangle instead of treating its request as a minimum."""

        self.set_allocation(allocation)
        child = self.get_child()
        if child is None or not child.get_visible():
            return
        if self.forced_size is None:
            child_width = allocation.width
            child_height = allocation.height
            offset_x = 0
            offset_y = 0
        else:
            child_width, child_height = self.forced_size
            offset_x = max(0, (allocation.width - child_width) // 2)
            offset_y = max(0, (allocation.height - child_height) // 2)
        # 2026-08-17: GtkBox considera set_size_request una misura minima;
        # l'allocazione esplicita è necessaria perché innerWidth non resti Desktop.
        child_allocation = Gdk.Rectangle()
        child_allocation.x = allocation.x + offset_x
        child_allocation.y = allocation.y + offset_y
        child_allocation.width = child_width
        child_allocation.height = child_height
        child.size_allocate(child_allocation)


class BrowserPage(Gtk.Box):
    """Display one WebKit page with navigation, loading state and developer tools."""

    def __init__(
        self,
        entry: BrowserEntry,
        context: object,
        on_state_changed: Callable[["BrowserPage"], None],
        on_reload_on_bell_changed: Callable[["BrowserPage", bool], None],
        on_error: Callable[[str], None],
        web_view: object | None = None,
        load_initial: bool = True,
    ) -> None:
        """Build and start one page in its persistent or private context."""

        if WebKit2 is None:
            raise RuntimeError("WebKitGTK 4.1 is unavailable")
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.entry = entry
        self.project_name = entry.project_name
        self.identifier = entry.identifier
        self.on_state_changed = on_state_changed
        self.on_reload_on_bell_changed = on_reload_on_bell_changed
        self.on_error = on_error
        self.failed_loading = False
        self.inspector_open = False
        self._syncing_inspector = False
        self.closed = False
        self.external_popup_bridges: set[object] = set()
        self.site_data_cancellable: Gio.Cancellable | None = None
        self.site_data_types: object | None = None
        self.site_data_host: str | None = None
        self._syncing_reload_on_bell = False
        self.responsive_viewport: tuple[int, int] | None = None
        self.responsive_layout: tuple[int, int, int, int, float] | None = None
        self.desktop_layout: tuple[int, int] | None = None

        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        toolbar.get_style_context().add_class("browser-toolbar")
        self.back_button = self._icon_button("go-previous", "Back (Alt+Left)")
        self.forward_button = self._icon_button("go-next", "Forward (Alt+Right)")
        self.reload_button = self._icon_button(
            "view-refresh", "Reload (F5 / Ctrl+R)"
        )
        self.back_button.connect("clicked", self._on_back_clicked)
        self.forward_button.connect("clicked", self._on_forward_clicked)
        self.reload_button.connect("clicked", self._on_reload_clicked)
        toolbar.pack_start(self.back_button, False, False, 0)
        toolbar.pack_start(self.forward_button, False, False, 0)
        toolbar.pack_start(self.reload_button, False, False, 0)

        self.development_button = Gtk.MenuButton(label="Development")
        self.development_menu = Gtk.Menu()
        self.hard_reload_item = Gtk.MenuItem(
            label="Reload without cache"
        )
        self.hard_reload_item.set_tooltip_text(
            "Reload without cache (Ctrl+Shift+R)"
        )
        self.clear_site_data_item = Gtk.MenuItem(
            label="Clear site data (excluding cookies)…"
        )
        self.hard_reload_item.connect(
            "activate", self._on_hard_reload_activate
        )
        self.clear_site_data_item.connect(
            "activate", self._on_clear_site_data_activate
        )
        self.development_menu.append(self.hard_reload_item)
        self.development_menu.append(self.clear_site_data_item)
        self.development_menu.show_all()
        self.development_button.set_popup(self.development_menu)
        self.development_button.set_tooltip_text(
            "Development actions for cache and site data"
        )
        toolbar.pack_start(self.development_button, False, False, 0)

        self.reload_on_bell_check = Gtk.CheckButton(
            label="Reload on BELL"
        )
        self.reload_on_bell_check.set_active(entry.reload_on_bell)
        self.reload_on_bell_check.set_tooltip_text(
            "Reload this page when a project terminal emits BELL"
        )
        self.reload_on_bell_check.connect(
            "toggled", self._on_reload_on_bell_toggled
        )
        toolbar.pack_start(self.reload_on_bell_check, False, False, 0)

        self.uri_entry = Gtk.Entry()
        self.uri_entry.set_placeholder_text("Enter URL")
        self.uri_entry.set_tooltip_text("Location (Ctrl+L)")
        self.uri_entry.set_activates_default(False)
        self.uri_entry.connect("activate", self._on_uri_activated)
        toolbar.pack_start(self.uri_entry, True, True, 0)

        self.spinner = Gtk.Spinner()
        self.spinner.set_tooltip_text("Loading")
        toolbar.pack_start(self.spinner, False, False, 0)
        self.responsive_button = Gtk.MenuButton(label="Responsive")
        self.responsive_menu = Gtk.Menu()
        self.responsive_menu_items: dict[str, Gtk.RadioMenuItem] = {}
        disabled_item = Gtk.RadioMenuItem.new_with_label(None, "Disabled")
        self.responsive_menu.append(disabled_item)
        self.responsive_menu_items["none"] = disabled_item
        disabled_item.connect(
            "toggled", self._on_responsive_menu_item_toggled, "none"
        )
        for preset_name, preset in BROWSER_VIEWPORT_PRESETS.items():
            item = Gtk.RadioMenuItem.new_with_label_from_widget(
                disabled_item, preset.label
            )
            item.connect(
                "toggled", self._on_responsive_menu_item_toggled, preset_name
            )
            self.responsive_menu.append(item)
            self.responsive_menu_items[preset_name] = item
        disabled_item.set_active(True)
        self.responsive_menu.show_all()
        self.responsive_button.set_popup(self.responsive_menu)
        self.responsive_button.set_tooltip_text(
            "Choose a responsive viewport or restore Desktop"
        )
        toolbar.pack_start(self.responsive_button, False, False, 0)
        self.responsive_label = Gtk.Label()
        self.responsive_label.set_no_show_all(True)
        self.responsive_label.set_tooltip_text(
            "Requested CSS viewport and scale used to fit it"
        )
        toolbar.pack_start(self.responsive_label, False, False, 0)
        self.inspector_button = Gtk.ToggleButton()
        self.inspector_button.set_image(
            Gtk.Image.new_from_file(
                str(Path(__file__).with_name("developer-tools.svg"))
            )
        )
        self.inspector_button.set_tooltip_text(
            "Developer tools (F12 / Ctrl+Shift+I)"
        )
        self.inspector_button.get_accessible().set_name(
            "Developer tools"
        )
        self.inspector_button.connect("toggled", self._on_inspector_toggled)
        toolbar.pack_start(self.inspector_button, False, False, 0)
        self.pack_start(toolbar, False, False, 0)

        self.error_bar = Gtk.InfoBar()
        self.error_bar.set_message_type(Gtk.MessageType.ERROR)
        self.error_bar.set_show_close_button(True)
        self.error_bar.add_button("Reload", Gtk.ResponseType.ACCEPT)
        self.error_bar.connect("response", self._on_error_response)
        self.error_label = Gtk.Label(xalign=0)
        self.error_label.set_line_wrap(True)
        self.error_bar.get_content_area().add(self.error_label)
        self.pack_start(self.error_bar, False, False, 0)

        self.web_view = web_view or WebKit2.WebView.new_with_context(context)
        settings = self.web_view.get_settings()
        settings.set_enable_developer_extras(True)
        if entry.private:
            # 2026-08-21: WebKit anima molto lentamente gli scatti della rotella
            # in alcuni profili effimeri su Cinnamon; lo scroll discreto rende
            # Incognito immediato senza cambiare le WebView normali.
            settings.set_enable_smooth_scrolling(False)
        self.inspector = self.web_view.get_inspector()
        self.inspector.connect("closed", self._on_inspector_closed)
        self.inspector.connect("attach", self._on_inspector_opened)
        self.inspector.connect("bring-to-front", self._on_inspector_opened)
        self.inspector.connect("open-window", self._on_inspector_opened)
        self.web_view.connect("notify::title", self._on_page_identity_changed)
        self.web_view.connect("notify::uri", self._on_page_identity_changed)
        self.web_view.connect("load-changed", self._on_load_changed)
        self.web_view.connect("load-failed", self._on_load_failed)
        self.web_view.connect(
            "web-process-terminated", self._on_web_process_terminated
        )
        self.web_view.connect("decide-policy", self._on_decide_policy)
        self.web_view.connect("create", self._on_create)
        self.web_view.connect(
            "context-menu-dismissed", self._on_context_menu_dismissed
        )
        # 2026-08-17: il contenitore dedicato forza la misura solo nella preview;
        # in Desktop lascia invece alla WebView l'intera allocazione disponibile.
        self.viewport_stage = _ResponsiveViewport()
        self.viewport_stage.set_hexpand(True)
        self.viewport_stage.set_vexpand(True)
        self.viewport_stage.add(self.web_view)
        self.viewport_stage.connect(
            "size-allocate", self._on_viewport_stage_size_allocate
        )
        self.viewport_overlay = Gtk.Overlay()
        self.viewport_overlay.add(self.viewport_stage)
        self.exit_responsive_button = Gtk.Button(label="× Desktop")
        self.exit_responsive_button.set_halign(Gtk.Align.END)
        self.exit_responsive_button.set_valign(Gtk.Align.START)
        self.exit_responsive_button.set_margin_top(12)
        self.exit_responsive_button.set_margin_end(12)
        self.exit_responsive_button.set_no_show_all(True)
        self.exit_responsive_button.set_tooltip_text(
            "Exit Responsive mode (Esc)"
        )
        self.exit_responsive_button.get_style_context().add_class(
            "browser-responsive-exit"
        )
        self.exit_responsive_button.connect(
            "clicked", self._on_exit_responsive_clicked
        )
        self.viewport_overlay.add_overlay(self.exit_responsive_button)
        self.pack_start(self.viewport_overlay, True, True, 0)
        self.uri_entry.set_text(entry.url)
        # 2026-08-17: il caricamento parte solo dopo la materializzazione lazy;
        # il ripristino della sola riga non crea WebView né richieste di rete.
        if load_initial:
            self.web_view.load_uri(entry.url)
        self._update_navigation_state()
        self._update_development_actions()

    @staticmethod
    def _icon_button(icon_name: str, tooltip: str) -> Gtk.Button:
        """Create one compact themed browser-toolbar button."""

        button = Gtk.Button()
        button.set_image(
            Gtk.Image.new_from_icon_name(icon_name, Gtk.IconSize.BUTTON)
        )
        button.set_tooltip_text(tooltip)
        button.get_accessible().set_name(tooltip)
        return button

    def _on_context_menu_dismissed(self, _web_view: object) -> None:
        """Persist a possible native context-menu copy outside WebKit."""

        if not self.entry.private:
            return
        # 2026-08-21: il profilo effimero può perdere il proprietario X11 degli
        # appunti quando la WebView viene nascosta; Gtk chiede al clipboard
        # manager di conservare tutti i formati senza trasformarli in solo testo.
        GLib.idle_add(self._store_clipboard)

    def _store_clipboard(self) -> bool:
        """Ask the desktop clipboard manager to retain WebKit's current data."""

        clipboard = self.web_view.get_clipboard(Gdk.SELECTION_CLIPBOARD)
        if clipboard is not None:
            clipboard.store()
        return GLib.SOURCE_REMOVE

    @property
    def reference(self) -> BrowserRef:
        """Return the stable runtime identity used by the project tree."""

        return self.entry.reference

    @property
    def title(self) -> str:
        """Return the page title or a compact fallback derived from its URI."""

        title = (self.web_view.get_title() or "").strip()
        if title:
            return title
        uri = self.web_view.get_uri() or self.entry.url
        parsed = urlsplit(uri)
        return parsed.hostname or ("Browser" if uri == "about:blank" else uri)

    @property
    def uri(self) -> str:
        """Return the current URI with the saved URL as lazy-safe fallback."""

        return self.web_view.get_uri() or self.entry.url

    def focus_location(self) -> None:
        """Focus and select the complete URL for immediate navigation."""

        self.uri_entry.grab_focus()
        self.uri_entry.select_region(0, -1)

    def toggle_inspector(self) -> None:
        """Toggle the docked or detached Web Inspector from UI shortcuts."""

        self.inspector_button.set_active(
            not self.inspector_button.get_active()
        )

    def disable_responsive(self) -> None:
        """Return explicitly to Desktop through the shared radio-menu action."""

        self.responsive_menu_items["none"].set_active(True)

    def reload(self) -> None:
        """Retry or refresh the current page and dismiss its prior error."""

        self.failed_loading = False
        self.error_bar.hide()
        self.web_view.reload()

    def reload_bypass_cache(self) -> None:
        """Refresh the current page while bypassing WebKit's network cache."""

        self.failed_loading = False
        self.error_bar.hide()
        self.web_view.reload_bypass_cache()

    def set_reload_on_bell(self, enabled: bool) -> None:
        """Synchronize the runtime BELL option without feeding back to its manager."""

        self.entry.reload_on_bell = enabled
        if self.reload_on_bell_check.get_active() == enabled:
            return
        self._syncing_reload_on_bell = True
        self.reload_on_bell_check.set_active(enabled)
        self._syncing_reload_on_bell = False

    def close(self) -> None:
        """Stop loading and release developer tools and pending popup bridges."""

        self.closed = True
        for popup_view in tuple(self.external_popup_bridges):
            self._release_external_popup_bridge(popup_view)
        if self.site_data_cancellable is not None:
            self.site_data_cancellable.cancel()
            self.site_data_cancellable = None
        self.web_view.stop_loading()
        if self.inspector_button.get_active():
            self.inspector_button.set_active(False)
        else:
            self.inspector.close()

    def _on_back_clicked(self, _button: Gtk.Button) -> None:
        """Navigate backward when WebKit exposes a prior history item."""

        if self.web_view.can_go_back():
            self.web_view.go_back()

    def _on_forward_clicked(self, _button: Gtk.Button) -> None:
        """Navigate forward when WebKit exposes a following history item."""

        if self.web_view.can_go_forward():
            self.web_view.go_forward()

    def _on_reload_clicked(self, _button: Gtk.Button) -> None:
        """Stop an active request or reload the current page."""

        if self.web_view.is_loading():
            self.web_view.stop_loading()
        else:
            self.reload()

    def _on_hard_reload_activate(self, _item: Gtk.MenuItem) -> None:
        """Run the explicit development reload without consulting HTTP cache."""

        self.reload_bypass_cache()

    def _on_reload_on_bell_toggled(
        self, button: Gtk.CheckButton
    ) -> None:
        """Publish an explicit runtime BELL association for this browser page."""

        if self._syncing_reload_on_bell:
            return
        self.on_reload_on_bell_changed(self, button.get_active())

    def _on_clear_site_data_activate(self, _item: Gtk.MenuItem) -> None:
        """Confirm deletion of the current host's WebKit data except cookies."""

        parsed = urlsplit(self.uri)
        hostname = (parsed.hostname or "").casefold().rstrip(".")
        if parsed.scheme not in {"http", "https"} or not hostname:
            return
        parent = self.get_toplevel()
        dialog = Gtk.MessageDialog(
            transient_for=parent if isinstance(parent, Gtk.Window) else None,
            modal=True,
            message_type=Gtk.MessageType.WARNING,
            buttons=Gtk.ButtonsType.NONE,
            text=f"Clear data for {hostname}?",
        )
        dialog.format_secondary_text(
            "Cache, storage, databases, and service workers will be removed. "
            "Cookies will remain unchanged."
        )
        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        delete_button = dialog.add_button(
            "Clear and reload", Gtk.ResponseType.ACCEPT
        )
        delete_button.get_style_context().add_class("destructive-action")
        dialog.set_default_response(Gtk.ResponseType.CANCEL)
        response = dialog.run()
        dialog.destroy()
        if response != Gtk.ResponseType.ACCEPT:
            return

        # 2026-08-18: remove() consente di limitare la pulizia al bucket host
        # corrente; clear() cancellerebbe invece i dati di tutti i repository.
        self.site_data_types = (
            WebKit2.WebsiteDataTypes.ALL
            & ~WebKit2.WebsiteDataTypes.COOKIES
        )
        self.site_data_host = hostname
        self.site_data_cancellable = Gio.Cancellable()
        self._update_development_actions()
        data_manager = self.web_view.get_website_data_manager()
        data_manager.fetch(
            self.site_data_types,
            self.site_data_cancellable,
            self._on_site_data_fetched,
        )

    def _on_site_data_fetched(
        self, data_manager: object, result: Gio.AsyncResult
    ) -> None:
        """Filter asynchronously fetched website-data records to the requested host."""

        try:
            website_data = data_manager.fetch_finish(result)
        except GLib.Error as error:
            if error.matches(Gio.io_error_quark(), Gio.IOErrorEnum.CANCELLED):
                return
            self._finish_site_data_clear(
                f"Unable to read site data: {error}"
            )
            return
        matching_data = [
            item
            for item in website_data
            if (item.get_name() or "").casefold().rstrip(".")
            == self.site_data_host
        ]
        if not matching_data:
            self._finish_site_data_clear(None)
            return
        data_manager.remove(
            self.site_data_types,
            matching_data,
            self.site_data_cancellable,
            self._on_site_data_removed,
        )

    def _on_site_data_removed(
        self, data_manager: object, result: Gio.AsyncResult
    ) -> None:
        """Complete host data removal and reload the page without cache."""

        try:
            removed = data_manager.remove_finish(result)
        except GLib.Error as error:
            if error.matches(Gio.io_error_quark(), Gio.IOErrorEnum.CANCELLED):
                return
            self._finish_site_data_clear(
                f"Unable to clear site data: {error}"
            )
            return
        if not removed:
            self._finish_site_data_clear(
                "WebKit did not finish clearing the site data."
            )
            return
        self._finish_site_data_clear(None)

    def _finish_site_data_clear(self, error: str | None) -> None:
        """Restore the clear action and either report failure or hard-reload."""

        self.site_data_cancellable = None
        self.site_data_types = None
        self.site_data_host = None
        if self.closed:
            return
        self._update_development_actions()
        if error is not None:
            self.on_error(error)
            return
        self.reload_bypass_cache()

    def _on_inspector_toggled(self, button: Gtk.ToggleButton) -> None:
        """Open or close developer tools to match the toolbar toggle state."""

        if self._syncing_inspector:
            return
        if button.get_active():
            self.inspector_open = True
            self.inspector.show()
            return
        if self.inspector_open:
            self.inspector_open = False
            self.inspector.close()

    def _on_inspector_opened(self, _inspector: object) -> bool:
        """Synchronize the toolbar when WebKit opens or presents its Inspector."""

        self.inspector_open = True
        if not self.inspector_button.get_active():
            self._syncing_inspector = True
            self.inspector_button.set_active(True)
            self._syncing_inspector = False
        return False

    def _on_inspector_closed(self, _inspector: object) -> None:
        """Synchronize the toolbar when Inspector closes from its own UI."""

        self.inspector_open = False
        if self.inspector_button.get_active():
            self._syncing_inspector = True
            self.inspector_button.set_active(False)
            self._syncing_inspector = False

    def _on_exit_responsive_clicked(self, _button: Gtk.Button) -> None:
        """Leave responsive preview from its deliberate canvas control."""

        self.disable_responsive()

    def _on_responsive_menu_item_toggled(
        self, item: Gtk.CheckMenuItem, preset: str
    ) -> None:
        """Apply only the newly selected radio-menu viewport or restore Desktop."""

        if not item.get_active():
            return
        viewport = BROWSER_VIEWPORT_PRESETS.get(preset)
        if viewport is not None:
            viewport_width = viewport.width
            viewport_height = viewport.height
            self.responsive_viewport = (viewport_width, viewport_height)
            self.responsive_layout = None
            self.desktop_layout = None
            self.responsive_button.set_label(
                f"Responsive · {viewport_width}×{viewport_height}"
            )
            self.exit_responsive_button.show()
            self.viewport_stage.get_style_context().add_class(
                "browser-responsive-stage"
            )
            self.responsive_label.show()
            allocation = self.viewport_stage.get_allocation()
            self._apply_responsive_layout(
                allocation.width, allocation.height
            )
            return
        self.responsive_viewport = None
        self.responsive_layout = None
        self.desktop_layout = None
        self.responsive_button.set_label("Responsive")
        self.exit_responsive_button.hide()
        self.responsive_label.hide()
        self.viewport_stage.get_style_context().remove_class(
            "browser-responsive-stage"
        )
        self.web_view.set_zoom_level(1.0)
        allocation = self.viewport_stage.get_allocation()
        self._apply_desktop_layout(allocation.width, allocation.height)

    def _on_viewport_stage_size_allocate(
        self, _stage: Gtk.Widget, allocation: Gdk.Rectangle
    ) -> None:
        """Recalculate preview fit only after a real GTK allocation change."""

        if self.responsive_viewport is None:
            self._apply_desktop_layout(allocation.width, allocation.height)
        else:
            self._apply_responsive_layout(allocation.width, allocation.height)

    def _apply_desktop_layout(
        self, available_width: int, available_height: int
    ) -> None:
        """Expand the WebView to the complete stage while Responsive is off."""

        if available_width <= 0 or available_height <= 0:
            return
        layout = (available_width, available_height)
        if layout == self.desktop_layout:
            return
        self.desktop_layout = layout
        self.viewport_stage.set_forced_size(None, None)

    def _apply_responsive_layout(
        self, available_width: int, available_height: int
    ) -> None:
        """Fit the selected CSS viewport and expose its effective preview scale."""

        if self.responsive_viewport is None:
            return
        viewport_width, viewport_height = self.responsive_viewport
        fit_width = max(1, available_width - _RESPONSIVE_STAGE_PADDING)
        fit_height = max(1, available_height - _RESPONSIVE_STAGE_PADDING)
        scale = responsive_fit_scale(
            fit_width,
            fit_height,
            viewport_width,
            viewport_height,
        )
        pixel_width = max(1, round(viewport_width * scale))
        pixel_height = max(1, round(viewport_height * scale))
        offset_x = max(0, (available_width - pixel_width) // 2)
        offset_y = max(0, (available_height - pixel_height) // 2)
        layout = (offset_x, offset_y, pixel_width, pixel_height, scale)
        if layout == self.responsive_layout:
            return
        self.responsive_layout = layout
        # 2026-08-17: WebKit page zoom compensa la minore allocazione GTK; la
        # pagina continua così a impaginare usando il viewport CSS scelto.
        self.viewport_stage.set_forced_size(pixel_width, pixel_height)
        self.web_view.set_zoom_level(scale)
        self.responsive_label.set_text(
            f"{viewport_width} × {viewport_height} · {round(scale * 100)}%"
        )

    def _on_uri_activated(self, entry: Gtk.Entry) -> None:
        """Validate and load the address explicitly submitted by the user."""

        requested = entry.get_text().strip()
        uri = normalize_browser_uri(requested)
        if uri is None:
            scheme = urlsplit(requested).scheme.lower()
            if scheme and scheme not in _BLOCKED_SCHEMES:
                self._open_external_uri(requested)
                return
            _report_blocked_navigation(requested, scheme, "address bar")
            self.on_error("Invalid URL or disallowed scheme.")
            return
        self.error_bar.hide()
        self.failed_loading = False
        entry.set_text(uri)
        entry.set_position(-1)
        self.web_view.load_uri(uri)

    def _on_page_identity_changed(
        self, _web_view: object, _property: object
    ) -> None:
        """Publish title and URI changes without stealing URL-entry focus."""

        if not self.uri_entry.has_focus():
            self.uri_entry.set_text(self.uri)
            self.uri_entry.set_position(-1)
        self._update_development_actions()
        self.on_state_changed(self)

    def _on_load_changed(self, _web_view: object, load_event: object) -> None:
        """Reflect event-driven loading and navigation availability in the toolbar."""

        if WebKit2 is None:
            return
        loading = load_event != WebKit2.LoadEvent.FINISHED
        if load_event == WebKit2.LoadEvent.STARTED:
            self.failed_loading = False
            self.error_bar.hide()
        self._set_loading(loading)
        if load_event in {
            WebKit2.LoadEvent.COMMITTED,
            WebKit2.LoadEvent.FINISHED,
        }:
            if not self.failed_loading:
                self.error_bar.hide()
            self.on_state_changed(self)

    def _on_load_failed(
        self,
        _web_view: object,
        _load_event: object,
        failing_uri: str,
        error: GLib.Error,
    ) -> bool:
        """Keep the page usable and expose a non-modal retry after load failure."""

        self.failed_loading = True
        self._set_loading(False)
        self.error_label.set_text(f"Failed to load {failing_uri}: {error}")
        self.error_bar.show_all()
        return True

    def _on_web_process_terminated(
        self, _web_view: object, _reason: object
    ) -> None:
        """Expose a recoverable reload action after a WebKit process failure."""

        self.failed_loading = True
        self._set_loading(False)
        self.error_label.set_text(
            "The page process stopped. Press Reload to try again."
        )
        self.error_bar.show_all()

    def _on_error_response(
        self, _info_bar: Gtk.InfoBar, response: int
    ) -> None:
        """Reload on explicit retry or dismiss the non-modal browser error."""

        if response == Gtk.ResponseType.ACCEPT:
            self.reload()
        else:
            self.error_bar.hide()

    def _set_loading(self, loading: bool) -> None:
        """Update spinner and the dynamic Stop/Reload toolbar button."""

        if loading:
            self.spinner.show()
            self.spinner.start()
            icon_name = "process-stop"
            tooltip = "Stop loading (Esc)"
        else:
            self.spinner.stop()
            self.spinner.hide()
            icon_name = "view-refresh"
            tooltip = "Reload (F5 / Ctrl+R)"
        image = Gtk.Image.new_from_icon_name(icon_name, Gtk.IconSize.BUTTON)
        image.show()
        self.reload_button.set_image(image)
        self.reload_button.set_tooltip_text(tooltip)
        self.reload_button.get_accessible().set_name(tooltip)
        self._update_navigation_state()

    def _update_navigation_state(self) -> None:
        """Enable history controls only when their actions are applicable."""

        self.back_button.set_sensitive(self.web_view.can_go_back())
        self.forward_button.set_sensitive(self.web_view.can_go_forward())

    def _update_development_actions(self) -> None:
        """Enable site-data deletion only for a valid idle HTTP(S) host."""

        parsed = urlsplit(self.uri)
        has_host = parsed.scheme in {"http", "https"} and bool(parsed.hostname)
        self.clear_site_data_item.set_sensitive(
            has_host and self.site_data_cancellable is None
        )

    def _on_create(
        self, web_view: object, navigation_action: object
    ) -> object | None:
        """Route a requested web window outside SLATE without creating a row."""

        request = navigation_action.get_request()
        uri = request.get_uri() or ""
        scheme = urlsplit(uri).scheme.lower()
        if uri in {"", "about:blank"}:
            # 2026-08-18: WordPress apre prima una finestra nominata vuota e
            # poi vi invia il form di preview. Un bridge correlato completa il
            # POST e passa solo l'URL finale al browser predefinito.
            return self._create_external_popup_bridge(web_view)
        # 2026-08-17: una finestra richiesta dal sito non equivale a una tab
        # creata dall'utente in SLATE; i popup espliciti escono nel browser di
        # sistema e quelli automatici vengono soppressi.
        if navigation_action.is_user_gesture() and (
            scheme in {"http", "https"} or uri == "about:blank"
        ):
            self._open_external_uri(uri)
        return None

    def _create_external_popup_bridge(self, opener: object) -> object:
        """Complete a named-window navigation invisibly before opening it externally."""

        popup_view = WebKit2.WebView.new_with_related_view(opener)
        self.external_popup_bridges.add(popup_view)
        popup_view.connect(
            "load-changed", self._on_external_popup_bridge_load_changed
        )
        popup_view.connect("destroy", self._on_external_popup_bridge_destroyed)
        popup_view.connect("create", self._on_create)
        popup_view.connect("decide-policy", self._on_decide_policy)
        return popup_view

    def _on_external_popup_bridge_load_changed(
        self, web_view: object, load_event: object
    ) -> None:
        """Open the final HTTP(S) destination after the staged POST completes."""

        if load_event != WebKit2.LoadEvent.FINISHED:
            return
        uri = web_view.get_uri() or ""
        if urlsplit(uri).scheme.lower() not in {"http", "https"}:
            return
        self._open_external_uri(uri)
        self._release_external_popup_bridge(web_view)

    def _release_external_popup_bridge(self, web_view: object) -> None:
        """Stop and destroy one invisible related WebView exactly once."""

        if web_view not in self.external_popup_bridges:
            return
        self.external_popup_bridges.discard(web_view)
        web_view.stop_loading()
        web_view.destroy()

    def _on_external_popup_bridge_destroyed(self, web_view: object) -> None:
        """Forget a bridge destroyed by WebKit before reaching a final URL."""

        self.external_popup_bridges.discard(web_view)

    def _on_decide_policy(
        self, _web_view: object, decision: object, decision_type: object
    ) -> bool:
        """Allow web navigation and route explicit external schemes safely."""

        if WebKit2 is None or decision_type not in {
            WebKit2.PolicyDecisionType.NAVIGATION_ACTION,
            WebKit2.PolicyDecisionType.NEW_WINDOW_ACTION,
        }:
            return False
        action = decision.get_navigation_action()
        request = action.get_request()
        uri = request.get_uri() or ""
        scheme = urlsplit(uri).scheme.lower()
        if decision_type == WebKit2.PolicyDecisionType.NEW_WINDOW_ACTION:
            if uri in {"", "about:blank"}:
                # Il create handler deve ricevere il contenitore iniziale usato
                # dai form con target nominato; ignorarlo troncherebbe il POST.
                decision.use()
                return True
            decision.ignore()
            if action.is_user_gesture() and (
                scheme in {"http", "https"} or uri == "about:blank"
            ):
                self._open_external_uri(uri)
            else:
                _report_blocked_navigation(uri, scheme, "page popup")
            return True
        if scheme in {"http", "https", "blob"} or uri == "about:blank":
            # 2026-08-18: Gutenberg costruisce il documento di editor-canvas
            # come blob: della pagina autenticata. Bloccarlo lascia l'iframe
            # vuoto senza errori JS; la barra URL continua a rifiutare blob:.
            return False
        decision.ignore()
        if scheme == "file":
            self.on_error("Access to file:// is not allowed in the SLATE browser.")
        elif action.is_user_gesture() and scheme and scheme not in _BLOCKED_SCHEMES:
            self._open_external_uri(uri)
            return True
        _report_blocked_navigation(uri, scheme, "page")
        return True

    def _open_external_uri(self, uri: str) -> None:
        """Launch an explicitly requested non-web URI outside SLATE."""

        Gio.AppInfo.launch_default_for_uri_async(
            uri,
            None,
            None,
            self._on_external_uri_opened,
            uri,
        )

    def _on_external_uri_opened(
        self, _source: object, result: Gio.AsyncResult, uri: str
    ) -> None:
        """Report failure from an asynchronously launched external URI."""

        try:
            Gio.AppInfo.launch_default_for_uri_finish(result)
        except GLib.Error as error:
            self.on_error(f"Unable to open {uri}: {error}")


class BrowserManager:
    """Own lazy persistent tabs backed by normal or ephemeral profiles."""

    def __init__(
        self,
        workspace: Gtk.Stack,
        on_collection_changed: Callable[[], None],
        on_state_changed: Callable[[BrowserPage], None],
        on_error: Callable[[str], None],
        data_directory: Path | None = None,
        cache_directory: Path | None = None,
    ) -> None:
        """Prepare browser ownership without constructing a WebKit context."""

        self.workspace = workspace
        self.on_collection_changed = on_collection_changed
        self.on_state_changed = on_state_changed
        self.on_error = on_error
        self.pages: dict[BrowserRef, BrowserEntry] = {}
        data_home = Path(
            os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))
        )
        cache_home = Path(
            os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache"))
        )
        self.data_directory = data_directory or data_home / "slate" / "webkit"
        self.cache_directory = cache_directory or cache_home / "slate" / "webkit"
        self.context: object | None = None
        self.data_manager: object | None = None
        self.next_identifier = 1

    def restore(self, projects: list[dict]) -> None:
        """Register configured rows without initializing WebKit or loading URLs."""

        for project in projects:
            for item in project.get("browsers", []):
                entry = BrowserEntry(
                    project_name=project["name"],
                    identifier=item["id"],
                    url=item["url"],
                    title=item["title"],
                    private=item.get("private", False) is True,
                )
                self.pages[entry.reference] = entry
                self._observe_identifier(entry.identifier)

    def open_page(self, project_name: str, private: bool = False) -> BrowserEntry | None:
        """Create and display one normal or isolated private browser row."""

        if WebKit2 is None:
            self.on_error("Missing dependency: WebKit2 4.1 typelib.")
            return None
        identifier = self._next_identifier()
        entry = BrowserEntry(
            project_name=project_name,
            identifier=identifier,
            title="Browser",
            private=private,
            context=WebKit2.WebContext.new_ephemeral() if private else None,
        )
        self.pages[entry.reference] = entry
        self._materialize(entry)
        self.on_collection_changed()
        return entry

    def show_page(self, project_name: str, identifier: str) -> bool:
        """Materialize and reveal a browser selected from the project tree."""

        entry = self.pages.get((project_name, identifier))
        if entry is None:
            return False
        page = self._materialize(entry)
        if page is None:
            return False
        if self.workspace.get_visible_child() is page:
            # 2026-08-21: un secondo click sulla riga già visibile non rimappa la
            # WebView: alcuni profili effimeri interpretano il ciclo GTK come
            # una nuova attivazione e il sito può ricaricare il documento.
            return True
        self.workspace.set_visible_child(page)
        self.on_state_changed(page)
        return True

    def _materialize(self, entry: BrowserEntry) -> BrowserPage | None:
        """Create one WebView on first explicit activation and reuse it afterward."""

        if entry.page is not None:
            return entry.page
        if WebKit2 is None:
            self.on_error("Missing dependency: WebKit2 4.1 typelib.")
            return None
        # 2026-08-17: una tab anonima ripristinata conserva URL e identità, ma
        # riceve un profilo effimero nuovo per non ripristinare cookie/sessione.
        context = entry.context or (
            WebKit2.WebContext.new_ephemeral()
            if entry.private
            else self._normal_context()
        )
        page = BrowserPage(
            entry,
            context,
            self._on_page_state_changed,
            self._on_reload_on_bell_changed,
            self.on_error,
        )
        entry.context = context if entry.private else None
        entry.page = page
        self.workspace.add_named(
            page, f"browser:{entry.project_name}:{entry.identifier}"
        )
        page.show_all()
        page.error_bar.hide()
        page.spinner.hide()
        self.workspace.set_visible_child(page)
        page.focus_location()
        return page

    def _normal_context(self) -> object:
        """Create the one app-wide persistent WebKit profile on first use."""

        if WebKit2 is None:
            raise RuntimeError("WebKitGTK 4.1 is unavailable")
        if self.context is None:
            # 2026-08-17: directory XDG esplicite impediscono a WebKit di
            # scegliere percorsi impliciti o scrivere dentro le working copy.
            self.data_directory.mkdir(parents=True, exist_ok=True)
            self.cache_directory.mkdir(parents=True, exist_ok=True)
            self.data_manager = WebKit2.WebsiteDataManager(
                base_data_directory=str(self.data_directory),
                base_cache_directory=str(self.cache_directory),
            )
            # 2026-08-17: WebsiteDataManager non rende persistenti i cookie da
            # solo; il database esplicito conserva login normali fra gli avvii.
            cookie_manager = self.data_manager.get_cookie_manager()
            cookie_manager.set_persistent_storage(
                str(self.data_directory / "cookies.sqlite"),
                WebKit2.CookiePersistentStorage.SQLITE,
            )
            self.data_manager.set_persistent_credential_storage_enabled(True)
            self.context = WebKit2.WebContext.new_with_website_data_manager(
                self.data_manager
            )
        return self.context

    def _on_page_state_changed(self, page: BrowserPage) -> None:
        """Update persisted metadata and publish one page UI change."""

        page.entry.url = page.uri
        page.entry.title = page.title
        self.on_state_changed(page)

    def _on_reload_on_bell_changed(
        self, page: BrowserPage, enabled: bool
    ) -> None:
        """Keep at most one runtime BELL target inside each project."""

        # 2026-08-18: l'associazione è volutamente runtime e mutuamente
        # esclusiva; nessun flag viene aggiunto al JSON o ripristinato al boot.
        for entry in self.pages.values():
            if entry.project_name != page.project_name:
                continue
            selected = enabled and entry is page.entry
            entry.reload_on_bell = selected
            if entry.page is not None:
                entry.page.set_reload_on_bell(selected)

    def reload_on_bell_target(
        self, project_name: str
    ) -> BrowserEntry | None:
        """Return the materialized runtime BELL target for one project."""

        return next(
            (
                entry
                for entry in self.pages.values()
                if entry.project_name == project_name
                and entry.reload_on_bell
                and entry.page is not None
            ),
            None,
        )

    def current_page(self) -> BrowserPage | None:
        """Return the visible browser page, excluding terminals and editors."""

        child = self.workspace.get_visible_child()
        return child if isinstance(child, BrowserPage) else None

    def handle_key(self, event: Gdk.EventKey) -> bool:
        """Handle browser navigation and developer shortcuts only when visible."""

        page = self.current_page()
        if page is None:
            return False
        keyval = Gdk.keyval_to_lower(event.keyval)
        control = bool(event.state & Gdk.ModifierType.CONTROL_MASK)
        shift = bool(event.state & Gdk.ModifierType.SHIFT_MASK)
        alternate = bool(event.state & Gdk.ModifierType.MOD1_MASK)
        if event.keyval == Gdk.KEY_F12 or (control and shift and keyval == Gdk.KEY_i):
            page.toggle_inspector()
        elif control and keyval == Gdk.KEY_l:
            page.focus_location()
        elif alternate and keyval == Gdk.KEY_Left and page.web_view.can_go_back():
            page.web_view.go_back()
        elif alternate and keyval == Gdk.KEY_Right and page.web_view.can_go_forward():
            page.web_view.go_forward()
        elif control and shift and keyval == Gdk.KEY_r:
            page.reload_bypass_cache()
        elif event.keyval == Gdk.KEY_F5 or (control and keyval == Gdk.KEY_r):
            page.reload()
        elif event.keyval == Gdk.KEY_Escape:
            if page.web_view.is_loading():
                page.web_view.stop_loading()
            elif page.responsive_viewport is not None:
                page.disable_responsive()
            else:
                return False
        elif control and keyval == Gdk.KEY_w:
            self.close_page(page.reference)
        elif page.entry.private and (
            (control and keyval == Gdk.KEY_c)
            or (
                control
                and event.keyval in (Gdk.KEY_Insert, Gdk.KEY_KP_Insert)
            )
        ):
            # 2026-08-21: il salvataggio avviene nell'idle successivo, dopo che
            # WebKit ha eseguito la copia nativa; l'evento deve proseguire fino
            # alla WebView affinché Ctrl+C conservi la sua semantica normale.
            GLib.idle_add(page._store_clipboard)
            return False
        else:
            return False
        return True

    def close_page(self, reference: BrowserRef) -> bool:
        """Remove one browser row and release its optional WebKit widget."""

        entry = self.pages.pop(reference, None)
        if entry is None:
            return False
        page = entry.page
        if page is not None:
            if self.workspace.get_visible_child() is page:
                self.workspace.show_terminal()
            page.close()
            self.workspace.remove(page)
            page.destroy()
            entry.page = None
        entry.context = None
        self.on_collection_changed()
        return True

    def close_project(self, project_name: str, notify: bool = True) -> None:
        """Close every normal and private browser owned by a removed project."""

        references = [
            reference for reference in self.pages if reference[0] == project_name
        ]
        for reference in references:
            entry = self.pages.pop(reference)
            if entry.page is not None:
                if self.workspace.get_visible_child() is entry.page:
                    self.workspace.show_terminal()
                entry.page.close()
                self.workspace.remove(entry.page)
                entry.page.destroy()
            entry.page = None
            entry.context = None
        if references and notify:
            self.on_collection_changed()

    def serialized_project(self, project_name: str) -> list[dict[str, object]]:
        """Return tab metadata while excluding all WebKit profile data."""

        return [
            {
                "id": entry.identifier,
                "url": entry.url,
                "title": entry.title,
                "private": entry.private,
            }
            for entry in self.pages.values()
            if entry.project_name == project_name
        ]

    def _next_identifier(self) -> str:
        """Allocate one collision-free browser identifier across all projects."""

        while any(
            entry.identifier == f"browser-{self.next_identifier}"
            for entry in self.pages.values()
        ):
            self.next_identifier += 1
        identifier = f"browser-{self.next_identifier}"
        self.next_identifier += 1
        return identifier

    def _observe_identifier(self, identifier: str) -> None:
        """Advance the numeric allocator beyond a restored standard identifier."""

        prefix = "browser-"
        if identifier.startswith(prefix) and identifier[len(prefix) :].isdigit():
            self.next_identifier = max(
                self.next_identifier,
                int(identifier[len(prefix) :]) + 1,
            )

    def shutdown(self) -> None:
        """Close materialized pages and release persistent/private contexts."""

        for entry in tuple(self.pages.values()):
            if entry.page is not None:
                entry.page.close()
                self.workspace.remove(entry.page)
                entry.page.destroy()
                entry.page = None
            entry.context = None
        self.pages.clear()
        self.context = None
        self.data_manager = None
