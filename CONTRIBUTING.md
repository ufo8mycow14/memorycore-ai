# Contributing

I welcome help making MemoryCore AI reliable, useful and economical. I keep the main repository under maintainer control while accepting changes through forks and pull requests.

## My contribution workflow

1. Open an issue for a bug or substantial design change, using synthetic examples.
2. Fork the repository and create a focused branch in your fork.
3. Make the change with relevant tests or reproducible validation.
4. Open a pull request describing the problem, resulting behaviour and checks actually run.
5. Address review feedback. I decide whether a change is merged.

I do not require repository write access to contribute. Public fork access does not permit changes to this repository’s branches or settings. I retain ownership and administration; any future collaborator access will follow least privilege.

## Checks I expect

```sh
python -B -m unittest discover -s scripts -p "test*.py"
cargo test --manifest-path rust-broker/Cargo.toml --locked --all-targets
cargo clippy --manifest-path rust-broker/Cargo.toml --locked --all-targets -- -D warnings
```

I expect contributors to state missing capabilities and skips, especially for SQLCipher, native binaries and model caches. Changes to those surfaces need their relevant integration checks; a skipped test is not evidence that a feature works.

I use Australian English in project documentation. I ask for source-grounded claims, compact reports and focused diffs. I do not accept tests containing personal data, real conversations, secrets, model caches or vaults.

## Review boundaries

I pay particular attention to scope isolation, source freshness, retention/deletion, token accounting, dependency changes and workflow permissions. Changes must not silently expand authority, download models during recall, replace source evidence with memory, or claim measured savings without complete accounting.

The pull-request workflow has read-only repository permissions. I do not run untrusted pull requests with repository secrets or a privileged `pull_request_target` workflow. New contributors may need workflow approval before checks run.

## Licence and conduct

By submitting a contribution, you agree to license that contribution under Apache 2.0 and confirm that you have the right to submit it. Existing third-party licence notices must be preserved.

I expect respectful, evidence-based discussion. Harassment, discriminatory abuse, threats and publishing someone else’s private information are not acceptable. I may remove content or restrict participation to protect the project and its contributors. Security concerns belong in the private route described in [SECURITY.md](SECURITY.md).
