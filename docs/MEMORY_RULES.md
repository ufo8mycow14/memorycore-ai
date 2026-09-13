# Memory rules and what I adapted from Codex

I took inspiration from Codex’s documented approach to local memory: wait for work to become idle, protect quota, separate using memories from generating them, and keep required instructions in authoritative project files. I adapted those ideas to MemoryCore’s explicit scopes, evidence review, durable vault and compact retrieval.

This page compares documented policy with the published implementation. I use “adapted” to describe behavioural design choices; it is not a claim that Codex’s memory engine is bundled in this repository. Actual vendored code and its licence are identified in [third-party notices](../THIRD_PARTY_NOTICES.md).

## The 24-hour rule, step by step

I evaluate elapsed time since the host-observed `last_activity_at`. The native threshold is `24 * 60 * 60` seconds; the Python policy has the same default. I do not run a daily calendar job or infer that closing a window establishes an idle timestamp.

| Event in an illustrative local timeline | Result |
| --- | --- |
| Last activity Monday 10:00 | The earliest idle eligibility is Tuesday 10:00 |
| More activity Monday 18:00 | Eligibility moves to Tuesday 18:00 |
| A turn is still active Tuesday 18:00 | Generation is rejected even if the elapsed-time threshold is met |
| Idle threshold is met but a quota window has 24% remaining | This background pass is skipped |
| Idle threshold is met, all supplied applicable windows have at least 25%, and observations are current | The gate can return `eligible`; the host may proceed with a proposal pass |

The host must recheck activity at dispatch and provide truthful state. A model-controlled string saying “the chat is idle” is not trusted lifecycle telemetry.

The current repository supplies the gate and deterministic source-proposal path. It does not automatically observe all native chats, schedule a pass exactly when the clock reaches 24 hours, or install a live quota connector.

Source: [`generation::evaluate`](../rust-broker/src/native/generation.rs), [`ChatMemoryPolicy`](../scripts/chat_memory.py), [`generation_decision` and `background_propose`](../scripts/generation_quota.py).

## Other conditions the gate checks

I require generation to be enabled, `active=false`, valid observation fields, and an eligible idle duration. The chat observation and quota observation must each be no more than 60 seconds old and must not be future-dated. Missing, malformed or stale observations skip the pass.

Quota is represented as remaining percentages by window. The host supplies every applicable window; the gate takes the minimum and requires at least 25%. Exactly 25% is eligible. An omitted window is not automatically discovered by the gate, so the trusted host is responsible for a complete observation.

When `disable_on_external_context` is enabled, a chat observed to have used external context is excluded from background proposal generation. That optional choice does not itself disable ordinary recall.

I return explicit reasons such as `chat_active`, `chat_not_idle_long_enough`, `chat_state_stale`, `quota_unavailable`, `quota_stale`, `quota_below_threshold` and `external_context_excluded`. Eligibility is separate from successful extraction or durable acceptance.

## What remains available during the wait

I do not apply this idle gate to existing memory recall, explicit manual proposals/saves, reviewed acceptance or maintenance. Their own session permissions, source checks, confirmation and review requirements still apply. Disabling generation is a separate control and can block proposal/acceptance even when time and quota would otherwise allow them.

Local semantic indexing and reranking are also separate from this background-generation quota policy. They have their own CPU, memory and queue controls. The current quota-gated proposal path uses deterministic extraction; I do not claim that it is an installed paid model-based background writer.

Source: [native dispatch](../rust-broker/src/native/service.rs), [reference quota wrapper](../scripts/generation_quota.py), [semantic host](../scripts/memory_host.py).

## Three clocks that must not be confused

| Clock | Purpose | Starting point |
| --- | --- | --- |
| 24-hour background idle threshold | Avoid extracting from work that is still active | Last observed chat activity |
| Default 24-hour staging/proposal lifetime | Limit how long unaccepted material remains usable | Stage/proposal creation and its explicit expiry |
| Default 365-day native archive retention | Retain archived chat/project memory until expiry or an earlier manual action | Recorded archive lifecycle state/time |

