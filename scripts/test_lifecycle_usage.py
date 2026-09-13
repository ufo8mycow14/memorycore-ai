"""Incomplete native generation counters must never be treated as zero."""
import copy
import unittest
from scripts.lifecycle_usage import PHASES,compare,summarise


def arm(inputs=1000,cached=200,output=100):
    return {"model_profile":{"provider":"openai","model":"synthetic-model","reasoning_effort":"medium",
                              "reasoning_mode":"standard","reasoning_context":"all_turns",
                              "text_verbosity":"medium"},
            "coverage":{p:"observed" if p=="answer" else "no_calls" for p in PHASES},
            "calls":[{"task":"a","call_id":"1","phase":"answer","status":"completed","input_tokens":inputs,
                      "cached_input_tokens":cached,"output_tokens":output,"reasoning_tokens":40}]}


class UsageTests(unittest.TestCase):
    def fixture(self):
        return {"format":"memory-lifecycle-usage/1","baseline":arm(),"repaired":arm(600,200),"matched_conditions":True,
                "paired_tasks":[{"id":"a","baseline_pass":True,"repaired_pass":True}]}

    def test_savings_include_output_without_double_counting_reasoning(self):
        result=compare(self.fixture())
        self.assertEqual(result["baseline"]["observed_tokens"]["total"],1100)
        self.assertEqual(result["baseline"]["observed_tokens"]["ordinary_input"],800)
        self.assertAlmostEqual(result["total_token_saving"],1-700/1100)
        self.assertAlmostEqual(result["uncached_input_saving"],.5)
        self.assertTrue(result["savings_target_met"])
        self.assertIsNone(result["cost_saving"])
        self.assertEqual(result["model_profile"]["reasoning_effort"],"medium")

    def test_missing_background_or_cached_usage_prevents_claim(self):
        document=self.fixture()
        document["baseline"]["coverage"]["consolidate"]="unknown"
        result=compare(document)
        self.assertFalse(result["comparable"])
        self.assertIsNone(result["total_token_saving"])
        document=self.fixture()
        document["repaired"]["calls"][0]["cached_input_tokens"]=None
        self.assertFalse(compare(document)["savings_target_met"])

    def test_failures_retries_and_quality_regressions_are_not_discarded(self):
        document=self.fixture()
        retry=copy.deepcopy(document["repaired"]["calls"][0])
        retry.update(call_id="2",phase="retry",status="failed")
        document["repaired"]["coverage"]["retry"]="observed"
        document["repaired"]["calls"].append(retry)
        result=compare(document)
        self.assertEqual(result["repaired"]["observed_tokens"]["failed_calls"],1)
        self.assertLess(result["total_token_saving"],0)
        document=self.fixture()
        document["paired_tasks"][0]["repaired_pass"]=False
        self.assertFalse(compare(document)["savings_target_met"])

    def test_duplicate_and_inconsistent_counters_rejected(self):
        data=arm()
        data["calls"].append(copy.deepcopy(data["calls"][0]))
        with self.assertRaises(ValueError):
            summarise(data)

    def test_observed_phase_without_calls_is_incomplete(self):
        data=arm()
        data["coverage"]["consolidate"]="observed"
        self.assertFalse(summarise(data)["complete"])

    def test_unknown_usage_keeps_failed_call_count(self):
        data=arm()
        data["calls"][0].update(status="failed",input_tokens=None)
        result=summarise(data)
        self.assertFalse(result["complete"])
        self.assertEqual(result["observed_tokens"]["calls"],1)
        self.assertEqual(result["observed_tokens"]["failed_calls"],1)

    def test_missing_task_or_unpaired_usage_cannot_claim_savings(self):
        document=self.fixture()
        document["paired_tasks"].append({"id":"b","baseline_pass":True,"repaired_pass":True})
        with self.assertRaises(ValueError):
            compare(document)
        document=self.fixture()
        document["repaired"]["calls"][0]["task"]="unpaired"
        with self.assertRaises(ValueError):
            compare(document)
        data=arm()
        data["calls"][0]["cached_input_tokens"]=1001
        with self.assertRaises(ValueError):
            summarise(data)

    def test_cache_write_tokens_are_tracked_separately(self):
        data=arm(inputs=1200,cached=500)
        data["calls"][0]["cache_write_tokens"]=300
        result=summarise(data)["observed_tokens"]
        self.assertEqual(result["cached_input"],500)
        self.assertEqual(result["cache_write_input"],300)
        self.assertEqual(result["ordinary_input"],400)
        self.assertEqual(result["uncached_input"],700)

    def test_reasoning_profile_mismatch_prevents_comparison(self):
        document=self.fixture()
        document["repaired"]["model_profile"]["reasoning_effort"]="high"
        result=compare(document)
        self.assertFalse(result["comparable"])
        self.assertIsNotNone(result["profile_mismatch"])
        self.assertIsNone(result["total_token_saving"])
