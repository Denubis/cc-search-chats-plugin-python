# Cross-Vendor Semantic Search Design

**Status:** Current design

**GitHub Issue:** None

## Authority Sources

| Decision or instruction | Exact source | Resolver | Resolution condition |
|---|---|---|---|
| Index the isolated Claude and Codex Ponytail session corpora while keeping message attribution out of this delivery. | `ccchat:v1:codex:01a0222a-8cf2-7a12-ac38-2c0f471f81b2:id:msg_01a0227b-a5d7-70f0-9eac-ecfdfe195d77` | `sed -n '812p' /home/brian/.codex/sessions/2026/08/21/rollout-2026-08-21T12-33-29-01a0222a-8cf2-7a12-ac38-2c0f471f81b2.jsonl` | Exactly one native user `response_item` states both boundaries. |
| Retain crash/generation records but do not retain unused message, alias, or embedding copies. | `ccchat:v1:codex:01a0222a-8cf2-7a12-ac38-2c0f471f81b2:id:msg_01a0229d-4034-7ec2-b5b0-7923a3b8afe9` | `sed -n '916p' /home/brian/.codex/sessions/2026/08/21/rollout-2026-08-21T12-33-29-01a0222a-8cf2-7a12-ac38-2c0f471f81b2.jsonl` | Exactly one native user `response_item` accepts generation metadata and rejects unused copies. |
| Proceed with the reconciled storage, freshness, Ponytail, UAT, and documentation work. | `ccchat:v1:codex:01a0222a-8cf2-7a12-ac38-2c0f471f81b2:id:msg_01a022a9-3b5a-70d1-ab14-b7b2a363589c` | `sed -n '927p' /home/brian/.codex/sessions/2026/08/21/rollout-2026-08-21T12-33-29-01a0222a-8cf2-7a12-ac38-2c0f471f81b2.jsonl` | Exactly one native user `response_item` says `do it`. |
| Accept this reconciled design as matching the intended outcome. | `ccchat:v1:codex:01a0222a-8cf2-7a12-ac38-2c0f471f81b2:id:msg_01a02398-fc3a-7670-8f76-e267cfe92208` | `sed -n '1338p' /home/brian/.codex/sessions/2026/08/21/rollout-2026-08-21T12-33-29-01a0222a-8cf2-7a12-ac38-2c0f471f81b2.jsonl` | Exactly one native user `response_item` accepts the rendered design. |

## Summary

Extend `cc-search-chats` from Claude-only full-text search into a local,
cross-vendor search and exact-resolution tool for native Claude and Codex chat
logs. A dedicated PostgreSQL 18 database is the derived search authority for
normalized metadata, PostgreSQL full-text search, and pgvector
embeddings. Large search relations and indexes live in a dedicated tablespace
on configured external storage; PostgreSQL data and WAL remain operator-managed.
Search refreshes appended native records before retrieval, exposes live progress
and freshness watermarks, and never locks the vendor logs that supervisors are
writing. If semantic refresh or the GPU is unavailable, hybrid search fails
explicitly while the last committed literal generation remains usable.
Corpus and semantic generations are small publication and recovery records;
they never own full copies of messages, physical aliases, or embeddings.

## Definition of Done

- Natural-language and exact-term queries search native Claude and Codex chats using hybrid semantic and FTS ranking. Agy and the transport archive are out of scope as searchable corpora.
- Default results include only visible user/assistant prose from primary sessions. `--agents` includes Claude subagent and Codex child-agent conversations. `--tools` adds lexical-only tool names, inputs, and results. Reasoning and developer/system instructions remain excluded.
- Embedding and retrieval run locally. Once the model is installed, searches work offline and transmit no chat content.
- An overnight bulk job maintains the baseline index. Searches detect and index appended records before retrieval and emit machine-readable phase, progress, elapsed-time, coverage, and freshness state rather than appearing hung, including when on-demand indexing takes roughly 30 seconds.
- Every indexed record has a provider-qualified stable locator. Exact resolution is independent of search ranking and distinguishes unique, missing, ambiguous, stale, unavailable, malformed, and unsupported-schema outcomes.
- Search, context, extract, list, and reference-only output share one additive JSON identity shape. Results report provider roots, projects or repositories, files searched, skipped or unreadable sources, unrecognised conversation-shaped records, and index freshness.
- Large indexes and local model data live under a configured external-storage root rather than inside the repository. The tool does not render transcript archives, summarise chats, or write notes, ADRs, plans, or constraints.
- Native Claude and Codex chats remain the content authority. Message
  attribution, receipt correlation, and authorship classification are deferred
  comms-plumbing work and are not changed or accepted by this delivery.

## Acceptance Criteria

### cross-vendor-semantic-search.AC1: Native cross-vendor search

- **Success:** One default query can return ranked native Claude and Codex
  prose results, and every result identifies its provider and native session.
- **Success:** Natural-language search fuses semantic and FTS ranks, while
  `--literal` performs FTS-only retrieval without loading the embedding model.
- **Failure:** Agy sessions and rendered transport archives never enter the
  searchable corpus, even when their files are reachable from a configured
  source root.

### cross-vendor-semantic-search.AC2: Content and session boundaries

- **Success:** Default results contain only visible user/assistant prose from
  sessions positively classified as primary.
- **Success:** `--agents` additionally includes sessions classified as agent or
  unknown, and reports the retained classification. `--tools` additionally
  searches persisted tool names, inputs, and results through FTS only.
- **Success:** `--literal --tools --exhaustive` returns every matching tool or
  prose occurrence in deterministic locator order; ranked search never claims
  to be exhaustive.
- **Failure:** Reasoning, thinking, developer instructions, system instructions,
  injected context, and unrecognised content shapes are not indexed or returned
  by any flag. The existing `--everything` flag is rejected with migration
  guidance rather than retaining its reasoning-inclusive meaning.

### cross-vendor-semantic-search.AC3: Local semantic model

- **Success:** Semantic passages and queries use
  `nvidia/Nemotron-3-Embed-8B-BF16`, the model-card passage/query prefixes,
  1024-dimensional Matryoshka slicing, and post-slice normalization.
- **Success:** After explicit installation into the operator-configured model
  cache, indexing and search work with network access disabled and transmit no
  chat content.
- **Failure:** Missing semantic dependencies or model files produce a nonzero,
  machine-readable error with installation/remediation state; runtime commands
  do not download a model implicitly or redirect package/model caches.

### cross-vendor-semantic-search.AC4: Scheduled and on-demand freshness

- **Success:** A scheduled `index` command can build and compact the baseline
  corpus overnight. A later search discovers new files and complete appended
  records and refreshes them without reparsing unchanged files.
