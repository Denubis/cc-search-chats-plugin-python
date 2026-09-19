# Multi-Source Chat Search Design

**GitHub Issue:** None

## Summary

The design extends the existing Claude search architecture into a unified multi-source pipeline. Source-specific discovery and parsing adapters normalize Claude and Codex transcripts into shared source and provenance contracts, which feed one source-aware SQLite/FTS5 index and common query/output path. Internal identities are namespaced by source and physical transcript version, while provider-supplied IDs and existing Claude-facing JSON fields remain stable.

Codex processing is tolerant, streaming, and read-only: it discovers every active and archived rollout version, tracks provenance and compaction state, ignores malformed records, and preserves recorded working directories as historical project metadata. Before any parsed content reaches persistent or transient search storage, a vendored offline Betterleaks scanner marks tainted JSONL line ranges. The parser retains safe structural metadata, substitutes redaction records for blocked content, and consults an exact-version SQLite allowlist that cannot be authored by transcript text.

## Definition of Done
`cc-search-chats` automatically discovers and indexes both Claude and Codex transcripts, including active and archived Codex sessions, while treating absent Codex data as normal. A source filter can isolate Claude or Codex across `search`, `index`, `list`, `extract`, and `context`; existing Claude behavior and JSON contracts remain compatible, with tests and documentation covering both formats.

Filename discovery and JSONL parsing explicitly support Codex's dated `rollout-*.jsonl` paths, session metadata, message envelopes, compaction records, malformed lines, and foreseeable format drift without modifying source transcript files.

Search results and recovered conversations advertise their available provenance, including the source product, model and model provider, client/originator, main-agent or subagent kind, agent identity/name, parent session or thread, and archive state. Metadata is carried at message/turn resolution when available, falls back to session-level values when appropriate, and remains explicitly unknown when the transcript does not supply it.

Project matching and regression coverage deeply exercise primary checkouts; linked worktrees from normal and bare repositories; detached, locked, prunable, deleted, and Codex-managed temporary worktrees; and worktree paths containing dots, repeated hyphens, spaces, Unicode, symlinks, and nested `.worktrees` directories. Historical recorded working directories remain valid search metadata even after the corresponding worktree is removed.

Every new or changed physical transcript version is scanned offline with the vendored Betterleaks binary before indexing. The raw JSONL pass audits all source material; a second numbered normalized-record pass catches values revealed by JSON unescaping, decoding, or parser concatenation before SQLite; and a final result-unit pass protects stdout. Non-allowlisted findings cannot enter persistent FTS, transient `--everything` storage, or command output; users receive source/version/path/line/timestamp alerts and can inspect, allow, revoke, and rescan exact findings without placing raw matches or secrets in SQLite. Existing indexes are rebuilt through this boundary so previously indexed toxic data is not carried forward.

## Acceptance Criteria

### multi-source-chat-search.AC1: Source discovery

- **multi-source-chat-search.AC1.1 Success:** Default discovery finds Claude UUID sessions plus Codex `rollout-*.jsonl` sessions in active and archived roots.
- **multi-source-chat-search.AC1.2 Success:** Codex discovery honors `$CODEX_HOME`, falling back to `~/.codex`.
- **multi-source-chat-search.AC1.3 Success:** Active and archived files with the same provider session ID remain independently indexed and visibly versioned; neither location is preferred or hidden.
- **multi-source-chat-search.AC1.4 Edge:** A missing source root contributes zero sessions under the default selection.
- **multi-source-chat-search.AC1.5 Failure:** Invalid filenames, insufficient identity, and symlink escapes are skipped and reported.

### multi-source-chat-search.AC2: JSONL normalization

- **multi-source-chat-search.AC2.1 Success:** Canonical Codex user/assistant messages appear exactly once.
- **multi-source-chat-search.AC2.2 Success:** Codex compaction records create correct epochs and summaries.
- **multi-source-chat-search.AC2.3 Success:** `--everything` includes available tool inputs/outputs transiently.
- **multi-source-chat-search.AC2.4 Success:** Model and agent changes are attributed at message/turn resolution.
- **multi-source-chat-search.AC2.5 Edge:** Historical formats and missing message IDs receive deterministic identities.
- **multi-source-chat-search.AC2.6 Failure:** Malformed, incomplete, unknown, and adversarial JSON lines are skipped independently without crashing.
- **multi-source-chat-search.AC2.7 Failure:** Developer/system injections and duplicate event renderings never enter clean conversation search.

### multi-source-chat-search.AC3: Unified CLI behavior

