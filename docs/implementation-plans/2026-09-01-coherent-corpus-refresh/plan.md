# Coherent corpus refresh implementation plan

**Status:** Ready for execution

**Design authority:**
`docs/design-plans/2026-08-10-cross-vendor-semantic-search.md`

**Working root:**
`/home/brian/people/Brian/cc-search-chats-plugin-python`

**Integration target:** `main` at
`866c3e3fffe77d0be31b38ca71a73e465870d076`, equal to `origin/main` when this
plan was written.

The working root is already the primary `main` checkout and contains only the
accepted design/UAT edits for this repair. The separate dirty
`message-attribution` worktree is unrelated and must remain untouched. Do not
create another branch or worktree. Because execution would edit the default
branch, obtain the human's explicit assent before the first implementation
edit. Execution may not commit, push, install, migrate production, start a
production build, enable the nightly timer, or prune data without the separate
authority required for each action.

The completed 2026-08-10 plan and the 2026-08-29 repair plan remain historical
evidence. Do not rewrite their worklogs. This plan supersedes the latter only
where it made automatic refresh literal-only, selected literal and semantic
state independently, measured cooldown from request admission, and exposed the
schema-v2 revision vocabulary.

## Scope and acceptance ownership

This plan repairs the accepted design's current AC4, AC5, and AC7 boundaries:

- **AC4:** one full literal-and-semantic update owner; candidate work remains
  invisible; one final transaction publishes a coherent corpus; automatic
  updates have a five-minute post-completion quiet period.
- **AC5:** ranked search may wait for that publication only inside one
  five-second request-to-answer deadline and reports the exact completed corpus,
  age, and continuing update state.
- **AC7:** every command and progress consumer cuts over atomically to JSON
  schema version 3 and corpus-generation/semantic-build vocabulary.

AC1-AC3, AC6, AC8, and AC8a remain regression boundaries. This repair must not
change source parsing, identity, searchable content, native-log read-only
ownership, configured cache locations, vector math, or retained-snapshot prune
authority except where the coherent publication transaction necessarily changes
their state names or selection joins.

## Current repository evidence

- `storage/postgresql/refresh.py::_publish_staged_refresh` currently mutates
  `message_current`, aliases, checkpoints, `corpus_state`, and `refresh_run` in
  one literal publication transaction before semantic indexing begins.
- `storage/postgresql/semantic.py::_sync_chunks` mutates
  `semantic_chunk_current` separately, and `_publish_semantic_revision` advances
  a separate `semantic_state` pointer.
- `cli.py::_handle_postgres` marks an automatic request complete immediately
  after `refresh_native_sources`; `index_embeddings` runs afterward.
- `auto_refresh.py::admit_auto_refresh` says completion cooldown in its
  docstring but gates admission on `requested_at`.
- Ranked search opens its read transaction before asking for an automatic
  refresh, never waits for publication, and may report `partial_hybrid` from
  independently aged literal and semantic state.
- `cc-search-chats-refresh.service` invokes
  `index --literal-only --background-refresh`.
- Migrations 1-6 are applied, checksummed history. They are immutable; this
  repair is migration 7.
- The existing parser checkpoints, changed-source temporary tables, one session
  advisory update owner, reusable `embedding_value` pool, five-second bootstrap
  clock, bounded query-embedding child, and five-second/12-passage-per-second
  throughput guard are foundations to preserve.

Psycopg 3.3's `Connection.notifies(timeout=..., stop_after=...)` supplies the
bounded wait primitive. PostgreSQL 18's `LISTEN` contract requires the listener
transaction to commit before durable state is inspected; notifications are
only wake-up hints and become visible after the notifying transaction commits.
Implementation must therefore LISTEN, reread durable publication state, wait,
and reread again rather than treating a notification payload as selection
authority.

References:

- <https://www.psycopg.org/psycopg3/docs/api/connections.html#psycopg.Connection.notifies>
- <https://www.postgresql.org/docs/18/sql-listen.html>
- <https://www.postgresql.org/docs/18/sql-notify.html>

## Target invariants and schema cutover

