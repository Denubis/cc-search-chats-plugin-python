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
