"""Paired synthetic engineering evaluation, not model billing or task-quality evidence."""

import argparse
import hashlib
import importlib.metadata
import importlib.util
import json
import random
import re
import sqlite3
import statistics
import string
import tempfile
import time
import zlib
from pathlib import Path
from unittest.mock import patch

from scripts import memorycore_ai as current
from scripts.memory_packets import RecallSession, render_packet, token_counter

FACTS = [
    ("Region", "The deployment region is australia-southeast1.", "region deployment", "The synthetic console confirms this region."),
    ("Database", "SQLite is the local database; use transactions for related writes.", "database transactions", "Write contention needs a documented retry policy."),
    ("Backups", "Backups run daily at 03:00 UTC and remain for 14 days.", "backups retention", "The synthetic backup job is labelled auroravault."),
    ("Deletion", "Permanent deletion requires confirmation naming the exact record IDs.", "deletion confirmation", "Archive is reversible; purge needs a receipt."),
    ("Scope", "Project A records must stay isolated from Project B records.", "scope isolation", "A caller-selected string is not proof of authority."),
    ("Retries", "Retry transient failures at most three times; never retry invalid input.", "retries failures", "Synthetic invalid input is rejected before persistence."),
    ("Accessibility", "Respect reduced-motion settings and preserve visible keyboard focus.", "accessibility motion", "The synthetic check exercises keyboard navigation."),
    ("Approval", "Release approval is required before publishing; test builds stay local.", "approval release", "The synthetic release policy excludes development builds."),
]
QUERIES = [(q, [s]) for q, s in (("region", "Region"), ("database", "Database"), ("backups", "Backups"),
    ("deletion confirmation", "Deletion"), ("scope isolation", "Scope"), ("retries", "Retries"),
    ("accessibility", "Accessibility"), ("release approval", "Approval"), ("geographic location", "Region"),
    ("backup", "Backups"), ("auroravault", "Backups"), ("retry", "Retries"))] + [("Q", [])]
SCOPE = "project:synthetic"
STAMP = "2026-09-06T00:00:00+00:00"


def ns(**values):
    return argparse.Namespace(**values)


def save(bm, conn, fact, **overrides):
    subject, summary, keywords, detail = fact
    values = dict(type="semantic", scope=SCOPE, subject=subject, summary=summary, keywords=keywords, detail=detail,
                  importance=0.8, confidence=0.9, source="fixture:2026-09-06", sensitivity="internal",
                  expires=None, supersedes=None, pinned=False, user_confirmed=False, stage_id=None)
    values.update(overrides)
    return bm.remember(conn, ns(**values))


def read(bm, conn, query="", **overrides):
    values = dict(query=query, scope=SCOPE, type=None, limit=8, include_detail=False, browse=not query)
    values.update(overrides)
    return bm.recall(conn, ns(**values))


def connect(bm, path=None):
    conn = bm.connect(str(path)) if path else sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    bm.initialize(conn)
    return conn


