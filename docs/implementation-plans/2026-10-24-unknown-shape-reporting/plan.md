# Unknown shape reporting implementation plan

**Status:** Ready for execution

**Design authority:** `docs/design-plans/2026-10-24-unknown-shape-reporting.md`
(Current design; accepted by the human on 2026-10-24, authority row
`:uuid:e9352d86-676f-4e00-a7ea-072a81153143`). The decision record
`.notes/decisions/2026-10-24-codex-additional-tools-and-noisy-ignore-flag.md`
holds the verbatim rulings.

**Working root:** `/home/brian/people/Brian/cc-search-chats-plugin-python`

**Integration target:** `main` at `59bbbe247e96b8f576ff1e8df2b79f6f38ae9540`
(the Codex metadata outer-shape fix the design depends on), equal to `HEAD`
and `origin/main` when this plan was written. The working tree held only the
untracked design plan and this plan directory.

**Workspace handling.** No isolation was requested, no planned file is dirty,
and no concurrent agent is working here, so execution uses this checkout.
The checkout is on the default branch. Before the first edit, execution
creates a task-owned local branch `unknown-shape-reporting` from `main` in
this checkout and obtains the human's assent to work there; it does not edit
`main` directly. The execution invocation authorises frequent private
checkpoint commits on that branch through `denubis-git-commit:commit`. It does
not authorise pushing, installing, production migration, a production index
run, timer changes, or rewriting published history. After the human accepts
the finished-work UAT, checkpoints are normalised onto the accepted tree, the
verification set is rerun, and a separate direct-delivery request integrates
into `main`. Release, install, migration 12 on production (with its ADR 0007
backup) and the baseline reparse are the deployment that follows, per
`docs/runbooks/laptop-deployment.md`.

## Current repository evidence

Verified on 2026-10-24 against `59bbbe2`:

- Provider parsers emit a diagnostic for an unrecognised record and continue:
  `providers/claude.py:1394-1454`, `providers/antigravity.py:567-574`,
  `providers/codex.py:1775-1855`. Diagnostic dataclasses are per provider
  (`CodexDiagnostic` at `providers/codex.py:461`, Claude and Antigravity
  equivalents) and satisfy the `ParseDiagnostic` protocol at
  `providers/registry.py:72-89`: `code`, `detail`, `record_ordinal`,
  `source_line`, `source_byte_offset`. There is no shape field anywhere;
  `core/` has no diagnostic type (`core/canonicalization.py:40-48` is a
  different concern).
- Adapter sets live in `providers/registry.py`: `unsupported_codes`,
  `scan_unsupported_codes`, `skippable_codes`, `repaired_codes` (field
  definitions `531-534`; Claude `638-667`, Codex `690-723`, Antigravity
  `746-772`). Codex `unknown_metadata_shape` and `unknown_compaction` and
  Claude `invalid_compact_boundary` appear in none of them.
- Refresh aborts a file on the first `unsupported_codes` diagnostic in
  `_parse_batch` (`storage/postgresql/refresh.py:839-875`), called from
  `_stage_parsed_batch` (`1180-1204`), which then builds skipped-record
  diagnostics (`878-935`), derives a batch-local `skipped_ordinals` set
  (`1196-1198`) and stages `_messages_without_skipped_records` (`938-949`).
  Failures are recorded by `_record_failed_observation` (`1376-1472`) into
  `source_failure_current` (DDL `incremental_refresh_schema.sql:13-36`, no
  shape column) and summarised by `source_failure_summary` (`218-246`).
  Retry gating is `_failed_observation_matches` / `_failed_observation_blocks`
  (`623-652`, `force_retry` at `646-648`).
- Per-file checkpoint columns are written at `refresh.py:1001-1050` (upsert)
  and `1349` (`skipped_record_count = prior + len(parsed.skipped)`);
  `skipped_record_count` came from migration 8
  (`skipped_record_coverage_schema.sql`). Excluded sources are checkpointed by
  `_stage_excluded_source` (`1297-1316`).
- Migrations are an ordered tuple `_MIGRATIONS` in
  `storage/postgresql/migrations.py:22-33`, last entry
  `Migration(11, "provider_antigravity_schema.sql")`; the ledger pins bytes
  (`tests/postgresql/test_migrations.py:275`, `322`, `479`).