- **Success:** Native Claude/Codex writers are never locked. Each refresh records
  a per-file byte watermark, and a source that advances during refresh is
  reported as advanced rather than falsely described as fully current.
- **Success:** One cross-process refresh owner serializes index writes. Other
  callers continue reading committed snapshots or emit `waiting_for_index`
  progress until the requested freshness is committed.
- **Failure:** Truncation, rotation, replacement, prefix changes, partial final
  JSONL records, and unsupported schema changes cannot be mistaken for a clean
  append. The affected source is reparsed or skipped with an explicit reason.
- **Success:** An unchanged second refresh reads source metadata but no JSONL
  content bytes; a valid append reads only the suffix after the last complete
  record watermark.

### cross-vendor-semantic-search.AC5: Observable work and semantic failure

- **Success:** Scan, parse, FTS commit, model preflight/load, embedding, semantic
  commit, query embedding, retrieval, and completion emit structured phase,
  elapsed-time, completed-unit, total-unit when known, and freshness events.
- **Success:** Human output and NDJSON progress identify the active index owner
  and continue emitting a heartbeat during a long phase, so a roughly
  30-second refresh does not appear hung.
- **Failure:** If semantic refresh fails, staged semantic rows do not become
  current, the last valid semantic generation remains intact, current FTS state
  is retained, and hybrid/semantic search exits nonzero rather than serving
  stale vectors.
- **Failure:** Model-load, query-embedding, or VRAM failure reports the failed
  phase, available/required VRAM when measurable, semantic freshness, and the
  literal text `Literal search is required for complete current results`, plus
  an executable `search --literal ...` form. No alternate model is selected.

### cross-vendor-semantic-search.AC6: Stable identity and exact resolution

- **Success:** Search, context, extract, list, and reference-only output use the
  same provider-qualified identity object and
  canonical locator string.
- **Success:** Exact resolution uses provider/session/record identity and source
  verification, not FTS or semantic ranking. Physical duplicate records resolve
  to one logical message with explicit physical aliases.
- **Failure:** Exact resolution distinguishes malformed locator, unsupported
  provider schema, unavailable source, stale locator/index, no match, and
  multiple matches. It never collapses those outcomes into an empty result.

### cross-vendor-semantic-search.AC7: Coverage and JSON contract

- **Success:** Every JSON command returns an object with `schema_version`,
  command, identity-bearing results, source coverage, refresh state, semantic
  state, warnings, and a terminal status. Additive fields do not change existing
  field meanings within a schema version.
- **Success:** Coverage reports configured and resolved provider roots, projects
  or repositories, discovered/read/indexed/skipped/unreadable file counts,
  unknown session kinds, unrecognised conversation-shaped records, and source
  watermarks.
- **Failure:** A partial scan or skipped source cannot produce
  `completeness = "complete"`. Progress uses stderr so stdout remains one valid
  final JSON document.

### cross-vendor-semantic-search.AC8: Storage and component ownership

- **Success:** The dedicated database stores all large search tables and indexes,
  including staged and current vectors, in a PostgreSQL tablespace below a
  configured external data root. Search migrations, refresh, and maintenance
  are confined to rebuildable search relations. Refresh ownership, status, and
  generation state are transactional PostgreSQL state rather than ad hoc
  lock/status files.
- **Success:** Before refresh, the application verifies the tablespace location,
  configured mount identity, writability, and sufficient peak rebuild space. A
  missing/read-only/replaced external mount fails without creating a fallback
  search database or writing into the underlying root-filesystem mountpoint.
- **Failure:** If PostgreSQL or the search database is unavailable, all search
  modes fail with a named database-unavailable error; “offline” promises no
  network dependency after installation, not daemonless operation.
- **Failure:** The feature does not modify native chats, render transcript
  archives, summarize conversations, author notes/ADRs/plans/constraints, or
  mutate any deferred comms-plumbing schema.

### cross-vendor-semantic-search.AC8a: No retained snapshot copies

- **Success:** A canonical message has one current stored content row, each
  genuine native occurrence has one physical-alias row, and a semantic vector
  is stored once per embedding profile and normalized input digest.
- **Success:** Generation records retain status, watermarks, counts, timing, and
  failure diagnostics without owning copies of corpus or vector rows.
- **Success:** Staging contains only changed sources and is reclaimed after a
  successful publication or an explicitly diagnosed abandoned run.
- **Failure:** Re-running an unchanged index does not increase corpus-generation,
  message, alias, semantic-generation, or embedding row counts. Appending one
  record does not copy unchanged rows. Diagnostic run records have an explicit
  bounded retention policy.
- **Failure:** Migration never prunes the deployed snapshot tables until the
  normalized current data, constraints, positive searches, and semantic joins
  have all passed independent checks.

### Deferred: Authorship classification

- **Success:** Every message retains vendor conversational `role` separately
  from `submitted_by`, whose closed values are `human`,
  `identified_harness`, and `unknown`.
- **Success:** An unmatched native `role=user` record is `unknown`. Positive
  allowlisted native harness provenance or one mutually unique exact
  native-record/receipt pair may upgrade it to `identified_harness` and records
  both compatibility cardinalities. `human` remains reserved until a future explicit positive
  producer/native contract exists.
- **Failure:** Fuzzy text, phrase containment, receipt absence, incompatible
  session/repository evidence, failed receipts, a native record compatible with
  zero or multiple receipts, or a receipt compatible with zero or multiple native
  records cannot establish human or harness authorship.

This remains a future design boundary, not an acceptance criterion or
implementation phase for the current delivery.

### Deferred: Independent provenance evidence

- **Success:** The producer-owned versioned PostgreSQL contract-metadata and
  evidence views are read in one read-only transaction snapshot and correlated by provider, session when
  available, repository/cwd, exact normalized digest and lengths, causal time,
  terminal outcome, and both compatibility cardinalities.
- **Success:** Native collaboration/Agent/SendMessage/local-mail provenance stays
  authoritative and does not receive duplicate synthetic receipt evidence.
- **Edge:** An absent, inaccessible, incompatible-version, incomplete, or
  malformed receipt contract/evidence view does not block chat indexing or literal search;
  affected messages remain `unknown` and diagnostics report why evidence could
  not be used. Loss of the entire PostgreSQL database remains the search-wide
  `database_unavailable` case.
- **Failure:** Search-chats never inserts, updates, deletes, or synthesizes
  producer receipts, never migrates or maintains their schema, and never claims
  historical uncertainty was resolved.

This remains a future design boundary, not an acceptance criterion or
implementation phase for the current delivery.

## Glossary

- **Content class** — A closed parser classification such as visible prose,
  tool input/output, or excluded private/instruction material.
