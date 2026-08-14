---
name: search-chat
description: "Search and recover context from Claude Code and Codex chat history. Use for previous conversations, lost context, cross-session references, earlier discussions, or recovering work after compression or a crash."
allowed-tools: ["Bash(cc-search-chats:*)"]
---

# Search Chat History

Use the PostgreSQL-backed CLI with `--json`. Check `schema_version` is `1`
before interpreting output.

## Route the request

```bash
# Topic or natural-language search (hybrid FTS + semantic)
cc-search-chats search "query" --json

# Exact words, filters, or semantic runtime unavailable
cc-search-chats search "query" --literal --json
cc-search-chats search "query" --literal --provider codex --days 7 --json

# Thinking and tool calls (persisted full-content literal search)
cc-search-chats search "query" --everything --json

# Recent sessions across Claude and Codex
cc-search-chats list --days 7 --json

# Recover a session; omit ID for the most recent substantial session
cc-search-chats extract [SESSION_ID] --json

# Surrounding messages from a search-result locator
cc-search-chats context CCCHAT_LOCATOR --depth 10 --json

# Exact resolution of a durable locator
cc-search-chats resolve CCCHAT_LOCATOR --json

# Idempotent refresh, or inspect a resumable semantic checkpoint
cc-search-chats index --json
cc-search-chats index --status --json
```

Run `index` when freshness matters or the newest indexed session predates the
requested conversation. It is idempotent, reuses unchanged vectors, resumes
interruptions, and reports reused/new/remaining work with an ETA. A miss alone
still merits trying alternative terms or `--literal` before refreshing.
Do not add `--project` unless `list` shows the desired path; older Codex rows
may have no project metadata and therefore cannot match that filter.

## Interpret output

- `search`: read `results`; show provider, session ID, timestamp, role, text,
  and locator. Offer `context` or `extract` for follow-up.
- `list`: read `sessions`; show provider, session ID, kind, latest timestamp,
  message count, repository, and cwd.
- `extract`, `context`, `resolve`: read `messages` in order.
- `index --status`: read `completed`, `total`, and `selected`.

Always retain provider-qualified session IDs and `ccchat:v1:` locators. A
vendor `role=user` does not prove human authorship; use `submitted_by` only when
the result provides positive provenance.

If `schema_version` is missing or not `1`, stop and report that the plugin and
CLI are out of sync.