- `refresh_native_sources` (`refresh.py:2276-2309`) and `index_corpus`
  (`refresh.py:2616`, `2713`) take `force_retry: bool`; the CLI passes
  `context.args.force_retry` at `cli.py:1699` from the argparse definition at
  `cli.py:2786-2822`. No `CC_SEARCH_*` variable controls index behaviour
  (`rg -n 'CC_SEARCH_' src` lists roots, `CONTAINED`, `DB_PATH`,
  `MODEL_PATH`, `QUERY_EMBEDDER_TEST_TOKEN`, `RUNTIME_DIR`,
  `SEMANTIC_WARM_SECONDS`). The containment re-exec preserves `sys.argv`
  (`cli.py:1402-1420`).
- The envelope is assembled by `_postgres_envelope` (`cli.py:1001-1060`) from
  `_postgres_refresh_row` (`751-783`; warnings are the latest
  `refresh_run.diagnostics`), `_postgres_coverage` (`881-929`) and
  `_coverage_completeness` (`871-878`). `index --status` is
  `_postgres_index_status` (`1629-1677`, `include_skipped_warnings=False`);
  the index run is `_postgres_index_run` (`1680-1720`). Human rendering of
  warnings happens in `_print_human_search` (`1164`) and the index/status
  paths at `1509` and `1715`.
- Schema-v5 pins: coverage keys superset and `roots` the only list
  (`tests/postgresql/test_cli_journey.py:45-67`); `index_state` and
  `semantic` exact (`74-85`, `100-109`); `completeness` asserted `complete`
  or `partial` across `tests/postgresql/test_refresh.py`,
  `test_antigravity_refresh.py`, `test_pending_tail_coverage.py`,
  `test_cli_journey.py`.
- Service unit `src/cc_search_chats/systemd/cc-search-chats-index.service`
  has `ExecStart=%h/.local/bin/cc-search-chats index` and
  `EnvironmentFile=-%h/.config/cc-search-chats/index.env`;
  `tests/test_systemd_units.py:16-35` pins these and forbids operator
  variables in the unit.
- Python 3.14 ships `tomllib` (stdlib, read-only TOML); the project has no
  TOML dependency and the design-plan guidance forbids new runtime
  dependencies.
- Documentation owners: `docs/decisions/0003-native-record-policy.md:55-60`
  and `docs/decisions/adr-register.md`;
  `docs/design-plans/2026-10-09-antigravity-primary-sessions.md:216`;
  `CLAUDE.md:61-62`, `111`; `README.md:207-229`;
  `skills/search-chat/SKILL.md:137-150`; `commands/search-chat.md:66`;
  `docs/architecture/database.md` (sections "Current relations",
  "Refresh publication"); `docs/runbooks/laptop-deployment.md:181-184`,
  `282-345`; `docs/runbooks/postgresql-index-maintenance.md`.

## Scope

In scope: the design's five phases as four implementation outcomes plus a
documentation-and-release-metadata outcome. Out of scope: deployment,
production migration, the baseline reparse on production, timer changes,
registering any shape the baseline reveals, and the `world_state` payload
exact-match that Opus flagged as a remaining instance of the pattern (not
observed to drop anything; separate fix if it ever does).

## Acceptance criteria

AC1–AC14 as written in the design. Primary evidence owners:

| Criterion | Owner | Outcome |
|---|---|---|
| AC1 fingerprint key names only | unit tests per provider | 1 |
| AC2 default unchanged, failure row carries shape | existing refresh tests + new assertion | 1 |
| AC6 silent family counted in both modes | PostgreSQL refresh test | 1 (default mode), 2 (flag mode) |
| AC14 migration gating | `test_migrations.py` + `maintenance_required` test | 1 |
| AC3 ignore mode indexes around the record | PostgreSQL refresh test | 2 |
| AC4 non-shape codes still block | PostgreSQL refresh test | 2 |
| AC5 batch boundary | PostgreSQL refresh test with small batch limit | 2 |
| AC13 flag plumbing, re-exec, unit unchanged | CLI tests + `test_systemd_units.py` | 2 |
| AC7 persistence across runs | PostgreSQL CLI journey test | 3 |
| AC8 dedupe and counts | PostgreSQL refresh/CLI test | 3 |
| AC11 schema v5 additive | `test_cli_journey.py` pins | 3 |
| AC14 baseline non-empty iff unknown shape exists | PostgreSQL test with seeded shape | 3 |
| AC9 acknowledgement lifecycle | PostgreSQL CLI test | 4 |
| AC10 malformed file fails closed | CLI unit tests | 4 |
| AC12 readers never read the file | CLI tests with unreadable file | 4 |
| Contract documents current | bounded agent inspection against the built CLI | 5 |

