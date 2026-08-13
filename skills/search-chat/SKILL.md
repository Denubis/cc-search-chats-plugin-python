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

Do not run `index` merely because a search misses. Try alternative terms and
literal mode first; indexing scans both complete native roots and may require
the GPU for new prose.

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
