"""Installer regression checks use only disposable synthetic configuration."""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import patch

from scripts import setup_local_rollout as setup


class SetupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="memorycore-setup-synthetic-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = self.root / "config.toml"

    def append(self, **kwargs):
        return setup.append_mcp_config(self.config, "fixture", Path(sys.executable),
                                       self.root / "host.json", self.root / "cache",
                                       self.root / "metrics.jsonl", **kwargs)

    def test_preview_has_no_filesystem_side_effects(self):
        with patch.object(sys, "argv", ["setup", "--dry-run", "--home", str(self.root / "new"),
                                       "--codex-config", str(self.config)]), contextlib.redirect_stdout(io.StringIO()):
            setup.main()
        self.assertEqual(list(self.root.iterdir()), [])

    def test_registration_preserves_existing_config_and_is_idempotent(self):
        original = '# Preserve comments\n[other]\nvalue = "existing"\n'
        self.config.write_text(original, encoding="utf-8")
        result = self.append()
        self.assertEqual(Path(result["backup"]).read_text(encoding="utf-8"), original)
        content = self.config.read_bytes()
        self.assertEqual(self.append()["reason"], "already_present")
        self.assertEqual(self.config.read_bytes(), content)
        self.assertEqual(tomllib.loads(content.decode())["other"]["value"], "existing")

    def test_quoted_existing_name_conflict_is_not_overwritten(self):
        original = '[mcp_servers."fixture"]\ncommand = "other"\n'
        self.config.write_text(original, encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "differs"):
            self.append()
        self.assertEqual(self.config.read_text(encoding="utf-8"), original)

    def test_paths_with_quotes_round_trip(self):
        host = self.root / "owner's \"fixture\".json"
        setup.append_mcp_config(self.config, "fixture", Path(sys.executable), host,
                                self.root, self.root / "metrics")
        entry = tomllib.loads(self.config.read_text(encoding="utf-8"))["mcp_servers"]["fixture"]
        self.assertIn(str(host), entry["args"])

    def test_disabled_registration_is_not_reported_installed(self):
        self.append()
        with self.config.open("a", encoding="utf-8") as stream:
            stream.write("enabled = false\n")
        with self.assertRaisesRegex(ValueError, "disabled"):
            self.append()

    def test_invalid_name_rejected_before_write(self):
        with self.assertRaises(ValueError):
            setup.append_mcp_config(self.config, "bad.name", Path(sys.executable), self.root, self.root, self.root)
        self.assertFalse(self.config.exists())

    def test_existing_retention_preserved_and_failed_startup_not_registered(self):
        host = self.root / "host.json"
        original = json.dumps({"synthetic": True, "backend": "native", "database": str(self.root / "db"),
                               "sessions": [{"id": "memorycore-ai-local", "archive_delete_after_days": 730}]})
        host.write_text(original, encoding="utf-8")
        with patch.object(sys, "argv", ["setup", "--home", str(self.root), "--codex-config", str(self.config),
                                       "--skip-deps", "--skip-models"]), patch.object(setup, "run"), \
                patch.object(setup, "verify_mcp", side_effect=ValueError("startup failed")):
            with self.assertRaisesRegex(ValueError, "startup failed"):
                setup.main()
        self.assertEqual(host.read_text(encoding="utf-8"), original)
        self.assertFalse(self.config.exists())

    def test_real_monitored_mcp_starts_and_exits(self):
        if not setup.BINARY.exists():
            self.skipTest("Native binary required")
        sources = self.root / "sources"
        sources.mkdir()
        host = self.root / "host.json"
        host.write_text(json.dumps({"synthetic": True, "backend": "native", "allow_plaintext": True, "read_workers": 4,
            "database": str(self.root / "fixture.sqlite3"), "sessions": [{"id": "memorycore-ai-local",
            "scope": "synthetic:setup", "source_root": str(sources), "allow_admin": True}]}), encoding="utf-8")
        setup.run([str(setup.BINARY), "--init", "--config", str(host)])
        result = setup.verify_mcp(Path(sys.executable), host,
            Path(os.environ.get("MEMORYCORE_AI_MODEL_CACHE", str(self.root / "cache"))), self.root / "metrics.jsonl")
        self.assertEqual(result, {"initialize": True, "memory_tool": True, "ping": True})


if __name__ == "__main__":
    unittest.main()