## Outcome 1: Unknown shapes are recorded

**Goal:** every unknown-shape diagnostic carries a key-name fingerprint; a
blocked file's failure row stores the shape that blocked it; an indexed file's
checkpoint stores per-shape counts for the silent family; migration 12 exists
and gates other commands until applied; default behaviour is otherwise
unchanged.
**Depends on:** `main` at `59bbbe2`.
**Owns:** AC1, AC2, AC6 (default mode), AC14 (migration gating).

### Files and consumers
- Create: `src/cc_search_chats/core/record_shape.py` — frozen `RecordShape`
  (`provider: str`, `record_path: str`, `keys: tuple[str, ...]`), canonical
  JSON string, SHA-256 digest, and a pure constructor from an envelope mapping
  and payload mapping that takes key names only. `core/` stays
  provider-neutral: it receives strings and mappings, not provider types.
- Modify: `src/cc_search_chats/providers/codex.py`, `claude.py`,
  `antigravity.py` — add an optional `shape: RecordShape | None = None` field
  to each diagnostic dataclass and populate it at every emission site for the
  codes listed in the design's `unknown_shape_codes` (Codex `1399`, `1435`,
  `1856`, the `unknown_metadata_shape`, `unknown_compaction`,
  `unknown_content_block`, `unknown_role` sites; Claude `1399` and the
  content-block/role/compact-boundary sites; Antigravity `512`).
- Modify: `src/cc_search_chats/providers/registry.py` — `ParseDiagnostic`
  gains `shape`; `ProviderAdapter` gains `unknown_shape_codes`; the three
  adapters populate it; Codex `parser_state_version` 6→7, Claude 8→9.
- Create: `src/cc_search_chats/storage/postgresql/unknown_shape_schema.sql` —
  migration 12: `ALTER TABLE source_file_current ADD COLUMN unknown_shapes
  jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(unknown_shapes) =
  'object')`; `ALTER TABLE source_failure_current ADD COLUMN failure_shape
  jsonb CHECK (failure_shape IS NULL OR jsonb_typeof(failure_shape) =
  'object')`. Register as `Migration(12, "unknown_shape_schema.sql")`.
- Modify: `src/cc_search_chats/storage/postgresql/refresh.py` — carry
  `shape` on `_SourceRefreshError` from `_parse_batch` and write
  `failure_shape` in `_record_failed_observation`; in `_stage_parsed_batch`,
  collect diagnostics whose code is in `adapter.unknown_shape_codes` but not
  in `unsupported_codes` (the silent family) into a per-batch shape map
  `{digest: {shape, count, first_ordinal, first_line, first_byte_offset,
  disposition: "ignored"}}`, merge it with the checkpoint's prior map on
  append and write it through the upsert at `1001-1050`; reset to `{}` on a
  byte-zero parse alongside `skipped_record_count`.
- Test: `tests/test_record_shape.py` (new), `tests/test_provider_codex.py`,
  `tests/test_provider_claude.py`, `tests/test_provider_antigravity.py`,
  `tests/test_provider_registry.py` (version locks 7 and 9),
  `tests/postgresql/test_migrations.py` (migration 12 applies, ledger pins
  bytes, columns present), `tests/postgresql/test_refresh.py` (failure row
  `failure_shape` populated for the existing unknown-shape block tests;
  silent-family count on an indexed file; `maintenance_required` before 12).
- First real consumer: `source_failure_current.failure_shape` and
  `source_file_current.unknown_shapes` rows read back by the tests in this
  outcome; Outcome 3 is the user-facing consumer.

