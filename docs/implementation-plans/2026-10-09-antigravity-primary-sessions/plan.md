# Antigravity primary sessions implementation plan

**Status:** Ready for execution

**Design authority:**
`docs/design-plans/2026-10-09-antigravity-primary-sessions.md` (accepted by the
human on 2026-10-09; its own status label stays Draft until the five authority
locators resolve through the *installed* CLI, which needs 2.3.7 installed and
indexed. Outcome 5 owns that flip and its failure route.)

**Working root:** `/home/brian/people/Brian/cc-search-chats-plugin-python`

**Integration target:** `main` at `27b637bbdd3cb99972132d533c3ce614906e95b2`,
equal to the checkout `HEAD` when this plan was written. The working tree was
clean. The unrelated dirty worktree `.worktrees/message-attribution` (branch
`message-attribution`) must remain untouched.

**Workspace handling.** No isolation was requested, no planned file is dirty,
and no concurrent agent is working here, so execution uses this checkout. The
checkout is on the default branch. Before the first edit, execution creates a
task-owned local branch `antigravity-primary-sessions` from `main` in this
checkout and obtains the human's assent to work there; it does not edit `main`
directly. The execution invocation authorises frequent private checkpoint
commits on that branch (route them through `denubis-git-commit:commit`). It
does not authorise pushing, installing, production migration, a production
index run, enabling timers, pruning, or rewriting published history. After the
human accepts the finished-work UAT, checkpoints are normalised onto the
accepted tree, the verification set is rerun, and a separate direct-delivery
request integrates into `main`.

## Current repository evidence

Verified on 2026-10-09 against `27b637b` (line numbers are current):

- `Provider` has two values and `NativeLocator.__post_init__` admits `uuid` for
  Claude and `id`/`ordinal` for Codex only
  (`src/cc_search_chats/core/identity.py:19-23`, `135-142`). `core/` must stay
  provider-neutral and must not import `providers/`.
- Default roots: `configured_source_roots` iterates a two-entry tuple of
  (provider, plural variable, singular variable, standard root, Ponytail root)
  (`providers/source_discovery.py:139-179`). Tests cover default, plural and
  singular resolution (`tests/test_source_discovery.py:660-722`).
- Discovery dispatch is binary with `else` meaning Codex at:
  `storage/postgresql/refresh.py:741-744` (`_discover_sources`),
  `storage/postgresql/staleness.py:88-92` (`_discover_root`),
  `storage/postgresql/resolution.py:239-266` (`_scan_unindexed_locator`).
- Parser dispatch is binary at `refresh.py:71-74` (`_PARSER_STATE_VERSIONS`),
  `263-291` (`source_failure_summary`, literal `VALUES ('claude', %s),
  ('codex', %s)`), `420-477` (`_serialize_parser_state`,
  `_deserialize_parser_state`), `1066-1128` (`_parse_batch`), `1131-1166`
  (`_skipped_record_diagnostics`), `1249-1270` (`_repaired_record_diagnostics`),
  `1405-1410` (initial state in `_parse_and_stage_source`), with the
  per-provider diagnostic sets at `refresh.py:88-117`.
- `_index_artifact` (`refresh.py:1343-1349`) runs `inspect_non_native_artifact`
  for every source before parsing; `_stage_index_artifact` (`1351-1387`)
  checkpoints an excluded artefact at `complete_byte_offset = 0`, so
  `pending_bytes` equals the file size and the file counts as unindexed in
  `staleness._unindexed_bytes` (`staleness.py:108-121`), as a pending tail
  (`cli.py:736`, `923`) and in `refresh.pending_bytes` (`cli.py:762-765`).
- `_plan_source` (`refresh.py:808-846`) returns no plan only when device,
  inode, size, mtime and parser version are unchanged; any growth of an
  `excluded` row falls through to `replace`. `staleness._Checkpoint`
  (`staleness.py:22-27`) does not carry `source_status`.
- Recorded complexity ceilings that a third `else` branch would worsen
  (`complexipy-snapshot.json`): `configured_source_roots` 23,
  `_discover_sources` 19, `_parse_and_stage_source` 23,
  `_skipped_record_diagnostics` 24, `refresh_native_sources` 39,
  `_scan_unindexed_locator` 22. `_parse_batch` (not recorded) must stay at or
  under 15. Every new function must be at or under 15; the ratchet admits no
  new exceptions.
- Three live CHECK constraints admit two providers: `message_current`
  (`storage/postgresql/schema.sql:24`), `source_root_current`
  (`refresh_schema.sql:3`), `source_failure_current`
  (`incremental_refresh_schema.sql:17`). All are unnamed, so PostgreSQL named
  them `<table>_provider_check`. The ledger ends at migration 10
  (`migrations.py:22-33`); applied bytes are immutable.
- Tests pinned to ten migrations: `tests/postgresql/test_migrations.py:299`,
  `326`, `371`; `tests/postgresql/test_cli_journey.py:306`. Enum pin:
  `tests/test_identity.py:53-55`.
- `--provider` choices are literal `("claude", "codex")` at `cli.py:2689`,
  `2729`, `2759`. `index --migrate` bypasses `require_current_schema`
  (`cli.py:2301-2304`); every other command raises `MaintenanceRequired`.
