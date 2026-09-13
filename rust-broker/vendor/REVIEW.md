# TurboVec Dependency Repair

The vendored source is crates.io TurboVec 1.0.0 (MIT). Its Rust algorithms are
unchanged. The build manifest alone selects statrs 0.18.0 with default features
disabled. TurboVec uses the Beta distribution's CDF/PDF, not statrs random
sampling or matrix operations. This removes the unused nalgebra/simba/paste
chain responsible for RUSTSEC-2024-0436, without removing TurboVec.

Cargo.toml.orig is retained as upstream evidence, not the active build manifest.
Upstream tests and the broker's exact-vector regression must pass against this
repair. Codebook compatibility is tested separately against the original pinned
dependency; changing a dependency version alone is not proof of numerical parity.
The upstream licence, tests and source are retained for review and redistribution.