- **multi-source-chat-search.AC3.1 Success:** All subcommands default to both sources.
- **multi-source-chat-search.AC3.2 Success:** `--source claude` and `--source codex` fully isolate every subcommand.
- **multi-source-chat-search.AC3.3 Success:** `--source`, `--all`, `--project`, `--everything`, `--epoch`, and `--days` retain distinct, composable meanings.
- **multi-source-chat-search.AC3.4 Success:** Local-first search covers both selected sources and widens only after a miss.
- **multi-source-chat-search.AC3.5 Edge:** A raw provider session ID that identifies multiple physical versions returns every version for listing/extraction; an operation requiring one version uses the advertised version-qualified reference and never chooses arbitrarily.
- **multi-source-chat-search.AC3.6 Edge:** Index output reports per-source indexed, skipped, malformed, and unreadable counts.
- **multi-source-chat-search.AC3.7 Success:** Default combined output is the intentional behavior change; `--source claude` matches legacy Claude-only semantics and golden outputs except for documented additive schema fields and security redactions.

### multi-source-chat-search.AC4: Provenance

- **multi-source-chat-search.AC4.1 Success:** Results advertise source, model/provider, client/originator, agent kind/identity/name, parent session, and archive state when available.
- **multi-source-chat-search.AC4.2 Success:** Message-level provenance overrides session fallback values.
- **multi-source-chat-search.AC4.3 Success:** Main agents and subagents are distinguishable when the transcript provides the relationship.
- **multi-source-chat-search.AC4.4 Edge:** Unavailable metadata remains null or `unknown`; it is never guessed.
- **multi-source-chat-search.AC4.5 Failure:** Arbitrary metadata, prompts, credentials, encrypted reasoning, and instruction blocks are not exposed as provenance.
- **multi-source-chat-search.AC4.6 Success:** Human and JSON output both expose useful provenance.
- **multi-source-chat-search.AC4.7 Success:** Session, message, result, compaction, scan, and finding timestamps are shown in human/JSON output whenever safely available and retain explicit timezone offsets.

### multi-source-chat-search.AC5: Identity and compatibility

- **multi-source-chat-search.AC5.1 Success:** Colliding Claude/Codex session and message IDs coexist safely.
- **multi-source-chat-search.AC5.2 Success:** Existing Claude IDs and output fields retain their current values and shapes.
- **multi-source-chat-search.AC5.3 Success:** An older derived index rebuilds with a clear diagnostic.
- **multi-source-chat-search.AC5.4 Success:** JSON remains additive under schema version 1.
- **multi-source-chat-search.AC5.5 Failure:** Rebuilding never modifies source transcripts.
- **multi-source-chat-search.AC5.6 Success:** Existing Claude regression tests continue to pass.
- **multi-source-chat-search.AC5.7 Success:** Betterleaks is vendored with its licence, immutable version, supported-platform archives, and verified checksums; scanning never downloads or resolves a PATH dependency at runtime.

### multi-source-chat-search.AC6: Worktree coverage

- **multi-source-chat-search.AC6.1 Success:** Primary checkouts and linked worktrees from normal and bare repositories match correctly.
- **multi-source-chat-search.AC6.2 Success:** Detached, locked, prunable, deleted, and Codex-managed temporary worktrees remain correctly attributed.
- **multi-source-chat-search.AC6.3 Edge:** Paths containing dots, repeated hyphens, spaces, Unicode, symlinks, and nested `.worktrees` directories retain correct identity/display.
- **multi-source-chat-search.AC6.4 Edge:** Removed historical worktrees remain globally searchable without requiring the cwd to exist.
- **multi-source-chat-search.AC6.5 Failure:** Symlink handling cannot expand discovery outside allowed transcript roots.
- **multi-source-chat-search.AC6.6 Success:** Current-project matching works consistently across both transcript sources.
- **multi-source-chat-search.AC6.7 Success:** A primary checkout and each recorded linked/temporary worktree cwd remain distinct project identities; repository common-directory membership never silently collapses their results.

### multi-source-chat-search.AC7: Resilience and verification

- **multi-source-chat-search.AC7.1 Success:** Transcript files are opened read-only and streamed rather than buffered wholesale.
- **multi-source-chat-search.AC7.2 Edge:** An incomplete live-session tail is skipped and recovered after a later mtime change.
- **multi-source-chat-search.AC7.3 Failure:** Unreadable sessions are counted and reported without aborting other sources.
- **multi-source-chat-search.AC7.4 Edge:** Large image/tool records do not pollute persistent clean-content storage.
- **multi-source-chat-search.AC7.5 Failure:** Property-based arbitrary JSON inputs cannot crash either parser.
- **multi-source-chat-search.AC7.6 Success:** Tests, Ruff checks, formatting checks, type checks, and synthetic read-only smoke tests pass.
- **multi-source-chat-search.AC7.7 Success:** README, plugin skill/command, manifests, changelog, project context, and database documentation describe the delivered behavior.

