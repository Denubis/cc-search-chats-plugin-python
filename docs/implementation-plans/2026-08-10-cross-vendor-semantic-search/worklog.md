# Cross-vendor semantic search worklog

## 2026-08-21 — planning baseline

- Recovered and audited the current request against the native Codex session.
- Accepted design:
  `docs/design-plans/2026-08-10-cross-vendor-semantic-search.md`.
- Working tree: `main` at baseline commit
  `fb76d17ee55c55681dca7043bbefc50b8e7223a2`, package version 2.0.4.
- Initial dirty state for this delivery: accepted design documentation only; no
  implementation, commit, installation, production migration, or prune.
- Existing deterministic baseline before design edits: 581 pytest tests passed;
  Ruff lint, Ruff format check, and ty passed.
- Deployed database evidence used to constrain the design:
  approximately 130 GB total; `message` approximately 53 GB with 19.55 million
  estimated rows but 1,651,678 rows in selected corpus revision 14;
  `physical_alias` approximately 16 GB with 19.97 million estimated rows but
  1,682,012 selected rows; `message_embedding` approximately 13 GB with
  2,413,617 rows across ten semantic revisions. Selected semantic revision 9
  has 270,012 vectors for corpus revision 13; revision 10 targets current corpus
  revision 14 but has zero vectors.
- Native input evidence: standard Claude/Codex roots plus present isolated
  Ponytail session roots are in scope. Only their session corpora are readable;
  Ponytail configuration/runtime isolation is preserved.
- Explicit exclusion: message attribution, receipt correlation, authorship
  classification, and their quarantine schema.
- The former `delivery.md` was retired because it prescribed retained full
  revisions and receipt-evidence work, both contrary to the accepted design.
- Current next gate: human assent to implement directly in the current `main`
  working tree with changes left uncommitted.

## 2026-08-22 — execution authorized

- The human instructed `commit, implement`, authorizing a planning-state commit
  followed by implementation in the current `main` working tree.
- No installation, production migration, production prune, push, or publication
  was authorized.

## 2026-08-22 — Outcome 1 normalized storage complete

- Replaced revision-owned active copies with canonical `message_current` and
  `physical_alias_current` relations. Unchanged corpus and alias rows retain
  their PostgreSQL row versions; an unchanged index creates no generation.
- Added small corpus/semantic generation metadata and digest-keyed reusable
  `embedding_value` rows with current message mappings. An appended corpus
  preserves unchanged mapping row versions and embeds only missing text
  digests.
- Added an ordered SHA-256 migration ledger. Reapplication is idempotent,
  changed applied bytes are rejected, and a missing later migration rolls back
  without advancing the ledger.
- Captured selected deployed snapshot IDs without treating snapshot content as
  parser state. The explicit legacy-vector import joins each selected vector to
  text from its own corpus revision, is idempotent, and seeds only the reusable
  pool; it never publishes stale semantic state.
- Added a read-only legacy prune plan reporting exact selected counts and total
  relation allocation. The drop path is statically allowlisted and refuses to
  run without a matching fresh fingerprint plus an accepted current cutover
  validation containing four-corpus positive UAT and a complete semantic join.
- Retired every ordinary PostgreSQL read/write dependency on the legacy
  `message`, `physical_alias`, and `message_embedding` tables. The only remaining
  production references are the explicitly named quarantine inventory/import/
  prune paths.
- Made every PostgreSQL test schema-isolated; this removed a prior semantic test
  dependency on execution order.
- Red evidence included generation `2 != 1` on an unchanged corpus, changed
  `xmin` for an unchanged appended row, and changed `xmin` for a reused semantic
  mapping. Each owning regression passed after the normalized implementation.
- Fresh evidence: `uv run --frozen pytest tests/postgresql -q` reported 21
  passed; `uv run --frozen pytest --ignore=tests/postgresql -q` reported 570
  passed; Ruff lint/format and ty passed. One aggregate `pytest` invocation ran
  the tests but wedged while `pg_ctl` waited during session teardown, so it was
  interrupted and the two test partitions were rerun successfully.

## 2026-08-22 — Outcome 2 root collection begun

- Added stable provider/path-derived root IDs, plural environment variables,
  singular-variable migration compatibility, deterministic deduplication, and
  present standard/Ponytail defaults. Only `projects` or `sessions` roots are
  selected; adjacent isolated configuration files are not.
