# cc-search-chats

Search and recover context from Claude Code and Codex chat history.

## The Problem

Claude Code compresses conversation context when sessions grow large. Earlier context is summarised and the original messages become inaccessible through normal conversation. This tool recovers that content by indexing the underlying JSONL session files that Claude Code stores on disk.

## Installation

The agent plugin and the Python CLI are separate artifacts. The plugin provides
the search workflow, but it invokes the independently installed
`cc-search-chats` executable. Install and verify the CLI first.

### 1. Install the CLI

Install [uv](https://docs.astral.sh/uv/) and ensure Python 3.14 or newer is
available. The shipped search skill uses hybrid search by default, so the
semantic installation is recommended:

```console
uv tool install \
  'cc-search-chats[semantic] @ git+https://github.com/Denubis/cc-search-chats-plugin-python@main'
```

For literal search without the local model runtime:

```console
uv tool install \
  'cc-search-chats @ git+https://github.com/Denubis/cc-search-chats-plugin-python@main'
```

Use `--force` with the same command to replace an existing installation.
Replace `main` with a commit hash when the machine must use an exactly pinned
build.

Verify the executable and its package version before installing the plugin:

```console
cc-search-chats --version
cc-search-chats 2.0.0a7
```

### 2. Install an agent plugin

The Codex plugin bundle has its own release number (`2.0.1`); the version shown
by `cc-search-chats --version` is the Python CLI version. Both shipped skills
expect JSON `schema_version` 1.

#### Claude Code

Run these commands inside Claude Code:

```text
/plugin marketplace add Denubis/cc-search-chats-plugin-python
/plugin install cc-search-chats@cc-search-chats-marketplace
```

Or from inside Claude Code, use `/plugin` and navigate to **Discover** to browse and install.

#### Codex

```console
codex plugin marketplace add Denubis/cc-search-chats-plugin-python --ref main
codex plugin add cc-search-chats@cc-search-chats-marketplace
```

Start a new Claude Code or Codex session after installing the plugin.

## PostgreSQL Setup

The standard CLI uses local database `cc_search_chats` as
`cc_search_chats_owner`. PostgreSQL 18 and pgvector must already be installed.
As a PostgreSQL administrator, provision them once:

```sql
CREATE ROLE cc_search_chats_owner LOGIN;
\password cc_search_chats_owner
CREATE DATABASE cc_search_chats OWNER cc_search_chats_owner;
\connect cc_search_chats
CREATE EXTENSION vector;
```

Configure the standard libpq service once in `~/.pg_service.conf`:

```ini
[cc_search_chats]
host=127.0.0.1
port=5432
dbname=cc_search_chats
user=cc_search_chats_owner
```

Store the password separately in `~/.pgpass` (mode `0600`) so cron and agents
need no connection or password environment variables:

```text
127.0.0.1:5432:cc_search_chats:cc_search_chats_owner:YOUR_PASSWORD
```

Large installations may create the database in an administrator-managed
tablespace on external storage. The application creates only its own schema; it
never creates roles, databases, extensions, or tablespaces.

Semantic search uses the pinned local `nvidia/Nemotron-3-Embed-8B-BF16`
snapshot in the normal Hugging Face cache (`~/.cache/huggingface` or `$HF_HOME`).
No network access is used at runtime.

## Quick Start

```bash
# Find discussions about a topic
cc-search-chats search "database migration"

# Idempotently refresh the Claude + Codex corpus and semantic vectors
cc-search-chats index

# Inspect the resumable checkpoint
cc-search-chats index --status --json

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
| `search "query"` | Hybrid PostgreSQL full-text + semantic search | `--literal`, `--provider`, `--limit`, `--json` |
| `extract [SESSION_ID]` | Extract a conversation (auto-discovers if no ID given) | `--epoch N`, `--verbose` |
| `list` | List sessions with metadata | `--days N`, `--project PATH` |
| `context UUID` | Show messages around a specific message | `--depth N` |
| `index` | Idempotently refresh Claude + Codex and resume semantic work | `--literal-only`, `--semantic-only`, `--status`, `--json` |

All commands support `--json` for structured output suitable for programmatic consumption. Every payload is an object carrying `schema_version`; e.g. `search` → `results`, `list` → `sessions`.

## How It Works

Chat sessions are read from Claude Code JSONL under `~/.claude/projects/` and
Codex JSONL under `~/.codex/sessions/`.

**cc-search-chats** stores immutable corpus and semantic revisions in PostgreSQL,
uses PostgreSQL full-text search and pgvector, and atomically selects complete
revisions. Indexing commits small batches, resumes missing vectors, reuses
unchanged vectors after refresh, and prevents concurrent workers with a
PostgreSQL advisory lock. Semantic indexing automatically runs in a polite,
memory- and task-bounded systemd user scope.

### Refresh behavior

`cc-search-chats index` is the ordinary safe refresh command for humans, cron,
and agents. It scans both native roots, creates a new corpus revision when the
snapshot changed, reuses identical vectors, embeds every new eligible passage,
and atomically selects the result only when complete. With no semantic delta it
returns without loading the model. Interrupted work resumes from committed
batches, while the last complete revision remains searchable.

Progress distinguishes work already reused from work actually required:

```text
Semantic refresh: 256081 reused, 2143 new passages
Semantic refresh: 256081 reused, 300 embedded, 1843 remaining, 4.8/s, ETA 6m24s
```

An explicit `index` does not suppress real changes behind a threshold: any new
eligible prose is indexed. Use `index --status --json` for a read-only durable
checkpoint. Search itself does not silently start a refresh.

### Cross-Project Search

PostgreSQL searches the whole indexed corpus by default. Use `--project PATH`
only when the indexed rows show that exact path in `repository` or `cwd`;
older Codex rows without project metadata cannot match a project filter.

The index only contains projects it has already seen. To make the whole machine searchable, build the global index once:

```bash
cc-search-chats index
```

This walks both native roots. Re-runs create a fresh corpus receipt, reuse
unchanged vectors, and resume any interrupted semantic work. To keep the index
fresh, add a cron entry or a systemd user timer:

```bash
# crontab -e  (hourly)
0 * * * * ~/.local/bin/cc-search-chats index >/dev/null 2>&1
```

```ini
# ~/.config/systemd/user/cc-search-index.service
[Service]
Type=oneshot
ExecStart=%h/.local/bin/cc-search-chats index

# ~/.config/systemd/user/cc-search-index.timer
# enable with: systemctl --user enable --now cc-search-index.timer
[Timer]
OnCalendar=hourly
Persistent=true

[Install]
WantedBy=timers.target
```

### Searching Thinking and Tool Calls

PostgreSQL persists conversation text, reasoning, and tool inputs/outputs. Add
`--everything` to select literal full-content search without loading the
embedding model:

```bash
cc-search-chats search "that regex we tried" --everything
```

The legacy SQLite backend retains its slower live-scan behavior.

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
- PostgreSQL 18 with pgvector
- A CUDA-capable GPU and the pinned local Nemotron model for semantic search

Set `CC_SEARCH_DB_PATH` only to opt into the legacy SQLite implementation.
Standard libpq variables such as `PGSERVICE` and `PGHOST`, plus
`CC_SEARCH_MODEL_PATH` and source-root variables, remain available for
non-default deployments.

## Acknowledgements

- [pcvelz/cc-search-chats-plugin](https://github.com/pcvelz/cc-search-chats-plugin) -- original bash implementation that this project is forked from
- [akatz-ai/cc-conversation-search](https://github.com/akatz-ai/cc-conversation-search) -- validated the SQLite FTS5 + JIT indexing approach

## Licence

MIT