- `events._retention` (`events.py:65-80`) retains `user` prose when
  `submitted_by` is `unknown` and `session_kind` is `primary`, so Antigravity
  user prose is exported with no events change.
- `--project` is `COALESCE(m.repository, m.cwd) = %s` (`index.py:138`).
- PostgreSQL tests set only `CC_SEARCH_CLAUDE_ROOT`/`CC_SEARCH_CODEX_ROOT`
  (for example `tests/postgresql/test_cli_journey.py:285-286`); nothing today
  stops a real `~/.gemini/antigravity-cli/brain` from becoming a default root
  once Outcome 2 exists. A missing configured root is a `_ROOT_FAILURES`
  runtime error (`refresh.py:745-751`), so the isolating fixture must create
  its empty directory.
- Native store observed read-only on 2026-10-09: 237 UUID directories, 228
  with `transcript_full.jsonl`; first records are `USER_EXPLICIT`/`USER_INPUT`
  (227) or `SYSTEM`/`SYSTEM_MESSAGE` (1); eight start in October (seven human,
  one system). October record pairs: `USER_INPUT` 28, `PLANNER_RESPONSE` 149,
  `GENERIC` 331, `SYSTEM_MESSAGE` 5, `CHECKPOINT` 2. Every record carries
  `source`, `type`, `created_at` (`YYYY-MM-DDTHH:MM:SSZ`), `status`,
  `step_index`. `PLANNER_RESPONSE` carries `content`, `thinking` and
  `tool_calls` **each optionally** (all four observed combinations occur), plus
  token counters. `tool_calls` items are `{"name": str, "args": object}`;
  `run_command` args are an object with a string `Cwd` (seven of seven October
  human sessions have one). `USER_INPUT` content opens with `<USER_REQUEST>`
  and continues with `<ADDITIONAL_METADATA>` and `<USER_SETTINGS_CHANGE>`
  elements. `GENERIC` may carry `error`.
- Release artefacts carry the version in `pyproject.toml:7`,
  `.claude-plugin/plugin.json:4`, `.claude-plugin/marketplace.json:13`,
  `.codex-plugin/plugin.json:3`, `tests/test_plugin_packaging.py:21`, and
  `uv.lock`; `CHANGELOG.md` uses `**Fixed:**`/`**Changed:**` headings.

## Scope and acceptance ownership

| Criterion | Primary owner |
|---|---|
| AC1 Admission | Outcome 3 (unit tests on the parser; PostgreSQL index test for messages and checkpoint rows) |
| AC2 Quiet exclusions | Outcome 3 (PostgreSQL refresh tests including the three-run stale-loop test) |
| AC3 Incremental identity | Outcome 3 (PostgreSQL refresh and resolution tests) |
| AC4 Content boundary | Outcome 3 (parser unit tests; PostgreSQL search tests) |
| AC5 Isolation | Outcome 3 (discovery unit tests; existing `non_native_agy` test kept green) |
| AC6 Derived working directory | Outcome 4 |
| AC7 Migration 11 | Outcome 2 |
| AC8 Unchanged providers | Outcome 1 (and reconfirmed by every later outcome) |
| Human judgment | Finished-work UAT after Outcome 5 |

Out of scope, per the design's non-goals: subagent sessions, Gemini CLI chats,
pre-October sessions, a sliding window, sidecar or `history.jsonl` reads,
message attribution, retiring `non_native_agy`. Production migration,
production index, installation, pushing and publication are separate release
actions and are not executed by this plan.

Implementation decisions this plan fixes from repository evidence (they are
not design changes; the implementer should not reopen them):

- **Required keys per registered pair.** Every record requires string
  `source`, `type` and a `created_at` matching `YYYY-MM-DDTHH:MM:SSZ`.
  `USER_INPUT` additionally requires string `content` containing a leading
  `<USER_REQUEST>` element. `PLANNER_RESPONSE` requires none of `content`,
  `thinking`, `tool_calls`, but each present value must have the right type
  (`content` string, `thinking` string, `tool_calls` list of objects with
  string `name` and any `args`); a wrong type blocks the source. `GENERIC`,
  `SYSTEM_MESSAGE` and `CHECKPOINT` require only the common keys. A missing
  `status` or `step_index` never blocks (extra keys never block, and these are
  never read).
- **Truncation marker.** A `<truncated N bytes>` marker inside indexed prose is
  reported through the adapter's repaired-diagnostic set, which surfaces in
  `coverage.repaired_records` without a warning. That is the "run diagnostic"
  the design names.
- **Tool rows.** One `tool_name` row holds the call names joined with a single
  space in call order; one `tool_input` row holds each call's `args` rendered
  by the same JSON rendering `codex._tool_text` uses, joined with a newline in
  call order. Both rows share the record's locator and logical ID with its
  prose row.
