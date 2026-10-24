# Worklog

## Established before execution (2026-10-24)

- Design accepted by the human on 2026-10-24 (design authority table, last
  row). Plan written against `main` at
  `59bbbe247e96b8f576ff1e8df2b79f6f38ae9540`, which includes the Codex
  metadata outer-shape fix the design depends on (commit `59bbbe2`: 2,009
  silently dropped `turn_context`/`world_state` records now recognised; no
  message or parser-state change in any of 227 local Codex files; no
  parser-state bump).
- Production on this machine: cc-search-chats 2.4.3 at commit `12bd65c`,
  schema 11, index run 84, generation 81, semantic build 79, 0 blocked
  sources. Codex parser-state version 6, Claude 8, Antigravity 2.
- Timing evidence for the baseline reparse: the 2.4.3 index reparsed 640 MB
  of Codex in about twelve minutes including the semantic build
  (`~/.local/state/cc-search-chats/upgrades/12bd65cb…/`); the Claude corpus
  is about 1.0 GB (`du -sb ~/.claude/projects`).
- Read-only investigation (Opus, verified by the supervisor) established the
  mechanism facts cited in `plan.md` "Current repository evidence".

## Outcome 1: Unknown shapes are recorded (2026-10-24)

Workspace: task branch `unknown-shape-reporting` created from `main` at
`59bbbe2` in this checkout; no edit was made on `main`.

Changed surfaces:

- New `src/cc_search_chats/core/record_shape.py`: frozen `RecordShape`
  (provider, record_path, two-segment sorted keys), canonical compact JSON,
  SHA-256 digest, `record_shape()` constructor from key names only,
  `record_shape_from_json()` round-trip, `path_segment()` for untrusted type
  values.
- Codex, Claude and Antigravity diagnostics gain `shape: RecordShape | None`;
  every emission site for the design's `unknown_shape_codes` populates it
  (Codex 12 sites incl. `_message_content` now taking the message payload and
  record; Claude 9 sites with `record_type` threaded through the content
  extractors; Antigravity via `_rejected_shape`). Codex
  `_excluded_outer_metadata_diagnostic` split its token-usage branch to attach
  the shape only to the unknown case.
- `providers/registry.py`: `ParseDiagnostic.shape`, `ProviderAdapter.unknown_shape_codes`
  for all three adapters, Codex parser-state version 6→7, Claude 8→9.
- Migration 12 `unknown_shape_schema.sql`: `source_file_current.unknown_shapes`
  jsonb object, `source_failure_current.failure_shape` jsonb nullable,
  `refresh_run.ignored_unknown_record_count` (written first in Outcome 2).
- `storage/postgresql/refresh.py`: `_SourceRefreshError.shape` → `failure_shape`;
  `_Checkpoint.unknown_shapes`, `_SourcePlan.prior_unknown_shapes`,
  `_StagedBatch`, `_ParsedSource.unknown_shapes`, `_unknown_shape_entries`
  (silent family = `unknown_shape_codes - unsupported_codes`),
  `_merge_shape_entries`, stage table and publish upsert carry `unknown_shapes`.
- `docs/architecture/database.md`: relations table and refresh-publication
  paragraph describe the two columns, the fingerprint and the derivation rule.
- Tests: new `tests/test_record_shape.py` (7 tests); shape fingerprint classes
  appended to the three provider test files; `failure_shape` assertion on the
  2.4.3 `additional_tools` block test; new
  `test_silent_unknown_metadata_shape_is_counted_without_blocking` (AC6
  default mode, with a recognised extra-key `turn_context` control);
  migration ledger/pending/packaged pins include 12; schema-version pins in
  `test_cli_journey.py` and `test_background_search.py` moved 11→12.

Red evidence: `tests/test_record_shape.py` failed on import before the module
existed (by construction). PostgreSQL red observed before the refresh change:
`test_silent_unknown_metadata_shape_is_counted_without_blocking` and the
`additional_tools` `failure_shape` assertion failed (`None == {...}`); the
first also exposed a test-construction defect (shared session id across the
two fixture files), fixed by giving the control file its own `session_meta`.
Honest gap: the provider shape tests were written in the same pass as the
provider code and were not individually observed red.

