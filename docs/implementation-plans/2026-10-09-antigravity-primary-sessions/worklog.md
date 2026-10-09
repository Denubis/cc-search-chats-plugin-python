# Worklog

Completed work, changed surfaces, decisions reached during execution, and
exact evidence. Append in order; never move pending work here early.

## Planning evidence (2026-10-09, before execution)

- Design accepted by the human on 2026-10-09 (design Authority Sources, fifth
  row). The five authority locators return `stale_index` through the installed
  CLI 2.3.6; the documented grep fallback printed `1` for each UUID in
  `~/.claude/projects/-home-brian-people-Brian-cc-search-chats-plugin-python/42102a00-89eb-4f30-ad4e-e12c7a65e697.jsonl`.
- Working tree clean on `main` at `27b637bbdd3cb99972132d533c3ce614906e95b2`;
  unrelated worktree `.worktrees/message-attribution` present.
- Read-only survey of `~/.gemini/antigravity-cli/brain` (metadata plus first
  records and key sets only): 237 directories, 228 `transcript_full.jsonl`,
  eight October sessions (seven `USER_EXPLICIT`/`USER_INPUT`, one
  `SYSTEM`/`SYSTEM_MESSAGE`); `PLANNER_RESPONSE` has `content`, `thinking`
  and `tool_calls` each optional; `tool_calls` items are `{name, args}`;
  `run_command` args carry a string `Cwd` in all seven October human sessions.
- Every line citation in plan.md "Current repository evidence" was opened at
  `27b637b` during planning.

## Before the first edit (2026-10-09)

- `git status` clean on `main` at `27b637bbdd3cb99972132d533c3ce614906e95b2`
  apart from the untracked plan directory; `.worktrees/message-attribution`
  at `c724344` untouched.
- Baseline at `27b637b`: `uv run --frozen pytest -q -m 'not postgresql'` →
  875 passed, 148 deselected; `uv run --frozen pytest -q -m postgresql` →
  148 passed, 875 deselected; `uv run --frozen complexipy --failed --plain` →
  "Snapshot watermark passed", exit 0, snapshot unchanged.
- Created local branch `antigravity-primary-sessions` from `main` in this
  checkout; human assent to work there requested before the first edit.

- Human assent received (2026-10-09) to execute on `antigravity-primary-sessions`
  in this checkout with private checkpoints and no edits to `main`.

## Outcome 1: Claude and Codex run through a provider registry (done)

Changed surfaces:
- New `src/cc_search_chats/providers/registry.py`: `ProviderAdapter` (root
  defaults and variables, discovery, locator key kinds, parser-state version,
  initial/serialize/deserialize state, source-session derivation, parse entry,
  unsupported/scan-unsupported/skippable/repaired code sets,
  `inspect_artifacts`, raw-record match, scan candidate filter, parsed
  session guard) plus `scan_unindexed` as a generic bounded scan, the
  Claude and Codex entries, `provider_adapter`, `provider_adapters`, and
  `configured_source_roots`.
- `core/identity.py`: `_LOCATOR_KEY_KINDS` table and
  `permitted_locator_key_kinds`; `NativeLocator.__post_init__` reads the table.
- `storage/postgresql/refresh.py`: `_PARSER_STATE_VERSIONS`, the literal
  `VALUES` provider list, the state codec, `_parse_batch`,
  `_skipped_record_diagnostics`, `_repaired_record_diagnostics`, the initial
  state and `_index_artifact` gate all route through `plan.adapter`.
- `storage/postgresql/staleness.py` and `resolution.py` use
  `adapter.discover` / `adapter.scan_unindexed`; the two provider scan
  functions and `_ScanEvidence` left `resolution.py`.
- `providers/source_discovery.py`: `configured_source_roots` moved to
  `registry.py` (corrected implementation detail: `source_discovery` is
  imported by `registry`, so an in-place rewrite would have been a circular
  import). `cli.py` and `tests/test_source_discovery.py` import the new home.
- Tests: new `tests/test_provider_registry.py` (15 tests); parser-version
  monkeypatches in `tests/postgresql/test_refresh.py` and
  `test_unindexed_sources.py` now replace the registry entry via
  `_set_parser_state_version` instead of a refresh-module dict.

Evidence (2026-10-09):
- `uv run --frozen pytest -q -m 'not postgresql'` → 890 passed (875 baseline
  + 15 registry tests).
- `uv run --frozen pytest -q -m postgresql` → 148 passed (same count as
  baseline; CLI-journey `coverage`/`index_state` assertions unchanged).
- `uv run --frozen complexipy --failed --plain` → passed; snapshot diff:
  `configured_source_roots` (23) and `_skipped_record_diagnostics` (24) fell
  below 15 and left the snapshot; `_discover_sources` 19→17;
  `_parse_and_stage_source` 23→22; `resolution.py` entries (17, 19, 22) all
  removed. No recorded function worsened.