### Work
1. Red: unit tests that a Codex unknown response item, a Claude unknown
   record, and an Antigravity unknown pair each produce a diagnostic with the
   expected `shape` whose serialisation contains none of the fixture's string
   values (fixtures carry a long sentinel value and a nested object). Red:
   `test_refresh` assertion that the 2.4.3 `additional_tools`-at-version-5
   block (`tests/postgresql/test_refresh.py`,
   `test_codex_additional_tools_source_blocked_at_parser_5_recovers_unsearchable`)
   records `failure_shape` with keys `{id, role, tools, type}` plus the outer
   keys. Red: a Codex file holding the conversation-shaped `turn_context`
   from `test_provider_codex.py:2174` between two prose records indexes with
   `unknown_shapes` count 1 and both prose rows searchable.
2. Implement `core/record_shape.py`; thread `shape` through the three
   diagnostic types and emission sites; add `unknown_shape_codes`; write
   migration 12 and register it; extend `_SourceRefreshError`,
   `_record_failed_observation`, `_stage_parsed_batch`, the checkpoint
   dataclass and upsert; bump the two parser versions and their lock tests.
3. Documentation owned here: `docs/architecture/database.md` gains the two
   columns in "Current relations" and the per-file shape map in "Refresh
   publication" as implemented state. Nothing user-facing changes yet; the
   CHANGELOG entry is written in Outcome 5.

### Verification
- Run: `uv run --frozen pytest -q -m 'not postgresql'` and
  `uv run --frozen pytest -q -m postgresql`.
- Positive signal: new tests pass; the migration ledger test reports 12
  applied with a recorded checksum; the version-lock test reads Codex 7 and
  Claude 9; `test_cli_journey` still passes unchanged (no envelope change
  yet).
- Failure signal: any fingerprint serialisation containing a fixture value;
  `failure_shape` NULL for a shape code; a silent-family record blocking the
  file; any pre-existing refresh test changing outcome.
- Also run the full gate set from CLAUDE.md (ruff, format, ty, complexipy,
  vulture, pre-commit validate-config).

### Finished-work implication
None: automated evidence settles it.

## Outcome 2: Ignore mode indexes around unknown shapes

**Goal:** `index --ignore-unknown-records` or
`CC_SEARCH_INDEX_IGNORE_UNKNOWN_RECORDS=1` makes a file with an unknown-shape
record index with that record omitted and counted; identity, payload and
admission failures still block; the packaged unit is unchanged.
**Depends on:** Outcome 1.
**Owns:** AC3, AC4, AC5, AC6 (flag mode), AC13.

### Files and consumers
- Modify: `src/cc_search_chats/cli.py` — argparse `--ignore-unknown-records`
  on `index` (`2786-2822`); resolve the effective value from the flag or the
  environment variable in `_postgres_index_run` and pass
  `ignore_unknown_records=` to `index_corpus`; the containment re-exec needs
  no change because it preserves `sys.argv` and the environment (prove with a
  test, not an assumption).
- Modify: `src/cc_search_chats/storage/postgresql/refresh.py` —
  `index_corpus` and `refresh_native_sources` accept
  `ignore_unknown_records: bool = False` and carry it on `_SourcePlan`;
  `_parse_batch` raises only for `unsupported_codes` diagnostics that are not
  in `unknown_shape_codes` when the mode is on; `_stage_parsed_batch` adds
  ignored ordinals (shape codes that would have blocked, excluding
  `unknown_content_block`) to `skipped_ordinals` and counts them into the
  shape map with disposition `ignored`; `refresh_run` gains an
  `ignored_unknown_record_count` column (part of migration 12, written in
  Outcome 1's SQL file so the migration stays one immutable unit; the column
  is written for the first time here).
- Test: `tests/postgresql/test_refresh.py` (AC3, AC4, AC5 with the bounded
  reader's `DEFAULT_MAX_RECORDS_PER_BATCH`
  (`providers/source_discovery.py:240`) lowered by monkeypatch so an unknown
  item ends one batch), `tests/test_cli.py` (flag and env resolution,
  env wins when either is set, re-exec argv preserved),
  `tests/test_systemd_units.py` (unchanged and passing).
