# Incremental refresh and bounded search repair worklog

## 2026-08-29 — accepted repair baseline

- Task worktree:
  `/home/brian/people/Brian/cc-search-chats-plugin-python/.worktrees/incremental-refresh-deadline`.
- Branch `fix/incremental-refresh-deadline` starts from `origin/main` at
  `eba720c51580f7cc4365ba71883e235babebf594`.
- Release 2.0.5 synchronously refreshes before every PostgreSQL search. Four
  searches in another live Codex session produced failed refresh runs 22–25;
  each retried 8,409 failed sources and published no corpus revision.
- Production cleanup required no deletion: no refresh/backend remained active,
  PostgreSQL temp staging vanished with the failed clients, selected corpus
  revision remained 25, and the four small terminal diagnostics were retained
  as evidence. The nightly timer is disabled.
- The installed 2.0.5 currently resolves to release commit
  `eba720c51580f7cc4365ba71883e235babebf594`, not the earlier requested
  `15d94ac181af91b66f4307844728a0f56378c79c`; no replacement install has been
  attempted.
- The accepted design now owns a five-second invocation-to-answer ranked-search
  deadline, literal-first fallback with explicit staleness, a five-minute
  automatic-refresh cooldown, durable systemd background ownership, explicit
  migration, and no pruning in this delivery.
- Authority to finish cleanup and release a working version resolves to
  `ccchat:v1:codex:01a0324d-aec0-7991-88b0-3ad1baedf614:id:msg_01a04cdd-2dd8-7491-9a1d-eba2472fb46a`.

## 2026-08-29 — implementation and mechanical verification

- A read-only host probe found `cc-search-chats-index.service` inactive,
  `cc-search-chats-index.timer` inactive/disabled, and no matching index/search
  process. The new refresh service is not installed in the host user-systemd
  configuration. No production search, index, migration, or service start was
  invoked during implementation.
- Migration 6 adds separate deterministic/transient failed observations,
  truthful attempted-work counters, and one durable automatic-refresh
  singleton. Refresh now checks the migration ledger without mutation;
  `index --migrate` is the explicit DDL owner.
- Ranked search reads literal results, identity resolution, coverage, and the
  reported revision from one repeatable-read snapshot. It never calls native
  refresh or semantic indexing. Query embedding runs in a bounded/reaped child;
  unavailable or late semantic work returns the literal answer.
- After committing the read snapshot, search atomically admits/retries one
  automatic request per five-minute cooldown and asks user systemd to start the
  packaged literal-only oneshot with `--no-block`. Duplicate oneshot activation
  is a successful durable-state no-op. Launch failures retain retry/backoff and
  appear in background state, warnings, and staleness.
- Falsification-first review found and corrected an initial multi-transaction
  search implementation. The regression journey publishes a replacement corpus
  between literal retrieval and resolution and proves the returned result and
  reported revision remain on the original snapshot.
- Fresh evidence on the formatted working tree:
  - `uv run --frozen pytest -q -m 'not postgresql'`: 610 passed, 64 deselected;
  - `uv run --frozen pytest -q -m postgresql`: 64 passed, 610 deselected;
  - Ruff lint and format checks passed over `src tests`;
  - `uv run --frozen ty check src tests` passed;
  - `git diff --check`, packaged CLI help, and Fish syntax validation of the UAT
    command blocks passed.
- Installation, production migration, production smoke checks, four-corpus UAT,
  human acceptance, timer enablement, release normalization, and pruning remain
  unperformed. The timer must stay disabled.

## 2026-08-29 — production migration and parser-compatibility repair

- Candidate `1b7a67107c29e6c8803d1863000ea8a30e073855` passed the complete
  pre-install gate, was installed non-editably from that exact local Git commit
  with the semantic extra, and was verified through `direct_url.json`, import
  path, packaged-resource hashes, and CLI version. The static refresh service
  was installed without enabling or starting it; the nightly timer remains
  disabled and inactive.
- A schema-only production backup was captured before explicit migration 6.
  `index --migrate --json` applied the checksummed migration and an immediate
  repeat proved it idempotent. The live migration ledger is now version 6; no
  prune was requested or run.
- The first literal refresh published revision 26 but truthfully ended partial:
  run 27 discovered 11,082 sources and retained 3,508 deterministic failed
  observations. This exposed provider-compatibility gaps that the synthetic
  fixtures had not represented. Semantic refresh and four-corpus UAT stopped at
  that boundary.
