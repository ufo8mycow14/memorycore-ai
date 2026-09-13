"""Monitored project-scoped MCP adapter for local MemoryCore AI rollout.

The monitor records payload-free operational metrics beside the existing native
MCP adapter. It must not become a second memory store: queries, recalled text,
source snippets and exact content are never written to the metrics log.
"""
import argparse
import json
import statistics
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

from scripts.native_mcp import NativeMCP, serve, unique_object


def _percentile(values, fraction):
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * fraction))))
    return ordered[index]


def _token_counter():
    try:
        from scripts.memory_packets import token_counter
        return token_counter()
    except SystemExit:
        return None


class MetricsLog:
    def __init__(self, path, *, clock=time.time, perf=time.perf_counter):
        self.path = Path(path)
        self.clock = clock
        self.perf = perf
        self.count = _token_counter()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def start(self):
        return self.perf()

    def count_tokens(self, value):
        if self.count is None:
            return None
        return self.count(json.dumps(value, ensure_ascii=False, separators=(",", ":")))

    def write(self, row):
        row = {"ts": self.clock(), "schema": "memorycore-ai-monitor/v1", **row}
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, ensure_ascii=True, allow_nan=False, separators=(",", ":")) + "\n")

    def record(self, *, operation, arguments, response, started, error=None):
        elapsed = (self.perf() - started) * 1000
        result = response.get("result") if isinstance(response, dict) else None
        tool_result = result.get("result") if isinstance(result, dict) else None
        text = ""
        if isinstance(tool_result, dict):
            for item in tool_result.get("content", []):
                if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str):
                    text += item["text"]
        packet = {}
        if text:
            try:
                decoded = json.loads(text)
                if isinstance(decoded, dict):
                    packet = decoded
            except json.JSONDecodeError:
                packet = {}
        packet_text = packet.get("packet", "") if isinstance(packet.get("packet"), str) else ""
        packet_lines = packet_text.splitlines()
        compact_omitted = None
        if packet_lines and packet_lines[-1].startswith("omitted="):
            try:
                compact_omitted = int(packet_lines[-1].split(";", 1)[0].split("=", 1)[1])
            except (IndexError, ValueError):
                compact_omitted = None
        compact_included = None
        if packet_lines:
            compact_included = max(0, len(packet_lines) - 2)
        host = response.get("semantic_host", {}) if isinstance(response, dict) else {}
        retrieval = host.get("retrieval", {}) if isinstance(host, dict) else {}
        packet_cache = host.get("packet_cache", {}) if isinstance(host, dict) else {}
        timing = host.get("timing", {}) if isinstance(host, dict) else {}
        budget = host.get("resource_budget", {}) if isinstance(host, dict) else {}
        index = host.get("index", {}) if isinstance(host, dict) else {}
        call_args = arguments.get("arguments", {}) if isinstance(arguments, dict) else {}
        memory_args = call_args.get("arguments", {}) if isinstance(call_args, dict) else {}
        if not memory_args and isinstance(arguments, dict) and arguments.get("name") == "memory":
            memory_args = arguments.get("arguments", {})
        requested = sorted(key for key in memory_args if key in {
            "recall", "graph", "propose", "accept", "reject", "forget", "freshness", "relations",
            "review-forget",
        })
        row = {
            "operation": operation,
            "memory_arguments": requested,
            "latency_ms": elapsed,
            "error": error or response.get("error") or (result.get("error") if isinstance(result, dict) else None),
            "outcome_unknown": response.get("outcome_unknown") if isinstance(response, dict) else None,
            "tool_text_tokens": self.count(text) if text and self.count else None,
            "response_tokens": self.count_tokens(response) if isinstance(response, dict) else None,
            "token_counter": "o200k_base" if self.count else "unavailable",
            "packet": {
                "mode": packet.get("mode"),
                "included": packet.get("included", compact_included),
                "omitted": packet.get("omitted", compact_omitted),
                "tokens": packet.get("tokens"),
            },
            "retrieval": {
                "vector_state": retrieval.get("vector_state"),
                "reranker_state": retrieval.get("reranker_state"),
                "reranker_scored": retrieval.get("reranker_scored"),
                "reranker_candidates": retrieval.get("reranker_candidates"),
                "admission": retrieval.get("admission"),
                "packet_cache": retrieval.get("packet_cache"),
            },
            "packet_cache": {key: packet_cache.get(key) for key in (
                "hits", "misses", "stores", "evictions", "invalidations", "entries", "scopes")},
            "timing_ms": {key: timing.get(key) for key in ("encode_ms", "candidates_ms", "rerank_ms", "final_ms")},
            "resource": {
                "limited": host.get("resource_limited") if isinstance(host, dict) else None,
                "pressured": budget.get("pressured"),
                "memory_pressure": budget.get("memory_pressure"),
                "system_cpu_pressure": budget.get("system_cpu_pressure"),
                "host_cpu_pressure": budget.get("host_cpu_pressure"),
                "inference_limit": budget.get("inference_limit"),
            },
            "index": {
                "errors": host.get("index_errors") if isinstance(host, dict) else None,
                "pending_high_water": index.get("pending_high_water"),
                "stored": index.get("stored"),
                "failed": index.get("failed"),
                "deferred": index.get("deferred"),
            },
        }
        self.write(row)