Green evidence (2026-10-24, this checkout):

- `uv run --frozen pytest -q -m 'not postgresql'` → 1017 passed.
- `uv run --frozen pytest -q -m postgresql` → 160 passed after the two
  schema-version pins (first run: 158 passed, 2 failed on the 11→12 pins).
- `ruff check`, `ruff format --check`, `ty check`: all passed.
- `complexipy --failed --plain`: snapshot watermark passed, snapshot unchanged.
- `vulture`: exit 0. `pre-commit validate-config`: ok.

## Outcome 2: Ignore mode indexes around unknown shapes (2026-10-24)

Changed surfaces:

- `cli.py`: `index --ignore-unknown-records`; `_index_ignore_unknown_records(args,
  environ)` enables the mode from the flag or
  `CC_SEARCH_INDEX_IGNORE_UNKNOWN_RECORDS=1`; `_postgres_index_run` passes it to
  `index_corpus`. The containment re-exec needed no change (it forwards
  `sys.argv` and the environment), proven by
  `test_index_reexec_preserves_the_ignore_unknown_records_flag`.
- `refresh.py`: `index_corpus` / `refresh_native_sources` accept
  `ignore_unknown_records`; `_SourcePlan.ignore_unknown_records` and
  `blocking_codes` (unsupported minus unknown-shape codes when on);
  `_parse_batch` raises only for blocking codes; `_unknown_shape_entries`
  counts every shape code outside the blocking set and returns the ordinals to
  omit (blocking shape codes except `unknown_content_block`, whose message keeps
  its recognised blocks); omitted ordinals join the skipped set before staging;
  `_StagedBatch` / `_StagedSource` replace the positional tuples;
  `refresh_run.ignored_unknown_record_count` is written by both publishers and
  the no-publication branch; `RefreshResult` / `CorpusIndexResult` carry
  `ignored_unknown_record_count`. Adopted default beyond the plan text: a flagged
  run retries a file whose recorded failure code is an unknown-shape code
  without `--force-retry` (`_failed_observation_blocks`), because otherwise the
  nightly timer with the variable set would never pick up files blocked before
  the flag was turned on; non-shape failures keep their block.
- `docs/runbooks/laptop-deployment.md`: `index.env` paragraph gains the variable
  as operator policy, not a packaged default.
- Tests: `test_refresh.py` AC3 (`test_ignore_mode_indexes_around_an_unknown_response_item`,
  all `--agents`/`--tools` modes plus exhaustive, no alias at the unknown
  ordinal, checkpoint entry, run count, no failure row), AC4
  (`test_ignore_mode_keeps_non_shape_failures_blocking`: Codex identity-less
  `session_meta` and Claude missing uuid both block under the flag), AC5
  (`test_ignore_mode_batch_boundary_drops_only_the_unknown_ordinal`: reader
  wrapped to three-record batches, user message aliases `[1, 3]` pair across
  the boundary, assistant `[4]`, no ordinal 2); new
  `tests/postgresql/test_unknown_shapes.py` AC13 through `main()` for both
  routes (default run blocks, flagged run indexes, run row count 1, no decoy
  stored); `tests/test_cli.py` resolution table (six cases) and re-exec argv.

Red evidence: the CLI resolution and re-exec tests failed on the missing helper
and flag (7 failed); the PostgreSQL tests failed on
`index_corpus() got an unexpected keyword argument 'ignore_unknown_records'`.
Two test-construction defects were found and fixed on the way to green: the
CLI test's only source was the blocked file, so a fresh database had no corpus
to publish (pre-existing behaviour, `coherent corpus publication did not select
a build`), fixed by seeding a clean Claude file; and the AC3 test's second run
exposed that a flagged run did not retry an already-blocked file, which led to
the adopted default above.

Green evidence (2026-10-24, this checkout):

- `uv run --frozen pytest -q -m postgresql` → 165 passed.
- `uv run --frozen pytest -q -m 'not postgresql'` → 1024 passed.
- `ruff check`, `ruff format --check`, `ty check`: passed; `complexipy`
  watermark passed with the snapshot unchanged; `vulture` exit 0.