- **Registry location and shape.** `src/cc_search_chats/providers/registry.py`
  exposes one frozen `ProviderAdapter` per provider and
  `provider_adapter(provider)` / `provider_adapters()` lookups. The adapter
  owns: `provider`, root defaults (standard path and Ponytail-style optional
  path builders, plural and singular variable names), `discover(root,
  inspect_content)`, `parser_state_version`, `initial_state()`,
  `serialize_state`, `deserialize_state`, `parse(envelopes, *,
  source_session_id, source_diagnostics, prior_state)`,
  `unsupported_codes`, `skippable_codes`, `repaired_codes`,
  `inspect_artifacts` (True for Claude and Codex, False for Antigravity),
  `admission(first_record_bytes)` (None for Claude and Codex), and
  `scan_unindexed(path, source_file_relative, locator)`. Locator key-kind
  permissions stay a data table inside `core/identity.py` because `core/`
  cannot import `providers/`; a unit test asserts the registry and the table
  agree.
- **Version.** This is a new provider, so the release is `2.4.0` with an
  `**Added:**` CHANGELOG heading.

## Outcome 1: Claude and Codex run through a provider registry

**Goal:** Every provider-specific decision outside the two adapters is made by
looking up a registry entry. Behaviour, output, locators, parser-state
serialisation, diagnostics and schema are byte-for-byte unchanged for Claude
and Codex, and the complexity gate reports no recorded function worse.
**Depends on:** nothing.
**Owns:** AC8; the design's "Provider registry" section.

### Files and consumers
- Create: `src/cc_search_chats/providers/registry.py` — `ProviderAdapter` and
  the two entries, built from the existing `claude.py` and `codex.py` surfaces
  (`parse_claude_session`, `parse_codex_session`, the `*SessionContext`,
  `*ParserState`, `*ParseResult` types).
- Modify: `src/cc_search_chats/storage/postgresql/refresh.py` — replace
  `_PARSER_STATE_VERSIONS`, the literal `VALUES` list in
  `source_failure_summary`, `_serialize_parser_state`,
  `_deserialize_parser_state`, the discovery branch in `_discover_sources`,
  `_parse_batch`, `_skipped_record_diagnostics`,
  `_repaired_record_diagnostics`, the initial-state branch in
  `_parse_and_stage_source`, and gate `_index_artifact` on
  `adapter.inspect_artifacts`.
- Modify: `src/cc_search_chats/storage/postgresql/staleness.py` —
  `_discover_root` uses `adapter.discover`.
- Modify: `src/cc_search_chats/storage/postgresql/resolution.py` —
  `_scan_unindexed_locator` uses `adapter.discover` and
  `adapter.scan_unindexed`; the Claude session-stem filter moves behind the
  adapter (Claude's `scan_unindexed` receives the discovery source and decides
  whether the session matches).
- Modify: `src/cc_search_chats/providers/source_discovery.py` —
  `configured_source_roots` iterates the registry's root defaults instead of
  its inline tuple.
- Modify: `complexipy-snapshot.json` — the passing gate rewrites it; stage the
  rewritten file.
- Test: `tests/test_provider_registry.py` — registry entries agree with the
  identity key-kind table and with each adapter's public surface.
- First real consumer: `refresh_native_sources` and `cc-search-chats index`
  (unchanged observable behaviour, now routed through the registry).

### Work
1. Run the full non-PostgreSQL and PostgreSQL suites and record the complexity
   gate output as the baseline (positive control: both suites green at
   `27b637b`).
2. Write `tests/test_provider_registry.py`: for each `Provider` value a
   registry entry exists; its `parser_state_version` equals the value the
   refresh module used before (Claude 6, Codex 5); `serialize_state(
   initial_state())` round-trips through `deserialize_state`; the key-kind
   table in `core/identity.py` and the adapter agree. Confirm the test fails
   for the right reason (no registry module).
3. Introduce the registry and move each dispatch site onto it, one site at a
   time, rerunning the PostgreSQL refresh, resolution, unindexed-source and
   CLI-journey suites after each move. Keep the two adapters' modules
   untouched except for exposing already-existing names.
4. Replace the three dispatch functions' `if provider is Provider.CLAUDE`
   shape with adapter lookups so their complexity falls. Do not restructure
   anything the registry does not touch.
5. Run the complexity gate; stage the rewritten snapshot. Run vulture (every
   adapter field needs a consumer).

### Verification
- Run: `uv run --frozen pytest -q -m 'not postgresql'` and
  `uv run --frozen pytest -q -m postgresql`
  - Positive signal: all tests pass with the same collected counts as the
    baseline plus the new registry tests.
  - Failure signal: any Claude or Codex behavioural test fails, or
    `test_cross_vendor_index.py` reports different locators or coverage.
- Run: `uv run --frozen complexipy --failed --plain`
  - Positive signal: exit 0; `git diff complexipy-snapshot.json` shows only
    unchanged or lowered values for the recorded refresh, resolution and
    discovery functions.
  - Failure signal: a new function above 15 or a recorded function higher.
- Run: `uv run --frozen ruff check src tests scripts`,
  `uv run --frozen ruff format --check src tests scripts`,
  `uv run --frozen ty check src tests scripts`, `uv run --frozen vulture`
  - Positive signal: all exit 0. Failure signal: any non-zero exit.
- Operational probe: `uv run --frozen cc-search-chats index --status --json`
  against the disposable cluster in the CLI-journey test keeps its
  `coverage` and `index_state` keys unchanged (this is asserted by the
  existing journey test; no new assertion).