### multi-source-chat-search.AC8: Secret containment and remediation

- **multi-source-chat-search.AC8.1 Success:** Every new or changed Claude and Codex transcript version receives a raw JSONL scan and a parsed normalized-record scan before content enters persistent or transient search storage.
- **multi-source-chat-search.AC8.2 Success:** Scanner line ranges are mapped to numbered JSONL records; blocked message records retain only validated structural metadata and render a redaction marker in sequence.
- **multi-source-chat-search.AC8.3 Failure:** A non-allowlisted finding can never enter FTS, `--everything`, human/JSON content output, logs, scanner reports on disk, or a rebuilt index.
- **multi-source-chat-search.AC8.4 Success:** SQLite stores only safe finding metadata and an exact finding/version/config-scoped allowlist; it never stores Betterleaks `Line`, `Match`, `Secret`, raw validation data, or a plaintext secret fingerprint.
- **multi-source-chat-search.AC8.5 Failure:** Transcript-authored Betterleaks/Gitleaks allow comments are ignored. Allowing a finding requires an explicit CLI action with a reason. A change to the flagged record, its location/version, rule set, or scanner configuration invalidates that decision; unrelated appends to a live transcript do not.
- **multi-source-chat-search.AC8.6 Edge:** If scanning fails, changed bytes are not ingested; the last successfully scanned index generation may remain available only with a stale-security warning.
- **multi-source-chat-search.AC8.7 Success:** `audit-secrets` scans/lists findings with source, physical version, active/archive location, safe timestamps, path, line range, rule, scanner/config version, allowlist state, and remediation guidance.
- **multi-source-chat-search.AC8.8 Success:** Parsed-record findings map back to original source lines, and final materialized result units pass the same scanner before stdout, closing JSON-encoding, legacy-index, and formatter bypass paths.
- **multi-source-chat-search.AC8.9 Success:** Synthetic canary tests prove that detected secrets never appear in the database file, WAL, temporary database, stdout, stderr, snapshots, or newly generated transcript fixtures.
- **multi-source-chat-search.AC8.10 Failure:** Allow/revoke reasons and other operator-authored persistent security metadata are length-limited and scanned before storage; a reason containing a finding is rejected without echoing it.
- **multi-source-chat-search.AC8.11 Edge:** Live files are scanned and parsed through one read-only descriptor bounded to a captured byte length; raw and final parse digests must match before commit. Concurrent appends wait for the next refresh, while in-place mutation rolls back.

## Glossary

- **Adapter:** A source-specific component that translates filesystem layouts or transcript formats into shared application contracts.
- **Additive JSON evolution:** Extending an existing JSON schema with new fields without removing, renaming, or reshaping established fields.
- **Clean conversation search / full-content mode:** Clean search persistently indexes normalized conversation content; full-content mode (`--everything`) temporarily includes available tool inputs and outputs.
- **Compaction / epoch:** Compaction summarizes or compresses earlier conversation context; an epoch is the segment of a conversation bounded by compaction events.
- **Codex record types:** `session_meta` describes the session; `response_item` carries canonical messages; `event_msg` may duplicate rendered messages; `turn_context` updates turn-level attribution; `compacted` marks a compaction boundary.
- **Derived index:** A rebuildable SQLite cache created from source transcripts rather than an authoritative data store.
- **Deterministic degraded identity / fallback reference:** A stable identifier synthesized from available facts when the transcript lacks a complete provider-supplied ID.
- **Format drift:** Upstream transcript structure changing over time through new, missing, or reshaped fields and records.
- **FTS5:** SQLite's full-text search extension, used to index and query normalized conversation content.
- **Functional Core / Imperative Shell:** An architecture that keeps transformations and query construction pure while isolating filesystem, database, CLI, and output side effects at the edges.
- **JIT indexing:** Just-in-time indexing that refreshes stale sessions when the current project is accessed.
- **JSONL:** A format containing one independent JSON value per line, allowing records to be streamed and malformed lines to be skipped individually.
- **Local-first search:** Searching the current project across selected sources first, then widening the same search only after a miss.
- **mtime:** A file's modification timestamp, used to decide whether a transcript needs re-indexing.
- **Provenance:** Whitelisted attribution describing where a record came from, such as source, model/provider, client, agent, parent session, and archive state.
- **Provider-native/public ID:** An identifier supplied by Claude or Codex and retained for display or lookup, distinct from collision-safe internal keys.
- **Physical transcript version:** One concrete JSONL file under an active or archived source hierarchy. Multiple versions may share one provider session ID and must all remain visible.
- **Secret finding / tainted record:** A Betterleaks match represented only by safe rule/location metadata; every JSONL record intersecting its line range is tainted until an exact allowlist decision applies.
- **Secret allowlist:** User-authored SQLite policy that admits one exact finding for one immutable transcript version and scanner configuration. It is never inferred from transcript content and does not apply to copies in other transcripts.
- **Source-namespaced key:** An internal identity that incorporates the source so equal Claude and Codex IDs cannot collide.
- **Streaming parsing:** Processing a transcript one record at a time instead of loading the entire file into memory.
- **Symlink escape:** A symlink that resolves outside an allowed transcript root and must therefore be excluded from discovery.
- **Worktree states:** Git worktrees may be linked, detached, locked, prunable, deleted, or temporary; these states affect current reachability but not the validity of recorded historical project metadata.
- **`--source`, `--all`, `--project`, and `--everything`:** Independent CLI controls for transcript source, all-project scope, a non-widening project restriction, and transient tool-inclusive content respectively.