- `git diff --exit-code 59bbbe2 -- src/cc_search_chats/systemd/` → unit
  byte-identical; `tests/test_systemd_units.py` passes within the suite.
- `cc-search-chats index --help` lists `--ignore-unknown-records` with the
  variable in its help text.

## Outcome 3: Every reader reports pending shapes (2026-10-24)

Changed surfaces:

- New `storage/postgresql/unknown_shapes.py`: `UnknownShapeReport` (shape,
  record_count, file_count, first root/file/line, dispositions, max parser
  version; `pending` is any disposition other than `acknowledged`) and
  `unknown_shape_summary(connection)`: one SQL aggregate over
  `source_file_current.unknown_shapes` (`jsonb_each`) unioned with
  `source_failure_current.failure_shape` rows as `blocked`, grouped by the
  shape JSON value, file count as distinct (root, file), first example ordered
  by (root, file, line). Exported from the storage package.
- `cli.py`: `_postgres_envelope` computes the summary once per envelope and
  appends one `unknown_record_shape` warning per pending shape after the
  refresh-run warnings; `_postgres_coverage` gains `unknown_shape_records`,
  `unknown_shapes_pending`, `unknown_shapes_acknowledged`;
  `_postgres_index_status` adds top-level `unknown_shapes: {pending,
  acknowledged}`; `_unknown_shape_line` renders the design's one-line form and
  `_print_unknown_shape_lines` prints it after search results, after
  `index --status`, and on stderr after an `index` run.
- Contracts: README JSON contract bullet; CLAUDE.md CLI contract (new bullet
  after `--project`) and JSON contract (scalars and status lists);
  `skills/search-chat/SKILL.md` and `commands/search-chat.md` each gain the
  "parser-coverage note, not missing conversations, mention when it bears on
  the answer" paragraph.
- Tests: `tests/postgresql/test_unknown_shapes.py` gains
  `test_pending_shape_warnings_persist_on_every_reader_until_resolved`
  (AC14 baseline empty, AC7 persistence across a no-run `index`, AC11 pins:
  three scalars, `roots` the only list, no `unknown_shapes` on search, lists on
  status; exact warning key set; no decoy value anywhere in the envelope;
  human lines after the last result on search, on status) and
  `test_one_shape_across_files_is_one_warning_with_summed_counts` (AC8: 7
  records in 5 files, lowest example). `tests/test_cli.py` pins the
  one-line format. `test_cli_journey.py` superset gains the three scalars and
  its two `warnings == []` pins now assert exactly the three silent-family
  shapes the modern Codex fixture carries (`turn_context`, `world_state`,
  `compacted`) and the status lists. `test_refresh.py`'s appended-shape test
  now expects the blocked shape's warning beside `source_refresh_failed`.

Red evidence: `KeyError: 'unknown_shape_records'` on the baseline coverage and
the missing `_unknown_shape_line` formatter.

Deadline probe (design Decision 4): production database (`service=cc_search_chats`,
schema 11, 4,239 `source_file_current` rows, 0 failure rows) does not yet have
the migration-12 columns, so a read-only `EXPLAIN (ANALYZE, BUFFERS)` ran the
same aggregate with `parser_state` standing in for `unknown_shapes` (one
synthetic entry per row, 258 distinct groups, more than any real shape count)
and `failure_detail` for `failure_shape`. Result: Sort + GroupAggregate, both
quicksorts in memory (682 kB and 2,666 kB), `Buffers: shared hit=726 read=19`,
no temporary blocks, planning 0.9 ms, execution 82.9 ms. The plan's claim that
`docs/runbooks/postgresql-index-maintenance.md` requires this probe did not
resolve (no such sentence exists); the probe is recorded here on the design's
authority instead.

Green evidence (2026-10-24, this checkout):

- `uv run --frozen pytest -q -m postgresql` → 167 passed (after the two
  intended pin updates above).
- `uv run --frozen pytest -q -m 'not postgresql'` → 1025 passed.
- `ruff check`, `ruff format --check`, `ty check`: passed; `complexipy`
  watermark passed, snapshot unchanged; `vulture` exit 0.

