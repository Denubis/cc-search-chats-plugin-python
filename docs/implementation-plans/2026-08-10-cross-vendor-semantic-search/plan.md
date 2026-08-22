# Cross-vendor semantic search implementation plan

**Status:** In progress

**Design authority:**
`docs/design-plans/2026-08-10-cross-vendor-semantic-search.md`

**Working tree:** `/home/brian/people/Brian/cc-search-chats-plugin-python`

**Integration target:** `main` at
`fb76d17ee55c55681dca7043bbefc50b8e7223a2` before documentation edits.
No branch or worktree is required: on 2026-08-22 the human explicitly authorized
committing the accepted planning state and implementing in the current `main`
working tree. That authority does not include installation, production
migration, production pruning, pushing, or publishing.

## Outcome 1: normalized storage and reversible migration

Replace revision-owned full snapshots with one canonical current corpus and one
reusable embedding per profile/input digest, while retaining only bounded
generation and failure metadata. The first real consumer is every PostgreSQL
read and write path; no compatibility layer may continue writing full copies.

### Files

- Modify `src/cc_search_chats/storage/postgresql/schema.sql`.
- Add the smallest ordered migration owner under
  `src/cc_search_chats/storage/postgresql/` after testing the repository's
  preferred module-versus-SQL-file shape.
- Modify `src/cc_search_chats/storage/postgresql/index.py` and
  `src/cc_search_chats/storage/postgresql/semantic.py`.
- Add `tests/postgresql/test_migrations.py`; extend
  `tests/postgresql/test_cross_vendor_index.py` and existing semantic tests.

### Red-green-refactor sequence

1. Add disposable-PostgreSQL tests proving that the current schema multiplies
   unchanged message, alias, and embedding rows across generations. The new
   expected behavior is one current canonical message row, one alias per real
   physical occurrence, and one vector per embedding profile/input digest.
2. Add migration-ledger tests for ordered application, byte/checksum mismatch,
   interrupted migration, repeated application, and explicit schema version.
3. Add candidate-migration tests that import the selected deployed corpus into
   normalized tables, preserve physical aliases, reject constraint/count/digest
   mismatches, and leave legacy snapshot tables untouched.
4. Add semantic migration tests that join selected revision-9 vectors to their
   revision-13 text, import only compatible vectors into the reusable pool, and
   refuse to call them current for corpus revision 14 until coverage is complete.
5. Implement the minimal schema, ledger, canonical upserts, reusable-vector
   keys, generation metadata, diagnostic-retention policy, and separately
   callable prune dry-run. The dry-run names exact candidate relations, selected
   counts, dependencies, and expected reclaimed allocation; it cannot drop.
6. Refactor only under green tests. Remove old full-snapshot write paths only
   after every PostgreSQL consumer uses the normalized relations.

### Evidence

- Positive fixtures retrieve known Claude and Codex records from the normalized
  tables after migration.
- An unchanged index and semantic pass leave generation, message, alias, and
  embedding row counts unchanged.
- Appending one record changes only that source's canonical rows and required
  vectors.
- Failed candidate validation leaves the old selected snapshot queryable.
- Prune dry-run identifies the legacy relations but no test or ordinary command
  drops them.

**Primary acceptance owner:** AC8 and AC8a.

## Outcome 2: incremental multi-root source refresh

Treat source collections, per-file checkpoints, parser continuation state, and
changed-source staging as one refresh boundary. Standard and Ponytail session
corpora are searchable inputs; their configuration, credentials, caches,
plugins, and runtime state remain isolated and unread.

### Files

- Modify `src/cc_search_chats/providers/source_discovery.py`,
  `src/cc_search_chats/storage/postgresql/refresh.py`, provider adapters where
  serialization needs an explicit contract, and `src/cc_search_chats/cli.py`.
- Extend `tests/test_source_discovery.py`, provider tests, and
  `tests/postgresql/test_refresh.py`; add fixtures only for distinct observed
  record shapes.

### Red-green-refactor sequence

1. Add discovery tests for plural `CC_SEARCH_CLAUDE_ROOTS` and
   `CC_SEARCH_CODEX_ROOTS`, platform path-separator parsing, singular-variable
   migration compatibility, deduplication, nonexistent optional defaults, and
   these present defaults:
   `~/.claude/projects`, `~/.claude-ponytail/projects`, `~/.codex/sessions`, and
   `~/.codex-ponytail/sessions`.
