"""Synthetic ten-session warm recall benchmark; not production capacity proof."""
import argparse
import json
import math
import platform
import time
from pathlib import Path
from .test_rust_broker import Harness, packet


def percentile(values, p):
    return sorted(values)[max(0, math.ceil(len(values) * p)-1)]


def run():
    h = Harness(records_per_scope=10)
    try:
        h.start()
        timings, services, queues = [], [], []
        for cycle in range(21):
            started = {}
            for i in range(10):
                before = time.monotonic()
                mid = h.send(f"chat-{i}", "call", {"name": "memory", "arguments": {"recall": "item0"}})
                started[mid] = before
            for _ in range(10):
                result = h.receive()
                elapsed = (time.monotonic()-started[result["id"]])*1000
                i = int(result["session"].split("-")[1])
                body = packet(result)["packet"]
                assert f"Service{i}" in body
                assert all(f"Service{other}" not in body for other in range(10) if other != i)
                if cycle:
                    timings.append(elapsed)
                    services.append(result["timing"]["service_ms"])
                    queues.append(result["timing"]["queue_ms"])
        return {"kind":"rust-broker-synthetic-recall", "platform":platform.platform(),
            "sessions":10,"records":100,"read_workers":4,"write_workers":1,
            "measured_requests":len(timings),"warmup_requests":10,"errors":0,
            "startup_ms":h.startup_ms,"round_trip_ms":{f"p{int(p*100)}":percentile(timings,p) for p in (.5,.95,.99)},
            "service_p95_ms":percentile(services,.95),"queue_p95_ms":percentile(queues,.95),
            "limitations":["Small synthetic corpus and warm local reads; not sustained multi-user or mixed-write capacity proof.",
                "Rust scheduling with persistent Python compatibility workers, not a Python-free implementation.",
                "No encryption, native capture, network service or production activation.",
                "All processes are shut down after measurement."]}
    finally:
        h.close()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    if args.output.exists():
        raise ValueError("preserve earlier evidence")
    result = run()
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, indent=2)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
