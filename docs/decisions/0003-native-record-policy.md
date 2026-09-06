# 0003. Persist visible conversation, bounded tool metadata, and monotone exclusions

Status: Accepted
Date: 2026-09-03

## Context

Native Claude Code and Codex logs mix visible conversation with harness-injected
context, reasoning, tool calls, tool results, malformed records, and strings
that PostgreSQL cannot store. The projection needs a stable searchable boundary
without turning recoverable defects into corpus-wide alarms.

The governing records are:

- Brian, 2026-09-02: *"um, give me the three malformed json, I'll just go ...
  remove those lines? (or can we skip them without alarming out?) And um, why
  are we indexing tool call output? We shouldn't be? and just tofu bad utf 16?
  the issue is that this shouldn't cause an alarm/partial state?"*
  (`ccchat:v1:claude:6c653783-2718-45a1-9a7f-5154a71c6f71:uuid:a60090d4-ed89-4a7d-aad0-7dc356110d86`).
- Brian, 2026-06-12: *"there needs to be a flag for *everything* not just the
  cleaned conversation. It comes up sometimes that tool calls and thinking
  should be searched"*
  (`ccchat:v1:claude:a5ab7bf2-b20a-430e-b65b-eab81695168d:uuid:ce96727e-882c-4a14-92d7-038a819365f0`),
  followed by *"um, I'd prefer the everything not be persistened, that's quite
  rare"*
  (`ccchat:v1:claude:a5ab7bf2-b20a-430e-b65b-eab81695168d:uuid:ab24ce07-425f-4578-bd7c-95a2f7547558`).
- Supervisor, 2026-09-03: *"Tool results: stop persisting them in the index,
  keep tool names and inputs. Yes or no."* Brian answered: *"then yes, do
  that"*
  (`ccchat:v1:claude:6c653783-2718-45a1-9a7f-5154a71c6f71:uuid:befb0bf3-39c0-4e06-8615-cb319a39d34d`).
- Supervisor, 2026-09-03: *"Step 6 will exclude dot-directories from discovery.
  No ruling needed unless you object."*
  (`ccchat:v1:claude:6c653783-2718-45a1-9a7f-5154a71c6f71:uuid:161c995d-bf04-4138-9b34-1e827e18e4b9`).
  Brian's next message, the preceding tool-results ruling, raised no objection
  (`ccchat:v1:claude:6c653783-2718-45a1-9a7f-5154a71c6f71:uuid:befb0bf3-39c0-4e06-8615-cb319a39d34d`).

## Decision

The persistent searchable corpus contains visible user and assistant prose.
Agent and unknown sessions are available only with `--agents`. Tool names and
inputs are persisted and available only with `--literal --tools`.

Reasoning, system/developer/injected context, and tool results are never
persisted. An unpersisted, on-demand “everything” scan remains a separate,
deferred capability.

Provider parsers replace lone Unicode surrogates and U+0000 in retained strings
with U+FFFD before computing the embedding-input digest. A skippable individual
record is counted and retained in refresh diagnostics, but it is not an alarm
and does not make coverage partial. The active index invocation reports skipped
records to its operator; read commands do not repeat them.

Claude discovery does not descend into directories whose names begin with `.`.

On 2026-09-03, Codex lifecycle and inter-agent exclusion key sets became
required minima rather than exact schemas. A payload is recognized as excluded
when its registered type is known and its keys are a superset of at least one
registered required-key set. Extra metadata cannot turn a known non-conversation
event into searchable content or block its source. A registered type missing a
required key, or an unregistered type, remains unknown and fails closed.

The 2026-09-06 compatibility fix applies this boundary to Codex
`token_usage_record` envelopes and legacy `ghost_snapshot` response items.
Their audited metadata shapes are excluded with an `excluded_metadata`
diagnostic; they do not create messages, alter session identity, or advance
conversation epochs. Additional metadata fields remain excluded. Legacy
`input_image` blocks without `detail` and `turn_aborted` events without a turn
ID retain the existing non-text and lifecycle exclusions.

## Consequences

Existing persisted tool-result and injected-context rows leave current state on
the next full parse. Both provider parser-state versions therefore advance, so
the first index after release reparses every source from byte zero. Semantic
work is content-addressed: only messages whose post-policy embedding-input
digest changed are re-embedded.

The retained `tool_output` database value remains valid historical/schema
vocabulary, so this decision needs no schema migration. Repair accounting is
run-scoped because no repair-count checkpoint column exists.

The Codex parser-state version advances for the monotone exclusion rule. The
first index under this change reparses every Codex source from byte zero so a
deterministic failure recorded by the previous exact matcher is retried even
when its file metadata is unchanged.