## Architecture

The CLI uses one normalized indexing pipeline with source-specific adapters at the filesystem and JSONL boundaries. `claude` and `codex` are the only source identifiers. Every command accepts `--source {all,claude,codex}`; `all` is the default and remains distinct from the existing `--all` project-scope flag.

### Source and provenance contracts

`src/cc_search_chats/core/models.py` owns the source-neutral contracts:

- `ChatSource`: closed source identifier (`claude` or `codex`).
- `Provenance`: safe, whitelisted attribution comprising source, model, model provider, client/originator, agent kind, agent ID/name, parent session/thread ID, and archive state. Missing values remain null or `unknown`.
- `SessionMeta`: discovery result extended with provenance, physical transcript version, storage location, and an internal source/version-namespaced session key while retaining the original session ID and project path.
- `SessionRecord`: normalized message/summary extended with source, deterministic internal identity, and message/turn provenance. Raw provider IDs remain available when supplied.
- `CompactEvent`: normalized compression boundary extended with source/version-namespaced identity and an optional summary supplied directly by the source.
- `SecretFinding`: safe scanner metadata containing only finding key, rule ID, numbered JSONL range, scanner/config identity, timestamps, and allowlist state.
- `NumberedRecord`: one raw JSONL line plus its stable line number and scan disposition; raw content exists only in the streaming ingestion shell.

Message/turn provenance overrides session defaults. Parsers never copy arbitrary provider metadata, prompts, instructions, encrypted reasoning, credentials, or other unapproved payloads into provenance.

### Discovery adapters

Existing Claude discovery remains in `src/cc_search_chats/core/discovery.py` and keeps its public behavior. New Codex discovery in `src/cc_search_chats/core/codex_discovery.py` resolves `CODEX_HOME` (default `~/.codex`), recursively scans regular `rollout-*.jsonl` files below `sessions/` and `archived_sessions/`, and never follows a symlink outside those roots.

The first valid `session_meta` record is authoritative for Codex session ID, cwd, model-provider/client fields, and available agent relationships. The dated filename provides a deterministic degraded identity and creation timestamp when metadata is partial. A file without enough identity to be indexed is reported and skipped. Every physical file is emitted independently. Active and archived files may share a provider session ID, but retain distinct version-qualified references derived from source, storage-root kind, and root-relative path. The version key remains stable while an active JSONL file grows; a separate whole-file digest identifies the generation last scanned/indexed. No active/archive preference or content deduplication hides a version.

Project matching treats recorded cwd as historical metadata rather than requiring the path to exist. This supports primary checkouts, linked and bare-repository worktrees, detached/locked/prunable worktrees, removed historical worktrees, Codex-managed temporary worktrees, and adversarial but valid path spelling.

The normalized recorded cwd is the project identity. A primary checkout and `/repo/.worktrees/feature`, for example, remain separate projects even when Git reports the same common directory. Directory hierarchy may be exposed for display/grouping, and current-cwd symlink resolution may locate an exact recorded project, but neither mechanism merges stored worktree identities or rewrites their historical display paths.

### Parsing adapters

Existing Claude parsing remains in `src/cc_search_chats/core/parser.py`. New pure streaming transforms in `src/cc_search_chats/core/codex_parser.py` normalize Codex lines without file I/O. A small source dispatcher selects the parser from `SessionMeta.source`; the shared indexer does not inspect provider JSON.

Clean Codex parsing accepts canonical `response_item` message records with user or assistant roles. It excludes developer/system context and duplicated `event_msg` renderings. `turn_context` and `session_meta` update provenance state for subsequent normalized records. `compacted` creates an epoch boundary and retains its source-provided summary without treating retained startup/context packets as ordinary chat. Full-content mode additionally flattens tool names, inputs, and outputs; reasoning remains excluded when encrypted or unavailable.

