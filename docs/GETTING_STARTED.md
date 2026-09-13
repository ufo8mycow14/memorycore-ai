# Getting started

I recommend starting with the Python reference workflow, then adding the native runtime and semantic models as separate steps. I require synthetic fixtures and explicit disposable paths throughout.

## Reference environment

I use Python 3.12 or later. From the repository root:

```sh
python -m venv .venv
```

For PowerShell:

```powershell
.venv\Scripts\Activate.ps1
```

For a POSIX shell:

```sh
. .venv/bin/activate
```

Then:

```sh
python -m pip install -r requirements-test.txt
python -B scripts/memorycore_ai.py capabilities
python -B -m unittest discover -s scripts -p "test*.py"
```

I use [the synthetic walkthrough](../references/operations.md) to create an explicit temporary database, save and correct a fact, retrieve it under a token budget, and exercise lifecycle operations. The example is PowerShell-specific. Tokenizer encodings may need an initial download; budgeted recall does not silently substitute character estimates.

## Native runtime

I build from the repository root with a current Rust toolchain supporting edition 2024 and the locked dependencies:

```sh
cargo build --manifest-path rust-broker/Cargo.toml --locked --release
cargo test --manifest-path rust-broker/Cargo.toml --locked --all-targets
cargo clippy --manifest-path rust-broker/Cargo.toml --locked --all-targets -- -D warnings
```

I use a C compiler for bundled SQLite. For an encrypted build, I also need the vendored OpenSSL prerequisites, including Perl:

```sh
cargo build --manifest-path rust-broker/Cargo.toml --locked --release --features sqlcipher --target-dir rust-broker/target-sqlcipher
```

I keep host configuration outside the repository and select absolute database/source paths, `synthetic: true`, `backend: native`, and explicit session/scope settings. An encrypted configuration names a host-managed key environment variable through `key_env`; it never contains the key itself. Disposable plaintext fixtures must explicitly choose `allow_plaintext: true` and omit `key_env`. I never point fixture commands at an existing personal vault.

The executable accepts `--init --config`, `--config`, `--native-command --config` and `--backup --config`. Initialisation refuses to overwrite a database. I use `scripts/test_rust_broker.py` and `scripts/test_rust_encryption.py` as executable configuration examples, including generated synthetic keys.

## Local semantic models

I keep inference dependencies separate from the reference setup:

```sh
python -m pip install -r scripts/vector-requirements.txt
python -B -m scripts.vector_pipeline --help
```

Model acquisition is an explicit setup operation. I pin model revisions and asset hashes in `scripts/model-assets-lock.json`. The default ranking profile also requires the conversion dependencies in `scripts/model-build-requirements.txt` during provisioning. That conversion environment has known ONNX dependency advisories; I describe the current limitation in [the security policy](../SECURITY.md). I consult `download-model --help` and `download-reranker --help` before selecting a cache location. The cache and weights are not repository assets.

I launch `scripts.memory_host`, `scripts.native_mcp` or `scripts.monitored_native_mcp` with the built broker, an explicit host configuration and the provisioned cache. Their `--help` output describes the current interface. The MCP adapter additionally binds an existing session. Each adapter process currently owns its own host, so launching one per project can duplicate resident models.

## Integration boundaries

I provide stdio MCP support for a trusted local client. I have not certified every MCP client. The adapter advertises one `memory` tool and excludes host administration from client calls.

The Windows helper `scripts/setup_local_rollout.py` is an advanced, explicit installer: it creates a local environment and synthetic vault, downloads dependencies/models, and edits a Codex configuration file. I do not use it as a read-only demo or recommend running it without inspecting its arguments and changes. It does not import existing conversations or replace platform memory.

For full local integration tests, I set `MEMORYCORE_AI_SQLCIPHER_BINARY`, `MEMORYCORE_AI_VECTOR_BINARY` and `MEMORYCORE_AI_MODEL_CACHE` to verified local resources. Native reference tests also expect the ordinary release binary under `rust-broker/target/release`. Missing capabilities produce skips, which I report separately from passes.
