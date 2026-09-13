# MemoryCore Ai - For Codex

[![MemoryCore AI architecture for Codex: an assistant sends a question to MemoryCore, which uses local embedding and reranking models, a TurboVec four-bit vector index and a SQLite or SQLCipher vault to return compact context with relevant facts and provenance.](docs/assets/memorycore-architecture.png)](docs/ARCHITECTURE.md)

**Local persistent memory for Codex. Relevant context. Less repeated work.**

[![Development](https://img.shields.io/badge/stage-working%20development%20build-orange)](#development-stage)
[![CI](https://github.com/ufo8mycow14/memorycore-ai-for-codex/actions/workflows/ci.yml/badge.svg)](https://github.com/ufo8mycow14/memorycore-ai-for-codex/actions/workflows/ci.yml)
[![Licence: Apache 2.0](https://img.shields.io/badge/licence-Apache%202.0-blue)](LICENSE)

I’m building **MemoryCore Ai - For Codex**, a **local persistent memory system for OpenAI Codex** with a **Model Context Protocol (MCP)** adapter. My goal is better memory for Codex project work and less token wastage from repeatedly loading history, rediscovering decisions and carrying irrelevant context into the next task.

My aim is to remember the right information, retain its source and qualifications, and retrieve only what the current question needs. Memory is useful when it helps work continue accurately; a larger archive alone does not achieve that.

## Why persistent memory for Codex?

I designed MemoryCore around longer-running Codex projects, where decisions, constraints and unfinished work need to remain useful across sessions. Instead of relying on repeated conversation replay, I store reviewed facts with their sources and retrieve a compact packet for the current question.

My approach combines **Codex memory through MCP**, **semantic search with TurboVec**, source freshness checks and explicit retention controls. I keep raw storage compression separate from **token efficiency**: the important result is less repeated context without losing the evidence needed to work accurately.

I maintain this as an independent project for Codex workflows. The implementation and integration limits below describe what is actually available.

## The 24-hour memory rule

I wait until a chat has been **inactive for at least 24 hours** before it becomes eligible for background memory extraction and proposal generation. This gives work time to settle before I turn it into durable memory.

The clock runs from the **last activity**, so returning to the chat moves the earliest eligible time forward. If the last activity was Monday at 10:00, the earliest eligibility is Tuesday at 10:00. Another message on Monday at 18:00 moves that to Tuesday at 18:00. This is an idle threshold, not a daily scheduled run.

Before a background pass can proceed, I also require:

- Memory generation enabled for the session and no active chat turn.
- Trusted chat and quota observations no older than **60 seconds**.
- At least **25% quota remaining in every applicable window** supplied by the host; exactly 25% passes.
- No disqualifying external context when that optional policy is enabled.

Existing memory recall and authorised manual saves do not have to wait 24 hours. The delay applies to background extraction/proposals, not permission to keep the only copy of completed work in RAM. Durable staging, source checks, review and session permissions remain separate requirements.

The current build implements and tests this eligibility gate. A trusted host must invoke it and supply current observations; I do not claim an installed automatic chat-capture feed or a timer that guarantees generation at the 24-hour mark. I explain the separate staging-expiry and archive-retention clocks in [memory rules and Codex influences](docs/MEMORY_RULES.md).

## What I adapted from Codex—and what I changed

I drew on Codex’s documented memory policies and adapted them for MemoryCore’s scoped vault, review process and token-efficiency goals.

| Policy or design idea | Codex’s documented behaviour | My MemoryCore adaptation |
| --- | --- | --- |
| Wait for idle work | `memories.min_rollout_idle_hours` defaults to **6 hours**, configurable from 1–48 | **24 hours** in the native gate and the Python policy default; a fresh host observation is required |
| Protect available quota | `memories.min_rate_limit_remaining_percent` defaults to **25%** | Keep the 25% threshold, require every applicable window from the trusted host, and reject stale/missing observations |
| Separate reading from generation | `memories.use_memories` and `memories.generate_memories` are independent controls | Host-selected session controls gate recall and generation separately; reviewed forgetting remains available |
| Exclude external context when selected | Optional `memories.disable_on_external_context`, default false | An optional gate for background proposals after tool/web context; it does not switch off ordinary recall |
| Extract and consolidate separately | Codex documents per-chat extraction and global consolidation model settings | Supported source blocks become proposals, then explicit digest-bound review/acceptance creates durable records |
| Keep required rules authoritative | Codex recommends `AGENTS.md` or checked-in guidance for mandatory rules | Source-checked recall supplements project instructions; memory does not become the only authority |

I additionally use a **SQLite/SQLCipher vault**, source-hash freshness checks, versioned corrections, **TurboVec semantic indexing**, compact token-budgeted packets, deletion tombstones and a **365-day native archive-retention default**. Archive retention is a different policy from Codex’s age/unused-memory consolidation settings.

I credit behavioural inspiration separately from bundled third-party code. The detailed [policy comparison](docs/MEMORY_RULES.md) links implementation files and explains the differences. Codex defaults above were checked against OpenAI’s [memory guide](https://learn.chatgpt.com/docs/customization/memories) and [configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference) on 13 September 2026.

## Development stage

I have a working development implementation, with a Rust storage broker, a Python semantic host, a Python reference implementation and synthetic regression tests. The native broker is **0.5.0-dev** and the Python/reference interfaces are **0.10.0-dev**. These are separate component versions.

I’m inviting help with testing, portability, retrieval quality, documentation and integration. I currently limit this build to **synthetic data**. It is not a production release, a hosted service or a turnkey replacement for a client’s internal memory.

## What MemoryCore adds to a Codex workflow

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

## How MemoryCore works with Codex

I use **Rust/Tokio** for the native broker, **SQLite/SQLCipher** for durable storage, **local ONNX models** for embeddings and reranking, and **TurboVec 1.0.0** for the four-bit vector search index. TurboVec remains a mandatory native dependency. It shortlists possible matches; current source, version and lifecycle checks decide which evidence is eligible to return.

I keep the technical details in linked guides with source references:

- [Architecture and component diagram](docs/ARCHITECTURE.md): how a fact moves from reviewed evidence to compact recall, and why I use both Rust and Python.
- [Retrieval, models and TurboVec](docs/RETRIEVAL.md): embeddings, quantisation, lexical/vector search, reranking, cache/index updates and graph expansion.
- [Storage and lifecycle](docs/STORAGE_AND_LIFECYCLE.md): record layout, compression, integrity, corrections, retention, encryption, deletion and recovery.
- [Interfaces and operations](docs/INTERFACES.md): session configuration, raw broker versus MCP requests, review actions, limits and monitoring.
- [Memory rules and Codex influences](docs/MEMORY_RULES.md): the 24-hour idle rule, quota checks, separate memory controls and policy adaptations.

I distinguish compressed storage bytes, compact vector indexes and reduced prompt context. Each has a different cost and must be measured separately.

## Codex MCP integration

I provide a local stdio MCP adapter that exposes one compact `memory` tool to a configured Codex session. The host binds the session to its scope and source root, while the Rust broker checks memory operations. The Python host adds local embeddings and reranking; the monitored adapter records operational counts without storing recalled text in its metrics log.

For a development trial, I start with the [synthetic setup guide](docs/GETTING_STARTED.md), build the native broker, provision reviewed local models, and configure the adapter for a dedicated synthetic project. The [interface guide](docs/INTERFACES.md) shows the session boundary and distinguishes MCP requests from raw broker requests.

The repository includes `scripts/setup_local_rollout.py` for an explicit Windows Codex configuration workflow. I describe its changes in the setup guide before recommending its use. MCP access does not automatically import existing Codex conversations, replace Codex platform history or supply native chat lifecycle events.

## Codex memory, ChatGPT memory and MemoryCore

If you are researching **ChatGPT memory**, **Codex persistent memory**, **AI agent memory** or ways to reduce repeated context, I want the storage and integration boundaries to be clear. OpenAI documents ChatGPT web memory and local Codex memory as [separate stores with separate controls](https://learn.chatgpt.com/docs/customization/memories).

| Memory surface | Where I draw the boundary |
| --- | --- |
| ChatGPT web memory | OpenAI’s ChatGPT memory system and account/workspace controls; this repository does not manage it |
| Local Codex memory | Codex’s own local memory files and controls, as documented by OpenAI |
| MemoryCore for Codex | This project’s separately configured local vault and MCP retrieval interface |

I currently target **Codex through MCP**. I have not implemented a verified ChatGPT connector, automatic ChatGPT conversation import, or memory synchronisation between ChatGPT and Codex. The common goal is useful long-term context; the integration being developed here is explicit, scoped and source-checked.

## How I intend it to be used

I designed this for long-running Codex project work: preserving reviewed decisions between sessions, recalling relevant constraints, checking the evidence behind a remembered fact, and preparing continuity context without repeatedly replaying whole conversations.

For example, a project can retain a reviewed deployment decision and its source. A later session asks for the deployment region and receives a compact, scoped answer. If that source changes, the old fact is excluded until it is reviewed again.

I also want MemoryCore AI to be a useful workbench for comparing memory strategies fairly. Small tasks may be cheaper to handle by reading the source directly. Memory acquisition, review, retrieval and maintenance all have costs.

## Codex memory and token efficiency

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
| OpenAI Codex | Primary intended client, through the local MCP adapter and explicit configuration helper. Automatic chat capture and control over platform history are not implemented. |
| Graphify, GitNexus, Serena | Experimental code-context adapters with contract tests; not blanket certification of live third-party integrations. |
| Windows | Primary development and local validation platform. Linux and macOS support need independent acceptance testing. |

I document setup choices and integration limits in [getting started](docs/GETTING_STARTED.md).

## Try the reference implementation

From a clone of this repository, with Python 3.12+:

```sh
git clone https://github.com/ufo8mycow14/memorycore-ai-for-codex.git
cd memorycore-ai-for-codex
python -m venv .venv
# Activate .venv using the command for your shell.
python -m pip install -r requirements-test.txt
python -B scripts/memorycore_ai.py capabilities
python -B -m unittest discover -s scripts -p "test*.py"
```

I keep the [complete synthetic CLI walkthrough](references/operations.md) executable by a regression test. Token-budgeted recall requires a supported tokenizer encoding; missing tokenizer support fails explicitly. Native and model-dependent tests skip when their capabilities are absent, so a reference-only run is not full-runtime validation.

## Help me improve it

I welcome focused pull requests, reproducible bugs, documentation improvements and careful evaluation. Good starting areas include Codex MCP setup, cross-platform validation, retrieval cases with negation or conflicting evidence, and complete token accounting for Codex workflows.

Please start with [contributing](CONTRIBUTING.md), the [development priorities](docs/ROADMAP.md), and my [security policy](SECURITY.md). I review changes through pull requests; contributing does not require write or administrator access to this repository.

## Licence

I release my original project code under [Apache 2.0](LICENSE). Vendored components retain their own licences; see [third-party notices](THIRD_PARTY_NOTICES.md). Model weights are downloaded separately and remain subject to their respective terms.