2. Prove with positive sentinel files that discovery traverses only the session
   roots and cannot see adjacent configuration, credential, plugin, cache, or
   runtime files.
3. Add root-aware identity tests: checkpoints and physical aliases include an
   internal source-root ID; the public canonical locator stays root-independent;
   identical relative paths under distinct roots cannot collide.
4. Add byte-observation tests for a no-op refresh, a complete append, a partial
   final JSONL record, append during scan, truncation, rotation/replacement,
   same-size modification, unsupported schema, unreadable source, parser-state
   upgrade, and a staged-generation crash/retry. Same-device/inode growth is
   append-only by explicit product decision; it does not re-verify the committed
   prefix.
5. Implement metadata-first discovery, complete-record watermarks, parser-state
   serialization, suffix-only reads, changed-source staging, one short atomic
   merge, obsolete-row collection, and staging cleanup.
6. Verify that an unchanged second refresh reads no JSONL content bytes and
   creates no generation; a valid append begins exactly after the last complete
   record and publishes only after parsing succeeds.

### Evidence

- Known positive standard and Ponytail Claude/Codex fixtures are all discovered,
  classified, staged, and retrievable.
- Negative boundary fixtures remain outside the corpus and appear in coverage
  diagnostics where applicable.
- Instrumented readers assert byte ranges rather than inferring incremental work
  from elapsed time or empty results.
- A source that advances during refresh reports its observed and newer
  watermarks and is never labelled fully current.

**Primary acceptance owner:** AC4. This outcome contributes source-isolation
evidence consumed by the AC1 and AC8 owners.

## Outcome 3: search-time freshness and semantic reuse

Make every search perform the lightweight discovery/refresh decision before
retrieval. Keep committed literal state available through semantic failures,
but never silently serve a stale semantic generation as current.

### Files

- Modify `src/cc_search_chats/storage/postgresql/refresh.py`,
  `src/cc_search_chats/storage/postgresql/semantic.py`,
  `src/cc_search_chats/core/search.py`, and `src/cc_search_chats/cli.py`.
- Extend `tests/postgresql/test_refresh.py`,
  `tests/postgresql/test_cli_journey.py`, and semantic-model tests.

### Red-green-refactor sequence

1. Add a positive journey in which a known appended message becomes searchable
   without a separate manual `index` command.
2. Add cross-process ownership tests for one PostgreSQL refresh owner, readable
   committed data for nonowners, explicit `waiting_for_index` heartbeats, owner
   identity, and takeover after diagnosed abandonment.
3. Add semantic tests for profile/input-digest reuse, only-missing-vector work,
   resumable staging, corpus/semantic generation match, and unreachable-vector
   garbage collection.
4. Add named failure tests for missing dependencies/model files, offline model
   preflight, load/query-embedding/VRAM failures, and semantic refresh failure.
   Assert nonzero machine-readable outcomes, semantic freshness, the literal
   sentence `Literal search is required for complete current results`, and an
   executable literal-search form.
5. Implement the freshness gate, transactional ownership/status records,
   heartbeats, semantic staging/publication, and failure preservation. Runtime
   commands must never download models or redirect configured caches.

### Evidence

- The known appended-message journey returns its locator after search-triggered
  refresh.
- Literal search remains positively usable after induced semantic failure;
  hybrid/semantic search fails rather than reading a mismatched generation.
- Concurrent callers either observe the new committed freshness or an explicit
  wait state; none see staged rows.
- Once the configured model is installed, an offline-marked integration check
  has no network dependency. GPU/model checks remain separately marked and do
  not weaken deterministic default gates.

**Primary acceptance owner:** AC3. This outcome contributes search-boundary and
semantic-failure evidence consumed by the AC4 and AC5 owners.

## Outcome 4: cross-vendor consumer contract

Complete the existing provisional CLI as one contract across search, resolve,
context, extract, list, and reference-only output. This is not authorship work:
vendor conversational role and session kind are retained, while `submitted_by`,
receipt correlation, and message attribution remain untouched.

