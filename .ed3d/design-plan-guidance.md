# Design Plan Guidance

## Domain Context

This is a Claude Code plugin that provides context recovery and cross-referencing of chat session history. It reads JSONL files that Claude Code writes to `~/.claude/projects/`. The primary use cases are:

1. **Recovery from compression failures** — when Claude Code compresses conversation context and important information is lost, retrieve it from the underlying JSONL session files
2. **Cross-referencing within a session** — look back at earlier parts of the current conversation after compression has occurred
3. **Cross-referencing across sessions** — find relevant prior conversations within the same project or across projects

## Architectural Constraints

- **Read-only access to chat data.** Never write to `~/.claude/projects/`. Treat session files as an external data source.
- **Functional Core / Imperative Shell.** Pure functions for parsing, searching, and formatting. Side effects (file I/O, CLI output) at the boundaries only.
- **Dual interface.** Must work as both a standalone CLI via `uvx` (the primary invocation) and as a Claude Code plugin command. The CLI is the primary interface; the plugin command wraps it. Package must define a console script entry point in `pyproject.toml`.
- **Subagent-friendly.** Search operations should be delegable to sonnet-class subagents via the Task tool. Design APIs that return structured data suitable for subagent consumption.

## Technology Requirements

| Category | Required | Notes |
|----------|----------|-------|
| Language | Python 3.14+ | Modern idioms, type hints, t-strings for safe output formatting |
| Package manager | uv | For dependency management and virtual environments |
| Testing | pytest | Property-based testing with Hypothesis where appropriate |
| Linting | ruff | Format and lint |
| Type checking | ty | Strict mode |
| CLI framework | argparse (stdlib) | Zero dependencies — critical for fast `uvx` cold starts |
| Data models | dataclasses (stdlib) | For JSONL record models |
| Search index | sqlite3 FTS5 (stdlib) | JIT indexing into SQLite for full-text search |

## Technology Preferences

- **Zero external dependencies.** The package must install and run with no pip dependencies. This is a hard constraint for `uvx` invocation performance.
- SQLite FTS5 for search indexing (stdlib `sqlite3` module)
- No async unless there's a clear performance justification
- Graceful degradation on malformed JSONL records (skip, don't crash)

## Forbidden

- No JavaScript or TypeScript in the plugin implementation
- No shell scripts as primary implementation (the bash script is being replaced)
- No web frameworks or HTTP servers
- No modification of Claude Code's internal files or directories

## Stakeholders

- **Primary user:** Developer using Claude Code who needs to recover context or cross-reference sessions
- **Secondary consumer:** Claude Code subagents (sonnet) that search on behalf of the user during active sessions

## Plugin Conventions

- Commands defined in `commands/*.md` with YAML frontmatter
- Agents defined in `agents/*.md` with YAML frontmatter
- Skills defined in `skills/*/SKILL.md` with YAML frontmatter
- Plugin manifest in `.claude-plugin/plugin.json`
- The plugin command should delegate to the Python CLI, not reimplement logic
