# Coherent corpus refresh completed work and evidence

## 2026-09-01 — implementation plan grounded

- Accepted design:
  `docs/design-plans/2026-08-10-cross-vendor-semantic-search.md`.
- Working root is the primary `main` checkout at
  `866c3e3fffe77d0be31b38ca71a73e465870d076`, equal to `origin/main` at plan
  creation. The only other worktree is the unrelated dirty
  `message-attribution` checkout, which remains untouched.
- Project-note inventory returned two Markdown records and no excluded Markdown.
  The relevant release-gate note was read completely; its hard-coded schema-v2
  smoke requirement needs a separately approved maintenance change after v3 is
  implemented. The reboot handoff declares itself superseded and does not alter
  this task.
- Current code inspection established four causal boundaries:
  `admit_auto_refresh` gates on request time; the CLI completes automatic state
  after literal refresh; literal and semantic pointers publish separately; and
  ranked search requests refresh only after its result snapshot is complete.
- Current official Psycopg/PostgreSQL contracts establish the race-free wait:
  commit LISTEN, inspect durable state, treat NOTIFY as a wake-up hint, and use a
  bounded notification iterator.
- The human clarified that continuously growing transcripts are normal and the
  system must not recompute on every search. The design now resolves that
  instruction exactly and defines the five-minute quiet period from successful
  full-update completion.
- No implementation, commit, installation, service start, production migration,
  production build, timer change, or prune was performed while writing this
  plan.

## 2026-09-01 — Outcome 1 coherent full-index publication

- Added immutable migration 7, `coherent_corpus_schema.sql`. Migrations 1-6
  retain their exact packaged checksums. The upgrade renames the active schema
  to corpus-generation/semantic-build vocabulary, backfills only a matching
  complete selected pair, clears an inconsistent old selection, removes the
  independent semantic pointer, and constrains selection to one complete
  same-generation build.
- Added `index_corpus` as the composed owner. Native literal deltas and candidate
  chunks remain in temporary projections; reusable vector writes occur outside
  the final publication transaction; one short transaction validates exact
  message/chunk/vector counts, mutates affected current rows, completes the
  generation/build, advances the sole pointer, and emits the commit-time wake
  hint.
- Failure, pause, and eight final-mutation rollback controls prove the old
  literal and semantic corpus remains visible until the complete candidate
  commits. Coherent no-op indexing creates no generation or vector work; a
  missing selection forces one full coherent build. Unchanged message/chunk
  row versions and reusable vectors remain unchanged across append publication.
- Restored the semantic worker's server-prepared materialized batch query,
  per-digest queue deletion, throttled durable progress checkpoints, named
  model failures, retry reuse, and independent database heartbeat against the
  owned semantic build.
- The CLI applies schema version 7, consumes `index_corpus` for manual and
  background full indexing, keeps `--literal-only` diagnostic/non-selecting,
  and rejects `--semantic-only` with composed-index guidance.
- Focused verification:
  `uv run --frozen pytest -q -m postgresql tests/postgresql/test_migrations.py tests/postgresql/test_refresh.py tests/postgresql/test_cross_vendor_index.py tests/postgresql/test_cli_journey.py`
  completed with `67 passed in 15.55s`.
- No commit, installation, service start, production migration, production
  build, timer change, or prune was performed.

## 2026-09-01 — urgent bounded event-export correction

- The installed schema-v2 exporter failed for the audited Sydney window with
  `temporary file size exceeds "temp_file_limit" (65536kB)`. A production
  `EXPLAIN` showed a `GroupAggregate` above a `Gather Merge` sorting the fully
  joined message/alias rows at an estimated width of 498 bytes. The planner
  estimated 9,891 joined rows while the bounded population contained 1,841,687
  content rows.
- Replaced the corpus-sized join/group/order with 1,000-row primary-key keyset
  pages. Python folds adjacent content classes into logical messages, preserves
  metadata-conflict detection and distinct physical-alias counts, then orders
  only retained events by the prior timestamp/identity key.
- The public-boundary regression first failed in the old wide aggregate with
  `temporary file size exceeds "temp_file_limit" (4096kB)`. The implemented
  query passed with 4,000 logical messages and 16,000 content rows under the
  same 4 MiB temporary-file cap. Page-boundary ordering, conflict detection,
  and the existing CLI event journey also pass.
- Production read-only evidence, without installing or migrating, executed the
  exact new row query and fold across 1,841,687 content rows in 19.896 seconds
  under the unchanged 64 MiB guardrail. It observed 1,366,226 logical messages,
  43,588 retained events, and no metadata conflicts. The first 1,000-row page
  used the message and alias primary-key indexes, completed in 15.863 ms, and
  reported no temp I/O. This check did not exercise the public function's v3
  corpus-state lookup because production remains schema v2.
- Focused verification:
  `uv run --frozen pytest -q -m postgresql tests/postgresql/test_events.py tests/postgresql/test_cli_journey.py::test_postgresql_cli_journey_with_events`
  completed with `4 passed in 2.14s`; Ruff lint/format and ty passed for the two
  changed exporter files.
- The complete PostgreSQL partition is not yet green: it completed with 76
  passes and 12 failures in tests that still assert schema version 6, seed the
  renamed `corpus_revision`, or expect literal-only publication. Those are
  pending coherent-corpus consumer updates, not exporter failures.