Migration 7 is a new immutable resource,
`storage/postgresql/coherent_corpus_schema.sql`. It performs the active-schema
naming and selection cutover in one migration:

- rename active `corpus_revision`/`revision_id` state to
  `corpus_generation`/`corpus_generation` and
  `corpus_state.current_corpus_generation`;
- rename active `semantic_revision` state to `semantic_build`, with primary key
  `semantic_build` and owning `corpus_generation`;
- give each completed `corpus_generation` one selected `semantic_build` and
  enforce with a composite foreign key that the build belongs to that corpus;
- add a deferrable constraint trigger on selection so the referenced build must
  have terminal `complete` status and a nonnull completion time at commit;
- remove `semantic_state` as an independently selectable pointer after
  backfilling a selected build only when the old selected semantic row is
  complete and targets the old selected corpus;
- rename dependent active checkpoint, run, validation, and query columns to the
  same corpus/build vocabulary. Legacy-inventory columns that identify
  quarantined pre-normalization snapshot relations remain explicitly legacy;
- leave the selected corpus null on an inconsistent old pair. Search then
  reports no coherent corpus until one explicit full `index` succeeds; migration
  must never bless the mismatched pair.

The database must be able to reject selecting an incomplete semantic build or a
build belonging to another corpus. A selected corpus's joint `completed_at` is
the public `indexed_at`. There is no second current pointer.

Candidate parsing, chunking, model calls, and embedding writes occur outside a
long write transaction while the existing session advisory owner is held.
Changed-source rows stay in connection-local temporary tables. Add temporary
affected-message and candidate-chunk relations so candidate semantic coverage is
the union of:

1. unchanged current messages/chunks referenced in place; and
2. changed or surviving affected messages/chunks derived from the staged alias
   projection.

Only validated reusable vectors may persist before publication. One short final
transaction must revalidate the candidate, apply literal rows/aliases and
checkpoints, replace affected current chunks, complete the matching semantic
build and corpus generation, advance the single corpus pointer, complete the
refresh/automatic request, and issue the publication wake-up. Failure before or
during that transaction leaves the prior pointer, literal rows, chunks, and
checkpoints current; already validated vectors remain reusable.

A successful no-change automatic run creates no corpus generation and performs
no embedding work when the selected corpus is already coherent, but it still
records automatic completion so the next five minutes are quiet. A no-native-
change run must still build and publish a coherent generation when migration or
profile state has left no selected coherent corpus.

`index` is the publishing maintenance operation. The retained explicit
`--literal-only` mode is a non-selecting parser/candidate diagnostic: it may
report literal candidate work but cannot advance `corpus_state` or describe a
hybrid corpus as current. `--semantic-only` must not remain an independent
publication path; schema-v3 CLI guidance directs operators to the composed
`index` operation.

## Changed boundary flow

| Actor | Durable input/output | Ordering and failure rule | Owner |
|---|---|---|---|
| Ranked search | selected corpus time plus `auto_refresh_state` | Fresh searches return immediately. Stale searches LISTEN, then atomically admit or join one request. | Outcome 2 |
| Automatic request | request ID, active state, retry time, `completed_at`, run ID | Any active request blocks duplicates. Success starts five quiet minutes from completion; failure retries the same request after backoff. | Outcome 2 |
| systemd oneshot | one claimed automatic request | Runs the same full `index` composition as manual/nightly maintenance and survives the caller. | Outcome 2 |
| Update owner | temp literal/chunk deltas, reusable vectors, candidate metadata | Holds one session advisory lock but no long write transaction. Semantic failure cannot publish literal work. | Outcome 1 |
| Final publication | message/alias/checkpoint/chunk delta, corpus/build IDs | One transaction validates and advances one corpus pointer; commit emits a wake-up hint. | Outcome 1 |
| Search result | one repeatable-read corpus snapshot | Publication inside budget may be used; otherwise the previous completed corpus is queried with exact age/update state. | Outcomes 2 and 3 |

## Outcome 1: explicit full index publishes one coherent corpus

**Goal:** `cc-search-chats index` stages native deltas, prepares all required
semantic chunks/vectors, and makes both searchable together in one short final
transaction. No observable state can contain a newly selected literal corpus
with an older semantic build.

