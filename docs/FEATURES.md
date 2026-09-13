# Feature and implementation map

I distinguish implemented development features from integration work and future goals. All current use remains synthetic-only.

| Area | Implemented surface | Source |
| --- | --- | --- |
| Core memory | Stage, remember, consolidate, recall, inspect, list, corrections, pins, expiry, archive, restore, prune, purge, import/export | `scripts/memorycore_ai.py` |
| Compact packets | Token and character budgets; provenance and whole-fact rendering; explicit omissions | `scripts/memory_packets.py` |
| Knowledge | Source-bound proposals, reviewed acceptance, freshness, aliases, relationship metadata | `scripts/knowledge_layer.py` |
| Native authority | Host-selected sessions/scopes, native transactions, read workers, one coordinated writer, bounded transport | `rust-broker/src/` |
| Encrypted storage | SQLCipher feature; key-required configuration; encrypted backup and reopen checks | `rust-broker/Cargo.toml`, `scripts/test_rust_encryption.py` |
| Semantic retrieval | Local embeddings/reranking, verified model assets, scope-aware caches, index catch-up and lexical fallback | `scripts/memory_host.py`, `scripts/vector_pipeline.py`, `scripts/model_assets.py` |
| Graph expansion | Reviewed edges, depth/intent bounds, source and lifecycle eligibility | `scripts/graph_memory.py`, native graph code in `rust-broker/src/` |
| MCP | Fixed-session stdio adapter; clients cannot select arbitrary sessions or invoke host administration | `scripts/native_mcp.py` |
| Monitoring | Operation counts, timing, packet/token estimates and resource state without query/source/recall payloads | `scripts/monitored_native_mcp.py` |
| Chat lifecycle | Versioned host-supplied events; archive/unknown exclusion; deletion propagation and tombstones | `scripts/chat_memory.py`, `scripts/test_native_chat_lifecycle.py` |
| Background policy | Separate use/generate controls, fresh quota observations and a 24-hour idle gate for proposal generation | `scripts/generation_quota.py`, `scripts/memory_policy.py` |
| Continuity | Nine-category checkpoints, source verification, cost forecasts and a durable handoff outbox | `scripts/session_routing.py`, `scripts/fresh_context.py` |
| Code context | Local Graphify data and bounded GitNexus/Serena provider calls | `scripts/code_context.py`, `scripts/mcp_peer.py` |
| Resource controls | Bounded queues/caches, foreground read/write admission and background indexing controls | `scripts/resource_budget.py`, `scripts/resource_limits.py`, `scripts/memory_host.py` |

## Boundaries I preserve

- A source hash proves that content matches reviewed bytes; it does not prove that a claim is true.
- Scope labels in the reference CLI are filters. A trusted native host must establish authority for untrusted callers.
- The native SQLCipher feature protects database storage, not original documents, authorised plaintext output or all process memory.
- Archive and logical deletion do not erase historical backups. Current snapshots require fresh lifecycle evidence for linked chat data before reuse.
- The staging lifetime and 24-hour generation delay are distinct policies. The delay must not imply that completed turns are safe to leave only in RAM.
- Background policy helpers do not supply a live quota connector, scheduler or native chat event feed.
- The semantic host requires explicitly provisioned models. Recall does not silently download them.
- The Python `capabilities` output describes the reference backend. Its missing-trained-embedding entry does not describe the separate native semantic host.

## Work I have not completed

I have not shipped production identity/key custody, hosted multi-user access, automatic private chat ingestion, cross-device synchronisation, automatic desktop dispatch, contact management, or universal client compatibility. I also do not claim independent security certification, a production latency guarantee or universal token savings.