### Finished-work implication
None: automated evidence settles it.

## Outcome 2: Schema and identity admit the `antigravity` provider

**Goal:** Migration 11 is a new immutable ordered resource; the three provider
CHECK constraints admit `claude`, `codex` and `antigravity`; `Provider` has a
third value; the locator grammar accepts exactly `ordinal`+`sha256` for it;
`--provider antigravity` is a valid CLI choice. No source is yet discovered,
so the searchable corpus is unchanged.
**Depends on:** Outcome 1 (the registry needs a third entry here, with
discovery returning no sources until Outcome 3).
**Owns:** AC7; the design's "Identity" grammar and the "Public contract"
provider value.

### Files and consumers
- Create: `src/cc_search_chats/storage/postgresql/provider_antigravity_schema.sql`
  — a `DO` block that, for each of `message_current`, `source_root_current`,
  `source_failure_current`, finds every CHECK constraint whose only referenced
  column is `provider` (via `pg_constraint` joined to `pg_attribute` through
  `conkey`), drops it, then adds `<table>_provider_check CHECK (provider IN
  ('claude', 'codex', 'antigravity'))`. It must not touch the legacy
  `message` relation or the quarantine schema.
- Modify: `src/cc_search_chats/storage/postgresql/migrations.py` — append
  `Migration(11, "provider_antigravity_schema.sql")`.
- Modify: `src/cc_search_chats/core/identity.py` — `Provider.ANTIGRAVITY =
  "antigravity"`; the key-kind table permits only `ORDINAL` for it;
  `parse_locator` and `format_locator` need no change beyond the table.
- Registry: no Antigravity entry in this outcome (a placeholder adapter
  would be an interface valid only later). `provider_adapter(Provider.
  ANTIGRAVITY)` raises `KeyError` until Outcome 3; no runtime path reaches it
  because roots come only from registry defaults or variables, and
  `_scan_unindexed_locator` returns `source_unavailable` when no root matches
  the locator's provider. The Outcome 1 registry test stays parameterised over
  the registered providers and asserts the unregistered value raises.
- Modify: `src/cc_search_chats/cli.py:2689`, `2729`, `2759` — choices derive
  from `tuple(value.value for value in Provider)`.
- Test: `tests/postgresql/test_migrations.py` — version pins move to eleven;
  new test: rename the three constraints to arbitrary names on a version-10
  schema, migrate, assert each table has exactly one `provider` CHECK named
  `<table>_provider_check`, that `antigravity` inserts into all three, and
  that `gemini` is rejected with a check-violation error.
- Test: `tests/postgresql/test_cli_journey.py:306` — applied version eleven;
  new assertion that a version-10 database makes `search --literal --json`
  report `maintenance_required` with pending version 11 and leaves the ledger
  at 10.
- Test: `tests/test_identity.py` — enum pin becomes three values; locator
  tests: `ccchat:v1:antigravity:<uuid>:ordinal:3:sha256:<d>` round-trips;
  `uuid` and `id` keys for `antigravity` raise `ValueError`; parse of such a
  string returns the malformed outcome.
- First real consumer: `cc-search-chats index --migrate` on the disposable
  cluster; `parse_locator` for `resolve`.

### Work
1. Write the failing migration, journey and identity tests above.
2. Add the enum value, key-kind table entry and migration resource; append the
   ledger entry; derive CLI choices.
3. Run the DBA review rubric mentally on the `DO` block: it must be
   idempotent against constraint names, run inside the migration transaction,
   and fail loudly if a table has no `provider` CHECK (raise rather than add a
   duplicate).
4. Update `docs/architecture/database.md` provider vocabulary where it lists
   constraint values (bounded agent inspection against the applied schema).

### Verification
- Run: `uv run --frozen pytest -q -m postgresql tests/postgresql/test_migrations.py tests/postgresql/test_cli_journey.py`
  - Positive signal: renamed-constraint test passes; `gemini` insert raises
    `psycopg.errors.CheckViolation`; journey reports version 11.
  - Failure signal: migration adds a second CHECK, or a pre-migration command
    changes the ledger.
- Run: `uv run --frozen pytest -q tests/test_identity.py`
  - Positive signal: three enum values; antigravity ordinal locator
    round-trips. Failure signal: `uuid` key accepted for antigravity.
- Run: the full gate set from Outcome 1.

### Finished-work implication
None: automated evidence settles it.

## Outcome 3: Antigravity primary sessions are discovered, admitted, indexed, quiet when excluded, and exactly resolvable

**Goal:** With `~/.gemini/antigravity-cli/brain` present (or
`CC_SEARCH_ANTIGRAVITY_ROOTS` set), `index` admits October human-initiated
sessions as `antigravity`/`primary` messages with NULL `cwd`, checkpoints
excluded sessions silently at full size, blocks unrecognised sessions
deterministically, refreshes admitted sessions by suffix, and `resolve`
verifies their locators against native bytes. Test suites never read the real
store.
**Depends on:** Outcome 2.
**Owns:** AC1, AC2, AC3, AC4, AC5; the design's discovery, admission,
excluded-checkpoint, record-policy and failure sections.

