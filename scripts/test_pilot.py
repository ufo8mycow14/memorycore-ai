"""Pilot reproducibility, physical-layout and accounting regression tests."""

import hashlib
import json
import sqlite3
import unittest
from unittest.mock import patch

from scripts import memorycore_ai as bm
from scripts import detail_projection, storage_experiment, workflow_pilot
from scripts.test_memorycore_ai_regressions import memory_args
from scripts.test_recommendations import ns


class PilotTests(unittest.TestCase):
    def database(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        bm.initialize(conn)
        self.addCleanup(conn.close)
        return conn

    def test_export_order_does_not_depend_on_indexes(self):
        conn = self.database()
        for i in range(20):
            bm.remember(conn, memory_args(subject=f"Synthetic {i}", type="semantic" if i % 2 else "procedural"))
        before = bm.export_scope(conn, "project:fixture-a")
        conn.execute("DROP INDEX cortex_scope_status")
        conn.execute("CREATE INDEX cortex_scope_status ON cortex_memory(scope,status)")
        conn.execute("CREATE INDEX cortex_type_status ON cortex_memory(memory_type,status)")
        conn.commit()
        self.assertEqual(bm.export_scope(conn, "project:fixture-a"), before)

    def test_new_layout_keeps_integrity_and_scoped_query_index(self):
        conn = self.database()
        fields = [r[2] for r in conn.execute("PRAGMA index_info(cortex_scope_status)")]
        self.assertEqual(fields, ["scope", "status", "memory_type"])
        self.assertFalse(conn.execute("SELECT 1 FROM sqlite_master WHERE name='cortex_type_status'").fetchone())
        self.assertIn("WITHOUT ROWID", conn.execute("SELECT sql FROM sqlite_master WHERE name='cortex_tombstone'").fetchone()[0])
        self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_existing_database_not_silently_rebuilt(self):
        conn = self.database()
        conn.execute("DROP INDEX cortex_scope_status")
        conn.execute("CREATE INDEX cortex_scope_status ON cortex_memory(scope,status)")
        conn.execute("CREATE INDEX cortex_type_status ON cortex_memory(memory_type,status)")
        conn.commit()
        before = conn.serialize()
        bm.initialize(conn)
        self.assertEqual(conn.serialize(), before)

    def test_populated_tombstones_survive_portability(self):
        conn = self.database()
        for i in range(80):
            saved = bm.remember(conn, memory_args(subject=f"Synthetic {i}"))
            bm.purge(conn, ns(scope="project:fixture-a", memory_id=saved["memory_id"], user_confirmed=True))
        package = bm.export_scope(conn, "project:fixture-a")
        other = self.database()
        bm.import_scope(other, package, "project:fixture-a", user_confirmed=True)
        self.assertEqual(len(package["tables"]["cortex_tombstone"]), 80)
        self.assertEqual(bm.export_scope(other, "project:fixture-a"), package)

    def test_selective_detail_projection_round_trips(self):
        result = detail_projection.run()
        self.assertTrue(result["round_trips_verified"])
        self.assertFalse(result["adopted"])
        for case in result["cases"]:
            if case["detail_length"] > case["threshold"]:
                self.assertEqual(case["projected_payload_bytes"], case["baseline_payload_bytes"])
                self.assertEqual(case["added_summary_decode_bytes"], 0)

    def test_protocol_counter_counts_one_tool_response_not_two(self):
        case = workflow_pilot.fixtures()[0]
        small = workflow_pilot.request_cost(len, case, "read", {}, "x")
        large = workflow_pilot.request_cost(len, case, "read", {}, "x" * 101)
        self.assertEqual(large["total"] - small["total"], 100)
        self.assertEqual(large["first_request"], small["first_request"])

    def test_complete_workflow_contracts_and_missing_usage_are_explicit(self):
        result = workflow_pilot.run()
        self.assertEqual(len(result["model_tasks"]), 12)
        self.assertIsNone(result["actual_input_tokens"])
        self.assertFalse(result["model_quality_measured"])
        for case in result["scenarios"]:
            self.assertTrue(case["contract_assertions_passed"])
            self.assertGreater(case["setup_reference_tokens"], 0)
            self.assertGreater(case["exact_reference_tokens"], 0)
            self.assertGreater(case["maintenance_reference_tokens"], 0)
            self.assertGreater(case["warm_full_request_tokens"]["memory"][-1], case["warm_full_request_tokens"]["memory"][0])
            self.assertGreater(case["horizons"][-1]["memory_full_skill_reference_tokens"], case["horizons"][-1]["memory_reference_tokens"])


if __name__ == "__main__":
    unittest.main()
