"""Index freshness measurements are bounded and cannot hide unknown samples."""
import unittest
from scripts.index_telemetry import BOUNDS_MS, IndexLatency, IndexStages, latency_delta


class IndexStagesTests(unittest.TestCase):
    def test_records_failed_attempts_and_rejects_unbounded_names(self):
        times = iter((1, 1.05, 2, 2.1))
        stages = IndexStages(clock=lambda: next(times))
        with stages.measure("encode"):
            pass
        with self.assertRaises(RuntimeError):
            with stages.measure("encode"):
                raise RuntimeError("synthetic failure")
        result = stages.snapshot()
        self.assertEqual(result["encode"]["count"], 2)
        self.assertAlmostEqual(result["encode"]["total_ms"], 150)
        self.assertAlmostEqual(result["encode"]["max_ms"], 100)
        result["encode"]["count"] = 99
        self.assertEqual(stages.snapshot()["encode"]["count"], 2)
        with self.assertRaises(ValueError):
            with stages.measure("memory-id"):
                pass


class IndexLatencyTests(unittest.TestCase):
    def test_observations_use_conservative_bucket_upper_bounds(self):
        metrics = IndexLatency()
        before = metrics.snapshot()
        metrics.observe([1000, 1990, 2000, None, 2001], 2000)
        result = latency_delta(before, metrics.snapshot())
        self.assertEqual(result["count"], 3)
        self.assertEqual(result["p50_upper_ms"], 10)
        self.assertEqual(result["p95_upper_ms"], 1000)
        self.assertEqual(result["unmeasured"], 1)
        self.assertEqual(result["clock_regressions"], 1)
        self.assertFalse(any(before["buckets"]))

    def test_delta_excludes_prewarm_and_exposes_overflow(self):
        metrics = IndexLatency()
        metrics.observe([1000], 1001)
        before = metrics.snapshot()
        metrics.observe([1000], 61001)
        result = latency_delta(before, metrics.snapshot())
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["over_60000_ms"], 1)
        self.assertIsNone(result["p99_upper_ms"])
        self.assertEqual(len(result["buckets"]), len(BOUNDS_MS) + 1)
        with self.assertRaisesRegex(ValueError, "reset"):
            latency_delta(metrics.snapshot(), before)

    def test_counter_storage_is_independent_of_job_count(self):
        metrics = IndexLatency()
        for now in range(1000, 100000):
            metrics.observe([1000], now)
        self.assertEqual(sum(metrics.snapshot()["buckets"]), 99000)
        self.assertEqual(len(metrics.snapshot()["buckets"]), len(BOUNDS_MS) + 1)