### Files and consumers
- Create: `src/cc_search_chats/providers/antigravity.py` —
  `AntigravityDiagnosticCode` (including `UNRECOGNISED_FIRST_RECORD`,
  `BEFORE_SCOPE_START`, `NOT_HUMAN_INITIATED`, `UNKNOWN_RECORD_PAIR`,
  `MISSING_REQUIRED_KEY`, `INVALID_FIELD_TYPE`, `MISSING_USER_REQUEST`,
  `INVALID_CREATED_AT`, `MALFORMED_JSON`, `INVALID_ENCODING`,
  `INVALID_UNICODE`, `REPAIRED_UNICODE`, `TRUNCATED_PROSE`),
  `SCOPE_START = "2026-10-01T00:00:00Z"` as a module constant,
  `AntigravitySessionContext(source_session_id)`,
  `AntigravityParserState(next_conversation_epoch, cwd)` (cwd stays `None`
  until Outcome 4 populates it), `AntigravityParseResult`,
  `admit_antigravity_session(first_record_bytes)`, and
  `parse_antigravity_session(envelopes, *, context, source_diagnostics,
  prior_state)`. Every function at or under complexity 15: separate the
  per-pair projections (`_project_user_input`, `_project_planner_response`,
  `_project_checkpoint`) and the shape validators.
- Modify: `src/cc_search_chats/providers/source_discovery.py` —
  `discover_antigravity_sources(root, *, inspect_content)`: list immediate
  children with `os.scandir`, accept a directory entry (not following
  symlinks) whose name full-matches the canonical lowercase UUID pattern and
  whose `<uuid>/.system_generated/logs/transcript_full.jsonl` is a regular
  file by `lstat`; emit no archive diagnostic; reuse `_root_failure`. The
  default root `~/.gemini/antigravity-cli/brain` is configured only when it
  is a directory; plural variable `CC_SEARCH_ANTIGRAVITY_ROOTS`; no singular
  variable.
- Modify: `src/cc_search_chats/providers/registry.py` — third entry:
  version 1, `inspect_artifacts=False`, `admission=admit_antigravity_session`,
  diagnostic sets, serialisation of `{"next_conversation_epoch": int, "cwd":
  str | null}`, and `scan_unindexed` that applies admission first and reports
  nothing recognised for an excluded or blocked session.
- Modify: `src/cc_search_chats/storage/postgresql/refresh.py` —
  (a) `_parse_and_stage_source`: when `plan.start_byte_offset == 0` and the
  adapter has `admission`, decide on the first complete envelope of the first
  batch before parsing; `excluded` stages a checkpoint with
  `complete_byte_offset = observed.size`, `pending_bytes = 0`,
  `source_status = 'excluded'`, `parser_state = {"excluded_code": ...,
  "detail": ...}` and no messages; `blocked` raises `_SourceRefreshError`
  with `failure_class="deterministic"` and the admission code; a first batch
  with no complete record stages the ordinary undecided checkpoint.
  (b) `_stage_index_artifact`: `complete_byte_offset = plan.observed.size`
  for the existing artefact exclusion too (design: adopted default; production
  has zero excluded rows).
  (c) `_plan_source`: when `checkpoint.source_status == "excluded"`, the
  parser version matches, device and inode match, and
  `observed.size >= checkpoint.observed_size`, return `None` (sticky
  exclusion; the row is not rewritten). Shrink, identity change or version
  advance still plans `replace`.
  (d) `source_failure_summary` already iterates the registry (Outcome 1).
- Modify: `src/cc_search_chats/storage/postgresql/staleness.py` —
  `_Checkpoint` gains `source_status`; `_load_checkpoints` selects it;
  `_unindexed_bytes` returns `None` for an `excluded` checkpoint with the same
  device and inode and `st_size >= observed_size`.
- Modify: `src/cc_search_chats/storage/postgresql/resolution.py` — no new
  branch; the adapter's `scan_unindexed` filters to the session whose
  `source_file_relative.parts[0]` equals the locator's session ID.
- Modify: `tests/conftest.py` — autouse session fixture that creates an empty
  temporary directory and sets `CC_SEARCH_ANTIGRAVITY_ROOTS` to it for every
  test (both suites import this root conftest). Add one test in
  `tests/test_source_discovery.py` proving the fixture's effect: with the
  variable unset and a `home` containing `.gemini/antigravity-cli/brain`,
  `configured_source_roots` lists it after the Codex roots; with the variable
  set, it does not.
- Create fixtures under `tests/fixtures/providers/antigravity/`, **synthetic,
  containing no text copied from real sessions**: `october_human/` (a session
  directory with `.system_generated/logs/transcript_full.jsonl` holding a
  `USER_INPUT` with `<USER_REQUEST>`, `<ADDITIONAL_METADATA>` and
  `<USER_SETTINGS_CHANGE>` text, a `PLANNER_RESPONSE` with `content`,
  `thinking` and two `tool_calls` including `run_command` with `Cwd`, a
  `GENERIC` result, a `SYSTEM_MESSAGE`, a `CHECKPOINT`, a post-checkpoint
  exchange, and one prose record holding `<truncated 12 bytes>`), plus a
  sibling `transcript.jsonl`, `messages/`, `steps/` and `scratch/` decoys
  with distinctive text; `pre_october/` (first record
  `2026-09-30T23:59:59Z`); `boundary_admitted/` (first record
  `2026-10-01T00:00:00Z`); `system_first/`; `unknown_first/` (first record
  `{"hello": 1}`); `unregistered_pair/` (admitted session whose third record
  is `MODEL`/`SOMETHING_NEW`); and a root-level decoy `settings.json`,
  `antigravity-oauth-token` (mode 000 at test time) and a non-UUID directory
  `notes` containing a `transcript_full.jsonl`.
