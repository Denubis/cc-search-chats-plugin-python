# Unknown Record Shapes: Report Always, Ignore Under a Flag, Acknowledge by File

**Status:** Current design

**Amends:** ADR 0003 (`docs/decisions/0003-native-record-policy.md`, lines
55–60, "an unregistered type … remains unknown and fails closed") and the
Antigravity design's decision-table row that rejected "Skip unknown records"
unless "Overturned by the human"
(`docs/design-plans/2026-10-09-antigravity-primary-sessions.md`, line 216).
Fail-closed remains the default; this design adds an unconditional reporting
channel, an opt-in ignore mode for the unknown-shape family only, and an
operator acknowledgement file. A new ADR records the amendment when the design
is implemented.

## Purpose

Each new Claude Code or Codex release can introduce a record shape the parsers
have never seen. Today that takes whole sessions out of the search corpus until
a parser release ships (2.4.1, 2.4.2 and 2.4.3 inside a fortnight), while a
second family of unknown shapes is dropped with no signal at all. The operator
wants indexing to keep working when a new shape appears, wants to be told every
time it does, and wants a way to say "I have looked at this one and do not
care" without a release. This design delivers those three properties without
widening what the corpus persists.

## Authority Sources

| Decision or instruction | Exact source | Resolver | Resolution condition |
|---|---|---|---|
| A noisy ignore flag is wanted: "We probably need a 'ignore blocks' flag that's noisy". | `ccchat:v1:claude:d7cb779c-589c-4825-aeea-26c925b8943c:uuid:293a18c3-f8ab-41f8-b0ae-7e3a61645fb1` | `cc-search-chats resolve '<exact source>' --reference-only --json` | `resolved`; one user message, 2026-10-24T00:15:19.167Z. |
| Ruling: "yes, we need a noisy ignore so that things can get on with the work and I know to fix things properly"; and the challenge to the narrow heuristic, "how do we know what we don't know?" | `ccchat:v1:claude:d7cb779c-589c-4825-aeea-26c925b8943c:uuid:a570a119-0917-4b29-91e6-8d6dc3955df3` | `cc-search-chats resolve '<exact source>' --reference-only --json` | `resolved`; one user message, 2026-10-24T00:33:04.308Z. |
| Assent to the broad default (any unregistered shape, no prose-key heuristic): "ok". | `ccchat:v1:claude:d7cb779c-589c-4825-aeea-26c925b8943c:uuid:f2902893-5d5c-4277-bd53-9b075b756ec0` | `cc-search-chats resolve '<exact source>' --reference-only --json` | `resolved`; one user message, 2026-10-24T00:34:43.155Z. |
| Three-point shape accepted (unconditional reporting; flag governs block-versus-ignore only; completeness unchanged), with the denylist concern: "sure, though that's going to get very old. How do we maintain a denylist of stuff we know we don't care about?" | `ccchat:v1:claude:d7cb779c-589c-4825-aeea-26c925b8943c:uuid:669f36e5-12dd-42cd-a960-f3649cb83a0d` | `cc-search-chats resolve '<exact source>' --reference-only --json` | `resolved`; one user message, 2026-10-24T01:58:44.854Z. |
| Acknowledgement is an operator file, git tracked; and "fix the bug" (the Codex metadata outer-shape exact match). | `ccchat:v1:claude:d7cb779c-589c-4825-aeea-26c925b8943c:uuid:034bc69c-e05c-4bb7-8cf7-042413d7e9da` | `cc-search-chats resolve '<exact source>' --reference-only --json` | `resolved`; one user message, 2026-10-24T02:07:27.915Z. |
| The file travels between machines by the operator's own git, not by release: "yes, it travels by git". | `ccchat:v1:claude:d7cb779c-589c-4825-aeea-26c925b8943c:uuid:86e9ba9a-9085-4e7f-9b5c-9e0715d435bd` | `cc-search-chats resolve '<exact source>' --reference-only --json` | `resolved`; one user message, 2026-10-24T02:15:27.725Z. |
| The written design is accepted: "yes, the design seems fine"; the metadata fix is to be committed. | `ccchat:v1:claude:d7cb779c-589c-4825-aeea-26c925b8943c:uuid:e9352d86-676f-4e00-a7ea-072a81153143` | `cc-search-chats resolve '<exact source>' --reference-only --json` | `resolved`; one user message, 2026-10-24T02:27:56.421Z. |

