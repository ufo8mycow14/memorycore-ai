#!/usr/bin/env python3

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts import memorycore_ai as bm

SCRIPT = Path(__file__).with_name("memorycore_ai.py")


class MemoryCoreAITests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db = str(Path(self.temp_dir.name) / "memory.sqlite3")

    def tearDown(self):
        self.temp_dir.cleanup()

    def run_cli(self, *args, check=True, parse_json=True):
        result = subprocess.run(
            [sys.executable, "-B", str(SCRIPT), "--db", self.db, *args],
            text=True,
            capture_output=True,
            check=check,
        )
        return json.loads(result.stdout) if result.stdout and parse_json else result

    def test_stage_uses_128_bit_id_and_compresses(self):
        result = self.run_cli("stage", "--text", "remember this " * 100, "--scope", "project:test")
        self.assertEqual(len(result["stage_id"]), 32)
        self.assertLess(result["stored_bytes"], result["raw_bytes"])

    def test_deduplicate_recall_forget_and_restore(self):
        command = (
            "remember", "--type", "semantic", "--scope", "project:test",
            "--subject", "Region", "--summary", "The region is australia-southeast1.",
            "--keywords", "firebase,region", "--importance", "0.9", "--source", "user",
        )
        first = self.run_cli(*command)
        second = self.run_cli(*command)
        self.assertEqual(first["memory_id"], second["memory_id"])
        self.assertTrue(second["deduplicated"])
        self.assertEqual(len(first["memory_id"]), 32)

        recalled = self.run_cli("recall", "firebase region", "--scope", "project:test", "--format", "json")
        self.assertEqual(recalled["count"], 1)
        self.assertEqual(recalled["memories"][0]["summary"], "The region is australia-southeast1.")

        self.run_cli("forget", first["memory_id"], "--scope", "project:test")
        self.assertEqual(self.run_cli("recall", "region", "--scope", "project:test", "--format", "json")["count"], 0)
        self.run_cli("restore", first["memory_id"], "--scope", "project:test")
        self.assertEqual(self.run_cli("recall", "region", "--scope", "project:test", "--format", "json")["count"], 1)

        prompt = self.run_cli("recall", "region", "--scope", "project:test", "--max-chars", "1000", parse_json=False)
        self.assertIsInstance(prompt, subprocess.CompletedProcess)
        self.assertIn('"subject":"Region"', prompt.stdout)
        self.assertLessEqual(len(prompt.stdout), 1000)

    def test_classical_conditioning_requires_confirmation(self):
        result = self.run_cli(
            "remember", "--scope", "project:test", "--type", "classical_conditioning", "--subject", "Trigger",
            "--summary", "When X happens, do Y.", check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("requires --user-confirmed", result.stderr)

    def test_full_fidelity_round_trip_is_byte_exact(self):
        original = b"Exact bytes with CRLF: \r\nUTF-8 follows: \xe2\x98\x83"
        source = Path(self.temp_dir.name) / "original.bin"
        source.write_bytes(original)
        receipt = self.run_cli(
            "store-exact", "--file", str(source), "--media-type", "text/plain",
            "--scope", "project:test", "--user-confirmed",
        )
        self.assertEqual(len(receipt["archive_id"]), 32)
        self.assertEqual(receipt["original_bytes"], len(original))
        self.assertTrue(receipt["verified"])
        self.assertEqual(receipt["encryption"], "UNENCRYPTED_PROTOTYPE")

        recalled = subprocess.run(
            [sys.executable, "-B", str(SCRIPT), "--db", self.db, "recall-exact", receipt["archive_id"], "--scope", "project:test"],
            capture_output=True,
            check=True,
        )
        self.assertEqual(recalled.stdout, original)

    def test_archive_prune_and_confirmed_purge(self):
        memory = self.run_cli(
            "remember", "--type", "episodic", "--scope", "project:test",
            "--subject", "Old event", "--summary", "A low-value event completed.",
            "--importance", "0.1",
        )
        memory_id = memory["memory_id"]

        self.assertEqual(self.run_cli("archive", memory_id, "--scope", "project:test")["status"], "archived")
        self.assertEqual(self.run_cli("recall", "event", "--scope", "project:test", "--format", "json")["count"], 0)
        self.assertEqual(self.run_cli("unarchive", memory_id, "--scope", "project:test")["status"], "active")

        with sqlite3.connect(self.db) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("UPDATE cortex_memory SET created_at='2020-01-01T00:00:00+00:00' WHERE memory_id=?", (bytes.fromhex(memory_id),))
            bm.seal_row(conn, "cortex_memory", "memory_id", bytes.fromhex(memory_id))
        preview = self.run_cli("prune", "--scope", "project:test", "--older-than-days", "180", "--importance-below", "0.25")
        self.assertEqual(preview["mode"], "preview")
        self.assertEqual(preview["count"], 1)
        self.assertEqual(self.run_cli("recall", "event", "--scope", "project:test", "--format", "json")["count"], 1)

        rejected = self.run_cli("prune", "--apply", check=False)
        self.assertNotEqual(rejected.returncode, 0)
        applied = self.run_cli("prune", "--scope", "project:test", "--older-than-days", "180", "--importance-below", "0.25", "--reviewed-id", preview["candidates"][0]["review_token"], "--apply", "--user-confirmed")
        self.assertEqual(applied["mode"], "applied")
        listing = self.run_cli("list", "--scope", "project:test", "--status", "archived")
        self.assertEqual(listing["count"], 1)

        rejected = self.run_cli("purge", memory_id, check=False)
        self.assertNotEqual(rejected.returncode, 0)
        purged = self.run_cli("purge", memory_id, "--scope", "project:test", "--user-confirmed")
        self.assertEqual(purged["status"], "purged")
        self.assertEqual(self.run_cli("list", "--scope", "project:test")["count"], 0)

    def test_default_database_uses_dedicated_home(self):
        app_home = Path(self.temp_dir.name) / "MemoryCoreAIHome"
        environment = os.environ.copy()
        environment["MEMORYCORE_AI_HOME"] = str(app_home)
        result = subprocess.run(
            [sys.executable, "-B", str(SCRIPT), "init"],
            text=True,
            capture_output=True,
            check=True,
            env=environment,
        )
        initialized = json.loads(result.stdout)
        expected = app_home / "vault" / "memorycore-ai.sqlite3"
        self.assertEqual(Path(initialized["database"]), expected)
        self.assertTrue(expected.exists())
        if os.name != "nt":
            self.assertEqual(expected.parent.stat().st_mode & 0o777, 0o700)
            self.assertEqual(expected.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
