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