Until the session file `d7cb779c-….jsonl` is indexed (it is the live session
while this design is written), the fallback check is
`grep -c '"uuid":"<uuid>"' ~/.claude/projects/-home-brian-people-Brian-cc-search-chats-plugin-python/d7cb779c-589c-4825-aeea-26c925b8943c.jsonl`,
which prints `1` for each row. The decision record
`.notes/decisions/2026-10-24-codex-additional-tools-and-noisy-ignore-flag.md`
holds the same rulings verbatim with their context.

## Universe of discourse

- **Writers:** Claude Code, Codex CLI and the Antigravity CLI, which change
  their JSONL record shapes without coordination. Their logs are read-only.
- **Builder:** `cc-search-chats index`, run by a person, an agent, or the
  nightly systemd user timer (`src/cc_search_chats/systemd/`). It is the only
  process that parses native records and the only one that will read the
  acknowledgement file.
- **Readers:** `search`, `extract`, `resolve`, `events`, `index --status`, and
  the agent consumers in `skills/search-chat/` and `commands/search-chat.md`,
  which already read `warnings`, `coverage.source_issues` and
  `coverage.completeness` from the schema-v5 envelope.
- **Operator:** the human who owns `~/.config/cc-search-chats/`, keeps it in
  their own git, and decides whether a reported shape is housekeeping or a
  parser job.
- **Data:** record *shapes*, meaning a provider, a record path such as
  `response_item/additional_tools`, and the sorted top-level key names of the
  envelope and payload. Key names only. No values, no prose, no tool
  definitions.
- **Exclusions:** this design does not change what is persisted as searchable
  content, does not expose unknown records in any search mode, does not touch
  the legacy `CC_SEARCH_DB_PATH` backend, and does not alter identity,
  payload, or admission failures, which are not record shapes.

## Current state

- Each provider parser emits a diagnostic for an unrecognised record and
  continues (`claude.py:1394-1454`, `antigravity.py:567-574`,
  `codex.py:1775-1855`). Refresh aborts the whole file on the first diagnostic
  whose code is in the adapter's `unsupported_codes`
  (`storage/postgresql/refresh.py:839-875`), records one row in
  `source_failure_current` (`refresh.py:1376-1472`) and never replans the file
  while device, inode, size, mtime and parser version match
  (`refresh.py:623-652`). `source_failure_summary` (`refresh.py:218-246`)
  splits those rows into `retry_after_parser_update`, `retryable_failures`
  and `needs_attention`.
- The blocking sets (`providers/registry.py:638-647`, `690-701`, `746-754`)
  mix two kinds of code. Shape codes: Codex `unknown_response_item`,
  `unknown_event`, `unknown_outer_type`, `unknown_content_block`,
  `unknown_role`; Claude `unknown_conversation_record`,
  `unknown_content_block`, `unknown_role`; Antigravity
  `unknown_record_pair`. Non-shape codes: Codex `unsupported_source_shape`,
  `invalid_payload`, `unsupported_session_identity`; Claude
  `missing_message`, `non_object_message`, `missing_message_uuid`;
  Antigravity `missing_required_key`, `invalid_field_type`,
  `missing_user_request`, `invalid_created_at`.
- Three unknown-shape codes are in no blocking, skippable or repaired set and
  so are dropped with no diagnostic reaching any output: Codex
  `unknown_metadata_shape` and `unknown_compaction`, Claude
  `invalid_compact_boundary`. On this machine on 2026-10-24 a read-only parse
  of every Codex file found about 2,000 `turn_context` and `world_state`
  records in 162 of 227 files in that state, because
  `_metadata_outer_shape` (`providers/codex.py:1526-1531`) requires an exact
  outer key set and modern Codex adds `ordinal`. The parser test at
  `tests/test_provider_codex.py:2174` pins that a conversation-shaped payload
  under those outer types is reported `unknown_metadata_shape`; nothing pins
  what refresh does with it. That exact-match defect was fixed separately in
  commit `59bbbe2` under ADR 0003's required-minimum rule, with no
  parser-state bump because no persisted state differed; after it, the local
  corpus carries zero `unknown_metadata_shape` records and the remaining
  silent-family exposure is the conversation-shaped guard and the
  `unknown_compaction` / `invalid_compact_boundary` codes.
