# Third-party notices

I retain upstream notices for the dependencies included in this source distribution.

## TurboVec

I vendor TurboVec 1.0.0 under `rust-broker/vendor/turbovec`.

- Upstream: https://github.com/RyanCodrai/turbovec
- Copyright: 2026 Ryan Codrai
- Licence: MIT, preserved in `rust-broker/vendor/turbovec/LICENSE`.
- Local dependency metadata uses the reviewed `statrs` dependency; the vendored source remains separately licensed.

## Dependencies and model assets

I record Rust dependencies in `rust-broker/Cargo.lock` and Python dependencies in the requirements files. Those dependencies retain their own licences.

I do not distribute model weights in this repository. Model source revisions, declared licences and asset hashes are recorded in `scripts/model-assets-lock.json`. Downloaded or derived model assets remain subject to the applicable upstream terms; my project licence does not replace them.
