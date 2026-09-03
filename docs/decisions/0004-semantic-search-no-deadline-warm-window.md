# 0004. Semantic search has no deadline and reuses one short-lived warm model

Status: Accepted
Date: 2026-09-03

## Context

ADR 0002 gave ranked search one five-second invocation-to-answer deadline and
used one spawned, reaped query-embedding child per semantic request. Production
measurement later showed that the 8 B model takes about 8.2 seconds to load on
the target machine. A semantic request therefore could not satisfy that
deadline, while bursts paid the same model-loading cost repeatedly.

The governing records are:

- Brian, 2026-08-25: *"5 seconds is the time between rewyest and return  wr can
  tune it once we know what spinup costs"*
  (`ccchat:v1:codex:01a0324d-aec0-7991-88b0-3ad1baedf614:id:msg_01a03821-f0f6-7540-8182-a7405fdfce82`).
- Brian, 2026-09-01: *"The goal is that a querent gets results within 5
  seconds, with notes from when those results are from."*
  (`ccchat:v1:codex:01a05687-b8b7-76f0-a949-015d6ac3f940:id:msg_01a05a60-0fb6-7ef3-977c-ed7fc7d56f8e`).
- Brian, 2026-09-03, after being told that each semantic invocation cold-loads
  the model: *"yeah no, we're not going to keep the model loaded. That's fine.
  Just... what's the best way to do this with an AI agent that doesn't
  understand time? Do we say "get model warm" first? and then have it unwarm
  after some idle time? or do we just let first use warm it and it takes
  however long?"*
  (`ccchat:v1:claude:6c653783-2718-45a1-9a7f-5154a71c6f71:uuid:008dbbd2-9dc7-4254-b6aa-10b4ef58aa04`).
- Brian, 2026-09-03, after the supervisor recommended paying the load per call
  with no warm step: *"then yes, do that, though uh, these tend to come in
  bursts, so let warm stay warm for 30 seconds after the last query. and no
  timer on semantic. Just make sure it knows that it will take a whiel."*
  (`ccchat:v1:claude:6c653783-2718-45a1-9a7f-5154a71c6f71:uuid:befb0bf3-39c0-4e06-8615-cb319a39d34d`).

## Decision

This record amends ADR 0002 clause 6 for semantic mode only. Ranked literal
search retains its five-second request-to-answer deadline and partial-on-late
resolution behavior. Semantic search has no answer deadline: model loading,
query embedding, PostgreSQL retrieval, exact resolution, and rendering take as
long as they take.

The per-search query-embedding child is replaced by one same-user, ad hoc helper
process. The first semantic search starts it outside the caller's process group.
It listens on a Unix socket in the private runtime directory, admits only a peer
with its own UID, and holds a lifetime file lock as the single-instance owner.
Each connection carries one framed JSON request. `hello` exposes package/model
identity and warm state; `embed` streams model-load/query-embed progress before
one result or named failure; `shutdown` terminates the helper.

The helper loads the model on first use and exits thirty seconds after the last
completed query by default. Each query resets that idle window. An explicit
positive `CC_SEARCH_SEMANTIC_WARM_SECONDS` value changes the configured bound. A
live helper with a different package version or local model revision is shut
down and replaced before use. An index run also shuts down and waits for a live
helper before it acquires index ownership or starts model preflight.

Human and machine consumers are told the cost. Human output distinguishes a
cold load from warm reuse. Schema-v4 JSON adds model-load milliseconds,
query-embedding milliseconds, and a warm-reuse flag while retaining
`deadline_ms: null` for semantic mode.

## Consequences

Under the default configuration, the model remains resident for at most thirty
seconds after the last semantic query; an explicit operator override changes
that configured upper bound. The first-use cost is therefore paid once per
burst rather than once per call. Agents do not infer a deadline or reason about
elapsed time; their maintained instructions say that cold semantic search takes
about ten seconds, warm reuse takes about a second, and literal search is the
quick path.

The helper has no systemd unit and does not become a resident service. A
semantic search started while the index is embedding may encounter VRAM
pressure, report the model unavailable, and return named literal fallback.

The deterministic test embedder is reachable only when a private launch flag
and `CC_SEARCH_QUERY_EMBEDDER_TEST_TOKEN` match; clients additionally reject a
test helper unless their own token has the same digest, preventing a helper
left warm by a test from serving an ordinary search.
