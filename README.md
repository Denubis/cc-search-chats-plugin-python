# cc-search-chats

Search and recover context from native Claude Code and Codex chat history.

Current release: cc-search-chats 2.3.3

The CLI reads vendor JSONL session logs without modifying them, maintains a
normalized PostgreSQL search projection, and supports PostgreSQL full-text
search plus an optional local semantic model. Standard and isolated Ponytail
session roots can participate in one corpus without sharing either runtime's
configuration, credentials, caches, plugins, or other state.

## Installation

The plugin supplies the agent workflow; it invokes an independently installed
`cc-search-chats` executable. Python 3.14+, PostgreSQL 18, and pgvector are
required.

For a complete laptop deployment, follow the tracked
[laptop deployment runbook](docs/runbooks/laptop-deployment.md). It has separate
paths for a clean projection rebuild and a preserving upgrade. Both install the
semantic-capable CLI from an exact accepted Git commit, prove the installed
provenance, configure PostgreSQL, install the Codex host-routing rule and both
plugins, and activate scheduled maintenance only after positive smoke checks.

## PostgreSQL Setup

The default connection is libpq service `cc_search_chats`. The application
creates only its schema objects; database, role, pgvector, credentials,
tablespaces, mounts, backup, and reset decisions belong to the operator. The
laptop runbook owns their deployment order, while the
[PostgreSQL maintenance runbook](docs/runbooks/postgresql-index-maintenance.md)
owns database-specific migration, recovery, and pruning checks.

## Semantic Runtime

Hybrid search uses local snapshot
`nvidia/Nemotron-3-Embed-8B-BF16@c44c20ab3f6b430336706847a6372de4b2eb3dbd`.
The snapshot must already be present in the configured Hugging Face cache, or
`CC_SEARCH_MODEL_PATH` must name that exact snapshot directory. Runtime commands
use `local_files_only=True`, do not transmit chat text, and do not download a
model or redirect package/model caches.

Visible prose is split inside each logical message into tokenizer-aware chunks:
768 target content tokens, a 1,024-token hard input ceiling including prefix and
special tokens, and 96-token overlap. Each prefixed chunk digest maps to one
reusable normalized 1,024-dimensional vector. Retrieval keeps only the best
chunk per logical message before hybrid rank fusion.

The first semantic search in a burst starts an ad hoc same-user helper and takes
about ten seconds while it loads the model. Later semantic searches within
thirty seconds of the last query reuse that warm model and take about a second.
The helper then exits and releases the model; it has no systemd unit. Set
`CC_SEARCH_SEMANTIC_WARM_SECONDS` to tune the default thirty-second window.

Semantic search has no answer deadline. If its query-time model or helper is
unavailable, it returns its already-computed literal answer with named semantic
degradation. Literal search is the quick path and does not load the model.
Explicit semantic maintenance still exits nonzero when its required model work
cannot run.

## Quick Start

After completing the deployment runbook:

```console
# Model-ranked hybrid natural-language search over the committed snapshot
cc-search-chats search "database migration" --semantic

# Exact lexical search without the model
cc-search-chats search "schema_migration" --literal

# Include agent/unknown sessions in model-ranked hybrid search
cc-search-chats search "the earlier runner work" --semantic --agents

# Lexical tool names and inputs; reasoning, instructions, and results stay excluded
cc-search-chats search "git diff" --literal --tools

# Every matching prose/tool occurrence in deterministic order
cc-search-chats search "sentinel" --literal --tools --exhaustive --json

# Resolve and verify an exact native locator without returning message text
cc-search-chats resolve CCCHAT_LOCATOR --reference-only --json

# Export canonical human-message points from a half-open UTC window, without bodies
cc-search-chats events --from 2026-01-01T00:00:00Z --until 2027-01-01T00:00:00Z --json

# Explicit coherent corpus maintenance or read-only checkpoint
cc-search-chats index --json
cc-search-chats index --status --json
```

Search opens its repeatable-read snapshot without indexing, launching an index
service, joining index work, or waiting for publication. It reads the currently
selected coherent corpus even when newer native records exist. Every search
requires exactly one of `--literal` or `--semantic`. Literal is exact full-text
search, loads no model, and keeps the five-second invocation-to-answer
deadline. Semantic is model-ranked hybrid search over full-text and embedding
candidates and has no deadline. Human output states the requested mode first,
then the index time, current time, age, and unindexed chat count; semantic also
states whether it loaded or reused the warm model. JSON names `mode`, delivered
`retrieval_mode`, `index_state`, `corpus_generation`, `semantic_build`,
`indexed_at`, `corpus_age_ms`, and the mode-specific deadline. Run
`cc-search-chats index` intentionally when the selected corpus is too old.