- No commit, installation, production migration, build, service change, timer
  change, or prune was performed.

## 2026-09-01 — Outcome 2 completion-throttled automatic update

- Automatic admission now requires both a corpus at least five minutes old and
  a successful prior automatic completion at least five minutes old. Pending,
  launching, launched, and running requests block duplicates regardless of
  request age. Launch and full-run failures retain the same request ID with
  bounded exponential retry time; coherent no-op success starts the quiet
  period.
- Ranked search checks freshness before its result transaction. A stale search
  establishes `LISTEN` in autocommit, rereads durable corpus/request state,
  admits or joins one systemd request, and waits only until the one-second
  retrieval/render reserve. Every wake and timeout rereads the durable pointer;
  notification payload never selects a corpus. The repeatable-read result
  snapshot is opened and pinned only after coordination.
- The automatic oneshot now invokes `index --background-refresh`, the same
  composed literal-and-semantic operation as manual full indexing. The old
  post-result launch and automatic literal-only mode were removed.
- Positive journeys cover fresh/quiet suppression, immediate in-budget
  publication selecting the new sentinel, timeout selecting the old sentinel,
  early and duplicate notifications without publication, unavailable systemd,
  one launch under concurrent callers, post-completion cooldown, same-request
  retries, and unchanged embedding-rate thresholds.
- Exact focused verification:
  `uv run --frozen pytest -q -m postgresql tests/postgresql/test_auto_refresh.py tests/postgresql/test_background_search.py`
  completed with `18 passed in 2.63s`; `uv run --frozen pytest -q tests/test_cli.py tests/test_systemd_units.py`
  completed with `83 passed in 3.19s`. Ruff and ty passed for every modified
  Outcome 2 source and test file.
- No commit, installation, production migration, build, service change, timer
  change, or prune was performed.

## 2026-09-01 — Outcome 3 schema-v3 consumers and acceptance preparation

- Cut every PostgreSQL success/error envelope and progress event to schema
  version 3. Public routine-ingest identity is now
  `refresh.corpus_generation`; semantic identity is
  `semantic.semantic_build` plus `semantic.corpus_generation`; corpus time is
  reported once as `indexed_at` and database-computed `corpus_age_ms`. Event
  exports use `source_corpus_generation`. Deprecated revision fields and
  `partial_hybrid` are not emitted.
- Positive all-command journeys now assert the v3 envelope, exact terminal
  progress keys, matching corpus/build identity, event generation identity, and
  human `corpus N` output. The bundled README, project context, architecture,
  maintenance runbook, search skill, and command wrapper now describe coherent
  publication and pre-snapshot stale-search coordination. The runbook requires
  migration 7 followed by one full `index`; it no longer prescribes separate
  literal publication and semantic catch-up.
- Extended the four-root UAT with a second ranked search inside the successful
  post-completion quiet period. The script positively verifies that all four
  native roots grew, the completed request/corpus/build and refresh run remain
  identical, and the systemd invocation timestamp does not change. It waits on
  durable request state and then boundedly waits for the existing systemd
  process to exit; it never starts the oneshot merely to wait for it.
- A falsification-first conformance pass found that a changed automatic request
  was marked complete just after corpus publication. A process death in that
  gap could expose the new corpus while leaving the durable request `running`.
  A new trigger-injected regression first failed at the missing
  `automatic_request_id` boundary. Changed automatic work now completes its
  request inside the same transaction as refresh-run completion and corpus
  selection; failure of that update rolls back the pointer, current rows,
  checkpoints, build, and request state together. The focused rollback,
  automatic-refresh, background-search, and CLI suites passed after the fix.
- Local executable performance falsifiers remain intact: three rate-guard tests
  prove the first five seconds are unobserved, 11.8 passages/s fails against the
  12.0 minimum, and 12.0 passes; the queue test proves one materialization and
  one server-prepared materialized batch query. Its disposable call took 0.09 s,
  which is not comparable to the recorded 4.2 s production baseline. The
  accepted 8.4 s queue and 1 ms production batch-fetch p95 falsifiers remain
  unclaimed until an authorized installed production build/UAT; the existing
  0.075 ms observation is only the prior baseline.
- Final mechanical verification on the completed tree:
  `uv run --frozen pytest -q -m 'not postgresql'` completed with
  `699 passed, 101 deselected in 5.36s`;
  `uv run --frozen pytest -q -m postgresql` completed with
  `101 passed, 699 deselected in 20.49s`; Ruff lint and format, ty, CLI help,
  and `git diff --check` all exited zero. The post-verification UAT/packaging
  check completed with `12 passed in 0.38s`.
- The direct design-conformance review covered AC4 joint publication and
  automatic state, AC5 wait/deadline/age flow, AC7 v3 consumers, and the
  migration/runbook/UAT boundaries. It confirmed and repaired the atomic
  automatic-completion mismatch above. It does not substitute for installed
  production timing, four-root human UAT, or an external reviewer.
- The human approved the exact project-note maintenance proposal. Updated
  `.notes/feedback_main-merge-production-install-gate.md` item 5 to require the
  documented schema-v3 coherent corpus/build identity, age fields, migration 7,
  and one full index before a positive installed smoke can pass. The note still
  grants no commit, installation, migration, build, UAT, timer, or prune
  authority.
- No commit, installation, production migration, production build, service or
  timer change, UAT execution, or prune was performed.
