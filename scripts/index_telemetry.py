"""Bounded metadata-only indexing latency; never retains memory IDs or text."""
from bisect import bisect_left
from contextlib import contextmanager
import math
import threading
import time


BOUNDS_MS = tuple(range(0, 1001, 10)) + tuple(range(1025, 5001, 25)) + tuple(range(5500, 60001, 500))


class IndexStages:
    """Fixed-cardinality monotonic timings, including unsuccessful attempts."""
    NAMES = ("status", "claim", "encode", "put", "release", "pressure_wait")

    def __init__(self, clock=time.perf_counter):
        self.clock = clock
        self.lock = threading.Lock()
        self.values = {name: {"count": 0, "total_ms": 0.0, "max_ms": 0.0} for name in self.NAMES}

    @contextmanager
    def measure(self, name):
        if name not in self.values:
            raise ValueError("Unknown indexing stage")
        start = self.clock()
        try:
            yield
        finally:
            elapsed = max(0.0, (self.clock() - start) * 1000)
            with self.lock:
                value = self.values[name]
                value["count"] += 1
                value["total_ms"] += elapsed
                value["max_ms"] = max(value["max_ms"], elapsed)

    def snapshot(self):
        with self.lock:
            return {name: dict(value) for name, value in self.values.items()}


class IndexLatency:
    def __init__(self):
        self.lock = threading.Lock()
        self.buckets = [0] * (len(BOUNDS_MS) + 1)
        self.unmeasured = 0
        self.clock_regressions = 0

    def observe(self, enqueued_times, observed_ms):
        with self.lock:
            for enqueued_ms in enqueued_times:
                if type(enqueued_ms) is not int or enqueued_ms <= 0:
                    self.unmeasured += 1
                elif observed_ms < enqueued_ms:
                    self.clock_regressions += 1
                else:
                    self.buckets[bisect_left(BOUNDS_MS, observed_ms - enqueued_ms)] += 1

    def snapshot(self):
        with self.lock:
            return {"buckets": list(self.buckets), "unmeasured": self.unmeasured,
                    "clock_regressions": self.clock_regressions}


def latency_delta(before, after):
    if len(before["buckets"]) != len(BOUNDS_MS) + 1 or len(after["buckets"]) != len(BOUNDS_MS) + 1:
        raise ValueError("Index latency histogram contract changed")
    counts = [new - old for old, new in zip(before["buckets"], after["buckets"])]
    unmeasured = after["unmeasured"] - before["unmeasured"]
    regressions = after["clock_regressions"] - before["clock_regressions"]
    if min(counts + [unmeasured, regressions]) < 0:
        raise ValueError("Index latency counters reset during measurement")
    count = sum(counts)
    quantiles = {}
    for label, fraction in (("p50", .5), ("p95", .95), ("p99", .99), ("max", 1)):
        rank = math.ceil(count * fraction)
        cumulative = 0
        value = None
        for bound, n in zip((*BOUNDS_MS, None), counts):
            cumulative += n
            if count and cumulative >= rank:
                value = bound
                break
        quantiles[label + "_upper_ms"] = value
    return {"count": count, **quantiles, "over_60000_ms": counts[-1],
            "unmeasured": unmeasured, "clock_regressions": regressions,
            "bounds_ms": BOUNDS_MS, "buckets": counts,
            "measurement": "Database enqueue timestamp to host observation of successful vector commit; "
                           "conservative upper bound including acknowledgement IPC, with bucket upper bounds.",
            "coverage": "Completed indexing jobs only; superseded, purged, ineligible and pending jobs are not successful samples."}
