# 0011. An unrecognised record warns and is indexed around; only a failure blocks

Status: Accepted
Date: 2026-11-18

## Context

ADR 0009 made reporting of unknown record shapes unconditional but kept
fail-closed as the default: a file with an unregistered shape stayed out of the
corpus unless the run was flagged with `--ignore-unknown-records` or
`CC_SEARCH_INDEX_IGNORE_UNKNOWN_RECORDS=1`. The flag was never set on the
operator's machine (`~/.config/cc-search-chats/index.env` did not exist on
2026-11-18). On that date 102 Claude Code 2.1.325 files were blocked by one key,
`modelCallCostUSD`, added to the already registered `cost-state` metadata
record. The Claude parser matched metadata key sets exactly, so every key
Claude Code added to a known metadata type needed a parser release (2.4.1,
2.4.2, 2.4.3, 2.5.1, 2.5.2), whereas Codex metadata key sets have been
required minima since 2026-09-03 (ADR 0003).

The governing record is
[`2026-11-18-bbs-unknown-records-warn-not-block.md`](2026-11-18-bbs-unknown-records-warn-not-block.md):

- Brian, 2026-11-18: *"go look at prior precedent. if we are able to search it,
  then it's a warn. a block is because something's failed."*
  (`ccchat:v1:claude:df736ff6-7aff-45a9-9d02-b9a279a93026:uuid:de6e6ce4-c9e2-4104-a300-5aa71401d0a0`,
  resolved on 2026-11-18).
- Brian, 2026-11-18, asked whether the rest of a file should still be indexed,
  with a warning, when one user or assistant message has a body the reader
  cannot use: *"yes, but also is this a real issue? ..."*
  (`ccchat:v1:claude:df736ff6-7aff-45a9-9d02-b9a279a93026:uuid:32974400-4d3e-451c-98c8-91a291b3c233`).
  No occurrence was found in the live index or the git history that day.

## Decision

1. An unrecognised native record never blocks its file. `index` indexes the
   file with that record omitted, counts it, and reports it as an
   `unknown_record_shape` warning with the `ignored` disposition, on every run
   and on every machine. No operator file, environment variable or deployment
   step is involved. This supersedes ADR 0009 point 2 ("Fail-closed remains
   the default") and the sentence of ADR 0003 that a registered type missing a
   required key, or an unregistered type, "fails closed"; such a record
   remains unknown and is reported, but no longer blocks.
2. Format drift in the body of a known record type is an
   unrecognised record under point 1, not a failure. This reverses the
   "payload" part of ADR 0009 point 2 ("Identity, payload and admission
   failures are not shapes and still block"). The codes moved are Claude
   `missing_message` and `non_object_message`, Codex `invalid_payload`, and
   Antigravity `missing_required_key`, `invalid_field_type`,
   `invalid_created_at` and `missing_user_request`. Each now carries a
   key-name shape whose record path names the record type, so a left-out
   `user` or `assistant` record is visible as such (for example
   `user/message`).
3. A file is blocked only when something has failed: unreadable or truncated
   bytes, a source replaced during its read, conflicting content for one
   identity, a parser invariant, Antigravity first-record admission, and a
   record whose session or message identity is missing or unreadable (Codex
   `unsupported_source_shape` and `unsupported_session_identity` on
   `session_meta`, Claude `missing_message_uuid`). Each of those goes to the
   human individually before it changes.
4. ADR 0003's required-minimum rule extends to Claude metadata key sets. A
   payload is excluded metadata when its `type` is registered and its keys are
   a superset of at least one registered key set for that type. A registered
   type missing a required key, or an unregistered type, remains unknown and
   is reported under point 1. Only registered metadata types are matched, and
   the match runs before any conversation handling, so a `user`, `assistant`
   or `system` record is never treated as metadata.
5. `--ignore-unknown-records` and `CC_SEARCH_INDEX_IGNORE_UNKNOWN_RECORDS`
   remain accepted so existing invocations and `index.env` files keep working;
   they change nothing. No flag restores blocking.
6. Reporting, acknowledgement and `coverage.completeness` are unchanged from
   ADR 0009 points 1, 3, 4 and 5. The `blocked` disposition stays valid
   vocabulary: it now appears only for a failure row an earlier release
   wrote, until the next `index` retries that file.

## Consequences

- The Claude parser-state version advances 11→12, so the first index after
  installation reparses Claude sources once; unchanged embeddings are reused.
  The Codex and Antigravity versions do not advance. A file one of them
  blocked is retried because its recorded failure code is now an unknown-shape
  code, which the refresh no longer treats as blocking.
- A new upstream shape no longer takes sessions offline until a parser
  release. It becomes a warning and a status line until a parser registers it
  or the operator acknowledges it.
- What a drifted record held is out of the corpus until a parser registers its
  form. When the record is user or assistant conversation, that text is not
  searchable, and the warning's record path is the signal.
- Supersedes ADR 0009 point 2 and the "fails closed" sentence of ADR 0003's
  Decision; this record does not edit either.
- Design: none separate; the implementation and its tests are the 2.5.3
  change set.
