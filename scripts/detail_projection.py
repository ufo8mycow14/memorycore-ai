"""Bounded selective-detail projection, not a implemented storage-format change."""

import argparse
import hashlib
import json
import random
from pathlib import Path

from scripts import memorycore_ai as bm


def run():
    rng = random.Random(1408)
    records = []
    for length in (0, 64, 128, 256, 512, 4096):
        for i in range(50):
            detail = "".join(rng.choice("abcdefghijklmnopqrstuvwxyz ") for _ in range(length)).strip()
            fields = (f"Synthetic {length}-{i}", "Never publish before explicit approval.", detail, "publication approval", "synthetic")
            combined = bm.encode_payload(*fields)
            summary = bm.encode_payload(fields[0], fields[1], "", fields[3], fields[4])
            split_detail = bm.compress(detail.encode())
            assert tuple(bm.decode_payload(combined).values()) == fields
            records.append(dict(length=length, detail_bytes=len(detail.encode()), combined=combined, summary=summary, split_detail=split_detail))
    cases = []
    for length in (0, 64, 128, 256, 512, 4096):
        selected = [r for r in records if r["length"] == length]
        baseline = sum(len(r["summary"]) + len(r["split_detail"]) for r in selected)
        for threshold in (0, 128, 256, 512):
            inline = [r for r in selected if r["detail_bytes"] <= threshold]
            stored = sum(len(r["combined"]) if r["detail_bytes"] <= threshold else len(r["summary"]) + len(r["split_detail"]) for r in selected)
            cases.append(dict(detail_length=length, threshold=threshold, records=len(selected), baseline_payload_bytes=baseline,
                              projected_payload_bytes=stored, removable_detail_rows=len(inline),
                              added_summary_decode_bytes=sum(r["detail_bytes"] for r in inline)))
    return dict(kind="selective_detail_payload_projection", source_sha256=hashlib.sha256(Path(bm.__file__).read_bytes()).hexdigest(),
                records=len(records), round_trips_verified=True, cases=cases, adopted=False,
                limitations=["Payload projection only; not measured total database size or latency.",
                    "Inline detail increases summary decoding and changes existing reader/import invariants.",
                    "Adoption would require a separately versioned format and migration; current detail integrity rules remain unchanged."])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    result = run()
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"records": result["records"], "cases": len(result["cases"]), "adopted": False}))
