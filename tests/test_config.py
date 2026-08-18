"""Configuration persistence and recovery tests."""

import json
import tempfile
import unittest
from pathlib import Path

from slate.config import (
    BROWSER_VIEWPORT_PRESETS,
    ConfigStore,
    DEFAULT_CONFIG,
    new_project_config,
)


class ConfigStoreTest(unittest.TestCase):
    """Verify the single config file remains safe and human-readable."""

    def test_new_project_schema_supports_manual_and_adopted_initial_state(self) -> None:
        """One factory owns every field while callers choose initial terminals."""

        manual = new_project_config("repo", "/tmp/repo", ("main",))
        adopted = new_project_config("repo", "/tmp/repo")
        self.assertEqual(manual["terminals"], ["main"])
        self.assertEqual(manual["last_terminal"], "main")
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
                ("ipad-classic", "iPad mini/classico — 768 × 1024", 768, 1024),
                ("ipad-10", "iPad 10ª gen — 820 × 1180", 820, 1180),
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
            self.assertEqual(project["last_terminal"], "main")
            self.assertEqual(
                project["item_order"],
                [
                    {"kind": "terminal", "value": "main"},
                    {"kind": "terminal", "value": "logs"},
                ],
            )

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
            self.assertTrue(settings["terminal"]["status_bar"])

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
                        "title": "A",
                        "private": False,
                    },
                    {
                        "id": "browser-2",
                        "url": "https://example.com",
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