- **Corpus generation** — A monotonically increasing publication/recovery record
  bound to exact per-source watermarks. It does not own message or alias copies.
- **Exact resolver** — Lookup by provider-qualified source identity, independent
  of ranked search.
- **FTS generation** — The committed corpus generation visible to a search
  transaction.
- **Logical message** — One conversational message after provider-specific
  physical duplicates have been canonicalized.
- **Physical alias** — A native record that refers to the same logical message
  as another native record.
- **Primary session** — A session positively identified as a top-level human-
  facing Claude or Codex conversation; it does not imply human authorship for
  each `role=user` message.
- **Semantic generation** — Validation metadata binding one model/chunker
  profile to one corpus generation. Vectors are reusable rows keyed by profile
  and normalized input digest, not copied into each generation.
- **Source watermark** — The identity, size, and complete-record byte boundary
  through which one native file was read.
- **`submitted_by`** — Provenance classification independent of conversational
  role: `human`, `identified_harness`, or `unknown`.

## Architecture

### Measured corpus and design consequence

The 2026-08-10 read-only audit found 9,603 Claude JSONL files (5.9 GiB) and
704 Codex JSONL files (1.3 GiB). Visible text comprised 267,877 user/assistant
messages and approximately 264 million characters. A provisional
3,000-character/400-character-overlap estimate yielded 323,498 semantic chunks,
including agent sessions. At 1024 float32 dimensions, those vectors occupy about
1.26 GiB before metadata, indexes, and temporary staged replacements.

These are a dated planning snapshot, not an execution oracle. Storage preflight,
semantic row counts, and the backend benchmark remeasure the current corpus and
record their commands and inputs before authorizing production behavior.

The 2026-08-21 production audit found a provisional snapshot implementation,
not this normalized design. The selected corpus contains exactly 1,651,678
message rows and 1,682,012 physical aliases, while PostgreSQL statistics
estimate 19,550,042 and 19,967,284 rows respectively across retained snapshots.
The selected semantic generation contains 270,012 embeddings while exact
per-generation counts total 2,413,617 retained rows. Those three relations
occupy 82 GiB including
indexes. Search reads only singleton-selected snapshots; no implemented history
or rollback consumer uses the superseded rows. This is the migration input and
must not be normalized into an accepted retention policy.

That scale does not justify an approximate nearest-neighbour index before an
exact-scan benchmark demonstrates a problem. The first backend is PostgreSQL 18
with pgvector: PostgreSQL owns normalized metadata and full-text search, and
pgvector stores normalized 1024-dimensional float32 vectors. Retrieval performs
an exact filtered scan initially. HNSW remains an internal optimization only if
real cold/warm measurements demonstrate a need; it does not change identities,
generation semantics, or the JSON contract.

### Data flow and ownership

```mermaid
flowchart LR
    C["Claude native JSONL"]
    X["Codex native JSONL"]
    PA["Provider adapters"]
    I["Single refresh owner"]
    S[("PostgreSQL rebuildable search schema")]
    V["PostgreSQL FTS + pgvector"]
    Q["Search / resolve / context / extract / list"]

    C -->|read-only snapshot| PA
    X -->|read-only snapshot| PA
    PA --> I
    I --> S
    S --> V
    V --> Q
```

Native files remain content authority. Search relations and vectors are derived
state that may be discarded and rebuilt. Refreshes do not lock or write native
logs. Receipt and attribution plumbing is outside this delivery and cannot
become a dependency of indexing, freshness, migration, or acceptance.

### Provider adapters and fail-closed parsing

Each provider adapter owns discovery, schema recognition, session-kind
classification, physical-record identity, visible-content extraction, and
source verification. Both emit one common immutable record model:

```text
provider, source_session_id, logical_message_id, physical_locator,
source_file_relative, source_line, source_byte_offset, source_digest,
timestamp, role, session_kind, conversation_epoch, content_class, text,
repository/cwd
```

`session_kind` is `primary`, `agent`, or `unknown`. Default search admits only
`primary`; `--agents` admits all three while preserving the classification in
output. This prevents an unrecognised new subagent origin from silently entering
default results.

Adapters use an allowlist of understood record/content shapes. Recognized visible
text becomes prose. Recognized tool-use/tool-result blocks become tool rows.
Known reasoning/thinking/system/developer/injected shapes and all unrecognized
conversation-shaped payloads are excluded. Unknown shapes increment coverage
diagnostics rather than being flattened permissively.

Provider adapters also emit recognized compression/compaction boundaries as
non-searchable session metadata. Messages carry a zero-based
`conversation_epoch`; a session with no recognized boundary has epoch 0.
Claude `compact_boundary` and compatible Codex compaction records advance the
epoch only under their provider-specific schema contracts. Unknown boundary
shapes are diagnostic and never shift later messages speculatively. Search and
extract retain the existing epoch filter, while list/extract output retain epoch
counts and boundary metadata without indexing summaries as ordinary prose.

Claude message UUIDs provide the preferred physical identity. Codex
`response_item` message IDs are preferred when present. For Codex messages with
no native ID, the fallback binds session ID, complete-record ordinal, and an
exact source-record digest. Codex event/response duplicates are canonicalized as
one logical message; every retained native occurrence remains a physical alias
that exact resolution can report. Duplicate timestamp compatibility requires
both outer timestamps to parse as timezone-aware ISO-8601 values and to be
nondecreasing in physical-record order. It does not impose a duration cutoff.
The other pairing facts remain exact: provider session, source file,
conversation epoch, role, text digest, opposite recognized projection families,
mutual uniqueness, and no intervening visible prose message. This rule is based
on a 2026-08-11 read-only snapshot of 792 native Codex files: 32,752
consecutive-visible exact-role/text opposite-family pairs had valid timestamps
in physical order; 32,749 were physically adjacent, three were separated only
by non-prose metadata, and 11,505 had unequal timestamps spanning
0.001–20.609 seconds. The observed upper value is evidence against an equality
rule, not a timeout to encode.

### Configured source roots and isolation

The imperative shell accepts an ordered collection of roots per provider. Each
root receives an internal stable source ID derived from its provider and
resolved configured path. The source ID participates in checkpoint and physical
alias identity so identical relative filenames in different roots cannot
collide; it is not part of the public canonical message locator. Moving a root
may require a safe full reparse but does not invalidate public references.

On this host the default collection includes, when present:

```text
claude  ~/.claude/projects
claude  ~/.claude-ponytail/projects
codex   ~/.codex/sessions
codex   ~/.codex-ponytail/sessions
```

Plural `CC_SEARCH_CLAUDE_ROOTS` and `CC_SEARCH_CODEX_ROOTS` configuration uses
the platform path separator and replaces the corresponding default collection;
the singular variables remain compatible one-root overrides during migration.
Explicitly configured roots are required and fail loudly when unavailable.
Optional default Ponytail roots are included only when their session directory
exists.

