"""Synthetic chat controls and pre-persistence redaction regression coverage."""
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from scripts import memorycore_ai as bm
from scripts.chat_memory import ChatMemoryPolicy
from scripts.generation_quota import background_propose
from scripts.knowledge_layer import Knowledge, SourceRoot
from scripts.memory_mcp_lab import LabServer
from scripts.secret_redaction import redact_text


class ChatControlTests(unittest.TestCase):
    def observation(self, **changes):
        return dict({"observed_at": 100000, "last_activity_at": 13600,
                     "active": False, "external_context": False}, **changes)

    def test_idle_boundary_and_active_chat(self):
        p = ChatMemoryPolicy()
        self.assertIsNone(p.generation_reason(self.observation(), 100000))
        self.assertEqual(p.generation_reason(self.observation(last_activity_at=13601), 100000), "chat_not_idle_long_enough")
        self.assertEqual(p.generation_reason(self.observation(active=True), 100000), "chat_active")

    def test_external_context_is_optional_and_does_not_disable_recall(self):
        chat = self.observation(external_context=True)
        self.assertIsNone(ChatMemoryPolicy().generation_reason(chat, 100000))
        p = ChatMemoryPolicy(disable_on_external_context=True)
        self.assertEqual(p.generation_reason(chat, 100000), "external_context_excluded")
        self.assertTrue(p.allow_tool("memory_recall"))

    def test_read_and_write_choices_are_independent(self):
        for use in (False, True):
            for generate in (False, True):
                p = ChatMemoryPolicy(use_memories=use, generate_memories=generate)
                self.assertEqual(p.allow_tool("memory_recall"), use)
                self.assertEqual(p.allow_tool("memory_propose"), generate)
                self.assertEqual(p.allow_tool("memory_accept"), generate)
                self.assertTrue(p.allow_tool("memory_forget"))

    def test_invalid_or_stale_observations_fail_closed(self):
        p = ChatMemoryPolicy()
        for o in (None, {}, self.observation(active=1), self.observation(observed_at=99939),
                  self.observation(observed_at=100001), self.observation(last_activity_at=float("nan"))):
            self.assertIsNotNone(p.generation_reason(o, 100000))
        with self.assertRaises(ValueError):
            ChatMemoryPolicy(use_memories=1)

    def test_background_never_reads_source_when_chat_disallows(self):
        k = Mock()
        quota = {"observed_at": 100000, "remaining_percent_by_window": {"primary": 90}}
        for chat in (None, self.observation(active=True), self.observation(last_activity_at=29999)):
            self.assertFalse(background_propose(k, "fixture.md", quota, now=100000, chat=chat)["generated"])
        k.propose.assert_not_called()

    def test_durable_staging_is_independent_of_the_idle_generation_gate(self):
        policy = ChatMemoryPolicy()
        self.assertEqual(
            policy.generation_reason(self.observation(last_activity_at=99999), 100000),
            "chat_not_idle_long_enough",
        )
        with tempfile.TemporaryDirectory() as folder:
            database = Path(folder) / "staging.sqlite3"
            conn = sqlite3.connect(database)
            conn.row_factory = sqlite3.Row
            try:
                bm.initialize(conn)
                receipt = bm.stage(conn, type("Args", (), {
                    "text": "Synthetic completed turn.", "file": None,
                    "scope": "synthetic:chat", "source": "chat",
                    "expires": None,
                })())
                self.assertEqual(receipt["raw_bytes"], len("Synthetic completed turn."))
            finally:
                conn.close()
            reopened = sqlite3.connect(database)
            try:
                self.assertEqual(
                    reopened.execute("SELECT count(*) FROM hippocampus_stage WHERE status=0").fetchone()[0],
                    1,
                )
            finally:
                reopened.close()

    def test_tool_gate_precedes_backend_for_named_and_compact_requests(self):
        k = Mock()
        # _tool is the shared dispatch endpoint after both request formats resolve.
        server = LabServer(k, count=len, chat_policy=ChatMemoryPolicy(use_memories=False, generate_memories=False))
        for name, args in (("memory_recall", {"query": "fixture"}), ("memory_propose", {"path": "fixture.md"}),
                           ("memory_accept", {"id": "a", "review_digest": "b"})):
            with self.assertRaises(ValueError):
                server._tool(name, args)
        self.assertEqual(k.mock_calls, [])


