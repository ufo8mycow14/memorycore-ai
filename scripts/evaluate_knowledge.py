"""Paired synthetic retrieval/evidence and reference-token accounting.

No model is called. Evidence sufficiency is not generated-answer accuracy.
Aliases are fixed before queries; results include an intentional ambiguity case.
"""
import argparse
import hashlib
import json
import sqlite3
import statistics
import tempfile
import time
from pathlib import Path

from . import memorycore_ai as bm
from .knowledge_layer import Knowledge, SourceRoot, canonical
from .memory_packets import token_counter
from .code_context import GraphifyFile, context_packet
from .memory_mcp_lab import TOOLS

INSTRUCTION = "Use only supplied evidence. Preserve exceptions. Do not use stale facts; abstain when the answer is unsupported."
ALIASES = [["colour", "color", "hue"], ["location", "region", "town"],
           ["prune", "cleanup"], ["deploy", "release"]]
FIXTURES = [
    ("retry", "Decision: Retry policy\nRetry transient failures three times. Never retry invalid input."),
    ("palette", "Decision: Palette\nThe colour is blue. Do not use red for success."),
    ("location", "Decision: Deployment location\nThe deployment location is Adelaide. The backup location is Sydney."),
    ("cleanup", "Procedure: Prune policy\nPrune temporary build files after seven days. Keep pinned records."),
    ("deploy", "Procedure: Deploy procedure\nDeploy only after tests pass. Approval does not waive failed tests."),
    ("lock", "Procedure: Lock release\nRelease the lock after work, including failure paths."),
    ("retention", "Fact: Retention rule\nEpisodes expire after ninety days. Pinning does not extend expiry."),
    ("rollback", "Procedure: Rollback\nRestore the verified backup. Do not merge later writes implicitly."),
]
QUESTIONS = [
    ("retry policy", "retry", ["three times", "Never retry invalid input"], ["retry"]),
    ("hue", "palette", ["blue", "Do not use red"], ["palette"]),
    ("town", "location", ["Adelaide", "Sydney"], ["location"]),
    ("cleanup", "cleanup", ["seven days", "Keep pinned"], ["cleanup"]),
    ("deploy", "deploy", ["tests pass", "does not waive"], ["deploy"]),
    ("release", "lock", ["including failure paths"], ["lock"]),
    ("retention", "retention", ["ninety days", "does not extend"], ["retention"]),
    ("rollback", "rollback", ["verified backup", "Do not merge"], ["rollback"]),
    ("invalid input", "retry", ["Never retry invalid input"], ["retry"]),
]