The background wait does not extend a staging record’s expiry. Expired staged material is not eligible simply because a chat is now idle. I require a future trusted capture host to commit completed content to durable encrypted staging before acknowledging capture; RAM must not be the only retained copy during the wait. The capture and retention design must preserve required source evidence without silently extending an expired staging record.

Stage expiry, archive cleanup and actual byte disposal depend on the relevant maintenance path. I do not equate an elapsed timestamp with verified physical erasure from all backups. See [storage and lifecycle](STORAGE_AND_LIFECYCLE.md).

## Codex defaults and my adaptations

I checked these public defaults on **13 September 2026**. OpenAI can change them; the linked current documentation is the authority for Codex, while the linked code is the authority for this MemoryCore version.

| Codex reference | Documented setting/behaviour | What I retained or changed |
| --- | --- | --- |
| Idle generation | `memories.min_rollout_idle_hours`: default 6, clamped to 1–48 | I chose 24 hours in the native gate and as the Python default. I did not change Codex’s global setting. |
| Available quota | `memories.min_rate_limit_remaining_percent`: default 25, clamped to 0–100 | I retained 25% and added explicit 60-second freshness checks for trusted chat/quota observations. |
| Use existing memories | `memories.use_memories`: default true | I apply a host-session use control to MemoryCore’s recall/evidence surface. |
| Generate future memories | `memories.generate_memories`: default true | I gate MemoryCore proposals/acceptance separately from use; reviewed forgetting and rejection remain available. |
| External context | `memories.disable_on_external_context`: default false | I offer an optional background-proposal exclusion using host-supplied external-context state. |
| Extraction and consolidation | Separate `memories.extract_model` and `memories.consolidation_model` settings | I use evidence-preserving proposals followed by reviewed acceptance, not those Codex model stages. Local ONNX models serve search/reranking. |
| Required guidance | Mandatory rules belong in `AGENTS.md` or checked-in documentation | I preserve that separation and recheck source evidence instead of treating recalled text as instructions. |
| Secret handling | Codex documents redaction of generated memory fields | I add prohibited-data checks and an optional labelled redacted source projection before persistence; neither is a universal detector. |
| Memory housekeeping | `memories.max_rollout_age_days` and `memories.max_unused_days` each default to 30 for their documented eligibility purposes | I use separate record expiry, archive retention and lifecycle/tombstone policies. The 365-day archive default is not a replacement value for either Codex setting. |

I additionally implemented native session/scope validation, SQLite/SQLCipher storage, source hashes, review digests, versioned corrections, exact-text preservation, TurboVec indexing, bounded graph retrieval, compact packets and retry/deletion safeguards. These mechanisms have their own [architecture](ARCHITECTURE.md) and [retrieval](RETRIEVAL.md) documentation.

## ChatGPT, Codex and MemoryCore ownership

OpenAI’s memory guide distinguishes ChatGPT web memory from local Codex memory. I treat MemoryCore as another explicitly configured provider. Its archive/delete operations govern its own vault, indexes and derived records; they do not delete ChatGPT account memory, Codex platform history or unrelated client state.

I do not provide an automatic ChatGPT-to-Codex memory sync. A provider connection and a client’s internal memory controls are separate concerns.

## References and executable checks

- [OpenAI: Memories](https://learn.chatgpt.com/docs/customization/memories)
- [OpenAI: Configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference)
- [Native eligibility implementation and tests](../rust-broker/src/native/generation.rs)
- [Python quota tests](../scripts/test_generation_quota.py)
- [Chat policy and redaction tests](../scripts/test_chat_memory.py)
- [Native archive and lifecycle implementation](../rust-broker/src/native/retention.rs)
- [Source review and acceptance](../rust-broker/src/native/knowledge.rs)