class RedactionTests(unittest.TestCase):
    def test_safe_content_and_exceptions_are_preserved(self):
        text = "Decision: Retry three times.\nNever retry invalid input.\n"
        self.assertEqual(redact_text(text)["text"], text)
        self.assertEqual(redact_text(text)["redactions"], 0)

    def test_credentials_removed_without_returning_originals(self):
        for secret in ("password=synthetic-secret", "Authorization: Bearer synthetic-token-value",
                       "https://test:synthetic@example.invalid/path", "sk-abcdefghijklmnopqrstuvwx"):
            result = redact_text("Fact: Synthetic mode.\n" + secret + "\nNever publish.\n")
            self.assertNotIn(secret, str(result))
            self.assertIn("Never publish.", result["text"])
            self.assertEqual(result["redactions"], 1)

    def test_private_keys_and_ambiguous_multiline(self):
        result = redact_text("-----BEGIN PRIVATE KEY-----\nsynthetic-only\n-----END PRIVATE KEY-----")
        self.assertNotIn("synthetic-only", result["text"])
        for text in ('password="synthetic\ncontinued"', '-----BEGIN PRIVATE KEY-----\nunfinished',
                     'password: |\n  synthetic-value', 'password=synthetic\n  continuation',
                     'password=\nsynthetic-continuation'):
            with self.assertRaises(ValueError):
                redact_text(text)

    def test_non_secret_policy_rejections_remain(self):
        with self.assertRaises(ValueError):
            redact_text("4111 1111 1111 1111")

    def test_redacted_source_proposals_never_persist_secret_and_detect_change(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "fixture.md"
            original = "Fact: Synthetic service.\npassword=synthetic-secret\nNever publish.\n"
            path.write_text(original, encoding="utf-8")
            strict = SourceRoot(folder)
            with self.assertRaises(SystemExit):
                strict.read("fixture.md")
            source = SourceRoot(folder, redact_secrets=True)
            conn = sqlite3.connect(":memory:")
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            try:
                bm.initialize(conn)
                k = Knowledge(conn, scope="synthetic:chat", sources=source, synthetic=True, create=True)
                proposals = k.propose("fixture.md")["proposals"]
                self.assertEqual(len(proposals), 1)
                self.assertNotIn(b"synthetic-secret", conn.serialize())
                self.assertIn("Never publish.", proposals[0]["summary"])
                binding = proposals[0]["binding"]
                self.assertEqual(source.inspect(binding), "fresh")
                path.write_text(original.replace("synthetic-secret", "synthetic-changed"), encoding="utf-8")
                self.assertEqual(source.inspect(binding), "changed")
                with self.assertRaises(ValueError):
                    k.accept(proposals[0]["id"], proposals[0]["review_digest"])
            finally:
                conn.close()

    def test_actual_cli_flags_and_background_observation_contract(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "fixture.md"
            path.write_text("Fact: Synthetic service.\npassword=synthetic-value\nNever publish.\n", encoding="utf-8")
            base = [sys.executable, "-B", "-m", "scripts.knowledge_cli", "--synthetic",
                    "--db", str(Path(folder) / "test.sqlite3"), "--source-root", folder, "--scope", "synthetic:chat"]
            def call(operation, data=None, flags=()):
                return subprocess.run(base + list(flags) + [operation], input=json.dumps(data),
                    text=True, capture_output=True, cwd=Path(__file__).resolve().parents[1])
            self.assertEqual(call("init").returncode, 0)
            request = {"name": "memory", "arguments": {"propose": "fixture.md"}}
            self.assertNotEqual(call("call", request, ["--no-generate-memories"]).returncode, 0)
            result = call("call", request, ["--redact-secrets"])
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("synthetic-value", result.stdout)
            self.assertNotEqual(call("call", {"name": "memory", "arguments": {"recall": "service"}},
                ["--no-use-memories"]).returncode, 0)
            result = call("background-propose", {"path": "fixture.md", "quota": None, "chat": None})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["reason"], "chat_state_unavailable")
            self.assertNotIn(b"synthetic-value", (Path(folder) / "test.sqlite3").read_bytes())


if __name__ == "__main__":
    unittest.main()