def protocol_cost(count, question, operation, arguments, result):
    """Two input requests plus one tool-call output; final model answer unknown."""
    initial = [{"role": "system", "content": INSTRUCTION}, {"role": "user", "content": question}]
    call = {"role": "assistant", "tool_call": {"name": operation, "arguments": arguments}}
    receipt = {"role": "tool", "content": result}
    catalogue = ([{"name": "read_file", "description": "Read an explicitly selected source file.",
                   "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}]
                 if operation in {"direct_source", "read_file"} else
                 [{"name": name, "description": description, "inputSchema": contract} for name, (description, contract) in TOOLS.items()])
    first = count(canonical({"messages": initial, "tools": catalogue}))
    second = count(canonical({"messages": initial + [call, receipt], "tools": catalogue}))
    tool_output = count(canonical(call))
    return {"first_input": first, "second_input": second, "tool_call_output": tool_output, "catalogue_reference_tokens_per_request": count(canonical(catalogue)),
            "total_reference_tokens": first + second + tool_output}


def run():
    count = token_counter()
    arms = ["direct_source", "core_lexical", "fresh_lexical", "aliases", "hybrid"]
    rows, setup = [], {arm: 0 for arm in arms}
    with tempfile.TemporaryDirectory(prefix="brain-evaluation-synthetic-") as temp:
        root = Path(temp)
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            bm.initialize(conn)
            k = Knowledge(conn, scope="synthetic:evaluation", sources=SourceRoot(root), synthetic=True, create=True)
            mids = {}
            for name, text in FIXTURES:
                path = name + ".md"
                # Equal harmless surrounding material available to all arms.
                text += "\n" + "Synthetic supporting context; this line supplies no additional decision.\n" * 8
                (root / path).write_text(text, encoding="utf-8")
                proposed = k.propose(path)
                p = proposed["proposals"][0]
                accepted = k.accept(p["id"], p["review_digest"])
                mids[name] = accepted["memory_id"]
                cost = protocol_cost(count, "Review synthetic evidence and store the approved claim.", "propose", {"path": path}, proposed)["total_reference_tokens"]
                cost += count(text)  # Include source material used in extraction.
                cost += protocol_cost(count, "Accept reviewed proposal.", "accept", {"id": p["id"], "review_digest": p["review_digest"]}, accepted)["total_reference_tokens"]
                for arm in arms[1:]:
                    setup[arm] += cost
            k.aliases(ALIASES, reviewed=True)
            for arm in ("aliases", "hybrid"):
                setup[arm] += count(canonical(ALIASES))

            def evaluate(case, query, path, required, expected, stale_phrase=None):
                for arm in arms:
                    elapsed = []
                    for _ in range(3):
                        start = time.perf_counter()
                        if arm == "direct_source":
                            text = k.sources.read(path + ".md").decode()
                            result = {"path": path + ".md", "text": text} if count(text) <= 1600 else {"omitted": True}
                            subjects = [path] if "text" in result else []
                        elif arm == "core_lexical":
                            a = argparse.Namespace(query=query, scope=k.scope, type=None, limit=3, include_detail=False)
                            result = bm.recall(conn, a, _allow_extensions=True)
                            subjects = [next((name for name, mid in mids.items() if mid == m["memory_id"]), "other") for m in result["memories"]]
                            # Enforce the same evidence-response budget without truncating a fact.
                            while result["memories"] and count(canonical(result)) > 1600:
                                result["memories"].pop()
                                subjects.pop()
                        else:
                            result = k.recall(query, mode="lexical" if arm == "fresh_lexical" else arm, limit=3, max_tokens=1600)
                            subjects = [next((name for name, mid in mids.items() if mid == m["id"]), "other") for m in result["memories"]]
                        elapsed.append((time.perf_counter()-start)*1000)
                    text = canonical(result)
                    answer_evidence = all(fragment in text for fragment in required)
                    precision = len(set(subjects) & set(expected)) / len(set(subjects)) if subjects else None
                    cost = protocol_cost(count, query + "; relevant source: " + path + ".md", arm, {"query": query, "scope": k.scope}, result)
                    rows.append({"case": case, "arm": arm, "required_evidence_present": answer_evidence,
                                 "retrieved_topics": subjects, "expected_topics": expected, "topic_precision": precision,
                                 "stale_fact_returned": bool(stale_phrase and stale_phrase in text),
                                 "response_reference_tokens": count(text), "median_retrieval_ms": round(statistics.median(elapsed), 4), **cost})

            for n, (query, path, required, expected) in enumerate(QUESTIONS):
                evaluate(f"initial_{n+1}", query, path, required, expected)
            (root / "retry.md").write_text("Decision: Retry policy\nRetry transient failures two times. Never retry invalid input.", encoding="utf-8")
            evaluate("changed_source", "retry policy", "retry", ["two times", "Never retry invalid input"], ["retry"], "three times")
            p = k.propose("retry.md")["proposals"][0]
            corrected = k.accept(p["id"], p["review_digest"], supersedes=mids["retry"])
            mids["retry"] = corrected["memory_id"]
            maintenance = protocol_cost(count, "Review and accept corrected retry policy.", "correct", p, corrected)["total_reference_tokens"]
            evaluate("reviewed_correction", "retry policy", "retry", ["two times", "Never retry invalid input"], ["retry"], "three times")

            (root / "code.py").write_text("def retry():\n    return 3\n\ndef caller():\n    return retry()\n", encoding="utf-8")
            graph = {"nodes": [{"id": n, "label": n, "source_file": "code.py"} for n in ("retry", "caller")],
                     "links": [{"source": "caller", "target": "retry", "relation": "calls", "confidence": "EXTRACTED", "source_file": "code.py"}]}
            (root / "graph.json").write_text(canonical(graph), encoding="utf-8")
            provider = GraphifyFile(k.sources, "graph.json", {"code.py": hashlib.sha256((root / "code.py").read_bytes()).hexdigest()})
            graph_result = context_packet(provider, "retry", max_tokens=1600)
            code_comparison = {"fixture": "synthetic Graphify-format graph; graph construction not benchmarked",
                               "graph_evidence_contains_caller": "caller" in canonical(graph_result),
                               "graph_protocol": protocol_cost(count, "Who calls retry?", "code_context", {"symbol": "retry"}, graph_result),
                               "source_protocol": protocol_cost(count, "Who calls retry?", "read_file", {"path": "code.py"}, k.sources.read("code.py").decode())}
        finally:
            conn.close()
    summary = {}
    for arm in arms:
        chosen = [r for r in rows if r["arm"] == arm]
        summary[arm] = {"cases": len(chosen), "evidence_sufficient": sum(r["required_evidence_present"] for r in chosen),
                        "stale_returns": sum(r["stale_fact_returned"] for r in chosen),
                        "mean_topic_precision_when_nonempty": statistics.mean(r["topic_precision"] for r in chosen if r["topic_precision"] is not None),
                        "setup_reference_tokens": setup[arm],
                        "maintenance_reference_tokens": 0 if arm == "direct_source" else maintenance,
                        "query_reference_tokens": sum(r["total_reference_tokens"] for r in chosen),
                        "median_retrieval_ms": statistics.median(r["median_retrieval_ms"] for r in chosen)}
        summary[arm]["total_reference_tokens"] = sum(summary[arm][key] for key in ("setup_reference_tokens", "maintenance_reference_tokens", "query_reference_tokens"))
    return {"format": "brain-knowledge-evaluation/1", "synthetic_only": True, "rows": rows, "summary": summary,
            "aliases": ALIASES, "code_context": code_comparison, "actual_provider_input_tokens": None,
            "actual_provider_output_tokens": None, "generated_answer_accuracy": None,
            "quality_metric": "required evidence availability and topic precision, not model answer accuracy",
            "token_accounting": "o200k reference replay of two model inputs and a tool-call output, including the full lab tool catalogue for memory arms, extraction sources, proposal review, correction and instructions; final generated answers unavailable. Core lexical uses the same hypothetical catalogue for backend comparison. Direct reading receives the relevant file path in the task.",
            "latency_boundary": "warm retrieval only; excludes model, process startup, encryption and network",
            "hybrid_method": "lexical plus cosine over reviewed concept groups; no trained embedding model",
            "decision": "Keep lexical as default; offer reviewed aliases and concept hybrid experimentally. Tiny synthetic results do not justify embeddings or account-savings claims."}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--output", required=True)
    args = p.parse_args()
    result = run()
    with Path(args.output).open("x", encoding="utf-8") as stream:
        json.dump(result, stream, indent=2)
        stream.write("\n")
    print(canonical({"summary": result["summary"], "decision": result["decision"]}))


if __name__ == "__main__":
    main()
