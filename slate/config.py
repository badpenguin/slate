"""Load and atomically persist the single SLATE configuration."""

from __future__ import annotations

import copy
import json
import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


@dataclass(frozen=True, slots=True)
class BrowserViewportPreset:
    """Describe one responsive-browser menu entry and its CSS viewport."""

    label: str
    width: int
    height: int


# 2026-08-18: un catalogo unico impedisce che label e dimensioni divergano;
# i preset device e le risoluzioni generiche mantengono l'ordine curato in UI.
BROWSER_VIEWPORT_PRESETS: dict[str, BrowserViewportPreset] = {
    "galaxy-a-series": BrowserViewportPreset("Samsung Galaxy A15/A16/A5x — 360 × 800", 360, 800),
    "iphone-x": BrowserViewportPreset("iPhone X/XS/11 Pro — 375 × 812", 375, 812),
    "samsung-mid-range": BrowserViewportPreset("Samsung mid-range — 384 × 832", 384, 832),
    "redmi-note": BrowserViewportPreset("Xiaomi Redmi Note — 393 × 873", 393, 873),
    "iphone-xr": BrowserViewportPreset("iPhone XR/11/11 Pro Max — 414 × 896", 414, 896),
    "ipad-classic": BrowserViewportPreset("iPad mini/classic — 768 × 1024", 768, 1024),
    "ipad-10": BrowserViewportPreset("iPad 10th gen — 820 × 1180", 820, 1180),
    "desktop-xga": BrowserViewportPreset("Desktop XGA — 1024 × 768", 1024, 768),
    "laptop-hd": BrowserViewportPreset("Laptop HD — 1366 × 768", 1366, 768),
    "laptop": BrowserViewportPreset("Laptop — 1440 × 900", 1440, 900),
    "laptop-scaled": BrowserViewportPreset("Laptop 1080p, scaling 125% — 1536 × 864", 1536, 864),
    "desktop-fhd": BrowserViewportPreset("Desktop Full HD — 1920 × 1080", 1920, 1080),
    "desktop-qhd": BrowserViewportPreset("Desktop QHD — 2560 × 1440", 2560, 1440),
}


DEFAULT_EXTRA_COMMAND_ICON = "utilities-terminal"


def is_extra_command_icon(value: Any) -> bool:
    """Accept one bounded freedesktop icon name without paths or resources."""

    return isinstance(value, str) and re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}", value
    ) is not None


DEFAULT_CONFIG: dict[str, Any] = {
    "projects": [],
    "active_terminal": None,
    "expanded_projects": [],
    "pane_positions": [220, 900],
    "window": {"width": 1600, "height": 900, "maximized": False},
    "settings": {
        "revisions": {"font_size": 10},
        "files": {"font_size": 10},
        "editor": {"font_size": 10},
        "external_apps": {"editor_command": ["gvim", "-f"]},
        "terminal": {"status_bar": False},
        "commands": {"items": []},
    },
    "editor": {"tabs": [], "active_tab": None},
}


