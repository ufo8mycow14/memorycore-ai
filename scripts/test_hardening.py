"""Independent-review regressions and isolated security-lab acceptance tests."""

import ast
import base64
import copy
import hashlib
import io
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import memorycore_ai as bm
from scripts import secure_vault_lab as lab
from scripts.memory_packets import token_counter
from scripts.memory_policy import check_exact
from scripts.test_recommendations import ns
from scripts.test_memorycore_ai_regressions import memory_args

SCOPE = "project:fixture-a"


def database():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    bm.initialize(conn)
    return conn


def alter_package(package, table, updates):
    result = copy.deepcopy(package)
    row = {k: base64.b64decode(v["base64"]) if isinstance(v, dict) else v for k, v in result["tables"][table][0].items()}
    row.update(updates)
    row["record_checksum"] = bm.record_digest(row)
    result["tables"][table][0] = bm.encode_row(row)
    body = {k: v for k, v in result.items() if k != "sha256"}
    result["sha256"] = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return result


class HardeningTests(unittest.TestCase):
    def setUp(self):
        self.conn = database()

    def tearDown(self):
        self.conn.close()

    def test_duplicate_explicit_metadata_rejected(self):
        bm.remember(self.conn, memory_args())
        for updates in ({"observed_at": "2099-01-01T00:00:00Z"}, {"valid_from": "2099-01-01T00:00:00Z"},
                        {"valid_to": "2099-01-01T00:00:00Z"}, {"source_hash": "a" * 64},
                        {"confidence_reason": "Different evidence"}, {"claim_id": "a" * 32}):
            with self.subTest(updates=updates), self.assertRaises(SystemExit):
                bm.remember(self.conn, memory_args(**updates))

    def test_sensitive_escaped_container_keys_rejected(self):
        for value in ('["synthetic"]', '{"nested":"synthetic"}', 'null', 'false'):
            with self.subTest(value=value), self.assertRaises(SystemExit):
                check_exact(('{"pass\\u0077ord":' + value + '}').encode(), "application/json")
        with self.assertRaises(SystemExit):
            check_exact(b'{"x":{"pass\\u0077ord":"synthetic"},"x":"safe"}', "application/json")

    def test_checksummed_invalid_import_preflight_never_writes_target(self):
        bm.remember(self.conn, memory_args())
        bm.stage(self.conn, ns(scope=SCOPE, text="Synthetic staged material", file=None, source="synthetic", expires=None))
        package = bm.export_scope(self.conn, SCOPE)
        before = self.conn.serialize()
        cases = [("cortex_memory", {"valid_to": "not-a-timestamp"}),
                 ("cortex_memory", {"valid_from": "2099-01-01T00:00:00"}),
                 ("cortex_memory", {"source_hash": "not-a-hash"}),
                 ("cortex_memory", {"confidence_reason": "password = SYNTHETIC_ONLY"}),
                 ("cortex_memory", {"claim_id": None}),
                 ("cortex_memory", {"status": 9}),
                 ("cortex_memory", {"confidence": 128.5}),
                 ("cortex_memory", {"payload_raw_bytes": 1}),
                 ("hippocampus_stage", {"status": 1})]
        for table, updates in cases:
            with self.subTest(updates=updates), patch.object(bm, "_import_scope", wraps=bm._import_scope) as importing:
                with self.assertRaises(SystemExit):
                    bm.import_scope(self.conn, alter_package(package, table, updates), SCOPE, user_confirmed=True)
                self.assertEqual(importing.call_count, 1)
                self.assertIsNot(importing.call_args.args[0], self.conn)
                self.assertEqual(self.conn.serialize(), before)

    def test_cli_preflight_precedes_target_creation(self):
        args = ["memorycore_ai.py", "--db", "synthetic-never-created.sqlite3", "import", "--scope", SCOPE,
                "--file", "synthetic-invalid.json", "--user-confirmed"]
        with patch("sys.argv", args), patch.object(Path, "open", return_value=io.BytesIO(b"{}")), patch.object(bm, "connect") as connect:
            with self.assertRaises(SystemExit):
                bm.main()
            connect.assert_not_called()

    def legacy(self, *, broken=None):
        original = Path(__file__).resolve().parents[1] / "fixtures" / "legacy" / "memorycore_ai.py"
        tree = ast.parse(original.read_text(encoding="utf-8"))
        schema = next(ast.literal_eval(n.value) for n in tree.body if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "SCHEMA" for t in n.targets))
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(schema)
        for i in (1, 2):
            scope = "project:fixture-b" if broken == "scope" and i == 2 else SCOPE
            payload = bm.encode_payload("Legacy", f"Synthetic decision {i}", "Original detail", "legacy", "synthetic")
            raw = bm.decompress(payload)
            predecessor = bytes([1]) * 16 if i == 2 else None
            if broken == "missing" and i == 2:
                predecessor = b"X" * 16
            if broken == "cycle" and i == 1:
                predecessor = bytes([2]) * 16
            conn.execute("""INSERT INTO cortex_memory(memory_id,memory_type,scope,payload_blob,payload_raw_bytes,payload_stored_bytes,
                importance,confidence,sensitivity,created_at,updated_at,checksum_sha256,status,supersedes_id)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (bytes([i]) * 16, 1, scope, payload, len(raw), len(payload), 128, 255, 1,
                bm.now_utc(), bm.now_utc(), hashlib.sha256(bytes((1, 1)) + scope.encode() + raw).digest(),
                0 if i == 2 or broken == "active" else 1, predecessor))
        conn.commit()
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def test_migrated_correction_claims_remain_one_chain(self):
        old = self.legacy()
        try:
            bm.initialize(old)
            self.assertEqual(old.execute("SELECT count(DISTINCT claim_id) FROM cortex_memory").fetchone()[0], 1)
            args = ns(scope=SCOPE, memory_id=(bytes([1]) * 16).hex(), action="forget")
            bm.lifecycle(old, args)
            args.action = "restore"
            with self.assertRaises(SystemExit):
                bm.lifecycle(old, args)
            self.assertEqual(old.execute("SELECT count(*) FROM cortex_memory WHERE status=0").fetchone()[0], 1)
        finally:
            old.close()

    def test_failed_migrations_roll_back_schema_and_records(self):
        for broken in ("missing", "scope", "cycle", "active"):
            old = self.legacy(broken=broken)
            try:
                before = old.serialize()
                with self.subTest(broken=broken), self.assertRaises(SystemExit):
                    bm.initialize(old)
                self.assertEqual(old.serialize(), before)
                self.assertEqual(old.execute("PRAGMA user_version").fetchone()[0], 0)
            finally:
                old.close()

    def test_old_schema_read_refuses_implicit_migration(self):
        old = self.legacy()
        before = old.serialize()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "legacy.sqlite3"
            path.write_bytes(before)
            with patch("sys.argv", ["memorycore_ai.py", "--db", str(path), "list", "--scope", SCOPE]), self.assertRaises(SystemExit):
                bm.main()
            self.assertEqual(path.read_bytes(), before)
        old.close()

    def test_bounded_decompression_rejects_bombs_and_trailing_stream(self):
        with patch.object(bm, "MAX_DECOMPRESSED", 32):
            for blob in (bm.compress(b"a" * 33), b"N" + b"a" * 33, bm.compress(b"a" * 30) + b"trailing"):
                with self.assertRaises(SystemExit):
                    bm.decompress(blob)

    def test_exact_purge_tombstone_prevents_id_recreation(self):
        args = ns(scope=SCOPE, text="Synthetic exact content", file=None, media_type="text/plain", source="synthetic",
                  retention="until_user_deletes", expires=None, user_confirmed=True, pinned=False)
        saved = bm.store_exact(self.conn, args)
        bm.purge(self.conn, ns(scope=SCOPE, memory_id=saved["archive_id"], user_confirmed=True))
        with self.assertRaises(SystemExit):
            bm.store_exact(self.conn, args)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM cortex_verbatim").fetchone()[0], 0)

    def test_json_nested_duplicate_values_are_inspected(self):
        data = b'{"x":["api\\u005fkey=SYNTHETIC_ONLY"],"x":"safe"}'
        with self.assertRaises(SystemExit):
            check_exact(data, "application/json")

    def test_concurrent_corrections_leave_one_current_version(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "concurrent.sqlite3"
            conn = bm.connect(str(path))
            bm.initialize(conn)
            first = bm.remember(conn, memory_args())
            conn.close()
            barrier, results = threading.Barrier(2), []
            def correct(label):
                connection = bm.connect(str(path))
                try:
                    barrier.wait(timeout=10)
                    bm.remember(connection, memory_args(summary=f"Synthetic decision {label}", supersedes=first["memory_id"]))
                    results.append("accepted")
                except SystemExit:
                    results.append("rejected")
                finally:
                    connection.close()
            workers = [threading.Thread(target=correct, args=(i,)) for i in range(2)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(timeout=15)
                self.assertFalse(worker.is_alive())
            self.assertCountEqual(results, ["accepted", "rejected"])
            conn = bm.connect(str(path), read_only=True)
            self.assertEqual(conn.execute("SELECT count(*) FROM cortex_memory WHERE status=0").fetchone()[0], 1)
            conn.close()


class SecurityLabTests(unittest.TestCase):
    def setUp(self):
        self.key = os.urandom(32)
        self.vault = lab.EncryptedVault("a" * 32, self.key)
        self.broker = lab.LocalBroker(self.vault, {"synthetic-owner": {"scopes": [SCOPE], "operations": lab.LocalBroker.OPERATIONS}})
        self.token = self.grant()

    def tearDown(self):
        self.vault.close()

    def grant(self, **overrides):
        values = dict(client="codex", scopes=[SCOPE], operations=lab.LocalBroker.OPERATIONS, purpose="synthetic validation", max_tokens=700)
        values.update(overrides)
        return self.broker.issue("synthetic-owner", **values)

    def read_args(self, **overrides):
        values = dict(scope=SCOPE, query="decision", type=None, limit=8, include_detail=False)
        values.update(overrides)
        return ns(**values)

    def test_snapshot_authentication_identity_and_rollback(self):
        initial = self.vault.snapshot()
        self.broker.call(self.token, "remember", memory_args())
        current = self.vault.snapshot()
        revision = self.vault.conn.execute("SELECT revision FROM vault_state").fetchone()[0]
        restored = lab.EncryptedVault("a" * 32, self.key, snapshot=current, minimum_revision=revision)
        self.assertEqual(bm.recall(restored.conn, self.read_args())["count"], 1)
        restored.close()
        for key, identity, data, minimum in ((os.urandom(32), "a" * 32, current, 0), (self.key, "b" * 32, current, 0),
                (self.key, "a" * 32, current[:-1] + bytes([current[-1] ^ 1]), 0),
                (self.key, "a" * 32, current[:9] + bytes([current[9] ^ 1]) + current[10:], 0),
                (self.key, "a" * 32, initial, revision)):
            with self.assertRaises(ValueError):
                lab.EncryptedVault(identity, key, snapshot=data, minimum_revision=minimum)

    def test_backup_no_plaintext_exclusive_create_and_rotation(self):
        self.broker.call(self.token, "remember", memory_args(summary="SYNTHETIC_PLAINTEXT_MARKER decision"))
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "synthetic.bmlab"
            receipt = self.vault.save_snapshot(path)
            snapshot = path.read_bytes()
            self.assertNotIn(b"SYNTHETIC_PLAINTEXT_MARKER", snapshot)
            self.assertNotIn(b"SQLite format", snapshot)
            self.assertEqual(hashlib.sha256(snapshot).hexdigest(), receipt["sha256"])
            with self.assertRaises(FileExistsError):
                self.vault.save_snapshot(path)
            restored = lab.EncryptedVault("a" * 32, self.key, snapshot=snapshot)
            restored.close()
            new_key = os.urandom(32)
            rotated = self.vault.rotate_key(new_key)
            with self.assertRaises(ValueError):
                lab.EncryptedVault("a" * 32, self.key, snapshot=rotated)
            restored = lab.EncryptedVault("a" * 32, new_key, snapshot=rotated)
            restored.close()
            with patch.object(self.vault, "snapshot", side_effect=ValueError("synthetic failure")), self.assertRaises(ValueError):
                self.vault.rotate_key(os.urandom(32))
            self.assertEqual(self.vault._key, new_key)

    def test_passphrase_slots_and_wrong_passphrase(self):
        phrase = b"synthetic validation phrase only"
        slot = lab.wrap_key(self.key, phrase)
        self.assertEqual(lab.unwrap_key(slot, phrase), self.key)
        with self.assertRaises(ValueError):
            lab.unwrap_key(slot, b"different synthetic phrase only")
        for key, password in ((b"short", phrase), (self.key, "not bytes"), (self.key, b"short")):
            with self.assertRaises(ValueError):
                lab.wrap_key(key, password)

    @unittest.skipUnless(os.name == "nt", "DPAPI is Windows-only")
    def test_windows_current_user_slot_round_trip(self):
        slot = lab.protect_for_current_windows_user(self.key)
        self.assertNotIn(self.key, slot)
        self.assertEqual(lab.protect_for_current_windows_user(slot, unprotect=True), self.key)
        with self.assertRaises(OSError):
            lab.protect_for_current_windows_user(slot[:-1], unprotect=True)

    def test_capability_scope_operation_expiry_and_revocation(self):
        read_only = self.grant(operations=["recall"])
        for token, operation, args in ((read_only, "remember", memory_args()),
                (self.token, "recall", self.read_args(scope="project:fixture-b")),
                (b"forged", "recall", self.read_args())):
            with self.assertRaises(PermissionError):
                self.broker.call(token, operation, args)
        with self.assertRaises(PermissionError):
            self.grant(scopes=["project:fixture-b"])
        with patch.object(lab.time, "monotonic", return_value=10 ** 20), self.assertRaises(PermissionError):
            self.broker.call(self.token, "recall", self.read_args())
        self.broker.revoke_all()
        with self.assertRaises(PermissionError):
            self.broker.call(self.token, "recall", self.read_args())

    def test_lifecycle_action_and_filesystem_inputs_rejected(self):
        first = self.broker.call(self.token, "remember", memory_args())
        with self.assertRaises(PermissionError):
            self.broker.call(self.token, "forget", ns(scope=SCOPE, memory_id=first["memory_id"], action="purge"))
        for op in ("stage", "store-exact"):
            for file, text in (("synthetic-path", "synthetic text"), (None, None)):
                with self.assertRaises(PermissionError):
                    self.broker.call(self.token, op, ns(scope=SCOPE, file=file, text=text))

    def test_purge_requires_exact_host_approval_and_rejects_replay(self):
        first = self.broker.call(self.token, "remember", memory_args())
        second = self.broker.call(self.token, "remember", memory_args(subject="Second"))
        args = ns(scope=SCOPE, memory_id=first["memory_id"], user_confirmed=True)
        with self.assertRaises(PermissionError):
            self.broker.call(self.token, "purge", args)
        wrong = self.broker.approve(self.token, "purge", SCOPE, [second["memory_id"]])
        with self.assertRaises(PermissionError):
            self.broker.call(self.token, "purge", args, approval=wrong)
        approval = self.broker.approve(self.token, "purge", SCOPE, [first["memory_id"]])
        self.assertEqual(self.broker.call(self.token, "purge", args, approval=approval)["status"], "purged")
        with self.assertRaises(PermissionError):
            self.broker.call(self.token, "purge", args, approval=approval)

    def test_full_response_budgets_and_mutation_rollback(self):
        for i in range(10):
            self.broker.call(self.token, "remember", memory_args(subject=f"Synthetic {i}", summary="Synthetic decision with a complete qualifying condition. " * 8))
        for encoding in ("o200k_base", "cl100k_base"):
            token = self.grant(max_tokens=350)
            packet = self.broker.call(token, "recall", self.read_args(encoding=encoding))
            self.assertLessEqual(token_counter(encoding)(json.dumps(packet, ensure_ascii=False, separators=(",", ":"))), 350)
            self.assertGreater(packet["omitted"], 0)
        before = self.vault.conn.serialize()
        token = self.grant(max_tokens=64)
        args = ns(scope=SCOPE, text="Synthetic original", file=None, source="synthetic", user_confirmed=True,
                  media_type="text/plain", retention="until_user_deletes", expires=None, pinned=False)
        with self.assertRaises(ValueError):
            self.broker.call(token, "store-exact", args)
        self.assertEqual(self.vault.conn.serialize(), before)

    def test_session_cache_and_audit_tampering(self):
        self.broker.call(self.token, "remember", memory_args())
        first = self.broker.call(self.token, "recall", self.read_args(), session_id="fixture")
        second = self.broker.call(self.token, "recall", self.read_args(), session_id="fixture", acknowledgement=first["digest"], context_retained=True)
        self.assertTrue(second["unchanged"])
        self.broker.call(self.token, "remember", memory_args(subject="Second"))
        changed = self.broker.call(self.token, "recall", self.read_args(), session_id="fixture", acknowledgement=first["digest"], context_retained=True)
        self.assertFalse(changed["unchanged"])
        tip, length = self.broker._audit[-1]["mac"], len(self.broker._audit)
        self.assertTrue(self.broker.verify_audit(expected_tip=tip, expected_length=length))
        self.broker._audit.pop()
        self.assertFalse(self.broker.verify_audit(expected_tip=tip, expected_length=length))
        self.broker._audit[0]["outcome"] = "tampered"
        self.assertFalse(self.broker.verify_audit())
        self.assertNotIn("summary", json.dumps(self.broker._audit))

    def test_mutable_request_cannot_change_approved_target(self):
        first = self.broker.call(self.token, "remember", memory_args())
        second = self.broker.call(self.token, "remember", memory_args(subject="Second"))
        args = ns(scope=SCOPE, memory_id=first["memory_id"], user_confirmed=True)
        approval = self.broker.approve(self.token, "purge", SCOPE, [first["memory_id"]])
        original = bm.purge
        def mutate_then_purge(conn, owned_args):
            args.memory_id = second["memory_id"]
            return original(conn, owned_args)
        with patch.object(bm, "purge", side_effect=mutate_then_purge):
            receipt = self.broker.call(self.token, "purge", args, approval=approval)
        self.assertEqual(receipt["memory_id"], first["memory_id"])
        self.assertEqual(self.vault.conn.execute("SELECT memory_id FROM cortex_memory").fetchone()[0].hex(), second["memory_id"])

    def test_audit_failure_rolls_back_mutation(self):
        before = self.vault.conn.serialize()
        with patch.object(self.broker, "_event", side_effect=ValueError("synthetic audit failure")), self.assertRaises(ValueError):
            self.broker.call(self.token, "remember", memory_args())
        self.assertEqual(self.vault.conn.serialize(), before)
        self.assertEqual(self.broker._audit, [])

    def test_commit_failure_retracts_accepted_audit(self):
        original = bm.transaction
        from contextlib import contextmanager
        @contextmanager
        def reject_commit(conn, **kwargs):
            with original(conn, **kwargs):
                yield
                if conn.in_transaction and len(self.broker._audit):
                    raise ValueError("synthetic commit failure")
        before = self.vault.conn.serialize()
        with patch.object(bm, "transaction", reject_commit), self.assertRaises(ValueError):
            self.broker.call(self.token, "remember", memory_args())
        self.assertEqual(self.vault.conn.serialize(), before)
        self.assertEqual([e["outcome"] for e in self.broker._audit], ["rejected"])

    def test_ambient_transaction_is_rejected_without_committing_it(self):
        self.vault.conn.execute("BEGIN")
        with self.assertRaises(ValueError):
            self.broker.call(self.token, "remember", memory_args())
        self.assertTrue(self.vault.conn.in_transaction)
        self.vault.conn.rollback()
        self.assertEqual(self.vault.conn.execute("SELECT count(*) FROM cortex_memory").fetchone()[0], 0)
        self.assertNotIn("accepted", [e["outcome"] for e in self.broker._audit])

    def test_malformed_audit_fails_closed(self):
        self.broker.call(self.token, "remember", memory_args())
        valid = copy.deepcopy(self.broker._audit)
        for field in valid[0]:
            self.broker._audit = copy.deepcopy(valid)
            del self.broker._audit[0][field]
            self.assertFalse(self.broker.verify_audit())
        for field, value in (("mac", 123), ("sequence", "one"), ("previous", None), ("operation", []), ("outcome", {})):
            self.broker._audit = copy.deepcopy(valid)
            self.broker._audit[0][field] = value
            self.assertFalse(self.broker.verify_audit())

    def test_prune_approval_matches_review_state(self):
        saved = self.broker.call(self.token, "remember", memory_args(importance=0.1))
        self.vault.conn.execute("UPDATE cortex_memory SET created_at='2020-01-01T00:00:00+00:00'")
        bm.seal_row(self.vault.conn, "cortex_memory", "memory_id", bytes.fromhex(saved["memory_id"]))
        self.vault.conn.commit()
        args = ns(scope=SCOPE, older_than_days=180, importance_below=0.25, limit=10, apply=False, user_confirmed=False)
        preview = self.broker.call(self.token, "prune", args)
        reviewed = preview["candidates"][0]["review_token"]
        args.apply, args.user_confirmed, args.reviewed_ids = True, True, [reviewed]
        with self.assertRaises(PermissionError):
            self.broker.call(self.token, "prune", args)
        approval = self.broker.approve(self.token, "prune", SCOPE, [reviewed])
        self.broker.call(self.token, "prune", args, approval=approval)
        self.assertEqual(self.vault.conn.execute("SELECT status FROM cortex_memory").fetchone()[0], 4)

    def test_expired_approval_rejected(self):
        saved = self.broker.call(self.token, "remember", memory_args())
        approval = self.broker.approve(self.token, "purge", SCOPE, [saved["memory_id"]])
        expiry = self.broker._approvals[approval][-1]
        with patch.object(lab.time, "monotonic", return_value=expiry + 1), self.assertRaises(PermissionError):
            self.broker.call(self.token, "purge", ns(scope=SCOPE, memory_id=saved["memory_id"], user_confirmed=True), approval=approval)


if __name__ == "__main__":
    unittest.main()