- Three focused configuration tests pass.
- Blocked before checkpoint implementation by a design contradiction: proving
  an already-committed prefix unchanged requires reading prefix bytes, while the
  accepted append criterion permits reading only bytes after the old complete
  record watermark. Unchanged files can still be guaranteed as metadata-only.

## 2026-08-22 — append-only boundary resolved

- The human selected the fast append-only rule after the two consequences were
  stated explicitly. Same-device/inode growth reads only bytes after the last
  complete-record watermark.
- Truncation, replacement, same-size modification, and parser-state version
  changes still trigger a full reparse or explicit failure. An earlier in-place
  rewrite combined with same-inode growth is outside the detection contract.
- Authority:
  `ccchat:v1:codex:01a0222a-8cf2-7a12-ac38-2c0f471f81b2:id:msg_01a026ca-e241-7722-b606-ab5429dc3c86`.

## 2026-08-22 — Outcome 2 incremental multi-root refresh complete

- Wired the ordered standard and present Ponytail Claude/Codex session roots
  into the PostgreSQL refresh. Provider/path-derived root IDs keep equal
  relative filenames isolated while identical native identities share one
  canonical message and retain distinct physical aliases.
- Added metadata-first discovery and durable per-file checkpoints containing
  device/inode, size, nanosecond mtime, last complete byte, absolute record/line
  coordinates, parser-state version, and provider continuation state.
- An unchanged refresh reads no JSONL content and creates no generation, run,
  message, alias, root-version, or checkpoint-version changes. Valid same-inode
  growth reads only the uncommitted suffix and leaves unchanged message rows at
  their existing PostgreSQL row versions.
- Partial tails remain pending at the last complete newline. Truncation, inode
  replacement, same-size modification, and parser-state upgrades reparse from
  byte zero. A writer advancing during a bounded scan is reported and deferred
  to the next refresh.
- Changed sources stage outside the publication transaction and merge in one
  short transaction. Failed parsing, unsupported conversation records,
  unreadable stats/reads/probes, and publication crashes preserve committed
  messages and checkpoints; removed sources delete only their own aliases.
- Refresh-run diagnostics retain at most 100 terminal records. Run and corpus
  generations retain metadata but no message, alias, or embedding copies.
- Red evidence included an unexpected full-root call boundary, root/checkpoint
  `xmin` changes on a no-op, an unsupported append advancing its revision, a
  stat failure being treated as deletion, an unreadable artifact probe erasing
  committed rows, and an unavailable root replacing last-known metadata.
- Fresh evidence: `uv run --frozen pytest tests/postgresql -q` reported 40
  passed; `uv run --frozen pytest --ignore=tests/postgresql -q` reported 570
  passed; Ruff lint, Ruff format check, and ty all passed.

## 2026-08-23 — Outcome 3 freshness and semantic reuse complete

- PostgreSQL search now runs native metadata discovery and incremental refresh
  before retrieval. Literal search finds a newly appended complete native
  message without a separate `index` command.
- Default hybrid search owns the database index session through corpus refresh,
  missing-vector work, semantic publication, query embedding, and retrieval.
  A second append journey embeds only the new passage and searches the semantic
  generation bound to the newly committed corpus revision.
- Refresh callers wait through the shared advisory owner, emit
  `waiting_for_index`, identify a live refresh run/backend PID when present,
  and wake through PostgreSQL notifications with a five-second heartbeat
  fallback. Direct semantic callers wait on the same owner instead of failing
  fast.
- Refresh and semantic generation rows expose owner PID, phase, heartbeat,
  completed units, and total units. Independent bounded connections advance
  heartbeats during deliberately paused parse/model calls. The next owner marks
  an abandoned building refresh failed; semantic failures remain retryable.
- Semantic publication rechecks the current corpus plus exact eligible/mapped
  cardinality in its transaction. Failed semantic work leaves current literal
  search available and stale semantic search fails explicitly. Successful
  publication reclaims embedding values no longer reachable from current
  mappings.
- Missing model snapshots, dependencies, CUDA, model load, passage/query
  embedding, and terminal VRAM exhaustion retain named phase/code metadata.
  Measurable VRAM is reported, and CLI semantic failure prints the literal text
  `Literal search is required for complete current results` plus a shell-safe
  `search --literal` command preserving the query and filters.
- Red evidence included an append invisible to search, a silent refresh waiter,
  absent owner/heartbeat columns, a non-advancing heartbeat, direct semantic
  fail-fast behavior, three retained unreachable vectors instead of one, stale
  semantic state during hybrid search, generic model-error wrapping, unnamed
  runtime/VRAM failures, and invalid vectors leaving a generation `building`.