Only the listed native session directories are traversed. Search never reads or
shares the isolated homes' configuration, credentials, plugins, skills, caches,
locks, or XDG state. It never writes any provider root. Identical observations
across roots become distinct physical aliases of one canonical message;
conflicting content under one canonical identity aborts publication.

### Provider-qualified locators

Canonical locator version 1 has provider-specific record keys:

```text
ccchat:v1:claude:<session-id>:uuid:<message-uuid>
ccchat:v1:codex:<session-id>:id:<item-id>
ccchat:v1:codex:<session-id>:ordinal:<ordinal>:sha256:<record-digest>
```

The serialized JSON identity also carries logical message ID, source-relative
path, line, byte offset, digest, and physical aliases. Absolute storage roots are
not identity, so moving `~/.claude/projects` or `~/.codex/sessions` behind a
configured root/symlink does not invalidate references. Line and byte offset are
accelerators, not proof: resolution verifies provider, session, native ID when
available, and digest/cardinality.

Resolution reads the native source directly when possible. It does not issue an
FTS/vector query. Named terminal states are:

```text
resolved, no_match, multiple_matches, source_unavailable, stale_source,
stale_index, malformed_locator, unsupported_provider_schema
```

`resolve LOCATOR` exposes this exact operation; `resolve --reference-only`
returns the verified identity, aliases, and source coordinates
without message text. `context` accepts the same canonical locator rather than
an unqualified Claude UUID. `extract` retains convenient session-ID input but
accepts `--provider` and returns `multiple_matches` when an unqualified session
ID is not unique across providers.

Provider-qualified identity cannot be represented honestly by the existing
Claude-only JSON schema version 1 fields. Command cutover therefore makes one
intentional version-2 break and updates the bundled skill/command in the same
phase. Every version-2 response has common `schema_version`, `command`,
`status`, `coverage`, `refresh`, `semantic`, and `warnings` fields plus
command-specific data. Every message-bearing item embeds the same `identity`
object containing provider, source session, logical message, canonical locator,
physical aliases, and source coordinates. PostgreSQL surrogate keys and absolute
provider roots are never public identity. Version 2 then evolves additively.

### PostgreSQL schema and cutover

The dedicated `cc_search_chats` database on the local PostgreSQL 18 cluster is
owned by `cc_search_chats_owner`; the runtime role receives only the DML and
read privileges required by the rebuildable application schema. Migration DDL
is owner-only and recorded in an ordered ledger. Normal search and refresh
commands cannot create databases, roles, extensions, tablespaces, or unrelated
schemas. Receipt-authority ownership remains a deferred comms-plumbing concern
and is not touched by this migration.

Principal relations are normalized around stable provider identity:

- `source_root` and `source_file` record configured roots, provider, relative
  path, device/inode when meaningful, size, nanosecond mtime, complete-record
  byte/line/ordinal watermarks, validation fingerprints, adapter schema version,
  and serialized parser continuation state;
- `chat_session` records provider session identity, repository/cwd, session kind,
  parent linkage, and activity; `session_epoch` records recognized provider
  compaction boundaries and their non-searchable metadata;
- `message` stores one current row per provider, native session, logical message,
  and content class. It owns current visible metadata, exact content, content
  digest, and its generated `simple`-configuration search vector;
- `physical_alias` stores each genuine native occurrence once and includes its
  internal source-root ID, relative path, record coordinates, locator, and
  digest. It references `message` without a generation key;
- `index_generation` records requested targets, status, counts, timestamps,
  coverage, and terminal diagnostics. Run-owned staging relations contain only
  changed-source deltas and are never visible to search;
- `semantic_profile` identifies the complete model/input contract.
  `semantic_embedding` stores one validated `vector(1024)` per profile and
  normalized prefixed-input digest. Current messages or chunks join to it by
  digest; semantic generations contain validation and publication metadata, not
  vector memberships or copies.

Required scalar fields are `NOT NULL`; nullable columns represent genuinely
unknown or inapplicable information. Foreign keys, uniqueness constraints, and
closed vocabulary relations enforce provider-qualified identities and state
transitions in the database. SQL migrations are ordered, transactional where
PostgreSQL permits, and recorded in a migration ledger. The implementation also
creates or updates `docs/architecture/database.md` with table ownership,
cardinality, keys, invariants, and generation visibility.

The deployed snapshots do not contain parser continuation state and therefore
cannot seed safe append checkpoints. Migration performs one full bounded parse
of every configured native root into normalized candidate relations, including
standard and Ponytail sources. It compares overlapping standard-source
identities and content against the singleton-selected snapshot and classifies
every difference; the native sources remain authoritative. The candidate is
checked for exact counts, canonical conflicts, alias coverage, constraints, and
positive literal queries before atomic cutover. Snapshot tables remain
quarantined and read-only through acceptance. Only a separate verified prune
step drops them; it never addresses the existing message-attribution quarantine
schema or native logs.

Production currently selects corpus generation 14 while its selected semantic
generation 9 targets corpus generation 13; incomplete semantic generation 10
targets 14 and contains no vectors. Migration therefore joins the selected old
vectors to their generation-13 text, validates the pairing, and imports them
only into the reusable profile/input-digest pool. It does not declare semantic
state current until every eligible message in the normalized current corpus
resolves to a validated vector and the missing current inputs have been embedded.

### Transactions, generations, and refresh ownership

Parsing, model loading, and embedding occur outside database transactions.
Changed rows are loaded in bounded batches with Psycopg `COPY`, associated with
an `index_generation`, and invisible to search while incomplete. A short final
transaction validates row counts, source watermarks, canonical conflicts, and
referential integrity; replaces aliases only for successfully reparsed sources;
merges appended messages and aliases; garbage-collects messages with no physical
aliases; advances checkpoints; and commits generation metadata. PostgreSQL MVCC
makes readers see either the old or new canonical rows. No full-corpus
membership or row copy is needed. Interrupted or invalid staging rows are
diagnosable and reclaimable but never current.

A semantic refresh writes only missing reusable vectors. Its short publication
transaction verifies that the corpus generation is unchanged and that every
eligible current message or chunk resolves to exactly one vector under the
selected profile, then records the selected semantic generation. Failed work
may leave validated reusable vectors but cannot make an incomplete generation
current; unreachable vectors are reclaimed after a later successful publication.