### Files

- Modify `src/cc_search_chats/core/identity.py`,
  `src/cc_search_chats/core/models.py`, `src/cc_search_chats/output.py`,
  `src/cc_search_chats/core/search.py`, and `src/cc_search_chats/cli.py`.
- Modify provider adapters only where a tested content/session classification is
  missing.
- Extend `tests/test_identity.py`, `tests/test_output.py`, `tests/test_cli.py`,
  `tests/test_provider_claude.py`, `tests/test_provider_codex.py`,
  `tests/postgresql/test_cli_journey.py`, and
  `tests/postgresql/test_resolution_guardrails.py`.
- Update `skills/search-chat/SKILL.md` and `commands/search-chat.md` only after
  their implemented CLI examples pass.

### Red-green-refactor sequence

1. Add shared JSON-v2 contract tests requiring `schema_version`, command,
   identity-bearing results, coverage, refresh state, semantic state, warnings,
   and terminal status while preserving existing field meanings.
2. Add content-boundary tests for primary visible user/assistant prose by
   default, `--agents` for agent/unknown sessions, `--tools` for lexical-only
   tool content, deterministic `--literal --tools --exhaustive`, and an explicit
   migration error for `--everything`. Reasoning, thinking, developer/system
   instructions, injected context, and unrecognised shapes remain excluded.
3. Add exact-resolution tests driven by known locators and native source
   contents—not ranking—for unique, physical duplicate, malformed, unsupported
   schema, unavailable, stale, missing, and ambiguous outcomes.
4. Add positive coverage controls before asserting totals for configured and
   resolved roots, repositories/projects, files discovered/read/indexed/skipped/
   unreadable, unknown session kinds, unrecognised conversation shapes, and
   source watermarks. Partial work cannot report complete coverage.
5. Add progress tests for scan, parse, FTS commit, model preflight/load,
   embedding, semantic commit, query embedding, retrieval, and completion with
   phase, elapsed time, completed/total units where known, freshness, owner, and
   heartbeat. NDJSON/human progress stays on stderr; JSON stdout is one document.
6. Implement the smallest shared identity, output, flag, resolution, coverage,
   progress, and named-error changes that make those tests pass.

### Evidence

- One known natural-language query returns ranked native Claude and Codex prose
  with provider and native session identity; one literal query avoids loading
  the model.
- Agy and transport archive sentinels reachable beside configured roots do not
  enter the corpus.
- Every exact outcome is asserted positively by status/error code and, for the
  unique case, by source-backed message identity.
- JSON parses as one complete document while progress is independently observed
  on stderr.

**Primary acceptance owner:** AC1, AC2, AC5, AC6, and AC7.

## Outcome 5: operator and project truth

Make the living documentation describe the implemented system rather than the
retired Claude-only SQLite design. Documentation follows behavior and tests; it
does not invent planned behavior as current.

### Files

- Rewrite `CLAUDE.md` fully.
- Update `README.md`,
  `src/cc_search_chats/systemd/cc-search-chats-index.service`, and the timer only
  where its tested operating contract changes.
- Add `docs/architecture/database.md` and
  `docs/runbooks/postgresql-index-maintenance.md`.
- Replace the failed-only record in `docs/uat/cross-vendor-search-wip.md` with a
  reproducible acceptance script and append actual results only when run.
- Review project-owned `.notes/` after implementation; propose any durable note
  change to the human before writing it.

### Sequence and evidence

1. Rewrite architecture, data authority, source-root isolation, freshness,
   progress, model/cache, external-storage, migration, backup/rebuild, and prune
   boundaries against the tested implementation.
2. Add systemd tests for plural roots and explicit operator configuration; the
   unit must not silently supply credentials, cache redirects, or fallback
   storage.
3. Verify every documented command against `--help` and a disposable database.
4. Search the rewritten docs for retired claims such as Claude-only, SQLite
   FTS5, zero runtime dependencies, or JIT indexing, then positively verify the
   replacement PostgreSQL/cross-vendor statements.
5. State message attribution, authorship, receipts, rendered archives,
   summaries, and project-note authorship as excluded/deferred.

**Acceptance contribution:** operator-facing evidence consumed by the AC3, AC4,
AC5, AC7, and AC8 owners; this documentation outcome does not re-own them.

