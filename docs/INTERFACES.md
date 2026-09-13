# Interfaces and operational boundaries

I expose several interfaces for different purposes. I keep the raw broker protocol separate from the MCP protocol and from the reference CLI.

| Interface | What I use it for | Entry point |
| --- | --- | --- |
| Reference CLI | Explicit synthetic storage and lifecycle experiments | [memorycore_ai.py](../scripts/memorycore_ai.py) |
| Knowledge CLI | Source-bound proposals, review, acceptance and retrieval | [knowledge_cli.py](../scripts/knowledge_cli.py) |
| Raw native broker | Trusted-host NDJSON requests to configured native sessions | [main.rs](../rust-broker/src/main.rs) |
| Semantic host | Add automatic local embeddings/reranking to broker calls | [memory_host.py](../scripts/memory_host.py) |
| Native MCP adapter | Fixed-session JSON-RPC/MCP tool access over stdio | [native_mcp.py](../scripts/native_mcp.py) |
| Monitored MCP adapter | The same client-facing boundary with payload-free operational metrics | [monitored_native_mcp.py](../scripts/monitored_native_mcp.py) |
| Routing workbench | Cost forecasts, checkpoints and local handoff preparation | [routing_cli.py](../scripts/routing_cli.py) |

## Host configuration and authority

I use this shape for a disposable plaintext native fixture. The absolute paths describe synthetic storage, and the source directory must exist before initialisation:

```json
{
  "synthetic": true,
  "backend": "native",
  "database": "C:/MemoryCoreSynthetic/vault.sqlite3",
  "allow_plaintext": true,
  "read_workers": 4,
  "sessions": [
    {
      "id": "demo-client",
      "scope": "synthetic:demo",
      "source_root": "C:/MemoryCoreSynthetic/sources",
      "use_memories": true,
      "generate_memories": false,
      "allow_admin": false
    }
  ]
}
```

I disable generation and administration in this example. These flags do not make the session fully read-only: reviewed forgetting and proposal rejection remain available as user controls. A separate trusted operator configuration is needed to seed facts or configure semantic indexing. An empty fixture has no memories to recall. For encryption I omit `allow_plaintext`, use a SQLCipher build and set `key_env` to the name of a host-managed key variable; I never put a key value in a document or configuration.

The launcher chooses sessions, scopes, roots and permissions. The MCP client cannot choose an arbitrary session or supply host-admin operations. `use_memories` and `generate_memories` are independent controls; maintenance authority is separate. The reference CLI’s scope argument is not equivalent to a production authentication system.

Source: [`Config`, `Session` and request validation](../rust-broker/src/lib.rs), [fixed-session adapter](../scripts/native_mcp.py).

## Raw broker request

I send one JSON object per line on the trusted local pipe. This example asks a configured synthetic session to recall evidence:

```json
{"session":"demo-client","id":"request-1","operation":"call","arguments":{"name":"memory","arguments":{"recall":"Which deployment region was selected?"}}}
```

The reply preserves request/session identity. Different sessions can complete out of order; I wait for a reply before dependent work in the same session. The raw broker is not itself an MCP endpoint. Its ready event and host diagnostics are transport information, not additional model evidence.

Native input frames are bounded to 65,536 bytes, responses below 1 MiB and pending admission to 32 requests. These byte bounds are separate from the compact tool’s token envelope. The one-shot administrative command has a separate 32 MiB transfer bound. Duplicate JSON keys, unknown fields and invalid scope/action combinations are rejected.

## MCP request

After MCP initialisation and catalogue discovery, the corresponding client tool call is:

```json
{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"memory","arguments":{"recall":"Which deployment region was selected?"}}}
```

I do not include a session field here: the adapter already binds it. The adapter negotiates the supported protocol versions `2024-11-05`, `2025-03-26` and `2025-06-18`. Notifications never execute memory mutations.

I advertise one compact tool to avoid repeating full schemas for every operation. Calls still receive action-specific validation; the small catalogue does not remove review or permission checks.

| Action field | Meaning |
| --- | --- |
| `recall` | A question/query for relevant evidence |
| `propose` | A supported relative source path to inspect for proposals |
| `review` | A proposal ID whose evidence and digest need inspection |
| `accept` / `reject` | A proposal ID plus the returned `review_digest`; corrections may include `supersedes` |
| `freshness` / `relations` | A memory ID to inspect |
| `graph` | A memory ID with optional intent and depth for bounded relationship expansion |
| `review_forget` | A memory ID to obtain a deletion review |
| `forget` | A memory ID plus its reviewed digest |
| `code_context` | A symbol plus a configured provider |

I take IDs and review digests from actual returned results rather than inventing them. The exact advertised schema is [catalogue.json](../rust-broker/src/native/catalogue.json), with dispatch and envelope checks in [service.rs](../rust-broker/src/native/service.rs).

## Source review and example workflow

For a synthetic source containing `Fact: The deployment region is Adelaide.`, I propose its relative path, inspect the returned proposal and evidence, then accept using the returned digest. A later recall can return that fact with its provenance. If the file changes to Melbourne, I expect the old source-bound fact to be excluded until the new evidence is reviewed and accepted as a correction.

The native protocol and the reference CLI have different envelopes. I provide an executable reference walkthrough in [operations.md](../references/operations.md), and native/MCP examples in [broker tests](../scripts/test_rust_broker.py) and [MCP tests](../scripts/test_native_mcp.py).

## External code-context providers

I provide a local Graphify-format reader and bounded MCP adapters for GitNexus `context` and Serena `find_symbol`. The trusted host chooses the provider process and repository. Model-controlled arguments cannot supply arbitrary shell commands.

These are experimental adapters with contract tests. Their output remains evidence to verify, and I do not claim that every installed provider version is supported. This code-context surface is separate from the memory evidence graph.

Source: [code_context.py](../scripts/code_context.py), [mcp_peer.py](../scripts/mcp_peer.py).

## Monitoring and cost accounting

I keep operation type, argument names, duration, estimated tool/response tokens, included/omitted counts and resource/index conditions in operational monitoring. I exclude query text, source snippets, recalled text and exact content. The metrics log is not a second memory store.

The token values are tokenizer estimates for observed tool payloads. They do not measure all client instructions, retained conversation context, model reasoning, cache billing or acquisition costs. Missing counters remain unavailable rather than becoming zero. My [evaluation guide](EVALUATION.md) sets the complete-workflow comparison boundary.

## Errors and operator responsibilities

I distinguish missing/unavailable models, rejected scope/source/review checks, bounded-result omissions, backpressure and uncertain write outcomes. I do not treat every failure as an empty factual answer or retry a possibly committed write with a fresh identity.

The host owns configuration, key custody, lifecycle/idle/quota observations and any production authentication design. The Windows rollout helper changes a client configuration only when explicitly run; it is not part of read-only discovery. I have not supplied a native chat event connector, automatic message dispatcher or production key-recovery service.