Each JSONL file is processed as numbered lines. Before ordinary normalization, the parser receives the Betterleaks line-range disposition. A blocked line is still parsed defensively enough to retain validated record kind, role, timestamp, compaction boundary, and relative ordering, but no free-form string or provider identifier from that line survives. Blocked conversation records become non-FTS redaction markers; blocked compaction records still advance the epoch with a redacted summary. A line is admitted only when every intersecting finding is explicitly allowlisted. Malformed, incomplete, or unknown lines are skipped independently. Re-indexing on a later mtime recovers a live file's previously incomplete tail. Arbitrary JSON shapes must not raise from either parser, and exceptions/logs never include raw lines.

### Vendored secret-containment boundary

Betterleaks v1.6.1 is vendored under `src/cc_search_chats/vendor/betterleaks/` as upstream release archives for Linux, macOS, and Windows on x64 and arm64, together with the MIT licence, upstream checksum material, a local manifest, and provenance for the immutable upstream release commit. Release maintenance verifies upstream checksums and Sigstore material before updating the vendored manifest. Runtime platform selection and extraction are deterministic; the selected executable is checked against the manifest before every first use of that version. Search never downloads a binary and never falls back to a program found on `PATH`.

`src/cc_search_chats/security/scanner.py` invokes the binary without a shell and streams the raw transcript through stdin. It supplies the package-owned configuration explicitly, sets JSON reporting to stdout with 100% report redaction, disables banners/colors/verbose logging, ignores transcript-authored `gitleaks:allow`/`betterleaks:allow` comments, and never enables validation or passes validation environment variables. Scanner stdout is parsed in memory through an allowlist of safe fields; value-bearing `Line`, `Match`, `Secret`, validation, and arbitrary metadata fields are discarded. Scanner stderr is never replayed verbatim.

The scanner runs first over every new or changed physical transcript version. Ingestion opens one read-only descriptor and captures a byte-length/stat snapshot. The raw pass scans exactly that prefix while hashing it. A second pass parses the same bounded prefix and streams a framed decoded-plaintext projection to Betterleaks; every projection line maps out-of-band to its original JSONL line and record key, so multiline content and normalized findings resolve to source provenance rather than temporary coordinates. No candidate transcript or projection is spooled to disk. This pass catches secrets revealed by JSON unescaping, decoding, or parser concatenation. A third bounded parse applies the combined taint/allowlist map and writes only admitted or fixed-redaction records inside a transaction; its digest must equal the raw-pass digest before commit. Concurrent appends beyond the captured length wait for the next refresh, while in-place mutation rolls back.

Before human or JSON output, the scanner processes framed decoded result units and the final serialization with origin-span mapping, so an old index, cross-unit formatting, or formatter error cannot carry a detected secret into another chat. An output finding is admitted only when it maps wholly to an origin/rule with an active exact allowlist; otherwise the affected unit, or the whole serialization when it cannot be mapped safely, is suppressed. `--everything` scans its distinct normalized projection before populating the in-memory database. A scanner failure at any pass is fail-closed for changed bytes or output: no new records replace the last successfully scanned generation and no unverified result content is printed.

Current safe findings are stored in `secret_finding`, including which raw, normalized, or output stages observed the rule. Observations from different stages merge under an origin finding key based on physical version, source record/range, full-record digest, rule, and scanner configuration—not the whole-file digest, temporary projection line, or matched value. The user-controlled `secret_allowlist` admits only that exact origin. An allowlist entry requires a length-limited reason that itself passes the scanner before storage, is preserved across derived-index rebuilds, survives unrelated appends to a live transcript, and becomes stale when that record/location/rule/configuration changes. It never applies globally to a rule, token, provider session, active/archive pair, or copied transcript. This deliberate narrowness prevents one false-positive decision from becoming a cross-transcript contamination channel.

### Shared index and query flow

The data flow is:

`source selection -> versioned discovery -> raw scan -> numbered JSONL parser -> normalized scan/redaction -> SQLite/FTS5 -> source-aware query -> output scan -> human/JSON output`

`src/cc_search_chats/storage/schema.sql` separates internal keys from displayed provider IDs. Source/version-namespaced keys are primary/foreign keys for sessions, messages, compact events, findings, and materialized summaries. `(source, provider_session_id)` is indexed but intentionally non-unique; physical transcript versions are unique by their version key and location. Messages without provider IDs receive deterministic references derived from source, physical version, and stable record position; Claude's existing UUIDs remain unchanged in public output.

Conversation/search tables are derived cache data; `secret_allowlist` is local user-authored policy. An explicit internal database schema version in `src/cc_search_chats/storage/index.py` detects the incompatible identity layout and atomically rebuilds content from read-only transcripts through the scanner. Only structurally validated, non-secret allowlist rows are carried into the replacement database. The previous database remains in place if scanning or verification fails, and no message/FTS rows are copied from a pre-containment index.

