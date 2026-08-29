# PostgreSQL index maintenance

Last verified: 2026-08-29

This runbook separates read-only inspection, candidate migration, production
UAT, and irreversible legacy pruning. Preparing or testing code does not
authorize installation, production migration, UAT, or prune.

## 1. Establish the exact candidate

Before production work, require:

- the intended commit is accepted and present on clean local `main`;
- local `main`, freshly fetched `origin/main`, and the installation target name
  the same exact SHA;
- the CLI is installed non-editably from that SHA with the `semantic` extra;
- installed `direct_url.json` commit ID and imported module path identify that
  installation—not merely the package version;
- both pytest partitions, Ruff lint/format, ty, and `git diff --check` passed on
  the exact candidate.

Stop if any SHA differs or a worktree is dirty. Preserve unrelated user changes.

## 2. Back up and preflight storage

Retain a current PostgreSQL recovery path and all legacy search relations until
after accepted UAT. Record database identity, schema size, selected legacy
generation IDs, and relation sizes before migration.

As a PostgreSQL administrator, inspect placement:

```sql
SELECT current_database(), current_user, version();
SELECT extversion FROM pg_extension WHERE extname = 'vector';
SHOW default_tablespace;
SHOW temp_tablespaces;
SELECT spcname, pg_tablespace_location(oid)
FROM pg_tablespace
ORDER BY spcname;
```

Expected default and temporary tablespaces must resolve below the dedicated
external mount. Separately verify host mount identity/read-only flags,
postgres-account writability through a bounded database probe, and a documented
peak-space margin. Missing, replaced, read-only, unmeasurable, or insufficient
storage blocks migration. Do not create a fallback database, tablespace, cache,
or model location.

Confirm the runtime connection without exposing its password:

```console
pg_isready -d 'service=cc_search_chats'
cc-search-chats index --status --json
```

`index --status` is read-only and may report an older-schema error before
migration. Preserve that error rather than changing production from an
unverified checkout.

## 3. Apply migration without pruning

Only after explicit production-migration authority, use the exact installed
entrypoint. Migration and literal refresh are separate commands; a failed
migration cannot be obscured by refresh output:

```console
cc-search-chats index --migrate --json
cc-search-chats index --literal-only --json
```

The migration result must report `applied_schema_version == 6`. Re-running it is
idempotent. Routine search/index commands must have reported
`maintenance_required` without schema mutation before this explicit step.

Capture each stdout/stderr separately. Stdout must parse as one schema-v2 object.
Stderr must parse as ordered schema-v2 NDJSON ending in exactly one terminal
event. Require positive root/file counts; zero resolved roots is not passing.
Complete coverage passes directly. Partial coverage may proceed only when every
current failure is reconciled to a reviewed deterministic native-source defect,
the exact expected blocked-source count is recorded in the UAT evidence, and
transient failures are zero. Never coerce malformed native bytes or mark the
coverage complete manually.

Repeat the literal command. An unchanged run must report no changed source and
create no corpus generation or current-row version changes. Then run the
prepared four-corpus literal, exact-resolution, and append cases.

## 4. Build semantic state

The pinned snapshot and dependencies must already exist in operator-configured
locations. Runtime remains offline and does not redirect caches.

```console
cc-search-chats index --semantic-only --json
cc-search-chats index --status --json
```

Require terminal success, `semantic.fresh == true`, selected semantic/corpus
agreement, and `completed == total`. Total counts semantic chunks, not logical
messages. Retry may reuse existing chunk vectors but must not create another
vector for the same profile/input digest.

On semantic failure, preserve its phase/code and run a positive literal control.
Literal search must remain available. Ranked hybrid search must return that
literal answer with named degradation and may fuse only digest-valid mappings;
explicit semantic maintenance/status must not claim current completeness.

## 5. Recovery

- **Migration checksum mismatch:** installed code and recorded history disagree.
  Restore the exact matching candidate or add a new migration; never edit
  recorded migration bytes.
- **Partial/unreadable source:** preserve diagnostics/checkpoints, restore source
  availability, and rerun. Do not mark coverage complete manually.
- **Interrupted refresh:** publication rolls back; the next owner diagnoses
  abandoned state and retries changed sources.
- **Interrupted semantic work:** reusable vectors remain; the failed generation
  stays unselected and retry embeds only missing chunk digests.
- **Stale exact source:** refresh and repeat exact resolution. Do not substitute
  a ranked match.
- **Database/storage unavailable:** restore the configured boundary. Do not use
  the legacy local backend or root filesystem as fallback.

## 6. Legacy prune gate

Legacy relations are quarantine, not query state. There is intentionally no
ordinary CLI prune flag. The reviewed library boundary is:

- `plan_legacy_snapshot_prune(connection)` — read-only relation list, selected
  counts, allocated bytes, and fingerprint;
- `prune_legacy_snapshots(connection, expected_fingerprint=...,
  accepted_validation_id=...)` — allowlisted transactional drop.

Before the second function can be invoked, require:

1. explicit human prune authority for the exact installed commit;
2. a fresh reviewed dry-run fingerprint/relation list;
3. current accepted `cutover_validation` with positive `claude`,
   `claude-ponytail`, `codex`, and `codex-ponytail` evidence plus
   `semantic_join: passed`;
4. current message/alias counts equal corpus metadata;
5. the selected semantic generation targets the current corpus, is complete,
   and every current chunk joins one vector.

Only legacy `message_embedding`, `physical_alias`, and `message` snapshot
relations are allowlisted. Native logs, normalized current relations,
generation/crash metadata, and any message-attribution quarantine are outside
the prune path.

The production invocation must be written/reviewed only after prune authority;
this runbook deliberately does not make the destructive API pasteable.

## 7. Post-prune proof

Record removed relations and remaining recovery. Repeat current message, alias,
chunk, vector, generation, foreign-key, semantic-join, relation-size,
freshness/coverage, and all four positive UAT checks. Prune is not complete
until the human accepts the repeated behavior and timing.
