# cc-search-chats

Search and recover context from Claude Code chat history.

## The Problem

Claude Code compresses conversation context when sessions grow large. Earlier context is summarised and the original messages become inaccessible through normal conversation. This tool recovers that content by indexing the underlying JSONL session files that Claude Code stores on disk.

## Installation

### As a Claude Code Plugin (Recommended)

Add the marketplace and install:

```bash
# Add the plugin marketplace (once)
/plugin marketplace add Denubis/cc-search-chats-plugin-python

# Install the plugin
claude plugin install cc-search-chats@cc-search-chats-marketplace
```

Or from inside Claude Code, use `/plugin` and navigate to **Discover** to browse and install.

### Standalone CLI

```bash
# Install from git (requires Python 3.14+)
uv tool install git+https://github.com/Denubis/cc-search-chats-plugin-python

# Then use directly
cc-search-chats
```

No runtime dependencies -- the package uses only Python's standard library.

## Quick Start

```bash
# Find discussions about a topic
cc-search-chats search "database migration"

# Recover the most recent substantial session
cc-search-chats extract

# Get pre-compression content (epoch 0 = before compression)
cc-search-chats extract --epoch 0

# Show recent sessions
cc-search-chats list --days 7

# Context around a specific message
cc-search-chats context MESSAGE_UUID
```

## Commands

| Command | Description | Key Flags |
|---------|-------------|-----------|
| `search "query"` | Full-text search; current project first, broadens to all indexed projects on a miss | `--all`, `--everything`, `--epoch N`, `--days N`, `--project PATH` |
| `extract [SESSION_ID]` | Extract a conversation (auto-discovers if no ID given) | `--epoch N`, `--verbose` |
| `list` | List sessions with metadata | `--days N`, `--project PATH` |
| `context UUID` | Show messages around a specific message | `--depth N` |
| `index` | Reindex the current project, or every project with `--all` | `--all`, `--project PATH` |

All commands support `--json` for structured output suitable for programmatic consumption. Every payload is an object carrying `schema_version`; e.g. `search` → `results`, `list` → `sessions`.

## How It Works

Chat sessions are stored by Claude Code as JSONL files in `~/.claude/projects/`. Each file contains timestamped messages with roles, content, and metadata.

**cc-search-chats** builds a local SQLite FTS5 (full-text search) index over these files. The index is created just-in-time on first access and updated incrementally when session files change. Sessions are indexed in reverse chronological order so the most recent content is available first.

### Cross-Project Search

By default `search` looks at the current project (the one matching your working directory). If that turns up nothing, it automatically widens to **every indexed project** and tells you it did (`scope: widened`). Use `--all` to search everything up front, or `--project PATH` to pin a single project (which never broadens).

The index only contains projects it has already seen. To make the whole machine searchable, build the global index once:

```bash
cc-search-chats index --all
```

This walks every project under `~/.claude/projects/` and indexes it. It is incremental — re-runs only touch new or changed sessions — so it is cheap to schedule. To keep the global index warm, add a cron entry or a systemd user timer:

```bash
# crontab -e  (hourly)
0 * * * * ~/.local/bin/cc-search-chats index --all >/dev/null 2>&1
```

```ini
# ~/.config/systemd/user/cc-search-index.service
[Service]
Type=oneshot
ExecStart=%h/.local/bin/cc-search-chats index --all

# ~/.config/systemd/user/cc-search-index.timer
# enable with: systemctl --user enable --now cc-search-index.timer
[Timer]
OnCalendar=hourly
Persistent=true

[Install]
WantedBy=timers.target
```

### Searching Thinking and Tool Calls

The persistent index stores the clean conversation only. To search reasoning and tool inputs/outputs as well, add `--everything`:

```bash
cc-search-chats search "that regex we tried" --everything
```

This is a live scan: it builds a throwaway in-memory index over the in-scope sessions' full content and discards it afterwards — nothing extra is stored. It defaults to the current project; add `--all` to scan every project (slower, since it re-reads the raw files).

### Epoch Model

When Claude Code compresses a session, it inserts a `compact_boundary` marker. cc-search-chats uses these markers to segment sessions into **epochs**:

- **Epoch 0**: Messages from before the first compression -- the "lost context" that most users want to recover
- **Epoch 1+**: Content after each successive compression event

You can filter searches and extractions to specific epochs with `--epoch N`.

### Known Limitations

- **Project path decoding is lossy.** Claude Code encodes project paths by replacing `/` with `-`. This encoding is not reversible when the original path contains hyphens. Project paths displayed in output may differ from the actual filesystem path in these cases.
- **Python 3.14+ required.** The package uses language features introduced in Python 3.14.

## Compression Recovery

This is the primary use case. When Claude Code compresses a long session:

1. The original messages are still on disk in the JSONL file
2. Claude Code inserts a `compact_boundary` marker with metadata about how many tokens were compressed
3. A summary of the compressed content appears in the next message

To recover the pre-compression content:

```bash
# See what sessions exist
cc-search-chats list

# Extract pre-compression content from a session
cc-search-chats extract SESSION_ID --epoch 0

# Or search for specific content across all pre-compression epochs
cc-search-chats search "that thing we discussed" --epoch 0
```

## For Subagents

All commands support `--json` output for programmatic consumption. Use this when building workflows that consume search results:

```bash
# Structured search results
cc-search-chats search "auth" --json

# Structured session list
cc-search-chats list --days 7 --json

# Structured extraction
cc-search-chats extract --json
```

JSON output includes session IDs, epoch numbers, timestamps, and message content -- everything needed to drill down further. Every `--json` payload is an object carrying `schema_version` (currently `1`): read `search` results from `.results`, `list` from `.sessions`, `extract` from `.epochs`, and `context` from `.target`/`.before`/`.after`. The search payload also carries `scope` (`local` / `widened` / `all`). Check `schema_version` before parsing — if it differs from what your consumer expects, the CLI and the consumer are out of sync. Evolution is additive within a `schema_version`, so new fields may appear over time.

## Tip: Add to Your CLAUDE.md

Adding a line to your project's `CLAUDE.md` makes Claude automatically use `/search-chat` when you casually reference something from a previous session:

```markdown
## Chat History

When I reference a previous conversation, earlier discussion, or ask to continue/revisit a topic from another session, use `/search-chat` to find it.
```

Now you can say things like *"that staging bug from the other day"* and Claude will search your chat history instead of asking you to explain from scratch.

## Requirements

- Python 3.14+
- SQLite with FTS5 support (included in standard Python builds)
- Zero external dependencies

## Acknowledgements

- [pcvelz/cc-search-chats-plugin](https://github.com/pcvelz/cc-search-chats-plugin) -- original bash implementation that this project is forked from
- [akatz-ai/cc-conversation-search](https://github.com/akatz-ai/cc-conversation-search) -- validated the SQLite FTS5 + JIT indexing approach

## Licence

MIT
