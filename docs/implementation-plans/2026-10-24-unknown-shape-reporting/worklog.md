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
