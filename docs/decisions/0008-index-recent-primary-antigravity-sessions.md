# 0008. Index recent primary Antigravity sessions under a new design

Status: Accepted
Date: 2026-10-08

## Context

The accepted cross-vendor design of 2026-08-10 lists "Agy sessions and rendered
transport archives never enter the searchable corpus" as a failure criterion
(`docs/design-plans/2026-08-10-cross-vendor-semantic-search.md`, Scope and
Failure sections). Discovery enforces it with the `non_native_agy` diagnostic in
`src/cc_search_chats/providers/source_discovery.py`, and tests cover the
exclusion. No decision record explained it; the project knew Antigravity only as
a plugin consumer of search, not as a session source.

On 2026-10-08 an Antigravity agent working in another repository could not
verify its own chat citations through this tool. A read-only scan of the
Antigravity CLI store under `~/.gemini/antigravity-cli/brain/` (derived
evidence from an Opus investigator; verify before relying) found 234
transcripts, of which 199 are subagent sessions; a truncating
`transcript.jsonl` beside a complete `transcript_full.jsonl`; unstable step
indexes; and a SQLite sidecar outside the session root that alone records
workspace and parent session. Gemini CLI chat files under `~/.gemini/tmp/` are a
different tool with a different, mostly non-JSONL, format.

The governing records are:

- Brian, 2026-10-08: *"yes, I don't care about subagent work for agy -- right,
  do we care about any of this? but yes, it's a new design and I only care about
  the most recent agy"*
  (`ccchat:v1:claude:42102a00-89eb-4f30-ad4e-e12c7a65e697:uuid:078509b3-01ba-4056-981f-d7e288b6ad33`).
- Supervisor, 2026-10-08: proposed this record, asking whether Gemini CLI chats
  stay out of scope and only Antigravity primaries come in. Brian answered:
  *"yes"*
  (`ccchat:v1:claude:42102a00-89eb-4f30-ad4e-e12c7a65e697:uuid:780c7360-079f-425d-90f8-a379384d3358`).

## Decision

The Agy exclusion is narrowed. Primary Antigravity CLI sessions are an in-scope
corpus for a new design. Antigravity subagent sessions stay excluded. Gemini
CLI chat files stay out of scope. Within primaries the scope is "most recent";
the design makes that bound precise.

## Consequences

- The existing `non_native_agy` discovery exclusion stays in force until the
  new design replaces it. Nothing in this record changes current behaviour.
- The design must settle, among other things: how primary sessions are selected
  without reading the SQLite sidecar or with an explicit ruling to read it;
  whether `transcript_full.jsonl` is the source; ordinal-plus-digest locators
  for a provider without stable record identity; and a new ordered migration
  for the provider constraint. Those are the design's open tickets, not entries
  in this repository's open-questions file.
- This record was written after the 2.3.7 release commit and is independent of
  it.