Pure query builders in `src/cc_search_chats/core/search.py` accept source selection. Storage operations propagate it through search, list, extract, context, JIT indexing, all-project indexing, transient full-content scanning, and secret auditing. Bare provider session IDs may enumerate/extract every matching physical version. Operations that require one transcript/message use an advertised source/version-qualified reference and fail with candidates rather than choosing arbitrarily.

### CLI and output behavior

`src/cc_search_chats/cli.py` applies one shared `--source` option to every subcommand, including `audit-secrets`. Local-first search discovers and JIT-indexes the current project across the selected sources, then widens across that same source selection after a miss. `--project` remains a non-widening project pin; `--all` remains all projects; `--everything` remains transient full-content search.

Defaulting to `--source all` intentionally changes unqualified invocations by adding Codex results. Compatibility is defined at the isolation boundary: `--source claude` must match the prior Claude-only selection, ranking, extraction, context, and human/JSON golden behavior except for additive provenance/version/security fields and mandatory redaction. This is tested separately from the combined-source golden outputs.

Missing source roots are normal under the default `all` selection. Explicitly selecting a source with no readable sessions returns a source-specific diagnostic. Index operations report per-source and per-storage-location indexed, skipped, malformed, unreadable, scanned, blocked, and stale counts. `audit-secrets` lists or rescans findings; explicit allow/revoke actions require a finding ID and record the reason and timestamp in SQLite.

`src/cc_search_chats/output.py` evolves JSON schema version 1 additively. Existing fields and shapes remain; source, physical version, timestamps, provenance, redaction state, and safe security findings are added to search results, session listings, extracts, context messages, and index statistics. Human output adds a compact provenance label such as `[codex · gpt-5.4 · subagent xyz · archive]`. Unknown metadata is rendered honestly, never inferred. Open findings are warned on every affected selected version until remediated or explicitly allowlisted; no warning includes matched content.

## Existing Patterns

This design follows established repository patterns:

- Functional Core / Imperative Shell: JSONL transforms and query construction remain pure; filesystem, SQLite, CLI, and stderr behavior remain at the edges.
- Streaming JSONL parsing: records are processed independently and malformed lines are skipped without buffering an entire transcript.
- SQLite FTS5 indexing: persistent clean conversation search and transient `--everything` indexing continue to share the normalized indexing path.
- JIT indexing by mtime: current-project access refreshes stale sessions, while all-project indexing remains incremental.
- Additive JSON evolution: schema version 1 gains fields without removing, renaming, or reshaping existing payloads.
- Standard-library-only Python runtime plus a pinned, vendored Betterleaks executable; layered pytest coverage includes Hypothesis for adversarial parser/scan-report inputs and canary leakage assertions.

The new adapter boundary is a deliberate extension. Translating Codex records into Claude-shaped JSON was rejected because it would conceal distinct compaction, identity, and provenance semantics. Separate per-source databases were rejected because they would duplicate query behavior and make combined local-first ranking and context recovery harder.

## Implementation Phases

<!-- START_PHASE_1 -->
### Phase 1: Source, Version, Provenance, and Security Contracts

**Goal:** Establish normalized source/version/provenance/security types and the collision-safe SQLite identity and allowlist contracts.

**Components:**

- `src/cc_search_chats/core/models.py` — source, physical version, provenance, session, message, compact-event, redaction, and safe finding contracts.
- `src/cc_search_chats/storage/schema.sql` — source/version-namespaced internal keys, non-unique provider IDs, provenance columns, findings, exact allowlist policy, constraints, and affected summary relationships.
- `src/cc_search_chats/storage/index.py` — internal schema-version detection, atomic derived-index rebuild, and allowlist preservation behavior.
- `tests/test_index.py`, `tests/test_indexing.py`, and `tests/conftest.py` — schema, atomic rebuild, allowlist preservation, deterministic identity, version coexistence, and cross-source collision coverage.
- `docs/database.md` — complete index data model, ERD, security data flow, dictionary, and schema decisions.

**Dependencies:** None.

**Done when:** New and legacy indexes open safely, legacy content rebuilds without copying old message/FTS data, validated allowlist policy survives rebuild, every physical version can coexist, cross-source IDs cannot collide, raw Claude IDs remain visible, and the phase's contract/schema tests pass.
<!-- END_PHASE_1 -->

<!-- START_PHASE_2 -->
### Phase 2: Codex Discovery and Worktree Resolution

**Goal:** Discover every active and archived Codex rollout file and associate each physical version with stable session, project, agent, and storage metadata.

**Components:**

