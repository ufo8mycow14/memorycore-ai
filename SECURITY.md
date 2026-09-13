# Security policy

I currently support this project as developmental, synthetic-only software. I do not recommend storing real personal information, credentials, payment details or private conversations in it.

## Reporting a vulnerability

Please use [GitHub private vulnerability reporting](https://github.com/ufo8mycow14/memorycore-ai/security/advisories/new). I ask for affected versions, a minimal synthetic reproduction, expected behaviour and the security impact. Please do not put exploitable details or private payloads in a public issue.

I review reports as a community maintainer and cannot promise a fixed response or remediation deadline.

## Known model-build dependency limitation

As of 13 September 2026, GitHub reports eight security advisories for the optional `onnx==1.19.1` conversion dependency in `scripts/model-build-requirements.txt`, including high-severity model/file-handling issues. I retain that pin for the current reproducible model recipe and checksums. I do not regard this conversion environment as safe for untrusted models or production processing.

I keep conversion separate from loading an already provisioned model cache. The reference test environment does not install ONNX. A reviewed upgrade must validate the conversion recipe and resulting model assets as well as dependency versions. I keep the dependency alerts open rather than treating synthetic test results as proof that the advisories are resolved.

## Important boundaries

- I require a trusted host to establish session and scope authority; the local pipe is not an authenticated public service.
- SQLCipher encryption is optional at build time and mandatory when a keyed configuration is selected. The reference SQLite workflow is plaintext.
- Encryption does not protect original source files, authorised output, every copy of process memory or historical backups.
- Secret screening and optional redaction are conservative controls, not proof that arbitrary content is safe to store.
- Retention and tombstones govern current memory state; logical deletion does not establish physical erasure from all backups.
- Repository protections reduce unauthorised changes. They cannot guarantee immunity from owner-account compromise or actions by an authorised administrator.

I keep security-related changes reviewable and test scope, freshness, lifecycle and restoration behaviour with synthetic fixtures.
