# MemoryCore AI

**Durable memory. Relevant context. Less repeated work.**

[![Development](https://img.shields.io/badge/stage-working%20development%20build-orange)](#development-stage)
[![CI](https://github.com/ufo8mycow14/memorycore-ai/actions/workflows/ci.yml/badge.svg)](https://github.com/ufo8mycow14/memorycore-ai/actions/workflows/ci.yml)
[![Licence: Apache 2.0](https://img.shields.io/badge/licence-Apache%202.0-blue)](LICENSE)

I’m building MemoryCore AI to give assistants useful, durable memory while reducing the tokens wasted on repeatedly loading history, rediscovering decisions and carrying irrelevant context into the next task.

My aim is to remember the right information, retain its source and qualifications, and retrieve only what the current question needs. Memory is useful when it helps work continue accurately; a larger archive alone does not achieve that.

## Development stage

I have a working development implementation, with a Rust storage broker, a Python semantic host, a Python reference implementation and synthetic regression tests. The native broker is **0.5.0-dev** and the Python/reference interfaces are **0.10.0-dev**. These are separate component versions.

I’m inviting help with testing, portability, retrieval quality, documentation and integration. I currently limit this build to **synthetic data**. It is not a production release, a hosted service or a turnkey replacement for a client’s internal memory.

## What I’m building

| Capability | What the current implementation provides |
| --- | --- |
| Durable, scoped memory | SQLite-backed facts, decisions, procedures and continuity records; the native host binds configured sessions to scopes. |
| Economical recall | Token-budgeted packets, a compact `memory` tool catalogue, selective detail and whole-fact omission rather than silently cutting qualifications. |
| Source-aware retrieval | Source hashes, provenance, versioned corrections and freshness checks that exclude changed or unavailable bound evidence. |
| Semantic search | Local ONNX embeddings and reranking through the Python host, with native TurboVec indexing and disclosed lexical fallback. Model provisioning is explicit. |
| Evidence relationships | Reviewed support, dependency, applicability and contradiction links; bounded graph expansion when relationships matter. |
| Retention and deletion | Staging, expiry, archive/unarchive, review-bound deletion and tombstones. Native archive retention defaults to 365 days. |
| Exact preservation | Explicitly confirmed supported text formats, integrity checks and byte-range retrieval. |
| Native encryption | Optional SQLCipher build, host-supplied keys and independently keyed encrypted snapshots. Plain SQLite remains a separate synthetic fixture mode. |
| Continuity experiments | Selective checkpoints, route-cost planning and a durable handoff outbox. Automatic desktop message dispatch is not implemented. |
| Operational visibility | Payload-free MCP metrics, bounded queues, read/write admission and resource controls. |

I describe the boundaries and relevant source files in the [feature map](docs/FEATURES.md).

## How I intend it to be used

I designed this for long-running project work: preserving decisions between sessions, recalling relevant constraints, checking the evidence behind a remembered fact, and resuming unfinished work without repeatedly replaying whole conversations.

For example, a project can retain a reviewed deployment decision and its source. A later session asks for the deployment region and receives a compact, scoped answer. If that source changes, the old fact is excluded until it is reviewed again.

I also want MemoryCore AI to be a useful workbench for comparing memory strategies fairly. Small tasks may be cheaper to handle by reading the source directly. Memory acquisition, review, retrieval and maintenance all have costs.

## Reducing token wastage

I target four avoidable costs:

1. Replaying long histories to recover a few useful facts.
2. Sending repeated tool schemas and unnecessary detail.
3. Re-reading unchanged information or carrying unrelated task context.
4. Rework caused by stale, ambiguous or incomplete recall.

I count token reduction as useful only when the answer remains correct and the full workflow costs less. Shorter packets, compressed database bytes and quantised vectors do **not** by themselves prove lower provider usage or bills. I make no universal percentage-saving claim. My [evaluation guide](docs/EVALUATION.md) explains what a meaningful comparison needs to include.

## What it works with

| Surface | Current position |
| --- | --- |
| Python 3.12+ | Reference CLI, tests, semantic host and MCP adapters. |
| Rust | Native broker; Cargo lockfile included. Windows development has used Rust 1.98.0. |
| SQLite / SQLCipher | Local persistence; encryption requires the SQLCipher feature and explicit key configuration. |
| MCP over stdio | Dedicated, host-configured local adapter exposing one compact `memory` tool. Client compatibility still needs validation. |
| Codex | A local configuration helper and MCP adapter exist. I do not claim automatic chat capture or control over platform history. |
| Graphify, GitNexus, Serena | Experimental code-context adapters with contract tests; not blanket certification of live third-party integrations. |
| Windows | Primary development and local validation platform. Linux and macOS support need independent acceptance testing. |

I document setup choices and integration limits in [getting started](docs/GETTING_STARTED.md).

## Try the reference implementation

From a clone of this repository, with Python 3.12+:

```sh
python -m venv .venv
# Activate .venv using the command for your shell.
python -m pip install -r requirements-test.txt
python -B scripts/memorycore_ai.py capabilities
python -B -m unittest discover -s scripts -p "test*.py"
```

I keep the [complete synthetic CLI walkthrough](references/operations.md) executable by a regression test. Token-budgeted recall requires a supported tokenizer encoding; missing tokenizer support fails explicitly. Native and model-dependent tests skip when their capabilities are absent, so a reference-only run is not full-runtime validation.

## Help me improve it

I welcome focused pull requests, reproducible bugs, documentation improvements and careful evaluation. Good starting areas include clean-machine setup, cross-platform validation, retrieval cases with negation or conflicting evidence, and complete token accounting.

Please start with [contributing](CONTRIBUTING.md), the [development priorities](docs/ROADMAP.md), and my [security policy](SECURITY.md). I review changes through pull requests; contributing does not require write or administrator access to this repository.

## Licence

I release my original project code under [Apache 2.0](LICENSE). Vendored components retain their own licences; see [third-party notices](THIRD_PARTY_NOTICES.md). Model weights are downloaded separately and remain subject to their respective terms.