- Test: `tests/test_provider_antigravity.py` (unit, no PostgreSQL) —
  admission outcomes for the five first records and the two boundary
  timestamps; projection of each pair; `thinking`, `GENERIC`,
  `<ADDITIONAL_METADATA>`, `SYSTEM_MESSAGE` and `CHECKPOINT` text absent from
  every message; epoch 1 after `CHECKPOINT`; tool rows share the prose row's
  locator; malformed line skipped and consumes its ordinal; blocking codes
  for an unregistered pair, a missing wrapper, a wrong-typed `tool_calls`, and
  a bad `created_at`; truncation marker yields `TRUNCATED_PROSE` and the kept
  text; a parse split across two batches (small `max_batch_bytes`) yields the
  same messages and `next_state` as one batch.
- Test: `tests/test_source_discovery.py` — Antigravity discovery lists exactly
  the UUID session with the full transcript, ignores the `notes` directory,
  the unreadable token file, a symlinked session directory, and a UUID
  directory lacking the file, with zero diagnostics (AC5); the existing
  `non_native_agy` test at line 610 stays green (AC5 failure clause).
- Test: `tests/postgresql/test_antigravity_refresh.py` — AC1 (messages only
  from admitted fixtures, `provider='antigravity'`, `session_kind='primary'`,
  `cwd IS NULL` in this outcome; excluded rows carry their codes in
  `parser_state`; unknown-first lands in `source_failure_current` as
  deterministic and its text is unsearchable); AC2 (second run reads zero
  content bytes, creates no generation, `index_state` and `--status` report
  zero unindexed and `pending_tail_files`/`pending_bytes` zero; appending to
  an excluded file changes nothing; replacing an excluded file with the
  admitted fixture admits it; three consecutive runs leave the excluded
  checkpoint row byte-identical as compared by `row_to_json`, and the run
  rows show zero planned, read and failed sources for it); AC3 (append reads
  only past the watermark via `attempted_content_bytes`; earlier locators
  still resolve; a one-byte edit yields `stale_source`; a locator into an
  excluded session yields `no_match`); AC4 (search assertions per mode; the
  unregistered-pair fixture blocks and reports in `source_issues`); AC5
  (`transcript.jsonl` decoy text never found).
- Test: `tests/postgresql/test_cli_journey.py` — `search --literal --provider
  antigravity --json` returns the fixture message with `mode` and
  `retrieval_mode` unchanged; `events` exports its user prose as retained.
- First real consumer: `cc-search-chats index` followed by `search --literal`
  and `resolve` over the fixture root, exercised by the journey test.

### Work
1. Write the failing unit tests for admission and projection first, then
   discovery, then the PostgreSQL suite file; confirm each fails for a missing
   symbol or wrong outcome, not a fixture error.
2. Implement the parser module pure (no I/O), then discovery, then the
   registry entry, then the three refresh changes (a)–(c) and the staleness
   change, running the new PostgreSQL file after each.
3. Add the autouse root fixture before running any PostgreSQL test that
   consults `configured_source_roots()`; prove it by running the journey
   suite with a real-looking `~/.gemini/antigravity-cli/brain` present
   (the developer machine has one) and asserting the `index_state` root list
   contains only fixture roots.
4. Update `docs/architecture/database.md` (source-file checkpoint semantics:
   excluded rows checkpoint at full size and are sticky) and
   `CLAUDE.md` Source Roots (the third default root and variable) in this
   outcome, because the behaviour they describe lands here.
5. Rerun the complexity gate; every new function must be at or under 15.

### Verification
- Run: `uv run --frozen pytest -q tests/test_provider_antigravity.py tests/test_source_discovery.py`
  - Positive signal: all pass; the boundary tests show `2026-09-30T23:59:59Z`
    excluded and `2026-10-01T00:00:00Z` admitted.
  - Failure signal: any decoy text projected, or a blocking case parsed as a
    message.
- Run: `uv run --frozen pytest -q -m postgresql tests/postgresql/test_antigravity_refresh.py tests/postgresql/test_cli_journey.py tests/postgresql/test_unindexed_sources.py tests/postgresql/test_pending_tail_coverage.py`
  - Positive signal: the stale-loop test's three runs show
    `changed_source_count = 0`, `read_source_count = 0`,
    `attempted_content_bytes = 0`, no new `corpus_generation`, and identical
    checkpoint JSON; `index --status --json` shows `unindexed.files = 0`.
  - Failure signal: an excluded source reappears in any count, or the
    existing pending-tail and unindexed-source tests change outcome for
    Claude or Codex.
