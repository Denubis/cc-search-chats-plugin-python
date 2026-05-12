# Changelog

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
