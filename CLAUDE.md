# cc-search-chats-plugin-python

Last verified: 2026-06-12

## Overview

Claude Code plugin for context recovery and cross-referencing of chat history. Searches JSONL session files stored by Claude Code in `~/.claude/projects/`.

**Status:** Python 3.14+ package with Claude Code plugin wrapper. Provides context recovery and cross-referencing of chat history via SQLite FTS5 indexing. Forked from pcvelz/cc-search-chats-plugin.

## Tech Stack

- Language: Python 3.14+
- Package manager: uv
- Testing: pytest, hypothesis
- Linting: ruff
- Type checking: ty
- Plugin framework: Claude Code plugin system
- Zero runtime dependencies (stdlib only)

## Commands

- `cc-search-chats` — Primary CLI invocation (install via `uv tool install` from git or editable local)
  - `search "query"` — full-text search; current project first, auto-broadens to all indexed projects on a miss. `--all` forces machine-wide; `--project PATH` narrows (never broadens); `--everything` runs a live full-content scan including thinking + tool I/O (not persisted)
  - `extract [SESSION_ID]` — extract conversation (auto-discovers if no ID)
  - `list` — list sessions
  - `context UUID` — show context around a message
  - `index` — rebuild the current project's index; `index --all` incrementally indexes every project under `~/.claude/projects/`
  - All subcommands accept `--json`. Every payload is an extensible object carrying `schema_version` (currently 1): `search` → `.results`, `list` → `.sessions`, `extract` → `.epochs`, `context` → `.target`/`.before`/`.after`
- `uv run pytest` — Run tests
- `uv run ruff check .` — Lint
- `uv run ruff format .` — Format
- `uv run ty check` — Type check

## Project Structure

- `src/cc_search_chats/` — Python package
  - `cli.py` — Imperative shell: argparse entry point
  - `output.py` — Output formatting (t-string based, human + JSON)
  - `core/` — Functional core
    - `models.py` — Data models (SessionRecord, CompactEvent, SessionMeta)
    - `parser.py` — JSONL parser (parse_record, parse_session)
    - `discovery.py` — Path encoding, session discovery, ranking
    - `search.py` — FTS5 query builder
  - `storage/` — SQLite persistence
    - `schema.sql` — DDL for all tables and FTS5 indexes
    - `index.py` — Database operations (indexing, search, extraction)
- `commands/search-chat.md` — Claude Code slash command (delegates to `cc-search-chats` CLI)
- `skills/search-chat/SKILL.md` — Progressive search workflow skill
- `tests/` — pytest test suite
- `.claude-plugin/` — Plugin manifest and marketplace config
- `docs/` — Design plans and implementation plans

## Architecture

- **Functional Core / Imperative Shell**: Pure functions for parsing and search query building in `core/`. Side effects (database, filesystem, CLI I/O) at the edges in `cli.py`, `storage/`, and `output.py`.
- **t-string output formatting**: Uses PEP 750 t-strings for safe string interpolation in output formatters.
- **SQLite FTS5 indexing**: JIT (just-in-time) indexing — the current project's sessions are indexed on first access, updated incrementally by mtime. `index --all` seeds/refreshes a global index across all projects (cheap to re-run, e.g. from cron).
- **Cross-project search**: search is local-first and broadens to the whole index on a local miss (`scope` = local / widened / all). User queries are sanitised before reaching FTS5 (`sanitize_fts5_query`), so punctuation never injects query syntax.
- **Ephemeral full-content search**: `--everything` builds a throwaway in-memory index over in-scope sessions' full content (thinking + tool I/O via `parse_session(full_content=True)`), searches it, then discards it — nothing extra is persisted.
- **Epoch model**: Messages segmented by `compact_boundary` events. Epoch 0 = pre-compression content.

## Claude Code Chat Data Format

Sessions are stored as JSONL files at `~/.claude/projects/<encoded-path>/<session-uuid>.jsonl`. The encoded path replaces every non-alphanumeric character (`/`, `.`, spaces, ...) with `-` in the project's absolute path — e.g. `/home/brian/.worktrees/x` → `-home-brian--worktrees-x`. Each line is a JSON object with `sessionId`, `timestamp`, `cwd`, and `message` (containing `role` and `content`). Content can be a string or an array of text/tool_use objects.

## Conventions

- Functional Core / Imperative Shell: pure functions for parsing and searching, side effects at the edges
- Claude Code plugin conventions: commands in `commands/*.md`, skills in `skills/*/SKILL.md`
- Design-first: use starting-a-design-plan workflow before implementation
- Zero external dependencies: stdlib only for runtime, dev deps in dependency-groups
- JSON output contract: every `--json` payload is an extensible object carrying `schema_version` (`output.SCHEMA_VERSION`). Evolve additively — add fields, never remove/rename/reshape — and bump `SCHEMA_VERSION` only on an unavoidable break. The search-chat skill asserts the version, so a CLI/plugin mismatch fails loudly instead of mis-parsing.

## Boundaries

- Never touch: `~/.claude/projects/` data files (read-only access to chat history)