A session-level PostgreSQL advisory lock admits one refresh owner. The owner
publishes heartbeat, phase, progress, and requested/committed freshness in
`index_generation`; it does not hold a write transaction while scanning, parsing,
loading a model, or embedding. Search/index callers that requested freshness wait
without a transaction while reporting `waiting_for_index`; they do not silently
satisfy that request from an older committed generation. Read-only status and an
exact native resolution whose locator already names its source may inspect
committed state without requesting refresh. Advisory-lock ownership is released
automatically if the database session dies, so stale PID-file recovery is
unnecessary.

After on-demand refresh, search opens a read-only `REPEATABLE READ` transaction,
reads the committed corpus and semantic generation metadata once, and performs
metadata, lexical, and vector queries in that snapshot. Literal search requires
only a committed corpus generation. Hybrid search requires the selected
semantic generation's target corpus generation to equal the committed corpus
generation; mismatch is explicit semantic staleness, not permission to serve
old vectors.

Before a PostgreSQL CLI command opens a connection, it makes one blocking
`flock` request for its work-class single-flight gate. The file is only an
OS-owned admission primitive: contenders sleep until release, process death
releases ownership, and it is neither durable status nor a lease. Database
reads then make one blocking request for a transaction-scoped advisory lock
with local `lock_timeout`, `statement_timeout`, and temporary-file limits; the
transaction releases that lock on every exit path. This two-layer admission
prevents local process fan-out while still protecting the database from callers
on another host. A PostgreSQL queue table is deliberately excluded: without a
resident worker it would turn reads into writes and require lease expiry,
claiming, stale-row recovery, and another scheduler.

Exact locator resolution accepts an ordered locator array. Canonical and
physical-alias branches use identity indexes, union only narrow logical
identity keys, and fetch message bodies after deduplication. Misses and duplicate
inputs retain their input positions and independent result counts. It never
combines a wide-row `SELECT DISTINCT` with a `LEFT JOIN ... OR`, and integrity
checks use one process, connection, and batched database operation rather than a
subprocess per locator.

### Failure and recovery

A failed candidate migration leaves the deployed snapshot schema selected and
searchable. A failed changed-source refresh leaves canonical rows and checkpoints
unchanged. A failed semantic refresh leaves literal search current and does not
select incomplete semantic metadata. Every failure records the owning
generation, phase, and diagnostic without manufacturing a successful watermark.

Cutover and prune are distinct operations. Cutover atomically selects the
normalized relations while retaining schema-qualified snapshot tables. Prune
first repeats current counts, foreign-key checks, positive search controls, and
semantic completeness; its transaction addresses only the named snapshot
relations. PostgreSQL transaction rollback protects an interrupted drop before
commit. After commit, native logs remain sufficient to rebuild the derived
schema, but rollback to snapshot code is no longer available and therefore
requires accepted UAT before pruning.

### External PostgreSQL storage

Production configuration points `CC_SEARCH_DATA_ROOT` at
`/media/brian/storage/cc-search-chats` on this machine. A privileged provisioning
step creates an empty directory such as
`/media/brian/storage/cc-search-chats/postgresql` with ownership and permissions
for the PostgreSQL cluster account, creates a dedicated tablespace there, and
makes it the default and temporary tablespace for `cc_search_chats`. Application
tables, indexes, staging rows, and query spill therefore use external storage.
The shared cluster's catalog, configuration, and WAL remain in the
operator-managed PostgreSQL cluster; the application does not relocate them.

Provisioning records the expected filesystem identity and tablespace location.
Before a refresh, the application compares the configured root and PostgreSQL's
reported tablespace location, verifies the expected mount/device and read-only
flags, performs a bounded PostgreSQL temporary write/read probe through the
runtime role in the configured temporary tablespace, and computes a conservative
peak-space estimate from catalog-measured current heap/index allocation, retained
live data, operation-specific staged heap/index rows, semantic embedding and
membership counts, selected exact/ANN index construction scratch, configured
temporary-spill allowance, and a named safety margin of the greater of 20% of
incremental peak or 8 GiB. The estimate reports `current_allocated_bytes`, every
incremental component, `projected_peak_allocated_bytes`, and nonzero
`required_free_bytes`; unknown/unmeasurable inputs fail before staging rather
than becoming zero. The CLI's OS user is not expected to have direct write
permission to the postgres-owned tablespace directory. Read-only status performs
no probe; it reports mount/catalog facts and the last recorded successful
preflight and component breakdown. A missing, replaced, read-only, database-
unwritable, or insufficient mount fails closed. The tool never creates a
substitute database or tablespace on the root filesystem.

The 2026-08-10 host upgrade established PostgreSQL 18.4 and packaged pgvector
0.8.6, preserved the stopped PostgreSQL 16 cluster and a full logical backup,
verified 329 non-template databases after migration, and verified that the
running server uses the `postgres` OS account. The operational runbook retains
the PostgreSQL 16 rollback cluster and logical backup until search-database
provisioning and a post-restart verification pass.

Model and package caches are operator configuration, not application state. The
application reports their resolved paths but never overrides them. On this
machine `HF_HOME`, `TORCH_HOME`, `UV_CACHE_DIR`, `PIP_CACHE_DIR`, and
`XDG_CACHE_HOME` already point below `/media/brian/storage/.cache`.

### Incremental refresh and concurrent native writers

A lightweight discovery pass stats every configured native session file. Each
checkpoint records provider root, relative path, device/inode where meaningful,
size, nanosecond mtime, last complete-record byte offset, absolute next record/
line coordinates, prefix/tail fingerprints, and the adapter's immutable parser
continuation state: next conversation epoch, recognized boundary state, and
duplicate-canonicalization carry. An unchanged checkpoint is skipped. A valid
append reads only bytes after the prior complete-record boundary and resumes
with that exact state. Replacement, shrinkage, prefix change, or incompatible
schema triggers a full source reparse from epoch 0 or explicit skip. A canonical
event/response pair must inhabit the same epoch and cannot cross a recognized
compaction boundary.
If a previously represented source is temporarily unreadable, the next generation
retains its last committed observation and content, records the failed current
attempt separately, and reports partial coverage; absence of a successful read
never authorizes silent deletion.

At refresh start, each file receives a target size. Parsing stops at the last
complete newline-delimited record at or below that size. Native writers may
continue appending. A final stat records whether the file advanced and how many
bytes remain pending. Freshness is therefore a precise watermark, not a claim
that an actively changing corpus was frozen.

One refresh owner holds the session-level advisory lock described above. Its
short heartbeat updates name the database session and run, while PostgreSQL
itself releases ownership if that session ends. A second refresh caller emits
`waiting_for_index` and waits for or reuses the committed generation rather than
starting duplicate parsing or model work.

The FTS generation commits independently so current literal search survives a
semantic failure. Semantic search requires the current semantic generation to
target the selected current FTS generation for the corpus. A mismatch is explicit
semantic staleness, never an invitation to serve the previous semantic generation
silently.