- Run: the full gate set from Outcome 1 (both suites, complexity, ruff, ty,
  vulture).
- Operational probe (read-only, developer machine):
  ```
  uv run --frozen python -c "from pathlib import Path; from cc_search_chats.providers.source_discovery import discover_antigravity_sources as d; r = d(Path.home() / '.gemini/antigravity-cli/brain', inspect_content=False); print(len(r.sources), [x.code.value for x in r.diagnostics])"
  ```
  - Positive signal: 228 sources, zero diagnostics.
  - Failure signal: any diagnostic naming a sibling file, or a count other
    than the `transcript_full.jsonl` count observed that day.

### Finished-work implication
None at this outcome: the human judgment is collected once over the finished
surface after Outcome 5.

## Outcome 4: Sessions carry a derived working directory

**Goal:** Every row of an admitted session carries `cwd` equal to the `Cwd`
argument of the first `run_command` call in the checkpointed prefix, whether
that call was indexed in the first pass or arrived in a later append;
sessions without one keep NULL; identities and embedding digests do not
change when a tail first establishes the value.
**Depends on:** Outcome 3.
**Owns:** AC6; the design's "Derived working directory" section.

### Files and consumers
- Modify: `src/cc_search_chats/providers/antigravity.py` — the parser records
  the first `run_command` `Cwd` string into `next_state.cwd`; every message in
  the result carries the state's `cwd`; the result exposes
  `cwd_established: bool` (True when `prior_state.cwd` was None and
  `next_state.cwd` is not).
- Modify: `src/cc_search_chats/storage/postgresql/refresh.py` —
  (a) after a source's batches are parsed and staged, if the final state's
  `cwd` differs from the value the earlier batches of this run carried,
  update that source's staged message rows' `cwd` (batch hazard: a later
  batch in the same run may establish the value after earlier rows were
  staged); (b) when `plan.disposition == "append"` and the parse reports
  `cwd_established`, discard that source's staged suffix and reparse it as a
  `replace` plan from byte zero in the same run, exactly once (a second
  establishment is impossible by construction, but guard against looping with
  an assertion). Keep each helper at or under complexity 15.
- Modify: `src/cc_search_chats/providers/registry.py` — the parse result
  protocol gains the `cwd_established` flag (False for Claude and Codex).
- Test: `tests/test_provider_antigravity.py` — `cwd` set on messages before
  and after the first `run_command`; absent when there is none; a parse split
  across batches gives all messages the value; `cwd_established` only when
  newly set.
- Test: `tests/postgresql/test_antigravity_refresh.py` — AC6: index a fixture
  whose `run_command` arrives only in an appended tail; after the second run
  `--project <derived path>` matches every prose row, the locators of the
  earlier rows are unchanged, `embedding_value` row count is unchanged, and
  `read_source_count` shows the source was reparsed from zero in that run; a
  fixture without `run_command` has NULL `cwd` and is found only without
  `--project`; a fixture indexed with a batch limit smaller than the file has
  `cwd` on its first row.
- First real consumer: `search --literal --project <path>` and `list`.

### Work
1. Write the failing unit and PostgreSQL tests.
2. Implement the parser change, then the staged-row update, then the
   append-reparse route; rerun the Outcome 3 stale-loop test to show excluded
   sources are untouched by the new route.
3. Document the derived value in `docs/architecture/database.md` (`cwd` is
   derived for Antigravity, native for others) and in the CLI contract wording
   for `--project` in `CLAUDE.md`.

### Verification
- Run: `uv run --frozen pytest -q tests/test_provider_antigravity.py` and
  `uv run --frozen pytest -q -m postgresql tests/postgresql/test_antigravity_refresh.py`
  - Positive signal: AC6 tests pass; the append case reports a from-zero
    reparse in the run row while `attempted_content_bytes` for the earlier,
    unchanged Claude and Codex fixtures stays zero.
  - Failure signal: locators or `embedding_value` counts change across the
    reparse, or the first row of a multi-batch parse has NULL `cwd`.
- Run: the full gate set from Outcome 1.

### Finished-work implication
None at this outcome.

## Outcome 5: Consumers tell the truth and the release is assembled

**Goal:** Every human- and agent-facing surface describes the three-provider
corpus accurately, the superseded sentences of the 2026-08-10 design are
amended so one design owns each criterion, the design's status reflects the
installed-CLI check, and the release artefacts carry `2.4.0` with a CHANGELOG
entry. Production migration, production index and installation stay separate
release actions.
**Depends on:** Outcome 4.
**Owns:** the design's "Consumers to update at implementation" and
"Migration" documentation; the human UAT below.

### Files and consumers
- Modify: `README.md`, `skills/search-chat/SKILL.md`,
  `commands/search-chat.md` — providers, `--provider antigravity`, the root
  and variable, what is and is not indexed (October onwards, primaries only,
  no reasoning, tool results, injected context), and the derived `--project`
  value.
- Modify: `CLAUDE.md` — CLI Contract (`provider` values; `--agents` adds
  nothing for Antigravity; excluded sources are quiet) and Source Roots
  (already touched in Outcome 3; reconcile wording).