- `src/cc_search_chats/core/codex_discovery.py` — `CODEX_HOME` resolution, filename validation, recursive active/archive discovery, metadata probing, physical version identity, and safe root boundaries.
- `src/cc_search_chats/core/discovery.py` — shared discovery/ranking seams while retaining Claude behavior.
- `tests/test_codex_discovery.py` — filename/layout failures, same-ID active/archive coexistence, metadata degradation, root permissions, and the full worktree/path matrix.
- `tests/test_discovery.py` — unchanged Claude regression behavior plus shared-contract assertions.

**Dependencies:** Phase 1 contracts.

**Done when:** Valid Codex sessions are found under configured/default roots, missing roots are harmless, all active/archive versions remain independently addressable, historical/deleted worktree cwd values remain usable, unsafe paths are excluded, and discovery tests pass.
<!-- END_PHASE_2 -->

<!-- START_PHASE_3 -->
### Phase 3: Codex JSONL Normalization

**Goal:** Normalize Codex conversation, compaction, tool, model, agent, and taint dispositions into shared streaming contracts.

**Components:**

- `src/cc_search_chats/core/codex_parser.py` — pure numbered-record/session parsing, scan dispositions, redaction markers, clean/full-content projections, provenance state, compaction events, and deterministic fallback references.
- `src/cc_search_chats/core/parser.py` — source dispatch seam while retaining Claude parser behavior.
- `tests/fixtures/codex_session.jsonl` and `tests/fixtures/codex_compacted_session.jsonl` — synthetic current/historical rollout shapes.
- `tests/test_codex_parser.py` — canonical content, duplicates, tool content, compaction, tainted structural parsing, provenance changes, missing IDs, format drift, malformed tails, images, and adversarial JSON.
- `tests/test_parser.py` — Claude regression, tainted-record behavior, and shared parser invariants.

**Dependencies:** Phase 1 contracts and Phase 2 metadata.

**Done when:** Codex clean/full-content streams normalize without duplication or instruction leakage, tainted records cannot contribute free-form values, redaction markers preserve safe ordering/epochs, provenance is as precise as source data permits, malformed/arbitrary JSON never crashes parsing, and parser tests pass.
<!-- END_PHASE_3 -->

<!-- START_PHASE_4 -->
### Phase 4: Vendored Betterleaks and Contamination Gate

**Goal:** Prevent detected secrets from crossing transcript ingestion or result-output boundaries while supporting exact, reviewable false-positive exceptions.

**Components:**

- `src/cc_search_chats/vendor/betterleaks/` — pinned upstream archives for supported OS/architecture pairs, MIT licence, source/release provenance, checksum/signature evidence, and runtime manifest.
- `src/cc_search_chats/security/scanner.py` and package-owned Betterleaks config — platform selection, safe extraction/checksum validation, no-shell offline invocation, redacted report parsing, decoded plaintext framing, origin-span mapping, and bounded file hashing.
- `src/cc_search_chats/storage/index.py` — scan-before-parse orchestration, finding/allowlist lookup, fail-closed stale-generation behavior, and transient-mode enforcement.
- `src/cc_search_chats/cli.py` — `audit-secrets` list/rescan/allow/revoke operations with mandatory reasons for allow decisions.
- `.pre-commit-config.yaml` and development configuration — separate repository hygiene hooks using the vendored scanner plus standard Python/file checks.
- `tests/test_secret_scanner.py`, `tests/test_security_indexing.py`, and security fixtures — fake scanner protocol tests, real vendored-binary smoke tests, raw and JSON-unescaped canaries, multiline projection/source mapping, transcript allow-comment bypass attempts, allowlist invalidation, scanner failures, concurrent append/in-place mutation, and database/WAL/stdout/stderr canary absence.

**Dependencies:** Phase 1 security schema, Phase 2 version identity, and Phase 3 numbered parsing.

**Done when:** Vendored executables verify and run on supported test platforms, every ingestion/output path is scanned offline, blocked records are safely represented but never searchable, exact allow/revoke policy works without raw secret storage, failures retain only the last safe generation, and leakage canary tests pass.
<!-- END_PHASE_4 -->

<!-- START_PHASE_5 -->
### Phase 5: Unified Source-Aware Indexing and Queries

**Goal:** Index, search, rank, list, extract, and recover context across both sources and every physical version in one database.

**Components:**

- `src/cc_search_chats/storage/index.py` — parser dispatch, per-source/version discovery orchestration, JIT/all-project indexing, transcript scan policy, transient full-content indexing, and version-qualified resolution.
- `src/cc_search_chats/core/search.py` — source-aware pure query builders.
- `tests/test_indexing.py` and `tests/test_search.py` — combined-source/version indexing, collisions, incremental refresh, active/archive coexistence, local/widened/all scope, project filters, source filters, epoch behavior, and qualified/bare-ID behavior.

**Dependencies:** Phases 1-4.

