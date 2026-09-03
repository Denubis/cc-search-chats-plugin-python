# Database architecture

Last verified: 2026-09-03

## Authority and ownership

Native Claude Code and Codex JSONL session files are the content authority.
`cc_search_chats` is a rebuildable PostgreSQL projection for discovery
checkpoints, canonical visible content, exact source coordinates, full-text
search, reusable semantic vectors, and bounded run diagnostics. The application
reads provider roots and writes only its PostgreSQL schema; it never writes or
locks native logs.

The database, role, pgvector extension, tablespaces, filesystem mount, backup,
and credentials are operator-owned. Application migrations create
`cc_search_chats` schema objects but do not create or relocate those resources.

## Migration authority

`storage/postgresql/migrations.py` applies packaged SQL resources in numeric
order under a PostgreSQL advisory transaction lock. `schema_migration` stores
the resource name and SHA-256 checksum. Changed applied bytes, an unknown
recorded version, or a failed later resource aborts without advancing the
ledger.

| Version | Resource | Responsibility |
|---:|---|---|
| 1 | `schema.sql` | canonical messages/aliases, generations, profiles, vectors, legacy inventory |
| 2 | `refresh_schema.sql` | roots, per-file checkpoints, refresh runs |
| 3 | `freshness_schema.sql` | owners, phases, heartbeats, completed/total units |
| 4 | `coverage_schema.sql` | actual read and removed-source counts |
| 5 | `semantic_chunk_schema.sql` | exact chunk profile, chunk coordinates, retirement of whole-message mappings |
| 6 | `incremental_refresh_schema.sql` | failed-observation fingerprints, truthful attempted work, and the state table retired by migration 9 |
| 7 | `coherent_corpus_schema.sql` | corpus-generation/semantic-build naming and one jointly selected coherent corpus |
| 8 | `skipped_record_coverage_schema.sql` | durable count of skipped unstorable records per source checkpoint |
| 9 | `drop_auto_refresh_state_schema.sql` | retirement of the automatic-refresh state table |
| 10 | `coherent_selection_guard_schema.sql` | symmetric update guards for the selected corpus-generation/semantic-build pair |

Applied migration bytes are immutable. A future schema change is a new ordered
resource plus a ledger test.

## Current relations

| Relation | Cardinality/key | Visibility and retention |
|---|---|---|
| `source_root_current` | one per provider/resolved configured path | current configured roots only |
| `source_file_current` | one per root/relative file | current metadata, complete-record watermark, parser state, status |
| `message_current` | one per provider/session/logical message/content class | current canonical visible content and generated FTS vector |
| `physical_alias_current` | one per real root/file/record/content-class occurrence | exact coordinates/digest; cascades with message |
| `corpus_state` | singleton | selects one completed coherent corpus generation |
| `corpus_generation` | one small row per changed candidate | counts, watermarks, selected semantic build, terminal state; no message copies |
| `refresh_run` | one per attempted changed refresh | progress/diagnostics; terminal rows retained to newest 100 |
| `source_failure_current` | zero or one per current source | deterministic fingerprint or transient retry boundary without advancing the successful checkpoint |
| `embedding_profile` | one per embedding/chunker contract | model snapshot, prefixes, pooling, dimensions, normalization, attention, token budgets |
| `semantic_chunk_current` | one per current message/profile/chunk ordinal | source digest, token/character bounds, passage and prefixed-input digest |
| `embedding_value` | one per profile/prefixed-input digest | reusable `vector(1024)`; unreachable rows reclaimed after publication |
| `semantic_build` | one small row per corpus/profile attempt | owning corpus generation, progress/failure and coverage counts; no vector copies |
| `legacy_snapshot_inventory` | at most one migration record | quarantined selected snapshot inputs until approved prune |
| `cutover_validation` | one per production candidate | exact installed commit and human acceptance evidence |

The selected pair is guarded on both sides. Deferred, share-locked guards reject
any single transaction, and any interleaving of a selector and a demoter, that
would leave the selected pair incoherent. Key changes and deletes are refused by
the foreign keys. A delete-and-reinsert of a build key in one transaction is not
detected and is forbidden by the maintenance runbook.

Migration 5 drops the retired `message_embedding_current` whole-message
mapping. Chunks join directly to `embedding_value` by profile/input digest, so
there is no second vector or membership copy.

## Identity and exact resolution

Canonical identity is provider-qualified and root-independent. Equal native
observations share one `message_current` row while each real occurrence retains
a `physical_alias_current` row with its internal root ID and exact relative
byte/record coordinates. Conflicting content for one identity aborts
publication.

