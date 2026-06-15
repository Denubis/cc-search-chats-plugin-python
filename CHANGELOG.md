# Changelog

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