- First real consumer: the `index` CLI run with the flag; the nightly timer
  via the operator's `index.env`.

### Work
1. Red: PostgreSQL tests for AC3 (prose, unknown response item, prose:
   indexed, both prose rows found by literal and exhaustive search, nothing
   from the unknown record in any `prose_content` or tool row under any
   `--agents`/`--tools` combination, checkpoint `unknown_shapes` count 1
   disposition `ignored`, `completeness: complete`, run
   `ignored_unknown_record_count` 1); AC4 (Codex `unsupported_session_identity`
   file and Claude `missing_message_uuid` file both block with the flag); AC5
   (unknown item as the last record of a batch, prose in the next batch; no
   alias at the unknown ordinal, trailing-candidate pairing unchanged). Red:
   CLI tests that the flag and the variable each set the mode and that the
   default is off.
2. Implement the plumbing and the `_parse_batch` / `_stage_parsed_batch`
   behaviour; keep `unknown_content_block` emitting the message's recognised
   blocks while counting the shape.
3. Documentation owned here: `cc-search-chats index --help` text for the
   flag; the runbook's `index.env` paragraph
   (`docs/runbooks/laptop-deployment.md:181-184`) gains the variable with a
   one-line statement that it is operator policy, not a packaged default.

### Verification
- Run: both pytest suites and the gate set.
- Positive signal: AC3–AC5 tests pass; the existing default-mode block tests
  (including `test_refresh.py:1759-1811`) pass unchanged; `test_systemd_units`
  passes against a byte-identical unit (`git diff --exit-code
  src/cc_search_chats/systemd/`).
- Failure signal: a non-shape code indexes under the flag; any stored string
  from an ignored record; the unit changed; the default mode changed.

### Finished-work implication
None: automated evidence settles it.

## Outcome 3: Every reader reports pending shapes

**Goal:** `index`, `index --status` and `search` carry one
`unknown_record_shape` warning per distinct unacknowledged shape derived from
current rows; coverage gains three scalars; `index --status` lists pending
and acknowledged shapes; human output prints one line per pending shape;
agent guidance explains the warning.
**Depends on:** Outcome 2 (so both dispositions exist to report).
**Owns:** AC7, AC8, AC11, AC14 (baseline part).

### Files and consumers
- Modify: `src/cc_search_chats/storage/postgresql/refresh.py` (or a new
  `unknown_shapes.py` beside it) — `unknown_shape_summary(connection)`: one
  SQL aggregate over `source_file_current.unknown_shapes` (jsonb_each) and
  `source_failure_current.failure_shape`, grouped by digest, returning shape,
  record_count, file_count, first example ordered by (root, file, line),
  dispositions, max parser_state_version, and the acknowledgement fields when
  present (Outcome 4 fills them; here they are absent).
- Modify: `src/cc_search_chats/cli.py` — `_postgres_coverage` adds
  `unknown_shape_records`, `unknown_shapes_pending`,
  `unknown_shapes_acknowledged`; `_postgres_envelope` appends one warning per
  pending shape for every command; `_postgres_index_status` adds top-level
  `unknown_shapes: {pending: [...], acknowledged: [...]}`; human renderers at
  `1164`, `1509`, `1715` print the one-line form from the design.
- Modify: `skills/search-chat/SKILL.md:137-150` and
  `commands/search-chat.md:66` — one paragraph: what `unknown_record_shape`
  means, that it is a parser-coverage note and not missing conversations,
  and when to mention it.
- Test: `tests/postgresql/test_cli_journey.py` (AC11 pins hold with the new
  scalars; `search --json` has no `unknown_shapes` list; `index --status`
  has it), `tests/postgresql/test_refresh.py` or a new
  `tests/postgresql/test_unknown_shapes.py` (AC7: after a blocked and an
  ignored shape, a no-change `index` creates no run and `index --status` and
  `search --literal --json` still carry the warning; AC8: seven records of one
  shape across five files give one warning with the right counts and lowest
  first example; AC14 baseline: summary empty on a clean corpus, non-empty
  after one seeded unknown shape), `tests/test_cli.py` (human line format).
- First real consumer: the search-chat skill and command, and the human at
  the terminal.

