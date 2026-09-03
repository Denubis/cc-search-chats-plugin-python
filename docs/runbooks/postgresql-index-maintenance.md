# PostgreSQL index maintenance

Last verified: 2026-09-03

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
entrypoint. Migration is a separate command so a failed migration cannot be
obscured by indexing output:

First run the selected-pair inspection query in §5 and record that both statuses
are `complete`. If either is not, migration 10 clears the selection and the
following `cc-search-chats index` republishes it. Do not run `index --migrate`
while an index run is active on another host sharing the database; same-host
runs are already single-flight.

```console
cc-search-chats index --migrate --json
```

The migration result must report `applied_schema_version == 10`. Re-running it is
idempotent. Routine search/index commands must have reported
`maintenance_required` without schema mutation before this explicit step.

Capture stdout/stderr separately. Stdout must parse as one schema-v4 object.
Stderr must parse as ordered schema-v4 NDJSON ending in exactly one terminal
event.

## 4. Publish one coherent corpus

The pinned snapshot and dependencies must already exist in operator-configured
locations. Runtime remains offline and does not redirect caches.

```console
cc-search-chats index --json
cc-search-chats index --status --json
```

The first index after this release reparses every source from byte zero because
both provider parser-state versions changed; expect a long run. Only messages
whose repaired, filtered embedding-input digest changed are re-embedded. The
nightly timer performs this full reparse if nobody runs it by hand.

Require positive root/file counts; zero resolved roots is not passing. Complete
coverage may include counted, operator-visible skipped records and repaired
records. Partial coverage may proceed only when every current blocking failure
is reconciled to a reviewed deterministic native-source defect, the exact
expected blocked-source count is recorded in the UAT evidence, and transient
failures are zero. Never mark blocking coverage complete manually.

Require terminal success, a positive `refresh.corpus_generation`, a positive
`semantic.semantic_build`, semantic/corpus generation agreement,
`semantic.fresh == true`, and `completed_units == total_units`. Total units count
semantic chunks, not logical messages. The selected corpus generation must own
that completed semantic build. Retry may reuse existing chunk vectors but must
not create another vector for the same profile/input digest.

Repeat `cc-search-chats index --json`. An unchanged run must report no changed
source, retain the same `corpus_generation` and `semantic_build`, and create no
generation or current-row version changes. Then run the prepared four-corpus
literal, exact-resolution, and append cases. `index` has one composition: it
publishes literal and semantic state together.

On candidate semantic failure, preserve its phase/code and verify that the
previous corpus generation and semantic build remain selected. Run a positive
literal control against that prior corpus. Ranked hybrid search must return the
literal answer with named `literal_fallback` degradation if query-time semantic
work is unavailable; status must not claim the failed candidate is current.

## 5. Recovery

- **Migration checksum mismatch:** installed code and recorded history disagree.
  Restore the exact matching candidate or add a new migration; never edit
  recorded migration bytes.
- **Partial/unreadable source:** preserve diagnostics/checkpoints, restore source
  availability, and rerun. Do not mark coverage complete manually.
- **Interrupted coherent update:** publication rolls back, the previous corpus
  remains selected, and the next owner diagnoses abandoned state and retries.
  Reusable vectors remain; retry embeds only missing chunk digests.
- **Incoherent selection rejected (`23514`):** inspect the selected pair without
  changing it:

  ```sql
  SELECT state.current_corpus_generation,
         generation.status AS corpus_generation_status,
         generation.completed_at AS corpus_generation_completed_at,
         generation.semantic_build,
         build.status AS semantic_build_status,
         build.completed_at AS semantic_build_completed_at
  FROM cc_search_chats.corpus_state AS state
  LEFT JOIN cc_search_chats.corpus_generation AS generation
    ON generation.corpus_generation = state.current_corpus_generation
  LEFT JOIN cc_search_chats.semantic_build AS build
    ON (build.semantic_build, build.corpus_generation) =
       (generation.semantic_build, generation.corpus_generation)
  WHERE state.singleton;
  ```

  Never demote, delete, or replace the selected pair by hand, including by
  deleting and reinserting the same build key. Publish a new coherent pair with
  `cc-search-chats index`; after selection moves, the superseded rows are
  ordinary history.
- **Stale exact source:** refresh and repeat exact resolution. Do not substitute
  a ranked match.
- **Database/storage unavailable:** restore the configured boundary. Do not use
  the legacy local backend or root filesystem as fallback.

### Model revision mismatch

The observed revision is the resolved configured model directory's leaf name,
accepted only when it is a 40-character lowercase hexadecimal commit hash. Index
records that revision only when the stored value is literally `unknown`; semantic
search warns without writing in the same case. Any other stored value, including
the schema's seeded revision, is compared as authoritative profile state.

A mismatch requires a full semantic rebuild under a new model profile, but the
current CLI has no supported path to perform that rebuild. Do not edit
`embedding_profile` by hand; schedule and review the rebuild mechanism as a
separate change before changing the pinned model.

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
5. the selected semantic build belongs to the current corpus generation, is complete,
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