**Depends on:** accepted design and migrations 1-6.

**Owns:** AC4 joint publication/failure atomicity and the database portion of
the schema-v3 corpus/build vocabulary.

### Files and consumers

- Create `src/cc_search_chats/storage/postgresql/coherent_corpus_schema.sql` —
  immutable migration 7.
- Modify `src/cc_search_chats/storage/postgresql/migrations.py` — register the
  migration and update current-schema/prune joins without changing migrations
  1-6.
- Modify `src/cc_search_chats/storage/postgresql/refresh.py` — separate staging
  from final publication and construct the affected candidate projection.
- Modify `src/cc_search_chats/storage/postgresql/semantic.py` — chunk/embed the
  candidate projection, preserve reusable vectors, and validate one owned build.
- Modify `src/cc_search_chats/storage/postgresql/guardrails.py` and
  `src/cc_search_chats/storage/postgresql/__init__.py` only as needed to expose
  the one composed owner/publication API.
- Modify `src/cc_search_chats/cli.py` — make manual `index` the first real
  consumer; prevent literal-only/semantic-only modes from advancing split state.
- Test `tests/postgresql/test_migrations.py`,
  `tests/postgresql/test_refresh.py`,
  `tests/postgresql/test_cross_vendor_index.py`, and
  `tests/postgresql/test_cli_journey.py`.

### Work

1. Write failing migration tests that upgrade a representative version-6
   schema, prove migrations 1-6 retain their checksums, prove the coherent
   backfill only for a matching complete pair, and exercise the database
   constraints against incomplete/cross-corpus selection.
2. Write failing index journeys that pause or fail after literal staging and
   during semantic work. A concurrent positive literal and semantic control
   must still read the old corpus, and the appended sentinel must remain absent.
3. Refactor the current refresh function into a staged candidate owned by the
   caller connection. Compute affected final messages and candidate chunks
   without copying unchanged corpus rows; keep native parsing and reusable-vector
   writes outside the final transaction.
4. Change semantic indexing to consume that candidate union. Queue only missing
   input digests, retain the current server-prepared batch fetch/delete/checkpoint
   queries, and validate exact eligible-message, chunk, and vector cardinality.
5. Implement the single final publication transaction and publication
   notification. Test rollback at every mutation boundary and successful
   visibility of the new literal and semantic sentinel under one selected
   corpus/build pair.
6. Test coherent no-op behavior: no native change creates no generation or
   vector work when current state is valid; a missing coherent selection forces
   one complete build rather than claiming an unchanged current corpus.
7. Keep `--literal-only` non-selecting and reject independent semantic
   publication with direct composed-index guidance. Update only the CLI tests
   needed to prove those boundaries.

### Verification

- Run: `uv run --frozen pytest -q -m postgresql tests/postgresql/test_migrations.py tests/postgresql/test_refresh.py tests/postgresql/test_cross_vendor_index.py tests/postgresql/test_cli_journey.py`
- Positive signal: migration 7 applies once; failure/pause controls keep the old
  corpus searchable; success exposes the new sentinel through both literal and
  semantic retrieval with one matching corpus/build identity.
- Failure signal: any test observes staged literal/chunk state, advances either
  half independently, copies unchanged corpus rows, loses reusable vectors, or
  accepts an invalid selected build.

### Finished-work implication

None. Transaction and visibility claims are deterministic PostgreSQL behavior.

## Outcome 2: stale ranked search admits or joins one bounded full update

**Goal:** A ranked search whose completed corpus is at least five minutes old
starts or joins one systemd-owned full update, waits only while its one
five-second response budget allows, then searches either the newly published or
previous completed corpus. Completion—not request admission—starts the next
five-minute quiet period.

**Depends on:** Outcome 1's one-pointer publication operation.

**Owns:** the automatic portion of AC4 and the request-deadline/background-state
portion of AC5.

### Files and consumers

- Modify `src/cc_search_chats/storage/postgresql/auto_refresh.py` — completion-
  based single-flight admission and same-request retry.