The opt-in overnight unit invokes `index`. That composed
operation refreshes FTS and semantic state in order. It reports success only
when the selected semantic generation targets the newly selected FTS generation
under the requested profile; maintenance dry-run follows only that success.
Semantic failure leaves the newly committed FTS generation
literal-searchable, returns a nonzero partial terminal state, and does not let the
timer or status claim a fresh hybrid baseline.

The packaged user service is a low-priority oneshot rather than a resident
application service. Its persistent timer runs nightly at 03:00 with randomized
delay. Both the unit and CLI containment apply `Nice=10`, idle I/O scheduling,
low CPU/I/O weights, and memory/task bounds. The composed command holds one
session-scoped advisory lock across literal refresh and embedding, so separate
phase locks cannot admit an overlapping rebuild.

### Semantic chunks and vectors

Only visible prose receives embeddings. A tokenizer-aware, versioned chunker
splits within one logical message—never across turns—targeting 768 content
tokens, with a 1024-token hard maximum including the passage prefix and model
special tokens, and 96-token content overlap. Short messages remain one chunk.
Each chunk records message identity, ordinal, token and character bounds,
`chunker_id`, `model_id`, vector dimension, and source-text digest. Changing any
model, prefix, dimension, tokenizer, or chunker parameter requires a complete
semantic validation generation without invalidating FTS or copying unchanged
message rows.

Passages use the model-card `passage: ` prefix; queries use `query: `. Prefixing
is explicit and applied exactly once. The model's mean-pooled 4096-dimensional
output is validated; the first 1024 Matryoshka dimensions are sliced and
re-normalized before float32 persistence.
Chunk hits are collapsed to a logical message by the best semantic chunk before
hybrid fusion, preventing overlap from producing duplicate results.

The first vector backend stores normalized rows in pgvector `vector(1024)`
columns. A single SQL query joins the selected semantic generation metadata and
reusable embeddings to current message/session metadata, applies
agent/provider/project/date filters before
`ORDER BY` and `LIMIT`, and uses inner-product distance over normalized vectors
for exact cosine ordering. Excluded rows therefore cannot starve eligible
results. A real-corpus cold/warm benchmark records exact pgvector scan latency,
buffer use, and memory before enabling hybrid search by default. No HNSW index is
created unless that benchmark demonstrates a user-visible need; an index remains
an internal optimization rather than a new search contract.

### Model and GPU lifecycle

Core storage adds a compatible pinned range for Psycopg 3. Semantic support is
an explicit dependency extra containing the Python pgvector adapter and
compatible pinned ranges for CUDA PyTorch, Transformers, and NumPy. The direct
Transformers adapter owns tokenization, mean pooling, slicing, and normalization;
Sentence Transformers is not an application dependency. A compatibility spike
must prove Python 3.14 installation,
PostgreSQL 18/server-side pgvector round trips, model load, 1024-d output,
normalization, offline reload, and one bounded batch before implementation
assumes the stack is viable.

The embedding profile pins the exact model and tokenizer snapshot, currently
`c44c20ab3f6b430336706847a6372de4b2eb3dbd`, plus prefixes, pooling,
dimensions, normalization, attention implementation, package versions, and
chunker parameters. A dedicated `model install` operation is the only
network-capable path; it requires explicit operator action and uses the already
configured Hugging Face cache without redirecting it. `model status` and all
runtime commands are read-only with respect to that cache.

Runtime model loading uses local files only and `trust_remote_code=False`. SDPA
is the initial attention implementation; FlashAttention is not an implicit
dependency. A separate session-level advisory
lock prevents concurrent commands from loading multiple 8B copies onto one GPU.
The model is not kept in a resident daemon; connection/process exit releases the
lock and process exit releases VRAM. Preflight reports CUDA/runtime
availability and best-effort total/free/estimated-required VRAM. Allocation is
still attempted defensively because preflight cannot predict fragmentation or a
racing external allocation.

Embedding writes only missing reusable vectors and advances semantic generation
metadata only after every expected chunk and text/vector invariant validates.
OOM, interruption, incompatibility, or validation failure leaves the previous
semantic generation selected. Query embedding failure follows the same loud
literal-search boundary even when the stored semantic generation itself is
current. Because model loading and query embedding occur outside a database
transaction, retrieval rechecks the selected FTS generation, semantic
generation, and complete embedding profile in its read-only repeatable-read
snapshot and retries or fails if they changed during embedding.

### Search modes and ranking

`search` has three explicit retrieval semantics:

| Mode | Behaviour | Completeness claim |
|---|---|---|
| default | Natural-language FTS candidates plus semantic candidates, fused by reciprocal-rank fusion | Ranked top results only |
| `--literal` | Prose FTS only; add tool FTS with `--tools` | Ranked literal results |
| `--literal --exhaustive` | Stream/page every deterministic FTS occurrence; add tool occurrences with `--tools` | Exhaustive through reported source watermarks |

`--all` retains its current meaning of all indexed projects/providers and is not
repurposed to mean every result. `--everything` is removed because no supported
mode exposes reasoning/developer/system material. Natural-language lexical
candidate generation derives `simple`-configuration lexemes with PostgreSQL,
deduplicates them, and ORs only safely server-produced lexemes; user input never
becomes raw `tsquery` syntax. Literal mode uses the documented safe
`websearch_to_tsquery('simple', ...)` term, quoted-phrase, `OR`, and exclusion
grammar. Blank or non-indexable input is invalid rather than a successful empty
search.

Hybrid ranking uses unweighted reciprocal-rank fusion over bounded lexical and
semantic candidate lists. For output limit `n`, each component depth is
`min(1000, max(100, 5*n))`; ranked output limits are restricted to 1–200. With
one-based component ranks and `k=60`, each present component contributes
`1/(60+rank)`. Exact rational arithmetic and canonical-locator tie-breaking make
fusion deterministic. Raw PostgreSQL `ts_rank_cd` and inner-product scores are
exposed diagnostically but are not normalized into a shared pretend scale.
Component ranks, winning lexical occurrence/semantic chunk, fusion parameters,
filters, and deterministic tie-breakers are returned in JSON. Exact resolution
and exhaustive enumeration never use hybrid ranking.

The exhaustive unit is one persisted searchable content row: one prose row per
logical message or one tool-name/input/result row. Exhaustive output orders by
canonical locator, fixed content-class order, ordinal, and digest, and reports
every such row exactly once through the selected generation's watermarks. Ranked
literal and hybrid modes instead collapse winning occurrences/chunks to one
logical message before their component limits.

### Provenance correlation

