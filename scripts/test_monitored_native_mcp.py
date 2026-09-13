import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from scripts.monitored_native_mcp import MetricsLog, MonitoredNativeMCP, summarise


class MonitoredNativeMCPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.metrics = Path(self.tmp.name) / "metrics.jsonl"
        self.host = Mock(sessions={"fixed": {}})
        self.server = MonitoredNativeMCP(self.host, "fixed", MetricsLog(self.metrics))

    def initialize(self):
        return self.server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                   "params": {"protocolVersion": "2025-06-18"}})

    def test_metrics_are_payload_free_but_keep_recall_health(self):
        self.initialize()
        packet = {"mode": "verified_hybrid",
                  "packet": "Memory data; {\"scope\":\"local\"}\n{\"summary\":\"Synthetic Adelaide payload\"}\nomitted=0; candidates_capped=false",
                  "tokens": 11}
        def exchange(request):
            return {"id": request["id"], "session": "fixed", "semantic_host": {
                "resource_limited": False,
                "resource_budget": {"pressured": False, "memory_pressure": False,
                                    "system_cpu_pressure": False, "host_cpu_pressure": False,
                                    "inference_limit": 4},
                "retrieval": {"vector_state": "ready", "reranker_state": "ready",
                              "reranker_scored": 1, "reranker_candidates": 1},
                "timing": {"encode_ms": 1.0, "candidates_ms": 2.0, "rerank_ms": 3.0,
                           "final_ms": 4.0},
                "index_errors": 0,
                "index": {"stored": 1, "failed": 0, "deferred": 0},
            }, "result": {"result": {"content": [{"type": "text", "text": json.dumps(packet)}]}}}
        self.host.exchange.side_effect = exchange
        response = self.server.handle({"jsonrpc": "2.0", "id": "call", "method": "tools/call",
                                       "params": {"name": "memory",
                                                  "arguments": {"recall": "private query text"}}})
        self.assertIn("result", response)
        row = json.loads(self.metrics.read_text(encoding="utf-8"))
        self.assertEqual(row["memory_arguments"], ["recall"])
        self.assertEqual(row["packet"]["mode"], "verified_hybrid")
        self.assertEqual(row["packet"]["included"], 1)
        self.assertEqual(row["packet"]["omitted"], 0)
        self.assertEqual(row["retrieval"]["vector_state"], "ready")
        self.assertNotIn("private query text", json.dumps(row))
        self.assertNotIn("Synthetic Adelaide payload", json.dumps(row))

    def test_summary_counts_errors_and_states(self):
        rows = [
            {"latency_ms": 10, "tool_text_tokens": 50, "response_tokens": 70,
             "error": None, "memory_arguments": ["recall"], "packet": {"mode": "verified_hybrid"},
             "retrieval": {"vector_state": "ready", "reranker_state": "ready"}, "resource": {}},
            {"latency_ms": 30, "error": "queue_full", "memory_arguments": ["recall"],
             "packet": {}, "retrieval": {"vector_state": "unavailable"},
             "resource": {"system_cpu_pressure": True}},
        ]
        self.metrics.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
        result = summarise(self.metrics)
        self.assertEqual(result["rows"], 2)
        self.assertEqual(result["errors"], {"queue_full": 1})
        self.assertEqual(result["vector_states"], {"ready": 1, "unavailable": 1})
        self.assertEqual(result["resource_pressure_events"], {"system_cpu_pressure": 1})
        self.assertEqual(result["included_memories"]["count"], 0)


if __name__ == "__main__":
    unittest.main()