Public JSON omits the internal root ID but carries the canonical locator and
verified physical coordinates. Exact resolution parses the locator, checks
indexed identity/aliases, reads the bounded native record, verifies its digest
and provider schema, and returns a named outcome. Ranked search is never exact
evidence.

The read-only `events` projection groups content-class rows by canonical
logical identity inside a half-open, timezone-aware window. Primary-session
user prose is positive human evidence; identified harness submissions and
non-user/non-prose messages are excluded, while user prose from unknown session
origins remains unresolved. Retained events contain no prose and are pinned to
the selected corpus generation. The exporter walks canonical content rows in
bounded primary-key pages and resolves aliases by canonical identity, avoiding
an unbounded joined aggregate. Raw content-row counts, logical-message counts,
physical-alias counts, and exclusion/unresolved reasons make replay dedupe and
the human boundary auditable.

## Refresh publication

Discovery stats every configured session file. Unchanged sources do not read
JSONL bytes. Same-device/inode growth starts at the last complete-record byte
and parser-state watermark. Truncation, replacement, same-size modification, or
parser-state version change reparses that source from byte zero.

Changed records/checkpoints stage in connection-local temporary relations.
Normal indexing prepares candidate canonical rows, aliases, semantic chunks,
and reusable vectors while the previous corpus remains selected. One short
publication transaction validates conflicts, upserts only changed current
rows, removes vanished aliases and orphan messages, advances checkpoints,
marks the corpus generation and its semantic build complete, links them, and
selects that generation. A deferred constraint rejects selection without the
generation's own completed semantic build. PostgreSQL MVCC exposes either the
old or new coherent state. A no-op creates no generation and changes no current
row versions.

A deterministic parse failure stores its observed file identity, size, mtime,
parser version, failing coordinate/code, and attempted bytes separately from the
last successful checkpoint. The same observation is a metadata-only blocked
source on later refreshes. Transient I/O failures retain retry-after/backoff;
manual force retry and changed observations invalidate the relevant boundary.

One session advisory owner serializes corpus work. `index`, invoked
intentionally by a person, an agent, or the nightly timer, is the only builder
and always composes literal and semantic publication. Search does not admit,
join, launch, or wait for index work; it opens the currently selected coherent
corpus in a repeatable-read snapshot. Scanning, parsing, tokenization, model
loading, and embedding occur without a long write transaction. Independent
heartbeat connections expose progress; database session death releases
ownership.

## Semantic publication and retrieval

Only nonblank visible prose is eligible. The pinned tokenizer splits each
logical message independently into 768-target-token chunks with 96-token
overlap and a 1,024-token hard input ceiling including `passage: ` and special
tokens. Chunk rows record ordinal, token/character bounds, source-text digest,
passage, chunker ID, and SHA-256 of the exactly prefixed model input.

On corpus change, only absent, source-digest-stale, or wrong-chunker rows are
regenerated. Embedding inserts only missing normalized finite vectors.
Publication rechecks the candidate corpus, requires current-profile chunks for
every eligible message, rejects extra stale-profile rows, and requires every
current chunk to join one vector before joint selection. Failure marks the
candidate generation/build failed and leaves the previous coherent corpus
selected. Retry reuses already validated chunk vectors.

Semantic retrieval validates selected corpus/profile completeness, applies
filters before exact inner-product ranking, and keeps the best chunk per logical
message. Hybrid retrieval fuses bounded literal and semantic components with
exact reciprocal-rank-fusion arithmetic. Query-model or semantic-query failure
returns a named `literal_fallback` from the same selected corpus.

Ranked search starts its monotonic five-second clock in the console bootstrap,
uses deadline-derived connection and statement budgets, opens the selected
result snapshot immediately, reads literal results first, and runs query
embedding in a terminable/reaped child. It reports a named deadline error only
when no literal answer can be obtained; optional semantic failure degrades the
committed literal answer.

## Storage, backup, and rebuild boundary

Large deployments should place database default/temporary tablespaces below an
operator-managed external-storage mount. The current CLI does not provision or
substitute storage. Production acceptance must positively verify catalog
locations, mount identity/read-only state, database writability, and free-space
margin before candidate refresh.

Native logs can rebuild searchable content/checkpoints through a full bounded
parse. PostgreSQL backup remains necessary to preserve migration ledger state,
computed vectors, diagnostic history, legacy quarantine, and accepted cutover
evidence.

Message attribution, receipt correlation, rendered archives, summaries, and
project-note authorship are not read, migrated, or written by this schema.
