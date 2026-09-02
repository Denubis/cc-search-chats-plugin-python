---
description: "Search or recover previous Claude Code and Codex conversations"
allowed-tools: ["Bash(cc-search-chats:*)"]
---

# Search Chat History

**Request:** $ARGUMENTS

Use `cc-search-chats --json` and require `schema_version: 4`. In a sandboxed
caller, request the configured host approval route on the first attempt; do not
infer host PostgreSQL, D-Bus, CUDA, or model-cache state from a sandbox failure,
and do not invent connection or cache environment variables.

- Topic: `cc-search-chats search "QUERY" --semantic --json`
- Exact/filter search: `cc-search-chats search "QUERY" --literal --json`
- Agent/unknown sessions: `cc-search-chats search "QUERY" --semantic --agents --json`
- Tool content: `cc-search-chats search "QUERY" --literal --tools --json`
- Complete occurrences: `cc-search-chats search "QUERY" --literal --tools --exhaustive --json`
- Recent sessions: `cc-search-chats list --days 7 --json`
- Recover: `cc-search-chats extract [SESSION_ID] --provider PROVIDER --json`
- Verify a result: `cc-search-chats resolve CCCHAT_LOCATOR --reference-only --json`
- Follow a result: `cc-search-chats context CCCHAT_LOCATOR --depth 10 --json`
- Explicit maintenance: `cc-search-chats index --json`
- Semantic checkpoint: `cc-search-chats index --status --json`

A search requires exactly one mode: `--literal` is exact full-text with no
model or GPU, while `--semantic` is model-ranked hybrid full-text/embedding
fusion. It opens the currently selected coherent corpus without admitting,
launching, joining, or waiting for index work. Run `index` intentionally when a
newer corpus is required. A miss merits alternate terms, `--literal`, and
filter review before explicit maintenance. Both modes contain visible primary
prose from configured standard and Ponytail roots;
`--agents` adds agent/unknown sessions;
`--literal --tools` adds tool content. Reasoning, instructions, injected context,
and unrecognized shapes are unavailable.

Read `results`, `sessions`, `messages`, or batched `resolutions` as appropriate.
Retain provider-qualified native identity and `ccchat:v1:` locators when
presenting matches. Read requested `mode` separately from delivered
`retrieval_mode`. Check `index_state` for when the selected index was made, its
age, and bounded unindexed counts; human output states the same header before
results. A semantic `literal_fallback` must carry `semantic_search_degraded`
and must be described as literal results. Also check `coverage`,
`refresh.corpus_generation`, `semantic.semantic_build`,
`semantic.corpus_generation`, `corpus_age_ms`, `warnings`, and terminal
`status`; an empty result is not proof of absence when coverage is partial,
staleness is unknown, or filters exclude the intended corpus.
