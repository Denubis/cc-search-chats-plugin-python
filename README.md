# cc-search-chats

Search and recover context from Claude Code chat history.

## The Problem

Claude Code compresses conversation context when sessions grow large. Earlier context is summarised and the original messages become inaccessible through normal conversation. This tool recovers that content by indexing the underlying JSONL session files that Claude Code stores on disk.

## Installation

### As a Claude Code Plugin (Recommended)

Install from the marketplace, or directly:

```bash
claude plugin add github:Denubis/cc-search-chats-plugin-python
```

### Standalone CLI

```bash
uvx cc-search-chats
```

No dependencies to install -- the package uses only Python's standard library.

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
| `search "query"` | Full-text search across chat history | `--epoch N`, `--days N`, `--project PATH` |
| `extract [SESSION_ID]` | Extract a conversation (auto-discovers if no ID given) | `--epoch N`, `--verbose` |
| `list` | List sessions with metadata | `--days N`, `--project PATH` |
| `context UUID` | Show messages around a specific message | `--depth N` |
| `index` | Force a full reindex of the current project | `--project PATH` |

All commands support `--json` for structured output suitable for programmatic consumption.

## How It Works

Chat sessions are stored by Claude Code as JSONL files in `~/.claude/projects/`. Each file contains timestamped messages with roles, content, and metadata.

**cc-search-chats** builds a local SQLite FTS5 (full-text search) index over these files. The index is created just-in-time on first access and updated incrementally when session files change. Sessions are indexed in reverse chronological order so the most recent content is available first.

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

JSON output includes session IDs, epoch numbers, timestamps, and message content -- everything needed to drill down further.

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