Search and routine index/status commands never apply schema changes. A
`maintenance_required` result routes the operator back to the deployment and
PostgreSQL runbooks rather than authorizing an inline repair.

## Search Scope

PostgreSQL searches all configured roots by default. Filters narrow that corpus:

- `--provider claude|codex`
- `--project PATH` for exact recorded repository/cwd values
- `--role ROLE`, `--epoch N`, and `--days N`
- `--agents` for agent and unknown sessions
- `--literal --tools` for persisted tool names and inputs

Literal and semantic ranked searches return at most `--limit` results (1–200).
Semantic search fuses bounded lexical and semantic component rankings with
exact reciprocal-rank-fusion arithmetic. Only `--literal --exhaustive` claims
complete occurrence coverage; it pages through PostgreSQL and ignores the
ranked limit. `--tools` and `--exhaustive` require `--literal`.

No supported search mode indexes or returns reasoning/thinking, system or
developer instructions, injected context, or unrecognized content shapes.
`--everything` is retired and exits with migration guidance.

## Source Roots

Defaults:

| Provider | Standard | Isolated root included when present |
|---|---|---|
| Claude | `~/.claude/projects` | `~/.claude-ponytail/projects` |
| Codex | `~/.codex/sessions` | `~/.codex-ponytail/sessions` |

Plural variables replace a provider's default collection using the platform
path separator:

```fish
set -x CC_SEARCH_CLAUDE_ROOTS "$HOME/.claude/projects:$HOME/.claude-ponytail/projects"
set -x CC_SEARCH_CODEX_ROOTS "$HOME/.codex/sessions:$HOME/.codex-ponytail/sessions"
```

The singular `CC_SEARCH_CLAUDE_ROOT` and `CC_SEARCH_CODEX_ROOT` remain one-root
migration compatibility. Explicit roots fail loudly when unavailable; optional
Ponytail defaults are included only when their session directory exists.

Discovery traverses only those session directories. Equal native identities
share one canonical message and retain each genuine physical occurrence as an
alias; conflicting content for one identity aborts publication.

## Index and Storage

PostgreSQL stores one current canonical message row, one row per physical alias,
one current semantic chunk row per chunk/profile, and one vector per
profile/prefixed-input digest. Corpus generations and semantic builds contain
bounded publication, progress, and failure metadata—not copies of messages,
aliases, or vectors.

Unchanged refreshes read metadata but no JSONL content bytes and create no
generation. Same-device/inode growth reads only after the last complete-record
watermark. Partial tails remain pending; truncation, replacement, same-size
modification, and parser-version changes reparse the affected source from byte
zero. Native logs are never written or locked.

One PostgreSQL advisory owner serializes corpus work. Long phases
publish owner, heartbeat, completed/total units, and named state. Committed
state advances only when a corpus generation and its complete semantic build
publish together. Candidate failure leaves the previous coherent corpus
selected. If query-time model loading or the query helper fails, semantic
search returns `literal_fallback` from that selected corpus rather than stale
or partially mapped vectors. An index run first stops a live query helper so
the two paths do not compete for VRAM.

Deterministic source failures retain a separate observation fingerprint. An
unchanged blocked file is metadata-checked without reopening JSONL bytes;
metadata/parser changes or explicit `index --force-retry` retry it. Transient
failures retain a bounded retry time without advancing the successful source
checkpoint.

## JSON and Progress Contract

The default PostgreSQL surface emits JSON schema version 5. Each `--json`
command writes one stdout object containing:

- `schema_version`, `command`, and terminal `status`
- `coverage` with roots, a repository count, file counts, and diagnostics;
  `completeness` describes the selected generation's processing outcome and is
  partial only for a partial/failed refresh or failed, blocked, or transient
  source files
- `coverage.pending_tail_files` and `refresh.pending_bytes`, which report
  in-flight JSONL tails as staleness that is not searchable yet without making
  processing coverage partial
