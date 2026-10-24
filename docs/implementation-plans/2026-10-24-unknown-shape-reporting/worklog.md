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
