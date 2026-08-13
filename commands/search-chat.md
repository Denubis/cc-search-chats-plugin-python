---
description: "Search or recover previous Claude Code and Codex conversations"
allowed-tools: ["Bash(cc-search-chats:*)"]
---

# Search Chat History

**Request:** $ARGUMENTS

Use `cc-search-chats` with `--json` and require `schema_version: 1`.

- Topic: `cc-search-chats search "QUERY" --json`
- Exact/filter search: `cc-search-chats search "QUERY" --literal --json`
- Recent sessions: `cc-search-chats list --days 7 --json`
- Recover: `cc-search-chats extract [SESSION_ID] --json`
- Follow a result: `cc-search-chats context CCCHAT_LOCATOR --depth 10 --json`
- Reindex: `cc-search-chats index --json`
- Index progress: `cc-search-chats index --status --json`

Search results are in `results`; list results in `sessions`; extract/context/
resolve results in `messages`. Include provider, session ID, timestamp, and the
durable `ccchat:v1:` locator when presenting matches. Try alternative terms or
`--literal` before starting a reindex.