class MonitoredNativeMCP(NativeMCP):
    def __init__(self, host, session, metrics):
        super().__init__(host, session)
        self.metrics = metrics

    def exchange(self, operation, arguments):
        started = self.metrics.start()
        response = None
        error = None
        try:
            response = self.host.exchange({
                "session": self.session,
                "id": __import__("uuid").uuid4().hex,
                "operation": operation,
                "arguments": arguments,
            })
            if (response.get("session") != self.session or "error" in response
                    or not isinstance(response.get("result"), dict)):
                error = response.get("error") or "native_response_rejected"
                raise ValueError("Native response rejected")
            return response["result"]
        except Exception:
            if response is None:
                response = {"error": "host_exception"}
                error = "host_exception"
            raise
        finally:
            self.metrics.record(operation=operation, arguments=arguments, response=response,
                                started=started, error=error)


def summarise(path):
    latencies = []
    tool_tokens = []
    response_tokens = []
    included = []
    omitted = []
    errors = Counter()
    modes = Counter()
    vector_states = Counter()
    reranker_states = Counter()
    arguments = Counter()
    resources = Counter()
    packet_cache_states = Counter()
    packet_cache_latest = {}
    rows = 0
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        rows += 1
        if isinstance(row.get("latency_ms"), (int, float)):
            latencies.append(row["latency_ms"])
        if isinstance(row.get("tool_text_tokens"), int):
            tool_tokens.append(row["tool_text_tokens"])
        if isinstance(row.get("response_tokens"), int):
            response_tokens.append(row["response_tokens"])
        if row.get("error"):
            errors[str(row["error"])] += 1
        packet = row.get("packet", {})
        retrieval = row.get("retrieval", {})
        if isinstance(packet.get("included"), int):
            included.append(packet["included"])
        if isinstance(packet.get("omitted"), int):
            omitted.append(packet["omitted"])
        if packet.get("mode"):
            modes[packet["mode"]] += 1
        if retrieval.get("vector_state"):
            vector_states[retrieval["vector_state"]] += 1
        if retrieval.get("reranker_state"):
            reranker_states[retrieval["reranker_state"]] += 1
        if retrieval.get("packet_cache"):
            packet_cache_states[retrieval["packet_cache"]] += 1
        if isinstance(row.get("packet_cache"), dict) and any(
                isinstance(value, int) for value in row["packet_cache"].values()):
            packet_cache_latest = row["packet_cache"]
        for arg in row.get("memory_arguments", []):
            arguments[arg] += 1
        resource = row.get("resource", {})
        for key in ("pressured", "memory_pressure", "system_cpu_pressure", "host_cpu_pressure"):
            if resource.get(key):
                resources[key] += 1
    def stats(values):
        return {
            "count": len(values),
            "median": statistics.median(values) if values else None,
            "p95": _percentile(values, .95),
            "p99": _percentile(values, .99),
            "max": max(values) if values else None,
        }
    hits = packet_cache_latest.get("hits") if isinstance(packet_cache_latest.get("hits"), int) else None
    misses = packet_cache_latest.get("misses") if isinstance(packet_cache_latest.get("misses"), int) else None
    total_cacheable = hits + misses if hits is not None and misses is not None else None
    hit_rate = round((hits / total_cacheable) * 100, 2) if total_cacheable else None
    return {
        "schema": "memorycore-ai-monitor-summary/v1",
        "rows": rows,
        "latency_ms": stats(latencies),
        "tool_text_tokens": stats(tool_tokens),
        "response_tokens": stats(response_tokens),
        "included_memories": stats(included),
        "omitted_memories": stats(omitted),
        "errors": dict(errors),
        "memory_arguments": dict(arguments),
        "packet_modes": dict(modes),
        "vector_states": dict(vector_states),
        "reranker_states": dict(reranker_states),
        "packet_cache_states": dict(packet_cache_states),
        "packet_cache_latest": packet_cache_latest,
        "packet_cache_effect": {
            "cacheable_recalls": total_cacheable,
            "hits": hits,
            "misses": misses,
            "hit_rate_percent": hit_rate,
            "estimated_full_retrievals_avoided": hits,
        },
        "resource_pressure_events": dict(resources),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command")
    serve_parser = sub.add_parser("serve")
    serve_parser.add_argument("--binary", type=Path, required=True)
    serve_parser.add_argument("--config", type=Path, required=True)
    serve_parser.add_argument("--cache", type=Path, required=True)
    serve_parser.add_argument("--session", required=True)
    serve_parser.add_argument("--metrics", type=Path, required=True)
    summary_parser = sub.add_parser("summary")
    summary_parser.add_argument("--metrics", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "summary":
        print(json.dumps(summarise(args.metrics), indent=2, sort_keys=True))
        return
    if args.command != "serve":
        parser.error("select serve or summary")
    configuration = json.loads(args.config.read_text(encoding="utf-8"), object_pairs_hook=unique_object)
    if (configuration.get("synthetic") is not True or configuration.get("backend") != "native"
            or args.session not in {s["id"] for s in configuration.get("sessions", [])}):
        raise ValueError("Explicit synthetic native session required")
    from scripts.memory_host import MemoryHost
    host = MemoryHost(args.binary, args.config, args.cache)
    try:
        serve(MonitoredNativeMCP(host, args.session, MetricsLog(args.metrics)), sys.stdin.buffer, sys.stdout)
    finally:
        host.close()


if __name__ == "__main__":
    main()
