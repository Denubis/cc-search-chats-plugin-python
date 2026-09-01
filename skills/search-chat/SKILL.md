---
name: search-chat
description: "Search and recover context from Claude Code and Codex chat history. Use for previous conversations, lost context, cross-session references, earlier discussions, or recovering work after compression or a crash."
allowed-tools: ["Bash(cc-search-chats:*)"]
---

# Search Chat History

Use the PostgreSQL-backed CLI with `--json`. Require `schema_version: 3` before
interpreting stdout. Progress is an independent NDJSON stream on stderr.

## Execution boundary

In Codex, request the harness's configured host approval or escalation route for
every `cc-search-chats` invocation on the first attempt. The host PostgreSQL
service, D-Bus user session, GPU, and configured model cache may be absent from a
sandbox, so a sandbox failure establishes only that process's environment. Do
not invent connection, source-root, or cache variables to bypass the boundary.

Claude Code's allowed Bash invocation runs in its ordinary host environment.
For either provider, report an environmental failure as process-scoped unless a
host probe established the corresponding host fact.

## Choose the smallest search

```console
# Topic or natural-language search (hybrid FTS + semantic)
cc-search-chats search "query" --json

# Exact words, filters, or semantic runtime unavailable
cc-search-chats search "query" --literal --json
cc-search-chats search "query" --literal --provider codex --days 7 --json

# Include agent and unknown sessions
cc-search-chats search "query" --agents --json

# Lexical tool names/inputs/outputs; reasoning and instructions stay excluded
cc-search-chats search "query" --literal --tools --json

# Complete deterministic prose/tool occurrences rather than ranked top results
cc-search-chats search "query" --literal --tools --exhaustive --json

# Recent sessions across configured standard and Ponytail Claude/Codex roots
cc-search-chats list --days 7 --json

# Recover one session; qualify collisions with --provider
cc-search-chats extract [SESSION_ID] --provider codex --json

# Verify and follow a search-result locator
cc-search-chats resolve CCCHAT_LOCATOR --reference-only --json
cc-search-chats context CCCHAT_LOCATOR --depth 10 --json

# Explicit maintenance or read-only semantic checkpoint
cc-search-chats index --json
cc-search-chats index --status --json
```

A stale ranked search admits or joins one full systemd-owned update. A stale ranked search may wait within the command deadline for that update before opening its result snapshot. If publication does not finish in budget, it reads
the previous coherent corpus. A completed update, including a no-op, starts a
five-minute quiet period. Use `index` for explicit maintenance, not as the
automatic response to a miss. A miss merits alternate terms, `--literal`, and
careful filter review. Add `--project` only when `list` or prior results show the
exact recorded repository/cwd; missing project metadata cannot match it.

Default search is visible primary-session prose. `--agents` adds agent and
unknown sessions. `--tools` requires `--literal`; `--exhaustive` also requires
literal mode and is the only complete occurrence mode. Ranked results are
bounded top results. No flag exposes reasoning/thinking, system/developer
instructions, injected context, or unrecognized record shapes.

## Interpret schema v3

Every command object contains `command`, terminal `status`, `coverage`,
`refresh`, `semantic`, `indexed_at`, `corpus_age_ms`, `background_refresh`, and
`warnings`. Read the selected identity from `refresh.corpus_generation` and the
coherent semantic identity from `semantic.semantic_build` plus
`semantic.corpus_generation`.

- `search`: read `results`; retain `identity.provider`,
  `identity.source_session_id`, `identity.canonical_locator`, physical aliases,
  timestamp, role, session kind, content class, text, and ranking evidence.
- `list`: read `sessions`; retain provider, source session ID, kind, latest
  timestamp, message count, repository, and cwd.
- `extract` and `context`: read ordered `messages`.
- Single `resolve`: read `status` and `messages`; batched `resolve --stdin` reads
  `resolutions` in input order. `--reference-only` deliberately omits text.
- `index --status`: read `completed`, `total`, `selected`, and the shared
  semantic/refresh objects.
- `events`: read `source_corpus_generation`, `population`, and `events`; each
  retained event repeats `source_corpus_generation` and contains no message
  body.

Treat `coverage.completeness != "complete"`, unreadable files, pending bytes,
unrecognized records, warnings, or stale semantic state as evidence limits—not
as a clean absence result. An empty result is meaningful only after the searched
roots, filters, freshness, and positive controls are established.

Exact resolution statuses are `resolved`, `no_match`, `multiple_matches`,
`source_unavailable`, `stale_source`, `stale_index`, `malformed_locator`, and
`unsupported_provider_schema`. Report the status rather than turning every
non-result into “not found.”

If `schema_version` is missing or not `3`, stop: the plugin instructions and CLI
are out of sync.
