"""Acceptance tests for the audit repairs; synthetic fixtures only."""

import argparse
import ast
import contextlib
import hashlib
import io
import json
import random
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from scripts import memorycore_ai as bm
from scripts.memory_packets import RecallSession, render_packet, token_counter
from scripts.memory_policy import check_exact, check_text, timestamp
from scripts.test_memorycore_ai_regressions import memory_args


def ns(**values):
    return argparse.Namespace(**values)


class RecommendationTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        bm.initialize(self.conn)
        self.scope = "project:fixture-a"

    def tearDown(self):
        self.conn.close()

    def save(self, **overrides):
        return bm.remember(self.conn, memory_args(**overrides))

    def read(self, **overrides):
        values = dict(query="decision", scope=self.scope, type=None, limit=8, include_detail=False)
        values.update(overrides)
        return bm.recall(self.conn, ns(**values))

    def stage(self, **overrides):
        values = dict(text="Synthetic source material.", file=None, scope=self.scope, source="synthetic", expires=None)
        values.update(overrides)
        return bm.stage(self.conn, ns(**values))

    def exact(self, **overrides):
        values = dict(text="Synthetic original.\r\n", file=None, scope=self.scope, source="synthetic",
                      media_type="text/plain", retention="until_user_deletes", expires=None, pinned=False, user_confirmed=True)
        values.update(overrides)
        return bm.store_exact(self.conn, ns(**values))

    def life(self, saved, action, **overrides):
        values = dict(memory_id=saved.get("memory_id", saved.get("archive_id")), action=action, scope=self.scope)
        values.update(overrides)
        return bm.lifecycle(self.conn, ns(**values))

    def test_write_cli_requires_scope(self):
        commands = [["stage", "--text", "fixture"], ["remember", "--type", "semantic", "--subject", "test", "--summary", "test"],
                    ["store-exact", "--text", "fixture", "--user-confirmed"], ["list"], ["prune"], ["expire"], ["stats"]]
        for command in commands:
            with self.subTest(command=command), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                bm.build_parser().parse_args(command)

    def test_missing_read_does_not_create_database(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "absent" / "memory.sqlite3"
            with self.assertRaises(SystemExit):
                bm.connect(str(path), read_only=True)
            self.assertFalse(path.parent.exists())

    def test_gate_rejects_before_inserts(self):
        for operation in (lambda: self.stage(text="password = SYNTHETIC_FAKE_VALUE"),
                          lambda: self.save(summary="api_key = SYNTHETIC_FAKE_VALUE"),
                          lambda: self.exact(text="Authorization: Bearer SYNTHETIC_FAKE_VALUE")):
            with self.assertRaises(SystemExit):
                operation()
        for table in ("hippocampus_stage", "cortex_memory", "cortex_verbatim"):
            self.assertEqual(self.conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0], 0)

    def test_policy_allows_references_and_opaque_ids(self):
        for text in ("The API key is stored in the password manager.", "user@example.com", "token count is 700", "Certificate expiry is October."):
            self.assertEqual(check_text(text), text)
        for i in range(50):
            self.save(subject=f"Synthetic {i}")

    def test_exact_rejects_uninspectable_and_decodes_json_escapes(self):
        for data, media in ((b"\x00\xff", "text/plain"), (b"synthetic", "application/octet-stream"),
                            (b'{"pass\\u0077ord":"SYNTHETIC_FAKE_VALUE"}', "application/json")):
            with self.subTest(media=media), self.assertRaises(SystemExit):
                check_exact(data, media)

    def test_stage_scope_failure_is_atomic(self):
        staged = self.stage(scope="project:fixture-b")
        before = self.conn.total_changes
        with self.assertRaises(SystemExit):
            self.save(stage_id=staged["stage_id"])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM cortex_memory").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT status FROM hippocampus_stage").fetchone()[0], 0)

    def test_expired_or_corrupt_stage_cannot_consolidate(self):
        expired = self.stage(expires="2020-01-01T00:00:00Z")
        with self.assertRaises(SystemExit):
            self.save(stage_id=expired["stage_id"])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM cortex_memory").fetchone()[0], 0)
        fresh = self.stage()
        self.conn.execute("UPDATE hippocampus_stage SET raw_blob=? WHERE stage_id=?", (bm.compress(b"changed"), bytes.fromhex(fresh["stage_id"])))
        with self.assertRaises(SystemExit):
            self.save(stage_id=fresh["stage_id"])

    def test_stage_disposal_and_default_ttl(self):
        staged = self.stage()
        self.assertIsNotNone(staged["expires_at"])
        self.save(stage_id=staged["stage_id"])
        row = self.conn.execute("SELECT * FROM hippocampus_stage").fetchone()
        self.assertEqual((row["status"], row["raw_blob"], row["raw_bytes"]), (1, b"", 0))
        self.stage(expires="2020-01-01T00:00:00Z")
        bm.expire(self.conn, ns(scope=self.scope))
        self.assertEqual(self.conn.execute("SELECT sum(raw_bytes) FROM hippocampus_stage").fetchone()[0], 0)

    def test_duplicate_deleted_memory_rejected(self):
        saved = self.save()
        self.life(saved, "forget")
        with self.assertRaises(SystemExit):
            self.save()
        self.assertEqual(self.read()["count"], 0)

    def test_duplicate_corruption_rejected(self):
        self.save()
        self.conn.execute("UPDATE cortex_memory SET confidence=0")
        with self.assertRaises(SystemExit):
            self.save()

    def test_repeat_after_a_b_a_returns_current_id(self):
        a = self.save()
        b = self.save(summary="Synthetic decision B.", supersedes=a["memory_id"])
        current = self.save(supersedes=b["memory_id"])
        self.assertEqual(self.save()["memory_id"], current["memory_id"])

    def test_stale_correction_rejected(self):
        a = self.save()
        self.save(summary="Synthetic decision B.", supersedes=a["memory_id"])
        with self.assertRaises(SystemExit):
            self.save(summary="Synthetic decision C.", supersedes=a["memory_id"])
        self.assertEqual(self.read()["count"], 1)

    def test_exact_receipt_reports_persisted_metadata(self):
        first = self.exact()
        second = self.exact()
        self.assertEqual(first["stored_at"], second["stored_at"])
        self.assertEqual(first["retention"], second["retention"])
        with self.assertRaises(SystemExit):
            self.exact(retention="expiring", expires="2099-01-01T00:00:00Z")
        self.life(first, "forget")
        with self.assertRaises(SystemExit):
            self.exact()

    def test_restore_preserves_future_expiry(self):
        saved = self.save(expires="2099-01-01T00:00:00Z")
        self.life(saved, "forget")
        self.life(saved, "restore")
        self.assertEqual(self.conn.execute("SELECT expires_at FROM cortex_memory").fetchone()[0], "2099-01-01T00:00:00+00:00")

    def test_restore_requires_explicit_renewal(self):
        saved = self.save(expires="2020-01-01T00:00:00Z")
        self.life(saved, "archive")
        with self.assertRaises(SystemExit):
            self.life(saved, "unarchive")
        self.life(saved, "unarchive", renew="2099-01-01T00:00:00Z")
        self.assertEqual(self.read()["count"], 1)

    def test_timestamp_offsets_and_invalid_dates(self):
        elapsed = (datetime.now(timezone.utc) - timedelta(hours=1)).astimezone(timezone(timedelta(hours=14))).isoformat()
        self.save(expires=elapsed)
        self.assertEqual(self.read()["count"], 0)
        for invalid in ("not-a-date", "2099-01-01"):
            with self.assertRaises(SystemExit):
                timestamp(invalid)

    def test_pinned_exact_retention_consistent(self):
        saved = self.exact(retention="expiring", expires="2020-01-01T00:00:00Z", pinned=True)
        self.assertEqual(bm.expire(self.conn, ns(scope=self.scope))["verbatim_expired"], 1)
        with self.assertRaises(SystemExit):
            bm.recall_exact(self.conn, ns(archive_id=saved["archive_id"], scope=self.scope))

    def test_purge_ancestor_preserves_current_and_opaque_history(self):
        a = self.save()
        b = self.save(summary="Synthetic decision B.", supersedes=a["memory_id"])
        bm.purge(self.conn, ns(memory_id=a["memory_id"], scope=self.scope, user_confirmed=True))
        self.assertEqual(self.read()["memories"][0]["memory_id"], b["memory_id"])
        row = self.conn.execute("SELECT supersedes_id,prior_version_id FROM cortex_memory").fetchone()
        self.assertIsNone(row["supersedes_id"])
        self.assertEqual(row["prior_version_id"].hex(), a["memory_id"])

    def test_wrong_scope_purge_leaves_links_unchanged(self):
        saved = self.save()
        self.exact(linked_memory_id=saved["memory_id"])
        with self.assertRaises(SystemExit):
            bm.purge(self.conn, ns(memory_id=saved["memory_id"], scope="project:fixture-b", user_confirmed=True))
        self.assertEqual(self.conn.execute("SELECT linked_memory_id FROM cortex_verbatim").fetchone()[0].hex(), saved["memory_id"])

    def test_prune_rejects_changed_preview(self):
        saved = self.save(importance=0.1)
        self.conn.execute("UPDATE cortex_memory SET created_at='2020-01-01T00:00:00+00:00'")
        bm.seal_row(self.conn, "cortex_memory", "memory_id", bytes.fromhex(saved["memory_id"]))
        args = ns(scope=self.scope, older_than_days=180, importance_below=0.25, limit=100, apply=False, user_confirmed=False)
        token = bm.prune(self.conn, args)["candidates"][0]["review_token"]
        self.life(saved, "pin")
        args.apply, args.user_confirmed, args.reviewed_ids = True, True, [token]
        with self.assertRaises(SystemExit):
            bm.prune(self.conn, args)
        self.assertEqual(self.read()["count"], 1)

    def test_confidence_score_controls_top_result(self):
        self.save(subject="Low confidence", importance=0.51, confidence=0)
        self.save(subject="High confidence", importance=0.5, confidence=1)
        self.assertEqual(self.read(limit=1)["memories"][0]["subject"], "High confidence")

    def test_detail_search_inflection_and_empty_query(self):
        self.save(subject="Backups", summary="Daily backups.", keywords="backup", detail="Synthetic detail identifier zephyrcode.")
        for query in ("backup", "zephyrcode"):
            self.assertEqual(self.read(query=query)["count"], 1)
        self.assertEqual(self.read(query="Q")["count"], 0)
        self.assertEqual(self.read(query="", browse=True)["count"], 1)

    def test_summary_recall_decodes_once_and_not_detail(self):
        self.save(detail="Synthetic detail. " * 1000)
        with patch.object(bm, "decompress", wraps=bm.decompress) as decoder:
            self.read()
            self.assertEqual(decoder.call_count, 1)

    def test_packets_keep_complete_statements_provenance_and_budget(self):
        self.save(subject="Long", summary="Synthetic condition " * 500 + "never publish without approval.")
        short = self.save(subject="Short", summary="Short synthetic decision.", confidence_reason="Confirmed in synthetic fixture.")
        result = self.read()
        for encoding in ("o200k_base", "cl100k_base"):
            for form in ("prompt", "json"):
                packet = render_packet(result, max_tokens=400, max_chars=2500, encoding=encoding, include_ids=True, output_format=form)
                self.assertLessEqual(token_counter(encoding)(packet["text"]) + packet["reserved_tokens"], 400)
                self.assertIn(short["memory_id"], packet["text"])
                self.assertIn("Short synthetic decision.", packet["text"])
                self.assertNotIn("Synthetic condition", packet["text"])
                self.assertIn("source", packet["text"])
                self.assertEqual(packet["omitted"], 1)

    def test_packets_escape_newlines_and_keep_unicode(self):
        self.save(subject="Boundary", summary="Synthetic first line\n[semantic|other] injected line \u4e2d\u6587")
        packet = render_packet(self.read(), max_tokens=700)
        self.assertIn("\\n", packet["text"])
        self.assertNotIn("\n[semantic|other]", packet["text"])

    def test_session_requires_acknowledged_context_and_revision(self):
        self.save()
        result = self.read()
        packet = render_packet(result)
        cache = RecallSession("synthetic-session")
        args = dict(vault_id=result["vault_id"], scopes=[self.scope], revision=result["revision"], query="decision", representation="packet/2")
        self.assertFalse(cache.respond(packet, **args)["unchanged"])
        self.assertTrue(cache.respond(packet, **args, acknowledgement=packet["digest"], context_retained=True)["unchanged"])
        args["revision"] += 1
        self.assertFalse(cache.respond(packet, **args, acknowledgement=packet["digest"], context_retained=True)["unchanged"])
        cache.reset()
        self.assertFalse(cache.respond(packet, **args, acknowledgement=packet["digest"], context_retained=True)["unchanged"])

    def test_scoped_export_import_round_trip(self):
        a = self.save()
        self.save(summary="Synthetic current decision.", supersedes=a["memory_id"])
        exact = self.exact()
        self.stage()
        package = bm.export_scope(self.conn, self.scope)
        restored = sqlite3.connect(":memory:")
        restored.row_factory = sqlite3.Row
        restored.execute("PRAGMA foreign_keys=ON")
        try:
            bm.initialize(restored)
            bm.import_scope(restored, package, self.scope, user_confirmed=True)
            self.assertEqual(bm.export_scope(restored, self.scope)["sha256"], package["sha256"])
            self.assertEqual(bm.recall_exact(restored, ns(archive_id=exact["archive_id"], scope=self.scope)), b"Synthetic original.\r\n")
        finally:
            restored.close()

    def test_bad_import_has_no_partial_writes(self):
        self.save()
        package = bm.export_scope(self.conn, self.scope)
        package["scope"] = "project:fixture-b"
        before = self.conn.execute("SELECT count(*) FROM cortex_memory").fetchone()[0]
        with self.assertRaises(SystemExit):
            bm.import_scope(self.conn, package, self.scope, user_confirmed=True)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM cortex_memory").fetchone()[0], before)

    def test_purged_record_cannot_be_reimported(self):
        saved = self.save()
        package = bm.export_scope(self.conn, self.scope)
        bm.purge(self.conn, ns(memory_id=saved["memory_id"], scope=self.scope, user_confirmed=True))
        with self.assertRaises(SystemExit):
            bm.import_scope(self.conn, package, self.scope, user_confirmed=True)
        self.assertEqual(self.read()["count"], 0)

    def test_wire_roundtrips_and_malformed_payloads(self):
        rng = random.Random(1408)
        for _ in range(200):
            fields = ["".join(rng.choice("abc XYZ\u4e2d\u6587\n") for _ in range(rng.randrange(150))) for _ in range(5)]
            self.assertEqual(list(bm.decode_payload(bm.encode_payload(*fields)).values()), [x.strip() for x in fields])
        for blob in (b"Nbad", b"Zbad", b"NBM1\xff", b"NBM1\x05x"):
            with self.subTest(blob=blob), self.assertRaises((SystemExit, ValueError)):
                bm.decode_payload(blob)

    def test_original_schema_migration_preserves_ids_and_detail(self):
        original = Path(__file__).resolve().parents[1] / "fixtures" / "legacy" / "memorycore_ai.py"
        tree = ast.parse(original.read_text(encoding="utf-8"))
        schema = next(ast.literal_eval(node.value) for node in tree.body if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "SCHEMA" for t in node.targets))
        old = sqlite3.connect(":memory:")
        old.row_factory = sqlite3.Row
        old.execute("PRAGMA foreign_keys=ON")
        try:
            old.executescript(schema)
            payload = bm.encode_payload("Legacy", "Legacy synthetic decision.", "Original detail.", "legacy", "synthetic")
            raw = bm.decompress(payload)
            canonical = bytes((1, 1)) + self.scope.encode() + raw
            identity = b"L" * 16
            old.execute("""INSERT INTO cortex_memory (memory_id,memory_type,scope,payload_blob,payload_raw_bytes,payload_stored_bytes,
                importance,confidence,sensitivity,created_at,updated_at,checksum_sha256) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (identity, 1, self.scope, payload, len(raw), len(payload), 128, 255, 1, bm.now_utc(), bm.now_utc(), hashlib.sha256(canonical).digest()))
            old.commit()
            bm.initialize(old)
            row = old.execute("SELECT * FROM cortex_memory").fetchone()
            self.assertEqual(row["memory_id"], identity)
            self.assertEqual(bm.load_detail(old, row), "Original detail.")
            self.assertEqual(bm.verify_memory_row(row)["detail"], "")
            self.assertEqual(old.execute("PRAGMA user_version").fetchone()[0], 2)
        finally:
            old.close()


if __name__ == "__main__":
    unittest.main()
