# Changelog

## cc-search-chats 2.0.2

Prevents exact-locator integrity checks and concurrent indexing from creating
an I/O stampede on large PostgreSQL corpora.

**New:**
- `resolve --stdin` resolves ordered newline-delimited locators through one
  process, connection, and database operation with independent result counts.
- Packaged systemd user units provide a persistent, dephased nightly refresh.

**Changed:**
- PostgreSQL CLI work uses blocking single-flight admission instead of retry
  loops; killed clients release both local and database advisory locks.
- Every indexing mode runs at low CPU and idle I/O priority with bounded memory
  and task counts.

**Fixed:**
- Exact canonical and physical-alias lookups use revision-scoped indexes and
  deduplicate narrow identities before fetching message bodies. The executed
  PostgreSQL 18 regression plan writes no temporary blocks and does not scan
  either large relation.
- One database advisory owner now spans literal refresh and semantic embedding,
  preventing overlapping rebuild phases.

## cc-search-chats 2.0.0a7

Routes Codex chat-history commands through the configured host approval boundary
and packages the shared search skill for deliberate Codex discovery.

**New:**
- The repository ships Codex marketplace metadata, search-skill discovery
  metadata, a prompt-level host-routing rule, and an idempotent rule installer.
- Packaging tests verify the Codex marketplace, skill metadata, approval rule,
  and safe rule installation behavior.

**Changed:**
- Codex search instructions request the configured host approval route on the
  first attempt instead of treating a sandbox's PostgreSQL, D-Bus, GPU, or model
  cache visibility as a host fact.
- A CUDA-unavailable error now identifies its evidence as process-scoped and
  gives sandboxed callers the host-approval remedy before literal fallback.

## cc-search-chats 2.0.0a5

Fixes indexing, which never finished on large corpora, and surfaces each
session's true filesystem path.

**New:**
- `list` and `search` display the session's real filesystem path, recovered
  from each session's `cwd`, because the `~/.claude/projects` directory
  encoding is lossy and cannot be reversed. The lossy `project_path` remains
  the filter/group key, and `real_project_path` is an additive `--json`
  field.

**Fixed:**
- `index` / `index --all` no longer runs an unconditional corpus-wide TF-IDF
  keyword recompute after every build. On large corpora that pass took hours
  and made indexing appear to hang, and nothing read its output. The keyword
  feature is removed entirely (both call sites, `compute_epoch_keywords` /
  `update_all_keywords`, the `message_fts_vocab` vtable, and the
  `epoch_summary.keywords` column). `index --all` now completes in ~2s.

## cc-search-chats 2.0.0a4

Hardens the `--json` contract so the CLI and plugin cannot silently
mis-parse when their versions drift.

**New:**
- Every `--json` payload now carries a `schema_version` field
  (`output.SCHEMA_VERSION`). The `/search-chat` skill asserts it and tells
  the user to reinstall the CLI on a mismatch, rather than mis-parsing.

**Changed:**
- **Breaking:** `list --json` now returns an object
  `{schema_version, sessions}` instead of a bare array — read the
  `sessions` array. This was the last array-shaped output; all `--json`
  payloads are now extensible objects, so future additions are additive
  and non-breaking.

## cc-search-chats 2.0.0a3

Cross-project search and full-content scanning, plus a search-query crash fix.

**New:**
- `index --all` incrementally indexes every project under
  `~/.claude/projects/`, building one global index. It is mtime-incremental,
  so re-runs are cheap (suitable for cron / a systemd timer — recipes in the
  README).
- `search` is now local-first and automatically broadens to all indexed
  projects on a local miss. `--all` forces a machine-wide search up front;
  `--project PATH` narrows to one project and never broadens.
- `search --everything` runs a live full-content scan including thinking
  blocks and tool inputs/outputs, via a throwaway in-memory index — nothing
  extra is persisted.

**Changed:**
- **Breaking:** `search --json` now returns an object
  `{scope, searched_project, project_count, results}` instead of a bare
  array. Consumers must read the `results` array. The bundled `/search-chat`
  command and skill are updated to match — update the plugin and the CLI
  together.
- Search results carry their originating `project_path`; human output labels
  the project when results span more than the current one.

**Fixed:**
- Free-text queries containing punctuation (e.g. `0.90`, `pole:`) no longer
  crash FTS5 with `fts5: syntax error`. User input is sanitised into quoted
  FTS5 terms before matching, closing a query-injection surface.

## cc-search-chats 2.0.0a2

Fixes a crash that made indexing unusable on real Claude Code session files
containing duplicate message UUIDs.

**Fixed:**
- `reindex_project` and `jit_reindex` no longer raise
  `sqlite3.IntegrityError: UNIQUE constraint failed: message.uuid` when a
  session JSONL contains the same message UUID more than once. This occurs
  in practice when Claude Code rewrites byte-identical records into the same
  JSONL after a resume or replay by a newer CLI version. `INSERT OR IGNORE`
  is now used for both the `message` and `compact_event` INSERTs;
  first-seen wins, AFTER INSERT triggers stay consistent on conflict, and
  re-indexing is fully idempotent.
- Added regression tests in `tests/test_indexing.py::TestDuplicateUuidHandling`
  covering intra-file message duplication, cross-session UUID sharing, and
  intra-file `compact_boundary` duplication.

## cc-search-chats 2.0.0a1

Initial Python port of the bash-based `cc-search-chats` plugin. SQLite FTS5
indexing of `~/.claude/projects/` session JSONLs with search, extract, list,
context, and index subcommands. Functional Core / Imperative Shell layout
and stdlib-only runtime dependencies.