- `refresh.corpus_generation`, `semantic.semantic_build`,
  `semantic.corpus_generation`, and their state/progress fields; semantic
  search also reports `semantic.model_load_ms`, `semantic.query_embed_ms`, and
  `semantic.warm_reused`
- top-level `indexed_at`, clock-derived `corpus_age_ms`, and `warnings`
- search `mode` for the requested literal/semantic mode and `retrieval_mode`
  for the delivered literal, exhaustive-literal, hybrid, or literal-fallback
  result
- search and `index --status` `index_state`, including one-clock `made_at`,
  `now`, `age_ms`, selected corpus/build identity, and bounded unindexed
  file/directory/byte counts or a closed unknown reason
- ranked-search `deadline_ms`, `elapsed_ms`, and `stale_reasons`; semantic uses
  `deadline_ms: null`, while a post-retrieval literal deadline returns `status:
  partial` with `deadline_degraded` instead of discarding hits
- command-specific `results`, `sessions`, `messages`, `resolutions`, or `events`

A semantic request that cannot complete model ranking returns literal results
with `retrieval_mode: literal_fallback` and a `semantic_search_degraded` warning.
On a VRAM allocation failure, that warning includes the process-scoped
`gpu_processes` snapshot: GPU UUID, PID, executable name, used MiB, and whether
the row is the failing helper itself. If the installed `nvidia-smi` probe
cannot run, `gpu_processes_unavailable_reason` says why. Human output prints
the same consumer list before the literal results.

Search/extract/context/resolve messages carry provider-qualified canonical
identity plus verified physical source coordinates. Exact resolution statuses
are `resolved`, `no_match`, `multiple_matches`, `source_unavailable`,
`stale_source`, `stale_index`, `malformed_locator`, and
`unsupported_provider_schema`.

`events` is a read-only export from the selected corpus generation. Its required
timezone-aware `--from` and `--until` bounds are half-open. It emits no message
bodies: retained rows contain canonical locator, UTC timestamp, provider,
session kind, cwd/repository provenance, and physical-alias count. Its positive
population block separately reconciles scanned content rows and logical
messages with retained, excluded, and unresolved authorship counts. The export
and every event carry `source_corpus_generation`.

Progress never contaminates JSON stdout. JSON or non-TTY execution writes
ordered schema-v5 NDJSON events to stderr, including periodic heartbeats and
exactly one terminal event. Use `--progress human` for concise terminal text.

A direct `cc-search-chats index` preflights its exact bounded systemd user-scope
wrapper before re-exec and never indexes uncontained. If that scope cannot be
created, schema-v5 output reports `status: containment_unavailable`, error code
`systemd_scope_unavailable`, and exit 9. The remedy is: "You are probably
blocked by your sandbox: ask the user for permission to run `cc-search-chats
index` on the host through the configured approval route." The read-only
`cc-search-chats index --status` needs no scope. The packaged service supplies
`CC_SEARCH_CONTAINED=1` and its own bounds, so it does not create a nested
scope.

## Scheduled Maintenance

The distribution includes one low-priority full-index service and persistent
nightly timer. The [laptop deployment
runbook](docs/runbooks/laptop-deployment.md#install-scheduled-maintenance) owns
installation, executable-path verification, activation order, and optional
source-root configuration. Search never starts the service. The query embedder
is an independent ad hoc process that exits after its idle warm window.

## Recovery and Release Boundaries

Native logs are the rebuild authority. Applied SQL migrations are ordered and
checksummed; their bytes are immutable. Legacy full-snapshot relations remain
quarantined through migration and positive four-corpus UAT. Pruning them is a
separate human-authorized operation requiring a matching fresh dry-run,
accepted exact-commit validation, complete current semantic join, and repeated
post-prune checks. Deployment and upgrade sequencing lives in the
[laptop deployment runbook](docs/runbooks/laptop-deployment.md); see also the
maintenance runbook and
[`docs/uat/cross-vendor-search-wip.md`](docs/uat/cross-vendor-search-wip.md).

Message attribution, receipt correlation, rendered archives, summaries, and
project-note authorship are outside the search delivery.

## Development

```console
uv run --frozen pytest -q -m 'not postgresql'
uv run --frozen pytest -q -m postgresql
uv run --frozen ruff check src tests
uv run --frozen ruff format --check src tests
uv run --frozen ty check src tests
```

## Licence

MIT
