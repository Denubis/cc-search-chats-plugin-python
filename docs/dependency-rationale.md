# Dependency Rationale

## Betterleaks 1.6.1

**Status:** Approved for vendoring by the multi-source chat search design.  
**Upstream:** https://github.com/betterleaks/betterleaks  
**Release:** https://github.com/betterleaks/betterleaks/releases/tag/v1.6.1  
**Licence:** MIT  
**Upstream commit:** `28f08b4`  
**Reviewed:** 2026-07-19

### Capability required

`cc-search-chats` reads transcripts that may contain live credentials. Without an ingestion boundary, one credential can be copied from a source JSONL file into the persistent SQLite/FTS index, search output, and then another model transcript. The package therefore needs an offline detector with:

- file/stdin scanning without modifying the source;
- deterministic rule configuration;
- JSON findings with line ranges that can be mapped to raw and framed normalized records;
- report redaction before serialization;
- no required credential validation or network access;
- current maintenance and broad credential-pattern coverage;
- static binaries for the plugin's supported operating systems and architectures.

Betterleaks meets these requirements and is maintained by the creators of Gitleaks. Upstream publishes static archives for Darwin, Linux, and Windows on amd64 and arm64. Validation is optional and remains disabled in this project.

### Why it is vendored

Secret containment is a mandatory runtime boundary, so it cannot depend on a coincidentally installed executable or a network download during search. The repository vendors the immutable v1.6.1 upstream archives for all supported platform/architecture pairs together with:

- the upstream MIT licence;
- upstream checksum and Sigstore verification material;
- a reviewed local manifest of archive and executable SHA-256 digests;
- the pinned Betterleaks configuration used for transcript scanning;
- release/commit provenance and an update procedure.

At runtime the Python package selects only the matching archive, extracts it into the application's private cache without trusting archive paths, verifies the executable digest, and invokes it with an argument vector rather than a shell. It never searches `PATH` and never downloads a replacement.

### Security configuration

The wrapper enforces the following non-optional controls:

- stdin input and JSON output captured in memory;
- `--redact=100`, no banner, no color, no verbose finding logs;
- explicit package-owned configuration rather than source-directory or environment configuration;
- `--ignore-gitleaks-allow`, so transcript text cannot exempt itself;
- no `--validation` and no validation environment variables;
- a minimal subprocess environment without inherited credentials;
- strict parsing of only rule/location fields from the report;
- raw transcript, normalized-record, and final-output scans with source-record mapping;
- bounded read-only file snapshots with matching raw/commit digests and no candidate-content disk spool;
- no persistence or replay of `Line`, `Match`, `Secret`, validation data, arbitrary metadata, or raw stderr;
- timeout/non-zero/protocol failures treated as scan failures, never clean scans.

### Alternatives considered

- **Gitleaks:** Suitable interface and a compatibility reference, but upstream describes it as feature-complete and directs new development to Betterleaks. A system Gitleaks fallback would also make containment installation-dependent.
- **Yelp detect-secrets:** Easy to embed as Python and useful for repository/pre-commit hygiene, but its scanner is primarily line/plugin/baseline oriented and its latest release is older. It remains a possible development hook, not the transcript ingestion authority.
- **TruffleHog:** Broad detector coverage, but credential verification and analysis are central features and structured reports can contain raw findings. Disabling and safely adapting that larger surface is unnecessary for this boundary.
- **Custom regular expressions:** Avoids a binary but creates an unmaintained security product inside this plugin and would provide materially worse detector coverage.
- **Runtime download:** Reduces repository size but adds network availability and remote supply-chain decisions to ordinary local search.
- **Optional PATH scanner:** Preserves package size but makes an absent/mismatched scanner a common failure mode and weakens reproducibility.

### Upgrade policy

Betterleaks upgrades are security-sensitive dependency changes, not routine cache refreshes. Upgrade one version at a time and:

1. Read upstream release notes and security changes.
2. Fetch immutable archives and checksum/signature material from the upstream release.
3. Verify upstream checksums and Sigstore evidence before updating the local manifest.
4. Review rule/config differences deliberately; do not extend a moving upstream default at runtime.
5. Run real-binary protocol tests on every supported platform/architecture in CI.
6. Run false-positive fixtures, known-secret canaries, allowlist invalidation tests, and database/WAL/stdout/stderr absence assertions.
7. Record archive sizes, hashes, upstream commit, licence changes, and the falsifiable reason for the upgrade.

### Falsifiable retention test

Remove Betterleaks only if another maintained offline component demonstrates equal or better detection on the project's canary corpus, preserves line-range/redacted-report behavior, supports every shipped platform without runtime network/PATH assumptions, and passes the same no-contamination tests with a smaller or safer operational surface.
