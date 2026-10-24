# 0009. Report every unknown record shape; ignore under a flag; acknowledge by file

Status: Accepted
Date: 2026-10-24

## Context

ADR 0003 makes an unregistered native record shape fail closed: the whole file
stays out of the corpus until a parser release registers the shape. Between
2026-10-10 and 2026-10-24 that produced three parser releases (2.4.1, 2.4.2,
2.4.3) for Claude Code 2.1.295–2.1.302 and Codex CLI 0.164.0 shapes, each
taking sessions offline for days. A second family of unknown shapes (Codex
`unknown_metadata_shape` and `unknown_compaction`, Claude
`invalid_compact_boundary`) was in no blocking, skippable or repaired set and
was dropped with no signal at all; on 2026-10-24 about 2,000 such Codex records
across 162 of 227 local files were in that state until the separate
outer-shape fix in commit `59bbbe2`.

The governing records (verbatim in
`.notes/decisions/2026-10-24-codex-additional-tools-and-noisy-ignore-flag.md`,
locators in the design's authority table):

- Brian, 2026-10-24: *"yes, we need a noisy ignore so that things can get on
  with the work and I know to fix things properly."*
- To the proposal of ignoring only shapes with no known prose-bearing keys:
  *"'no known prose-bearing keys' fascinating, how do we know what we don't
  know?"* The narrow heuristic was withdrawn; the broad default was assented
  to with *"ok"*.
- To unconditional reporting: *"... um. sure, though that's going to get very
  old. How do we maintain a denylist of stuff we know we don't care about?"*
- To an operator file: *"op file is fine ... just a file, I think. git
  tracked."* and *"yes, it travels by git"*.
- The written design was accepted: *"yes, the design seems fine"*.

## Decision

1. Every unregistered record shape the parsers meet is reported, in both
   modes, by a key-name fingerprint: provider, record path, and the sorted
   key names of the unit that failed recognition followed by the
   `payload.`-prefixed key names of its nested payload
   (`core/record_shape.py`). Values never enter the fingerprint; the record
   path carries the writer's own discriminators (`type`, `source`, and for an
   unknown message role the role), so two unknown roles are two shapes
   (ruled 2026-10-24). A unit that is not a JSON object has no keys and
   cannot be acknowledged. The report
   is derived from current rows (`source_file_current.unknown_shapes` and
   `source_failure_current.failure_shape`, migration 12), so it persists
   across unchanged runs and no separately maintained table can go stale.
   Every command carries one `unknown_record_shape` warning per pending
   shape; `coverage` gains three scalars; `index --status` lists pending and
   acknowledged shapes.
2. Fail-closed remains the default. `index --ignore-unknown-records`, or
   `CC_SEARCH_INDEX_IGNORE_UNKNOWN_RECORDS=1` in the operator's `index.env`,
   makes a file with an unknown shape index with that record omitted and
   counted, for that run only. Identity, payload and admission failures are
   not shapes and still block. A flagged run retries files an earlier run
   blocked on an unknown-shape code. The packaged unit is unchanged.
3. Acknowledgement is operator-owned, exact, and applied at index time: the
   builder reads `$XDG_CONFIG_HOME/cc-search-chats/acknowledged-shapes.toml`,
   validates it closed, and stamps matching shapes `acknowledged` so they stop
   warning but stay counted. The file travels between machines by the
   operator's own git. Readers never open it. A malformed or unreadable file
   stops the run with exit 2 before the systemd scope or any database write.
4. There is no prose-key heuristic: a key-name check can only recognise keys
   already seen, so it would protect against the case least in need of
   protection.
5. `coverage.completeness` keeps its meaning: a run that ignored records and
   blocked nothing is `complete`; the warning carries the condition.
6. ADR 0003's required-minimum rule extends to the Codex `turn_context` and
   `world_state` outer wrappers (commit `59bbbe2`), which was the cause of the
   silent family's size.

## Consequences

- The Codex (6→7) and Claude (8→9) parser-state versions advance so the first
  index after installation reparses both corpora and the first report covers
  every indexed file. A blocked file contributes only the shape that blocked
  it (one failure row, one shape); its remaining unknown shapes appear once a
  flagged run indexes around the first. The reparse is roughly half an hour
  per machine, extrapolated from the 2.4.3 run (640 MB of Codex in about
  twelve minutes; about 1.0 GB of Claude). Migration 12 requires the ADR 0007
  backup first.
- A new upstream shape no longer takes sessions offline until a release when
  the timer runs flagged; it becomes a warning and a status line until a
  parser registers it or the operator acknowledges it. Registering shapes
  found by the baseline stays ordinary parser work scheduled from the report.
- The design's remaining instance of the exact-match pattern, the
  `world_state` payload guard, is not observed to drop anything and is a
  separate fix if it ever does.
- Supersedes the Antigravity design's decision-table rejection of "Skip
  unknown records" for flagged runs
  (`docs/design-plans/2026-10-09-antigravity-primary-sessions.md`).
- Design: `docs/design-plans/2026-10-24-unknown-shape-reporting.md`.
  Operator procedure: `docs/runbooks/postgresql-index-maintenance.md`,
  "Acknowledge an unknown shape".
