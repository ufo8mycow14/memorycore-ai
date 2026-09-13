"""Real process reopening and public handoff interface acceptance."""
import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from scripts.native_capabilities import manifest
from scripts.fresh_context import start_params
from scripts.test_session_routing import state
from scripts.routing_calibration import fresh_verification_profile


class RoutingCliTests(unittest.TestCase):
    def test_calibration_counts_intermediate_calls_and_rejects_incomplete_evidence(self):
        record = {"route":{"action":"fresh"}, "accepted":True, "native":{"status":"completed",
                  "usage_delta":{"inputTokens":39000, "outputTokens":100, "totalTokens":39100},
                  "usage":{"last":{"inputTokens":20000, "outputTokens":40, "totalTokens":20040}}}}
        profile = fresh_verification_profile([record])
        self.assertEqual(profile["fresh_verification_tokens"], 19060)
        self.assertFalse(profile["future_savings_guaranteed"])
        record["accepted"] = False
        with self.assertRaises(ValueError):
            fresh_verification_profile([record])
        record["accepted"] = True
        record["native"]["usage_delta"]["totalTokens"] = 1
        with self.assertRaises(ValueError):
            fresh_verification_profile([record])
    def test_persistent_checkpoint_handoff_cancel_and_complete_export(self):
        with tempfile.TemporaryDirectory(prefix="brain-routing-cli-") as folder:
            command = [sys.executable, "-B", "-m", "scripts.routing_cli", "--synthetic", "--db", str(Path(folder)/"fixture.sqlite3"),
                       "--source-root", folder, "--scope", "synthetic:cli-routing"]
            def call(operation, data=None):
                result = subprocess.run(command+[operation], input=json.dumps(data) if data is not None else "",
                    text=True, capture_output=True, cwd=Path(__file__).resolve().parents[1], timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)
                return json.loads(result.stdout)
            self.assertTrue(call("init")["initialised"])
            checkpoint = call("checkpoint", {"task_key":"alpha", "source_session":"synthetic-session", "state":state()})
            self.assertEqual(call("recall", {"receipt":checkpoint})["state"], state())
            route = call("plan", {"current":{"id":"synthetic-session", "task_key":"alpha", "placement":{
                    "host":"local", "project_id":"synthetic-project", "surface":"project_chat"},
                    "history_tokens":30000, "status":"idle", "revision":"r1", "observed_at":time.time(), "turns_since_transition":5},
                "boundary":{"task_key":"beta", "meaningful_change":True, "needs_current_history":False, "selective_context_complete":True},
                "costs":{"remaining_turns":3, "fresh_context_tokens":1200}})
            self.assertEqual(route["action"], "fresh")
            prepared = call("prepare", {"message_id":"pending-1", "message":"New task: beta\nKeep exact identifier B-18.",
                "route":route, "current_task":"alpha", "state":state(), "target_packet":{"task_key":"beta", "state":{}}})
            self.assertEqual(prepared["state"], "PREPARED")
            handoff = call("handoff", {"message_id":"pending-1"})
            self.assertFalse(handoff["occurred"])
            self.assertEqual(handoff["state"], "HANDOFF")
            self.assertEqual(handoff["target_placement"]["project_id"], "synthetic-project")
            exported = call("export")
            self.assertEqual(exported["intents"][0]["message"], handoff["pending_message"])
            self.assertFalse(exported["automatic_delivery_restore"])
            self.assertTrue(call("cancel", {"message_id":"pending-1"})["continue_original"])
            self.assertEqual(call("export")["intents"][0]["state"], "CANCELLED")

    def test_current_native_manifest_cannot_enable_dispatch(self):
        capability = manifest()
        self.assertFalse(capability["automatic_dispatch"])
        self.assertTrue(capability["unverified_contracts"])
        self.assertFalse(any(capability["dispatch_capabilities"].values()))

    def test_fresh_preserves_native_instruction_defaults_and_project_parameter(self):
        self.assertEqual(start_params({"projectId":"synthetic-project"}), {"projectId":"synthetic-project"})
        for field in ("baseInstructions", "developerInstructions"):
            with self.assertRaises(ValueError):
                start_params({field:"replace native defaults"})


if __name__ == "__main__":
    unittest.main()