Search-chats reads the producer-owned versioned contract-metadata and evidence views through its
least-privilege PostgreSQL role in one read-only `REPEATABLE READ` snapshot. It
requires the exact supported contract version/checksum, view identifier,
columns/types, normalization, and grants. Receipt authority and search writes
share a server but not ownership; search migrations and maintenance cannot
address the producer schema, while the view exposes only producer-committed
normalized evidence.

For a native user record, the consumer applies the receipt contract's
`utf8-nfc-lf-v1` normalization and selects compatible confirmed candidates by
provider, session when present, exact cwd, exact repository whenever the receipt
carries one, digest, normalized lengths, and
bounded causal time. A native record may set
`submitted_by=identified_harness` only when it has exactly one compatible receipt
and that receipt has exactly one compatible native record in the bounded corpus.
All other compatibility graphs remain `unknown`; the consumer does not guess a
maximum matching.

The 2026-08-11 controlled-send seam test captured an attempt at
`2026-08-11T06:59:39.931721Z` and the one canonical native user record at
`2026-08-11T07:00:11.376Z`: a known-positive interval of 31,444,279 microseconds.
It therefore falsifies the draft 30-second upper bound. The producer contract must
freeze a replacement causal interval from reviewed evidence; search consumes that
value and does not choose an independent cutoff. Time remains part of causal
ordering and ambiguity control alongside exact identity, bindings, digest, lengths,
and mutual uniqueness. The evidence locators are the producer session at line 4519
of `/home/brian/.codex/sessions/2026/08/10/rollout-2026-08-10T15-46-39-019fea35-7485-7280-adc0-47393145d1cf.jsonl`
and lines 1, 9, and 10 of
`/home/brian/.codex/sessions/2026/08/11/rollout-2026-08-11T16-56-12-019fef9b-7cad-7193-b51c-b3f45f49be5e.jsonl`.
Line 9 is the canonical `response_item/message/role=user`; line 10 is its
`event_msg/user_message` projection, not a second logical message. Producer and
consumer must also share one reviewed path
derivation/normalization fixture; neither infers symlink, worktree, nonexistent,
or lexical-path handling from the other repository's implementation.
Native collaboration/Agent/SendMessage/local-mail records use their own positive
metadata. There is no text similarity fallback and no `role=user` shortcut to
`human`.

The initial audited native schemas provide positive harness/agent provenance but
no sufficiently strong human-positive field. The closed `human` value is
reserved for a future producer/native signal with an explicit contract; initial
adapters do not emit it merely because a record appeared in a primary session.

Historical correlation is a separately reported best effort over existing
evidence. Future producer receipts improve positive harness attribution but do
not change the conservative cardinality rule. Receipt producer implementation,
availability policy, and PostgreSQL DDL remain outside this repository. A
producer-owned importer is required only for a deployment that positively
identifies an older receipt authority; none is deployed here.

Receipt evidence changes independently of message text, so provenance assessment
has its own immutable revision and singleton current pointer; promotion of that
pointer does not advance the FTS or semantic revision. The native classification
stored with a message version remains authoritative; an assessment records
effective classification, evidence rows, both native-to-receipt and
receipt-to-native compatibility cardinalities,
receipt-authority status, and the exact database/schema-version/snapshot
observation used. If the receipt schema is currently absent, inaccessible,
incompatible, incomplete, or malformed, a new current provenance revision
records unknown for receipt-derived attribution; prior evidence remains stale
diagnostic history but cannot currently upgrade `submitted_by`. Native
positive evidence remains effective. A provenance revision targets one FTS
revision. If FTS advances first, consumers report provenance stale and use only
the selected message versions' native classification until a complete matching
provenance revision is promoted; they never join prior receipt-derived upgrades
across that mismatch and never block literal search on receipt refresh.

The current deployment has no receipt authority: the producer changes remain
uninstalled and the formerly proposed fixed SQLite file does not exist. The
producer therefore creates PostgreSQL as the clean initial authority, validates
its writer/contract/evidence-view path, and only then enables receipt-required opaque
sending; search enables correlation after the reviewed views exist. There is no
SQLite import, freeze, rollback file, or dual-authority interval. If another
deployment later positively identifies an existing receipt authority, activation
stops for a separately reviewed producer-owned migration plan; search never
invents or owns that importer.

### Progress, terminal state, and errors

The canonical progress stream is NDJSON on stderr. JSON result mode and
non-TTY execution always use it; interactive human mode may render the same
typed events compactly, and an explicit progress option can force NDJSON.
Stdout remains the final human result or one valid JSON object. Events share
these stable fields:

```text
schema_version, sequence, event, run_id, phase, state, elapsed_ms,
completed_units, total_units, owner, fts_revision, semantic_revision,
source_watermark, warning, error, coverage, refresh, semantic
```

Phases are `scan`, `parse`, `fts_commit`, `model_preflight`, `model_load`,
`semantic_embed`, `semantic_commit`, `query_embed`, `retrieve`, and `done`.
Long phases emit a heartbeat at least every five seconds. Refresh waiters use
database notifications only as wakeups and re-read transactional run/lock state;
session advisory-lock ownership, not heartbeat age, decides ownership. Every
command emits exactly one terminal event. In JSON result mode it precedes one
final JSON object, and their terminal status, revisions, coverage, and semantic
state agree so a caller does not need retained stderr to interpret results.
Human result mode derives its final presentation from the same terminal state.

Errors have stable names in JSON and distinct nonzero process outcomes where a
caller must branch: invalid invocation/locator, source unavailable, no match,
multiple matches, stale state, unsupported schema, semantic unavailable, and
internal failure. Literal success after a prior semantic failure is an ordinary
success whose diagnostics still report semantic state.

## Existing Patterns Preserved and Changed

- Preserve argparse, functional-core/imperative-shell separation, read-only
  native sources, integrity checks, current local-first/project scoping, and
  additive JSON evolution.
- Replace Claude-only discovery/parsing with provider adapters and provider-
  qualified compound identity.
- Replace the application-owned SQLite/FTS5 index with a dedicated PostgreSQL 18
  database using PostgreSQL full-text search and pgvector. Keep the legacy index
  intact until independent cutover checks pass, then remove its runtime path.
- Replace transient reasoning-inclusive `--everything` with persistent,
  explicitly requested lexical tool search; reasoning remains excluded.
- Keep literal operation available without semantic dependencies. Psycopg becomes
  the core storage dependency; the Python pgvector adapter, NumPy, and CUDA/model
  packages remain in an explicit semantic extra.
- Rebuild from native logs into a new externally tablespaced schema rather than
  forcing the current UUID-primary-key database through an unsafe row migration.
- Insulate repository discovery from ambient `GIT_DIR` by clearing Git routing
  variables or using explicit `git -C <candidate>` calls for every probe.

