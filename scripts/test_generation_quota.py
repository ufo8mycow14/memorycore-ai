"""Quota boundary tests; no model calls or live account access."""
import unittest
from unittest.mock import Mock

from scripts.generation_quota import background_propose, generation_decision


class GenerationQuotaTests(unittest.TestCase):
    def test_eligibility_is_not_a_generation_receipt(self):
        result = generation_decision({"observed_at":30000,
            "remaining_percent_by_window":{"short":25,"weekly":25}}, now=30000,
            chat={"observed_at":30000,"last_activity_at":-56400,"active":False,"external_context":False})
        self.assertEqual(result["reason"], "eligible")
        self.assertFalse(result["generated"])
        self.assertNotIn("proposal", result)

    def run_gate(self, windows, observed=100, now=100):
        k = Mock()
        k.propose.return_value = {"proposal_id": "synthetic"}
        result = background_propose(k, "fixture.md", {
            "observed_at": observed, "remaining_percent_by_window": windows}, now=now,
            chat={"observed_at": now, "last_activity_at": now-86400, "active": False, "external_context": False})
        self.assertEqual(k.propose.call_count, int(result["generated"]))
        return result

    def test_threshold_is_remaining_not_used_and_inclusive(self):
        for value, expected in [(0, False), (24.99, False), (25, True), (75, True), (100, True)]:
            with self.subTest(value=value):
                self.assertEqual(self.run_gate({"primary": value})["generated"], expected)

    def test_every_window_must_meet_threshold(self):
        self.assertFalse(self.run_gate({"primary": 90, "weekly": 24})["generated"])
        self.assertTrue(self.run_gate({"primary": 25, "weekly": 30})["generated"])

    def test_unknown_and_invalid_windows_fail_closed(self):
        for windows in [{}, None, [], {"p": None}, {"p": True}, {"p": "90"},
                        {"p": float("nan")}, {"p": float("inf")}, {"p": -1}, {"p": 101}]:
            with self.subTest(windows=windows):
                self.assertEqual(self.run_gate(windows)["reason"], "quota_unavailable")

    def test_stale_future_and_invalid_observations(self):
        for observed in [39, 101]:
            self.assertEqual(self.run_gate({"p": 90}, observed)["reason"], "quota_stale")
        self.assertTrue(self.run_gate({"p": 90}, 40)["generated"])
        self.assertEqual(self.run_gate({"p": 90}, None)["reason"], "quota_unavailable")

    def test_missing_quota_never_calls_generator(self):
        k = Mock()
        for quota in [None, {}, {"usedPercent": 1}]:
            self.assertFalse(background_propose(k, "fixture.md", quota, now=30000,
                chat={"observed_at": 30000, "last_activity_at": 0, "active": False, "external_context": False})["generated"])
        k.propose.assert_not_called()

    def test_generation_errors_are_not_reported_as_success(self):
        k = Mock()
        k.propose.side_effect = ValueError("invalid synthetic source")
        with self.assertRaises(ValueError):
            background_propose(k, "fixture.md", {"observed_at": 100,
                "remaining_percent_by_window": {"p": 25}}, now=100,
                chat={"observed_at": 100, "last_activity_at": -86300, "active": False, "external_context": False})


if __name__ == "__main__":
    unittest.main()