## Outcome 4: Operator acknowledgement file (2026-10-24)

Changed surfaces:

- New `core/acknowledged_shapes.py`: `acknowledged_shapes_path` (honours
  `XDG_CONFIG_HOME`, empty value falls back to `~/.config`),
  `parse_acknowledged_shapes` via `tomllib` with closed validation naming
  `shape[i].field` (only `[[shape]]` at top level; required fields provider,
  record_path, keys, note, acknowledged; unknown fields, unknown provider,
  empty/non-string/unsorted keys via `RecordShape`'s own rule, non-date
  `acknowledged`, duplicates all fail), `load_acknowledged_shapes` (absent →
  empty; unreadable → fails closed), `AcknowledgedShapesError` carrying the
  path.
- `cli.py`: `_dispatch_postgres` loads the file only for an `index` run (not
  `--status`/`--migrate`), before the containment re-exec and before any
  connection, onto `args.acknowledged_shapes` (argparse default `()` so direct
  callers of `_handle_postgres` agree); `AcknowledgedShapesError` exits 2 with
  status `configuration_invalid`, code `acknowledged_shapes_invalid`, the
  message and path; `_postgres_index_run` passes the set to `index_corpus`.
- `refresh.py`: `index_corpus`/`refresh_native_sources(acknowledged_shapes=)`;
  `_stamp_current_acknowledgements` runs one UPDATE over current checkpoints
  at the start of every run (inside the roots transaction, so an unchanged
  corpus needs no generation), stamping matching digests `acknowledged` with
  note and date and reverting everything else to `ignored` with those fields
  removed, writing only rows whose map actually changes;
  `_apply_acknowledgements` stamps a file's merged map before its checkpoint
  is staged. Blocked failure rows are never stamped.
- `unknown_shapes.py`: summary carries `note` and `acknowledged`
  (`max()` over entries); `as_json` includes them only for non-pending shapes,
  so warnings keep their exact key set.
- Docs: `docs/examples/acknowledged-shapes.toml` (example only, full
  fingerprint keys); runbook section "5. Acknowledge an unknown shape" in
  `postgresql-index-maintenance.md` (later sections renumbered, the one `§5`
  cross-reference to the selected-pair query updated to `§6`); pointer from
  `laptop-deployment.md`'s configuration paragraph.
- Tests: `tests/test_acknowledged_shapes.py` (17: valid parse, empty/absent,
  load, unreadable, twelve malformed cases naming the entry, XDG path);
  `tests/test_cli.py` exit-2 path with no scope created and no database call,
  and `index --status` never reading a broken file;
  `tests/postgresql/test_unknown_shapes.py` AC9 lifecycle
  (`test_acknowledged_shape_stops_warning_but_stays_counted`: no run created,
  warning gone, pending 0 / acknowledged 1 / records 1, status entry with note
  and date, human status quiet, extra-key shape re-warns as distinct, removal
  reverts and drops notes) and AC12/AC10
  (`test_readers_never_open_the_acknowledgement_file`: 000-mode file; search,
  extract, resolve, events and `index --status` exit 0; `index` exits 2).

Red evidence: parser tests failed at collection (module absent); the CLI
tests failed on the missing gate; the PostgreSQL lifecycle test failed because
the warning persisted after acknowledgement and `index` exited 0 on the
unreadable file.

A full-suite run then exposed one regression: `test_composed_index_holds_the_database_queue_through_embedding`
calls `_handle_postgres` directly with parsed args, so `args.acknowledged_shapes`
was missing and the index thread died before embedding. Fixed by the argparse
default; the test passes again.

Green evidence (2026-10-24, this checkout):

- `uv run --frozen pytest -q -m postgresql` → 169 passed.
- `uv run --frozen pytest -q -m 'not postgresql'` → 1044 passed
  (`tests/test_cli.py` alone: 111 passed after the fix).
- `ruff check`, `ruff format --check`, `ty check`: passed; `complexipy`
  watermark passed, snapshot unchanged; `vulture` exit 0; `pre-commit
  validate-config` ok.