- Modify `src/cc_search_chats/cli.py` — stale check, race-free LISTEN/state
  reread, bounded wait, post-wait snapshot, and one result budget.
- Modify `src/cc_search_chats/systemd/cc-search-chats-refresh.service` — consume
  `index --background-refresh`, not literal-only indexing.
- Modify `tests/postgresql/test_auto_refresh.py`,
  `tests/postgresql/test_background_search.py`, `tests/test_cli.py`, and
  `tests/test_systemd_units.py`.
- First real consumer: ranked `search`, both default and `--literal`.

### Work

1. Add fake-time/concurrency failures proving an old `requested_at` cannot admit
   a duplicate while a request is pending/running, a just-completed long run is
   quiet for five minutes, exactly one request is admitted after five minutes,
   and a successful no-op completion also resets the clock.
2. Make launch and run failures retain the same request with bounded retry time.
   A later eligible search may reclaim that request; it cannot increment the
   request ID or launch on every query.
3. Before the ranked result transaction, inspect corpus age and automatic state.
   Fresh/quiet searches do not launch. For stale work, establish LISTEN in
   autocommit, reread durable state, admit or join, and launch only a claimed
   request.
4. Wait with Psycopg notifications only until the measured connection,
   retrieval, serialization, and rendering reserve must begin. After every
   wake and at timeout, reread the corpus pointer and automatic state. Open the
   read-only repeatable-read result transaction only after this step.
5. Add positive timing journeys for publication before budget (new corpus),
   publication after budget (old corpus plus running state), lost/duplicate/
   early notification (durable state wins), unavailable systemd (old answer plus
   named warning), and concurrent searches (one request/service launch).
6. Run automatic service completion only after the composed full index has
   succeeded or coherently no-op'd. Preserve the throughput alarm: it remains
   disarmed for the first five seconds, compares measured rate to the desired
   16 passages/s, and fails below 12 passages/s.

### Verification

- Run: `uv run --frozen pytest -q -m postgresql tests/postgresql/test_auto_refresh.py tests/postgresql/test_background_search.py`
- Run: `uv run --frozen pytest -q tests/test_cli.py tests/test_systemd_units.py`
- Positive signal: positive sentinels select the expected old/new corpus at the
  timing boundary; repeated searches share one request; five minutes are
  measured from successful completion; the packaged service runs full index.
- Failure signal: request age admits a duplicate, search exceeds five seconds,
  notification payload selects state without a durable reread, or the service
  completes before semantic publication.

### Finished-work implication

None. Fake clocks, controlled publication, and wall-clock process tests settle
the deterministic boundary.

## Outcome 3: schema-v3 consumers and release evidence tell one story

**Goal:** Every command, progress event, agent wrapper, test, and living
operator document uses the same coherent corpus contract and no public routine-
ingest field is called a revision.

**Depends on:** Outcomes 1 and 2.

**Owns:** AC5 observable state, AC7 schema version 3, living documentation, full
regression/performance evidence, and finished human UAT preparation.

### Files and consumers

- Modify `src/cc_search_chats/cli.py`,
  `src/cc_search_chats/output.py`, and
  `src/cc_search_chats/storage/postgresql/events.py` — common v3 envelope,
  terminal progress, human output, status, and event export.
- Modify `tests/test_cli.py`, `tests/test_output.py`,
  `tests/postgresql/test_cli_journey.py`, and affected PostgreSQL consumer tests.
- Modify `README.md`, `CLAUDE.md`, `docs/architecture/database.md`,
  `docs/runbooks/postgresql-index-maintenance.md`,
  `docs/uat/cross-vendor-search-wip.md`, `skills/search-chat/SKILL.md`, and
  `commands/search-chat.md` after implementation makes them true.
- First real consumers: installed CLI callers and the bundled Claude/Codex
  search workflow.

### Work

1. Start with failing all-command JSON/progress journeys. Cut every PostgreSQL
   command and NDJSON event to `schema_version: 3` together. Replace public
   `revision_id`, `fts_revision`, `semantic_revision`, `snapshot_age_ms`, and
   event `source_revision` with `corpus_generation`, `semantic_build`,
   `corpus_age_ms`, and `source_corpus_generation` as applicable. Do not emit
   deprecated aliases in v3.