- Red tests now cover the complete set of observed Claude UI metadata and
  legacy flat user/assistant records, Codex lifecycle/inter-agent/non-text
  projections, and native owner identity followed by directly attested copied
  lineage chains. Exact-key allowlists preserve the fail-closed behavior for
  unobserved variants. Parser-state versions advance from 1 to 2 so durable
  checkpoints and failed observations are reparsed from byte zero.
- A read-only structural replay of the complete currently discovered production
  corpus inspected 11,084 native sources and 10,234,600,033 bytes without
  invoking `search` or `index` or writing the database. It found no remaining
  unknown record, content, event, response-item, source-shape, or session-
  identity diagnostic. Its positive controls retained 36 sources containing
  non-scalar Unicode and 3 containing malformed JSON; those 39 sources must
  remain explicit partial coverage rather than be coerced into searchable text.
  Discovery also positively excluded 6,056 non-native transport archives.
- Fresh verification after the repair:
  - `uv run --frozen pytest -q -m 'not postgresql'`: 692 passed, 64 deselected;
  - `uv run --frozen pytest -q -m postgresql`: 64 passed, 692 deselected;
  - Ruff lint/format, ty, `git diff --check`, and packaged CLI help passed.
- Exact parser-repair candidate
  `7ab2faf3f9f66149c71bc33a8bee4d5069f0a68e` was then installed and verified,
  but literal run 28 found one separate canonicalization defect after parsing:
  two occurrences of the same Claude UUID, timestamp, role, epoch, tool-use ID,
  and searchable output differed only in `cwd`. Publication failed atomically;
  selected revision stayed 26, all durable source checkpoints stayed at parser
  version 1, and no semantic work followed.
- A second complete read-only collision audit covered 11,087 native sources and
  10,247,026,633 bytes. It found 583 identical repeated message projections and
  exactly one divergent identity, whose only differing canonical field was
  `cwd`. Red PostgreSQL tests now force that replay across parser batches and
  through both ingestion paths. Canonical conflict detection ignores `cwd`
  alone, retains the earliest deterministic message row plus every physical
  alias, and continues to reject changed searchable content and every other
  canonical field.
- Fresh verification after the canonicalization repair reports 692
  non-PostgreSQL tests and 66 PostgreSQL tests passing, plus green Ruff, format,
  ty, and diff hygiene. The installed tool still resolves to `7ab2faf3...`, not
  this final repair. A new exact commit/install and literal production refresh
  are required before semantic refresh or four-corpus UAT. The timer stays
  disabled; no prune, push, merge, or timer enablement has occurred.

## 2026-08-29 — literal production catch-up and reporting correction

- Exact canonicalization candidate
  `5187b2abdc20ff378e4185b01ab76b884a8261b2` was built, installed offline from
  that Git commit, and verified through direct-URL and packaged-resource hashes.
  The production timer and both index services remained disabled/inactive.
- Literal run 29 completed and selected corpus revision 28. It discovered
  11,088 sources, read and indexed 11,049, retained the 39 reviewed
  deterministic failures, and reported no transient or unrecognized parser
  failures. Semantic state remained untouched and stale.
- A chained catch-up/no-op check then selected revision 29 in run 30 after five
  changed sources. The immediately repeated command created neither a refresh
  run nor a corpus revision, proving the database no-op behavior. It also
  exposed two CLI serialization defects: the no-op envelope reused run 30's
  five-source read counters, and both envelopes called coverage complete despite
  39 blocked sources.
- Red tests reproduce both defects through the PostgreSQL CLI journey and an
  unchanged durable blocked observation. Index serialization now consumes the
  current `RefreshResult`: the no-op reports zero attempted sources/content
  bytes, no run ID, and `state = "unchanged"`. Coverage remains partial whenever
  blocked or transient failures exist; durable pending bytes still come from
  current database state. This corrects the documented schema-v2 meanings and
  does not add a schema or migration.
- Fresh verification after the reporting correction: 692 non-PostgreSQL tests
  and 66 PostgreSQL tests pass; repository-wide Ruff lint/format and ty pass;
  `git diff --check` passes. Production remains installed at `5187b2a...`; the
  reporting correction is not yet committed or installed. Semantic refresh and
  four-corpus UAT remain unperformed. The timer stays disabled; no prune, push,
  merge, or timer enablement has occurred.
