# Pending work

## Outcome 2: Ignore mode
- [ ] Red: AC3, AC4, AC5 PostgreSQL tests; CLI flag/env tests (AC13).
- [ ] `index --ignore-unknown-records` and
      `CC_SEARCH_INDEX_IGNORE_UNKNOWN_RECORDS`; plumb to `index_corpus`,
      `refresh_native_sources`, `_SourcePlan`.
- [ ] `_parse_batch` and `_stage_parsed_batch` ignore-mode behaviour;
      `ignored_unknown_record_count` on the run.
- [ ] Prove the unit is byte-identical; `--help` text; runbook `index.env`
      paragraph.
- [ ] Gates green; checkpoint commit.

## Outcome 3: Reporting
- [ ] Red: AC7, AC8, AC11, AC14-baseline tests; human line format test.
- [ ] `unknown_shape_summary` aggregate (SQL group by digest).
- [ ] Coverage scalars, per-shape warnings on every command, status list,
      human lines.
- [ ] Skill and command paragraphs; README and CLAUDE.md contracts.
- [ ] `EXPLAIN (ANALYZE, BUFFERS)` of the aggregate on the laptop database,
      recorded in the worklog.
- [ ] Gates green; checkpoint commit.

## Outcome 4: Acknowledgement file
- [ ] Red: AC9, AC10, AC12 tests; parser validation tests.
- [ ] `core/acknowledged_shapes.py` with `tomllib`.
- [ ] Index-time load before session/scope; exit 2 on invalid; stamping and
      revert in refresh; reader exclusion.
- [ ] `docs/examples/acknowledged-shapes.toml`; runbook section; pointer.
- [ ] Gates green; checkpoint commit.

## Outcome 5: Decisions, contracts, release metadata
- [ ] ADR 0009 and register entry; ADR 0003 pointer; Antigravity row note.
- [ ] Reconcile CLAUDE.md, README, skill, command, runbooks, architecture.
- [ ] CHANGELOG and version 2.5.0 across the packaging sites.
- [ ] Bounded documentation inspection against `--help` and a real status
      envelope, recorded in the worklog.
- [ ] Full gate set and `git diff --check`; checkpoint commit.

## After Outcome 5
- [ ] Collated UAT probe 1 on the disposable cluster with the human.
- [ ] Normalise checkpoints onto the accepted tree; rerun verification.
- [ ] Hand off to direct delivery (integration into `main`, release, deploy
      with migration 12 backup and baseline reparse) under separate authority.
