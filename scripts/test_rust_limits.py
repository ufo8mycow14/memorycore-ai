"""Synthetic regression tests for bounded recovery and growth handling."""
import copy
import json
import subprocess
import time
import unittest
import hashlib
import os
import signal
from pathlib import Path

from scripts.test_rust_broker import Harness, BINARY
from scripts import memorycore_ai as bm
from scripts.knowledge_layer import Knowledge, SourceRoot, source_binding


@unittest.skipUnless(BINARY.is_file(), "build Rust release binary")
class NativeRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness(0)
        self.addCleanup(self.h.close)
        self.h.config.update(backend="native")
        self.h.config["sessions"][0]["allow_admin"] = True
        self.h.file.write_text(json.dumps(self.h.config), encoding="utf-8")

    def run_request(self, request, success=True):
        p = subprocess.run([str(BINARY), "--native-command", "--config", str(self.h.file)],
                           input=json.dumps(request).encode(), capture_output=True, timeout=30)
        if success:
            self.assertEqual(p.returncode, 0, p.stderr)
            return json.loads(p.stdout)
        self.assertNotEqual(p.returncode, 0)

    def assert_replayed_response(self, first, replay):
        payloads = []
        for response in (first, replay):
            payload = dict(response)
            timing = payload.pop("native_timing")
            self.assertEqual(set(timing), {"prepare_ms", "begin_ms", "execute_ms", "commit_ms", "snapshot_ms"})
            for value in timing.values():
                self.assertIsInstance(value, (int, float))
                self.assertGreaterEqual(value, 0)
                self.assertLess(value, float("inf"))
            payloads.append(payload)
        self.assertEqual(*payloads)
        stored = self.h.conn.execute("SELECT response FROM native_retry_receipt").fetchone()[0]
        self.assertEqual(json.loads(stored), payloads[0])

    def test_committed_response_survives_process_restart_without_rewrite(self):
        request = {"session":"chat-0", "id":"durable-write", "operation":"admin",
                   "arguments":{"action":"stage", "arguments":{"text":"Synthetic retry example"}},
                   "recovery":{"key":"one-write", "expires_at":int(time.time())+3600}}
        first = self.run_request(request)
        self.assert_replayed_response(first, self.run_request(request))
        self.assertEqual(self.h.conn.execute("SELECT count(*) FROM hippocampus_stage").fetchone()[0], 1)
        changed = copy.deepcopy(request)
        changed["arguments"]["arguments"]["text"] = "Conflicting synthetic data"
        self.run_request(changed, success=False)
        expired = copy.deepcopy(request)
        expired["recovery"]["expires_at"] = int(time.time())-1
        self.run_request(expired, success=False)
        far = copy.deepcopy(request)
        far["recovery"]["expires_at"] = int(time.time())+90000
        self.run_request(far, success=False)
        self.assertEqual(self.h.conn.execute("SELECT count(*) FROM hippocampus_stage").fetchone()[0], 1)

    def test_read_recovery_rejected_and_expired_receipts_cleaned(self):
        request = {"session":"chat-0", "id":"read", "operation":"ping", "arguments":{},
                   "recovery":{"key":"write", "expires_at":int(time.time())+3600}}
        self.run_request(request, success=False)
        request.update(operation="admin", arguments={"action":"stage", "arguments":{"text":"Synthetic cleanup"}})
        self.run_request(request)
        self.h.conn.execute("UPDATE native_retry_receipt SET expires_at=0")
        self.h.conn.commit()
        request["recovery"]["key"] = "new-write"
        self.run_request(request)
        self.assertEqual(self.h.conn.execute("SELECT count(*) FROM native_retry_receipt").fetchone()[0], 1)

    def test_atomic_batch_retry_does_not_repeat_any_child(self):
        request={"session":"chat-0","id":"batch","operation":"admin",
                 "arguments":{"action":"batch","arguments":{"items":[
                     {"action":"stage","arguments":{"text":f"Synthetic batch item {i}"}} for i in range(4)]}},
                 "recovery":{"key":"batch-retry","expires_at":int(time.time())+3600}}
        first=self.run_request(request)
        self.assert_replayed_response(first, self.run_request(request))
        self.assertEqual(self.h.conn.execute("SELECT count(*) FROM hippocampus_stage").fetchone()[0],4)

    def test_dead_readers_are_retired_and_writer_survives(self):
        ready=self.h.start()
        for pid in ready["worker_pids"][:self.h.config["read_workers"]]:
            os.kill(pid,signal.SIGTERM)
        failures=[]
        for _ in range(self.h.config["read_workers"]+2):
            self.h.send("chat-0","ping",{})
            failures.append(self.h.receive()["error"])
        self.assertEqual(failures[-2:],["worker_unavailable"]*2)
        self.h.send("chat-0","admin",{"action":"stage","arguments":{"text":"Synthetic surviving writer"}})
        self.assertNotIn("error",self.h.receive())

    def test_purge_revokes_cached_content_without_reexecuting_original_write(self):
        request = {"session":"chat-0", "id":"original", "operation":"admin",
                   "arguments":{"action":"stage", "arguments":{"text":"Synthetic data to purge"}},
                   "recovery":{"key":"purged-write", "expires_at":int(time.time())+3600}}
        saved = self.run_request(request)["result"]
        self.run_request({"session":"chat-0", "id":"purge", "operation":"admin",
                          "arguments":{"action":"purge", "arguments":{"memory_id":saved["stage_id"], "user_confirmed":True}}})
        self.run_request(request, success=False)
        self.assertEqual(self.h.conn.execute("SELECT count(*) FROM hippocampus_stage").fetchone()[0], 0)
        self.assertEqual(self.h.conn.execute("SELECT response FROM native_retry_receipt").fetchone()[0], "")

    def test_broad_query_over_two_thousand_matches_is_bounded_and_disclosed(self):
        session = self.h.config["sessions"][0]
        root = Path(session["source_root"])
        raw = b"Fact: Synthetic common policy preserves approval.\n"
        (root / "policy.md").write_bytes(raw)
        k = Knowledge(self.h.conn, scope=session["scope"], sources=SourceRoot(root), synthetic=True)
        with bm.transaction(self.h.conn):
            for n in range(2100):
                args = bm.build_parser().parse_args(["remember", "--scope", session["scope"], "--type", "semantic",
                    "--subject", f"Synthetic common policy {n}", "--summary", f"Synthetic common policy item {n} preserves approval.",
                    "--importance", "1" if n == 2099 else "0.1", "--source", "policy.md",
                    "--source-hash", hashlib.sha256(raw).hexdigest()])
                saved = bm.remember(self.h.conn, args)
            k._put("source", source_binding("policy.md", raw), owner=saved["memory_id"])
        result = self.run_request({"session":"chat-0", "id":"broad", "operation":"admin",
            "arguments":{"action":"recall", "arguments":{"query":"Synthetic common policy", "max_tokens":1400}}})["result"]
        self.assertTrue(result["candidates_capped"])
        self.assertEqual(result["candidate_limit"], 256)
        self.assertIsNone(result["unexamined_matches"])
        self.assertEqual(result["memories"][0]["id"], saved["memory_id"])


if __name__ == "__main__":
    unittest.main()
