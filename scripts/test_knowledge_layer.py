"""Behavioural and boundary tests for the synthetic knowledge extensions."""
import argparse
import hashlib
import json
import os
import sqlite3
import sys
import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts import memorycore_ai as bm
from scripts.knowledge_layer import Knowledge, SourceRoot, canonical, digest
from scripts.code_context import GraphifyFile, MCPCodeProvider, context_packet
from scripts.memory_mcp_lab import LabServer
from scripts.mcp_peer import StdioPeer
from scripts.memory_policy import check_text
from unittest.mock import patch
from types import SimpleNamespace


class KnowledgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="brain-knowledge-synthetic-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.conn = self.database()
        self.k = Knowledge(self.conn, scope="synthetic:a", sources=SourceRoot(self.root), synthetic=True, create=True)

    def database(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        bm.initialize(conn)
        self.addCleanup(conn.close)
        return conn

    def write(self, path="decision.md", text="Decision: Retry transient failures three times.\nNever retry invalid input."):
        (self.root / path).write_text(text, encoding="utf-8")
        return hashlib.sha256((self.root / path).read_bytes()).hexdigest()

    def remember(self, summary="Retry transient failures three times; never retry invalid input.", subject="Retry policy", source="decision.md"):
        args = bm.build_parser().parse_args(["remember", "--scope", self.k.scope, "--type", "semantic",
            "--subject", subject, "--summary", summary, "--source", source])
        return bm.remember(self.conn, args)["memory_id"]

    def bound(self, **kwargs):
        sha = self.write()
        mid = self.remember(**kwargs)
        self.k.bind_source(mid, "decision.md", sha)
        return mid

    def test_freshness_is_independent_of_ttl_and_never_silently_renewed(self):
        mid = self.bound()
        self.assertEqual(self.k.freshness(mid)["state"], "fresh")
        self.write(text="Decision: Retry twice.")
        self.assertEqual(self.k.freshness(mid)["state"], "stale")
        self.assertEqual(self.k.recall("retry", count=len, max_tokens=2000)["memories"], [])
        self.assertIsNone(self.k._memory(mid)["expires_at"])
        with self.assertRaises(ValueError):
            self.k.bind_source(mid, "decision.md", hashlib.sha256((self.root / "decision.md").read_bytes()).hexdigest())

    def test_missing_and_unverified_sources_are_explicit(self):
        mid = self.bound()
        (self.root / "decision.md").unlink()
        self.assertEqual(self.k.freshness(mid)["sources"][0]["state"], "missing")
        other = self.remember(subject="Other")
        result = self.k.recall("retry", count=len, max_tokens=2000)
        self.assertEqual(result["excluded"], {"stale": 1, "unverified": 1})
        result = self.k.recall("retry", require_fresh=False, count=len, max_tokens=2000)
        self.assertEqual([m["id"] for m in result["memories"]], [other])

    def test_paths_cannot_escape_the_host_root(self):
        for path in ("../outside", "C:/outside", "a\\b", "/etc/passwd", "./a", "a/../b", "x:stream"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.k.sources.read(path)

    def test_source_links_are_rejected(self):
        self.write()
        try:
            (self.root / "linked.md").symlink_to(self.root / "decision.md")
        except OSError:
            # Exercise the Windows reparse-bit rejection without depending on
            # the host's symlink privilege. This is a unit, not OS ACL, check.
            with patch.object(Path, "lstat", return_value=SimpleNamespace(st_file_attributes=0x400, st_mode=0o100644)):
                with self.assertRaises(ValueError):
                    self.k.sources.read("decision.md")
            return
        with self.assertRaises(ValueError):
            self.k.sources.read("linked.md")

    def test_prohibited_source_never_becomes_proposal(self):
        self.write(text="Decision: password=synthetic-prohibited-value")
        with self.assertRaises(SystemExit):
            self.k.propose("decision.md")
        self.assertEqual(self.k._items(), [])

    def test_decoded_json_secrets_are_rejected(self):
        self.write("source.json", '{"pass\\u0077ord":"synthetic-only"}')
        with self.assertRaises(SystemExit):
            self.k.propose("source.json")

    def test_hex_fingerprints_do_not_trigger_card_substring_false_positive(self):
        check_text("a" * 24 + "4532015112830366" + "b" * 24)
        with self.assertRaises(SystemExit):
            check_text("Card number: 4532015112830366")
        with self.assertRaises(SystemExit):
            check_text("card4532015112830366suffix")
        with self.assertRaises(SystemExit):
            check_text("password=" + "a" * 24 + "4532015112830366" + "b" * 24)

    def test_legacy_recall_cannot_bypass_source_validation(self):
        self.bound()
        args = bm.build_parser().parse_args(["recall", "retry", "--scope", self.k.scope])
        with self.assertRaises(SystemExit):
            bm.recall(self.conn, args)

    def test_proposals_preserve_exceptions_and_are_not_recalled_before_review(self):
        self.write()
        proposed = self.k.propose("decision.md")["proposals"][0]
        self.assertIn("Never retry invalid input", proposed["summary"])
        self.assertEqual(self.k.recall("retry", count=len)["memories"], [])
        with self.assertRaises(ValueError):
            self.k.accept(proposed["id"], "incorrect")
        saved = self.k.accept(proposed["id"], proposed["review_digest"])
        recalled = self.k.recall("retry", count=len, max_tokens=2000)["memories"][0]
        self.assertEqual(recalled["id"], saved["memory_id"])
        self.assertIn("Never retry invalid input", recalled["summary"])
        self.assertEqual(self.k._items("proposal"), [])
        with self.assertRaises(ValueError):
            self.k.accept(proposed["id"], proposed["review_digest"])

    def test_changed_source_invalidates_proposal_acceptance(self):
        self.write()
        p = self.k.propose("decision.md")["proposals"][0]
        self.write(text="Decision: Different rule.")
        with self.assertRaises(ValueError):
            self.k.accept(p["id"], p["review_digest"])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM cortex_memory").fetchone()[0], 0)

    def test_pending_proposals_expire_and_are_disposed(self):
        self.write()
        p = self.k.propose("decision.md")["proposals"][0]
        with patch.object(bm, "now_utc", return_value=bm.after_days(2)):
            with self.assertRaises(ValueError):
                self.k.accept(p["id"], p["review_digest"])
            self.assertEqual(self.k.expire_proposals()["expired_proposals_disposed"], 1)
        self.assertEqual(self.k._items("proposal"), [])

    def test_extractor_skips_unlabelled_prose_and_deduplicates(self):
        self.write(text="Unlabelled synthetic conversation.")
        self.assertEqual(self.k.propose("decision.md")["proposals"], [])
        self.write()
        a = self.k.propose("decision.md")
        self.assertEqual(a, self.k.propose("decision.md"))

    def test_span_submission_is_verbatim_and_bounded(self):
        sha = self.write(text="Synthetic note.\nNever retry invalid input.\nRetry transient failures only.")
        p = self.k.propose_spans("decision.md", [{"line_start": 2, "line_end": 3, "subject": "Retry", "type": "procedural"}], expected_sha256=sha)
        self.assertEqual(p["proposals"][0]["summary"], "Never retry invalid input.\nRetry transient failures only.")
        with self.assertRaises(SystemExit):
            self.k.propose_spans("decision.md", [{"line_start": 2, "line_end": 99, "subject": "Retry", "type": "procedural"}], expected_sha256=sha)

    def test_correcting_memory_does_not_retarget_old_evidence(self):
        self.write(text="Decision: Retry policy\nUse three attempts.")
        p = self.k.propose("decision.md")["proposals"][0]
        old = self.k.accept(p["id"], p["review_digest"])["memory_id"]
        self.write(text="Decision: Retry policy\nUse two attempts.")
        p = self.k.propose("decision.md")["proposals"][0]
        with self.assertRaises(ValueError):
            self.k.accept(p["id"], p["review_digest"])
        new = self.k.accept(p["id"], p["review_digest"], supersedes=old)["memory_id"]
        self.assertNotEqual(old, new)
        self.assertEqual(self.k.freshness(new)["state"], "fresh")
        with self.assertRaises(ValueError):
            self.k.freshness(old)

    def test_aliases_improve_synonym_recall_without_cross_scope_leakage(self):
        self.bound(summary="Use the colour blue.", subject="Palette")
        self.assertFalse(self.k.recall("hue", count=len, max_tokens=2000)["memories"])
        self.k.aliases([["colour", "hue", "color"]], reviewed=True)
        for mode in ("aliases", "hybrid"):
            self.assertEqual(len(self.k.recall("hue", mode=mode, count=len, max_tokens=2000)["memories"]), 1)
        other = Knowledge(self.conn, scope="synthetic:b", sources=self.k.sources, synthetic=True)
        self.assertEqual(other.recall("hue", mode="aliases", count=len)["memories"], [])

    def test_relations_check_scope_lifecycle_and_cycles(self):
        a, b = self.bound(), self.remember(subject="Evidence")
        self.k.relate(a, b, "supported_by", "Reviewed synthetic evidence", reviewed=True)
        self.k.relate(b, a, "depends_on", "Reviewed reverse link", reviewed=True)
        self.assertEqual(len(self.k.relations(a, depth=3)["edges"]), 2)
        with self.assertRaises(ValueError):
            self.k.relate(a, b, "proves", "not allowed", reviewed=True)
        other = Knowledge(self.conn, scope="synthetic:b", sources=self.k.sources, synthetic=True)
        with self.assertRaises(ValueError):
            other.relations(a)
        bm.lifecycle(self.conn, argparse.Namespace(action="forget", scope=self.k.scope, memory_id=b))
        self.assertEqual(self.k.relations(a)["edges"], [])

    def test_purge_cascades_source_and_relation_metadata(self):
        a, b = self.bound(), self.remember(subject="Evidence")
        self.k.relate(a, b, "supported_by", "Synthetic", reviewed=True)
        bm.purge(self.conn, argparse.Namespace(scope=self.k.scope, memory_id=a, user_confirmed=True))
        self.assertEqual(self.k._items(), [])

    def test_export_never_silently_loses_extension_records(self):
        self.bound()
        with self.assertRaises(SystemExit):
            bm.export_scope(self.conn, self.k.scope)
        p = self.k.export()
        other = Knowledge(self.database(), scope=self.k.scope, sources=self.k.sources, synthetic=True, create=True)
        other.import_package(p, reviewed=True)
        self.assertEqual(other.export(), p)

    def test_bad_extension_import_rolls_back_core(self):
        self.bound()
        package = self.k.export()
        package["items"][0]["scope"] = "synthetic:other"
        package["sha256"] = digest({k:v for k,v in package.items() if k != "sha256"})
        other = Knowledge(self.database(), scope=self.k.scope, sources=self.k.sources, synthetic=True, create=True)
        with self.assertRaises(ValueError):
            other.import_package(package, reviewed=True)
        self.assertEqual(other.conn.execute("SELECT count(*) FROM cortex_memory").fetchone()[0], 0)

    def test_corrupt_metadata_is_rejected_before_use(self):
        self.bound()
        self.conn.execute("UPDATE knowledge_item SET payload='{}'")
        self.conn.commit()
        with self.assertRaises(ValueError):
            self.k.recall("retry", count=len)

    def test_budget_omits_whole_facts_and_reports_omission(self):
        self.bound(summary="Never retry invalid input. " * 80)
        packet = self.k.recall("retry", count=len, max_tokens=400)
        self.assertLessEqual(len(canonical(packet)), 400)
        self.assertEqual(packet["memories"], [])
        self.assertEqual(packet["omitted"], 1)

    def server(self, **kwargs):
        server = LabServer(self.k, **kwargs)
        result = server.handle({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {"protocolVersion": "2024-11-05", "clientInfo": {"name": "codex"}}})
        self.assertIn("result", result)
        return server

    def call(self, server, name, args):
        return server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}})

    def test_mcp_mutation_rolls_back_when_complete_envelope_exceeds_budget(self):
        self.write(text="Decision: " + "Synthetic long fact. " * 100)
        server = self.server(count=len, max_tokens=500)
        result = self.call(server, "memory_propose", {"path": "decision.md"})
        self.assertIn("error", result)
        self.assertEqual(self.k._items(), [])

    def test_mcp_rejects_scope_override_unknown_fields_and_notifications(self):
        self.write()
        server = self.server(count=len)
        result = self.call(server, "memory_propose", {"path": "decision.md", "scope": "other"})
        self.assertIn("error", result)
        server.handle({"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "memory_propose", "arguments": {"path": "decision.md"}}})
        self.assertEqual(self.k._items(), [])

    def test_mcp_forget_digest_cannot_be_replayed_or_swapped(self):
        mid = self.bound()
        other = self.remember(subject="Other")
        server = self.server(count=len, max_tokens=4000)
        review = json.loads(self.call(server, "memory_review_forget", {"id": mid})["result"]["content"][0]["text"])
        args = {"id": other, "review_digest": review["review_digest"]}
        self.assertIn("error", self.call(server, "memory_forget", args))
        args["id"] = mid
        self.assertIn("result", self.call(server, "memory_forget", args))
        self.assertIn("error", self.call(server, "memory_forget", args))

    def test_stdio_transport_handshake_and_proposal_review(self):
        self.write()
        cmd = [sys.executable, "-B", "-m", "scripts.memory_mcp_lab", "--synthetic", "--source-root", str(self.root), "--scope", self.k.scope]
        with StdioPeer(cmd, cwd=Path(__file__).resolve().parents[1], allowed_tools=["memory_propose", "memory_accept", "memory_recall"]) as peer:
            result = peer.call_tool("memory_propose", {"path": "decision.md"})
            proposal = json.loads(result["content"][0]["text"])["proposals"][0]
            peer.call_tool("memory_accept", {"id": proposal["id"], "review_digest": proposal["review_digest"]})
            recalled = json.loads(peer.call_tool("memory_recall", {"query": "retry"})["content"][0]["text"])
            self.assertIn("Never retry", recalled["packet"])
            with self.assertRaises(ValueError):
                peer.call_tool("memory_forget", {})
        self.assertIsNotNone(peer.process.poll())

    def test_graphify_adapter_checks_index_sources_and_keeps_confidence(self):
        sha = self.write("retry.py", "def retry():\n    return 3\n")
        graph = {"nodes": [{"id": "retry", "label": "retry", "source_file": "retry.py"},
                           {"id": "caller", "label": "caller", "source_file": "retry.py"}],
                 "links": [{"source": "caller", "target": "retry", "source_file": "retry.py", "confidence": "INFERRED", "relation": "calls"}]}
        self.write("graph.json", json.dumps(graph))
        provider = GraphifyFile(self.k.sources, "graph.json", {"retry.py": sha})
        result = provider.context("retry")
        self.assertEqual(result["status"], "fresh")
        self.assertEqual(result["edges"][0]["confidence"], "INFERRED")
        self.assertTrue(context_packet(provider, "retry", count=len, max_tokens=128)["omitted"])
        self.write("retry.py", "def retry():\n    return 2\n")
        self.assertEqual(provider.context("retry")["status"], "stale_index")

    def test_mcp_code_adapters_use_only_fixed_read_tools(self):
        calls = []
        class Peer:
            def call_tool(self, tool, arguments):
                calls.append((tool, arguments))
                return {"content": [{"type": "text", "text": "Synthetic symbol evidence"}]}
        for name in ("gitnexus", "serena"):
            p = MCPCodeProvider(name, Peer(), repository="synthetic-repo")
            self.assertEqual(p.context("retry")["status"], "provider_reported_unverified")
        self.assertEqual(calls, [("context", {"name": "retry", "repo": "synthetic-repo"}),
                                 ("find_symbol", {"name_path_pattern": "retry", "include_body": False})])

    def test_persistent_cli_reopens_and_exports_complete_state(self):
        self.write()
        command = [sys.executable, "-B", "-m", "scripts.knowledge_cli", "--synthetic", "--db", str(self.root / "fixture.sqlite3"),
                   "--source-root", str(self.root), "--scope", self.k.scope]
        def run(operation, data=None):
            r = subprocess.run(command + [operation], input=canonical(data) if data is not None else "", capture_output=True, text=True,
                               cwd=Path(__file__).resolve().parents[1], timeout=15)
            self.assertEqual(r.returncode, 0, r.stderr)
            return json.loads(r.stdout)
        run("init")
        p = json.loads(run("call", {"name": "memory_propose", "arguments": {"path": "decision.md"}})["result"]["content"][0]["text"])["proposals"][0]
        run("call", {"name": "memory_accept", "arguments": {"id": p["id"], "review_digest": p["review_digest"]}})
        package = run("export")
        self.assertEqual(len(package["items"]), 1)
        self.assertEqual(package["items"][0]["kind"], "source")

    def test_peer_timeout_closes_only_the_spawned_fixture(self):
        command = [sys.executable, "-c", "import time; time.sleep(5)"]
        with self.assertRaises(ValueError):
            StdioPeer(command, timeout=0.2)

    def test_explicit_synthetic_and_foreign_key_guards(self):
        with self.assertRaises(ValueError):
            Knowledge(self.conn, scope=self.k.scope, sources=self.k.sources)
        self.conn.execute("PRAGMA foreign_keys=OFF")
        with self.assertRaises(ValueError):
            Knowledge(self.conn, scope=self.k.scope, sources=self.k.sources, synthetic=True)

    def test_explicit_evidence_end_does_not_retain_unrelated_context(self):
        self.write(text="Constraint: Retry policy\nNever retry invalid input.\nEnd memory.\nUnrelated discussion.")
        p = self.k.propose("decision.md")["proposals"][0]
        self.assertIn("Never retry invalid input", p["summary"])
        self.assertNotIn("Unrelated", p["summary"])

    def test_compact_tool_reuses_renderer_and_preserves_constraints(self):
        self.bound()
        server = self.server(max_tokens=1400)
        tools = server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]
        self.assertEqual([tool["name"] for tool in tools], ["memory"])
        result = self.call(server, "memory", {"recall": "retry"})
        packet = json.loads(result["result"]["content"][0]["text"])
        self.assertIn("never retry invalid input", packet["packet"])
        self.assertTrue(packet["source_checked"])
        self.assertIn("error", self.call(server, "memory", {"recall": "retry", "propose": "unexpected.md"}))
        self.assertIn("error", self.call(server, "memory", {"recall": "retry", "supersedes": "unexpected"}))

    def test_shared_source_is_read_once_per_request_and_rechecked_next_time(self):
        self.bound()
        second = self.remember(subject="Retry evidence")
        sha = hashlib.sha256((self.root / "decision.md").read_bytes()).hexdigest()
        self.k.bind_source(second, "decision.md", sha)
        with patch.object(self.k.sources, "read", wraps=self.k.sources.read) as read:
            self.assertEqual(len(self.k.recall("retry", max_tokens=2000)["memories"]), 2)
            self.assertEqual(read.call_count, 1)
            self.write(text="Decision: Different retry policy.")
            result = self.k.recall("retry", max_tokens=2000)
            self.assertEqual(result["excluded"]["stale"], 2)
            self.assertEqual(read.call_count, 2)


if __name__ == "__main__":
    unittest.main()
