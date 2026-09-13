"""Real Rust-process compatibility, concurrency and isolation checks."""
import json
import os
import queue
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import memorycore_ai as bm
from scripts.knowledge_layer import Knowledge, SourceRoot, canonical
from scripts.rust_worker import Backend, loads
from scripts.session_routing import RouteOutbox

ROOT = Path(__file__).resolve().parents[1]
BINARY = ROOT / "rust-broker" / "target" / "release" / ("memorycore-ai-broker.exe" if os.name == "nt" else "memorycore-ai-broker")


class Harness:
    def __init__(self, records_per_scope=1, *, binary=None, environment=None, timestamp_responses=False):
        self.binary = binary or BINARY
        self.timestamp_responses = timestamp_responses
        self.environment = environment
        self.tmp = tempfile.TemporaryDirectory(prefix="rust-broker-synthetic-")
        self.root = Path(self.tmp.name)
        self.db = self.root / "memory.sqlite3"
        self.config = {"synthetic": True, "allow_plaintext": True, "backend":"python", "python": sys.executable, "backend_root": str(ROOT),
                       "database": str(self.db), "read_workers": 4, "sessions": []}
        self.conn = bm.connect(str(self.db))
        self.receipts = {}
        bm.initialize(self.conn)
        for i in range(10):
            source = self.root / f"source-{i}"
            source.mkdir()
            session = {"id": f"chat-{i}", "scope": f"synthetic:chat-{i}", "source_root": str(source)}
            self.config["sessions"].append(session)
            k = Knowledge(self.conn, scope=session["scope"], sources=SourceRoot(source), synthetic=True, create=True)
            RouteOutbox(k, create=True)
            for n in range(records_per_scope):
                (source / f"item{n}.md").write_text(f"Fact: Service{i} item{n} uses SQLite.\nNever publish without approval.\n", encoding="utf-8")
                proposal = k.propose(f"item{n}.md")["proposals"][0]
                self.receipts[i, n] = k.accept(proposal["id"], proposal["review_digest"])
        self.config["sessions"].append(dict(self.config["sessions"][0], id="disabled", use_memories=False, generate_memories=False))
        self.file = self.root / "host.json"
        self.file.write_text(json.dumps(self.config), encoding="utf-8")
        self.process = None
        self.responses = queue.Queue()
        self.sequence = 0

    def start(self):
        started = time.monotonic()
        self.process = subprocess.Popen([str(self.binary), "--config", str(self.file)], cwd=ROOT, env=self.environment,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        def read():
            try:
                for line in self.process.stdout:
                    response=json.loads(line)
                    if self.timestamp_responses:
                        response['_benchmark_received_at']=time.perf_counter()
                    self.responses.put(response)
            finally:
                self.responses.put(None)
        self.reader = threading.Thread(target=read, daemon=True)
        self.reader.start()
        ready = self.receive()
        if ready is None or ready.get("event") != "ready":
            raise AssertionError("Rust broker did not become ready")
        self.startup_ms = (time.monotonic() - started) * 1000
        return ready

    def send(self, session, operation, arguments):
        self.sequence += 1
        request = {"session": session, "id": str(self.sequence), "operation": operation, "arguments": arguments}
        self.process.stdin.write((json.dumps(request) + "\n").encode())
        self.process.stdin.flush()
        return request["id"]

    def receive(self):
        response = self.responses.get(timeout=30)
        if response is None:
            raise AssertionError("Rust broker exited unexpectedly")
        return response

    def call(self, session, action, value):
        self.send(session, "call", {"name": "memory", "arguments": {action: value}})
        return self.receive()

    def close(self):
        if self.conn.in_transaction:
            self.conn.rollback()
        if self.process:
            self.process.stdin.close()
            try:
                code = self.process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
                raise
            finally:
                self.process.stdout.close()
                self.reader.join(timeout=5)
            if code != 0:
                raise AssertionError("Rust broker failed on shutdown")
        self.conn.close()
        self.tmp.cleanup()


def packet(response):
    assert "error" not in response, response
    result = response["result"]
    assert "error" not in result, result
    return json.loads(result["result"]["content"][0]["text"])


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness()
        self.addCleanup(self.h.close)

    def test_readonly_connection_serves_recall_while_writer_holds_lock(self):
        backend = Backend(self.h.config, "read")
        self.addCleanup(backend.conn.close)
        self.h.conn.execute("BEGIN IMMEDIATE")
        response = json.loads(backend.respond({"session": "chat-0", "id": "1", "operation": "call",
            "arguments": {"name": "memory", "arguments": {"recall": "item0"}}}))
        self.assertIn("Service0", packet(response)["packet"])
        self.assertTrue(self.h.conn.in_transaction)

    def test_read_worker_cannot_write_even_if_misrouted(self):
        backend = Backend(self.h.config, "read")
        self.addCleanup(backend.conn.close)
        response = json.loads(backend.respond({"session": "chat-0", "id": "1", "operation": "call",
            "arguments": {"name": "memory", "arguments": {"propose": "item0.md"}}}))
        self.assertIn("error", response["result"])

    def test_outer_transport_budget_rolls_back_mutation(self):
        backend = Backend(self.h.config, "write")
        self.addCleanup(backend.conn.close)
        before = self.h.conn.execute("SELECT revision FROM vault_state").fetchone()[0]
        def oversized(_):
            backend.conn.execute("UPDATE vault_state SET revision=revision+1")
            return "x" * (1024 * 1024)
        with patch.object(backend, "execute", side_effect=oversized):
            with self.assertRaises(ValueError):
                backend.respond({"session": "chat-0", "id": "1"})
        self.assertEqual(self.h.conn.execute("SELECT revision FROM vault_state").fetchone()[0], before)

    def test_duplicate_nested_fields_rejected(self):
        with self.assertRaises(ValueError):
            loads('{"a":{"b":1,"b":2}}')


@unittest.skipUnless(BINARY.is_file(), "build the Rust release binary before integration tests")
class RustProcessTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness()
        self.addCleanup(self.h.close)
        self.h.start()

    def test_ten_sessions_concurrent_reads_and_controls(self):
        for i in range(10):
            self.h.send(f"chat-{i}", "call", {"name": "memory", "arguments": {"recall": "item0"}})
        seen = set()
        for _ in range(10):
            response = self.h.receive()
            i = int(response["session"].split("-")[1])
            self.assertEqual(response["lane"], "read")
            text = packet(response)["packet"]
            self.assertIn(f"Service{i}", text)
            for other in range(10):
                if other != i:
                    self.assertNotIn(f"Service{other}", text)
            seen.add(i)
        self.assertEqual(len(seen), 10)
        response = self.h.call("disabled", "recall", "item0")
        self.assertIn("error", response["result"])
        self.h.send("foreign", "ping", {})
        self.assertEqual(self.h.receive()["error"], "invalid_request_or_session")

    def test_reads_progress_while_write_waits_and_same_chat_is_bounded(self):
        self.h.conn.execute("BEGIN IMMEDIATE")
        write_id = self.h.send("chat-0", "call", {"name": "memory", "arguments": {"propose": "item0.md"}})
        self.h.send("chat-0", "ping", {})
        self.h.send("chat-1", "call", {"name": "memory", "arguments": {"recall": "item0"}})
        early = [self.h.receive(), self.h.receive()]
        self.assertTrue(any(r.get("error") == "session_busy" for r in early))
        read = next(r for r in early if r["session"] == "chat-1")
        self.assertIn("Service1", packet(read)["packet"])
        self.assertTrue(self.h.conn.in_transaction)
        self.h.conn.rollback()
        write = self.h.receive()
        self.assertEqual(write["id"], write_id)
        self.assertEqual(write["lane"], "write")
        self.assertTrue(packet(write)["proposals"])

    def test_ten_concurrent_writes_return_scoped_receipts(self):
        for i in range(10):
            self.h.send(f"chat-{i}", "call", {"name": "memory", "arguments": {"propose": "item0.md"}})
        seen = set()
        for _ in range(10):
            response = self.h.receive()
            i = int(response["session"].split("-")[1])
            self.assertEqual(response["lane"], "write")
            proposal = packet(response)["proposals"][0]
            self.assertIn(f"Service{i}", proposal["summary"])
            seen.add(i)
        self.assertEqual(len(seen), 10)

    def test_reviewed_write_then_recall_and_low_quota(self):
        observed = time.time() - 1
        self.h.send("chat-0", "background-propose", {"path": "item0.md",
            "quota": {"observed_at": observed, "remaining_percent_by_window": {"primary": 24}},
            "chat": {"observed_at": observed, "last_activity_at": observed-86400,
                     "active": False, "external_context": False}})
        self.assertEqual(self.h.receive()["result"]["reason"], "quota_below_threshold")
        response = self.h.call("chat-0", "propose", "item0.md")
        self.assertTrue(packet(response)["proposals"])
        # Previously accepted memory remains available; proposals do not replace it.
        self.assertIn("Service0", packet(self.h.call("chat-0", "recall", "item0"))["packet"])


@unittest.skipUnless(BINARY.is_file(), "build the Rust release binary before integration tests")
class RustAdmissionTests(unittest.TestCase):
    def test_queue_bound_under_forty_simultaneous_writes(self):
        h = Harness()
        self.addCleanup(h.close)
        h.config["sessions"] = [dict(h.config["sessions"][0], id=f"burst-{i}") for i in range(40)]
        h.file.write_text(json.dumps(h.config), encoding="utf-8")
        h.start()
        h.conn.execute("BEGIN IMMEDIATE")
        for i in range(40):
            h.send(f"burst-{i}", "call", {"name": "memory", "arguments": {"propose": "item0.md"}})
        rejected = [h.receive() for _ in range(8)]
        self.assertTrue(all(r.get("error") == "queue_full" and not r["outcome_unknown"] for r in rejected))
        h.conn.rollback()
        accepted = [h.receive() for _ in range(32)]
        self.assertTrue(all(packet(r)["proposals"] for r in accepted))
        self.assertEqual(len({r["id"] for r in rejected + accepted}), 40)

    def test_failed_writer_is_quarantined_and_outcome_is_unknown(self):
        h = Harness()
        self.addCleanup(h.close)
        fake = h.root / "fake-backend"
        scripts = fake / "scripts"
        scripts.mkdir(parents=True)
        (scripts / "__init__.py").write_text("", encoding="utf-8")
        (scripts / "rust_worker.py").write_text(
            'import sys\nsys.stdin.readline()\nprint(\'{"ready":true}\', flush=True)\nsys.stdin.readline()\n', encoding="utf-8")
        h.config["backend_root"] = str(fake)
        h.file.write_text(json.dumps(h.config), encoding="utf-8")
        h.start()
        first = h.call("chat-0", "propose", "item0.md")
        self.assertEqual(first["error"], "worker_failed_no_automatic_retry")
        self.assertEqual(first["worker_failure"]["kind"],"transport_or_protocol")
        self.assertTrue(first["outcome_unknown"])
        second = h.call("chat-0", "propose", "item0.md")
        self.assertEqual(second["error"], "worker_unavailable")
        self.assertFalse(second["outcome_unknown"])


if __name__ == "__main__":
    unittest.main()
