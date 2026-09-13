# Storage, corrections, retention and recovery

I keep durable memory in a local SQLite database, with an optional SQLCipher build for encrypted native storage. The search index and model caches are derived acceleration structures; they do not replace the vault or source evidence.

## What is stored

| Record family | Purpose in my implementation |
| --- | --- |
| `hippocampus_stage` | Bounded staged source material with scope, timestamps, expiry and integrity metadata |
| `cortex_memory` | Durable atomic memories, 128-bit IDs, scope, status, confidence/importance, timestamps, validity and correction identity |
| `cortex_detail` | Separately stored detail so ordinary summary recall need not load it |
| `cortex_term` | Hashed lexical lookup terms associated with memory rows |
| `cortex_verbatim` | Explicitly confirmed supported exact content, byte counts, integrity and retention metadata |
| `cortex_tombstone` | Deletion identity used to prevent removed records being recreated by stale transfers |
| `vault_state` | Vault identity and revision tracking |
| Knowledge/native extensions | Source bindings, proposals, relationships, chat/project lifecycle, vectors/jobs, retry receipts and routing state |

I keep core schema 2 in [schema.sql](../rust-broker/src/native/schema.sql). Extension tables are established by their owning modules, including [knowledge](../rust-broker/src/native/knowledge.rs), [vectors](../rust-broker/src/native/vectors.rs), [chat lifecycle](../rust-broker/src/native/chat_lifecycle.rs), [retention](../rust-broker/src/native/retention.rs) and [retry recovery](../rust-broker/src/native/recovery.rs). The core DDL alone is not the complete database specification.

## Compression and integrity

I encode ordinary memory payloads using the `BM1` format: five UTF-8 fields with variable-length byte counts. The codec chooses zlib compression only when it is smaller; `Z` and `N` markers distinguish compressed and uncompressed payloads. Decompression checks size, stream completion and field boundaries.

The ordinary semantic encoder trims field whitespace. It must not be used as a substitute for an explicit byte-exact save. Exact preservation has a separate record type and supported-format checks.

I use full SHA-256 integrity values alongside compact identifiers and fingerprints. A 128-bit memory ID identifies a record; it is not a compressed container for arbitrary knowledge. Hashed lexical terms are indexing aids, not encryption or a guarantee that no information can be inferred.

Source: [native codec](../rust-broker/src/native/codec.rs), [database integrity and records](../rust-broker/src/native/database.rs), [exact-content operations](../rust-broker/src/native/exact.rs).

## From proposal to correction

I derive proposals from supported, bounded source content and preserve evidence positions and a source hash. Review returns a digest of the proposal being considered. Acceptance checks the reviewed identity and current source again before writing the durable record.

Corrections identify the current version through `supersedes`. I preserve correction relationships rather than silently replacing the old meaning. An outdated review, changed source or stale version requires a new review. Source freshness establishes agreement with the selected file bytes; it does not establish that the file’s claim is true.

A host-selected source root and relative path checks constrain evidence access. The native reader uses a directory capability, validates candidate files and rechecks source content on requests. Opt-in redaction produces a labelled source projection without rewriting the original file; changing the original source can invalidate the binding even when the changed value would be redacted.

Source: [`SourceReader`, `propose`, `review`, `accept` and `freshness`](../rust-broker/src/native/knowledge.rs), [prohibited-data policy](../rust-broker/src/native/policy.rs).

## The different clocks

| Policy | Default/behaviour I currently use |
| --- | --- |
| Staging and pending proposals | A 24-hour default lifetime; expiry/maintenance disposes expired material |
| Background proposal eligibility | At least 24 hours idle, no active chat turn, and fresh trusted observations |
| Generation quota | At least 25% remaining in every supplied applicable window; observations older than 60 seconds fail the gate |
| Native archive retention | 365 days by default, configurable through operator maintenance; manual deletion or unarchive can occur earlier |
| Explicit record expiry | Remains distinct from pinning and archive policy; a pin does not cancel expiry |
| Retry envelope | A host-selected expiry within the next 24 hours, with a shorter window preferable for ordinary retries |