**Done when:** One index supports correct combined and isolated results for every storage operation, all physical versions remain visible, local-first semantics remain intact, full-content scans cover selected sources without persistent unsafe content, bare IDs enumerate versions, qualified IDs resolve exactly, and indexing/query tests pass.
<!-- END_PHASE_5 -->

<!-- START_PHASE_6 -->
### Phase 6: CLI and Output Contracts

**Goal:** Expose automatic combined search, explicit source isolation, versions, provenance, timestamps, and security diagnostics without breaking existing callers.

**Components:**

- `src/cc_search_chats/cli.py` — shared `--source` option and source-aware orchestration for search, index, list, extract, context, and secret auditing/allowlist decisions.
- `src/cc_search_chats/output.py` — additive schema-version-1 provenance/version/security fields, timestamps, per-source statistics, compact human labels, redaction markers, and candidate/error rendering.
- `tests/test_cli.py` and `tests/test_output.py` — flag interaction matrix, unchanged invocation behavior, JSON compatibility, version/provenance/timestamp rendering, findings/allowlist output, missing-source behavior, and malformed/unreadable statistics.

**Dependencies:** Phase 5 unified operations.

**Done when:** Default commands operate across both sources, `--source` isolates all subcommands, existing JSON fields/shapes remain valid, every version/source/provenance/timestamp is advertised, security diagnostics and exact allow/revoke flows are actionable, output re-scanning is enforced, and CLI/output tests pass.
<!-- END_PHASE_6 -->

<!-- START_PHASE_7 -->
### Phase 7: Plugin Guidance, Project Context, and Release Verification

**Goal:** Teach the plugin workflow about multi-source and security-aware search and verify the complete release surface.

**Components:**

- `skills/search-chat/SKILL.md` and `commands/search-chat.md` — source-aware routing, result interpretation, security/redaction handling, drill-down, and recovery guidance.
- `README.md`, `CHANGELOG.md`, `.claude-plugin/plugin.json`, and `.claude-plugin/marketplace.json` — installation/usage documentation, vendored third-party notice, security/remediation workflow, and multi-source descriptions.
- `docs/dependency-rationale.md` — Betterleaks selection, vendoring/update policy, threat boundaries, and rejected alternatives.
- `CLAUDE.md` — durable architecture, command, source-format, JSON/security-contract, and read-only transcript boundaries.
- Complete test, lint, format, type-check, pre-commit, checksum, and synthetic-root smoke verification.

**Dependencies:** Phases 1-6.

**Done when:** Documentation accurately covers combined/default and isolated usage, physical versions, provenance, scanner/allowlist/remediation behavior, and vendored upgrades; plugin consumers interpret schema version correctly; project context reflects the new contracts; and tests, Ruff, formatting, type checks, pre-commit, vendored checksum verification, and synthetic read-only security smoke tests all pass.
<!-- END_PHASE_7 -->

## Additional Considerations

**Read-only source boundary:** Claude and Codex transcripts are never edited, renamed, moved, repaired, or deleted. Only the derived SQLite index is rebuilt.

**Format drift:** Codex transcript JSON is an adapter-owned, tolerant input rather than a shared-domain contract. Unknown records and fields are ignored; absence of required identity/content produces source-specific skip statistics. Synthetic fixtures represent multiple observed shapes so a new upstream field does not become a failure.

**Live and large files:** Streaming avoids whole-file memory use and tolerates a partially written final line. Valid individual JSONL records may still be large because of embedded images/tool output; clean mode avoids retaining irrelevant payloads, while full-content mode remains explicitly transient.

**Identifier ambiguity:** Public IDs remain provider-native where possible. A provider session ID can identify multiple physical versions even within one source. Listing/extraction can return all candidates; operations requiring one record advertise and require the source/version-qualified reference rather than recommending an arbitrary candidate.

**Worktree semantics:** Project identity is the recorded working directory, not current Git reachability, common Git directory, or branch state. Primary and linked/temporary worktrees stay distinct. Symlink normalization must not erase the displayed historical path, and a removed/prunable worktree remains discoverable in global history.

**Physical versions:** Directory hierarchy and root-relative filename are part of physical version provenance. Matching provider IDs or content never cause an active/archive version to disappear. Bare extraction shows every candidate; context and security decisions use the exact advertised version reference.

**Allowlist authority:** Transcript text is untrusted and cannot suppress scanning. The SQLite allowlist is the only exception mechanism, is exact-version/config scoped, requires an operator reason, and is preserved separately from rebuildable conversation rows. Allowing content authorizes output only from that origin; a copied occurrence in another transcript is a new finding.

**Scanner limits:** Betterleaks detection is a high-value containment layer, not proof that an unflagged transcript contains no secret. Its pinned configuration makes decisions reproducible; updates are explicit dependency changes with canary and false-positive review.