2. Make the shared envelope report one `indexed_at`, corpus age, coherent
   semantic build/profile, exact automatic request/run state, and retrieval
   mode. Remove `partial_hybrid`; query-side semantic failure is
   `literal_fallback` from the same corpus.
3. Update human output to say `corpus N`. Check every command and progress
   consumer positively; do not treat absence-only string searches as evidence.
4. Update living architecture, runbook, README, project context, bundled skill,
   command wrapper, and UAT only after their described behavior exists. The
   release runbook must require explicit migration 7 followed by one full
   `index`; it must not prescribe a separate literal publication or semantic
   catch-up.
5. Extend UAT with a second ranked search during the post-completion quiet
   period. It must report the same completed request/corpus and must not start a
   new service run even though the four native roots have continued growing.
6. Preserve and record falsifiers rather than only elapsed anecdotes. The queue
   thresholds below are provisional regression tripwires derived from the one
   production baseline and must be replaced only by recorded evidence:
   - external ranked invocation must finish in less than 5,000 ms;
   - embedding rate is observed only after five seconds, targets 16 passages/s,
     and fails below 12 passages/s;
   - semantic queue materialization occurs once per build. The existing 4.2 s
     production observation is the baseline; more than 8.4 s requires
     investigation before release;
   - the existing 0.075 ms batch-fetch observation is the baseline; a realistic
     production p95 at or above 1 ms, or any per-batch queue rebuild, fails the
     performance gate pending explanation.
7. Run full mechanical gates, then a falsification-first diff review against
   the accepted design. Production installation/migration and UAT remain
   separately authorized actions. Keep the nightly timer disabled and do not
   prune.

### Verification

- Run: `uv run --frozen pytest -q -m 'not postgresql'`
- Run: `uv run --frozen pytest -q -m postgresql`
- Run: `uv run --frozen ruff check src tests`
- Run: `uv run --frozen ruff format --check src tests`
- Run: `uv run --frozen ty check src tests`
- Run: `uv run --frozen cc-search-chats --help`
- Run: `git diff --check`
- Positive signal: both test partitions report passing counts; lint, format,
  type, help, and diff checks exit zero; positive command journeys parse v3 and
  agree on one corpus/build; the independent review finds no design mismatch.
- Failure signal: any command or bundled consumer remains on v2, any public
  routine-ingest revision field remains, any check did not exercise its intended
  surface, or measured timing crosses a stated falsifier.

### Finished-work implication

After all mechanical and operational gates, the human runs the prepared
four-root UAT and judges whether the returned context/ranking is useful and the
age/update explanation is understandable. A falsifier is a missing sentinel,
misleading age or update state, a second automatic run inside the quiet period,
a half-published corpus, or an invocation at/over five seconds. Script success
does not imply human acceptance.

## Project-note maintenance boundary

`.notes/feedback_main-merge-production-install-gate.md` exists and currently
hard-codes schema version 2 in its production smoke gate. Do not edit it without
separate human approval. The exact later proposal is to change item 5 to require
the release's documented schema version—version 3 for this cutover—so future
release work cannot accept the wrong installed output contract. Evidence is the
accepted design's v3 authority row plus the completed all-command v3 journeys.

## Git, production, and recovery boundaries

- Planning and implementation do not authorize a commit. Present the reviewed
  diff and fresh verification before requesting commit authority.
- A later release must follow the main/install provenance note: accepted tree,
  clean `main`, local/remote exact SHA agreement, exact-SHA noneditable uv-tool
  installation with semantic extra, direct-URL/import-path proof, then explicit
  production migration and full build.
- Production migration 7 may temporarily leave no selected coherent corpus when
  the old literal/semantic pair is mismatched. The timer stays disabled; the
  operator immediately runs the authorized full `index`, and search must report
  the unavailable boundary rather than selecting a half.
- A failed candidate is retried from native checkpoints and reusable vectors.
  Never edit applied migrations, invent cache locations, fall back to SQLite,
  enable the timer, or prune legacy relations as recovery.
