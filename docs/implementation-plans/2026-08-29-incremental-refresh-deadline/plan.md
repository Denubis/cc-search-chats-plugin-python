# Incremental refresh and bounded search repair plan

**Status:** In progress

**Design authority:**
`docs/design-plans/2026-08-10-cross-vendor-semantic-search.md`

**Working tree:**
`/home/brian/people/Brian/cc-search-chats-plugin-python/.worktrees/incremental-refresh-deadline`

**Integration target:** `origin/main` at
`eba720c51580f7cc4365ba71883e235babebf594`.

The current user request authorizes implementation, truthful cleanup, a working
release, production preflight and migration, and four-corpus UAT without
pruning. It does not authorize enabling the nightly timer before acceptance or
deleting the retained production failure diagnostics. The task-owned worktree
and branch `fix/incremental-refresh-deadline` isolate this delivery.

The completed 2026-08-10 plan is historical evidence. Its Outcome 3 prescribed
synchronous search-time refresh and is superseded only at that boundary by the
current accepted design; it must not be rewritten as if the old behavior had
never shipped.

## Outcome 1: repeat failures become metadata-only no-ops

Persist a failed observation separately from the last successful source
checkpoint. A deterministic failure for the same file identity, size, mtime,
parser-state version, and failure coordinate is not read again until one of
those inputs changes or a manual force-retry explicitly invalidates it.
Transient I/O and concurrent-writer failures retain bounded retry/backoff state
without advancing the successful checkpoint.

### Owned surfaces

- Add migration 6 under `src/cc_search_chats/storage/postgresql/` and register
  its immutable bytes in `migrations.py`.
- Modify `storage/postgresql/refresh.py` and the smallest provider parser
  surfaces needed to classify demonstrated production record shapes.
- Extend `tests/postgresql/test_migrations.py`,
  `tests/postgresql/test_refresh.py`, and provider fixtures/tests.

### Red-green evidence

1. A valid checkpoint followed by a deterministic unsupported append fails
   once, preserves the valid checkpoint and committed messages, and stores the
   failed observation.
2. A second unchanged refresh performs metadata observation but reads zero
   content bytes, creates no corpus revision, and does not repeat the diagnostic
   work.
3. Changed metadata or parser version and explicit force-retry each attempt the
   source again; transient failures retry only after their durable backoff.
4. `refresh_run` reports attempted sources/content bytes even when parsing
   fails, separately from successfully staged sources and committed bytes.
5. Known valid standard and Ponytail Claude/Codex record families parse; an
   actually unsupported conversation-shaped record remains a named failure.

## Outcome 2: search is bounded and refresh is background maintenance

Ranked search establishes a monotonic five-second deadline before importing the
database or semantic stack, reads the selected committed snapshot without
waiting for an index owner, and performs literal retrieval first. Semantic work
may improve the answer only inside the remaining budget. Timeout, unavailable
semantic state, or an in-progress refresh returns literal results with explicit
revision, as-of time, staleness reasons, and background state.

After literal retrieval, search atomically admits at most one automatic refresh
request per five-minute cooldown. A durable user-systemd service owns the
low-priority literal-only incremental refresh and survives the requesting CLI;
search never calls `refresh_native_sources`, runs migrations, waits on the
index admission queue, or spawns an orphan refresh process. Failed launch state
is durable and retryable. `--exhaustive --literal` remains explicitly outside
the ranked deadline.

### Owned surfaces

- Add the minimal pre-import CLI bootstrap and background-refresh admission
  owner; modify `cli.py`, `pyproject.toml`, and PostgreSQL status/query helpers.
- Add and package `systemd/cc-search-chats-refresh.service`; keep the nightly
  timer disabled through acceptance.
- Extend `tests/test_cli.py`, `tests/postgresql/test_cli_journey.py`, refresh
  concurrency tests, and `tests/test_systemd_units.py`.

### Red-green evidence

1. A search with a newly appended native message returns the prior committed
   snapshot promptly, marks it stale, and records one background request rather
   than indexing inline.
2. Repeated searches inside five minutes launch no additional refresh; one
   search after the boundary admits exactly one request under concurrency.
3. A held refresh lock cannot delay literal retrieval. Slow database/semantic
   fakes cross the five-second deadline and still return a truthful literal
   answer or a named deadline error when literal retrieval itself cannot finish.
4. Semantic timeout/unavailability cannot erase literal results or describe
   stale vectors as current.
5. The real systemd unit invokes only the literal incremental owner, applies
   low-priority containment, and records launch/run state visible to search.

## Outcome 3: explicit migration, truthful operations, and working release

Remove implicit schema migration from refresh and search. The operator-owned
explicit migration/preflight path must upgrade a disposable database and then
the production database before the new search or service path is used. Living
CLI, architecture, runbook, plugin, and service documentation must describe the
same boundary.

### Verification and acceptance

1. Run focused red/green owners, both PostgreSQL and non-PostgreSQL suites,
   Ruff lint/format, ty, packaged-resource checks, and a consumer-oriented
   sanity review. `git diff --check` must pass.
2. Build/install only from the exact verified release commit using the normal
   configured uv cache. Verify `direct_url.json`, executable import origin,
   package version, and plugin payload against that commit.
3. Keep the timer disabled. Run explicit production preflight and migration,
   then positive literal and background-refresh smoke checks without pruning.
4. Run positive UAT against standard Claude, Claude Ponytail, standard Codex,
   and Codex Ponytail. Record invocation-to-answer time, committed revision,
   staleness/background fields, and subsequent incremental byte work.
5. Ask the human to judge the finished search interaction. Do not call the
   delivery complete, enable the timer, prune data, or normalize/release history
   before accepted UAT.
