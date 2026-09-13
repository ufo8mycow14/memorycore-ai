# Retrieval, local models and TurboVec

I use several distinct mechanisms to find useful memories. TurboVec is still included: **version 1.0.0 is mandatory in native builds**, including `--no-default-features`. The `vector-search` Cargo feature is only a compatibility alias. The dependency is resolved to the vendored source through `[patch.crates-io]`.

## What TurboVec actually does

I build a `turbovec::IdMapIndex` with four-bit quantisation. It maps vector-search identifiers back to memory IDs and returns a bounded candidate list for a query vector. The native code then uses the stored float32 vectors for exact similarity rescoring and applies memory/source eligibility checks.

TurboVec does not generate embeddings, summarise conversations, encrypt the vault or decide whether a claim is true. Its quantised index is a derived search structure. The durable records and stored vectors remain in SQLite/SQLCipher, and raw fact text is not replaced by four-bit data.

Four bits per quantised coordinate is an eightfold reduction relative to 32 bits **for that representation alone**. I do not apply that ratio to total RAM, database size or prompt tokens: the system also retains float32 vectors, identifiers, mappings, codebooks, build buffers and other state.

Source: [Cargo dependency and patch](../rust-broker/Cargo.toml), [`IndexEntry`, `build_index`, `candidates` and `similarity`](../rust-broker/src/native/vectors.rs), [upstream licence and attribution](../THIRD_PARTY_NOTICES.md).

## The local model pipeline

| Role | Default implementation I use | Result |
| --- | --- | --- |
| Embedding | `bge-int8`, using the reviewed `memorycore-ai/bge-small-en-v1.5-int8` profile based on BGE small English v1.5 | A normalised 384-dimensional vector |
| Candidate search | Native TurboVec index plus the lexical term index | A bounded set of possible memories |
| Reranking | `memorycore-ai/minilm-l6-range7`, derived from `Xenova/ms-marco-MiniLM-L-6-v2` | Query/document relevance scores |
| Final selection | Native version, source, lifecycle, requested-field and packet-budget checks | Eligible evidence in a compact packet |

I pin upstream revisions and asset hashes in [model-assets-lock.json](../scripts/model-assets-lock.json). The default embedding profile uses a reviewed int8 ONNX asset; the default ranking profile uses a pinned conversion recipe. These are distinct from TurboVec’s four-bit index quantisation.

The indexing projection takes at most 4,096 characters from subject, summary and detail, and the embedding model has a 512-token input limit. I retain the original durable text separately; bounded embedding input can miss information, so a semantic match is not proof of complete understanding.

I provision models explicitly before normal recall. Existing missing or tampered assets are not silently replaced with different weights. Loading an already provisioned cache is separate from conversion tooling, whose known ONNX advisories are documented in [the security policy](../SECURITY.md).

Source: [`LocalModel` and `LocalReranker`](../scripts/vector_pipeline.py), [`reviewed_model` and `derive_range7`](../scripts/model_assets.py).

## What happens when I ask a question

1. **Bind the request.** The MCP adapter fixes the session; the host and broker enforce its scope and use/generation controls. The query must satisfy input bounds.
2. **Project the search query.** Conservative query handling can remove an explicit tracking label while retaining the actual question. It preserves ambiguous references and meaningful identifiers. The full original query remains part of cache/selection identities.
3. **Gather candidates.** The semantic shortlist combines up to 64 lexical candidates and up to 64 TurboVec candidates. The separate ordinary lexical recall path has its own 256-candidate bound and explicit capped-result metadata.
4. **Rescore and verify.** Native vector similarity uses stored float32 vectors. The system checks current record identity, status and source freshness; inactive or stale evidence cannot become valid merely because its index score is high.
5. **Shortlist for reranking.** Focused questions use up to eight documents; detected multi-topic questions can use sixteen. Requested-field checks suppress candidates that do not support the requested kind of answer.
6. **Rank the shortlist locally.** The reranker scores query/document pairs. Its logits are not calibrated probabilities. Focused selection can prefer a subject while retaining that subject’s qualifications; compound queries can retain multiple topics.
7. **Revalidate before delivery.** The final native selection rechecks records and sources, so a preview or cached score is not a durable freshness assertion.
8. **Render whole facts.** The selected semantic packet includes at most eight memories and must fit the response budget. The normal compact native tool response has a 1,400-token envelope check, with a smaller internal content budget. Whole facts can be omitted; qualifications are not silently cut to manufacture savings.

I use heuristic similarity/ranking thresholds and a default 250 ms foreground reranker wait. These are implementation choices, not guarantees of answerability or end-to-end latency. If semantic enhancement is unavailable, I expose the fallback state rather than claiming equivalent semantic coverage.

Source: [native candidate and final selection](../rust-broker/src/native/vectors.rs), [lexical and compact recall](../rust-broker/src/native/knowledge.rs), [answerability checks](../rust-broker/src/native/answerability.rs), [query projection](../scripts/query_projection.py), [host orchestration](../scripts/memory_host.py).

## How the index stays current

I queue eligible memory versions for embedding and bind submitted vectors to model identity, dimensions and record checksums. The database retains vector changes; the in-memory index can apply retained changes or rebuild when its history/model is no longer compatible.

Replacement generations are built and published with generation guards. An older generation cannot overwrite a newer one. Build work has a bounded memory reservation; index construction is not free, and the database snapshot is released before the normal host performs expensive quantisation.

A correction, deletion or source change can affect eligibility before every derived structure has caught up. I therefore keep final version, lifecycle and source checks on the retrieval path. Index catch-up is a performance concern; it must not become permission to return deleted or stale evidence.

Source: [native index jobs, deltas and generation publication](../rust-broker/src/native/vectors.rs), [model-cache invalidation](../scripts/memory_host.py), [index telemetry](../scripts/index_telemetry.py).

## When graph retrieval helps

After ordinary recall, I can explicitly expand a remembered fact’s reviewed relationships: `supported_by`, `applies_to`, `contradicts` and `depends_on`. Graph traversal is bounded to depth one through three and produces its own token-budgeted evidence packet.

I use graph expansion for evidence, dependencies, impact or conflicts when those connections matter. It does not automatically invent relationships or run on every factual recall. Stale/inactive endpoints remain subject to eligibility checks.

Source: [native graph retrieval](../rust-broker/src/native/graph.rs), [reference graph implementation](../scripts/graph_memory.py), [graph regressions](../scripts/test_graph_memory.py).

## How I check retrieval claims

I use synthetic tests for vector validation, version changes, index publication, query projection, graph evidence and stale-source exclusion. I separately measure cold/warm execution, fallback, omitted evidence and answer correctness. I do not turn an index benchmark or shorter packet into a universal token-saving claim.

Relevant checks: [vector runtime](../scripts/test_vector_runtime.py), [model assets](../scripts/test_model_assets.py), [host behaviour](../scripts/test_memory_host.py), [query projection](../scripts/test_query_projection.py), [evaluation methodology](EVALUATION.md).
