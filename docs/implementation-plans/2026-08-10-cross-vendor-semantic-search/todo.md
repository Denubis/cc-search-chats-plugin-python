# Cross-vendor semantic search todo

All items are pending. Check an item only after its named evidence passes; an
empty search result is not completion evidence.

## Outcome 1: normalized storage

- [ ] Write failing snapshot-multiplication and no-op cardinality tests.
- [ ] Write failing migration-ledger, candidate validation, and rollback tests.
- [ ] Implement normalized current corpus, aliases, generations, staging, and
      reusable embedding storage.
- [ ] Implement and test a non-dropping prune dry-run.
- [ ] Prove all PostgreSQL consumers use normalized relations.

## Outcome 2: incremental multi-root refresh

- [ ] Write failing plural-root and standard/Ponytail discovery tests.
- [ ] Write failing root-isolation and cross-root collision tests.
- [ ] Write failing no-op/append/partial/truncate/replace/advance/crash tests with
      byte-range observations.
- [ ] Implement checkpoints, parser state, suffix reads, changed-source staging,
      atomic merge, and cleanup.
- [ ] Prove the unchanged refresh reads no JSONL content and creates no rows.

## Outcome 3: freshness and semantic reuse

- [ ] Write the failing append-then-search journey.
- [ ] Write failing refresh-owner/waiter/heartbeat/recovery tests.
- [ ] Write failing vector-reuse, missing-only, resume, and cleanup tests.
- [ ] Write failing named semantic/model/cache/VRAM outcome tests.
- [ ] Implement search freshness and semantic publication boundaries.

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
