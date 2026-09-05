---
name: search-chat
description: "Search and recover context from Claude Code and Codex chat history. Use for previous conversations, lost context, cross-session references, earlier discussions, or recovering work after compression or a crash."
allowed-tools: ["Bash(cc-search-chats:*)"]
---

# Search Chat History

Use the PostgreSQL-backed CLI with `--json`. Require `schema_version: 4` before
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

An `index` answer with `status: containment_unavailable` means the caller's
process could not create the systemd user scope; ask the user for permission to
run it on the host rather than retrying in place.

## Choose the smallest search

```console
# Topic or natural-language search (model-ranked hybrid)
cc-search-chats search "query" --semantic --json

# Exact words, filters, or semantic runtime unavailable
cc-search-chats search "query" --literal --json
cc-search-chats search "query" --literal --provider codex --days 7 --json

# Include agent and unknown sessions
cc-search-chats search "query" --semantic --agents --json

# Lexical tool names and inputs; reasoning, instructions, and results stay excluded
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

Search requires exactly one mode. `--literal` is exact full-text search with no
model or GPU and is the quick path. `--semantic` is model-ranked hybrid fusion
of full-text and embedding candidates with no deadline: first use takes about
ten seconds, while another query within the thirty-second burst window takes
about a second by reusing the warm model. Search opens the currently selected coherent corpus
without admitting, launching, joining, or waiting for index work. Use `index`
intentionally when a newer corpus is required. A miss merits alternate terms,
`--literal`, and careful filter review. Add `--project` only when `list` or
prior results show the exact recorded repository/cwd; missing project metadata
cannot match it.

Both modes search visible primary-session prose. `--agents` adds agent and
unknown sessions. `--tools` requires `--literal` and adds persisted tool names
and inputs; tool results are not persisted. `--exhaustive` also requires literal
mode and is the only complete occurrence mode. Ranked results are bounded top
results. No flag exposes reasoning/thinking, system/developer instructions,
injected context, or unrecognized record shapes.

## Interpret schema v4

Every command object contains `command`, terminal `status`, `coverage`,
`refresh`, `semantic`, `indexed_at`, `corpus_age_ms`, and `warnings`. Read the
selected identity from `refresh.corpus_generation` and the
coherent semantic identity from `semantic.semantic_build` plus
`semantic.corpus_generation`.

For `search`, read requested `mode` before delivered `retrieval_mode`. A
semantic request may deliver `literal_fallback`; require the
`semantic_search_degraded` warning and report those as literal results. A
post-retrieval literal deadline returns retained hits with `status: partial`
and `deadline_degraded`; semantic has `deadline_ms: null`. Read
`semantic.model_load_ms`, `semantic.query_embed_ms`, and
`semantic.warm_reused` as the observed query-helper timing and reuse state.

- `search`: read `index_state` and `results`; retain `identity.provider`,
  `identity.source_session_id`, `identity.canonical_locator`, physical aliases,
  timestamp, role, session kind, content class, text, and ranking evidence.
- `list`: read `sessions`; retain provider, source session ID, kind, latest
  timestamp, message count, repository, and cwd.
- `extract` and `context`: read ordered `messages`.
- Single `resolve`: read `status` and `messages`; batched `resolve --stdin` reads
  `resolutions` in input order. `--reference-only` deliberately omits text.
- `index --status`: read `index_state`, `completed`, `total`, `selected`, and
  the shared semantic/refresh objects.
- `events`: read `source_corpus_generation`, `population`, and `events`; each
  retained event repeats `source_corpus_generation` and contains no message
  body.

Human search output places the mode line first and the index made/now/age plus
missing-chat header before results. `index_state.unindexed: null` means the
bounded scan did not complete; report `unindexed_reason` instead of guessing.

Treat `coverage.completeness != "complete"`, unreadable files, pending bytes,
unrecognized records, warnings, or stale semantic state as evidence limits—not
as a clean absence result. An empty result is meaningful only after the searched
roots, filters, freshness, and positive controls are established.

Exact resolution statuses are `resolved`, `no_match`, `multiple_matches`,
`source_unavailable`, `stale_source`, `stale_index`, `malformed_locator`, and
`unsupported_provider_schema`. Report the status rather than turning every
non-result into “not found.”

If `schema_version` is missing or not `4`, stop: the plugin instructions and CLI
are out of sync.