- `ruff check`, `ruff format --check`, `ty check`, `vulture` → all exit 0.

- Checkpoint `1931202` (refactor: provider registry) on
  `antigravity-primary-sessions`; pre-commit hooks all passed.

## Outcome 2: Schema and identity admit the `antigravity` provider (done)

Changed surfaces:
- `core/identity.py`: `Provider.ANTIGRAVITY = "antigravity"`; key-kind table
  permits only `ordinal` for it.
- New `storage/postgresql/provider_antigravity_schema.sql` (migration 11): a
  `DO` block that, per target table, drops every single-column CHECK on
  `provider` by catalogue lookup, raises if none existed, and adds
  `<table>_provider_check` admitting `claude`, `codex`, `antigravity`.
  `migrations.py` ledger appended with `Migration(11, ...)`.
- `cli.py`: `--provider` choices on `search`, `context`, `extract` derive
  from `Provider` via `_PROVIDER_CHOICES`.
- `docs/architecture/database.md`: migration-11 row and provider vocabulary;
  `docs/runbooks/postgresql-index-maintenance.md`: expected
  `applied_schema_version == 11` and migration-11 failure behaviour.
- Tests: `tests/test_identity.py` (three enum values, antigravity ordinal
  locator round-trip, `uuid`/`id` keys rejected and malformed on parse);
  `tests/postgresql/test_migrations.py` (ledger pins → 11; new
  `test_migration_11_replaces_provider_checks_whatever_their_names` renames
  the three constraints on a v10 schema, plants a decoy legacy `message`
  relation, migrates, asserts one `<table>_provider_check` per table,
  `antigravity` inserts into all three, `gemini` raises `CheckViolation`,
  and the legacy relation's check is byte-identical);
  `tests/postgresql/test_cli_journey.py` (future-ledger probe moves to 12;
  journey proves a v10 database makes `search --literal --json` exit 6 with
  `pending_versions == [11]` and ledger max 10 before `index --migrate`
  reports 11); `tests/postgresql/test_background_search.py` pending list → 11;
  `tests/test_provider_registry.py` parameterised over the registered
  providers and asserts `provider_adapter(Provider.ANTIGRAVITY)` raises
  `KeyError` until Outcome 3.

Evidence (2026-10-09):
- `pytest -q tests/test_identity.py tests/test_provider_registry.py` → 66 passed.
- `pytest -q -m postgresql tests/postgresql/test_migrations.py
  tests/postgresql/test_cli_journey.py tests/postgresql/test_background_search.py`
  → 41 passed.
- Full suites: non-PostgreSQL 895 passed; PostgreSQL 149 passed.
- `complexipy --failed --plain` passed with no snapshot change; `ruff check`,
  `ruff format --check`, `ty check`, `vulture` all exit 0.
- `cc-search-chats search --help` shows `--provider {claude,codex,antigravity}`.
- Checkpoint `bdc29ae` (feat: admit the antigravity provider); hooks passed.
  The plan-file update for this outcome landed in the next checkpoint.

## Outcome 3: Antigravity sessions discovered, admitted, indexed, quiet when excluded, exactly resolvable (done)

Changed surfaces:
- New `providers/antigravity.py` (pure): `SCOPE_START`,
  `AntigravityDiagnosticCode`, `AdmissionOutcome`/`AdmissionDecision`,
  `admit_antigravity_session`, context/state/result types, and
  `parse_antigravity_session` with per-pair projections; every function at or
  under complexity 15 (snapshot unchanged by the module).
- `providers/source_discovery.py`: `discover_antigravity_sources` (immediate
  canonical-UUID directories, `lstat` regular-file check, no symlink follow,
  no archive diagnostics, other children silent).
- `providers/registry.py`: Antigravity entry (version 1,
  `inspect_artifacts=False`, admission, code sets, state codec
  `{next_conversation_epoch, cwd}`, session ID from `parts[0]`); adapters gain
  `admission` and `admits()`; `scan_unindexed` applies admission on the first
  batch so excluded/blocked sessions report nothing recognised.
- `storage/postgresql/refresh.py`: `_parse_and_stage_source` split into
  `_read_source_batch`, `_admission_decision` (excluded → `_ExcludedSource`,
  blocked → deterministic `_SourceRefreshError` at ordinal 0),
  `_stage_parsed_batch`, `_parse_source_batches`, `_verified_final_size`,
  `_stage_excluded_source` (checkpoint at full size); `_stage_index_artifact`
  also checkpoints at full size; `_plan_source` sticky-exclusion rule.
  `_parse_and_stage_source` (22) fell below 15 and left the snapshot.
- `storage/postgresql/staleness.py`: `_Checkpoint.source_status`; excluded
  checkpoints with the same identity and no shrink count no unindexed bytes.
- `tests/conftest.py`: session-scoped autouse `CC_SEARCH_ANTIGRAVITY_ROOTS`
  → empty temp dir; `build_antigravity_root`, `antigravity_transcript`,
  `ANTIGRAVITY_SESSION_IDS`.
- Synthetic fixtures under `tests/fixtures/providers/antigravity/` (no real
  session text): `sessions/{october_human, pre_october, boundary_admitted,
  system_first, unknown_first, unregistered_pair}` and `root_decoys/`.
- Tests: `tests/test_provider_antigravity.py` (admission incl. both boundary
  timestamps, projections, decoy exclusion, epochs, shared tool-row identity,
  truncation diagnostic, malformed-line skip, eight blocking shapes, extra
  keys, blank content, two-batch equivalence); `tests/test_source_discovery.py`
  (`TestAntigravityDiscovery`, `TestAntigravityConfiguredRoots` incl. the
  present-default and plural-variable proof; existing `non_native_agy` test
  unchanged and green); `tests/postgresql/test_antigravity_refresh.py`
  (AC1–AC5 incl. the three-run stale loop with `row_to_json` identity,
  append-to-excluded, replace-admits, append watermark, `stale_source`,
  excluded-session `no_match`, tool/thinking/metadata boundary, unregistered
  pair blocks with `needs_attention`, root and sibling decoys silent);
  `tests/postgresql/test_cli_journey.py` (three configured roots with only
  fixture paths, `search --literal --provider antigravity --json`, `resolve`
  of its locator, `events` retained user prose).
- Docs: `docs/architecture/database.md` excluded-checkpoint semantics and
  admission; `CLAUDE.md` Source Roots (third default root, plural variable,
  discovery and admission summary, suite isolation).

Observations (not changed, out of scope):
- An unchanged `index` run echoes the previous run's `repaired_records` in
  `coverage` because coverage diagnostics come from the latest `refresh_run`
  row; the journey uses the truncation-free `boundary_admitted` fixture so
  the pre-existing behaviour is not asserted either way.
- `events` reports retained unknown-authorship primary user prose with
  `submitted_by: human` (existing derivation; Claude and Codex behave the
  same in the journey).

Evidence (2026-10-09):
- `pytest -q tests/test_provider_antigravity.py tests/test_source_discovery.py`
  → 74 passed (boundary: `2026-09-30T23:59:59Z` excluded,
  `2026-10-01T00:00:00Z` admitted).
- `pytest -q -m postgresql tests/postgresql/test_antigravity_refresh.py` →
  5 passed; stale-loop runs show `changed_source_count = read_source_count =
  attempted_content_bytes = 0`, same `corpus_generation`, identical
  checkpoint JSON, `unindexed.files = 0`, `freshness = no_source_changes`.
- Read-only probe against `~/.gemini/antigravity-cli/brain`:
  `discover_antigravity_sources` → `228 []` (228 sources, zero diagnostics);
  `find … -name transcript_full.jsonl | wc -l` → 228.
- Checkpoint `55a533a` (feat: index primary Antigravity sessions); hooks passed.

## Outcome 4: Sessions carry a derived working directory (done)

Changed surfaces:
- `providers/antigravity.py`: `_first_run_command_cwd`; `_Projection` carries
  `run_command_cwd`; the parser records the first string `Cwd` into
  `next_state.cwd`, stamps every message of the result with the final value,
  and exposes `cwd_established` (True only on the batch that first set it).
- `providers/registry.py`: adapters gain `state_cwd` and `cwd_established`
  callables (None/False for Claude and Codex).
- `storage/postgresql/refresh.py`: `_stamp_staged_cwd` rewrites earlier
  staged rows of the source after its last batch; `_ParsedSource` carries
  `cwd_established`; an `append` plan whose parse establishes cwd clears its
  staged suffix and reparses once as a from-zero `replace` plan (the replace
  disposition cannot re-trigger the route).
- Docs: `docs/architecture/database.md` derived-`cwd` paragraph; `CLAUDE.md`
  CLI contract `--project` wording.
- Tests: `tests/test_provider_antigravity.py::TestDerivedWorkingDirectory`
  (stamps every row, NULL without `run_command`, first call wins, non-string
  `Cwd` ignored, split after the call carries forward, `cwd_established`);
  `tests/postgresql/test_antigravity_refresh.py` AC6 (tail establishes cwd →
  reads start `[watermark, 0]`, `--project` matches every prose row, earlier
  locators and their `embedding_input_digest` unchanged, exactly one new
  embedding for the new prose row, excluded and other sessions' checkpoints
  untouched; one-record batches stamp ordinal 0). AC1 now asserts the
  derived value on the October fixture.

Evidence (2026-10-09):
- `pytest -q tests/test_provider_antigravity.py` → 32 passed.
- `pytest -q -m postgresql tests/postgresql/test_antigravity_refresh.py` →
  7 passed (includes the Outcome 3 stale-loop test rerun under the new route).
