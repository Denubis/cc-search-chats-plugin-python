---
description: "Search or recover previous Claude Code and Codex conversations"
allowed-tools: ["Bash(cc-search-chats:*)"]
---

# Search Chat History

**Request:** $ARGUMENTS

Use `cc-search-chats --json` and require `schema_version: 3`. In a sandboxed
caller, request the configured host approval route on the first attempt; do not
infer host PostgreSQL, D-Bus, CUDA, or model-cache state from a sandbox failure,
and do not invent connection or cache environment variables.

- Topic: `cc-search-chats search "QUERY" --json`
- Exact/filter search: `cc-search-chats search "QUERY" --literal --json`
- Agent/unknown sessions: `cc-search-chats search "QUERY" --agents --json`
- Tool content: `cc-search-chats search "QUERY" --literal --tools --json`
- Complete occurrences: `cc-search-chats search "QUERY" --literal --tools --exhaustive --json`
- Recent sessions: `cc-search-chats list --days 7 --json`
- Recover: `cc-search-chats extract [SESSION_ID] --provider PROVIDER --json`
- Verify a result: `cc-search-chats resolve CCCHAT_LOCATOR --reference-only --json`
- Follow a result: `cc-search-chats context CCCHAT_LOCATOR --depth 10 --json`
- Explicit maintenance: `cc-search-chats index --json`
- Semantic checkpoint: `cc-search-chats index --status --json`

A search opens the currently selected coherent corpus without admitting,
launching, joining, or waiting for index work. Run `index` intentionally when a
newer corpus is required. A miss merits alternate terms, `--literal`, and
filter review before explicit maintenance. Default search contains visible
primary prose from configured standard and Ponytail roots;
`--agents` adds agent/unknown sessions;
`--literal --tools` adds tool content. Reasoning, instructions, injected context,
and unrecognized shapes are unavailable.

Read `results`, `sessions`, `messages`, or batched `resolutions` as appropriate.
Retain provider-qualified native identity and `ccchat:v1:` locators when
presenting matches. Check `coverage`, `refresh.corpus_generation`,
`semantic.semantic_build`, `semantic.corpus_generation`, `corpus_age_ms`,
`warnings`, and terminal `status`; an empty result is not proof of absence when
coverage is partial or filters exclude the intended corpus.