## Outcome 6: verification, production cutover, UAT, and gated prune

This outcome has three separate authority boundaries. Mechanical verification
and disposable-PostgreSQL migration tests are authorized implementation work.
Committing/installing requires an explicit user request. Production candidate
migration, real-corpus UAT, and pruning require the release gate and explicit
human acceptance described below.

### Mechanical gates

Run from the working tree without cache overrides:

```text
uv run --frozen pytest
uv run --frozen ruff check .
uv run --frozen ruff format --check .
uv run --frozen ty check
git diff --check
```

Focused red-green loops may use exact test nodes under `tests/` and
`tests/postgresql/`, but the final claim requires the complete gates above.
PostgreSQL tests use the repository's disposable PostgreSQL 18 fixture and
packaged vector extension, never the operator's live database.

### Release and installation gate

1. Present the uncommitted diff and fresh gate results for review.
2. Obtain explicit UAT authorization and, separately, explicit commit authority.
3. If authorized, create the accepted commit and require clean `main` at that
   exact commit.
4. Install that exact commit non-editably and prove the installed executable
   resolves to version/commit evidence for it. The currently deployed baseline
   is version 2.0.4 at `fb76d17ee55c55681dca7043bbefc50b8e7223a2`.
5. Do not test production through a dirty tree, editable install, or mismatched
   global executable.

### Production candidate migration and positive UAT

1. Preflight the dedicated database, role, schema, pgvector extension,
   tablespace path below the configured external-storage mount, mount identity,
   writability, and conservative peak space. Never create a fallback on the root
   filesystem or modify deferred comms-plumbing schemas.
2. Perform the required one-time bounded parse of all configured native roots;
   deployed snapshot rows cannot seed parser continuation state. Keep native
   logs read-only and record before/after hashes or mtimes for known sources.
3. Validate normalized constraints and counts against the old selected literal
   snapshot. Import compatible old selected vectors into the reusable pool, then
   compute only missing current vectors. Preserve legacy tables as quarantine.
4. Run known-positive literal, semantic, exact-resolution, append-refresh, and
   coverage cases for all four corpora: standard Claude, Claude Ponytail,
   standard Codex, and Codex Ponytail. Every case records query/locator,
   provider/root/session, terminal state, freshness, and source-backed expected
   evidence; a zero-result query is not a passing check.
5. Obtain explicit human UAT acceptance of behavior and timing.

### Prune gate

Only after the exact installed commit and human UAT acceptance may the operator
approve the separate prune command. First compare its dry-run relation list,
selected counts, dependencies, and allocation estimate with the candidate
migration record. If authorized, prune only superseded message,
physical-alias, and embedding snapshot relations; do not touch native logs,
generation/crash metadata within retention, or any message-attribution
quarantine. Re-run row counts, relation sizes, freshness, integrity constraints,
and the four positive UAT cases afterward.

**Acceptance contribution:** integrated proof and human UAT for AC1 through
AC8a; the criterion owners remain those in the matrix below.

## Acceptance ownership matrix

| Criterion | Primary outcome | Required positive evidence |
|---|---:|---|
| AC1 | 4 | One ranked query returns known Claude and Codex native prose; exclusion sentinels stay absent with coverage proving they were in view. |
| AC2 | 4 | Known prose/agent/tool fixtures exercise every flag and excluded content class. |
| AC3 | 3 | Exact model/profile math tests and a separately marked installed-model offline run. |
| AC4 | 2 | Instrumented no-byte no-op, suffix-only append, active-writer, and serialization tests. |
| AC5 | 4 | Positive phase/heartbeat stream and named semantic-failure journeys. |
| AC6 | 4 | Source-backed unique locator plus each distinct failure outcome. |
| AC7 | 4 | Parsed JSON-v2 documents and positive coverage controls for each provider/root. |
| AC8 | 1 | Guardrail/migration tests against disposable PostgreSQL and exact external-storage preflight in production. |
| AC8a | 1 | Stable no-op counts, one-record append delta, reusable-vector counts, and quarantine-before-prune proof. |

Deferred authorship classification and independent provenance evidence have no
implementation outcome and no acceptance gate in this plan.