def new_project_config(
    name: str,
    path: str,
    terminals: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Return the complete persistent schema for one newly configured project."""

    # 2026-08-18: aggiunta manuale e riadozione condividono lo schema, ma solo
    # la prima richiede `main`; il chiamante dichiara quindi i terminali iniziali.
    terminal_names = list(terminals)
    return {
        "name": name,
        "path": path,
        "terminals": terminal_names,
        "terminal_commands": {},
        "terminal_icons": {},
        "last_terminal": terminal_names[0] if terminal_names else None,
        "browsers": [],
        "item_order": [
            {"kind": "terminal", "value": terminal_name}
            for terminal_name in terminal_names
        ],
        "file_manager": {
            "show_hidden": False,
            "show_excluded": False,
            "expanded_paths": [],
        },
        "repositories": {"known": [], "excluded": []},
    }


def _normalize_font_size(value: Any, fallback: int) -> int:
    """Accept only practical integer point sizes from hand-edited config."""

    return value if isinstance(value, int) and not isinstance(value, bool) and 8 <= value <= 32 else fallback


def _normalize_external_command(value: Any, fallback: list[str]) -> list[str]:
    """Accept a bounded external-editor argument vector without shell syntax."""

    if (
        not isinstance(value, list)
        or not value
        or len(value) > 32
        or any(
            not isinstance(argument, str)
            or not argument
            or len(argument) > 4096
            or "\0" in argument
            for argument in value
        )
    ):
        return list(fallback)
    return list(value)


def _normalize_extra_commands(value: Any) -> list[dict[str, str]]:
    """Return at most 32 unique, valid shell launchers in configured order."""

    if not isinstance(value, list):
        return []
    commands: list[dict[str, str]] = []
    labels: set[str] = set()
    # 2026-08-21: config modificata a mano viene filtrata con le stesse regole
    # della UI; il primo nome valido conserva la posizione scelta dall'utente.
    for item in value:
        if not isinstance(item, dict) or len(commands) >= 32:
            continue
        raw_label = item.get("label")
        label = raw_label.strip() if isinstance(raw_label, str) else ""
        command = item.get("command")
        icon = item.get("icon", DEFAULT_EXTRA_COMMAND_ICON)
        if (
            not label
            or len(label) > 80
            or "\n" in label
            or "\r" in label
            or "\0" in label
            or label.casefold() in labels
            or not isinstance(command, str)
            or not command.strip()
            or len(command) > 4096
            or "\n" in command
            or "\r" in command
            or "\0" in command
            or not is_extra_command_icon(icon)
        ):
            continue
        try:
            words = shlex.split(command, posix=True)
        except ValueError:
            continue
        if not words:
            continue
        labels.add(label.casefold())
        commands.append({"label": label, "command": command.strip(), "icon": icon})
    return commands


def _tmux_slug(value: str, maximum: int) -> str:
    """Normalize a configured name for collision validation without GTK imports."""

    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")[:maximum].rstrip("-")


def _normalize_browser_url(value: Any) -> str | None:
    """Accept only persisted HTTP(S) pages and the inert blank document."""

    url = str(value).strip()
    if any(character.isspace() for character in url):
        return None
    if url == "about:blank":
        return url
    parsed = urlsplit(url)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return None
    return url


class ConfigStore:
    """Manage validated in-memory configuration and atomic disk writes."""

    def __init__(self, path: Path | None = None) -> None:
        """Select the single config path and load it without destructive repair."""

        configured = os.environ.get("SLATE_CONFIG")
        self.path = path or (
            Path(configured).expanduser()
            if configured
            else Path.home() / ".config" / "slate" / "config.json"
        )
        self.error: str | None = None
        self.data = self._load()

    def _load(self) -> dict[str, Any]:
        """Read and normalize config, returning defaults on missing/corrupt input."""

        if not self.path.exists():
            return copy.deepcopy(DEFAULT_CONFIG)
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            return self._normalize(raw)
        except (OSError, ValueError, TypeError) as error:
            # 2026-08-16: retain corrupt input untouched so a user can recover
            # it; saving occurs only after a later explicit UI mutation.
            self.error = f"Unreadable configuration: {error}"
            return copy.deepcopy(DEFAULT_CONFIG)

    def _normalize(self, raw: Any) -> dict[str, Any]:
        """Return a safe complete config while preserving supported values."""

        if not isinstance(raw, dict):
            raise TypeError("the JSON root must be an object")
        data = copy.deepcopy(DEFAULT_CONFIG)
        projects = raw.get("projects", [])
        if not isinstance(projects, list):
            raise TypeError("projects must be a list")
        data["projects"] = [
            self._normalize_project(item) for item in projects if isinstance(item, dict)
        ]
        self._validate_projects(data["projects"])
        if isinstance(raw.get("active_terminal"), (str, type(None))):
            data["active_terminal"] = raw.get("active_terminal")
        expanded = raw.get("expanded_projects", [])
        if isinstance(expanded, list):
            data["expanded_projects"] = [str(item) for item in expanded]
        panes = raw.get("pane_positions", [])
        if (
            isinstance(panes, list)
            and len(panes) == 2
            and all(isinstance(value, int) for value in panes)
        ):
            data["pane_positions"] = panes
        window = raw.get("window", {})
        if isinstance(window, dict):
            for key in ("width", "height"):
                if isinstance(window.get(key), int) and window[key] > 0:
                    data["window"][key] = window[key]
            if isinstance(window.get("maximized"), bool):
                data["window"]["maximized"] = window["maximized"]
        raw_settings = raw.get("settings", {})
        if isinstance(raw_settings, dict):
            # 2026-08-16: le preferenze sono globali perché descrivono due viste
            # dell'applicazione, non proprietà differenti dei singoli progetti.
            for section in ("revisions", "files", "editor"):
                raw_section = raw_settings.get(section, {})
                if isinstance(raw_section, dict):
                    fallback = data["settings"][section]["font_size"]
                    data["settings"][section]["font_size"] = _normalize_font_size(
                        raw_section.get("font_size"), fallback
                    )
            raw_external_apps = raw_settings.get("external_apps", {})
            if isinstance(raw_external_apps, dict):
                # 2026-08-20: le applicazioni esterne sono preferenze globali
                # autonome e restano vettori argv per non invocare una shell.
                command_fallback = data["settings"]["external_apps"][
                    "editor_command"
                ]
                data["settings"]["external_apps"]["editor_command"] = (
                    _normalize_external_command(
                        raw_external_apps.get("editor_command"),
                        command_fallback,
                    )
                )
            raw_terminal = raw_settings.get("terminal", {})
            # 2026-08-17: tmux status visibility is the sole terminal display
            # preference and remains global because the server owns one bar.
            if isinstance(raw_terminal, dict) and isinstance(
                raw_terminal.get("status_bar"), bool
            ):
                data["settings"]["terminal"]["status_bar"] = raw_terminal[
                    "status_bar"
                ]
            raw_commands = raw_settings.get("commands", {})
            if isinstance(raw_commands, dict):
                data["settings"]["commands"]["items"] = _normalize_extra_commands(
                    raw_commands.get("items")
                )
        raw_editor = raw.get("editor", {})
        if isinstance(raw_editor, dict):
            known_projects = {project["name"] for project in data["projects"]}
            tabs: list[dict[str, str]] = []
            seen: set[tuple[str, str]] = set()
            raw_tabs = raw_editor.get("tabs", [])
            if isinstance(raw_tabs, list):
                # 2026-08-16: gli editor persistono soltanto riferimenti relativi
                # a progetti noti; contenuti e path esterni non entrano in config.
                for item in raw_tabs:
                    if not isinstance(item, dict):
                        continue
                    project_name = str(item.get("project", ""))
                    raw_path = str(item.get("path", "")).replace("\\", "/")
                    path = raw_path.strip("/")
                    key = (project_name, path)
                    if (
                        project_name not in known_projects
                        or not path
                        or raw_path.startswith("/")
                        or ".." in path.split("/")
                        or key in seen
                    ):
                        continue
                    seen.add(key)
                    tabs.append({"project": project_name, "path": path})
            data["editor"]["tabs"] = tabs
            active = raw_editor.get("active_tab")
            if isinstance(active, dict):
                raw_active_path = str(active.get("path", "")).replace("\\", "/")
                active_key = (
                    str(active.get("project", "")),
                    raw_active_path.strip("/"),
                )
                if not raw_active_path.startswith("/") and active_key in seen:
                    data["editor"]["active_tab"] = {
                        "project": active_key[0],
                        "path": active_key[1],
                    }
        return data

    def _normalize_project(self, raw: dict[str, Any]) -> dict[str, Any]:
        """Normalize one project entry, terminal list and chronological children."""

        terminals = raw.get("terminals", [])
        if not isinstance(terminals, list):
            terminals = []
        terminal_names = list(dict.fromkeys(str(item) for item in terminals if str(item)))
        terminal_commands: dict[str, str] = {}
        raw_terminal_commands = raw.get("terminal_commands", {})
        if isinstance(raw_terminal_commands, dict):
            # 2026-08-18: il comando resta associato all'identita del terminale
            # per ricreare launcher come Codex dopo la perdita del server tmux.
            for terminal_name, command in raw_terminal_commands.items():
                if (
                    terminal_name in terminal_names
                    and isinstance(command, str)
                    and command.strip()
                    and "\n" not in command
                    and "\r" not in command
                    and "\0" not in command
                ):
                    terminal_commands[terminal_name] = command.strip()
        terminal_icons: dict[str, str] = {}
        raw_terminal_icons = raw.get("terminal_icons", {})
        if isinstance(raw_terminal_icons, dict):
            for terminal_name, icon_name in raw_terminal_icons.items():
                if (
                    terminal_name in terminal_names
                    and is_extra_command_icon(icon_name)
                ):
                    terminal_icons[terminal_name] = icon_name
        last_terminal = raw.get("last_terminal")
        if last_terminal not in terminal_names:
            last_terminal = terminal_names[0] if terminal_names else None
        browsers: list[dict[str, object]] = []
        browser_ids: set[str] = set()
        raw_browsers = raw.get("browsers", [])
        if isinstance(raw_browsers, list):
            # 2026-08-17: anche le tab anonime persistono identità e ultima URL,
            # mai cookie, credenziali o altro stato del profilo effimero WebKit.
            for item in raw_browsers:
                if not isinstance(item, dict):
                    continue
                identifier = str(item.get("id", "")).strip()
                url = _normalize_browser_url(item.get("url"))
                if (
                    not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", identifier)
                    or identifier in browser_ids
                    or url is None
                ):
                    continue
                title = str(item.get("title", "")).strip()[:300]
                private = item.get("private", False) is True
                browsers.append(
                    {
                        "id": identifier,
                        "url": url,
                        "title": title or "Browser",
                        "private": private,
                    }
                )
                browser_ids.add(identifier)
        item_order: list[dict[str, str]] = []
        seen_items: set[tuple[str, str]] = set()
        raw_item_order = raw.get("item_order", [])
        if isinstance(raw_item_order, list):
            # 2026-08-17: terminali, editor e browser normali condividono un
            # ordine persistente, ma ogni riferimento deve esistere davvero.
            for item in raw_item_order:
                if not isinstance(item, dict):
                    continue
                kind = str(item.get("kind", ""))
                raw_value = str(item.get("value", "")).replace("\\", "/")
                value = raw_value.strip("/")
                reference = (kind, value)
                valid_terminal = kind == "terminal" and value in terminal_names
                valid_editor = (
                    kind == "editor"
                    and bool(value)
                    and not raw_value.startswith("/")
                    and ".." not in value.split("/")
                )
                valid_browser = kind == "browser" and value in browser_ids
                if reference in seen_items or not (
                    valid_terminal or valid_editor or valid_browser
                ):
                    continue
                seen_items.add(reference)
                item_order.append({"kind": kind, "value": value})
        for terminal_name in terminal_names:
            reference = ("terminal", terminal_name)
            if reference not in seen_items:
                item_order.append({"kind": "terminal", "value": terminal_name})
                seen_items.add(reference)
        for browser in browsers:
            reference = ("browser", browser["id"])
            if reference not in seen_items:
                item_order.append({"kind": "browser", "value": browser["id"]})
                seen_items.add(reference)
        raw_file_manager = raw.get("file_manager", {})
        if not isinstance(raw_file_manager, dict):
            raw_file_manager = {}
        raw_expanded = raw_file_manager.get("expanded_paths", [])
        expanded_paths: list[str] = []
        if isinstance(raw_expanded, list):
            # 2026-08-16: conserviamo soltanto path relativi canonici; valori
            # assoluti o con traversal non devono uscire dal progetto gestito.
            for item in raw_expanded:
                raw_value = str(item).replace("\\", "/")
                parts = raw_value.split("/")
                if raw_value.startswith("/") or ".." in parts:
                    continue
                value = raw_value.strip("/")
                if value:
                    expanded_paths.append(value)
        raw_repositories = raw.get("repositories", {})
        if not isinstance(raw_repositories, dict):
            raw_repositories = {}
        # 2026-08-17: repository paths stay relative and SCM-typed so the cache
        # cannot escape the workspace and can later coexist with Git entries.
        known_repositories: list[dict[str, str]] = []
        known_refs: set[tuple[str, str]] = set()
        raw_known = raw_repositories.get("known", [])
        if isinstance(raw_known, list):
            for item in raw_known:
                if not isinstance(item, dict) or item.get("type") not in {"hg", "git"}:
                    continue
                path = self._normalize_repository_path(item.get("path"))
                scm_type = str(item.get("type"))
                reference = (path or "", scm_type)
                if path is not None and reference not in known_refs:
                    known_repositories.append({"path": path, "type": scm_type})
                    known_refs.add(reference)
        excluded_repositories: list[dict[str, str]] = []
        excluded_refs: set[tuple[str, str]] = set()
        raw_excluded = raw_repositories.get("excluded", [])
        if isinstance(raw_excluded, list):
            for item in raw_excluded:
                # 2026-08-17: legacy exclusions predate Git and therefore refer
                # specifically to Mercurial repositories at those paths.
                raw_path = item.get("path") if isinstance(item, dict) else item
                scm_type = item.get("type") if isinstance(item, dict) else "hg"
                path = self._normalize_repository_path(raw_path)
                reference = (path or "", str(scm_type))
                if (
                    path is not None
                    and scm_type in {"hg", "git"}
                    and reference not in excluded_refs
                ):
                    excluded_repositories.append(
                        {"path": path, "type": str(scm_type)}
                    )
                    excluded_refs.add(reference)
        return {
            "name": str(raw.get("name", "")).strip(),
            "path": str(raw.get("path", "")).strip(),
            "terminals": terminal_names,
            "terminal_commands": terminal_commands,
            "terminal_icons": terminal_icons,
            "last_terminal": last_terminal,
            "browsers": browsers,
            "item_order": item_order,
            "file_manager": {
                "show_hidden": bool(raw_file_manager.get("show_hidden", False)),
                "show_excluded": bool(raw_file_manager.get("show_excluded", False)),
                "expanded_paths": list(dict.fromkeys(expanded_paths)),
            },
            "repositories": {
                "known": known_repositories,
                "excluded": excluded_repositories,
            },
        }

    @staticmethod
    def _normalize_repository_path(value: Any) -> str | None:
        """Accept only canonical project-relative repository identifiers."""

        raw_value = str(value).replace("\\", "/")
        if raw_value == ".":
            return "."
        path = raw_value.strip("/")
        if not path or raw_value.startswith("/") or ".." in path.split("/"):
            return None
        return path

    def _validate_projects(self, projects: list[dict[str, Any]]) -> None:
        """Reject loaded names and slugs that could attach the wrong tmux session."""

        names: set[str] = set()
        paths: set[str] = set()
        project_slugs: set[str] = set()
        for project in projects:
            name = project["name"]
            path = project["path"]
            project_slug = _tmux_slug(name, 30)
            if (
                not name
                or not path
                or any(character in name for character in "/|\n\r")
                or not project_slug
                or name in names
                or path in paths
                or project_slug in project_slugs
            ):
                raise ValueError("invalid or duplicate project, or ambiguous slug")
            names.add(name)
            paths.add(path)
            project_slugs.add(project_slug)
            terminal_names: set[str] = set()
            terminal_slugs: set[str] = set()
            for terminal_name in project["terminals"]:
                terminal_slug = _tmux_slug(terminal_name, 20)
                if (
                    any(character in terminal_name for character in "/|\n\r")
                    or not terminal_slug
                    or terminal_name in terminal_names
                    or terminal_slug in terminal_slugs
                ):
                    raise ValueError("invalid or duplicate terminal, or ambiguous slug")
                terminal_names.add(terminal_name)
                terminal_slugs.add(terminal_slug)

    def save(self) -> None:
        """Atomically write the complete configuration to its sole file."""

        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f"{self.path.name}.tmp")
        payload = json.dumps(self.data, ensure_ascii=False, indent=2) + "\n"
        with temporary.open("w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.path)
        self.error = None

    def find_project(self, name: str) -> dict[str, Any] | None:
        """Return the project with the exact display name, if present."""

        return next(
            (project for project in self.data["projects"] if project["name"] == name),
            None,
        )
