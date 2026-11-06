"""Configuration persistence and recovery tests."""

import json
import tempfile
import unittest
from pathlib import Path

from slate.config import (
    BROWSER_VIEWPORT_PRESETS,
    ConfigStore,
    DEFAULT_EXTRA_COMMAND_ICON,
    DEFAULT_CONFIG,
    new_project_config,
)


class ConfigStoreTest(unittest.TestCase):
    """Verify the single config file remains safe and human-readable."""

    def test_project_name_and_path_keep_literal_surrounding_spaces(self) -> None:
        """A saved project must never resolve to a different sibling after config reload."""

        with tempfile.TemporaryDirectory(dir="/tmp", prefix="slate-path-spaces-") as folder:
            root = Path(folder)
            selected = root / " project "
            selected.mkdir()
            (selected / "file.txt").write_text("selected\n")
            sibling = root / "project"
            sibling.mkdir()
            (sibling / "file.txt").write_text("different\n")
            config = ConfigStore(root / "config.json")
            config.data["projects"].append(new_project_config(selected.name, str(selected), ("main",)))
            config.save()
            restored = ConfigStore(root / "config.json")
            self.assertIsNone(restored.error)
            project = restored.data["projects"][0]
            self.assertEqual(project["name"], selected.name)
            self.assertEqual(project["path"], str(selected))
            self.assertEqual((Path(project["path"]) / "file.txt").read_text(), "selected\n")

    def test_terminal_history_ids_persist_and_exclude_custom_launchers(self) -> None:
        """Legacy configs get distinct stable identities only for plain terminals."""

        with tempfile.TemporaryDirectory(prefix="slate-config-", dir="/tmp") as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps({"projects": [{
                "name": "repo", "path": directory,
                "terminals": ["one", "two", "custom"],
                "terminal_commands": {"custom": "codex resume"},
                "terminal_history_ids": {"one": "../invalid", "custom": "f" * 32},
            }]}))
            config = ConfigStore(path)
            ids = config.data["projects"][0]["terminal_history_ids"]
            self.assertEqual(set(ids), {"one", "two"})
            self.assertNotEqual(ids["one"], ids["two"])
            self.assertRegex(ids["one"], r"^[0-9a-f]{32}$")
            config.save()
            restored = ConfigStore(path)
            self.assertEqual(restored.data["projects"][0]["terminal_history_ids"], ids)

    def test_new_project_schema_supports_manual_and_adopted_initial_state(self) -> None:
        """One factory owns every field while callers choose initial terminals."""

        manual = new_project_config("repo", "/tmp/repo", ("main",))
        adopted = new_project_config("repo", "/tmp/repo")
        self.assertEqual(manual["terminals"], ["main"])
        self.assertEqual(manual["last_terminal"], "main")
        self.assertEqual(manual["terminal_icons"], {})
        self.assertEqual(
            manual["item_order"],
            [{"kind": "terminal", "value": "main"}],
        )
        self.assertEqual(adopted["terminals"], [])
        self.assertIsNone(adopted["last_terminal"])
        self.assertEqual(set(manual), set(adopted))

    def test_browser_viewports_match_the_curated_ordered_catalog(self) -> None:
        """Keep only selected device data and standard desktop dimensions."""

        self.assertEqual(
            [
                (name, preset.label, preset.width, preset.height)
                for name, preset in BROWSER_VIEWPORT_PRESETS.items()
            ],
            [
                ("galaxy-a-series", "Samsung Galaxy A15/A16/A5x — 360 × 800", 360, 800),
                ("iphone-x", "iPhone X/XS/11 Pro — 375 × 812", 375, 812),
                ("samsung-mid-range", "Samsung mid-range — 384 × 832", 384, 832),
                ("redmi-note", "Xiaomi Redmi Note — 393 × 873", 393, 873),
                ("iphone-xr", "iPhone XR/11/11 Pro Max — 414 × 896", 414, 896),
                ("ipad-classic", "iPad mini/classic — 768 × 1024", 768, 1024),
                ("ipad-10", "iPad 10th gen — 820 × 1180", 820, 1180),
                ("desktop-xga", "Desktop XGA — 1024 × 768", 1024, 768),
                ("laptop-hd", "Laptop HD — 1366 × 768", 1366, 768),
                ("laptop", "Laptop — 1440 × 900", 1440, 900),
                ("laptop-scaled", "Laptop 1080p, scaling 125% — 1536 × 864", 1536, 864),
                ("desktop-fhd", "Desktop Full HD — 1920 × 1080", 1920, 1080),
                ("desktop-qhd", "Desktop QHD — 2560 × 1440", 2560, 1440),
            ],
        )

    def test_missing_file_uses_independent_defaults(self) -> None:
        """A missing file starts empty without creating anything on disk."""

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            store = ConfigStore(path)
            self.assertEqual(store.data, DEFAULT_CONFIG)
            self.assertFalse(path.exists())

    def test_corrupt_file_is_not_silently_rewritten(self) -> None:
        """Unreadable JSON survives loading until an explicit later save."""

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text("{broken", encoding="utf-8")
            store = ConfigStore(path)
            self.assertIsNotNone(store.error)
            self.assertEqual(path.read_text(encoding="utf-8"), "{broken")

    def test_save_replaces_tmp_with_complete_json(self) -> None:
        """Atomic save leaves one complete config and no temporary sibling."""

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            store = ConfigStore(path)
            store.data["projects"].append(
                {
                    "name": "prova",
                    "path": "/tmp/prova",
                    "terminals": ["main"],
                    "last_terminal": "main",
                }
            )
            store.save()
            self.assertEqual(json.loads(path.read_text())["projects"][0]["name"], "prova")
            self.assertFalse(path.with_name("config.json.tmp").exists())

    def test_normalization_repairs_only_in_memory(self) -> None:
        """Invalid last-terminal values fall back to the first listed terminal."""

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "projects": [
                            {
                                "name": "p",
                                "path": "/tmp/p",
                                "terminals": ["main", "main", "logs"],
                                "last_terminal": "missing",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            store = ConfigStore(path)
            project = store.data["projects"][0]
            self.assertEqual(project["terminals"], ["main", "logs"])
            self.assertEqual(project["terminal_commands"], {})
            self.assertEqual(project["terminal_icons"], {})
            self.assertEqual(project["last_terminal"], "main")
            self.assertEqual(
                project["item_order"],
                [
                    {"kind": "terminal", "value": "main"},
                    {"kind": "terminal", "value": "logs"},
                ],
            )

    def test_pinned_projects_keep_fifo_order_and_known_names_only(self) -> None:
        """Pin persistence drops duplicates and stale project references."""

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "projects": [
                            {"name": "alpha", "path": "/tmp/alpha"},
                            {"name": "beta", "path": "/tmp/beta"},
                        ],
                        "pinned_projects": ["beta", "missing", "alpha", "beta"],
                    }
                ),
                encoding="utf-8",
            )

            store = ConfigStore(path)

            self.assertEqual(store.data["pinned_projects"], ["beta", "alpha"])

    def test_terminal_commands_are_safe_and_reference_known_terminals(self) -> None:
        """Only single-line commands for configured terminals survive loading."""

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "projects": [
                            {
                                "name": "p",
                                "path": "/tmp/p",
                                "terminals": ["main", "codex-1", "logs"],
                                "terminal_commands": {
                                    "codex-1": "  codex resume  ",
                                    "main": "printf ok\nexit",
                                    "logs": "printf bad\0command",
                                    "missing": "other-agent resume",
                                },
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            project = ConfigStore(path).data["projects"][0]

            self.assertEqual(
                project["terminal_commands"], {"codex-1": "codex resume"}
            )

    def test_extra_commands_and_terminal_icons_are_normalized(self) -> None:
        """Global launchers and per-terminal icons retain only safe catalog data."""

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "settings": {
                            "commands": {
                                "items": [
                                    {
                                        "label": " Logs ",
                                        "command": "  journalctl -f  ",
                                        "icon": "text-x-script",
                                    },
                                    {
                                        "label": "logs",
                                        "command": "duplicate",
                                        "icon": DEFAULT_EXTRA_COMMAND_ICON,
                                    },
                                    {
                                        "label": "Broken",
                                        "command": "printf '",
                                        "icon": "system-run",
                                    },
                                ]
                            }
                        },
                        "projects": [
                            {
                                "name": "p",
                                "path": "/tmp/p",
                                "terminals": ["logs", "main"],
                                "terminal_icons": {
                                    "logs": "text-x-script",
                                    "main": "../../unknown-icon",
                                    "missing": "system-run",
                                },
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            data = ConfigStore(path).data

            self.assertEqual(
                data["settings"]["commands"]["items"],
                [
                    {
                        "label": "Logs",
                        "command": "journalctl -f",
                        "icon": "text-x-script",
                    }
                ],
            )
            self.assertEqual(
                data["projects"][0]["terminal_icons"],
                {"logs": "text-x-script"},
            )

    def test_file_manager_preferences_are_safe_and_backward_compatible(self) -> None:
        """Per-project file preferences retain only safe relative expanded paths."""

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "projects": [
                            {
                                "name": "p",
                                "path": "/tmp/p",
                                "terminals": [],
                                "file_manager": {
                                    "show_hidden": True,
                                    "show_excluded": True,
                                    "expanded_paths": ["src", "src/lib", "../fuori", "/etc"],
                                },
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            project = ConfigStore(path).data["projects"][0]
            self.assertEqual(
                project["file_manager"],
                {
                    "show_hidden": True,
                    "show_excluded": True,
                    "expanded_paths": ["src", "src/lib"],
                },
            )

    def test_global_font_settings_are_normalized_independently(self) -> None:
        """Valid sizes survive while unsafe hand-edited values use defaults."""

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "settings": {
                            "revisions": {"font_size": 17},
                            "files": {"font_size": 200},
                            "terminal": {
                                "status_bar": True,
                            },
                        }
                    }
                ),
                encoding="utf-8",
            )
            settings = ConfigStore(path).data["settings"]
            self.assertEqual(settings["revisions"]["font_size"], 17)
            self.assertEqual(settings["files"]["font_size"], 10)
            self.assertEqual(settings["editor"]["font_size"], 10)
            self.assertTrue(settings["general"]["close_to_tray"])
            self.assertNotIn("external_apps", settings)
            self.assertTrue(settings["terminal"]["status_bar"])


    def test_editor_appearance_round_trips_valid_values_and_rejects_css_or_invalid_colors(self) -> None:
        """Appearance stays in the global JSON as bounded RGB values and a scheme ID."""

        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps({"settings": {"editor": {
                "font_size": 17, "font_family": "JetBrains Mono", "style_scheme": "oblivion", "text_color": "#ABCDEF",
                "diff_added_color": "#123456", "diff_removed_color": "#654321",
            }}}))
            store = ConfigStore(path)
            self.assertEqual(store.data["settings"]["editor"]["text_color"], "#abcdef")
            self.assertEqual(store.data["settings"]["editor"]["font_family"], "JetBrains Mono")
            store.save()
            self.assertEqual(ConfigStore(path).data["settings"]["editor"], store.data["settings"]["editor"])
            path.write_text(json.dumps({"settings": {"editor": {
                "font_size": 17, "font_family": "invalid\nfamily", "style_scheme": "../invalid", "text_color": "red; background: blue",
                "diff_added_color": "#abc", "diff_removed_color": ["#123456"],
            }}}))
            editor = ConfigStore(path).data["settings"]["editor"]
            self.assertEqual(editor, {"font_size": 17, "font_family": "monospace", "style_scheme": "", "text_color": None,
                                      "diff_added_color": None, "diff_removed_color": None})

    def test_obsolete_external_editor_settings_are_dropped_on_save(self) -> None:
        """Old external-editor preferences disappear without losing active settings."""

        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps({"settings": {
                "external_apps": {"editor_command": ["gvim", "-f"]},
                "editor": {"font_size": 15},
                "terminal": {"status_bar": True},
            }}), encoding="utf-8")
            store = ConfigStore(path)
            self.assertNotIn("external_apps", store.data["settings"])
            store.save()
            saved = json.loads(path.read_text(encoding="utf-8"))["settings"]
            self.assertNotIn("external_apps", saved)
            self.assertEqual(saved["editor"]["font_size"], 15)
            self.assertTrue(saved["terminal"]["status_bar"])

    def test_revision_expansion_is_not_loaded_from_disk(self) -> None:
        """Revision branches always start expanded instead of reading config state."""

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "settings": {
                            "revisions": {
                                "font_size": 10,
                                "expanded_rows": [],
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            settings = ConfigStore(path).data["settings"]
            self.assertEqual(settings["revisions"], {"font_size": 10})

    def test_repository_cache_keeps_only_safe_typed_paths(self) -> None:
        """Repository state preserves Git/HG types and rejects unsafe paths."""

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "projects": [
                            {
                                "name": "p",
                                "path": "/tmp/p",
                                "terminals": [],
                                "repositories": {
                                    "known": [
                                        {"path": ".", "type": "hg"},
                                        {"path": "app", "type": "hg"},
                                        {"path": "app", "type": "hg"},
                                        {"path": "../outside", "type": "hg"},
                                        {"path": "future", "type": "git"},
                                    ],
                                    "excluded": ["legacy", "/outside", "../bad"],
                                },
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            repositories = ConfigStore(path).data["projects"][0]["repositories"]
            self.assertEqual(
                repositories["known"],
                [
                    {"path": ".", "type": "hg"},
                    {"path": "app", "type": "hg"},
                    {"path": "future", "type": "git"},
                ],
            )
            self.assertEqual(
                repositories["excluded"],
                [{"path": "legacy", "type": "hg"}],
            )

    def test_linux_backslashes_survive_config_round_trip_without_aliasing_slashes(self) -> None:
        """Editor, ordering, expanded directory and repository references retain literal backslashes."""

        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            path = Path(directory) / "config.json"
            literal = "a\\b.txt"
            raw = {"projects": [{"name": "project", "path": directory, "terminals": [],
                   "item_order": [{"kind": "editor", "value": literal}],
                   "file_manager": {"expanded_paths": ["dir\\name", "dir/name"]},
                   "repositories": {"known": [{"path": "repo\\name", "type": "git"}]}}],
                   "editor": {"tabs": [{"project": "project", "path": literal},
                                       {"project": "project", "path": "a/b.txt"}],
                              "active_tab": {"project": "project", "path": literal}}}
            store = ConfigStore(path)
            store.data = store._normalize(raw)
            store.save()
            restored = ConfigStore(path).data
            self.assertEqual([tab["path"] for tab in restored["editor"]["tabs"]], [literal, "a/b.txt"])
            self.assertEqual(restored["editor"]["active_tab"]["path"], literal)
            project = restored["projects"][0]
            self.assertEqual(project["item_order"][0]["value"], literal)
            self.assertEqual(project["file_manager"]["expanded_paths"], ["dir\\name", "dir/name"])
            self.assertEqual(project["repositories"]["known"][0]["path"], "repo\\name")

    def test_editor_rows_keep_only_safe_known_project_paths(self) -> None:
        """Persistent editors reject traversal, duplicates and unknown projects."""

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "projects": [
                            {"name": "p", "path": "/tmp/p", "terminals": []}
                        ],
                        "editor": {
                            "tabs": [
                                {"project": "p", "path": "TODO.md"},
                                {"project": "p", "path": "TODO.md"},
                                {"project": "p", "path": "../secret"},
                                {"project": "missing", "path": "AGENTS.md"},
                            ],
                            "active_tab": {"project": "p", "path": "TODO.md"},
                        },
                    }
                ),
                encoding="utf-8",
            )
            editor = ConfigStore(path).data["editor"]
            self.assertEqual(
                editor["tabs"], [{"project": "p", "path": "TODO.md"}]
            )
            self.assertEqual(
                editor["active_tab"], {"project": "p", "path": "TODO.md"}
            )

    def test_malformed_browser_url_does_not_discard_other_configuration(self) -> None:
        """An invalid IPv6 tab or Home URL cannot erase otherwise valid projects and settings."""

        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            path = Path(directory) / "config.json"
            raw = {"projects": [
                {"name": "first", "path": directory, "terminals": ["main"], "browsers": [
                    {"id": "good", "url": "https://example.com/", "title": "good"},
                    {"id": "bad", "url": "http://[::1", "title": "bad"},
                    {"id": "bad-home", "url": "https://other.example/", "home_url": "https://[invalid]/"}]},
                {"name": "second", "path": str(Path(directory) / "second"), "terminals": ["logs"]}],
                "settings": {"editor": {"font_size": 14}}, "pinned_projects": ["first"],
                "active_terminal": "first/main"}
            original = json.dumps(raw)
            path.write_text(original)
            store = ConfigStore(path)
            self.assertIsNone(store.error)
            self.assertEqual([project["name"] for project in store.data["projects"]], ["first", "second"])
            self.assertEqual(store.data["settings"]["editor"]["font_size"], 14)
            self.assertEqual(store.data["pinned_projects"], ["first"])
            self.assertEqual(store.data["active_terminal"], "first/main")
            browsers = store.data["projects"][0]["browsers"]
            self.assertEqual([browser["id"] for browser in browsers], ["good", "bad-home"])
            self.assertIsNone(browsers[1]["home_url"])
            self.assertEqual(path.read_text(), original)

    def test_browser_rows_keep_only_safe_normal_page_metadata(self) -> None:
        """Browser normalization rejects private, duplicate and unsafe records."""

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "projects": [
                            {
                                "name": "p",
                                "path": "/tmp/p",
                                "terminals": [],
                                "browsers": [
                                    {
                                        "id": "browser-1",
                                        "url": "https://example.com/a",
                                        "title": "A",
                                    },
                                    {
                                        "id": "browser-1",
                                        "url": "https://duplicate.example",
                                        "title": "Duplicato",
                                    },
                                    {
                                        "id": "browser-2",
                                        "url": "https://example.com",
                                        "home_url": "https://home.example/start",
                                        "title": "Anonimo",
                                        "private": True,
                                    },
                                    {
                                        "id": "browser-3",
                                        "url": "file:///etc/passwd",
                                        "title": "File",
                                    },
                                ],
                                "item_order": [
                                    {"kind": "browser", "value": "browser-1"},
                                    {"kind": "browser", "value": "browser-2"},
                                    {"kind": "browser", "value": "browser-3"},
                                ],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            project = ConfigStore(path).data["projects"][0]
            self.assertEqual(
                project["browsers"],
                [
                    {
                        "id": "browser-1",
                        "url": "https://example.com/a",
                        "home_url": "https://example.com/a",
                        "title": "A",
                        "private": False,
                    },
                    {
                        "id": "browser-2",
                        "url": "https://example.com",
                        "home_url": "https://home.example/start",
                        "title": "Anonimo",
                        "private": True,
                    }
                ],
            )
            self.assertEqual(
                project["item_order"],
                [
                    {"kind": "browser", "value": "browser-1"},
                    {"kind": "browser", "value": "browser-2"},
                ],
            )

    def test_slug_collision_marks_manual_config_as_corrupt(self) -> None:
        """Two display names may not silently address the same tmux session."""

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "projects": [
                            {"name": "Mio sito", "path": "/a", "terminals": []},
                            {"name": "mio-sito", "path": "/b", "terminals": []},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            store = ConfigStore(path)
            self.assertIsNotNone(store.error)
            self.assertEqual(store.data["projects"], [])


if __name__ == "__main__":
    unittest.main()
