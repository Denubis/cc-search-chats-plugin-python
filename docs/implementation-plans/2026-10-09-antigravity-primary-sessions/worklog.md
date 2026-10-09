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
- Checkpoint `01c961a` (feat: derive the Antigravity working directory); hooks passed.

## Outcome 5: Consumers tell the truth and the release is assembled (done except the status flip)

Changed surfaces:
- `README.md` (providers, release 2.4.0, `--provider claude|codex|antigravity`,
  derived `--project`, Antigravity scope paragraph, Source Roots row and
  `CC_SEARCH_ANTIGRAVITY_ROOTS`, metadata-only discovery, quiet exclusions);
  `skills/search-chat/SKILL.md` and `commands/search-chat.md` (description,
  example, provider/scope/`--agents`/`--project` statements; the required
  "Ponytail" wording retained); `CLAUDE.md` (Purpose, CLI Contract provider
  values, `--agents` adds nothing for Antigravity, quiet exclusions);
  `docs/architecture/database.md` (authority sentence, verified date);
  `docs/runbooks/laptop-deployment.md` (migration 11 after the backup; the
  first-index operational check: 228 discovered, 7 indexed, 221 excluded,
  none blocked as of 2026-10-09).
- `docs/design-plans/2026-08-10-cross-vendor-semantic-search.md` lines 55–56
  and 96–98 amended so the 2026-10-09 design owns primary Antigravity sessions.
