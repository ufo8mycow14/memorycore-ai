"""Cross-language checks against disposable Python-created vaults."""
import json
import os
import subprocess
import threading
import unittest
import time
import copy
import base64
from dataclasses import asdict
from pathlib import Path

from scripts import memorycore_ai as bm
from scripts.knowledge_layer import Knowledge, SourceRoot
from scripts.knowledge_layer import digest
from scripts.memory_packets import token_counter
from scripts.test_rust_broker import Harness, BINARY, ROOT, packet
from scripts import test_rust_broker as broker_tests
from scripts.session_routing import RouteOutbox, load_checkpoint, save_checkpoint
from scripts.routing_cli import handle as routing_handle
from scripts.test_session_routing import state, session, Boundary, Costs


class NativeHarness(Harness):
    def start(self, role="write"):
        self.config["backend"] = "native"
        self.config.pop("python", None)
        self.config.pop("backend_root", None)
        self.process = subprocess.Popen([str(BINARY), "--native-worker", "--role", role],
            cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        def read():
            try:
                for line in self.process.stdout:
                    self.responses.put(json.loads(line))
            finally:
                self.responses.put(None)
        self.reader = threading.Thread(target=read, daemon=True)
        self.reader.start()
        self.process.stdin.write((json.dumps(self.config) + "\n").encode())
        self.process.stdin.flush()
        assert self.receive() == {"ready": True}

    def tool(self, **arguments):
        self.send("chat-0", "call", {"name": "memory", "arguments": arguments})
        return packet(self.receive())

    def admin(self, operation, **arguments):
        self.send("chat-0", "admin", {"action": operation, "arguments": arguments})
        response = self.receive()
        assert "error" not in response, (operation, response)
        return response["result"]

    def route(self, action, **arguments):
        self.send("chat-0", "routing-" + action, arguments)
        response = self.receive()
        assert "error" not in response, (action, response)
        return response["result"]


@unittest.skipUnless(BINARY.is_file(), "build Rust release binary")
class NativeParityTests(unittest.TestCase):
    def setUp(self):
        self.h = NativeHarness()
        self.h.config["sessions"][0]["allow_admin"] = True
        self.addCleanup(self.h.close)
        self.k = Knowledge(self.h.conn, scope=self.h.config["sessions"][0]["scope"],
            sources=SourceRoot(self.h.config["sessions"][0]["source_root"]), synthetic=True)

    def test_python_records_native_recall_and_read_lock(self):
        self.h.start("read")
        self.h.conn.execute("BEGIN IMMEDIATE")
        self.assertIn("Service0", self.h.tool(recall="item0")["packet"])
        self.h.send("chat-0", "call", {"name": "memory", "arguments": {"propose": "item0.md"}})
        self.assertIn("error", self.h.receive())

    def test_native_accept_python_verification_unicode_and_correction(self):
        source = Path(self.h.config["sessions"][0]["source_root"]) / "new.md"
        source.write_text("Fact: Caf\u00e9 records use Unicode.\nPreserve all details.\n", encoding="utf-8")
        self.h.start()
        p = self.h.tool(propose="new.md")["proposals"][0]
        self.assertEqual(self.k.review(p["id"])["review_digest"], p["review_digest"])
        accepted = self.h.tool(accept=p["id"], review_digest=p["review_digest"])
        bm.verify_scope(self.h.conn, self.k.scope)
        self.assertIn("Caf\u00e9", self.k.recall("Unicode", max_tokens=1400)["memories"][0]["summary"])
        source.write_text("Fact: Caf\u00e9 records use Unicode.\nPreserve revised details.\n", encoding="utf-8")
        p = self.h.tool(propose="new.md")["proposals"][0]
        revised = self.h.tool(accept=p["id"], review_digest=p["review_digest"], supersedes=accepted["memory_id"])
        self.assertNotEqual(revised["memory_id"], accepted["memory_id"])
        bm.verify_scope(self.h.conn, self.k.scope)
        self.k.export()

    def test_stale_source_and_bad_review_rollback(self):
        self.h.start()
        p = self.h.tool(propose="item0.md")["proposals"][0]
        before = self.h.conn.execute("SELECT revision FROM vault_state").fetchone()[0]
        self.h.send("chat-0", "call", {"name": "memory", "arguments": {"accept": p["id"], "review_digest": "0" * 64}})
        self.assertIn("error", self.h.receive()["result"])
        self.assertEqual(before, self.h.conn.execute("SELECT revision FROM vault_state").fetchone()[0])
        source = Path(self.h.config["sessions"][0]["source_root"]) / "item0.md"
        source.write_text("Fact: Changed contents.\n", encoding="utf-8")
        self.assertNotIn("Service0", self.h.tool(recall="item0")["packet"])

    def test_forget_review_python_compatible(self):
        self.h.start()
        mid = self.h.receipts[0, 0]["memory_id"]
        review = self.h.tool(review_forget=mid)
        self.h.tool(forget=mid, review_digest=review["review_digest"])
        bm.verify_scope(self.h.conn, self.k.scope)
        self.assertNotIn("Service0", self.h.tool(recall="item0")["packet"])

    def test_export_matches_python_and_import_roundtrip(self):
        self.h.start()
        package = self.h.admin("export")
        self.assertEqual(package, self.k.export())
        target = self.h.root / "import.sqlite3"
        conn = bm.connect(str(target))
        self.addCleanup(conn.close)
        bm.initialize(conn)
        k = Knowledge(conn, scope=self.k.scope, sources=self.k.sources, synthetic=True, create=True)
        k.import_package(package, reviewed=True)
        self.assertEqual(k.export(), package)
        native_target = self.h.root / "native.sqlite3"
        config = dict(self.h.config, backend="native", database=str(native_target))
        config_path = self.h.root / "native.json"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        result = subprocess.run([str(BINARY), "--init", "--config", str(config_path)], capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        request = {"session":"chat-0", "id":"1", "operation":"admin", "arguments":{"action":"import", "arguments":{"package":package,"reviewed":True}}}
        result = subprocess.run([str(BINARY), "--native-command", "--config", str(config_path)], input=json.dumps(request).encode(), capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        native_conn = bm.connect(str(native_target))
        self.addCleanup(native_conn.close)
        native_k = Knowledge(native_conn, scope=self.k.scope, sources=self.k.sources, synthetic=True)
        self.assertEqual(native_k.export(), package)

    def test_exact_stage_lifecycle_and_purge(self):
        self.h.start()
        raw = "Synthetic exact text with preserved spacing.  \n"
        exact = self.h.admin("store-exact", text=raw, user_confirmed=True)
        recalled = self.h.admin("recall-exact", archive_id=exact["archive_id"])
        self.assertEqual(base64.b64decode(recalled["base64"]), raw.encode())
        staged = self.h.admin("stage", text="Synthetic staged evidence.")
        self.assertEqual(self.h.admin("list", kind="stage", status="active")["count"],1)
        saved = self.h.admin("remember", type="semantic", subject="Staged synthetic", summary="A retained fact.", stage_id=staged["stage_id"])
        bm.verify_scope(self.h.conn, self.k.scope)
        for action, status in [("archive","archived"),("unarchive","active"),("forget","deleted"),("restore","active")]:
            self.assertEqual(self.h.admin("lifecycle", memory_id=saved["memory_id"], action=action)["status"], status)
            bm.verify_scope(self.h.conn, self.k.scope)
        self.h.admin("purge", memory_id=saved["memory_id"], user_confirmed=True)
        bm.verify_scope(self.h.conn, self.k.scope)
        self.assertTrue(self.h.admin("verify")["verified"])

    def test_admin_requires_explicit_host_authority(self):
        self.h.start()
        self.h.send("chat-1", "admin", {"action":"export", "arguments":{}})
        self.assertIn("error", self.h.receive())

    def test_routing_plans_match_reference(self):
        self.h.start()
        current = asdict(session(observed_at=time.time()-1))
        boundary = asdict(Boundary("beta", meaningful_change=True, needs_current_history=False, selective_context_complete=True))
        cases = [({}, {}, {}), ({}, {"keep_here":True}, {}), ({}, {"related":True}, {}),
                 ({"status":"running"}, {}, {}), ({"turns_since_transition":0}, {}, {}),
                 ({}, {}, {"retry_rework_tokens":100000}), ({}, {"explicit_fresh":True}, {}),
                 ({}, {"selective_context_complete":False}, {})]
        for cur, bound, costs in cases:
            args = {"current": current | cur, "boundary": boundary | bound,
                    "costs": asdict(Costs(3,1200)) | costs,
                    "sessions":[asdict(session("prior","beta",1500,observed_at=time.time()-1))]}
            self.assertEqual(self.h.route("plan", **args), routing_handle(self.k, "plan", args))

    def test_checkpoint_outbox_portability_and_stale_head(self):
        self.h.start()
        args = {"task_key":"alpha", "source_session":"source", "state":state()}
        receipt = self.h.route("checkpoint", **args)
        self.assertEqual(self.h.route("recall", receipt=receipt), load_checkpoint(self.k, receipt))
        args["state"] = state() | {"goal":"Finish revised synthetic checker"}
        revised = self.h.route("checkpoint", **args)
        self.assertNotEqual(receipt["memory_id"], revised["memory_id"])
        self.h.send("chat-0", "routing-recall", {"receipt":receipt})
        self.assertIn("error", self.h.receive())
        route = self.h.route("plan", current=asdict(session(observed_at=time.time()-1)),
            boundary=asdict(Boundary("beta", meaningful_change=True, needs_current_history=False, selective_context_complete=True)), costs=asdict(Costs(3,1200)))
        args = {"message_id":"message-1", "message":"Continue synthetic beta", "route":route,
                "current_task":"alpha", "state":state(), "target_packet":{"task_key":"beta","state":{}}}
        item = self.h.route("prepare", **args)
        self.assertEqual(self.h.route("prepare", **args), item)
        self.assertEqual(RouteOutbox(self.k)._read("message-1"), item)
        self.assertFalse(self.h.route("handoff", message_id="message-1")["occurred"])
        self.assertEqual(self.h.route("export"), RouteOutbox(self.k).export())
        self.assertTrue(self.h.route("cancel", message_id="message-1")["continue_original"])
        bm.verify_scope(self.h.conn, self.k.scope)

    def test_invalid_import_is_atomic(self):
        self.h.start()
        package = self.k.export()
        before = self.h.admin("export")
        package["sha256"] = "0" * 64
        self.h.send("chat-0", "admin", {"action":"import", "arguments":{"package":package,"reviewed":True}})
        self.assertIn("error", self.h.receive())
        self.assertEqual(self.h.admin("export"), before)

    def test_redaction_never_persists_original_secret(self):
        self.h.config["sessions"][0]["redact_secrets"] = True
        source = Path(self.h.config["sessions"][0]["source_root"]) / "redacted.md"
        original = "Fact: Synthetic credential redaction.\npassword=synthetic-private-value\nNever publish.\n"
        source.write_text(original, encoding="utf-8")
        self.h.start()
        p = self.h.tool(propose="redacted.md")["proposals"][0]
        self.assertIn("Never publish.", p["summary"])
        self.assertNotIn("synthetic-private-value", json.dumps(p))
        projected = SourceRoot(source.parent, redact_secrets=True)
        self.assertEqual(projected.inspect(p["binding"]), "fresh")
        self.assertNotIn(b"synthetic-private-value", self.h.conn.serialize())
        source.write_text(original.replace("private-value","changed-value"), encoding="utf-8")
        self.assertEqual(self.h.tool(review=p["id"])["source_state"], "changed")

    def test_token_budget_corruption_and_oversize_rollback(self):
        source = Path(self.h.config["sessions"][0]["source_root"]) / "large.md"
        source.write_text("\n".join(f"Fact: Large synthetic span {n}.\n" + "Retain complete words. "*100 for n in range(8)), encoding="utf-8")
        self.h.start()
        response = self.h.call("chat-0", "recall", "item0")
        self.assertLessEqual(token_counter()(json.dumps(response["result"], separators=(",", ":"))),1400)
        before = self.h.admin("export")
        self.h.send("chat-0", "call", {"name":"memory","arguments":{"propose":"large.md"}})
        self.assertIn("error", self.h.receive()["result"])
        self.assertEqual(self.h.admin("export"), before)
        self.h.conn.execute("UPDATE cortex_memory SET confidence=1 WHERE memory_id=?", (bytes.fromhex(self.h.receipts[0,0]["memory_id"]),))
        self.h.conn.commit()
        self.h.send("chat-0", "call", {"name":"memory","arguments":{"recall":"item0"}})
        self.assertIn("error", self.h.receive()["result"])

    def test_unicode_lines_match_reference(self):
        source = Path(self.h.config["sessions"][0]["source_root"]) / "lines.md"
        source.write_text("Fact: Synthetic Unicode lines.\u2028Preserve exception.\u0085End memory.\r\n", encoding="utf-8")
        self.h.start()
        native = self.h.tool(propose="lines.md")
        self.assertEqual(native, self.k.propose("lines.md"))

    def test_valid_envelope_corrupt_import_rolls_back(self):
        package = self.k.export()
        package["core"]["tables"]["cortex_memory"][0]["confidence"] = 1
        core = package["core"]
        import hashlib
        core["sha256"] = hashlib.sha256(json.dumps({k:v for k,v in core.items() if k!="sha256"}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        package["sha256"] = digest({k:v for k,v in package.items() if k!="sha256"})
        target = self.h.root / "bad-import.sqlite3"
        config = dict(self.h.config, backend="native", database=str(target))
        config_path = self.h.root / "bad-import.json"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        result = subprocess.run([str(BINARY), "--init", "--config", str(config_path)], capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        request = {"session":"chat-0","id":"1","operation":"admin","arguments":{"action":"import","arguments":{"package":package,"reviewed":True}}}
        result = subprocess.run([str(BINARY), "--native-command", "--config", str(config_path)], input=json.dumps(request).encode(), capture_output=True, timeout=30)
        self.assertNotEqual(result.returncode, 0)
        conn = bm.connect(str(target))
        try:
            self.assertEqual(conn.execute("SELECT count(*) FROM cortex_memory").fetchone()[0],0)
            self.assertEqual(conn.execute("SELECT count(*) FROM knowledge_item").fetchone()[0],0)
        finally:
            conn.close()

    def test_reviewed_prune_pin_and_alias_relation_behaviour(self):
        self.h.start()
        saved = self.h.admin("remember", type="semantic", subject="Synthetic low importance", summary="Keep reviewed fact.", importance=0.1)
        self.h.admin("lifecycle", memory_id=saved["memory_id"], action="pin")
        self.assertEqual(self.h.admin("prune", older_than_days=0)["count"],0)
        self.h.admin("lifecycle", memory_id=saved["memory_id"], action="unpin")
        preview = self.h.admin("prune", older_than_days=0)
        self.assertEqual(preview["count"],1)
        result = self.h.admin("prune", older_than_days=0, apply=True, user_confirmed=True,
            reviewed_ids=[preview["candidates"][0]["review_token"]])
        self.assertEqual(result["count"],1)
        self.assertEqual(self.h.admin("list", status="archived")["count"],1)
        aliases = self.h.admin("aliases", groups=[["sqlite","database"]], reviewed=True)
        self.assertEqual(self.k._items("aliases"),[aliases])
        self.assertIn("Service0", self.h.admin("recall", query="database", mode="aliases", max_tokens=1400)["memories"][0]["summary"])
        self.h.admin("lifecycle", memory_id=saved["memory_id"], action="unarchive")
        edge = self.h.admin("relate", owner=self.h.receipts[0,0]["memory_id"], target=saved["memory_id"],
            relation="depends_on", evidence="Synthetic reviewed dependency.", reviewed=True)
        self.assertEqual(self.h.tool(relations=saved["memory_id"]),self.k.relations(saved["memory_id"]))
        self.assertEqual(self.k._items("relation"),[edge])

    def test_generation_threshold_and_native_scope_rejection(self):
        self.h.start()
        clock = time.time()-1
        args = {"path":"item0.md","chat":{"observed_at":clock,"last_activity_at":clock-86400,"active":False,"external_context":False},
                "quota":{"observed_at":clock,"remaining_percent_by_window":{"short":25,"long":25}}}
        self.h.send("chat-0","background-propose",args)
        self.assertTrue(self.h.receive()["result"]["generated"])
        args["quota"]["remaining_percent_by_window"]["long"] = 24.99
        self.h.send("chat-0","background-propose",args)
        self.assertEqual(self.h.receive()["result"]["reason"],"quota_below_threshold")
        self.h.send("chat-1","call",{"name":"memory","arguments":{"review_forget":self.h.receipts[0,0]["memory_id"]}})
        self.assertIn("error",self.h.receive()["result"])

    def test_secret_rejection_and_unicode_casefold_conflict(self):
        self.h.start()
        for value in ["password=synthetic", "Authorization: Bearer synthetic-value", "4111 1111 1111 1111", "\u0664\u0661\u0661\u0661 \u0661\u0661\u0661\u0661 \u0661\u0661\u0661\u0661 \u0661\u0661\u0661\u0661"]:
            self.h.send("chat-0","admin",{"action":"remember","arguments":{"type":"semantic","subject":"Synthetic","summary":value}})
            self.assertIn("error",self.h.receive())
        self.h.admin("remember",type="semantic",subject="Stra\u00dfe",summary="Synthetic original.")
        source = Path(self.h.config["sessions"][0]["source_root"])/"casefold.md"
        source.write_text("Fact: STRASSE\nSynthetic correction.\n",encoding="utf-8")
        p = self.h.tool(propose="casefold.md")["proposals"][0]
        self.h.send("chat-0","call",{"name":"memory","arguments":{"accept":p["id"],"review_digest":p["review_digest"]}})
        self.assertIn("error",self.h.receive()["result"])
        self.h.admin("remember",type="semantic",subject="DEL\x7f character",summary="Synthetic encoding.")
        bm.verify_scope(self.h.conn,self.k.scope)


class NativeBrokerProcessTests(broker_tests.RustProcessTests):
    def setUp(self):
        self.h = Harness()
        self.addCleanup(self.h.close)
        self.h.config.pop("backend")
        self.h.config.pop("python")
        self.h.config.pop("backend_root")
        self.h.file.write_text(json.dumps(self.h.config), encoding="utf-8")
        ready = self.h.start()
        self.assertEqual(ready["backend"], "rust-native")


if __name__ == "__main__":
    unittest.main()