I distinguish the idle generation delay from durable capture. A future capture host must durably stage completed content before acknowledging it; waiting 24 hours to extract useful facts is not a reason to keep the only copy in RAM. The repository does not supply an automatic native chat capture feed or an always-running quota scheduler.

I explain the last-activity clock, examples, skip reasons and separate configuration settings in [memory consolidation and retention](MEMORY_RULES.md).

Source: [generation policy](../rust-broker/src/native/generation.rs), [archive policy](../rust-broker/src/native/retention.rs), [reference lifecycle operations](../scripts/memorycore_ai.py).

## Archive, delete and restore

I exclude inactive and stale evidence from ordinary recall. Archive keeps records outside normal recall while retention applies. Reviewed forgetting is logical deletion; purge is a distinct exact-ID operation with its own confirmation and tombstone behaviour. Pruning uses a preview and unchanged reviewed candidates.

Trusted versioned chat/project events can mark linked sources active, archived, unknown or deleted. Deleting chat-only evidence removes its derived memories/vectors/jobs; evidence supported independently can survive after the deleted-source provenance is removed. A caller merely labelling a source independent is not proof of independent support.

Current encrypted snapshots preserve deletion authority and set non-deleted linked chat/project lifecycle state to unknown. A newer authoritative lifecycle receipt is needed before that evidence becomes eligible after restore. This protects against automatic reuse of stale snapshot state; it does not erase older backups or retrofit safeguards into snapshots made by older runtimes.

Source: [chat lifecycle](../rust-broker/src/native/chat_lifecycle.rs), [retention](../rust-broker/src/native/retention.rs), [snapshot lifecycle handling](../rust-broker/src/native/backup.rs), [lifecycle regression tests](../scripts/test_native_chat_lifecycle.py).

## Encryption and portability

I require an explicit key for encrypted native configuration and reject a keyed configuration in a build without SQLCipher. Disposable plaintext fixtures explicitly opt in with `allow_plaintext: true`; adding a key does not silently convert an existing plaintext database.

The trusted launcher supplies a random 256-bit hexadecimal key through the environment variable named by `key_env`. The configuration stores the variable’s name, not its value. Temporary Rust-owned key strings are zeroised; the parent environment and key recovery remain host responsibilities.

For encrypted portability, the native backup operation uses SQLCipher’s online backup API to create a consistent, separately keyed snapshot, including committed WAL data. It refuses an existing destination and verifies the result. Source documents are not included. Losing all usable key copies makes encrypted data unrecoverable.

Scope JSON exports are plaintext. Encrypted sessions reject them unless `allow_plaintext_export` is explicitly granted. Imports validate records and extension metadata; routing transfers retain audit information without automatically replaying deliveries. I distinguish an encrypted snapshot from a bounded JSON export.

SQLCipher protects database storage and WAL, not original source documents, authorised plaintext results or every copy of process memory. Logical deletion and secure-delete settings are not a promise of physical erasure from every historical backup.

Source: [encrypted backup](../rust-broker/src/native/backup.rs), [portable data](../rust-broker/src/native/portable.rs), [encryption tests](../scripts/test_rust_encryption.py), [security limitations](../SECURITY.md).

## Crash recovery and uncertain writes

I commit native mutations and their response-size checks transactionally. Optional retry envelopes add a receipt committed with the mutation. Repeating the exact unchanged request with the same unexpired recovery identity can return its committed result; a changed request or expired identity is rejected.

Receipts are bounded to 4,096 entries and 16 KiB per response. Purge revokes cached response content while preserving enough retry identity to reject stale replay. These are vault-local recovery guarantees, not distributed exactly-once delivery. A snapshot taken before a later write cannot know that write occurred.

If a worker fails after a write was dispatched, the response may indicate an unknown outcome. I do not blindly retry with a new identity. I use the original valid recovery envelope or inspect durable state through the trusted operator path.

Source: [recovery receipts](../rust-broker/src/native/recovery.rs), [transactional response handling](../rust-broker/src/native/service.rs), [worker failure handling](../rust-broker/src/main.rs).
