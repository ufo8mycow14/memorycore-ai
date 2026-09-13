"""Paired physical-layout benchmark; all databases contain disposable synthetic data."""

import argparse
import hashlib
import json
import random
import sqlite3
import statistics
import tempfile
import time
import uuid
from pathlib import Path
from unittest.mock import patch

from scripts import memorycore_ai as bm
from scripts.evaluate_synthetic import FACTS, SCOPE, STAMP, ns, read, save


DETAIL_DDL = """
CREATE TABLE IF NOT EXISTS cortex_detail (
    memory_id BLOB PRIMARY KEY REFERENCES cortex_memory(memory_id) ON DELETE CASCADE,
    detail_blob BLOB NOT NULL, checksum_sha256 BLOB NOT NULL
) WITHOUT ROWID;
"""
TOMBSTONE_DDL = """
CREATE TABLE IF NOT EXISTS cortex_tombstone (
    memory_id BLOB PRIMARY KEY, scope TEXT NOT NULL, removed_at TEXT NOT NULL
) WITHOUT ROWID;
"""
ROWID_TOMBSTONE_DDL = """
CREATE TABLE IF NOT EXISTS cortex_tombstone (
    memory_id BLOB PRIMARY KEY, scope TEXT NOT NULL, removed_at TEXT NOT NULL
);
"""


def measure(schema, fixture, records, repetition, *, scoped_index=False):
    identity_rng = random.Random(1408)
    text_rng = random.Random(1841)
    with tempfile.TemporaryDirectory(prefix="memorycore-ai-layout-") as folder:
        path = Path(folder) / "synthetic.sqlite3"
        conn = bm.connect(str(path))
        try:
            with patch.object(bm, "SCHEMA", schema), patch.object(bm, "now_utc", return_value=STAMP), \
                 patch.object(bm.uuid, "uuid4", side_effect=lambda: uuid.UUID(int=identity_rng.getrandbits(128))):
                bm.initialize(conn)
                conn.execute("DROP INDEX IF EXISTS cortex_scope_status")
                conn.execute("DROP INDEX IF EXISTS cortex_type_status")
                if scoped_index:
                    conn.execute("CREATE INDEX cortex_scope_status ON cortex_memory(scope,status,memory_type)")
                else:
                    conn.execute("CREATE INDEX cortex_scope_status ON cortex_memory(scope,status)")
                    conn.execute("CREATE INDEX cortex_type_status ON cortex_memory(memory_type,status)")
                conn.commit()
                started = time.perf_counter()
                for i in range(records):
                    subject, summary, keywords, detail = FACTS[i % len(FACTS)]
                    if fixture == "summary_only":
                        detail = ""
                    elif fixture == "small_detail":
                        detail = f"Synthetic fixture {i}: {detail}"
                    else:
                        detail = "Synthetic evidence " + "".join(text_rng.choice("abcdefghijklmnopqrstuvwxyz ") for _ in range(4096))
                    save(bm, conn, (f"{subject} {i}", summary, keywords, detail))
                insert_ms = (time.perf_counter() - started) * 1000
                expected = bm.export_scope(conn, SCOPE)["sha256"]
                recall_ms = []
                for _ in range(15):
                    start = time.perf_counter()
                    result = read(bm, conn, "region")
                    recall_ms.append((time.perf_counter() - start) * 1000)
                assert result["count"] == 8
                # Exercise deletion and exact portable restoration, not just file sizes.
                identity = result["memories"][0]["memory_id"]
                package = bm.export_scope(conn, SCOPE)
                restored = sqlite3.connect(":memory:")
                restored.row_factory = sqlite3.Row
                restored.execute("PRAGMA foreign_keys=ON")
                try:
                    bm.initialize(restored)
                    bm.import_scope(restored, package, SCOPE, user_confirmed=True)
                    assert bm.export_scope(restored, SCOPE)["sha256"] == expected
                    bm.purge(restored, ns(memory_id=identity, scope=SCOPE, user_confirmed=True))
                    assert restored.execute("SELECT count(*) FROM cortex_detail WHERE memory_id=?", (bytes.fromhex(identity),)).fetchone()[0] == 0
                    assert not restored.execute("PRAGMA foreign_key_check").fetchone()
                finally:
                    restored.close()
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                try:
                    allocation = {row[0]: row[1] for row in conn.execute("SELECT name,sum(pgsize) FROM dbstat GROUP BY name")}
                except sqlite3.OperationalError:
                    allocation = None
                return dict(fixture=fixture, repetition=repetition, records=records, database_bytes=path.stat().st_size,
                            export_sha256=expected, insert_ms=round(insert_ms, 3), recall_median_ms=round(statistics.median(recall_ms), 3),
                            allocation=allocation, integrity_check=conn.execute("PRAGMA integrity_check").fetchone()[0],
                            round_trip_and_purge=True)
        finally:
            conn.close()


def run(records=500, repetitions=3, *, indexed=False, baseline_report=None, all_variants=False):
    # Preserve the pre-pilot physical baseline even after new-database defaults change.
    original = ROWID_TOMBSTONE_DDL + bm.SCHEMA
    variants = {"baseline": original, "detail_without_rowid": DETAIL_DDL + original,
                "detail_and_tombstone_without_rowid": DETAIL_DDL + TOMBSTONE_DDL + original}
    if indexed:
        variants = {"scoped_composite_index": original, "scoped_index_compact_tombstones": TOMBSTONE_DDL + original}
    if all_variants:
        variants.update({"scoped_composite_index": original, "scoped_index_compact_tombstones": TOMBSTONE_DDL + original})
    baseline = json.loads(baseline_report.read_text(encoding="utf-8")) if baseline_report else None
    if baseline:
        assert baseline["source_sha256"] == hashlib.sha256(Path(bm.__file__).read_bytes()).hexdigest()
        assert (baseline["records"], baseline["repetitions"]) == (records, repetitions)
    results = []
    for fixture in ("summary_only", "small_detail", "large_detail"):
        for repetition in range(repetitions):
            reference = next((r["export_sha256"] for r in baseline["results"] if r["fixture"] == fixture and r["repetition"] == repetition and r["variant"] == "baseline"), None) if baseline else None
            for name, schema in variants.items():
                result = measure(schema, fixture, records, repetition, scoped_index=name.startswith("scoped_"))
                reference = reference or result["export_sha256"]
                assert result["export_sha256"] == reference
                result["variant"] = name
                results.append(result)
                print(json.dumps({k: result[k] for k in ("fixture", "repetition", "variant", "database_bytes")}), flush=True)
    return dict(kind="paired_physical_storage_experiment", source_sha256=hashlib.sha256(Path(bm.__file__).read_bytes()).hexdigest(),
                sqlite=sqlite3.sqlite_version, records=records, repetitions=repetitions, results=results,
                actual_model_tokens=None, limits=["Synthetic data only; model/token outcomes not measured.",
                    "Same IDs, inputs and logical exports across variants; elapsed timings are noisy local measurements.",
                    "WITHOUT ROWID may worsen allocation for large blobs; no candidate is automatically promoted."])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--records", type=int, default=500)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--indexed", action="store_true")
    parser.add_argument("--baseline-report", type=Path)
    parser.add_argument("--all-variants", action="store_true")
    args = parser.parse_args()
    result = run(args.records, args.repetitions, indexed=args.indexed, baseline_report=args.baseline_report, all_variants=args.all_variants)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