### Work
1. Red: the AC7, AC8, AC11 and AC14-baseline tests above.
2. Implement the aggregate, the envelope fields, the status list and the
   human lines. Keep the aggregate bounded: group by digest in SQL, never
   fetch per-file maps into Python.
3. Documentation owned here: README JSON contract (`207-229`) and CLAUDE.md
   CLI/JSON contract (`61-62`, `111`) gain the warning, the three scalars and
   the status list; the skill and command paragraphs.

### Verification
- Run: both pytest suites; the gate set; and, for the deadline decision, a
  read-only `EXPLAIN (ANALYZE, BUFFERS)` of the aggregate against the
  production-sized laptop database recorded in the worklog (the runbook
  requires this when retrieval SQL changes; the warning query runs on every
  search).
- Positive signal: tests pass; the EXPLAIN shows no temporary blocks written
  and sub-100 ms on ~4,200 checkpoint rows.
- Failure signal: a warning vanishing when the latest run has no
  diagnostics; a list in `coverage`; `unknown_shapes` on a search envelope;
  the aggregate exceeding the literal render reserve.

### Finished-work implication
Reserved for the collated UAT below.

## Outcome 4: Operator acknowledgement file

**Goal:** `index` reads
`$XDG_CONFIG_HOME/cc-search-chats/acknowledged-shapes.toml` (default
`~/.config/...`), validates it closed, stamps `acknowledged` on matching
shapes in current rows, and the readers stop warning for them while keeping
them counted; readers never open the file.
**Depends on:** Outcome 3.
**Owns:** AC9, AC10, AC12.

### Files and consumers
- Create: `src/cc_search_chats/core/acknowledged_shapes.py` — pure parsing
  and validation of the TOML document (via `tomllib`) into a frozen set of
  `RecordShape` plus note and date; closed validation errors with the entry
  index and field named.
- Modify: `src/cc_search_chats/cli.py` — in `_postgres_index_run` only,
  before `acquire_index_session` and before the containment re-exec decision
  point, load the file; on any validation error print the message and return
  exit 2; pass the acknowledged set to `index_corpus`.
- Modify: `src/cc_search_chats/storage/postgresql/refresh.py` — stamp
  `disposition: acknowledged` on matching entries when writing a file's
  `unknown_shapes`, and run one UPDATE over current rows at the start of the
  run so previously recorded shapes change disposition without reparsing;
  shapes present in rows but no longer in the file revert to `ignored`.
  Blocked shapes (`failure_shape`) are never acknowledged: a blocked file is
  a block until a parser release or a flagged run.
- Modify: the summary from Outcome 3 reads `note` and `acknowledged` from
  the stamped rows; warnings exclude acknowledged shapes;
  `unknown_shapes_acknowledged` counts them.
- Create: `docs/examples/acknowledged-shapes.toml` — one commented example
  entry (the `additional_tools` shape as an illustration, marked as an
  example only).
- Test: `tests/test_acknowledged_shapes.py` (parse, each validation failure,
  exact-key matching, sorted-keys requirement), `tests/test_cli.py` (exit 2
  paths with no run row and no scope; `XDG_CONFIG_HOME` honoured),
  `tests/postgresql/test_unknown_shapes.py` (AC9 lifecycle including the
  extra-key re-warn and revert on removal; AC12 readers succeed with an
  unreadable file at the path).
- First real consumer: the operator's file on this machine, exercised at UAT.

### Work
1. Red: the AC9, AC10, AC12 tests.
2. Implement the parser, the index-time load and stamping, and the reader
   exclusion.
3. Documentation owned here: runbook section "Acknowledge an unknown shape"
   in `docs/runbooks/postgresql-index-maintenance.md` with the format, the
   exact-match rule, the git-tracked operator ownership, and the failure
   behaviour; a pointer from `laptop-deployment.md`'s configuration paragraph.

### Verification
- Run: both pytest suites and the gate set.
- Positive signal: AC9 shows the acknowledged shape leaving `warnings` and
  `unknown_shapes_pending` while staying in `unknown_shape_records` and the
  status `acknowledged` list with its note; the extended shape warns as new;
  AC10 exits 2 with the entry named; AC12 passes with a 000-mode file.