- Skipped records already exist for malformed JSON, invalid encoding and
  invalid Unicode: `_skipped_record_diagnostics` (`refresh.py:878-935`) builds
  one `record_skipped` diagnostic per ordinal, `_messages_without_skipped_records`
  (`refresh.py:938-949`) drops every projection at a skipped ordinal, and the
  checkpoint's `skipped_record_count` accumulates across appends
  (`refresh.py:1349`; column from `skipped_record_coverage_schema.sql`). ADR
  0003 makes these deliberately quiet.
- Warnings are the latest `refresh_run.diagnostics` row (`cli.py:751-783`,
  `1016-1036`). A run plans only changed files, so an unchanged blocked file
  adds no diagnostic; production run 83 recorded five blocked sources and two
  warnings. `index --status` hides `record_skipped` warnings (`cli.py:1659`).
- The index CLI accepts `--project`, `--all`, `--force-retry` and the
  exclusive `--migrate`/`--status` (`cli.py:2786-2822`); `--force-retry` is
  plumbed through `index_corpus` to `refresh_native_sources`
  (`cli.py:1692-1700`, `refresh.py:2707-2716`). The containment re-exec keeps
  `sys.argv` verbatim (`cli.py:1402-1420`). The service unit's `ExecStart` has
  no flags and loads the optional `~/.config/cc-search-chats/index.env`. No
  `CC_SEARCH_*` variable controls index behaviour today.
- Schema-v5 tests pin `coverage` keys as a superset and require `roots` to be
  its only list-valued field (`tests/test_cli_journey.py:45-67`);
  `index_state` and `semantic` are pinned by exact equality. `completeness` is
  `complete` or `partial` (`cli.py:871-878`) and its meaning is stated in
  CLAUDE.md, README.md and the search-chat skill.

## Goals and non-goals

Goals:

1. Every unregistered record shape the parsers meet is reported, once per
   distinct shape, on `index`, `index --status` and `search`, with enough
   coordinates to find one example, and the report persists until the shape
   is registered by a parser release or acknowledged by the operator.
2. With an explicit opt-in, a file containing an unknown shape is indexed
   with that record left out instead of being blocked. Without it, behaviour
   is unchanged.
3. The operator can acknowledge a shape in a file they own, by exact key set,
   so that it stops warning but stays counted.
4. Nothing new is persisted as content, and no supported mode exposes an
   unknown record.

Non-goals: changing what `completeness` means; a heuristic that guesses
whether an unknown shape carries prose; read-time configuration for search;
registering the shapes found during the baseline (that is ordinary parser
work, scheduled from the report).

## Design

### Shape fingerprint (core and providers)

A shape is a frozen value `(provider, record_path, keys)` where `record_path`
is the provider's record family and type joined by `/` (Codex
`response_item/additional_tools`, `event_msg/<type>`, `<outer_type>` for an
unknown outer; Claude `<record_type>` or `<record_type>/content/<block_type>`;
Antigravity `<source>/<record_type>`), and `keys` is the sorted tuple of
top-level key names of the envelope followed by `payload.` prefixed top-level
payload key names. Values never enter the fingerprint. Its canonical string
form is the JSON of that triple, and its digest is the SHA-256 of that string.

Every diagnostic whose code is in the new per-adapter `unknown_shape_codes`
set carries a shape. The set is:

- Codex: `unknown_response_item`, `unknown_event`, `unknown_outer_type`,
  `unknown_metadata_shape`, `unknown_compaction`, `unknown_content_block`,
  `unknown_role`.
- Claude: `unknown_conversation_record`, `unknown_content_block`,
  `unknown_role`, `invalid_compact_boundary`.
- Antigravity: `unknown_record_pair`.

`unsupported_codes` keeps its current members. The intersection of the two
sets is the family the flag governs. Non-shape blocking codes and the
Antigravity first-record admission block are untouched.

### Persistence (migration 12)

One ordered migration with a recorded checksum, adding three columns:

- `source_file_current.unknown_shapes jsonb NOT NULL DEFAULT '{}'`: a map
  from shape digest to `{shape, count, first_ordinal, first_line,
  first_byte_offset, disposition}` for shapes met in that file's current
  parse, where `disposition` is `ignored` or `acknowledged`. It is rebuilt
  from byte zero on a full reparse and merged on an append, exactly as
  `skipped_record_count` is.
- `source_failure_current.failure_shape jsonb`: the fingerprint of the
  record that blocked the file, when the failure code is a shape code; NULL
  otherwise.
- `refresh_run.ignored_unknown_record_count bigint NOT NULL DEFAULT 0`: how
  many records that run omitted under ignore mode.

The corpus-wide shape summary is a query that aggregates these two sources
over current rows: per digest, the shape, total records, file count, first
example (lowest root, file, line), dispositions present, and the parser
version that last observed it. No separately maintained shape table exists,
so removal, replacement or reparse of a file cannot leave a stale count.

The Codex and Claude parser-state versions advance with this change so the
first index after installation reparses every source from byte zero and the
summary starts from a complete baseline rather than filling in as files
change. Antigravity's version advances only if its diagnostics change shape.

### Ignore mode (index only)

`index --ignore-unknown-records`, or `CC_SEARCH_INDEX_IGNORE_UNKNOWN_RECORDS=1`
in the environment, enables ignore mode for that run. The flag is read only by
the `index` command; `search` and the other readers never consult it. The
nightly timer gains it by one line in the operator's existing
`~/.config/cc-search-chats/index.env`; the packaged unit is unchanged.

In ignore mode, `_parse_batch` no longer raises for a diagnostic whose code is
in both `unknown_shape_codes` and `unsupported_codes`. Instead:

- the record's ordinal joins the skipped-ordinal set for that batch, so every
  projection with an alias at that ordinal is dropped before staging, and the
  shape is counted into the file's `unknown_shapes` with disposition
  `ignored`;
- `unknown_content_block` keeps the parsers' existing behaviour (the
  message's recognised blocks are still emitted; `claude.py:1153-1174`,
  `codex.py:1144-1176`) and the shape is counted without dropping the
  ordinal;
- a shape code that is not in `unsupported_codes` (the silent family) is
  counted the same way in both modes, which is what makes the reporting
  unconditional;
- non-shape blocking codes raise exactly as today.

Codex trailing candidates (`codex.py:1665-1708`) are carried across batches,
and the skipped-ordinal set is batch-local (`refresh.py:1194-1203`). An
ignored ordinal produces no candidate today, so no candidate can escape; the
acceptance criteria include a test that pins this with an unknown item
immediately before a batch boundary.

The run records `ignored_unknown_record_count` alongside its other counts.
`coverage.completeness` is computed as today: a run that ignored records and
blocked nothing is `complete`.

### Acknowledgement file (operator-owned, index-time)

Path: `$XDG_CONFIG_HOME/cc-search-chats/acknowledged-shapes.toml`, defaulting
to `~/.config/cc-search-chats/acknowledged-shapes.toml`. Absent is valid and
means nothing is acknowledged. The project ships the format in the runbook and
an example under `docs/`, never a live list.

```toml
[[shape]]
provider = "codex"
record_path = "response_item/additional_tools"
keys = ["id", "role", "tools", "type"]
note = "developer-role tool definitions; no prose"
acknowledged = 2026-10-24
```

Matching is exact on `(provider, record_path, keys)`; an added or removed key
is a different shape and warns again. `index` loads the file before any
refresh work. A file that is not valid TOML, an entry missing a field, an
unknown provider, an empty or unsorted `keys` list, or a duplicate shape fails
the run closed with exit 2 and a message naming the entry, before the systemd
scope is created and before any database write. A valid file stamps
`disposition: acknowledged` on matching shapes in the current rows as they
are written; shapes already in current rows whose disposition changes are
updated in the same run without reparsing their files. Search and status
therefore reflect an acknowledgement after the next `index` run on that
machine, and on the other machine after a `git pull` and its next run.

### Reporting (all readers)

Warnings are extended with one `unknown_record_shape` entry per distinct
unacknowledged shape, derived from the current-row summary rather than the
latest run's diagnostics, so it persists across runs and across unchanged
files:

```json
{"code": "unknown_record_shape", "provider": "codex",
 "record_path": "response_item/additional_tools",
 "keys": ["id", "role", "tools", "type"],
 "record_count": 7, "file_count": 5,
 "first_source_root_id": "…", "first_source_file_relative": "2026/10/23/…jsonl",
 "first_source_line": 3, "dispositions": ["blocked"],
 "parser_state_version": 6}
```

`dispositions` is the set observed across files: `blocked` while the file is
in `source_failure_current`, `ignored` when it was indexed under the flag.
Warnings are lists, so a `keys` list is allowed there; `coverage` gains only
scalars: `unknown_shape_records` (total records across current files,
acknowledged or not), `unknown_shapes_pending` (distinct unacknowledged
shapes) and `unknown_shapes_acknowledged`. `index --status` additionally
returns `unknown_shapes: {pending: [...], acknowledged: [...]}` at the top
level of its envelope, bounded by the number of distinct shapes, carrying the
same fields as the warning plus `note` and `acknowledged` for acknowledged
entries. Search does not return that list.

Human output prints one line per pending shape after the results, in the
form `unknown shape codex response_item/additional_tools {id, role, tools,
type}: 7 records in 5 files, first 2026/10/23/….jsonl:3, blocked`. Agent
guidance in the skill and command tells a consumer that these warnings
describe parser coverage, are not missing conversations, and should be
mentioned only when they bear on the answer or the user asks about coverage.

### Documents brought current at implementation

- New ADR 0009, referencing ADR 0003: unknown shapes are always reported;
  fail-closed is the default; ignore mode is opt-in per run; acknowledgement
  is operator-owned and exact. ADR 0003's sentence at lines 59–60 gains a
  pointer to ADR 0009.
- Antigravity design line 216: the "Skip unknown records" rejection row is
  annotated as overturned for flagged runs by this design.
- CLAUDE.md CLI contract and JSON contract; README JSON contract;
  `skills/search-chat/SKILL.md`; `commands/search-chat.md`;
  `docs/architecture/database.md` (new columns and the derived summary);
  `docs/runbooks/laptop-deployment.md` and
  `docs/runbooks/postgresql-index-maintenance.md` (migration 12 with its ADR
  0007 backup, `index.env` flag, acknowledgement file format and example).

## Failure and recovery

- **Unknown shape without the flag:** the file blocks exactly as today, with
  `source_refresh_failed`; the shape is additionally recorded on the failure
  row and appears in the `unknown_record_shape` warning with disposition
  `blocked`. Recovery is a parser release or a flagged run.
- **Unknown shape with the flag:** the record is omitted, the file is indexed,
  the warning appears with disposition `ignored`, `completeness` is
  `complete`. Recovery is a parser release (the version bump reparses the
  file and the record is classified) or an acknowledgement.
- **Non-shape failure with the flag:** blocks as today. The flag cannot make
  an identity, payload or admission failure pass.
- **Malformed acknowledgement file:** `index` exits 2 before creating its
  systemd scope or writing anything; the timer's next run fails the same way
  until the file is fixed, and `index --status` keeps reporting the previous
  state. Nothing is silently un-acknowledged.
- **Acknowledged shape changes keys:** the new key set is a new shape and
  warns. The old entry stays in the file as history until the operator removes
  it.
- **Migration 12 pending:** every PostgreSQL command reports
  `maintenance_required` (exit 6) until `index --migrate` runs after an ADR
  0007 backup, exactly as for migration 11.
- **Parser version bump:** one full reparse on the first index after
  installation; on this machine the 2.4.3 run reparsed 640 MB of Codex in
  about twelve minutes including the semantic build, and the Claude corpus is
  about 1.0 GB, so roughly half an hour once. A candidate failure leaves the
  previous coherent selection current, as today.

## Decisions

1. **Reporting is unconditional; the flag governs only block-versus-ignore.**
   Alternative: report only under the flag. Rejected because the silent
   family would then stay silent on default runs, and because the operator's
   stated need is to know, whether or not a run tolerated the shape.
   Invalidated if reporting is shown to leak values rather than key names.
2. **No prose-key heuristic.** Alternative: ignore only shapes lacking
   `content`/`text`/`output`-style keys. Rejected on the human's challenge
   that such a check can only recognise keys already seen, so it protects
   against the case least in need of protection. Durable; recorded in the
   decision file.
