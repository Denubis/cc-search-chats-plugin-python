# Pending work

Working root: `/home/brian/people/Brian/cc-search-chats-plugin-python`.
Completed items move to `worklog.md`; do not tick them here.

## Outcome 2: schema and identity
- [ ] Failing tests: migration 11 (renamed constraints, three-table insert,
      `gemini` rejected), journey version pins and `maintenance_required`
      at version 10, identity enum and antigravity locator grammar.
- [ ] Add `Provider.ANTIGRAVITY`, identity key-kind table entry, registry
      "unregistered raises" assertion.
- [ ] Add `provider_antigravity_schema.sql` and ledger entry 11.
- [ ] Derive CLI `--provider` choices from `Provider`.
- [ ] Update `docs/architecture/database.md` provider vocabulary.
- [ ] Full gate set; checkpoint commit.

## Outcome 3: discovery, admission, parser, quiet exclusions, resolution
- [ ] Build synthetic fixtures under `tests/fixtures/providers/antigravity/`
      (no real session text).
- [ ] Failing unit tests: `tests/test_provider_antigravity.py` (admission,
      boundary timestamps, projections, exclusions, blocking codes,
      truncation, two-batch equivalence).
- [ ] Failing discovery tests in `tests/test_source_discovery.py` (UUID
      session only; decoys silent; default root and variable; existing
      `non_native_agy` test unchanged).
- [ ] Failing PostgreSQL tests `tests/postgresql/test_antigravity_refresh.py`
      for AC1–AC5 including the three-run stale-loop test, and journey
      additions (`--provider antigravity`, `events` retention).
- [ ] Implement `providers/antigravity.py` (every function ≤ 15).
- [ ] Implement `discover_antigravity_sources` and the default root /
      `CC_SEARCH_ANTIGRAVITY_ROOTS`.
- [ ] Register the Antigravity adapter (`inspect_artifacts=False`,
      admission, codes, state serialisation, `scan_unindexed` with admission).
- [ ] Refresh changes: admission in `_parse_and_stage_source`; full-size
      excluded checkpoint in `_stage_index_artifact`; sticky exclusion in
      `_plan_source`.
- [ ] Staleness: `_Checkpoint.source_status`, excluded rows report no
      unindexed bytes.
- [ ] Autouse `CC_SEARCH_ANTIGRAVITY_ROOTS` fixture in `tests/conftest.py`;
      prove it with the real store present.
- [ ] Docs owned here: `database.md` checkpoint semantics; `CLAUDE.md`
      Source Roots.
- [ ] Read-only operational probe against the real store (228 sources, zero
      diagnostics); record output.
- [ ] Full gate set; checkpoint commit.

## Outcome 4: derived working directory
- [ ] Failing unit tests (cwd on all rows, none without `run_command`,
      multi-batch, `cwd_established`).
- [ ] Failing PostgreSQL AC6 tests (tail establishes cwd → same-run reparse,
      unchanged locators and `embedding_value` count; NULL case; small-batch
      case).
- [ ] Parser: record first `run_command` `Cwd`; stamp all messages; expose
      `cwd_established`.
- [ ] Refresh: staged-row `cwd` update after the source's last batch;
      append → replace reparse once when established.
- [ ] Rerun the Outcome 3 stale-loop test.
- [ ] Docs: `database.md` derived `cwd`; `CLAUDE.md` `--project` wording.
- [ ] Full gate set; checkpoint commit.

## Outcome 5: consumer truth and release
- [ ] Update `README.md`, `skills/search-chat/SKILL.md`,
      `commands/search-chat.md`, `CLAUDE.md` CLI Contract,
      `docs/architecture/database.md`, `docs/runbooks/laptop-deployment.md`.
- [ ] Execute every added command example; record output.
- [ ] Bounded documentation inspection with a planted positive control;
      record findings and fixes.
- [ ] Amend `docs/design-plans/2026-08-10-cross-vendor-semantic-search.md`
      lines 55–56 and 96–98.
- [ ] Version `2.4.0` in `pyproject.toml`, both plugin manifests,
      marketplace, `tests/test_plugin_packaging.py`, `uv.lock`; CHANGELOG
      `**Added:**` entry.
- [ ] Run the five authority resolves with the installed CLI; flip the design
      status only if all resolve, otherwise record and ask the human the one
      question in the plan.
- [ ] Full gate set incl. `pre-commit validate-config` and every `--help`.
- [ ] Final checkpoint commit (release commit).

## After execution (not authorised by this plan)
- [ ] Human UAT over the finished surface (plan.md, Finished-work UAT) after
      the separately authorised backup, `index --migrate` and production index.
- [ ] Normalise checkpoints; rerun gates; direct-delivery request to `main`.