- Release: `2.4.0` in `pyproject.toml`, `.claude-plugin/plugin.json`,
  `.claude-plugin/marketplace.json`, `.codex-plugin/plugin.json`,
  `tests/test_plugin_packaging.py`, `uv.lock` (`uv lock` → "Updated
  cc-search-chats v2.3.7 -> v2.4.0"); `CHANGELOG.md` `## cc-search-chats 2.4.0`
  with `**Added:**` and `**Changed:**`. Root `plugin.json` and
  `.agents/plugins/marketplace.json` carry no version string (checked).

Command executions (2026-10-09):
- `uv run --frozen cc-search-chats {search,list,extract} --help` →
  `--provider {claude,codex,antigravity}`; `context`, `resolve`, `events`,
  `index --help` have no provider flag (unchanged).
- `uv run --frozen cc-search-chats search "orchard ledger" --literal --provider
  antigravity --json` against the operator database (read-only) → `status:
  maintenance_required`, `pending_versions: [11]`, no schema change: the
  documented pre-migration behaviour.
- The documented `search --literal --provider antigravity --json`, `resolve
  <locator> --json` and `events --from/--until --json` commands execute
  end-to-end in `tests/postgresql/test_cli_journey.py` against the fixture
  root.

Documentation inspection (in-session; no reviewer model was named):
- Checked in README, SKILL, command, CLAUDE.md, laptop runbook, database.md
  and CHANGELOG: every `--provider` token is a CLI choice; every
  `CC_SEARCH_*_ROOTS` variable is a registry variable and no singular
  Antigravity variable is named; the only Antigravity root path named is
  `~/.gemini/antigravity-cli/brain`; scope-start wording; `--agents` adds
  nothing; thinking/tool results/injected context excluded; derived
  `--project`.
- Positive control: `--provider claude|codex|gemini` planted in README was
  reported (`('README.md', 'provider token', 'gemini')`) and then removed; the
  rerun reported only two false positives from the phrase "`--provider`
  accepts" and none after excluding that phrase.
- Finding and fix: the packaging test requires the word "Ponytail" in the
  skill and command documents; the first rewrite dropped it, restored.

Design status: all five authority locators still return `stale_index`
through the installed CLI (`cc-search-chats 2.3.6` at `~/.local/bin`):
`078509b3…`, `780c7360…`, `4bb6a285…`, `e1f036eb…`, `73a5b70e…` → `stale_index`,
exit 0 from the JSON wrapper. Status left Draft; question raised to the human.

Evidence (2026-10-09, at 2.4.0):
- non-PostgreSQL 940 passed; PostgreSQL 156 passed; `complexipy` passed with
  no snapshot change; `ruff check`, `ruff format --check`, `ty check`,
  `vulture`, `pre-commit validate-config` all exit 0;
  `tests/test_plugin_packaging.py` 16 passed.
- Checkpoint `9d4c608` (chore: release cc-search-chats 2.4.0); hooks passed.

## Independent sanity check over the real store (read-only, 2026-10-09)

In-memory admission over all 228 first records and a full parse of the
admitted sessions through the branch's parser (no database, no text printed):
`sources 228`, `outcomes {excluded: 221, admitted: 7}`,
`codes {antigravity_before_scope_start: 220, antigravity_not_human_initiated: 1}`,
`blocking {}`, `repaired {}`, `messages 263`, `with_cwd 7`. This matches the
runbook's expected first-index report (7 admitted, 221 excluded, none blocked)
and shows every admitted session carries a derived working directory.

## Execution state

- Five private checkpoints on `antigravity-primary-sessions`
  (`1931202`, `bdc29ae`, `55a533a`, `01c961a`, `9d4c608`) from `main` at
  `27b637b`; tree clean; nothing pushed, installed, migrated, or indexed in
  production.
- Finished-work UAT (plan.md) needs the separately authorised ADR 0007
  backup, `index --migrate` and one `index` run on an installed 2.4.0; not
  run by execution. Normalisation follows accepted UAT.

## Normalisation, integration and production deployment (2026-10-09)

Authorised by the human ("yes, commit and push", "happy to install", "do what
you need to, on the archive filesystem"). Procedure: `docs/runbooks/laptop-deployment.md`,
preserving upgrade.

- Checkpoints folded into `5c00e424ff22b64dbb2bd1ad2d4151f0d77d66f3`
  (accepted tree preserved); `main` fast-forwarded and pushed;
  `main == origin/main`.
- Installed `cc-search-chats 2.4.0` from that commit via
  `uv tool install --force` (provenance verified). Timer disabled for the
  upgrade; previous installed commit `0762b6b` (2.3.6), generation 104,
  semantic build 89, `needs_attention 98`.
- Full `pg_dump` (ADR 0007) completed before migration: 38,514,832,485 bytes,
  archived at `/mnt/archive/scratch/cc-search-chats/upgrades/5c00e424…/
  cc-search-chats-before.dump`; pointer in the evidence directory
  `~/.local/state/cc-search-chats/upgrades/5c00e424…/backup-location`.
- `index --migrate`: `applied_schema_version 11`; renamed provider
  constraints verified; legacy quarantine untouched.
- First `index` (run 107): exit 0, `status complete`, corpus generation 105,
  semantic build 90 (`fresh`, 428,737 units). The Claude parser-version 6
  bump from 2.3.7 reparsed 10,734 sources (7.15 GB read); no re-embedding of
  unchanged prose. Antigravity root: 228 discovered, 7 indexed, 221 excluded,
  0 pending, 0 blocked, matching the read-only sanity check. Evidence:
  `index.stdout.json`, `index.stderr.ndjson`, `post-index-status.json`.
- `refresh.state partial` from five blocked Claude files, all written today by
  Claude Code 2.1.295 and all still growing: four carry a `last-prompt`
  record with a new `explicit` key (10 records corpus-wide) and one subagent
  file carries an `attachment` of type `snapshot` with `agentId`,
  `rendered` and `renderedRole`. Neither keyset is in
  `providers/claude.py`; the deterministic `unknown_conversation_record`
  failure is the designed response. `needs_attention` fell from 98 to 5; the
  five are new. Ticket raised in `.notes/project_open-questions.md`; not in
  this plan's scope.
- Five design-authority locators now return `resolved` through the installed
  CLI; design status flipped to Accepted.
- Smoke on the installed CLI: `--literal` complete; `--semantic` complete
  with `retrieval_mode hybrid`, no degradation warning (`model_load_ms`
  15,068, `query_embed_ms` 2,038); `--literal --provider antigravity`
  returned only Antigravity hits with derived project paths.
- `cc-search-chats-index.timer` re-enabled (next 2026-10-10 03:29 AEDT).
  Claude marketplace refreshed and plugin at 2.4.0; Codex plugin re-added.
  Systemd units unchanged in this release.