3. **Acknowledgement is an operator file applied at index time.**
   Alternative A: register shapes only through code releases; rejected as the
   release cadence the operator is trying to escape. Alternative B: filter
   warnings at read time from the file; rejected because search would then
   read operator configuration, the two machines would disagree until a pull
   anyway, and the project's contract keeps search free of maintenance and
   configuration. Invalidated if a reader command gains a sanctioned
   configuration surface.
4. **Shape summary is derived from current rows, not a maintained table.**
   Alternative: an `unknown_shape_current` table updated incrementally;
   rejected because file removal, replacement and reparse would need matching
   bookkeeping and a stale count would be undetectable. Cost: an aggregate
   query over `source_file_current` on every envelope; the table has about
   4,200 rows on this machine and the aggregate is bounded by distinct shapes.
   Invalidated if that query measurably threatens the five-second literal
   deadline.
5. **Parser versions bump to baseline the summary.** Alternative: let counts
   fill in as files change; rejected because the first report would then
   understate the corpus for weeks and the operator could not trust a quiet
   status. Cost: one reparse of roughly half an hour per machine.
6. **Env variable plus flag, unit unchanged.** Alternative: append the flag to
   `ExecStart`; rejected because the packaged unit would then encode an
   operator policy and `tests/test_systemd_units.py` guards the unit against
   operator-specific content. Invalidated if the unit later gains a sanctioned
   per-site drop-in.
7. **`completeness` keeps its meaning.** Alternatives: a new enum value, or
   `partial` while shapes are pending. Rejected because `complete` already
   means no file was blocked, the skill's existing guidance already surfaces
   warnings to agents, and `partial` would stop distinguishing a blocked file
   from a tolerated record. Durable, under the human's acceptance of the
   three-point shape.

## Acceptance criteria

- **AC1 fingerprint is key names only.** For each provider, a fixture record
  whose unknown payload contains a long string value and a nested object
  produces a diagnostic whose shape equals the expected sorted key tuple and
  whose JSON serialisation contains none of the values. Failure: any value
  substring appears in the fingerprint, warning or status output.
- **AC2 default unchanged.** Without the flag, the existing refresh tests for
  unknown shapes (including `tests/postgresql/test_refresh.py:1759-1811` and
  the 2.4.3 `additional_tools` block test at parser version 5) pass
  unchanged, and the failure row now carries `shape`. Failure: a blocked file
  is indexed, or `shape` is NULL for a shape code.
- **AC3 ignore mode indexes around the record.** A file with visible prose
  before and after an unknown response item, indexed with the flag, yields
  `completeness: complete`, zero blocked files, both prose rows searchable,
  nothing from the unknown record in any stored row or any search mode, and
  `unknown_shapes` on the checkpoint with `count 1`, `disposition ignored`.
  Failure: the file blocks, the unknown record's strings are stored, or the
  count is absent.
- **AC4 non-shape codes still block under the flag.** A Codex file with an
  identity failure and a Claude file missing a message uuid block with the
  flag set. Failure: either is indexed.
- **AC5 batch boundary.** An unknown Codex item as the last record of one
  bounded batch, followed by prose in the next, indexes under the flag with no
  candidate from the unknown ordinal and the trailing candidate pairing
  unchanged. Failure: any row aliases the unknown ordinal.
- **AC6 silent family reported in both modes.** A Codex `turn_context` whose
  payload lacks a required field (the conversation-shaped case pinned at
  `tests/test_provider_codex.py:2174`, which stays `unknown_metadata_shape`
  after the separate outer-shape fix) is counted in `unknown_shapes` and
  produces an `unknown_record_shape` warning with the flag off and with it on;
  the file is indexed in both. A recognised `turn_context` with extra keys
  produces no diagnostic and no count. Failure: no warning for the unknown
  case, a count for the recognised case, or the file blocks without the flag.
- **AC7 persistence across runs.** After a blocked or ignored shape is
  recorded, a second `index` with no source changes creates no run and still
  reports the same warning on `index --status` and `search --literal --json`.
  Failure: the warning disappears when the latest run has no diagnostics.
