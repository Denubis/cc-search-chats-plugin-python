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