- Fresh evidence: `uv run --frozen pytest tests/postgresql -q` reported 47
  passed; `uv run --frozen pytest --ignore=tests/postgresql -q` reported 573
  passed; Ruff lint, Ruff format check, and ty all passed.

## 2026-08-24 — Outcome 4 cross-vendor consumer contract complete

- Unified PostgreSQL `search`, `list`, `extract`, `context`, `resolve`, and
  `index` output under JSON schema version 2. Every command envelope reports a
  terminal status plus coverage, refresh, semantic, and warning state; progress
  is a separate ordered stderr stream with exactly one terminal event.
- Search returns provider-qualified canonical identity and verified physical
  aliases. Ranked hybrid results expose exact reciprocal-rank-fusion inputs;
  ranked limits are 1–200 and component depth is bounded. Deterministic
  exhaustive literal search pages through the database without applying the
  ranked result limit.
- Default search includes visible primary prose only. `--agents` adds agent and
  unknown sessions; `--literal --tools` adds lexical tool name/input/output;
  `--exhaustive` requires literal mode. `--everything` now exits with explicit
  migration guidance. Reasoning, system/developer instructions, injected
  context, and unrecognised shapes remain unavailable.
- Exact resolution verifies native source bytes and distinguishes `resolved`,
  `no_match`, `multiple_matches`, `source_unavailable`, `stale_source`,
  `stale_index`, `malformed_locator`, and `unsupported_provider_schema`.
  `--reference-only` omits message text while retaining verified identity and
  source coordinates.
- Positive coverage controls exercise configured/resolved roots, repositories,
  discovered/read/indexed/skipped/excluded/removed/unreadable files, unknown
  sessions, unrecognised records, watermarks, and partial-versus-complete state.
  Progress covers scan, parse, FTS commit, model preflight/load, embedding,
  semantic commit, query embedding, retrieval, heartbeat, and completion.
- Fresh evidence on the combined Outcome 4 candidate: the non-PostgreSQL
  partition reported 582 passed and 54 deselected; the disposable PostgreSQL 18
  partition reported 54 passed and 582 deselected. Ruff lint, Ruff format check,
  and ty all passed.

## 2026-08-24 — Outcome 4 semantic contract correction

- The preceding Outcome 4 evidence exposed a design-conformance omission before
  handoff: semantic storage still embedded one truncated whole message instead
  of the accepted tokenizer-aware chunk contract. That earlier candidate was
  not installed or migrated in production.
- Added migration 5 with the exact model revision, prefixes, pooling,
  normalization, attention implementation, 1,024 dimensions, chunker identity,
  768 target content tokens, 1,024 hard model-input tokens, and 96-token
  overlap. The retired whole-message mapping is dropped by this migration.
- Added tokenizer-aware, within-message chunks with source/token/character
  bounds and exactly prefixed input digests. Values remain reusable by
  profile/input digest, only missing vectors are embedded, and retrieval keeps
  the best semantic chunk per logical message before RRF.
- Semantic publication now validates dimensions, finiteness, unit norm, current
  source digest, exact chunker identity, chunk/vector coverage, and the selected
  corpus. A wrong-chunker row makes direct semantic retrieval fail and is
  replaced on the next index pass without recomputing an already reusable
  vector.

## 2026-08-24 — Outcome 5 project truth complete

- Rewrote `CLAUDE.md`, README, the search skill, and the command wrapper against
  schema v2, PostgreSQL, search-time refresh, explicit scopes, and standard plus
  Ponytail Claude/Codex session roots. Message attribution remains explicitly
  deferred.
- Added current database architecture and PostgreSQL maintenance/prune guidance.
  The runbook separates inspection, migration, UAT, and prune authority and
  states that no ordinary CLI prune flag exists.
- Added a systemd environment-file seam without embedding source roots,
  credentials, or cache overrides. Added executable guidance checks and a fish
  parser check for the prepared four-corpus UAT script.
- Proposed, but did not write, the only warranted project-note correction:
  change the production-install gate's expected schema from version 1 to
  version 2. Project-note writes still require human agreement.
- Fresh candidate evidence: 586 non-PostgreSQL tests passed with 57 deselected;
  57 disposable-PostgreSQL tests passed with 586 deselected. Every implemented
  CLI help form exited successfully; the stale-claim search found only the
  intentional `--everything` retirement and migration-history references.