- **AC8 dedupe.** Seven records of one shape across five files produce one
  warning with `record_count 7`, `file_count 5` and the lowest-ordered first
  example. Failure: seven warnings, or wrong counts.
- **AC9 acknowledgement.** With a valid file acknowledging a shape, the next
  index stamps `acknowledged`; the shape leaves `warnings` and
  `unknown_shapes_pending`, stays in `unknown_shape_records` and in
  `index --status`'s `acknowledged` list with its note. The same shape with
  one extra key warns again as a distinct shape. Failure: the acknowledged
  shape still warns, the records vanish from the total, or the extended shape
  is treated as acknowledged.
- **AC10 malformed file fails closed.** An invalid TOML file, an entry with
  an unknown provider, and a duplicate entry each make `index` exit 2 with a
  message naming the entry, with no run row and no systemd scope created.
  Failure: the run proceeds, or exits 0.
- **AC11 schema v5 stays additive.** `tests/test_cli_journey.py` superset and
  single-list assertions pass with the three new coverage scalars; `search`
  envelopes carry no `unknown_shapes` list; `index --status` carries it.
  Failure: a list in coverage, or the list on search.
- **AC12 readers never read the file.** `search`, `extract`, `resolve` and
  `events` run with an unreadable acknowledgement file present and succeed.
  Failure: any reader opens or fails on the file.
- **AC13 flag plumbing.** `index --ignore-unknown-records` and
  `CC_SEARCH_INDEX_IGNORE_UNKNOWN_RECORDS=1` each enable the mode; the
  containment re-exec preserves the flag; the packaged unit is byte-identical
  to today's and `tests/test_systemd_units.py` passes. Failure: the mode is
  lost across re-exec or the unit changes.
- **AC14 migration and baseline.** On a database at schema 11, every command
  reports `maintenance_required` until `index --migrate` applies 12; the
  first index afterwards reparses every Codex and Claude source and the
  summary is non-empty if and only if the corpus holds an unregistered
  shape. Failure: a command mutates schema outside `--migrate`, or the
  summary stays empty while a seeded unknown shape exists.

## Implementation phases

1. **Fingerprint and registry.** Shape value in core, per-provider shape
   construction for every code in `unknown_shape_codes`, registry sets, and
   `shape` on the failure row (migration 12 column one). Owns AC1, AC2.
   Leaves: default behaviour unchanged, blocked shapes recorded. Depends on
   the separate Codex metadata outer-shape fix having landed.
2. **Ignore mode and per-file counts.** Flag and environment plumbing,
   `_parse_batch` behaviour, skipped-ordinal integration,
   `source_file_current.unknown_shapes` (migration 12 column two), parser
   version bumps, run count. Owns AC3, AC4, AC5, AC6, AC13, AC14 (migration
   part). Leaves: a flagged run indexes around unknown shapes and records
   them.
3. **Derived summary and reporting.** Aggregate query, warnings on all
   readers, coverage scalars, `index --status` list, human lines, agent
   guidance in skill and command. Owns AC7, AC8, AC11, AC14 (baseline part).
   Leaves: the operator sees every pending shape on every surface.
4. **Acknowledgement file.** Loader with closed validation, index-time
   stamping, documentation and example. Owns AC9, AC10, AC12. Leaves: the
   full lifecycle, new shape → warn → acknowledge or fix.
5. **Documents and release.** ADR 0009 and the ADR 0003 pointer, Antigravity
   row annotation, CLAUDE.md, README, runbooks, architecture doc, CHANGELOG,
   version. Deployment follows the preserving-upgrade route with the ADR 0007
   backup before migration 12, and is a separate authorised action.

## Verification and human judgment

Automation proves AC1 through AC14 in the disposable PostgreSQL cluster, the
non-PostgreSQL suite, and the repository gates. Deployment evidence proves the
migration, the baseline reparse and the first real summary on this machine.

Reserved for human judgment at UAT: whether the one-line-per-shape output on a
real search is informative rather than noise; whether the first baseline
report on this corpus names shapes the operator recognises as housekeeping;
whether acknowledging one of them through the file, pulling it on the other
machine and running `index` there feels like the lifecycle asked for; and
whether the nightly timer should carry the flag from the first deployment
(the supervisor's lean is yes, set in `index.env`; the operator decides when
the file is written).