## Alternatives Considered

### Shared PostgreSQL 18 with pgvector — selected

PostgreSQL supplies transactional revision promotion, crash recovery,
session-scoped advisory locks, mature migrations, full-text search, and filtered
vector queries in one authority. The existing host cluster has been upgraded
in place to PostgreSQL 18.4 with pgvector 0.8.6; the search database and large
relations are isolated in a dedicated external tablespace. This choice makes the
local PostgreSQL daemon a search dependency and adds cluster ownership,
authentication, backup, restore, and tablespace operations to acceptance. Those
costs are explicit rather than reimplemented through SQLite leases, WAL recovery,
and bespoke immutable-file promotion.

### SQLite/FTS5 with immutable vector files

This retains a daemonless literal path and the current standard-library runtime,
but requires application-owned writer leasing, heartbeat recovery, WAL/checkpoint
discipline, filesystem generation promotion, cross-store snapshot coordination,
and filtered exact-vector joins outside SQL. It also retains SQLite as the
bottleneck the operator specifically does not want for this workload. Rejected
in favour of PostgreSQL. A separate SQLite receipt authority was also rejected:
logical ownership does not require another database engine, and every opaque
transport instead writes the producer-owned PostgreSQL schema.

### FAISS approximate index

FAISS is mature and substantially reduces large-corpus search cost, but adds a
second native runtime and approximate-index tuning before the measured corpus
requires it. Filtering, incremental deletion, and deterministic rebuilds add
complexity. Deferred until the exact backend's real cold/warm benchmark fails an
observed usability need.

### Dynamic Qwen fallback

Qwen and Nemotron embeddings are incompatible spaces. A transparent fallback
would require a complete second semantic revision and would still not make an
8B model fit when Nemotron does not. Rejected by explicit product decision:
Nemotron only, with loud literal-search fallback.

### Long-lived embedding daemon

A resident process would reduce model-load latency but reserve roughly the model
weight footprint on the shared 4090 and interfere with other work. Rejected for
the initial design. Per-command loading exposes progress and releases VRAM on
exit.

### Retained full snapshots — rejected

Full snapshots make staging simple, but every successful refresh permanently
copies the corpus, aliases, and vectors even though search reads only one
selected generation. Keeping one prior full copy would still duplicate nearly
the entire data set and is unnecessary for crash safety: PostgreSQL MVCC plus
changed-source staging keeps the old committed rows visible until the short
publication transaction commits. Generation metadata and quarantined migration
tables provide the temporary recovery evidence without becoming a retention
policy.

## Implementation Phases

1. **Normalized storage and reversible migration.** Add disposable-PostgreSQL
   tests that expose snapshot multiplication, then implement the migration
   ledger, generation metadata, canonical current message/alias relations,
   reusable embedding storage, and candidate-copy validation. Leave deployed
   snapshot tables quarantined until later acceptance; provide a separate prune
   operation whose dry-run names exact relations, selected counts, and expected
   reclaimed allocation.
2. **Incremental multi-root refresh.** Add failing unchanged, append,
   partial-tail, truncation, replacement, conflict, unreadable-source, and
   cross-root collision fixtures. Implement configured source collections,
   standard and Ponytail defaults, per-file checkpoints, parser-state
   serialization, suffix reads, changed-source staging, and atomic merge. This
   phase leaves `index` idempotent without copying unchanged rows.
3. **Search freshness and semantic reuse.** Make search run the lightweight
   discovery/refresh boundary before retrieval. Add failure and concurrency
   tests for one refresh owner, waiters, active writers, literal availability
   after semantic failure, missing-vector resume, semantic generation matching,
   and unreachable-vector cleanup. This phase leaves a newly appended known
   message searchable without a separate manual index command.
4. **Cross-vendor consumer contract.** Complete the shared identity and JSON-v2
   shapes for search, resolve, context, extract, list, and reference-only output;
   enforce primary/prose defaults; add `--agents`, `--tools`, and literal
   `--exhaustive`; reject `--everything`; verify exact resolution against native
   sources; and emit coverage, progress, freshness, and named failure outcomes.
   This phase owns AC1, AC2, AC5, AC6, and AC7 wherever the current provisional
   CLI remains incomplete.
5. **Operator and project truth.** Rewrite `CLAUDE.md` against the implemented
   PostgreSQL, cross-vendor, semantic, multi-root architecture; update README,
   service configuration, database architecture, migration/prune runbook, and
   structured status output. Authorship remains explicitly deferred.
6. **Production cutover and acceptance.** Run all mechanical gates and the
   disposable migration suite, then follow the project release gate: accepted
   commit, exact clean `main`, exact non-editable global installation, production
   candidate migration, count/integrity checks, and positive standard/Ponytail
   Claude/Codex literal and semantic UAT. Only after those checks and human UAT
   accept the new behavior may the prune operation drop superseded snapshot
   tables. Recheck row counts, relation sizes, freshness, and positive searches
   after pruning.

Every phase follows red-green-refactor against provider fixtures and disposable
PostgreSQL 18 clusters initialized in test temporary directories with the
packaged `vector` extension. Tests never use the operator's live cluster and do
not require Docker. GPU/model tests are separately marked so the deterministic
unit suite never requires a downloaded model. Production pruning is an
acceptance-gated operation, not a unit-test side effect.

## Additional Considerations

- Relocating the 7.2 GiB native logs is a separate operational task. The locator
  scheme tolerates changed physical roots, but provider-client symlink/bind-mount
  behaviour must be tested with writers stopped before moving live data.
- The current external storage has enough space for this feature but is not an
  unlimited sink. External peak-space checks must account for model files,
  current relations and indexes, staged replacement rows, index construction,
  PostgreSQL temporary spill, and safety margin. Cluster-root monitoring must
  separately account for PostgreSQL WAL retained during bulk loading.
- Search-schema migration, rollback, and pruning are schema-qualified and cannot
  address the message-attribution quarantine or any deferred comms-plumbing
  schema.
- Exact pgvector performance is a measured assumption, not a permanent
  constraint. A later HNSW index is permitted only after the benchmark identifies
  a real need and filtered recall/latency tests establish safe settings. The
  candidate is built first in an isolated production-sized clone outside the
  migration ledger and removed on rejection. Migration 0006 is written only
  after that gate passes; a failed target apply is repaired by a new compensating
  migration, never by editing applied migration bytes or leaving an orphaned
  candidate index.
- Source freshness is always bounded by reported watermarks. No finite search can
  truthfully include records created after its snapshot while a supervisor keeps
  writing.
- The tool reports classifications and evidence; it does not infer intent,
  summarize conversations, or decide which agent statements are trustworthy.
