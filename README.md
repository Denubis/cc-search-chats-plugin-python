# cc-search-chats

Search and recover context from native Claude Code and Codex chat history.

Current release: cc-search-chats 2.0.5

The CLI reads vendor JSONL session logs without modifying them, maintains a
normalized PostgreSQL search projection, and supports PostgreSQL full-text
search plus an optional local semantic model. Standard and isolated Ponytail
session roots can participate in one corpus without sharing either runtime's
configuration, credentials, caches, plugins, or other state.

## Installation

The plugin supplies the agent workflow; it invokes an independently installed
`cc-search-chats` executable. Python 3.14+, PostgreSQL 18, and pgvector are
required.

Install the CLI with the semantic extra for default hybrid search:

```console
uv tool install \
  'cc-search-chats[semantic] @ git+https://github.com/Denubis/cc-search-chats-plugin-python@main'
```

For literal-only use:

```console
uv tool install \
  'cc-search-chats @ git+https://github.com/Denubis/cc-search-chats-plugin-python@main'
```

Pin a commit instead of `main` when reproducible installation matters. Verify
the executable before installing either plugin:

```console
cc-search-chats --version
cc-search-chats --help
```

Claude Code plugin:

```text
/plugin marketplace add Denubis/cc-search-chats-plugin-python
/plugin install cc-search-chats@cc-search-chats-marketplace
```

Codex plugin:

```console
codex plugin marketplace add Denubis/cc-search-chats-plugin-python --ref main
codex plugin add cc-search-chats@cc-search-chats-marketplace
```

Start a new agent session after installing or updating a plugin.

## PostgreSQL Setup

The default connection is libpq service `cc_search_chats`. PostgreSQL and
pgvector must already exist; the application creates only its own schema.
Provision the role/database once as a PostgreSQL administrator:

```sql
CREATE ROLE cc_search_chats_owner LOGIN;
GRANT SET ON PARAMETER temp_file_limit TO cc_search_chats_owner;
\password cc_search_chats_owner
CREATE DATABASE cc_search_chats OWNER cc_search_chats_owner;
\connect cc_search_chats
CREATE EXTENSION vector;
```

Configure `~/.pg_service.conf`:

```ini
[cc_search_chats]
host=127.0.0.1
port=5432
dbname=cc_search_chats
user=cc_search_chats_owner
```

Put the password in `~/.pgpass` with mode `0600`, not in the systemd unit:

```text
127.0.0.1:5432:cc_search_chats:cc_search_chats_owner:YOUR_PASSWORD
```

Large deployments should provision the database's default and temporary
tablespaces on operator-managed external storage before the first migration.
The CLI does not create a database, extension, tablespace, mount, or fallback
storage location. See
[`docs/runbooks/postgresql-index-maintenance.md`](docs/runbooks/postgresql-index-maintenance.md).

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

If the model, dependencies, CUDA, or VRAM are unavailable, hybrid search exits
nonzero with a named phase and a shell-safe literal fallback. Literal search
does not load the model.

## Quick Start

```console
# Hybrid natural-language search; refreshes changed native records first
cc-search-chats search "database migration"

# Exact lexical search without the model
cc-search-chats search "schema_migration" --literal

# Include agent/unknown sessions
cc-search-chats search "the earlier runner work" --agents

# Lexical tool content; reasoning and instructions remain excluded
cc-search-chats search "git diff" --literal --tools

# Every matching prose/tool occurrence in deterministic order
cc-search-chats search "sentinel" --literal --tools --exhaustive --json

# Resolve and verify an exact native locator without returning message text
cc-search-chats resolve CCCHAT_LOCATOR --reference-only --json

# Explicit maintenance or read-only semantic checkpoint
cc-search-chats index --json
cc-search-chats index --status --json
```

`search` performs metadata discovery and incremental refresh before retrieval.
Running `index` first is therefore unnecessary for freshness; it remains useful
for scheduled baseline/semantic maintenance.

## Search Scope

PostgreSQL searches all configured roots by default. Filters narrow that corpus:

- `--provider claude|codex`
- `--project PATH` for exact recorded repository/cwd values
- `--role ROLE`, `--epoch N`, and `--days N`
- `--agents` for agent and unknown sessions
- `--literal --tools` for persisted tool names, inputs, and outputs

Default and literal ranked searches return at most `--limit` results (1–200).
Hybrid search fuses bounded lexical and semantic component rankings with exact
reciprocal-rank-fusion arithmetic. Only `--literal --exhaustive` claims complete
occurrence coverage; it pages through PostgreSQL and ignores the ranked limit.

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

## Refresh and Storage

PostgreSQL stores one current canonical message row, one row per physical alias,
one current semantic chunk row per chunk/profile, and one vector per
profile/prefixed-input digest. Corpus and semantic generations contain bounded
publication, progress, and failure metadata—not copies of messages, aliases, or
vectors.

Unchanged refreshes read metadata but no JSONL content bytes and create no
generation. Same-device/inode growth reads only after the last complete-record
watermark. Partial tails remain pending; truncation, replacement, same-size
modification, and parser-version changes reparse the affected source from byte
zero. Native logs are never written or locked.

One PostgreSQL advisory owner serializes refresh/semantic work. Long phases
publish owner, heartbeat, completed/total units, and named state. Committed
literal rows remain searchable after semantic failure, while hybrid search
refuses a stale or incomplete semantic generation.

## JSON and Progress Contract

The default PostgreSQL surface emits JSON schema version 2. Each `--json`
command writes one stdout object containing:

- `schema_version`, `command`, and terminal `status`
- `coverage` with roots, repositories, file counts, diagnostics, and watermarks
- `refresh`, `semantic`, and `warnings`
- command-specific `results`, `sessions`, `messages`, or `resolutions`

Search/extract/context/resolve messages carry provider-qualified canonical
identity plus verified physical source coordinates. Exact resolution statuses
are `resolved`, `no_match`, `multiple_matches`, `source_unavailable`,
`stale_source`, `stale_index`, `malformed_locator`, and
`unsupported_provider_schema`.

Progress never contaminates JSON stdout. JSON or non-TTY execution writes
ordered schema-v2 NDJSON events to stderr, including periodic heartbeats and
exactly one terminal event. Use `--progress human` for concise terminal text.

## Scheduled Maintenance

The distribution includes a low-priority oneshot and persistent nightly timer.
Copy them to `~/.config/systemd/user/`. Optional operator configuration belongs
in `~/.config/cc-search-chats/index.env`, which the service reads if present.
For example:

```text
CC_SEARCH_CLAUDE_ROOTS=/home/USER/.claude/projects:/home/USER/.claude-ponytail/projects
CC_SEARCH_CODEX_ROOTS=/home/USER/.codex/sessions:/home/USER/.codex-ponytail/sessions
```

Keep libpq credentials in `.pgpass` and cache configuration in the operator's
existing environment. The packaged unit supplies neither.

```console
systemctl --user daemon-reload
systemctl --user enable --now cc-search-chats-index.timer
```

The timer runs nightly at 03:00 with up to 30 minutes randomized delay. No
resident daemon is required.

## Recovery and Release Boundaries

Native logs are the rebuild authority. Applied SQL migrations are ordered and
checksummed; their bytes are immutable. Legacy full-snapshot relations remain
quarantined through migration and positive four-corpus UAT. Pruning them is a
separate human-authorized operation requiring a matching fresh dry-run,
accepted exact-commit validation, complete current semantic join, and repeated
post-prune checks. See the maintenance runbook and
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
