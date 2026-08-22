# Cross-vendor semantic search todo

Only unresolved work remains here. Completed work and its exact evidence live in
`worklog.md`; an empty search result is not completion evidence.

## Outcome 4: consumer contract

- [ ] Write failing shared identity and JSON-v2 contract tests.
- [ ] Write failing primary/agent/tool/exhaustive/everything boundary tests.
- [ ] Write failing source-backed exact-resolution outcome tests.
- [ ] Write failing positive coverage and progress-stream tests.
- [ ] Implement the CLI/output/identity/resolution contract and verify skill and
      command examples.

## Outcome 5: project truth

- [ ] Rewrite `CLAUDE.md` against implemented behavior.
- [ ] Update README and tested systemd configuration.
- [ ] Add database architecture and migration/prune runbook.
- [ ] Prepare reproducible four-corpus UAT instructions.
- [ ] Propose any warranted `.notes/` change before writing it.

## Outcome 6: verification and acceptance

- [ ] Pass full pytest, Ruff lint, Ruff format, ty, and diff checks.
- [ ] Pass disposable PostgreSQL 18 migration/recovery suite.
- [ ] Present the uncommitted diff and evidence.
- [ ] Obtain separate commit/install/UAT authority before each gated action.
- [ ] Validate the production candidate without pruning quarantine tables.
- [ ] Pass positive standard/Ponytail Claude/Codex UAT and obtain human acceptance.
- [ ] If separately authorized, prune superseded snapshots and repeat integrity,
      size, freshness, and positive UAT checks.