- Failure signal: a reader opening the file (strace-free check: the test
  makes the file unreadable and asserts success); an acknowledged shape still
  warning; a blocked shape silenced.

### Finished-work implication
Reserved for the collated UAT below.

## Outcome 5: Decision records, contracts and release metadata

**Goal:** the durable documents say what the system now does, and the tree is
ready for the release action.
**Depends on:** Outcomes 1–4.
**Owns:** the "Contract documents current" row and the design's
documents-brought-current list.

### Files and consumers
- Create: `docs/decisions/0009-unknown-shape-reporting.md` — Accepted:
  unknown shapes are always reported by key-name fingerprint; fail-closed
  stays the default; ignore mode is opt-in per run; acknowledgement is
  operator-owned, exact, index-time; extends ADR 0003's required-minimum rule
  to Codex `turn_context`/`world_state` (commit `59bbbe2`). Register in
  `docs/decisions/adr-register.md`.
- Modify: `docs/decisions/0003-native-record-policy.md:59-60` — append a
  pointer sentence to ADR 0009.
- Modify: `docs/design-plans/2026-10-09-antigravity-primary-sessions.md:216`
  — annotate the row as overturned for flagged runs by the 2026-10-24
  design.
- Modify: `CHANGELOG.md` (new section), `pyproject.toml`, `uv.lock`,
  `.claude-plugin/marketplace.json`, `.claude-plugin/plugin.json`,
  `.codex-plugin/plugin.json`, `README.md` release label,
  `tests/test_plugin_packaging.py` `EXPECTED_CLI_VERSION` — next minor
  (2.5.0: new flag, new file, new migration).
- Modify: `docs/runbooks/laptop-deployment.md` preserving-upgrade section —
  migration 12 note with the ADR 0007 backup and the baseline reparse
  expectation (Codex and Claude from byte zero; the first status after it is
  expected to list the shapes the corpus holds).
- Test: `tests/test_plugin_packaging.py` passes; a bounded agent inspection
  compares every changed contract paragraph against `--help` output and one
  real `index --status --json` from the disposable cluster, reported in the
  worklog with the exact paragraphs checked.
- First real consumer: the deployment operator and the agents reading the
  skill.

### Work
1. Write ADR 0009 and the pointers; bring CLAUDE.md, README, skill, command,
   runbooks and architecture doc to the implemented state (Outcomes 1–4 own
   their first drafts; this outcome reconciles them).
2. Release metadata and CHANGELOG.
3. Full gate set plus `git diff --check`.

### Verification
- Run: full gate set; `uv run --frozen cc-search-chats --help` and
  `index --help`; the bounded documentation inspection.
- Positive signal: every gate exit 0; packaging test reads the new version;
  inspection finds each documented field present in a real envelope.
- Failure signal: a documented field absent from the envelope, or an
  envelope field undocumented.

### Finished-work implication
Reserved for the collated UAT below.

## Collated UAT (after Outcome 5, on the disposable cluster and then at deployment)

The human acts on the built surface; each probe has a falsifier.

1. **Noise level.** Run `cc-search-chats search <known phrase> --literal`
   against a corpus seeded with two unknown shapes. Judgment: the two lines
   after the results are informative and not in the way. Falsifier: the human
   would want them gone.
2. **Baseline legibility (deployment).** After migration 12 and the first
   index on this machine, read `index --status`. Judgment: every listed shape
   is something the human recognises as housekeeping or wants fixed; nothing
   is ambiguous. Falsifier: a shape whose line does not tell them which it is.
3. **Lifecycle.** Acknowledge one listed shape in
   `~/.config/cc-search-chats/acknowledged-shapes.toml`, run `index`, and see
   it leave the warnings; pull the file on the other machine and run `index`
   there. Judgment: this is the "denylist of stuff we know we don't care
   about" they asked for. Falsifier: it took a release, or the other machine
   disagreed after the pull.
4. **Timer policy.** Decide whether `index.env` carries
   `CC_SEARCH_INDEX_IGNORE_UNKNOWN_RECORDS=1` from the first deployment. The
   supervisor's lean is yes. Record the answer in the decision file.

Probes 2–4 belong to deployment, which is a separate authorised action; they
are listed here so the plan's UAT is complete-surface.