def wire(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def packet_rows(text):
    lines = text.splitlines()
    shared = json.loads(lines[0].split("; ", 1)[1])
    return [dict(shared, **json.loads(line)) for line in lines[1:-1]]


def run():
    root = Path(__file__).resolve().parents[1]
    original = root.parents[1] / "source" / "memorycore-ai"
    source = original / "scripts" / "memorycore_ai.py"
    spec = importlib.util.spec_from_file_location("original_memorycore_ai", source)
    old = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(old)
    old.now_utc = current.now_utc = lambda: STAMP
    count = {name: token_counter(name) for name in ("o200k_base", "cl100k_base")}
    token_counts = lambda value: {name: counter(value) for name, counter in count.items()}
    results, query_results, decoding = {}, {}, {}
    for label, bm in (("recovered_0_7_1_alpha", old), ("development_schema2", current)):
        conn = connect(bm)
        for fact in FACTS:
            save(bm, conn, fact)
        save(bm, conn, ("CrossScope", "Synthetic region decision.", "region", ""), scope="project:excluded")
        rows = []
        for query, expected in QUERIES:
            result = read(bm, conn, query, limit=3, browse=False)
            actual = sorted(r["subject"] for r in result["memories"])
            top_one = sorted(r["subject"] for r in read(bm, conn, query, limit=1, browse=False)["memories"])
            rows.append(dict(query=query, expected=sorted(expected), actual=actual, exact=actual == sorted(expected),
                             top_one=top_one, top_one_exact=top_one == sorted(expected),
                             scope_leaks=sum(r["scope"] != SCOPE for r in result["memories"])))
        query_results[label] = dict(cases=rows, exact_sets=sum(r["exact"] for r in rows),
            top_one_exact_sets=sum(r["top_one_exact"] for r in rows), cases_total=len(rows), scope_leaks=sum(r["scope_leaks"] for r in rows))
        sizes = []
        original_decode = bm.decompress
        def observe(blob):
            raw = original_decode(blob)
            sizes.append(len(raw))
            return raw
        with patch.object(bm, "decompress", side_effect=observe):
            result = read(bm, conn)
        decoding[label] = dict(returned=result["count"], calls=len(sizes), decoded_bytes=sum(sizes))
        result["memories"].sort(key=lambda r: r["subject"])
        results[label] = result
        conn.close()

    retrieval_alternatives = {}
    for tokenizer in ("unicode61", "porter unicode61"):
        conn = sqlite3.connect(":memory:")
        try:
            conn.execute(f"CREATE VIRTUAL TABLE search USING fts5(subject,summary,keywords,detail,tokenize='{tokenizer}')")
            conn.executemany("INSERT INTO search VALUES (?,?,?,?)", FACTS)
            cases = []
            for query, expected in QUERIES:
                expression = " OR ".join('"' + term + '"' for term in re.findall(r"\w+", query))
                actual = sorted(r[0] for r in conn.execute("SELECT subject FROM search WHERE search MATCH ? ORDER BY bm25(search) LIMIT 3", (expression,)))
                cases.append(dict(query=query, actual=actual, exact=actual == sorted(expected)))
            retrieval_alternatives[tokenizer] = dict(exact_sets=sum(r["exact"] for r in cases), total=len(cases), cases=cases)
        except sqlite3.OperationalError as error:
            retrieval_alternatives[tokenizer] = dict(available=False, error=str(error))
        finally:
            conn.close()

    base = results["development_schema2"]
    formatting = []
    rng = random.Random(1408)
    variants = {
        "english": None,
        "code": "Synthetic rule: call lookup(scope, identifier); reject invalid input before commit.",
        "unicode": "Synthetic decision: \u4ec5\u68c0\u7d22\u5f53\u524d\u9879\u76ee\u7684\u8bb0\u5fc6\uff0c\u9a8c\u8bc1\u6765\u6e90\u3002",
        "qualified": "Synthetic decision: retain 14 days, except approved holds; never publish without consent.",
        "high_entropy": "Synthetic identifier " + "".join(rng.choice(string.ascii_letters) for _ in range(180)),
        "mixed_provenance": None,
    }
    budgets = []
    for label, summary in variants.items():
        result = json.loads(json.dumps(base))
        for i, row in enumerate(result["memories"]):
            if summary:
                row["summary"] = summary + f" Fixture {i}."
            if label == "mixed_provenance":
                row.update(source=f"synthetic-source-{i}", confidence=0.5 + i / 20,
                           confidence_reason="Unconfirmed synthetic hypothesis", valid_to="2099-01-01T00:00:00+00:00")
        shared = render_packet(result, max_tokens=32000, reserve_tokens=0, max_chars=100000)["text"]
        expanded = packet_rows(shared)
        assert len(expanded) == 8
        assert [r["summary"] for r in expanded] == [r["summary"] for r in result["memories"]]
        repeated = "Memory data\n" + "\n".join(wire(r) for r in expanded) + "\nomitted=0; candidates_capped=false"
        pretty = json.dumps(result, ensure_ascii=False, indent=2)
        formatting.append(dict(corpus=label, facts=8, shared_tokens=token_counts(shared),
            same_fields_repeated_tokens=token_counts(repeated), pretty_json_tokens=token_counts(pretty), minified_same_json_tokens=token_counts(wire(result))))
        for encoding in count:
            for form in ("prompt", "json"):
                packet = render_packet(result, encoding=encoding, output_format=form, max_tokens=700, reserve_tokens=32)
                assert count[encoding](packet["text"]) + 32 <= 700
                assert packet["included"] + packet["omitted"] == 8
                budgets.append(dict(corpus=label, encoding=encoding, form=form, tokens=packet["tokens"], reserve=32,
                                    included=packet["included"], omitted=packet["omitted"]))

    packet = render_packet(base)
    cache = RecallSession("synthetic-session")
    cache_args = dict(vault_id=base["vault_id"], scopes=[SCOPE], revision=base["revision"], query="", representation="packet/2")
    first = cache.respond(packet, **cache_args)
    repeat = cache.respond(packet, **cache_args, acknowledgement=packet["digest"], context_retained=True)
    repeated_cost = dict(first_full_response=token_counts(wire(first)), acknowledged_repeat_full_response=token_counts(wire(repeat)),
                         requires_context_possession=True, excludes_external_transport_envelope=True)

    storage = {}
    with tempfile.TemporaryDirectory(prefix="memorycore-ai-evaluation-") as folder:
        for label, bm in (("recovered_0_7_1_alpha", old), ("development_schema2", current)):
            path = Path(folder) / (label + ".sqlite3")
            conn = connect(bm, path)
            try:
                for i in range(1000):
                    subject, summary, keywords, detail = FACTS[i % 8]
                    save(bm, conn, (f"{subject} {i}", summary, keywords, f"Synthetic fixture {i}. {detail} Evidence record {i}."))
                raw = [bm.decompress(r[0]) for r in conn.execute("SELECT payload_blob FROM cortex_memory")]
                blobs = [r[0] for r in conn.execute("SELECT payload_blob FROM cortex_memory")]
                if bm is current:
                    details = [r[0] for r in conn.execute("SELECT detail_blob FROM cortex_detail")]
                    raw.extend(bm.decompress(b) for b in details)
                    blobs.extend(details)
                timings = []
                for _ in range(25):
                    started = time.perf_counter()
                    read(bm, conn, "region")
                    timings.append((time.perf_counter() - started) * 1000)
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                storage[label] = dict(records=1000, raw_payload_bytes=sum(map(len, raw)), stored_payload_bytes=sum(map(len, blobs)),
                    database_bytes=path.stat().st_size, index_rows=conn.execute("SELECT count(*) FROM cortex_term").fetchone()[0],
                    query_median_ms=round(statistics.median(timings), 3), query_samples=25,
                    zlib_levels={str(level): sum(1 + min(len(r), len(zlib.compress(r, level))) for r in raw) for level in (1, 6, 9)})
            finally:
                conn.close()

    return dict(kind="implementation_paired_synthetic_evaluation", seed=1408, sqlite=sqlite3.sqlite_version,
        tokenizer_version=importlib.metadata.version("tiktoken"), baseline="Preserved recovered 0.7.1-alpha, not the later pre-audit development hash",
        source_hashes={"baseline": hashlib.sha256(source.read_bytes()).hexdigest(), "development": hashlib.sha256(Path(current.__file__).read_bytes()).hexdigest()},
        instructions={"original_skill": token_counts((original / "SKILL.md").read_text(encoding="utf-8")),
                      "current_skill": token_counts((root / "SKILL.md").read_text(encoding="utf-8"))},
        retrieval=query_results, retrieval_alternatives=retrieval_alternatives, formatting=formatting, budgets=budgets,
        old_prompt_tokens=token_counts(old.render_prompt_packet(results["recovered_0_7_1_alpha"], 100000, False)),
        new_provenance_prompt_tokens=token_counts(render_packet(base, max_tokens=32000, reserve_tokens=0, max_chars=100000)["text"]),
        session=repeated_cost, summary_decoding=decoding, storage=storage,
        limits=["Synthetic engineering measurements, not a population accuracy estimate or real-task outcome.",
                "No model calls, account billing measurements, consolidation costs or actual transport envelope measurements.",
                "Public tokenizer encodings are not verified as the active model tokenizer.",
                "Warm timings exclude startup, encryption, broker, tokenisation and model latency.",
                "Shared formatting comparison preserves the same eight facts and metadata; old prompt comparison does not.",
                "FTS5 was evaluated only in volatile synthetic storage; neither aliases nor FTS were enabled in the vault."])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = run()
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items() if k not in {"budgets", "retrieval", "retrieval_alternatives"}}, indent=2))