- Modify: `docs/architecture/database.md` — reconcile the Outcome 2–4 edits
  into one coherent provider section.
- Modify: `docs/runbooks/laptop-deployment.md` — the preserving-upgrade path
  now includes migration 11 after the ADR 0007 backup, and the expected first
  production index report (seven admitted, 221 excluded, none blocked as of
  2026-10-09) as an operational check.
- Modify: `docs/design-plans/2026-08-10-cross-vendor-semantic-search.md`
  lines 55–56 and 96–98 — amend to "Antigravity subagent sessions, Gemini CLI
  chats and rendered transport archives never enter the searchable corpus;
  primary Antigravity sessions are governed by the 2026-10-09 design".
- Modify: `docs/design-plans/2026-10-09-antigravity-primary-sessions.md` —
  status flip, see Work step 4.
- Modify: `CHANGELOG.md` (`## cc-search-chats 2.4.0`, `**Added:**`),
  `pyproject.toml`, `.claude-plugin/plugin.json`,
  `.claude-plugin/marketplace.json`, `.codex-plugin/plugin.json`,
  `tests/test_plugin_packaging.py:21`, and `uv.lock` (via `uv lock`; the
  configured cache only).
- First real consumer: the bundled skill, read by an agent running
  `cc-search-chats search --literal --provider antigravity`.

### Work
1. Execute every command example added to the docs against the disposable
   fixture root or `--help`, and record the output in the worklog.
2. Bounded agent inspection (a `denubis-plan-and-execute:code-reviewer` or
   `coherence-reviewer` agent, dispatched with the standing rules restated and
   at a model and effort the human has named; if none is named, perform the
   inspection in-session): every provider, root, flag and exclusion statement
   in the five consumer documents matches the implemented interface and the
   design's non-goals. Record each finding and its fix.
3. Amend the 2026-08-10 design lines; bump the version everywhere listed;
   `uv run --frozen pytest -q tests/test_plugin_packaging.py`.
4. Run the five authority resolves with the **installed** CLI
   (`cc-search-chats resolve '<locator>' --reference-only --json`). If all
   five return `resolved`, change the design's status to Accepted and record
   the output. If any returns `stale_index` or another outcome, leave the
   status Draft, record the outcome in the worklog, and raise one question to
   the human: whether to install 2.3.7 and index now so the condition can be
   met, or to accept the grep fallback as the resolution condition. Do not
   install or index production yourself.
5. Assemble the release commit as the final checkpoint on the task branch.

### Verification
- Run: `uv run --frozen pytest -q -m 'not postgresql'` and
  `uv run --frozen pytest -q -m postgresql` (full), plus complexity, ruff, ty,
  vulture, and `uv run --frozen pre-commit validate-config`.
  - Positive signal: all green at `2.4.0`.
  - Failure signal: the packaging test disagrees with any manifest.
- Run: `uv run --frozen cc-search-chats --help` and each subcommand `--help`.
  - Positive signal: `--provider` lists `antigravity`.
  - Failure signal: any help text still names two providers.
- Documentation: the inspection record in the worklog names each document,
  the claims checked, and the positive control (a deliberately wrong claim
  planted and caught, then removed).

### Finished-work implication
Collected below as the single UAT over the finished surface.

## Finished-work UAT (after Outcome 5, all gates green)

The automated suites prove the deterministic criteria on fixtures. What they
cannot settle is whether a real October Antigravity conversation, once
indexed on the operator's database, reads as that conversation. This UAT needs
the separately authorised production steps first (ADR 0007 backup,
`index --migrate`, one `index` run on the installed 2.4.0); it is not run by
execution.

1. **Action:** search for a phrase the human remembers typing into an October
   Antigravity session with `cc-search-chats search --literal --provider
   antigravity "<phrase>"`. **Judgment:** the hit is that session, the
   snippet is the human's request text and not harness metadata, and
   `context` around it reads as the conversation the human had. **Falsifier:**
   the hit shows `<ADDITIONAL_METADATA>` or settings text, or the surrounding
   context interleaves tool results or thinking.
2. **Action:** run `cc-search-chats resolve '<the hit's locator>'`.
   **Judgment:** it resolves to the same text. **Falsifier:** `stale_source`
   or `no_match` on an untouched session.
3. **Action:** run `cc-search-chats index --status`. **Judgment:** the report
   reads calm: the Antigravity root shows the admitted and excluded counts the
   operator expects, with no unindexed files or pending bytes for excluded
   sessions and no source issues. **Falsifier:** 221 excluded sessions appear
   as unindexed, pending, or needing attention.
4. **Action:** search without `--provider` for a phrase from a pre-October or
   subagent Antigravity session. **Judgment:** nothing from those sessions is
   returned. **Falsifier:** any hit from them.

## Lifecycle boundaries

- Private checkpoints on `antigravity-primary-sessions` are authorised by the
  execution invocation; each outcome ends with at least one.
- Normalisation after accepted UAT preserves the accepted tree, then reruns
  both suites and all gates.
- Integration into `main`, pushing, installing 2.4.0, the ADR 0007 backup,
  `index --migrate`, the first production index, and plugin publication each
  require their own authority and are not part of this plan's execution.
