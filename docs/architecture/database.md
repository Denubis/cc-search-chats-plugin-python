# Database architecture

Last verified: 2026-08-24

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

Applied migration bytes are immutable. A future schema change is a new ordered
resource plus a ledger test.

## Current relations

| Relation | Cardinality/key | Visibility and retention |
|---|---|---|
| `source_root_current` | one per provider/resolved configured path | current configured roots only |
| `source_file_current` | one per root/relative file | current metadata, complete-record watermark, parser state, status |
| `message_current` | one per provider/session/logical message/content class | current canonical visible content and generated FTS vector |
| `physical_alias_current` | one per real root/file/record/content-class occurrence | exact coordinates/digest; cascades with message |
| `corpus_state` | singleton | selects current corpus generation metadata |
| `corpus_revision` | one small row per changed publication | counts, watermarks, terminal state; no message copies |
| `refresh_run` | one per attempted changed refresh | progress/diagnostics; terminal rows retained to newest 100 |
| `embedding_profile` | one per embedding/chunker contract | model snapshot, prefixes, pooling, dimensions, normalization, attention, token budgets |
| `semantic_chunk_current` | one per current message/profile/chunk ordinal | source digest, token/character bounds, passage and prefixed-input digest |
| `embedding_value` | one per profile/prefixed-input digest | reusable `vector(1024)`; unreachable rows reclaimed after publication |
| `semantic_state` | singleton | selects one complete semantic generation |
| `semantic_revision` | one small row per corpus/profile attempt | progress/failure and coverage counts; no vector copies |
| `legacy_snapshot_inventory` | at most one migration record | quarantined selected snapshot inputs until approved prune |
| `cutover_validation` | one per production candidate | exact installed commit and human acceptance evidence |

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

## Refresh publication

Discovery stats every configured session file. Unchanged sources do not read
JSONL bytes. Same-device/inode growth starts at the last complete-record byte
and parser-state watermark. Truncation, replacement, same-size modification, or
parser-state version change reparses that source from byte zero.

Changed records/checkpoints stage in connection-local temporary relations. A
short publication transaction validates conflicts, upserts only changed
canonical rows/aliases, removes vanished aliases and orphan messages, advances
checkpoints, and selects a new `corpus_revision`. PostgreSQL MVCC exposes either
the old or new committed state. A no-op creates no generation and changes no
current row versions.

One session advisory owner serializes refresh/semantic work. Scanning, parsing,
tokenization, model loading, and embedding occur without a long write
transaction. Independent heartbeat connections expose progress; database
session death releases ownership.

## Semantic publication and retrieval

Only nonblank visible prose is eligible. The pinned tokenizer splits each
logical message independently into 768-target-token chunks with 96-token
overlap and a 1,024-token hard input ceiling including `passage: ` and special
tokens. Chunk rows record ordinal, token/character bounds, source-text digest,
passage, chunker ID, and SHA-256 of the exactly prefixed model input.

On corpus change, only absent, source-digest-stale, or wrong-chunker rows are
regenerated. Embedding inserts only missing normalized finite vectors.
Publication rechecks the corpus, requires current-profile chunks for every
eligible message, rejects extra stale-profile rows, and requires every current
chunk to join one vector before selection. Failure leaves literal state current
and semantic state stale for the new corpus. Retry reuses already validated
chunk vectors.

Semantic retrieval validates selected corpus/profile freshness, applies filters
before ranking, uses exact inner product over normalized vectors, and keeps the
best chunk per logical message. Hybrid retrieval fuses bounded literal and
semantic components with exact reciprocal-rank-fusion arithmetic.

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
