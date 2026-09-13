#!/usr/bin/env python3

import argparse
import hashlib
import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import memorycore_ai as bm


def args(**kwargs):
    return argparse.Namespace(**kwargs)


def memory_args(**overrides):
    defaults = dict(
        type="semantic",
        scope="project:fixture-a",
        subject="Decision",
        summary="Fixture A uses the source-linked decision.",
        detail="",
        keywords="decision fixture",
        importance=0.8,
        confidence=0.9,
        sensitivity="internal",
        source="synthetic",
        expires=None,
        supersedes=None,
        pinned=False,
        user_confirmed=False,
        stage_id=None,
    )
    defaults.update(overrides)
    return args(**defaults)


class MemoryCoreAIRegressionTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        bm.initialize(self.conn)

    def tearDown(self):
        self.conn.close()

    def remember(self, **overrides):
        return bm.remember(self.conn, memory_args(**overrides))

    def recall(self, query="fixture", **overrides):
        defaults = dict(query=query, scope="project:fixture-a", type=None, limit=8, include_detail=False)
        defaults.update(overrides)
        return bm.recall(self.conn, args(**defaults))

    def test_recall_requires_explicit_scope_by_default(self):
        self.remember(scope="project:fixture-a", subject="A", summary="A scoped record.", keywords="shared")
        self.remember(scope="project:fixture-b", subject="B", summary="B scoped record.", keywords="shared")
        with self.assertRaises(SystemExit):
            self.recall("shared", scope=None)

    def test_supersedes_must_match_caller_scope(self):
        original = self.remember(scope="project:fixture-a")
        with self.assertRaises(SystemExit):
            self.remember(
                scope="project:fixture-b",
                subject="Decision",
                summary="Fixture B must not supersede fixture A.",
                supersedes=original["memory_id"],
            )
        recalled = self.recall("decision", scope="project:fixture-a")
        self.assertEqual(recalled["count"], 1)
        self.assertEqual(recalled["memories"][0]["memory_id"], original["memory_id"])

    def test_recall_rejects_corrupted_semantic_payload(self):
        saved = self.remember(subject="Integrity", summary="Original verified semantic payload.", keywords="integrity")
        tampered = bm.encode_payload("Integrity", "Tampered semantic payload.", "", "integrity", "synthetic")
        self.conn.execute(
            "UPDATE cortex_memory SET payload_blob=? WHERE memory_id=?",
            (tampered, bytes.fromhex(saved["memory_id"])),
        )
        with self.assertRaises(SystemExit):
            self.recall("integrity")

    def test_duplicate_exact_receipt_verifies_persisted_record(self):
        payload = b"exact synthetic bytes"
        save_args = args(
            text=None,
            file=None,
            scope="project:fixture-a",
            source="synthetic",
            media_type="text/plain",
            retention="until_user_deletes",
            expires=None,
            pinned=False,
            user_confirmed=True,
        )
        save_args.text = payload.decode("utf-8")
        first = bm.store_exact(self.conn, save_args)
        self.conn.execute(
            "UPDATE cortex_verbatim SET original_blob=? WHERE archive_id=?",
            (bm.compress(b"corrupted persisted bytes"), bytes.fromhex(first["archive_id"])),
        )
        with self.assertRaises(SystemExit):
            bm.store_exact(self.conn, save_args)

    def test_duplicate_consolidation_marks_stage_consolidated(self):
        first = self.remember()
        stage = bm.stage(self.conn, args(text="duplicate stage", file=None, scope="project:fixture-a", source="chat", expires=None))
        duplicate = self.remember(stage_id=stage["stage_id"])
        self.assertTrue(duplicate["deduplicated"])
        stage_row = self.conn.execute("SELECT status FROM hippocampus_stage WHERE stage_id=?", (bytes.fromhex(stage["stage_id"]),)).fetchone()
        self.assertEqual(stage_row["status"], 1)
        self.assertEqual(first["memory_id"], duplicate["memory_id"])

    def test_correction_back_to_prior_value_preserves_new_version_history(self):
        first = self.remember(subject="State", summary="The state is A.", keywords="state")
        second = self.remember(subject="State", summary="The state is B.", keywords="state", supersedes=first["memory_id"])
        third = self.remember(subject="State", summary="The state is A.", keywords="state", supersedes=second["memory_id"])
        self.assertFalse(third["deduplicated"])
        self.assertNotEqual(third["memory_id"], first["memory_id"])
        active = self.recall("state")
        self.assertEqual(active["count"], 1)
        self.assertEqual(active["memories"][0]["memory_id"], third["memory_id"])

    def test_prune_apply_uses_reviewed_candidate_ids(self):
        old = self.remember(subject="Old", summary="Old low value record.", keywords="old", importance=0.1, pinned=False)
        self.conn.execute(
            "UPDATE cortex_memory SET created_at='2020-01-01T00:00:00+00:00' WHERE memory_id=?",
            (bytes.fromhex(old["memory_id"]),),
        )
        bm.seal_row(self.conn, "cortex_memory", "memory_id", bytes.fromhex(old["memory_id"]))
        preview = bm.prune(self.conn, args(scope="project:fixture-a", older_than_days=180, importance_below=0.25, limit=100, apply=False, user_confirmed=False))
        reviewed_ids = [candidate["review_token"] for candidate in preview["candidates"]]
        new = self.remember(subject="New", summary="New low value record.", keywords="new", importance=0.1, pinned=False)
        self.conn.execute(
            "UPDATE cortex_memory SET created_at='2020-01-01T00:00:00+00:00' WHERE memory_id=?",
            (bytes.fromhex(new["memory_id"]),),
        )
        bm.seal_row(self.conn, "cortex_memory", "memory_id", bytes.fromhex(new["memory_id"]))
        applied = bm.prune(
            self.conn,
            args(scope="project:fixture-a", older_than_days=180, importance_below=0.25, limit=100, apply=True, user_confirmed=True, reviewed_ids=reviewed_ids),
        )
        self.assertEqual(applied["count"], 1)
        self.assertEqual(applied["candidates"][0]["memory_id"], old["memory_id"])
        self.assertEqual(self.recall("new", scope="project:fixture-a")["count"], 1)

    def test_restore_expired_memory_clears_elapsed_expiry(self):
        saved = self.remember(subject="Expiry", summary="Expired memory can be restored.", keywords="expiry", expires="2020-01-01T00:00:00+00:00")
        bm.expire(self.conn, args(scope="project:fixture-a"))
        restored = bm.lifecycle(self.conn, args(action="restore", memory_id=saved["memory_id"], scope="project:fixture-a", renew="2099-01-01T00:00:00+00:00"))
        self.assertEqual(restored["status"], "active")
        self.assertEqual(self.recall("expiry")["count"], 1)

    def test_existing_schema_database_remains_usable_without_migration(self):
        saved = self.remember(subject="Compatibility", summary="Existing schema stays readable.", keywords="compatibility")
        bm.initialize(self.conn)
        recalled = self.recall("compatibility")
        self.assertEqual(recalled["count"], 1)
        self.assertEqual(recalled["memories"][0]["memory_id"], saved["memory_id"])


if __name__ == "__main__":
    unittest.main()
